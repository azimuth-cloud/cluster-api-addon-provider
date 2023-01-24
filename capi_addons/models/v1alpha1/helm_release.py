import base64
import contextlib
import tempfile
import typing as t

from pydantic import Field, AnyHttpUrl, constr
from pydantic.json import pydantic_encoder

from easykube import Configuration, ApiError
from easykube.kubernetes.client import AsyncClient

from kube_custom_resource import schema

from pyhelm3 import (
    Client as HelmClient,
    ReleaseRevision,
    ReleaseRevisionStatus,
    ReleaseNotFoundError,
    errors as helm_errors
)

from ...config import settings
from ...template import Loader
from ...utils import mergeconcat
from .base import Addon, AddonSpec, AddonStatus


# Type variable for forward references to the HelmRelease type
HelmReleaseType = t.TypeVar("HelmReleaseType", bound = "HelmRelease")


#: Type for a SemVer version
SemVerVersion = constr(regex = r"^v?\d+\.\d+\.\d+(-[a-zA-Z0-9\.\-]+)?(\+[a-zA-Z0-9\.\-]+)?$")


class ChartInfo(schema.BaseModel):
    """
    Specification for a Helm chart.
    """
    repo: AnyHttpUrl = Field(
        ...,
        description = "The Helm repository that the chart is in."
    )
    name: constr(regex = r"^[a-z0-9-]+$") = Field(
        ...,
        description = "The name of the chart."
    )
    version: SemVerVersion = Field(
        ...,
        description = "The version of the chart to use."
    )


class HelmValuesSourceNameKey(schema.BaseModel):
    """
    Model for a Helm values source that consists of a data resource name and key.
    """
    name: constr(regex = r"^[a-z0-9-]+$") = Field(
        ...,
        description = "The name of the resource to use."
    )
    key: constr(min_length = 1) = Field(
        "values.yaml",
        description = (
            "The key in the resource data to get the values from. "
            "Defaults to values.yaml."
        )
    )


class HelmValuesConfigMapSource(schema.BaseModel):
    """
    Model for a values source that renders a key in a configmap as a Jinja2 template
    and parses the result as YAML.
    
    The template is provided with the HelmRelease object, the Cluster API Cluster
    resource and the infrastructure cluster resource as template variables.
    """
    config_map: HelmValuesSourceNameKey = Field(
        ...,
        description = "The details of a configmap and key to use."
    )

    async def get_values(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        addon: HelmReleaseType,
        cluster: t.Dict[str, t.Any],
        infra_cluster: t.Dict[str, t.Any],
        cloud_identity: t.Optional[t.Dict[str, t.Any]]
    ) -> t.Dict[str, t.Any]:
        resource = await ek_client.api("v1").resource("configmaps")
        configmap = await resource.fetch(
            self.config_map.name,
            namespace = addon.metadata.namespace
        )
        return template_loader.yaml_string(
            configmap.data[self.config_map.key],
            addon = addon,
            cluster = cluster,
            infra_cluster = infra_cluster,
            cloud_identity = cloud_identity
        )


class HelmValuesSecretSource(schema.BaseModel):
    """
    Model for a values source that renders a key in a secret as a Jinja2 template
    and parses the result as YAML.
    
    The template is provided with the HelmRelease object, the Cluster API Cluster
    resource and the infrastructure cluster resource as template variables.
    """
    secret: HelmValuesSourceNameKey = Field(
        ...,
        description = "The details of a secret and key to use."
    )

    async def get_values(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        addon: HelmReleaseType,
        cluster: t.Dict[str, t.Any],
        infra_cluster: t.Dict[str, t.Any],
        cloud_identity: t.Optional[t.Dict[str, t.Any]]
    ) -> t.Dict[str, t.Any]:
        resource = await ek_client.api("v1").resource("secrets")
        secret = await resource.fetch(
            self.secret.name,
            namespace = addon.metadata.namespace
        )
        return template_loader.yaml_string(
            base64.b64decode(secret.data[self.secret.key]).decode(),
            addon = addon,
            cluster = cluster,
            infra_cluster = infra_cluster,
            cloud_identity = cloud_identity
        )


class HelmValuesTemplateSource(schema.BaseModel):
    """
    Model for a values source that renders the given string as as a Jinja2 template
    and parses the result as YAML.
    
    The template is provided with the HelmRelease object, the Cluster API Cluster
    resource and the infrastructure cluster resource as template variables.
    """
    template: constr(min_length = 1) = Field(
        ...,
        description = "The template to use to render the values."
    )

    async def get_values(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        addon: HelmReleaseType,
        cluster: t.Dict[str, t.Any],
        infra_cluster: t.Dict[str, t.Any],
        cloud_identity: t.Optional[t.Dict[str, t.Any]]
    ) -> t.Dict[str, t.Any]:
        return template_loader.yaml_string(
            self.template,
            addon = addon,
            cluster = cluster,
            infra_cluster = infra_cluster,
            cloud_identity = cloud_identity
        )


HelmValuesSource = schema.StructuralUnion[
    HelmValuesConfigMapSource,
    HelmValuesSecretSource,
    HelmValuesTemplateSource,
]
HelmValuesSource.__doc__ = "Union type for the possible values sources."


class HelmReleaseSpec(AddonSpec):
    """
    Specification for a Helm release to be deployed onto the target cluster.
    """
    chart: ChartInfo = Field(
        ...,
        description = "The chart specification."
    )
    target_namespace: constr(regex = r"^[a-z0-9-]+$") = Field(
        ...,
        description = "The namespace on the target cluster to make the release in."
    )
    release_name: t.Optional[constr(regex = r"^[a-z0-9-]+$")] = Field(
        None,
        description = "The name of the release. Defaults to the name of the resource."
    )
    release_timeout: t.Optional[schema.IntOrString] = Field(
        None,
        description = (
            "The time to wait for components to become ready. "
            "If not given, the default timeout for the operator will be used."
        )
    )
    values_sources: t.List[HelmValuesSource] = Field(
        default_factory = list,
        description = "The values sources for the release."
    )


class HelmReleasePhase(str, schema.Enum):
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


class HelmReleaseStatus(AddonStatus):
    """
    The status of an addon.
    """
    phase: HelmReleasePhase = Field(
        HelmReleasePhase.UNKNOWN.value,
        description = "The phase of the addon."
    )
    revision: int = Field(
        0,
        description = "The current revision of the addon."
    )
    failure_message: str = Field(
        "",
        description = "The reason for entering a failed phase."
    )


class HelmRelease(
    Addon,
    subresources = {"status": {}},
    printer_columns = [
        {
            "name": "Cluster",
            "type": "string",
            "jsonPath": ".spec.clusterName",
        },
        {
            "name": "Bootstrap",
            "type": "string",
            "jsonPath": ".spec.bootstrap",
        },
        {
            "name": "Target Namespace",
            "type": "string",
            "jsonPath": ".spec.targetNamespace",
        },
        {
            "name": "Release Name",
            "type": "string",
            "jsonPath": ".spec.releaseName",
        },
        {
            "name": "Phase",
            "type": "string",
            "jsonPath": ".status.phase",
        },
        {
            "name": "Revision",
            "type": "integer",
            "jsonPath": ".status.revision",
        },
        {
            "name": "Chart Name",
            "type": "string",
            "jsonPath": ".spec.chart.name",
        },
        {
            "name": "Chart Version",
            "type": "string",
            "jsonPath": ".spec.chart.version",
        },
    ]
):
    """
    Addon that deploys a Helm chart.
    """
    spec: HelmReleaseSpec = Field(
        ...,
        description = "The specification for the Helm release."
    )
    status: HelmReleaseStatus = Field(
        default_factory = HelmReleaseStatus,
        description = "The status for the Helm release."
    )

    def get_labels(self):
        return {
            settings.release_namespace_label: self.spec.target_namespace,
            settings.release_name_label: self.spec.release_name or self.metadata.name,
        }

    async def init_status(self, ek_client: AsyncClient):
        """
        Initialise the status of the addon if we have not seen it before.
        """
        if self.status.phase == HelmReleasePhase.UNKNOWN:
            self.set_phase(HelmReleasePhase.PENDING)
            await self.save_status(ek_client)

    def set_phase(self, phase: HelmReleasePhase, message: str = ""):
        """
        Set the phase of this addon to the specified phase.
        """
        self.status.phase = phase
        # Reset the message unless the phase is failed
        self.status.failure_message = message if phase == HelmReleasePhase.FAILED else ""

    def uses_configmap(self, name: str):
        for source in self.spec.values_sources:
            if (
                isinstance(source, HelmValuesConfigMapSource) and
                source.config_map.name == name
            ):
                return True
        else:
            return False

    def uses_secret(self, name: str):
        for source in self.spec.values_sources:
            if (
                isinstance(source, HelmValuesSecretSource) and
                source.secret.name == name
            ):
                return True
        else:
            return False

    def get_chart_info(self) -> ChartInfo:
        """
        Returns the chart to use for the Helm release.
        """
        return self.spec.chart

    async def get_values(
        self,
        # The template loader to use when rendering templates
        template_loader: Loader,
        # easykube client for the management cluster
        ek_client: AsyncClient,
        # The Cluster API cluster object
        cluster: t.Dict[str, t.Any],
        # The Cluster API infrastructure cluster object
        infra_cluster: t.Dict[str, t.Any],
        # The cloud identity object, if one exists
        cloud_identity: t.Optional[t.Dict[str, t.Any]]
    ) -> t.Dict[str, t.Any]:
        """
        Returns the values to use with the Helm release.
        """
        # Compose the values from the specified sources
        values = {}
        for source in self.spec.values_sources:
            values = mergeconcat(
                values,
                await source.get_values(
                    template_loader,
                    ek_client,
                    self,
                    cluster,
                    infra_cluster,
                    cloud_identity
                )
            )
        return values

    @contextlib.asynccontextmanager
    async def _clients_for_cluster(
        self,
        ek_client: AsyncClient,
        cluster: t.Dict[str, t.Any]
    ):
        # The kubeconfig for the cluster is in a secret
        ekresource = await ek_client.api("v1").resource("secrets")
        secret = await ekresource.fetch(
            f"{cluster.metadata.name}-kubeconfig",
            namespace = cluster.metadata.namespace
        )
        with tempfile.NamedTemporaryFile() as kubeconfig:
            kubeconfig_data = base64.b64decode(secret.data["value"])
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

    async def _get_current_revision(
        self,
        ek_client: AsyncClient,
        helm_client: HelmClient
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
                self.set_phase(HelmReleasePhase.PREPARING)
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
                self.set_phase(HelmReleasePhase.PREPARING)
                await self.save_status(ek_client)
                return await current_revision.release.rollback(
                    cleanup_on_fail = True,
                    timeout = self.spec.release_timeout,
                    wait = True
                )
            else:
                # All other statuses are proceedable
                return current_revision

    async def install_or_upgrade(
        self,
        # The template loader to use when rendering templates
        template_loader: Loader,
        # easykube client for the management cluster
        ek_client: AsyncClient,
        # The Cluster API cluster object
        cluster: t.Dict[str, t.Any],
        # The Cluster API infrastructure cluster object
        infra_cluster: t.Dict[str, t.Any],
        # The cloud identity object, if one exists
        cloud_identity: t.Optional[t.Dict[str, t.Any]]
    ):
        """
        Install or upgrade this addon on the target cluster.
        """
        try:
            # Get easykube and Helm clients for the target cluster and use them to deploy the addon
            clients = self._clients_for_cluster(ek_client, cluster)
            async with clients as (ek_client_target, helm_client):
                # Before making a new revision, check the current status
                # If a release was interrupted, e.g. by a connection failure or the controller
                # pod being terminated, the release may be in a pending state that we need to correct
                current_revision = await self._get_current_revision(ek_client, helm_client)
                # Get the chart and values that we will use for the release
                chart_info = self.get_chart_info()
                chart = await helm_client.get_chart(
                    chart_info.name,
                    repo = chart_info.repo,
                    version = chart_info.version
                )
                values = await self.get_values(
                    template_loader,
                    ek_client,
                    cluster,
                    infra_cluster,
                    cloud_identity
                )
                should_install_or_upgrade = await helm_client.should_install_or_upgrade_release(
                    current_revision,
                    chart,
                    values
                )
                if should_install_or_upgrade:
                    self.set_phase(
                        HelmReleasePhase.UPGRADING
                        if current_revision
                        else HelmReleasePhase.INSTALLING
                    )
                    await self.save_status(ek_client)
                    crds = await chart.crds()
                    for crd in crds:
                        await ek_client_target.apply_object(crd, force = True)
                    current_revision = await helm_client.install_or_upgrade_release(
                        self.spec.release_name or self.metadata.name,
                        chart,
                        values,
                        cleanup_on_fail = True,
                        namespace = self.spec.target_namespace,
                        timeout = self.spec.release_timeout,
                        wait = True
                    )
                # Make sure the status matches the current revision
                self.status.revision = current_revision.revision
                if current_revision.status == ReleaseRevisionStatus.DEPLOYED:
                    self.set_phase(HelmReleasePhase.DEPLOYED)
                elif current_revision.status == ReleaseRevisionStatus.FAILED:
                    self.set_phase(HelmReleasePhase.FAILED, "helm upgrade failed")
                else:
                    self.set_phase(HelmReleasePhase.UNKNOWN)
                await self.save_status(ek_client)
        except (
            helm_errors.ChartNotFoundError,
            helm_errors.FailedToRenderChartError,
            helm_errors.ResourceAlreadyExistsError
        ) as exc:
            # These are permanent errors that can only be resolved by changing the spec
            self.set_phase(HelmReleasePhase.FAILED, str(exc))
            await self.save_status(ek_client)

    async def uninstall(
        self,
        # easykube client for the management cluster
        ek_client: AsyncClient
    ):
        """
        Remove this addon from the target cluster.
        """
        ekapi = await ek_client.api_preferred_version("cluster.x-k8s.io")
        ekresource = await ekapi.resource("clusters")
        try:
            cluster = await ekresource.fetch(
                self.spec.cluster_name,
                namespace = self.metadata.namespace
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
        # Mark the addon as uninstalling
        self.set_phase(HelmReleasePhase.UNINSTALLING)
        await self.save_status(ek_client)
        clients = self._clients_for_cluster(ek_client, cluster)
        async with clients as (_, helm_client):
            try:
                await helm_client.uninstall_release(
                    self.spec.release_name or self.metadata.name,
                    namespace = self.spec.target_namespace,
                    timeout = self.spec.release_timeout,
                    wait = True
                )
                # We leave CRDs behind on purpose
            except helm_errors.ReleaseNotFoundError:
                # If the release doesn't exist, we are done
                return
