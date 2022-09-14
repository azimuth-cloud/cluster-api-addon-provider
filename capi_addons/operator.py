import base64
import contextlib
import functools
import logging
import sys
import tempfile

import kopf

from easykube import Configuration, ApiError, ResourceSpec, resources as k8s

from kube_custom_resource import CustomResourceRegistry

from pyhelm3 import Client as HelmClient
from pyhelm3 import errors as helm_errors

from . import models, template, utils
from .models import v1alpha1 as api
from .config import settings


logger = logging.getLogger(__name__)


# Initialise the template loader
template_loader = template.Loader(settings = settings)


# Initialise an easykube client from the environment
from pydantic.json import pydantic_encoder
ek_client = (
    Configuration
        .from_environment(json_encoder = pydantic_encoder)
        .async_client(default_field_manager = "cluster-api-addons-manager")
)


# Create a registry of custom resources and populate it from the models module
registry = CustomResourceRegistry(settings.api_group, settings.crd_categories)
registry.discover_models(models)


# Create resource specs for the Cluster API resources we need to access
Cluster = ResourceSpec("cluster.x-k8s.io/v1beta1", "clusters", "Cluster", True)


@kopf.on.startup()
async def apply_settings(**kwargs):
    """
    Apply kopf settings and register CRDs.
    """
    kopf_settings = kwargs["settings"]
    kopf_settings.persistence.finalizer = f"{settings.annotation_prefix}/finalizer"
    kopf_settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(
        prefix = settings.annotation_prefix
    )
    kopf_settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(
        prefix = settings.annotation_prefix,
        key = "last-handled-configuration",
    )
    try:
        for crd in registry:
            await ek_client.apply_object(crd.kubernetes_resource())
    except Exception:
        logger.exception("error applying CRDs - exiting")
        sys.exit(1)


@kopf.on.cleanup()
async def on_cleanup(**kwargs):
    """
    Runs on operator shutdown.
    """
    await ek_client.aclose()


def addon_handler(register_fn, **kwargs):
    """
    Decorator that registers a handler with kopf for every addon that is defined.
    """
    def decorator(func):
        @functools.wraps(func)
        async def handler(**handler_kwargs):
            # Get the model instance associated with the Kubernetes resource, making
            # sure to account for nested addon handlers
            if "addon" not in handler_kwargs:
                handler_kwargs["addon"] = registry.get_model_instance(handler_kwargs["body"])
            return await func(**handler_kwargs)
        for crd in registry:
            preferred_version = next(k for k, v in crd.versions.items() if v.storage)
            api_version = f"{crd.api_group}/{preferred_version}"
            handler = register_fn(api_version, crd.kind, **kwargs)(handler)
        return handler
    return decorator


@contextlib.asynccontextmanager
async def clients_for_cluster(kubeconfig_secret):
    """
    Context manager that yields a tuple of (easykube client, Helm client) configured to
    target the given Cluster API cluster.
    """
    # We pass a Helm client that is configured for the target cluster
    with tempfile.NamedTemporaryFile() as kubeconfig:
        kubeconfig_data = base64.b64decode(kubeconfig_secret.data["value"])
        kubeconfig.write(kubeconfig_data)
        kubeconfig.flush()
        # Get an easykube client for the target cluster
        ek_client_target = (
            Configuration
                .from_kubeconfig_data(kubeconfig_data, json_encoder = pydantic_encoder)
                .async_client(default_field_manager = "cluster-api-addons-manager")
        )
        # Get a Helm client for the target cluster
        helm_client = HelmClient(
            default_timeout = settings.helm_client.default_timeout,
            executable = settings.helm_client.executable,
            history_max_revisions = settings.helm_client.history_max_revisions,
            insecure_skip_tls_verify = settings.helm_client.insecure_skip_tls_verify,
            kubeconfig = kubeconfig.name,
            unpack_directory = settings.helm_client.unpack_directory
        )
        # Yield the clients as a tuple
        async with ek_client_target:
            yield (ek_client_target, helm_client)


async def fetch_ref(ref, default_namespace):
    """
    Returns the object that is referred to by a ref.
    """
    resource = await ek_client.api(ref.get("apiVersion", "v1")).resource(ref["kind"])
    return await resource.fetch(ref["name"], namespace = ref.get("namespace", default_namespace))


@addon_handler(kopf.on.create)
# Run when the annotations are updated as well as the spec
# This allows the use of checksum annotations to trigger a reinstall when the content
# of a configmap or secret changes
# Alternatively, the name of the configmap/secret can be changed when the content changes,
# and the corresponding change to the addon spec will trigger the reinstall
@addon_handler(kopf.on.update, field = "metadata.annotations")
@addon_handler(kopf.on.update, field = "spec")
async def handle_addon_updated(addon, **kwargs):
    """
    Executes whenever an addon is created or the annotations or spec of an addon are updated.

    Every type of addon eventually ends up as a Helm release on the target cluster as we
    want release semantics even if the manifests are not rendered by Helm - in particular
    we want to identify and remove resources that are no longer part of the addon.

    For non-Helm addons, this is done by turning the manifests into an "ephemeral chart"
    which is rendered and then disposed.
    """
    try:
        # Make sure the status has been initialised at the earliest possible opportunity
        await addon.init_status(ek_client)
        # Check that the cluster associated with the addon exists
        cluster = await Cluster(ek_client).fetch(
            addon.spec.cluster_name,
            namespace = addon.metadata.namespace
        )
        # Make sure that the cluster owns the addon
        await addon.init_metadata(ek_client, cluster)
        # Ensure that the cluster is in a suitable state
        # For bootstrap addons, just wait for the control plane to be initialised
        # For non-bootstrap addons, wait for the cluster to be ready
        ready = utils.check_condition(
            cluster,
            "ControlPlaneInitialized" if addon.spec.bootstrap else "Ready"
        )
        if not ready:
            raise kopf.TemporaryError(f"cluster '{addon.spec.cluster_name}' is not ready")
        # Get the infra cluster for the CAPI cluster
        infra_cluster = await fetch_ref(
            cluster.spec["infrastructureRef"],
            cluster.metadata.namespace
        )
        # Get the cloud identity for the cluster, if it exists
        # It is made available to templates in case they need to configure access to the host cloud
        id_ref = infra_cluster.spec.get("identityRef")
        if id_ref:
            cloud_identity = await fetch_ref(id_ref, cluster.metadata.namespace)
        else:
            cloud_identity = None
        # The kubeconfig for the cluster is in a secret
        secret = await k8s.Secret(ek_client).fetch(
            f"{cluster.metadata.name}-kubeconfig",
            namespace = cluster.metadata.namespace
        )
        # Get easykube and Helm clients for the target cluster and use them to deploy the addon
        async with clients_for_cluster(secret) as (ek_client_target, helm_client):
            await addon.install_or_upgrade(
                template_loader,
                ek_client,
                ek_client_target,
                helm_client,
                cluster,
                infra_cluster,
                cloud_identity
            )
    # Handle expected errors by converting them to kopf errors
    # This suppresses the stack trace in logs/events
    # Let unexpected errors bubble without suppressing the stack trace so they can be debugged
    except ApiError as exc:
        if exc.status_code == 404:
            raise kopf.TemporaryError(str(exc))
        else:
            raise
    except helm_errors.ConnectionError as exc:
        # Assume connection errors are temporary
        raise kopf.TemporaryError(str(exc))
    except (helm_errors.ChartNotFoundError, helm_errors.FailedToRenderChartError) as exc:
        # These are permanent errors that can only be resolved by changing the spec
        addon.set_phase(api.AddonPhase.FAILED, str(exc))
        await addon.save_status(ek_client)
        raise kopf.PermanentError(str(exc))


@addon_handler(kopf.on.delete)
async def handle_addon_deleted(addon, **kwargs):
    """
    Executes whenever an addon is deleted.
    """
    try:
        cluster = await Cluster(ek_client).fetch(
            addon.spec.cluster_name,
            namespace = addon.metadata.namespace
        )
    except ApiError as exc:
        # If the cluster does not exist, we are done
        if exc.status_code == 404:
            return
        else:
            raise
    else:
        # If the cluster is deleting, there is nothing for us to do
        if cluster.status.phase == "Deleting":
            return
    try:
        secret = await k8s.Secret(ek_client).fetch(
            f"{cluster.metadata.name}-kubeconfig",
            namespace = cluster.metadata.namespace
        )
    except ApiError as exc:
        # If the cluster does not exist, we are done
        if exc.status_code == 404:
            raise kopf.TemporaryError(str(exc))
        else:
            raise
    clients = clients_for_cluster(secret)
    async with clients as (ek_client_target, helm_client):
        try:
            await addon.uninstall(ek_client, ek_client_target, helm_client)
        except helm_errors.ReleaseNotFoundError:
            # If the release doesn't exist, we are done
            return
        except helm_errors.ConnectionError as exc:
            # Assume connection errors are temporary
            raise kopf.TemporaryError(str(exc))
