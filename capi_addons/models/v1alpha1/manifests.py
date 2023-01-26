import base64
import typing as t

from pydantic import Field, constr

from easykube.kubernetes.client import AsyncClient

from kube_custom_resource import schema

from ...config import settings
from ...template import Loader
from .argo_application import ArgoApplicationSpec
from .helm_release import ChartInfo, HelmRelease


# Type variable for forward references to the HelmRelease type
ManifestsType = t.TypeVar("ManifestsType", bound = "Manifests")


class ManifestSourceNameKeys(schema.BaseModel):
    """
    Model for a manifest source that consists of a resource name and sets of keys to
    explicitly include or exclude.
    """
    name: constr(regex = r"^[a-z0-9-]+$") = Field(
        ...,
        description = "The name of the resource to use."
    )
    keys: t.List[constr(min_length = 1)] = Field(
        default_factory = list,
        description = (
            "The keys in the resource to render as manifests. "
            "If not given, all the keys are considered."
        )
    )
    exclude_keys: t.List[constr(min_length = 1)] = Field(
        default_factory = list,
        description = "Keys to explicitly exclude from being rendered as manifests."
    )

    def filter_keys(self, keys: t.List[str]) -> t.List[str]:
        """
        Given a list of keys, return the keys that match the configured filters.
        """
        if self.keys:
            keys = (key for key in keys if key in self.keys)
        return [key for key in keys if key not in self.exclude_keys]


class ManifestConfigMapSource(schema.BaseModel):
    """
    Model for a manifest source that renders the keys in a configmap as Jinja2 templates.
    
    The templates are provided with the HelmRelease object, the Cluster API Cluster resource
    and the infrastructure cluster resource as template variables.
    """
    config_map: ManifestSourceNameKeys = Field(
        ...,
        description = "The details of a configmap and keys to use."
    )

    async def get_resources(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        addon: ManifestsType,
        cluster: t.Dict[str, t.Any],
        infra_cluster: t.Dict[str, t.Any],
        cloud_identity: t.Optional[t.Dict[str, t.Any]]
    ) -> t.Iterable[t.Dict[str, t.Any]]:
        resource = await ek_client.api("v1").resource("configmaps")
        configmap = await resource.fetch(
            self.config_map.name,
            namespace = addon.metadata.namespace
        )
        return (
            resource
            for key in self.config_map.filter_keys(configmap.data.keys())
            for resource in template_loader.yaml_string_all(
                configmap.data[key],
                addon = addon,
                cluster = cluster,
                infra_cluster = infra_cluster,
                cloud_identity = cloud_identity
            )
        )


class ManifestSecretSource(schema.BaseModel):
    """
    Model for a manifest source that renders the keys in a secret as Jinja2 templates.
    
    The templates are provided with the HelmRelease object, the Cluster API Cluster resource
    and the infrastructure cluster resource as template variables.
    """
    secret: ManifestSourceNameKeys = Field(
        ...,
        description = "The details of a secret and keys to use."
    )

    async def get_resources(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        addon: ManifestsType,
        cluster: t.Dict[str, t.Any],
        infra_cluster: t.Dict[str, t.Any],
        cloud_identity: t.Optional[t.Dict[str, t.Any]]
    ) -> t.Iterable[t.Dict[str, t.Any]]:
        resource = await ek_client.api("v1").resource("secrets")
        secret = await resource.fetch(
            self.secret.name,
            namespace = addon.metadata.namespace
        )
        return (
            resource
            for key in self.secret.filter_keys(secret.data.keys())
            for resource in template_loader.yaml_string_all(
                base64.b64decode(secret.data[key]).decode(),
                addon = addon,
                cluster = cluster,
                infra_cluster = infra_cluster,
                cloud_identity = cloud_identity
            )
        )


class ManifestTemplateSource(schema.BaseModel):
    """
    Model for a manifest source that renders the given string as as a Jinja2 template.
    
    The template is provided with the Manifests object, the Cluster API Cluster
    resource and the infrastructure cluster resource as template variables.
    """
    template: constr(min_length = 1) = Field(
        ...,
        description = "The template to use to render the manifests."
    )

    async def get_resources(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        addon: ManifestsType,
        cluster: t.Dict[str, t.Any],
        infra_cluster: t.Dict[str, t.Any],
        cloud_identity: t.Optional[t.Dict[str, t.Any]]
    ) -> t.Iterable[t.Dict[str, t.Any]]:
        return (
            resource
            for resource in template_loader.yaml_string_all(
                self.template,
                addon = addon,
                cluster = cluster,
                infra_cluster = infra_cluster,
                cloud_identity = cloud_identity
            )
        )


ManifestSource = schema.StructuralUnion[
    ManifestConfigMapSource,
    ManifestSecretSource,
    ManifestTemplateSource,
]
ManifestSource.__doc__ = "Union type for the possible manifest sources."


class ManifestsSpec(ArgoApplicationSpec):
    """
    Specification for a set of manifests to be deployed onto the target cluster.
    """
    release_name: t.Optional[constr(regex = r"^[a-z0-9-]+$")] = Field(
        None,
        description = "The name of the release. Defaults to the name of the resource."
    )
    manifest_sources: t.List[ManifestSource] = Field(
        default_factory = list,
        description = "The manifest sources for the release."
    )


class Manifests(
    HelmRelease,
    plural_name = "manifests",
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
    ]
):
    """
    Addon that deploys manifests.
    """
    spec: ManifestsSpec = Field(
        ...,
        description = "The specification for the manifests."
    )

    def uses_configmap(self, name: str):
        for source in self.spec.manifest_sources:
            if (
                isinstance(source, ManifestConfigMapSource) and
                source.config_map.name == name
            ):
                return True
        else:
            return False

    def uses_secret(self, name: str):
        for source in self.spec.manifest_sources:
            if (
                isinstance(source, ManifestSecretSource) and
                source.secret.name == name
            ):
                return True
        else:
            return False

    def get_chart_info(self) -> ChartInfo:
        """
        Returns the chart to use for the Helm release.
        """
        return ChartInfo(
            repo = settings.manifests_helm_chart.repo,
            name = settings.manifests_helm_chart.name,
            version = settings.manifests_helm_chart.version
        )

    async def get_values(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        cluster: t.Dict[str, t.Any],
        infra_cluster: t.Dict[str, t.Any],
        cloud_identity: t.Optional[t.Dict[str, t.Any]]
    ) -> t.Dict[str, t.Any]:
        return {
            "resources": [
                resource
                async for resource in self.get_resources(
                    template_loader,
                    ek_client,
                    cluster,
                    infra_cluster,
                    cloud_identity
                )
            ]
        }

    async def get_resources(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        cluster: t.Dict[str, t.Any],
        infra_cluster: t.Dict[str, t.Any],
        cloud_identity: t.Optional[t.Dict[str, t.Any]]
    ) -> t.Iterable[t.Dict[str, t.Any]]:
        """
        Returns the resources to use to build the ephemeral chart.
        """
        for source in self.spec.manifest_sources:
            for resource in await source.get_resources(
                template_loader,
                ek_client,
                self,
                cluster,
                infra_cluster,
                cloud_identity
            ):
                yield resource
