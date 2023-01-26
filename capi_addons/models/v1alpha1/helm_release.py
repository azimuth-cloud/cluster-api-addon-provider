import base64
import typing as t

from pydantic import Field, AnyHttpUrl, constr

import yaml

from easykube.kubernetes.client import AsyncClient

from kube_custom_resource import schema

from ...config import settings
from ...template import Loader
from ...utils import mergeconcat
from .argo_application import ArgoApplication, ArgoApplicationSpec


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


class HelmReleaseSpec(ArgoApplicationSpec):
    """
    Specification for a Helm release to be deployed onto the target cluster.
    """
    chart: ChartInfo = Field(
        ...,
        description = "The chart specification."
    )
    release_name: t.Optional[constr(regex = r"^[a-z0-9-]+$")] = Field(
        None,
        description = "The name of the release. Defaults to the name of the resource."
    )
    values_sources: t.List[HelmValuesSource] = Field(
        default_factory = list,
        description = "The values sources for the release."
    )


class HelmRelease(
    ArgoApplication,
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

    def get_labels(self):
        return {
            settings.release_namespace_label: self.spec.target_namespace,
            settings.release_name_label: self.spec.release_name or self.metadata.name,
        }

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

    async def get_argo_source(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        cluster: t.Dict[str, t.Any],
        infra_cluster: t.Dict[str, t.Any],
        cloud_identity: t.Optional[t.Dict[str, t.Any]]
    ) -> t.Dict[str, t.Any]:
        chart_info = self.get_chart_info()
        return {
            "repoURL": chart_info.repo,
            "chart": chart_info.name,
            "targetRevision": chart_info.version,
            "helm": {
                "releaseName": self.spec.release_name or self.metadata.name,
                "values": yaml.safe_dump(
                    await self.get_values(
                        template_loader,
                        ek_client,
                        cluster,
                        infra_cluster,
                        cloud_identity
                    )
                ),
            },
        }
