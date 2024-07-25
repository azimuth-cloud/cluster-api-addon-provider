import contextlib
from datetime import datetime, timezone
import hashlib
import pathlib
import re
import tempfile
import typing

import yaml

from pydantic import Field

from easykube import ApiError
from easykube.kubernetes.client import AsyncClient
from kube_custom_resource import CustomResource, schema
from pyhelm3 import (
    Client,
    Chart,
    ReleaseRevision,
    ReleaseRevisionStatus,
    ReleaseNotFoundError
)

from ...config import settings
from ...template import Loader


class LifecycleHookAction(str, schema.Enum):
    """
    Enumeration of possible actions to take as part of a lifecycle hook.
    """
    #: Indicates that a patch should be applied to matching resources
    PATCH = "patch"
    #: Indicates that matching resources should be deleted
    DELETE = "delete"
    #: Indicates that matching resources should be restarted
    #: Equivalent to "kubectl rollout restart"
    RESTART = "restart"


class LifecycleHook(schema.BaseModel):
    """
    Model for a single lifecycle hook.
    """
    api_version: schema.constr(pattern =r"^([a-z][a-z0-9\.-]*/)?v[0-9][a-z0-9]*$") = Field(
        ...,
        description = "The API version of the resource(s) to operate on."
    )
    kind: schema.constr(pattern =r"^[a-zA-Z0-9]+$") = Field(
        ...,
        description = "The kind of the resource(s) to operate on."
    )
    name: schema.Optional[schema.constr(pattern =r"^[a-z][a-z0-9-]*$")] = Field(
        None,
        description = (
            "The name of the resource to operate on. "
            "Takes precedence over selector if both are given."
        )
    )
    selector: schema.Dict[str, str] = Field(
        default_factory = dict,
        description = (
            "A selector for the resource(s) to operate on. "
            "If name and selector are both empty, the lifecycle hook does nothing."
        )
    )
    action: LifecycleHookAction = Field(
        ...,
        description = "The action to take for the lifecycle hook."
    )
    options: schema.Dict[str, schema.Any] = Field(
        default_factory = dict,
        description = "Options for the action."
    )

    async def _apply_patch(self, ek_resource, addon, patch_type, patch_data):
        if patch_type == "json":
            patch_func = ek_resource.json_patch
        elif patch_type == "merge":
            patch_func = ek_resource.json_merge_patch
        else:
            patch_func = ek_resource.patch
        if self.name:
            try:
                await patch_func(
                    self.name,
                    patch_data,
                    namespace = addon.spec.target_namespace
                )
            except ApiError as exc:
                if exc.status_code == 404:
                    return
                else:
                    raise
        elif self.selector:
            async for resource in ek_resource.list(
                labels = self.selector,
                namespace = addon.spec.target_namespace
            ):
                await patch_func(
                    resource.metadata.name,
                    patch_data,
                    namespace = resource.metadata.namespace
                )
        else:
            return

    async def _patch(self, ek_resource, addon):
        patch_data = self.options.get("data")
        if not patch_data:
            return
        await self._apply_patch(
            ek_resource,
            addon,
            self.options.get("type", "strategic"),
            patch_data
        )

    async def _delete(self, ek_resource, addon):
        propagation_policy = self.options.get("propagationPolicy", "Background")
        if self.name:
            await ek_resource.delete(
                self.name,
                propagation_policy = propagation_policy,
                namespace = addon.spec.target_namespace
            )
        elif self.selector:
            await ek_resource.delete_all(
                labels = self.selector,
                propagation_policy = propagation_policy,
                namespace = addon.spec.target_namespace
            )

    async def _restart(self, ek_resource, addon):
        if (
            self.api_version != "apps/v1" or
            self.kind not in {"Deployment", "DaemonSet", "StatefulSet"}
        ):
            return
        # We trigger a restart by patching the annotations of the pod template
        await self._apply_patch(
            ek_resource,
            addon,
            "strategic",
            {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                settings.lifecycle_hook_restart_annotation: (
                                    datetime.now(tz = timezone.utc)
                                ),
                            },
                        },
                    },
                },
            }
        )

    async def execute(
        self,
        # easykube client for the target cluster
        ek_client: AsyncClient,
        # The addon that the hook is for
        addon: typing.ForwardRef("Addon")
    ):
        """
        Execute this lifecycle hook for the given addon.
        """
        if not self.name and not self.selector:
            return
        # Get the easykube resource for the specified kind
        # If we can't find it, that is fine
        try:
            ek_resource = await ek_client.api(self.api_version).resource(self.kind)
        except ApiError as exc:
            if exc.status_code == 404:
                return
            else:
                raise
        if self.action == LifecycleHookAction.PATCH:
            await self._patch(ek_resource, addon)
        elif self.action == LifecycleHookAction.DELETE:
            await self._delete(ek_resource, addon)
        elif self.action == LifecycleHookAction.RESTART:
            await self._restart(ek_resource, addon)


class LifecycleHooks(schema.BaseModel):
    """
    Model for defining lifecycle hooks.
    """
    pre_upgrade: typing.List[LifecycleHook] = Field(
        default_factory = list,
        description = "Hooks to be run before the addon is installed or upgraded."
    )
    post_upgrade: typing.List[LifecycleHook] = Field(
        default_factory = list,
        description = "Hooks to be run after the addon is installed or upgraded."
    )
    pre_delete: typing.List[LifecycleHook] = Field(
        default_factory = list,
        description = "Hooks to be run before the addon is deleted."
    )
    post_delete: typing.List[LifecycleHook] = Field(
        default_factory = list,
        description = "Hooks to be run after the addon is deleted."
    )


class AddonSpec(schema.BaseModel):
    """
    Base class for the spec of addon resources.
    """
    cluster_name: schema.constr(min_length = 1) = Field(
        ...,
        description = "The name of the Cluster API cluster that the addon is for."
    )
    bootstrap: bool = Field(
        False,
        description = (
            "Indicates if this addon is part of cluster bootstrapping. "
            "Addons that are part of cluster bootstrapping are executed without "
            "waiting for the cluster to become ready."
        )
    )
    target_namespace: schema.constr(pattern =r"^[a-z0-9-]+$") = Field(
        ...,
        description = "The namespace on the target cluster to make the release in."
    )
    release_name: schema.Optional[schema.constr(pattern =r"^[a-z0-9-]+$")] = Field(
        None,
        description = "The name of the release. Defaults to the name of the resource."
    )
    release_timeout: schema.Optional[schema.IntOrString] = Field(
        None,
        description = (
            "The time to wait for components to become ready. "
            "If not given, the default timeout for the operator will be used."
        )
    )
    lifecycle_hooks: LifecycleHooks = Field(
        default_factory = LifecycleHooks,
        description = "Lifecycle hooks for the addon."
    )


class AddonPhase(str, schema.Enum):
    """
    The phase of the addon.
    """
    #: Indicates that the state of the addon is not known
    UNKNOWN = "Unknown"
    #: Indicates that the addon is waiting to be deployed
    PENDING = "Pending"
    #: Indicates that the addon is taking some action to prepare for an install/upgrade
    PREPARING = "Preparing"
    #: Indicates that the addon deployed successfully
    DEPLOYED = "Deployed"
    #: Indicates that the addon failed to deploy
    FAILED = "Failed"
    #: Indicates that the addon is installing
    INSTALLING = "Installing"
    #: Indicates that the addon is upgrading
    UPGRADING = "Upgrading"
    #: Indicates that the addon is uninstalling
    UNINSTALLING = "Uninstalling"


class AddonResource(schema.BaseModel):
    """
    Reference to a resource on the target cluster.
    """
    api_version: schema.constr(min_length = 1) = Field(
        ...,
        description = "The API version of the resource."
    )
    kind: schema.constr(min_length = 1) = Field(
        ...,
        description = "The kind of the resource."
    )
    name: schema.constr(min_length = 1) = Field(
        ...,
        description = "The name of the resource."
    )
    namespace: str = Field(
        "",
        description = "The namespace of the resource, if present."
    )


class AddonStatus(schema.BaseModel, extra = "allow"):
    """
    The status of an addon.
    """
    phase: AddonPhase = Field(
        AddonPhase.UNKNOWN.value,
        description = "The phase of the addon."
    )
    revision: int = Field(
        0,
        description = "The current revision of the addon."
    )
    resources: typing.List[AddonResource] = Field(
        default_factory = list,
        description = "The resources that were produced for the current revision."
    )
    notes: schema.Optional[str] = Field(
        None,
        description = "The notes from the release."
    )
    failure_message: str = Field(
        "",
        description = "The reason for entering a failed phase."
    )


class Addon(CustomResource, abstract = True):
    """
    Base class for all addon resources.    
    """
    spec: AddonSpec = Field(
        ...,
        description = "The spec for the addon."
    )
    status: AddonStatus = Field(
        default_factory = AddonStatus,
        description = "The status for the addon."
    )

    async def save_status(self, ek_client: AsyncClient):
        """
        Save the status of this addon using the given easykube client.
        """
        ekapi = ek_client.api(self.api_version)
        resource = await ekapi.resource(f"{self._meta.plural_name}/status")
        data = await resource.replace(
            self.metadata.name,
            {
                # Include the resource version for optimistic concurrency
                "metadata": { "resourceVersion": self.metadata.resource_version },
                "status": self.status.model_dump(exclude_defaults = True),
            },
            namespace = self.metadata.namespace
        )
        # Store the new resource version
        self.metadata.resource_version = data["metadata"]["resourceVersion"]

    async def init_metadata(
        self,
        ek_client: AsyncClient,
        cluster: typing.Dict[str, typing.Any],
        cloud_identity: typing.Optional[typing.Dict[str, typing.Any]]
    ):
        previous_labels = dict(self.metadata.labels)
        # Make sure that the cluster owns the addon
        owner_references_updated = self.metadata.add_owner_reference(
            cluster,
            block_owner_deletion = True
        )
        # Add labels that allow us to filter by cluster, release namespace and release name
        release_name = self.spec.release_name or self.metadata.name
        self.metadata.labels.update({
            settings.cluster_label: cluster.metadata.name,
            settings.release_namespace_label: self.spec.target_namespace,
            settings.release_name_label: release_name,
        })
        # If the cloud identity is a configmap or secret in the same namespace, add a label
        # that allows us to filter for it later
        if (
            cloud_identity["apiVersion"] == "v1" and
            cloud_identity["kind"] in {"ConfigMap", "Secret"} and
            cloud_identity["metadata"]["namespace"] == self.metadata.namespace
        ):
            cloud_identity_label_prefix = (
                settings.configmap_annotation_prefix
                if cloud_identity["kind"] == "ConfigMap"
                else settings.secret_annotation_prefix
            )
            cloud_identity_name = cloud_identity["metadata"]["name"]
            cloud_identity_label = f"{cloud_identity_name}.{cloud_identity_label_prefix}/uses"
            self.metadata.labels[cloud_identity_label] = ""
        # Add labels that allow us to filter by the configmaps and secrets that we reference
        self.metadata.labels.update({
            f"{name}.{settings.configmap_annotation_prefix}/uses": ""
            for name in self.list_configmaps()
        })
        self.metadata.labels.update({
            f"{name}.{settings.secret_annotation_prefix}/uses": ""
            for name in self.list_secrets()
        })
        labels_updated = self.metadata.labels != previous_labels
        # Apply the patch if required
        if owner_references_updated or labels_updated:
            ekapi = ek_client.api(self.api_version)
            resource = await ekapi.resource(self._meta.plural_name)
            data = await resource.patch(
                self.metadata.name,
                {
                    "metadata": {
                        "ownerReferences": self.metadata.owner_references,
                        "labels": self.metadata.labels,
                        # Include the resource version for optimistic concurrency
                        "resourceVersion": self.metadata.resource_version,
                    },
                },
                namespace = self.metadata.namespace
            )
            # Store the new resource version
            self.metadata.resource_version = data["metadata"]["resourceVersion"]

    async def init_status(self, ek_client: AsyncClient):
        """
        Initialise the status of the addon if we have not seen it before.
        """
        if self.status.phase == AddonPhase.UNKNOWN:
            self.set_phase(AddonPhase.PENDING)
            await self.save_status(ek_client)

    def set_phase(self, phase: AddonPhase, message: str = ""):
        """
        Set the phase of this addon to the specified phase.
        """
        self.status.phase = phase
        # Reset the message unless the phase is failed
        self.status.failure_message = message if phase == AddonPhase.FAILED else ""

    def list_configmaps(self) -> typing.List[str]:
        """
        Returns a list of configmaps consumed by this addon.
        """
        raise NotImplementedError

    def list_secrets(self) -> typing.List[str]:
        """
        Returns a list of secrets consumed by this addon.
        """
        raise NotImplementedError

    @contextlib.asynccontextmanager
    async def get_chart(
        self,
        # The template loader to use when rendering templates
        template_loader: Loader,
        # easykube client for the management cluster
        ek_client: AsyncClient,
        # Helm client (for the target cluster, but not relevant for chart pulling)
        helm_client: Client,
        # The Cluster API cluster object
        cluster: typing.Dict[str, typing.Any],
        # The Cluster API infrastructure cluster object
        infra_cluster: typing.Dict[str, typing.Any],
        # The cloud identity object, if one exists
        cloud_identity: typing.Optional[typing.Dict[str, typing.Any]]
    ) -> typing.AsyncIterator[Chart]:
        """
        Context manager that yields the chart for this addon.
        """
        raise NotImplementedError

    async def get_values(
        self,
        # The template loader to use when rendering templates
        template_loader: Loader,
        # easykube client for the management cluster
        ek_client: AsyncClient,
        # The Cluster API cluster object
        cluster: typing.Dict[str, typing.Any],
        # The Cluster API infrastructure cluster object
        infra_cluster: typing.Dict[str, typing.Any],
        # The cloud identity object, if one exists
        cloud_identity: typing.Optional[typing.Dict[str, typing.Any]]
    ) -> typing.Dict[str, typing.Any]:
        """
        Returns the values to use with the release.
        """
        return {}

    async def _get_current_revision(
        self,
        ek_client: AsyncClient,
        helm_client: Client
    ) -> ReleaseRevision:
        """
        Returns the current revision for the release.
        
        This method ensures that the install/upgrade is proceedable by correcting
        for the case where the release was interrupted and is stuck in a pending state.
        """
        try:
            current_revision = await helm_client.get_current_revision(
                self.spec.release_name or self.metadata.name,
                namespace = self.spec.target_namespace
            )
        except ReleaseNotFoundError:
            # This condition is an easy one ;-)
            return None
        else:
            if current_revision.status in {
                # If the release is stuck in pending-install, there is nothing to rollback to
                # Instead, we have to uninstall the release and try again
                ReleaseRevisionStatus.PENDING_INSTALL,
                # If the release is stuck in uninstalling, we need to complete the uninstall
                ReleaseRevisionStatus.UNINSTALLING,
            }:
                self.set_phase(AddonPhase.PREPARING)
                await self.save_status(ek_client)
                await current_revision.release.uninstall(
                    timeout = self.spec.release_timeout,
                    wait = True
                )
                return None
            elif current_revision.status in {
                # If the release is stuck in pending-upgrade, we need to rollback to the previous
                # revision before trying the upgrade again
                ReleaseRevisionStatus.PENDING_UPGRADE,
                # For a release stuck in pending-rollback, we need to complete the rollback
                ReleaseRevisionStatus.PENDING_ROLLBACK,
            }:
                self.set_phase(AddonPhase.PREPARING)
                await self.save_status(ek_client)
                return await current_revision.release.rollback(
                    cleanup_on_fail = True,
                    timeout = self.spec.release_timeout,
                    wait = True
                )
            else:
                # All other statuses are proceedable
                return current_revision

    async def _should_install_or_upgrade(
        self,
        current_revision: typing.Optional[ReleaseRevision],
        chart: Chart,
        values: typing.Dict[str, typing.Any]
    ):
        """
        Returns True if an install or upgrade is required based on the given revision,
        chart and values, False otherwise.
        """
        if current_revision:
            # If the current revision was not deployed successfully, always redeploy
            if current_revision.status != ReleaseRevisionStatus.DEPLOYED:
                return True
            # If the chart has changed from the deployed release, we should redeploy
            revision_chart = await current_revision.chart_metadata()
            if revision_chart.name != chart.metadata.name:
                return True
            if revision_chart.version != chart.metadata.version:
                return True
            # If the values have changed from the deployed release, we should redeploy
            revision_values = await current_revision.values()
            if revision_values != values:
                return True
            # If the chart and values are the same, there is nothing to do
            return False
        else:
            # No current revision - install is always required
            return True

    async def install_or_upgrade(
        self,
        # The template loader to use when rendering templates
        template_loader: Loader,
        # easykube client for the management cluster
        ek_client_management: AsyncClient,
        # easykube client for the target cluster
        ek_client_target: AsyncClient,
        # Helm client for the target cluster
        helm_client: Client,
        # The Cluster API cluster object
        cluster: typing.Dict[str, typing.Any],
        # The Cluster API infrastructure cluster object
        infra_cluster: typing.Dict[str, typing.Any],
        # The cloud identity object, if one exists
        cloud_identity: typing.Optional[typing.Dict[str, typing.Any]]
    ):
        """
        Install or upgrade this addon on the target cluster.
        """
        # Before making a new revision, check the current status
        # If a release was interrupted, e.g. by a connection failure or the controller
        # pod being terminated, the release may be in a pending state that we need to correct
        current_revision = await self._get_current_revision(ek_client_management, helm_client)
        # Now we know that the release is proceedable, we can fetch or assemble the chart
        chart_context = self.get_chart(
            template_loader,
            ek_client_management,
            helm_client,
            cluster,
            infra_cluster,
            cloud_identity
        )
        async with chart_context as chart:
            values = await self.get_values(
                template_loader,
                ek_client_management,
                cluster,
                infra_cluster,
                cloud_identity
            )
            should_install_or_upgrade = await self._should_install_or_upgrade(
                current_revision,
                chart,
                values
            )
            if should_install_or_upgrade:
                self.set_phase(
                    AddonPhase.UPGRADING
                    if current_revision
                    else AddonPhase.INSTALLING
                )
                await self.save_status(ek_client_management)
                for hook in self.spec.lifecycle_hooks.pre_upgrade:
                    await hook.execute(ek_client_target, self)
                crds = await chart.crds()
                for crd in crds:
                    await ek_client_target.apply_object(crd, force = True)
                current_revision = await helm_client.install_or_upgrade_release(
                    self.spec.release_name or self.metadata.name,
                    chart,
                    values,
                    cleanup_on_fail = True,
                    namespace = self.spec.target_namespace,
                    # Always reset to the values from the chart then apply our changes on top
                    reset_values = True,
                    timeout = self.spec.release_timeout,
                    wait = True
                )
        if (
            current_revision.status == ReleaseRevisionStatus.DEPLOYED and
            self.status.phase != AddonPhase.DEPLOYED
        ):
            for hook in self.spec.lifecycle_hooks.post_upgrade:
                await hook.execute(ek_client_target, self)
        # Make sure the status matches the current revision
        self.status.revision = current_revision.revision
        if current_revision.status == ReleaseRevisionStatus.DEPLOYED:
            self.set_phase(AddonPhase.DEPLOYED)
        elif current_revision.status == ReleaseRevisionStatus.FAILED:
            self.set_phase(AddonPhase.FAILED)
        else:
            self.set_phase(AddonPhase.UNKNOWN)
        self.status.resources = [
            AddonResource(
                api_version = resource["apiVersion"],
                kind = resource["kind"],
                name = resource["metadata"]["name"],
                namespace = resource["metadata"].get("namespace", "")
            )
            for resource in (await current_revision.resources())
            if resource
        ]
        self.status.notes = current_revision.notes
        await self.save_status(ek_client_management)

    async def uninstall(
        self,
        # easykube client for the management cluster
        ek_client_management: AsyncClient,
        # easykube client for the target cluster
        ek_client_target: AsyncClient,
        # Helm client for the target cluster
        helm_client: Client,
    ):
        """
        Remove this addon from the target cluster.
        """
        self.set_phase(AddonPhase.UNINSTALLING)
        await self.save_status(ek_client_management)
        for hook in self.spec.lifecycle_hooks.pre_delete:
            await hook.execute(ek_client_target, self)
        await helm_client.uninstall_release(
            self.spec.release_name or self.metadata.name,
            namespace = self.spec.target_namespace,
            timeout = self.spec.release_timeout,
            wait = True
        )
        # We leave CRDs behind on purpose
        for hook in self.spec.lifecycle_hooks.post_delete:
            await hook.execute(ek_client_target, self)


class EphemeralChartAddon(Addon, abstract = True):
    """
    Base class for addon that deploy manifests using an ephemeral chart.
    """
    async def get_resources(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        cluster: typing.Dict[str, typing.Any],
        infra_cluster: typing.Dict[str, typing.Any],
        cloud_identity: typing.Optional[typing.Dict[str, typing.Any]]
    ) -> typing.Iterable[typing.Dict[str, typing.Any]]:
        """
        Returns the resources to use to build the ephemeral chart.
        """
        raise NotImplementedError

    @contextlib.asynccontextmanager
    async def get_chart(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        helm_client: Client,
        cluster: typing.Dict[str, typing.Any],
        infra_cluster: typing.Dict[str, typing.Any],
        cloud_identity: typing.Optional[typing.Dict[str, typing.Any]]
    ) -> contextlib.AbstractAsyncContextManager[Chart]:
        # Write the files for the ephemeral chart
        with tempfile.TemporaryDirectory() as chart_directory:
            # Create the directories for the CRDs and templates
            chart_directory = pathlib.Path(chart_directory)
            crds_directory = chart_directory / "crds"
            crds_directory.mkdir(parents = True, exist_ok = True)
            templates_directory = chart_directory / "templates"
            templates_directory.mkdir(parents = True, exist_ok = True)
            # For each resource in the sources, write a separate file in the chart
            # CRDs go in the crds directory, everything else in the templates directory
            # As we go, we also build up a hash that is used in the chart name
            # This allows us to efficiently detect when a redeploy is required
            hasher = hashlib.sha256()
            async for resource in self.get_resources(
                template_loader,
                ek_client,
                cluster,
                infra_cluster,
                cloud_identity
            ):
                filename = "{}_{}_{}_{}.yaml".format(
                    resource["apiVersion"].replace("/", "_"),
                    resource["kind"].lower(),
                    resource["metadata"].get("namespace", ""),
                    resource["metadata"]["name"]
                )
                if resource["kind"] == "CustomResourceDefinition":
                    path = crds_directory / filename
                else:
                    path = templates_directory / filename
                content = yaml.safe_dump(resource)
                # Update the hash with the content of the file
                hasher.update(content.encode())
                # Escape any go template syntax in the resulting document
                # Note that we only need to escape the starting delimiters as ending delimiters
                # are ignored without the corresponding start delimiter
                content = re.sub(r"\{\{\-?", "{{ \"\g<0>\" }}", content)
                with path.open("w") as fh:
                    fh.write(content)
            # Write a minimal chart metadata file so that Helm recognises it as a chart
            chart_file = chart_directory / "Chart.yaml"
            with chart_file.open("w") as fh:
                yaml.safe_dump(
                    {
                        "apiVersion": "v2",
                        "name": self.spec.release_name or self.metadata.name,
                        # Use the first 8 characters of the digest in the version
                        # This is similar to how git short SHAs work
                        "version": f"0.1.0+{hasher.hexdigest()[:8]}",
                    },
                    fh
                )
            yield await helm_client.get_chart(chart_directory)
