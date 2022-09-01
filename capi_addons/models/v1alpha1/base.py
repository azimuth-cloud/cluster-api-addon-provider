import contextlib
import pathlib
import re
import tempfile
import typing

import yaml

from pydantic import Field, constr

from easykube.kubernetes.client import AsyncClient, Resource
from kube_custom_resource import CustomResource, Scope, schema
from pyhelm3 import (
    Client,
    Chart,
    ReleaseRevision,
    ReleaseRevisionStatus,
    ReleaseNotFoundError
)

from ...template import Loader


class AddonSpec(schema.BaseModel):
    """
    Base class for the spec of addon resources.
    """
    cluster_name: constr(min_length = 1) = Field(
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
    target_namespace: constr(regex = r"^[a-z0-9-]+$") = Field(
        ...,
        description = "The namespace on the target cluster to make the release in."
    )
    release_name: typing.Optional[constr(regex = r"^[a-z0-9-]+$")] = Field(
        None,
        description = "The name of the release. Defaults to the name of the resource."
    )
    release_timeout: typing.Optional[schema.IntOrString] = Field(
        None,
        description = (
            "The time to wait for components to become ready. "
            "If not given, the default timeout for the operator will be used."
        )
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
    api_version: constr(min_length = 1) = Field(
        ...,
        description = "The API version of the resource."
    )
    kind: constr(min_length = 1) = Field(
        ...,
        description = "The kind of the resource."
    )
    name: constr(min_length = 1) = Field(
        ...,
        description = "The name of the resource."
    )
    namespace: str = Field(
        "",
        description = "The namespace of the resource, if present."
    )


class AddonStatus(schema.BaseModel):
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

    async def init_metadata(
        self,
        ek_client: AsyncClient,
        cluster: typing.Dict[str, typing.Any]
    ):
        # Apply any required changes
        include_owner_references = self.metadata.add_owner_reference(
            cluster,
            block_owner_deletion = True
        )
        # Generate the patch for the changes
        patch = {}
        if include_owner_references:
            patch["ownerReferences"] = self.metadata.owner_references
        # Apply the patch if required
        if patch:
            resource = Resource(
                ek_client,
                self.api_version,
                self._meta.plural_name,
                self._meta.kind,
                self._meta.scope is Scope.NAMESPACED
            )
            await resource.server_side_apply(
                self.metadata.name,
                { "metadata": patch },
                namespace = self.metadata.namespace
            )

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

    async def save_status(self, ek_client: AsyncClient):
        """
        Save the status of this addon using the given easykube client.
        """
        resource = Resource(
            ek_client,
            self.api_version,
            f"{self._meta.plural_name}/status",
            self._meta.kind,
            self._meta.scope is Scope.NAMESPACED
        )
        await resource.server_side_apply(
            self.metadata.name,
            { "status": self.status.dict() },
            namespace = self.metadata.namespace
        )

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
        infra_cluster: typing.Dict[str, typing.Any]
    ) -> contextlib.AbstractAsyncContextManager[Chart]:
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
        infra_cluster: typing.Dict[str, typing.Any]
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
            # If the current revision was a successful deployment, we only want to deploy
            # an upgrade if the resources change
            return bool(await current_revision.release.simulate_upgrade(chart, values))
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
        infra_cluster: typing.Dict[str, typing.Any]
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
            infra_cluster
        )
        async with chart_context as chart:
            values = await self.get_values(
                template_loader,
                ek_client_management,
                cluster,
                infra_cluster
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
                crds = await chart.crds()
                for crd in crds:
                    await ek_client_target.apply_object(crd)
                current_revision = await helm_client.install_or_upgrade_release(
                    self.spec.release_name or self.metadata.name,
                    chart,
                    values,
                    cleanup_on_fail = True,
                    namespace = self.spec.target_namespace,
                    # We already installed the CRDs, so skip them here
                    skip_crds = True,
                    timeout = self.spec.release_timeout,
                    wait = True
                )
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
        ]
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
        await helm_client.uninstall_release(
            self.spec.release_name or self.metadata.name,
            namespace = self.spec.target_namespace,
            timeout = self.spec.release_timeout,
            wait = True
        )
        # We leave CRDs behind on purpose


class EphemeralChartAddon(Addon, abstract = True):
    """
    Base class for addon that deploy manifests using an ephemeral chart.
    """
    async def get_resources(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        cluster: typing.Dict[str, typing.Any],
        infra_cluster: typing.Dict[str, typing.Any]
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
        infra_cluster: typing.Dict[str, typing.Any]
    ) -> contextlib.AbstractAsyncContextManager[Chart]:
        # Write the files for the ephemeral chart
        with tempfile.TemporaryDirectory() as chart_directory:
            # Create the directories for the CRDs and templates
            chart_directory = pathlib.Path(chart_directory)
            crds_directory = chart_directory / "crds"
            crds_directory.mkdir(parents = True, exist_ok = True)
            templates_directory = chart_directory / "templates"
            templates_directory.mkdir(parents = True, exist_ok = True)
            # Write a minimal chart metadata file so that Helm recognises it as a chart
            chart_file = chart_directory / "Chart.yaml"
            with chart_file.open("w") as fh:
                yaml.safe_dump(
                    {
                        "apiVersion": "v2",
                        "name": self.spec.release_name or self.metadata.name,
                        "version": "0.1.0+generated",
                    },
                    fh
                )
            # For each resource in the sources, write a separate file in the chart
            # CRDs go in the crds directory, everything else in the templates directory
            async for resource in self.get_resources(
                template_loader,
                ek_client,
                cluster,
                infra_cluster
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
                # Escape any go template syntax in the resulting document
                # Note that we only need to escape the starting delimiters as ending delimiters
                # are ignored without the corresponding start delimiter
                content = re.sub(r"\{\{\-?", "{{ \"\g<0>\" }}", content)
                with path.open("w") as fh:
                    fh.write(content)
            yield await helm_client.get_chart(chart_directory)
