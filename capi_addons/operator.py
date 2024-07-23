import asyncio
import base64
import contextlib
import functools
import hashlib
import logging
import tempfile
import typing as t

from pydantic.json import pydantic_encoder

from easykube import PRESENT, Configuration, AsyncClient, ApiError, runtime

from kube_custom_resource import runtime_utils

from pyhelm3 import Client as HelmClient
from pyhelm3 import errors as helm_errors

from . import models, template, utils
from .config import settings
from .models import v1alpha1 as api


logger = logging.getLogger(__name__)


# Initialise the template loader
template_loader = template.Loader()


async def fetch_ref(
    client: AsyncClient,
    ref: t.Dict[str, t.Any],
    default_namespace: str
) -> t.Dict[str, t.Any]:
    """
    Returns the object that is referred to by a ref.
    """
    # By default, look for a secret unless otherwise specified
    api_version = ref.get("apiVersion", "v1")
    kind = ref.get("kind", "Secret")
    name = ref["name"]
    namespace = ref.get("namespace", default_namespace)
    resource = await client.api(api_version).resource(kind)
    return await resource.fetch(name, namespace = namespace)


@contextlib.asynccontextmanager
async def clients_for_cluster(
    kubeconfig_secret: t.Dict[str, t.Any]
) -> t.AsyncIterator[t.Tuple[AsyncClient, HelmClient]]:
    """
    Context manager that yields a tuple of (easykube client, Helm client) configured to
    target the given Cluster API cluster.
    """
    # We pass a Helm client that is configured for the target cluster
    with tempfile.NamedTemporaryFile() as kubeconfig:
        kubeconfig_data = base64.b64decode(kubeconfig_secret["data"]["value"])
        kubeconfig.write(kubeconfig_data)
        kubeconfig.flush()
        # Get an easykube client for the target cluster
        ek_client = (
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
        async with ek_client:
            yield (ek_client, helm_client)


async def until_addon_deleted(client: AsyncClient, addon: api.Addon):
    """
    Runs until the given addon is deleted.

    Used as a circuit-breaker to stop an in-progress install/upgrade when an addon is deleted.
    """
    ekapi = client.api(addon.api_version)
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


async def reconcile_addon_normal(
    client: AsyncClient,
    addon: api.Addon,
    logger: logging.Logger
) -> t.Tuple[api.Addon, runtime.Result]:
    """
    Reconciles the given instance.
    """
    try:
        # Make sure the status has been initialised at the earliest possible opportunity
        await addon.init_status(client)
        # Check that the cluster associated with the addon exists
        ekapi = await client.api_preferred_version("cluster.x-k8s.io")
        resource = await ekapi.resource("clusters")
        cluster = await resource.fetch(
            addon.spec.cluster_name,
            namespace = addon.metadata.namespace
        )
        # Get the infra cluster for the CAPI cluster
        infra_cluster = await fetch_ref(
            client,
            cluster.spec["infrastructureRef"],
            cluster.metadata.namespace
        )
        # Get the cloud identity for the infra cluster, if it exists
        # It is made available to templates in case they need to configure access to the cloud
        id_ref = infra_cluster.spec.get("identityRef")
        if id_ref:
            cloud_identity = await fetch_ref(client, id_ref, cluster.metadata.namespace)
        else:
            cloud_identity = None
        # Make sure that the cluster owns the addon and the addon has the required labels
        await addon.init_metadata(client, cluster, cloud_identity)
        # Make sure any referenced configmaps or secrets will be watched
        configmaps = await client.api("v1").resource("configmaps")
        for configmap in addon.list_configmaps():
            await configmaps.patch(
                configmap,
                {"metadata": {"labels": {settings.watch_label: ""}}},
                namespace = addon.metadata.namespace
            )
        secrets = await client.api("v1").resource("secrets")
        for secret in addon.list_secrets():
            await secrets.patch(
                secret,
                {"metadata": {"labels": {settings.watch_label: ""}}},
                namespace = addon.metadata.namespace
            )
        # If the cloud identity is a secret or configmap in the same namespace, watch it
        if (
            cloud_identity and
            cloud_identity["apiVersion"] == "v1" and
            cloud_identity["kind"] in {"ConfigMap", "Secret"} and
            cloud_identity["metadata"]["namespace"] == cluster.metadata.namespace and
            settings.watch_label not in cloud_identity.metadata.get("labels", {})
        ):
            cloud_identity = await client.patch_object(
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
            logger.warn("cluster '%s' is not ready", addon.spec.cluster_name)
            return addon, runtime.Result(True, settings.temporary_error_delay)
        # The kubeconfig for the cluster is in a secret
        secret = await secrets.fetch(
            f"{cluster.metadata.name}-kubeconfig",
            namespace = cluster.metadata.namespace
        )
        # Get easykube and Helm clients for the target cluster and use them to deploy the addon
        async with clients_for_cluster(secret) as (client_target, helm_client):
            # If the addon is deleted while an install or upgrade is in progress, cancel it
            install_upgrade_task = asyncio.create_task(
                addon.install_or_upgrade(
                    template_loader,
                    client,
                    client_target,
                    helm_client,
                    cluster,
                    infra_cluster,
                    cloud_identity
                )
            )
            wait_for_delete_task = asyncio.create_task(until_addon_deleted(client, addon))
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
    # Handle expected errors by converting them to results
    # This suppresses the stack trace in logs/events
    # Let unexpected errors bubble without suppressing the stack trace so they can be debugged
    except ApiError as exc:
        # 404 and 409 are recoverable
        if exc.status_code in {404, 409}:
            logger.warn(str(exc))
            return addon, runtime.Result(True, settings.temporary_error_delay)
        # All other 4xx errors are probably permanent, as they are client errors
        # They can likely only be resolved by changing the spec
        elif 400 <= exc.status_code < 500:
            logger.error(str(exc))
            addon.set_phase(api.AddonPhase.FAILED, str(exc))
            await addon.save_status(client)
            return addon, runtime.Result()
        else:
            raise
    except (
        helm_errors.ConnectionError,
        helm_errors.CommandCancelledError
    ) as exc:
        logger.warn(str(exc))
        return addon, runtime.Result(True, settings.temporary_error_delay)
    except (
        helm_errors.ChartNotFoundError,
        helm_errors.FailedToRenderChartError,
        helm_errors.ResourceAlreadyExistsError,
        helm_errors.InvalidResourceError
    ) as exc:
        # These are permanent errors that can only be resolved by changing the spec
        logger.error(str(exc))
        addon.set_phase(api.AddonPhase.FAILED, str(exc))
        await addon.save_status(client)
        return addon, runtime.Result()
    else:
        return addon, runtime.Result()


async def reconcile_addon_delete(
    client: AsyncClient,
    addon: api.Addon,
    logger: logging.Logger
) -> t.Tuple[api.Addon, runtime.Result]:
    """
    Reconciles the deletion of the given instance.
    """
    try:
        ekapi = await client.api_preferred_version("cluster.x-k8s.io")
        resource = await ekapi.resource("clusters")
        cluster = await resource.fetch(
            addon.spec.cluster_name,
            namespace = addon.metadata.namespace
        )
    except ApiError as exc:
        # If the cluster does not exist, we are done
        if exc.status_code == 404:
            return addon, runtime.Result()
        else:
            raise
    else:
        # If the cluster is deleting, there is nothing for us to do
        if cluster.status.phase == "Deleting":
            return addon, runtime.Result()
    secrets = await client.api("v1").resource("secrets")
    try:
        secret = await secrets.fetch(
            f"{cluster.metadata.name}-kubeconfig",
            namespace = cluster.metadata.namespace
        )
    except ApiError as exc:
        # If the kubeconfig does not exist, we are done
        if exc.status_code == 404:
            return addon, runtime.Result()
        else:
            raise
    async with clients_for_cluster(secret) as (client_target, helm_client):
        try:
            await addon.uninstall(client, client_target, helm_client)
        except ApiError as exc:
            if exc.status_code == 409:
                logger.warn(str(exc))
                return addon, runtime.Result(True, settings.temporary_error_delay)
            else:
                raise
        except helm_errors.ReleaseNotFoundError:
            # If the release doesn't exist, we are done
            return addon, runtime.Result()
        except helm_errors.ConnectionError as exc:
            # Assume connection errors are temporary
            logger.warn(str(exc))
            return addon, runtime.Result(True, settings.temporary_error_delay)
        else:
            return addon, runtime.Result()


async def reconcile_addon(
    model: t.Type[api.Addon],
    client: AsyncClient,
    request: runtime.Request
) -> t.Optional[runtime.Result]:
    """
    Handles a request to reconcile an addon.
    """
    model_logger = runtime_utils.AnnotatedLogger(logger, settings.api_group, model)
    mapper = runtime_utils.Mapper(client, settings.api_group, model)
    addon = await mapper.fetch(request)
    if not addon:
        model_logger.info("Could not find instance", extra = {"instance": request.key})
        return runtime.Result()
    addon_logger = runtime_utils.AnnotatedLogger(logger, settings.api_group, addon)
    if not addon.metadata.deletion_timestamp:
        addon = await mapper.ensure_finalizer(addon, settings.api_group)
        _, result = await reconcile_addon_normal(client, addon, addon_logger)
        return result
    else:
        addon, result = await reconcile_addon_delete(client, addon, addon_logger)
        # If a delete is reconciled without a requeue, assume we can remove the finalizer
        if not result.requeue:
            try:
                await mapper.remove_finalizer(addon, settings.api_group)
            except ApiError as exc:
                if exc.status_code == 409:
                    return runtime.Result(True, settings.temporary_error_delay)
                else:
                    raise
        return result


async def reconcile_config(
    kind: t.Literal["ConfigMap", "Secret"],
    client: AsyncClient,
    request: runtime.Request
) -> t.Optional[runtime.Result]:
    """
    Handles a request to reconcile a configmap or secret by annotating addons that use the
    config with the current checksum of the data.
    """
    resource = await client.api("v1").resource(kind)
    try:
        config = await resource.fetch(request.name, namespace = request.namespace)
    except ApiError as exc:
        if exc.status_code == 404:
            config = None
        else:
            raise
    # Compute the annotation value
    if not config or config.metadata.get("deletionTimestamp"):
        # If the config was deleted, indicate that in the annotation
        annotation_value = "deleted"
    else:
        # Otherwise, compute the checksum of the config data
        data = config.get("data", {})
        data_str = ";".join(sorted(f"{k}={v}" for k, v in data.items())).encode()
        annotation_value = hashlib.sha256(data_str).hexdigest()
    # Calculate the search labels and checksum annotation names
    prefix = (
        settings.configmap_annotation_prefix
        if kind == "ConfigMap"
        else settings.secret_annotation_prefix
    )
    search_labels = {f"{request.name}.{prefix}/uses": PRESENT}
    checksum_annotation = f"{request.name}.{prefix}/checksum"
    # Annotate any addons that depend on the config
    for model in [api.Manifests, api.HelmRelease]:
        ekapi = client.api(f"{settings.api_group}/{model._meta.version}")
        resource = await ekapi.resource(model._meta.plural_name)
        addons = resource.list(labels = search_labels, namespace = request.namespace)
        async for addon in addons:
            # If the annotation is already set to the correct value, we are done
            if addon.metadata.get("annotations", {}).get(checksum_annotation) == annotation_value:
                continue
            # Otherwise, try to patch the annotation for the addon
            try:
                _ = await resource.patch(
                    addon.metadata.name,
                    {"metadata": {"annotations": {checksum_annotation: annotation_value}}},
                    namespace = addon.metadata.namespace
                )
            except ApiError as exc:
                # If the addon got deleted, that is fine
                if exc.status_code != 404:
                    raise
    return runtime.Result()


async def run():
    """
    Runs the operator logic.
    """
    logger.info("Creating Kubernetes client")
    config = Configuration.from_environment(json_encoder = pydantic_encoder)
    client = config.async_client(default_field_manager = settings.easykube_field_manager)

    async with client:
        logger.info("Registering CRDs")
        await runtime_utils.register_crds(
            client,
            settings.api_group,
            models,
            categories = settings.crd_categories
        )

        # Create a manager
        manager = runtime.Manager()

        # Register the controllers
        logger.info("Creating Manifests controller")
        runtime_utils.create_controller_for_model(
            manager,
            settings.api_group,
            api.Manifests,
            functools.partial(reconcile_addon, api.Manifests)
        )
        logger.info("Creating HelmRelease controller")
        runtime_utils.create_controller_for_model(
            manager,
            settings.api_group,
            api.HelmRelease,
            functools.partial(reconcile_addon, api.HelmRelease)
        )
        logger.info("Creating ConfigMap controller")
        manager.create_controller(
            "v1",
            "ConfigMap",
            functools.partial(reconcile_config, "ConfigMap"),
            labels = {settings.watch_label: PRESENT}
        )
        logger.info("Creating Secret controller")
        manager.create_controller(
            "v1",
            "Secret",
            functools.partial(reconcile_config, "Secret"),
            labels = {settings.watch_label: PRESENT}
        )

        # Run the manager
        logger.info("Starting manager")
        await manager.run(client)


def main():
    """
    Launches the operator using asyncio.
    """
    # Configure the logging
    settings.logging.apply()
    # Run the operator using the default loop
    asyncio.run(run())
