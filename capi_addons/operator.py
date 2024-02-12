import asyncio
import base64
import contextlib
import functools
import hashlib
import logging
import sys
import tempfile

import kopf

from easykube import Configuration, ApiError, resources as k8s, PRESENT

from kube_custom_resource import CustomResourceRegistry

from pyhelm3 import Client as HelmClient
from pyhelm3 import errors as helm_errors

from . import models, template, utils
from .models import v1alpha1 as api
from .config import settings


logger = logging.getLogger(__name__)


# Initialise the template loader
template_loader = template.Loader()


# Initialise an easykube client from the environment
from pydantic.json import pydantic_encoder
ek_client = (
    Configuration
        .from_environment(json_encoder = pydantic_encoder)
        .async_client(default_field_manager = settings.easykube_field_manager)
)


# Create a registry of custom resources and populate it from the models module
registry = CustomResourceRegistry(settings.api_group, settings.crd_categories)
registry.discover_models(models)


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
                .async_client(default_field_manager = settings.easykube_field_manager)
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


async def until_deleted(addon):
    """
    Runs until the given addon is deleted.

    Used as a circuit-breaker to stop an in-progress install/upgrade when an addon is deleted.
    """
    ekapi = ek_client.api(addon.api_version)
    resource = await ekapi.resource(addon._meta.plural_name)
    try:
        while True:
            await asyncio.sleep(5)
            addon_obj = await resource.fetch(
                addon.metadata.name,
                namespace = addon.metadata.namespace
            )
            if addon_obj.metadata.get("deletionTimestamp"):
                return
    except asyncio.CancelledError:
        return


@addon_handler(kopf.on.create)
# Run when the annotations are updated as well as the spec
# This means that when we change an annotation in response to a configmap or secret being
# changed, the upgrade logic will be executed for the new configmap or secret content
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
        ekapi = await ek_client.api_preferred_version("cluster.x-k8s.io")
        resource = await ekapi.resource("clusters")
        cluster = await resource.fetch(
            addon.spec.cluster_name,
            namespace = addon.metadata.namespace
        )
        # Get the infra cluster for the CAPI cluster
        infra_cluster = await fetch_ref(
            cluster.spec["infrastructureRef"],
            cluster.metadata.namespace
        )
        # Get the cloud identity for the infra cluster, if it exists
        # It is made available to templates in case they need to configure access to the host cloud
        id_ref = infra_cluster.spec.get("identityRef")
        if id_ref:
            cloud_identity = await fetch_ref(id_ref, cluster.metadata.namespace)
        else:
            cloud_identity = None
        # Make sure that the cluster owns the addon and the addon has the required labels
        await addon.init_metadata(ek_client, cluster, cloud_identity)
        # Make sure any referenced configmaps or secrets will be watched
        configmaps = await ek_client.api("v1").resource("configmaps")
        for configmap in addon.list_configmaps():
            await configmaps.patch(
                configmap,
                {"metadata": {"labels": {settings.watch_label: ""}}},
                namespace = addon.metadata.namespace
            )
        secrets = await ek_client.api("v1").resource("secrets")
        for secret in addon.list_secrets():
            await secrets.patch(
                secret,
                {"metadata": {"labels": {settings.watch_label: ""}}},
                namespace = addon.metadata.namespace
            )
        # Ensure the cloud identity will be watched
        if (
            cloud_identity and
            settings.watch_label not in cloud_identity.metadata.get("labels", {})
        ):
            cloud_identity = await ek_client.patch_object(
                cloud_identity,
                {"metadata": {"labels": {settings.watch_label: ""}}}
            )
        # Ensure that the cluster is in a suitable state to proceed
        # For bootstrap addons, just wait for the control plane to be initialised
        # For non-bootstrap addons, wait for the cluster to be ready
        ready = utils.check_condition(
            cluster,
            "ControlPlaneInitialized" if addon.spec.bootstrap else "Ready"
        )
        if not ready:
            raise kopf.TemporaryError(
                f"cluster '{addon.spec.cluster_name}' is not ready",
                delay = settings.temporary_error_delay
            )
        # The kubeconfig for the cluster is in a secret
        secret = await k8s.Secret(ek_client).fetch(
            f"{cluster.metadata.name}-kubeconfig",
            namespace = cluster.metadata.namespace
        )
        # Get easykube and Helm clients for the target cluster and use them to deploy the addon
        async with clients_for_cluster(secret) as (ek_client_target, helm_client):
            # If the addon is deleted while an install or upgrade is in progress, cancel it
            install_upgrade_task = asyncio.create_task(
                addon.install_or_upgrade(
                    template_loader,
                    ek_client,
                    ek_client_target,
                    helm_client,
                    cluster,
                    infra_cluster,
                    cloud_identity
                )
            )
            wait_for_delete_task = asyncio.create_task(until_deleted(addon))
            done, pending = await asyncio.wait(
                { install_upgrade_task, wait_for_delete_task },
                return_when = asyncio.FIRST_COMPLETED
            )
            # Cancel the pending tasks and give them a chance to clean up
            for task in pending:
                task.cancel()
                try:
                    _ = await task
                except asyncio.CancelledError:
                    continue
            # Ensure that any exceptions from the completed tasks are raised
            for task in done:
                _ = await task
    # Handle expected errors by converting them to kopf errors
    # This suppresses the stack trace in logs/events
    # Let unexpected errors bubble without suppressing the stack trace so they can be debugged
    except ApiError as exc:
        # 404 and 409 are recoverable
        if exc.status_code in {404, 409}:
            raise kopf.TemporaryError(str(exc), delay = settings.temporary_error_delay)
        # All other 4xx errors are probably permanent, as they are client errors
        # They can likely only be resolved by changing the spec
        elif 400 <= exc.status_code < 500:
            addon.set_phase(api.AddonPhase.FAILED, str(exc))
            await addon.save_status(ek_client)
            raise kopf.PermanentError(str(exc))
        else:
            raise
    except (
        helm_errors.ConnectionError,
        helm_errors.CommandCancelledError
     ) as exc:
        # Assume connection errors are temporary
        raise kopf.TemporaryError(str(exc), delay = settings.temporary_error_delay)
    except (
        helm_errors.ChartNotFoundError,
        helm_errors.FailedToRenderChartError,
        helm_errors.ResourceAlreadyExistsError,
        helm_errors.InvalidResourceError
    ) as exc:
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
        ekapi = await ek_client.api_preferred_version("cluster.x-k8s.io")
        resource = await ekapi.resource("clusters")
        cluster = await resource.fetch(
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
            return
        else:
            raise
    clients = clients_for_cluster(secret)
    async with clients as (ek_client_target, helm_client):
        try:
            await addon.uninstall(ek_client, ek_client_target, helm_client)
        except ApiError as exc:
            if exc.status_code == 409:
                raise kopf.TemporaryError(str(exc), delay = settings.temporary_error_delay)
            else:
                raise
        except helm_errors.ReleaseNotFoundError:
            # If the release doesn't exist, we are done
            return
        except helm_errors.ConnectionError as exc:
            # Assume connection errors are temporary
            raise kopf.TemporaryError(str(exc), delay = settings.temporary_error_delay)


def compute_checksum(data):
    """
    Compute the checksum of the given data from a configmap or secret.
    """
    data_str = ";".join(sorted(f"{k}={v}" for k, v in data.items())).encode()
    return hashlib.sha256(data_str).hexdigest()


async def handle_config_event(type, name, namespace, body, annotation_prefix):
    """
    Handles an event for a watched configmap or secret by updating annotations
    using the specified prefix.
    """
    # Compute the annotation value
    if type == "DELETED":
        # If the config was deleted, indicate that in the annotation
        annotation_value = "deleted"
    else:
        # Otherwise, compute the checksum of the config data
        data = body.get("data", {})
        data_str = ";".join(sorted(f"{k}={v}" for k, v in data.items())).encode()
        annotation_value = hashlib.sha256(data_str).hexdigest()
    # Update any addons that depend on the config
    for crd in registry:
        preferred_version = next(k for k, v in crd.versions.items() if v.storage)
        ekapi = ek_client.api(f"{crd.api_group}/{preferred_version}")
        resource = await ekapi.resource(crd.plural_name)
        async for addon_obj in resource.list(
            labels = {f"{annotation_prefix}/{name}": PRESENT},
            namespace = namespace
        ):
            addon = registry.get_model_instance(addon_obj)
            annotation = f"{annotation_prefix}/{name}"
            # If the annotation is already set to the correct value, we are done
            if addon.metadata.annotations.get(annotation) == annotation_value:
                continue
            # Otherwise, try to patch the annotation for the addon
            try:
                _ = await resource.patch(
                    addon.metadata.name,
                    {"metadata": {"annotations": {annotation: annotation_value}}},
                    namespace = addon.metadata.namespace
                )
            except ApiError as exc:
                # If the addon got deleted, that is fine
                if exc.status_code != 404:
                    raise


@kopf.on.event("configmap", labels = { settings.watch_label: kopf.PRESENT })
async def handle_configmap_event(type, name, namespace, body, **kwargs):
    """
    Executes every time a watched configmap is changed.
    """
    await handle_config_event(
        type,
        name,
        namespace,
        body,
        settings.configmap_annotation_prefix
    )


@kopf.on.event("secret", labels = { settings.watch_label: kopf.PRESENT })
async def handle_secret_event(type, name, namespace, body, **kwargs):
    """
    Executes every time a watched secret is changed.
    """
    await handle_config_event(
        type,
        name,
        namespace,
        body,
        settings.secret_annotation_prefix
    )
