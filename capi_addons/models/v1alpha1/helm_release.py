import base64
import contextlib
import pathlib
import typing as t

from easykube.kubernetes.client import AsyncClient
from kube_custom_resource import schema
from pydantic import Field
from pyhelm3 import Client

from capi_addons.template import Loader
from capi_addons.utils import mergeconcat

from .base import Addon, AddonSpec

# Type variable for forward references to the HelmRelease type
HelmReleaseType = t.TypeVar("HelmReleaseType", bound="HelmRelease")


SEMVER_PATTERN = r"^v?\d+\.\d+\.\d+(-[a-zA-Z0-9\.\-]+)?(\+[a-zA-Z0-9\.\-]+)?$"


class HelmChart(schema.BaseModel):
    """
    Specification for the chart to use.
    """

    repo: schema.AnyHttpUrl = Field(
        ..., description="The Helm repository that the chart is in."
    )
    name: schema.constr(pattern=r"^[a-z0-9-]+$") = Field(
        ..., description="The name of the chart."
    )
    version: schema.constr(pattern=SEMVER_PATTERN) = Field(
        ..., description="The version of the chart to use."
    )


class HelmValuesSourceNameKey(schema.BaseModel):
    """
    Model for a Helm values source that consists of a data resource name and key.
    """

    name: schema.constr(pattern=r"^[a-z0-9-]+$") = Field(
        ..., description="The name of the resource to use."
    )
    key: schema.constr(min_length=1) = Field(
        "values.yaml",
        description=(
            "The key in the resource data to get the values from. "
            "Defaults to values.yaml."
        ),
    )


class HelmValuesConfigMapSource(schema.BaseModel):
    """
    Model for a values source that renders a key in a configmap as a Jinja2 template
    and parses the result as YAML.

    The template is provided with the HelmRelease object, the Cluster API Cluster
    resource and the infrastructure cluster resource as template variables.
    """

    config_map: HelmValuesSourceNameKey = Field(
        ..., description="The details of a configmap and key to use."
    )

    async def get_values(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        addon: HelmReleaseType,
        cluster: dict[str, t.Any],
        infra_cluster: dict[str, t.Any],
        cloud_identity: dict[str, t.Any] | None,
    ) -> dict[str, t.Any]:
        resource = await ek_client.api("v1").resource("configmaps")
        configmap = await resource.fetch(
            self.config_map.name, namespace=addon.metadata.namespace
        )
        return template_loader.yaml_string(
            configmap.data[self.config_map.key],
            addon=addon,
            cluster=cluster,
            infra_cluster=infra_cluster,
            cloud_identity=cloud_identity,
        )


class HelmValuesSecretSource(schema.BaseModel):
    """
    Model for a values source that renders a key in a secret as a Jinja2 template
    and parses the result as YAML.

    The template is provided with the HelmRelease object, the Cluster API Cluster
    resource and the infrastructure cluster resource as template variables.
    """

    secret: HelmValuesSourceNameKey = Field(
        ..., description="The details of a secret and key to use."
    )

    async def get_values(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        addon: HelmReleaseType,
        cluster: dict[str, t.Any],
        infra_cluster: dict[str, t.Any],
        cloud_identity: dict[str, t.Any] | None,
    ) -> dict[str, t.Any]:
        resource = await ek_client.api("v1").resource("secrets")
        secret = await resource.fetch(
            self.secret.name, namespace=addon.metadata.namespace
        )
        return template_loader.yaml_string(
            base64.b64decode(secret.data[self.secret.key]).decode(),
            addon=addon,
            cluster=cluster,
            infra_cluster=infra_cluster,
            cloud_identity=cloud_identity,
        )


class HelmValuesTemplateSource(schema.BaseModel):
    """
    Model for a values source that renders the given string as as a Jinja2 template
    and parses the result as YAML.

    The template is provided with the HelmRelease object, the Cluster API Cluster
    resource and the infrastructure cluster resource as template variables.
    """

    template: schema.constr(min_length=1) = Field(
        ..., description="The template to use to render the values."
    )

    async def get_values(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        addon: HelmReleaseType,
        cluster: dict[str, t.Any],
        infra_cluster: dict[str, t.Any],
        cloud_identity: dict[str, t.Any] | None,
    ) -> dict[str, t.Any]:
        return template_loader.yaml_string(
            self.template,
            addon=addon,
            cluster=cluster,
            infra_cluster=infra_cluster,
            cloud_identity=cloud_identity,
        )


HelmValuesSource = t.Annotated[
    HelmValuesConfigMapSource | HelmValuesSecretSource | HelmValuesTemplateSource,
    schema.StructuralUnion,
]
HelmValuesSource.__doc__ = "Union type for the possible values sources."


class HelmReleaseSpec(AddonSpec):
    """
    Specification for a Helm release to be deployed onto the target cluster.
    """

    chart: HelmChart = Field(..., description="The chart specification.")
    values_sources: list[HelmValuesSource] = Field(
        default_factory=list, description="The values sources for the release."
    )


class HelmRelease(
    Addon,
    subresources={"status": {}},
    printer_columns=[
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
    ],
):
    """
    Addon that deploys a Helm chart.
    """

    spec: HelmReleaseSpec = Field(
        ..., description="The specification for the Helm release."
    )

    def list_configmaps(self):
        return [
            source.config_map.name
            for source in self.spec.values_sources
            if isinstance(source, HelmValuesConfigMapSource)
        ]

    def list_secrets(self):
        return [
            source.secret.name
            for source in self.spec.values_sources
            if isinstance(source, HelmValuesSecretSource)
        ]

    def get_chart(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        helm_client: Client,
        cluster: dict[str, t.Any],
        infra_cluster: dict[str, t.Any],
        cloud_identity: dict[str, t.Any] | None,
    ) -> contextlib.AbstractAsyncContextManager[pathlib.Path]:
        return helm_client.pull_chart(
            self.spec.chart.name,
            repo=self.spec.chart.repo,
            version=self.spec.chart.version,
        )

    async def get_values(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        cluster: dict[str, t.Any],
        infra_cluster: dict[str, t.Any],
        cloud_identity: dict[str, t.Any] | None,
    ) -> dict[str, t.Any]:
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
                    cloud_identity,
                ),
            )
        return values
