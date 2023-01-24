import typing

from pydantic import Field, constr

from easykube.kubernetes.client import AsyncClient

from kube_custom_resource import CustomResource, schema

from ...config import settings
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


class AddonStatus(schema.BaseModel):
    """
    Base class for the status of addon resources.
    """


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
                "status": self.status.dict(exclude_defaults = True),
            },
            namespace = self.metadata.namespace
        )
        # Store the new resource version
        self.metadata.resource_version = data["metadata"]["resourceVersion"]

    def get_labels(self):
        """
        Returns addon-specific labels to apply.
        """
        return {}

    async def init_metadata(
        self,
        ek_client: AsyncClient,
        cluster: typing.Dict[str, typing.Any]
    ):
        """
        Initialises the addon's metadata, ensuring owner references and labels are set.
        """
        previous_labels = dict(self.metadata.labels)
        # Make sure that the cluster owns the addon
        owner_references_updated = self.metadata.add_owner_reference(
            cluster,
            block_owner_deletion = True
        )
        # Add a label for the cluster
        self.metadata.labels[settings.cluster_label] = cluster.metadata.name
        # Apply addon-specific labels
        self.metadata.labels.update(self.get_labels())
        # Check if the labels changed
        labels_updated = self.metadata.labels == previous_labels
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
        raise NotImplementedError

    def uses_configmap(self, name: str):
        """
        Returns True if this addon uses the named configmap, False otherwise.
        """
        raise NotImplementedError

    def uses_secret(self, name: str):
        """
        Returns True if this addon uses the named secret, False otherwise.
        """
        raise NotImplementedError

    async def install_or_upgrade(
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
    ):
        """
        Install or upgrade this addon on the target cluster.
        """
        raise NotImplementedError

    async def uninstall(
        self,
        # easykube client for the management cluster
        ek_client: AsyncClient
    ):
        """
        Remove this addon from the target cluster.
        """
        raise NotImplementedError
