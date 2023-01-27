import asyncio
import base64
import functools
import hashlib
import json
import logging
import sys

import kopf

import pydantic

import yaml

from easykube import Configuration, ApiError

from kube_custom_resource import CustomResourceRegistry

from . import models, template
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


@kopf.on.event("v1", "secrets", field = "type", value = "cluster.x-k8s.io/secret")
async def handle_kubeconfig_secret_event(type, body, name, namespace, meta, **kwargs):
    """
    Executes whenever an event occurs for a Cluster API secret.

    Looks for kubeconfig secrets and synchronises the corresponding Argo cluster secret.
    """
    # We are only interested in kubeconfig secrets
    if not name.endswith("-kubeconfig"):
        return
    # Get the Cluster API cluster name from the labels
    cluster_name = meta["labels"]["cluster.x-k8s.io/cluster-name"]
    argo_cluster_name = settings.argocd.cluster_template.format(
        namespace = namespace,
        name = cluster_name
    )
    if type == "DELETED":
        # When the kubeconfig secret is deleted, remove the Argo secret
        ekresource = await ek_client.api("v1").resource("secrets")
        _ = await ekresource.delete(
            argo_cluster_name,
            namespace = settings.argocd.namespace
        )
    else:
        # Extract the kubeconfig from the secret and parse it
        kubeconfig_b64 = body["data"]["value"]
        kubeconfig_bytes = base64.b64decode(kubeconfig_b64)
        kubeconfig = yaml.safe_load(kubeconfig_bytes)
        # Get the cluster and user information from the kubeconfig
        context_name = kubeconfig["current-context"]
        context = next(
            c["context"]
            for c in kubeconfig["contexts"]
            if c["name"] == context_name
        )
        cluster = next(
            c["cluster"]
            for c in kubeconfig["clusters"]
            if c["name"] == context["cluster"]
        )
        user = next(
            u["user"]
            for u in kubeconfig["users"]
            if u["name"] == context["user"]
        )
        # Write the secret for Argo CD to pick up
        _ = await ek_client.apply_object(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": argo_cluster_name,
                    "namespace": settings.argocd.namespace,
                    "labels": {
                        "argocd.argoproj.io/secret-type": "cluster",
                    },
                    # Annotate the secret with information about the Cluster API cluster
                    "annotations": {
                        f"{settings.annotation_prefix}/kubeconfig-secret": f"{namespace}/{name}",
                    },
                },
                "type": "Opaque",
                "stringData": {
                    "name": argo_cluster_name,
                    "server": cluster["server"],
                    "config": json.dumps(
                        {
                            "tlsClientConfig": {
                                "caData": cluster["certificate-authority-data"],
                                "certData": user["client-certificate-data"],
                                "keyData": user["client-key-data"],
                            },
                        }
                    ),
                },
            }
        )


@kopf.on.event(
    "v1",
    "secrets",
    labels = { "argocd.argoproj.io/secret-type": "cluster" },
    annotations = { f"{settings.annotation_prefix}/kubeconfig-secret": kopf.PRESENT }
)
async def handle_argocd_cluster_secret_event(type, name, namespace, meta, **kwargs):
    """
    Executes whenever an event occurs for an Argo cluster secret.

    Ensures that the referenced kubeconfig secret still exists and removes it if not.
    """
    # type is None for events that are fired on resume, which is all we want to respond to
    if not type:
        annotation = f"{settings.annotation_prefix}/kubeconfig-secret"
        secret_namespace, secret_name = meta["annotations"][annotation].split("/", maxsplit = 1)
        ekresource = await ek_client.api("v1").resource("secrets")
        try:
            _ = await ekresource.fetch(secret_name, namespace = secret_namespace)
        except ApiError as exc:
            if exc.status_code == 404:
                _ = await ekresource.delete(name, namespace = namespace)
            else:
                raise


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


async def fetch_ref(ref, default_namespace):
    """
    Returns the object that is referred to by a ref.
    """
    resource = await ek_client.api(ref.get("apiVersion", "v1")).resource(ref["kind"])
    return await resource.fetch(ref["name"], namespace = ref.get("namespace", default_namespace))


@addon_handler(kopf.on.create)
# Run when the annotations are updated as well as the spec
# This means that when we change an annotation in response to a configmap or secret being
# changed, the upgrade logic will be executed for the new configmap or secret content
@addon_handler(kopf.on.update, field = "metadata.annotations")
@addon_handler(kopf.on.update, field = "spec")
@addon_handler(kopf.on.resume)
async def handle_addon_updated(addon: api.Addon, **kwargs):
    """
    Executes whenever an addon is created or the annotations or spec of an addon are updated.

    Each addon maps to an Argo Application object targetting the specified cluster.
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
        # Make sure that the cluster owns the addon
        await addon.init_metadata(ek_client, cluster)
        # Ensure that the cluster is in a suitable state
        # For bootstrap addons, just wait for the control plane to be initialised
        # For non-bootstrap addons, wait for the cluster to be ready
        condition = "ControlPlaneInitialized" if addon.spec.bootstrap else "Ready"
        conditions = cluster.get("status", {}).get("conditions", [])
        ready = any(c["type"] == condition and c["status"] == "True" for c in conditions)
        if not ready:
            raise kopf.TemporaryError(
                f"cluster '{addon.spec.cluster_name}' is not ready",
                delay = settings.temporary_error_delay
            )
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
        # Install or update the addon
        await addon.install_or_upgrade(
            template_loader,
            ek_client,
            cluster,
            infra_cluster,
            cloud_identity
        )
    except ApiError as exc:
        if exc.status_code in {404, 409}:
            raise kopf.TemporaryError(str(exc), delay = settings.temporary_error_delay)
        else:
            raise


@addon_handler(kopf.on.delete)
async def handle_addon_deleted(addon: api.Addon, **kwargs):
    """
    Executes whenever an addon is deleted.
    """
    try:
        await addon.uninstall(ek_client)
    except ApiError as exc:
        if exc.status_code in {404, 409}:
            raise kopf.TemporaryError(str(exc), delay = settings.temporary_error_delay)
        else:
            raise


def on_event(*args, **kwargs):
    """
    Decorator that registers an event handler with kopf, with retry + exponential backoff.

    The events will retry until they succeed, unless a permanent error is raised.

    If a temporary error is raised, the delay from that is respected.
    """
    def decorator(func):
        @functools.wraps(func)
        async def handler(name, namespace, **kwargs):
            retries = 0
            while True:
                try:
                    return await func(
                        name = name,
                        namespace = namespace,
                        retries = retries,
                        **kwargs
                    )
                except kopf.PermanentError:
                    raise
                except kopf.TemporaryError as exc:
                    logger.error(
                        f"[{namespace}/{name}] "
                        f"Handler '{func.__name__}' failed temporarily: {str(exc)}"
                    )
                    await asyncio.sleep(exc.delay)
                except Exception:
                    sleep_duration = min(
                        settings.events_retry.backoff * 2 ** retries,
                        settings.events_retry.max_interval
                    )
                    logger.exception(
                        f"[{namespace}/{name}] "
                        f"Handler '{func.__name__}' raised an exception - "
                        f"retrying after {sleep_duration}s"
                    )
                    await asyncio.sleep(sleep_duration)
                retries = retries + 1
        return kopf.on.event(*args, **kwargs)(handler)
    return decorator


def compute_checksum(data):
    """
    Compute the checksum of the given data from a configmap or secret.
    """
    data_str = ";".join(sorted(f"{k}={v}" for k, v in data.items())).encode()
    return hashlib.sha256(data_str).hexdigest()


async def annotate_addon(addon, annotation, value):
    """
    Applies the given annotation to the specified addon if it is not already present.
    """
    existing = addon.metadata.annotations.get(annotation)
    if existing and existing == value:
        return
    ekapi = ek_client.api(addon.api_version)
    resource = await ekapi.resource(addon._meta.plural_name)
    try:
        _ = await resource.patch(
            addon.metadata.name,
            { "metadata": { "annotations": { annotation: value } } },
            namespace = addon.metadata.namespace
        )
    except ApiError as exc:
        # If the annotation doesn't exist, that is fine
        if exc.status_code != 404:
            raise


@on_event("configmap", labels = { settings.watch_label: kopf.PRESENT })
async def handle_configmap_event(type, name, namespace, body, **kwargs):
    """
    Executes every time a configmap is changed.
    """
    if type == "DELETED":
        # If the secret was deleted, indicate that in the annotation
        annotation = "deleted"
    else:
        # Compute the checksum of the configmap
        annotation = compute_checksum(body.get("data", {}))
    # For each addon type, check if there are any addons that depend on the configmap
    for crd in registry:
        preferred_version = next(k for k, v in crd.versions.items() if v.storage)
        ekapi = ek_client.api(f"{crd.api_group}/{preferred_version}")
        resource = await ekapi.resource(crd.plural_name)
        async for addon_data in resource.list(namespace = namespace):
            addon = registry.get_model_instance(addon_data)
            if addon.uses_configmap(name):
                await annotate_addon(
                    addon,
                    f"{settings.configmap_annotation_prefix}/{name}",
                    annotation
                )


@on_event("secret", labels = { settings.watch_label: kopf.PRESENT })
async def handle_secret_event(name, namespace, body, **kwargs):
    """
    Executes every time a secret is changed.
    """
    if type == "DELETED":
        # If the secret was deleted, indicate that in the annotation
        annotation = "deleted"
    else:
        # Compute the checksum of the configmap
        annotation = compute_checksum(body.get("data", {}))
    # For each addon type, check if there are any addons that depend on the secret
    for crd in registry:
        preferred_version = next(k for k, v in crd.versions.items() if v.storage)
        ekapi = ek_client.api(f"{crd.api_group}/{preferred_version}")
        resource = await ekapi.resource(crd.plural_name)
        async for addon_data in resource.list(namespace = namespace):
            addon = registry.get_model_instance(addon_data)
            if addon.uses_secret(name):
                await annotate_addon(
                    addon,
                    f"{settings.secret_annotation_prefix}/{name}",
                    annotation
                )


@on_event(
    settings.argocd.api_version,
    "application",
    annotations = {
        f"{settings.annotation_prefix}/owner-reference": kopf.PRESENT,
    }
)
async def handle_application_event(type, body, annotations, **kwargs):
    """
    Executes every time an Argo application is changed.
    """
    annotation = f"{settings.annotation_prefix}/owner-reference"
    api_group, plural_name, addon_namespace, addon_name = annotations[annotation].split(":")
    ekapi = await ek_client.api_preferred_version(api_group)
    ekresource = await ekapi.resource(plural_name)
    try:
        addon_data = await ekresource.fetch(addon_name, namespace = addon_namespace)
    except ApiError as exc:
        if exc.status_code == 404:
            return
        else:
            raise
    try:
        addon = registry.get_model_instance(addon_data)
    except (KeyError, pydantic.ValidationError) as exc:
        # Not being able to find the model or validate the data is a permanent error
        raise kopf.PermanentError(str(exc))
    # If the addon is not an Argo application, ignore it
    if not isinstance(addon, api.ArgoApplication):
        return
    try:
        if type == "DELETED":
            await addon.argo_application_deleted(ek_client, body)
        else:
            await addon.argo_application_updated(ek_client, body)
    except ApiError as exc:
        if exc.status_code in {404, 409}:
            raise kopf.TemporaryError(str(exc), delay = settings.temporary_error_delay)
        else:
            raise
