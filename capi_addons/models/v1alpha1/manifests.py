import base64
import typing as t

from easykube.kubernetes.client import AsyncClient
from kube_custom_resource import schema
from pydantic import Field

from capi_addons.models.v1alpha1.base import AddonSpec, EphemeralChartAddon
from capi_addons.template import Loader

# Type variable for forward references to the HelmRelease type
ManifestsType = t.TypeVar("ManifestsType", bound="Manifests")


class ManifestSourceNameKeys(schema.BaseModel):
    """
    Model for a manifest source that consists of a resource name and sets of keys to
    explicitly include or exclude.
    """

    name: schema.constr(pattern=r"^[a-z0-9-]+$") = Field(
        ..., description="The name of the resource to use."
    )
    keys: list[schema.constr(min_length=1)] = Field(
        default_factory=list,
        description=(
            "The keys in the resource to render as manifests. "
            "If not given, all the keys are considered."
        ),
    )
    exclude_keys: list[schema.constr(min_length=1)] = Field(
        default_factory=list,
        description="Keys to explicitly exclude from being rendered as manifests.",
    )

    def filter_keys(self, keys: list[str]) -> list[str]:
        """
        Given a list of keys, return the keys that match the configured filters.
        """
        if self.keys:
            keys = (key for key in keys if key in self.keys)
        return [key for key in keys if key not in self.exclude_keys]


class ManifestConfigMapSource(schema.BaseModel):
    """
    Model for a manifest source that renders the keys in a configmap as Jinja2 templates

    The templates are provided with the HelmRelease object, the Cluster API Cluster
    resource and the infrastructure cluster resource as template variables.
    """

    config_map: ManifestSourceNameKeys = Field(
        ..., description="The details of a configmap and keys to use."
    )

    async def get_resources(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        addon: ManifestsType,
        cluster: dict[str, t.Any],
        infra_cluster: dict[str, t.Any],
        cloud_identity: dict[str, t.Any] | None,
    ) -> t.Iterable[dict[str, t.Any]]:
        resource = await ek_client.api("v1").resource("configmaps")
        configmap = await resource.fetch(
            self.config_map.name, namespace=addon.metadata.namespace
        )
        return (
            resource
            for key in self.config_map.filter_keys(configmap.data.keys())
            for resource in template_loader.yaml_string_all(
                configmap.data[key],
                addon=addon,
                cluster=cluster,
                infra_cluster=infra_cluster,
                cloud_identity=cloud_identity,
            )
        )


class ManifestSecretSource(schema.BaseModel):
    """
    Model for a manifest source that renders the keys in a secret as Jinja2 templates.

    The templates are provided with the HelmRelease object, the Cluster API Cluster
    resource and the infrastructure cluster resource as template variables.
    """

    secret: ManifestSourceNameKeys = Field(
        ..., description="The details of a secret and keys to use."
    )

    async def get_resources(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        addon: ManifestsType,
        cluster: dict[str, t.Any],
        infra_cluster: dict[str, t.Any],
        cloud_identity: dict[str, t.Any] | None,
    ) -> t.Iterable[dict[str, t.Any]]:
        resource = await ek_client.api("v1").resource("secrets")
        secret = await resource.fetch(
            self.secret.name, namespace=addon.metadata.namespace
        )
        return (
            resource
            for key in self.secret.filter_keys(secret.data.keys())
            for resource in template_loader.yaml_string_all(
                base64.b64decode(secret.data[key]).decode(),
                addon=addon,
                cluster=cluster,
                infra_cluster=infra_cluster,
                cloud_identity=cloud_identity,
            )
        )


class ManifestTemplateSource(schema.BaseModel):
    """
    Model for a manifest source that renders the given string as as a Jinja2 template.

    The template is provided with the Manifests object, the Cluster API Cluster
    resource and the infrastructure cluster resource as template variables.
    """

    template: schema.constr(min_length=1) = Field(
        ..., description="The template to use to render the manifests."
    )

    async def get_resources(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        addon: ManifestsType,
        cluster: dict[str, t.Any],
        infra_cluster: dict[str, t.Any],
        cloud_identity: dict[str, t.Any] | None,
    ) -> t.Iterable[dict[str, t.Any]]:
        return (
            resource
            for resource in template_loader.yaml_string_all(
                self.template,
                addon=addon,
                cluster=cluster,
                infra_cluster=infra_cluster,
                cloud_identity=cloud_identity,
            )
        )


ManifestSource = t.Annotated[
    ManifestConfigMapSource | ManifestSecretSource | ManifestTemplateSource,
    schema.StructuralUnion,
]
ManifestSource.__doc__ = "Union type for the possible manifest sources."


class ManifestsSpec(AddonSpec):
    """
    Specification for a set of manifests to be deployed onto the target cluster.
    """

    manifest_sources: list[ManifestSource] = Field(
        default_factory=list, description="The manifest sources for the release."
    )


class Manifests(
    EphemeralChartAddon,
    plural_name="manifests",
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
    ],
):
    """
    Addon that deploys manifests.
    """

    spec: ManifestsSpec = Field(..., description="The specification for the manifests.")

    def list_configmaps(self):
        return [
            source.config_map.name
            for source in self.spec.manifest_sources
            if isinstance(source, ManifestConfigMapSource)
        ]

    def list_secrets(self):
        return [
            source.secret.name
            for source in self.spec.manifest_sources
            if isinstance(source, ManifestSecretSource)
        ]

    async def get_resources(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        cluster: dict[str, t.Any],
        infra_cluster: dict[str, t.Any],
        cloud_identity: dict[str, t.Any] | None,
    ) -> t.AsyncIterable[dict[str, t.Any]]:
        """
        Returns the resources to use to build the ephemeral chart.
        """
        for source in self.spec.manifest_sources:
            for resource in await source.get_resources(
                template_loader, ek_client, self, cluster, infra_cluster, cloud_identity
            ):
                yield resource
