from .base import Addon


class ArgoApplication(
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
    Addon that deploys an Argo application.
    """

    async def install(self):
        try:
            # Create the Argo application for the addon
            chart_info = addon.get_chart_info()
            application = {
                "apiVersion": "argoproj.io/v1alpha1",
                "kind": "Application",
                "metadata": {
                    "name": f"{addon._meta.singular_name}-{addon.metadata.namespace}-{addon.metadata.name}",
                    "namespace": settings.argocd_namespace,
                },
                "spec": {
                    "project": "default",
                    # Target the Argo cluster created in response to the kubeconfig secret
                    "destination": {
                        "name": f"clusterapi-{cluster.metadata.namespace}-{cluster.metadata.name}",
                        "namespace": addon.spec.target_namespace,
                    },
                    "source": {
                        "repoURL": chart_info.repo,
                        "chart": chart_info.name,
                        "targetRevision": chart_info.version,
                        "helm": {
                            "releaseName": addon.spec.release_name or addon.metadata.name,
                            "values": yaml.safe_dump(
                                await addon.get_values(
                                    template_loader,
                                    ek_client,
                                    cluster,
                                    infra_cluster,
                                    cloud_identity
                                )
                            )
                        },
                    },
                },
            }
            _ = await ek_client.apply_object(application)
        # Handle expected errors by converting them to kopf errors
        # This suppresses the stack trace in logs/events
        # Let unexpected errors bubble without suppressing the stack trace so they can be debugged
        except ApiError as exc:
            if exc.status_code in {404, 409}:
                raise kopf.TemporaryError(str(exc), delay = settings.temporary_error_delay)
            else:
                raise
