import json
import re
import typing as t

import kopf

from pydantic import Field, ValidationError, constr, validator

from easykube import ApiError
from easykube.kubernetes.client import AsyncClient

from kube_custom_resource import schema

from ...config import settings
from ...template import Loader
from .base import Addon, AddonSpec, AddonStatus


class ArgoApplicationSyncOptions(schema.BaseModel):
    """
    Model for sync options for the Argo application.
    """
    server_side_apply: t.Optional[bool] = Field(
        None,
        description = (
            "Indicates whether to use server-side apply (default true). "
            "Server-side apply may not be usable with some resources."
        )
    )


class ArgoApplicationSpec(AddonSpec):
    """
    Base class for the spec of addons that produce Argo applications.
    """
    target_namespace: constr(regex = r"^[a-z0-9-]+$") = Field(
        ...,
        description = "The namespace on the target cluster to deploy the application in."
    )
    project: t.Optional[constr(regex = r"^[a-z0-9-]+$")] = Field(
        None,
        description = (
            "The Argo project to deploy the application in. "
            "If not given, the default project will be used."
        )
    )
    sync_options: ArgoApplicationSyncOptions = Field(
        default_factory = ArgoApplicationSyncOptions,
        description = "The Argo sync options for the application."
    )


class ArgoApplicationPhase(str, schema.Enum):
    """
    The phase of the Argo application.
    """
    #: Indicates that the state of the application is not known
    UNKNOWN = "Unknown"
    #: Indicates that the application is waiting to be deployed
    PENDING = "Pending"
    #: Indicates that the application is currently synchronising
    RECONCILING = "Reconciling"
    #: Indicates that the application is ready to use
    READY = "Ready"
    #: Indicates that the application has been suspended
    SUSPENDED = "Suspended"
    #: Indicates that the application is being uninstalled
    UNINSTALLING = "Uninstalling"
    #: Indicates that the application is deployed but unhealthy
    UNHEALTHY = "Unhealthy"
    #: Indicates that the application failed to deploy
    FAILED = "Failed"


class ArgoApplicationStatus(AddonStatus):
    """
    The status for an Argo application.
    """
    phase: ArgoApplicationPhase = Field(
        ArgoApplicationPhase.UNKNOWN.value,
        description = "The phase of the application."
    )
    health_message: str = Field(
        "",
        description = "Human-readable health message."
    )
    failure_message: str = Field(
        "",
        description = "The reason for entering a failed phase."
    )

    @validator("phase", pre = True, always = True)
    def validate_phase(cls, v):
        """
        Allow migration from legacy phases by returning unknown.
        """
        if isinstance(v, ArgoApplicationPhase):
            return v
        try:
            return ArgoApplicationPhase(v)
        except ValueError:
            return ArgoApplicationPhase.UNKNOWN


class ArgoApplication(Addon, abstract = True):
    """
    Base class for addons that deploy an Argo application.
    """
    spec: ArgoApplicationSpec = Field(
        ...,
        description = "The spec for the Argo application."
    )
    status: ArgoApplicationStatus = Field(
        default_factory = ArgoApplicationStatus,
        description = "The status for the Argo application."
    )

    def set_phase(self, phase: ArgoApplicationPhase, message: str = ""):
        """
        Set the phase of this application.
        """
        self.status.phase = phase
        self.status.health_message = message if phase == ArgoApplicationPhase.UNHEALTHY else ""
        self.status.failure_message = message if phase == ArgoApplicationPhase.FAILED else ""

    async def init_status(self, ek_client: AsyncClient):
        """
        Initialise the status of the addon if we have not seen it before.
        """
        if self.status.phase == ArgoApplicationPhase.UNKNOWN:
            self.set_phase(ArgoApplicationPhase.PENDING)
            await self.save_status(ek_client)

    async def get_argo_source(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        cluster: t.Dict[str, t.Any],
        infra_cluster: t.Dict[str, t.Any],
        cloud_identity: t.Optional[t.Dict[str, t.Any]]
    ) -> t.Dict[str, t.Any]:
        """
        Returns the source for the Argo application.
        """
        raise NotImplementedError

    def _argo_app_name(self) -> str:
        """
        Returns the Argo app name for this object.
        """
        return settings.argocd.app_name_template.format(
            type = type(self).__name__.lower(),
            namespace = self.metadata.namespace,
            name = self.metadata.name,
            # In order to ensure uniqueness, we include the first 8 characters of the UID
            # Otherwise, ns-bob/cluster-1 and ns/bob-cluster-1 make the same app name
            id = self.metadata.uid[:8]
        )

    def _project_from_annotation(self, annotations: t.Dict[str, str]) -> t.Optional[str]:
        """
        Returns the project from the annotation, if present.
        """
        annotation = f"{settings.annotation_prefix}/project"
        annotation_value = annotations.get(annotation, "")
        # Check that the annotation value matches the same regex as the spec,
        # otherwise it is as if it doesn't exist
        if re.search(r"^[a-z0-9-]+$", annotation_value) is not None:
            return annotation_value
        else:
            return None

    def _sync_options_from_annotation(self) -> t.Optional[ArgoApplicationSyncOptions]:
        """
        Parses the sync options from the supported annotation, if present.
        """
        annotation = f"{settings.annotation_prefix}/sync-options"
        annotation_value_text = self.metadata.annotations.get(annotation, "")
        try:
            annotation_value = json.loads(annotation_value_text)
        except json.JSONDecodeError:
            return None
        try:
            return ArgoApplicationSyncOptions(**annotation_value)
        except ValidationError:
            return None

    async def install_or_upgrade(
        self,
        template_loader: Loader,
        ek_client: AsyncClient,
        cluster: t.Dict[str, t.Any],
        infra_cluster: t.Dict[str, t.Any],
        cloud_identity: t.Optional[t.Dict[str, t.Any]]
    ):
        # Decide which project to use
        # We allow specifying the project as an annotation for compatibility reasons,
        # but they value from the spec takes precedence if both are given
        # We also allow the cluster to specify the project as an annotation for all addons
        project = (
            self.spec.project or
            self._project_from_annotation(self.metadata.annotations) or
            self._project_from_annotation(cluster.metadata.get("annotations", {})) or
            settings.argocd.default_project
        )
        # Decide what sync options to use
        # We support specifying sync options using an annotation for compatibility reasons,
        # but values from the spec take precendence if they are given
        sync_opts_annotation = self._sync_options_from_annotation()
        sync_options = [
            # This prevents Argo syncing large charts over and over
            # when only a small number of resources are out-of-sync
            "ApplyOutOfSyncOnly=true",
            # Make sure to create namespaces
            "CreateNamespace=true",
        ]
        if self.spec.sync_options.server_side_apply is not None:
            if self.spec.sync_options.server_side_apply:
                sync_options.append("ServerSideApply=true")
        elif (
            sync_opts_annotation
            and sync_opts_annotation.server_side_apply is not None
        ):
            if sync_opts_annotation.server_side_apply:
                sync_options.append("ServerSideApply=true")
        else:
            # If server-side apply is not specified by the spec or annotation, default to true
            sync_options.append("ServerSideApply=true")
        # Ensure the corresponding Argo application is up-to-date
        _ = await ek_client.apply_object(
            {
                "apiVersion": settings.argocd.api_version,
                "kind": "Application",
                "metadata": {
                    "name": self._argo_app_name(),
                    "namespace": settings.argocd.namespace,
                    "labels": {
                        "app.kubernetes.io/managed-by": "cluster-api-addon-provider",
                    },
                    "annotations": {
                        # The annotation is used to identify the associated addon
                        # when an event is observed for the Argo application
                        f"{settings.annotation_prefix}/owner-reference": (
                            f"{settings.api_group}:"
                            f"{self._meta.plural_name}:"
                            f"{self.metadata.namespace}:"
                            f"{self.metadata.name}"
                        ),
                    },
                },
                "spec": {
                    "destination": {
                        "name": cluster.metadata.get("annotations", {}).get(
                            f"{settings.annotation_prefix}/argo-cluster-name",
                            settings.argocd.cluster_name_template.format(
                                namespace = cluster.metadata.namespace,
                                name = cluster.metadata.name,
                                id = cluster.metadata.uid[:8]
                            )
                        ),
                        "namespace": self.spec.target_namespace,
                    },
                    "project": project,
                    "source": await self.get_argo_source(
                        template_loader,
                        ek_client,
                        cluster,
                        infra_cluster,
                        cloud_identity
                    ),
                    "syncPolicy": {
                        "automated": {
                            "prune": True,
                            "selfHeal": settings.argocd.self_heal_applications,
                        },
                        "syncOptions": sync_options,
                    },
                },
            },
            force = True
        )

    async def uninstall(self, ek_client: AsyncClient):
        # Make sure that we are in the uninstalling phase
        if self.status.phase != ArgoApplicationPhase.UNINSTALLING:
            self.set_phase(ArgoApplicationPhase.UNINSTALLING)
            await self.save_status(ek_client)
        # Get the app
        ekapps = await ek_client.api(settings.argocd.api_version).resource("applications")
        try:
            app = await ekapps.fetch(self._argo_app_name(), namespace = settings.argocd.namespace)
        except ApiError as exc:
            if exc.status_code == 404:
                # If the app doesn't exist, we are done
                return
            else:
                raise
        # If the app is already deleting, we just want to wait for it not to exist
        if app.metadata.get("deletionTimestamp"):
            raise kopf.TemporaryError("waiting for application to delete", delay = 5)
        # Check if we have been triggered as part of a cluster deletion
        # This determines whether we want to do a cascading delete or not
        # See https://argo-cd.readthedocs.io/en/stable/user-guide/app_deletion/
        ekclusterapi = await ek_client.api_preferred_version("cluster.x-k8s.io")
        ekclusters = await ekclusterapi.resource("clusters")
        try:
            cluster = await ekclusters.fetch(
                self.spec.cluster_name,
                namespace = self.metadata.namespace
            )
        except ApiError as exc:
            # If the cluster does not exist, we are done
            if exc.status_code == 404:
                cluster = None
            else:
                raise
        # Ensure the presence or absence of the finalizer as required
        finalizers = set(app.metadata.get("finalizers", []))
        next_finalizers = finalizers.copy()
        if cluster and not cluster.metadata.get("deletionTimestamp"):
            next_finalizers.add(settings.argocd.resource_deletion_finalizer)
        elif settings.argocd.resource_deletion_finalizer in next_finalizers:
            next_finalizers.remove(settings.argocd.resource_deletion_finalizer)
        if next_finalizers != finalizers:
            app = await ekapps.patch(
                app.metadata.name,
                {
                    "metadata": {
                        "finalizers": list(next_finalizers),
                    },
                },
                namespace = app.metadata.namespace
            )
        # Delete the Argo app
        await ekapps.delete(app.metadata.name, namespace = app.metadata.namespace)
        # Wait for the app to be deleted
        raise kopf.TemporaryError("waiting for application to delete", delay = 5)

    async def argo_application_updated(
        self,
        ek_client: AsyncClient,
        application: t.Dict[str, t.Any]
    ):
        """
        Called when the Argo application for the addon is updated.
        """
        # Derive a single phase from the sync, operation and health statuses
        app_status = application.get("status", {})
        sync_status = app_status.get("sync", {}).get("status", "Unknown")
        health_status = app_status.get("health", {}).get("status", "Unknown")
        operation_phase = app_status.get("operationState", {}).get("phase")
        if not self.metadata.deletion_timestamp:
            if sync_status == "Unknown":
                self.set_phase(ArgoApplicationPhase.UNKNOWN)
            elif sync_status == "OutOfSync":
                if not operation_phase:
                    # Out-of-sync but no in-progress operation means an unhealthy addon
                    self.set_phase(ArgoApplicationPhase.UNHEALTHY, "resources out-of-sync")
                elif operation_phase in {"Running", "Succeeded", "Terminating"}:
                    self.set_phase(ArgoApplicationPhase.RECONCILING)
                else:  # Failed, Error
                    self.set_phase(
                        ArgoApplicationPhase.FAILED,
                        app_status.get("operationState", {}).get("message") or ""
                    )
            else:
                # Consider suspended apps as healthy
                if health_status == "Healthy":
                    self.set_phase(ArgoApplicationPhase.READY)
                elif health_status == "Suspended":
                    self.set_phase(ArgoApplicationPhase.SUSPENDED)
                else:
                    self.set_phase(
                        ArgoApplicationPhase.UNHEALTHY,
                        app_status.get("health", {}).get("message") or ""
                    )
        else:
            # If the sync job has failed, mark the addon as failed
            # Otherwise, it should be uninstalling
            if (
                sync_status == "OutOfSync" and
                operation_phase and
                operation_phase in {"Failed", "Error"}
            ):
                self.set_phase(
                    ArgoApplicationPhase.FAILED,
                    app_status.get("operationState", {}).get("message") or ""
                )
            else:
                self.set_phase(ArgoApplicationPhase.UNINSTALLING)
        await self.save_status(ek_client)

    async def argo_application_deleted(
        self,
        ek_client: AsyncClient,
        application: t.Dict[str, t.Any]
    ):
        """
        Called when the Argo application for the addon is deleted.
        """
        # If the Argo application was deleted and we are not deleting, our
        # state is now unknown
        if not self.metadata.deletion_timestamp:
            self.set_phase(ArgoApplicationPhase.UNKNOWN)
            await self.save_status(ek_client)
