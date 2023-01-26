import typing as t

from pydantic import Field, AnyHttpUrl, conint, constr, validator

from configomatic import Configuration as BaseConfiguration, Section, LoggingConfiguration


class ManifestsHelmChartConfiguration(Section):
    """
    Configuration for the Helm chart to use for manifests resources.
    """
    #: The Helm repository for the chart
    repo: AnyHttpUrl = "https://charts.helm.sh/incubator"
    #: The chart name
    name: constr(min_length = 1) = "raw"
    #: The chart version
    version: constr(min_length = 1) = "0.2.5"


class ArgoCDConfiguration(Section):
    """
    Configuration for the Argo CD integration.
    """
    #: The API version to use for Argo
    api_version: constr(min_length = 1) = "argoproj.io/v1alpha1"
    #: The namespace that Argo CD is running in
    namespace: constr(min_length = 1) = "argocd"
    #: The template to use for Argo cluster names
    cluster_template: constr(min_length = 1) = "clusterapi-{namespace}-{name}"
    #: Indicates whether to use self-healing for applications
    self_heal_applications: bool = True
    #: The finalizer indicating that an application should wait for resources to be deleted
    resource_deletion_finalizer: constr(min_length = 1) = "resources-finalizer.argocd.argoproj.io"
    #: The owner annotation, used to identify the addon that owns an Argo app
    owner_annotation: constr(min_length = 1) = "owner-reference"


class EventsRetryConfiguration(Section):
    """
    Configuration for the retries of the event handling.
    """
    #: The backoff in seconds
    backoff: conint(gt = 0) = 1
    #: The maximum wait time between retries
    max_interval: conint(gt = 0) = 120


class Configuration(BaseConfiguration):
    """
    Top-level configuration model.
    """
    class Config:
        default_path = "/etc/capi-addon-provider/config.yaml"
        path_env_var = "CAPI_ADDON_PROVIDER_CONFIG"
        env_prefix = "CAPI_ADDON_PROVIDER"

    #: The logging configuration
    logging: LoggingConfiguration = Field(default_factory = LoggingConfiguration)

    #: The API group of the cluster CRDs
    api_group: constr(min_length = 1) = "addons.stackhpc.com"
    #: The prefix to use for operator annotations
    annotation_prefix: constr(min_length = 1) = None
    #: A list of categories to place CRDs into
    crd_categories: t.List[constr(min_length = 1)] = Field(
        default_factory = lambda: ["cluster-api", "capi-addons"]
    )

    #: The field manager name to use for server-side apply
    easykube_field_manager: constr(min_length = 1) = "cluster-api-addon-provider"

    #: The delay to use for temporary errors by default
    temporary_error_delay: conint(gt = 0) = 15

    #: The retry configuration for events
    events_retry: EventsRetryConfiguration = Field(default_factory = EventsRetryConfiguration)

    #: The chart to use for manifests resources
    manifests_helm_chart: ManifestsHelmChartConfiguration = Field(
        default_factory = ManifestsHelmChartConfiguration
    )

    #: The Argo CD configuration
    argocd: ArgoCDConfiguration = Field(default_factory = ArgoCDConfiguration)

    #: Label indicating that an addon belongs to a cluster
    cluster_label: constr(min_length = 1) = None
    #: Label indicating the target namespace for the addon
    release_namespace_label: constr(min_length = 1) = None
    #: Label indicating the name of the release for the addon
    release_name_label: constr(min_length = 1) = None
    #: Label indicating that a configmap or secret should be watched for changes
    watch_label: constr(min_length = 1) = None
    #: Prefix to use for annotations containing a configmap checksum
    configmap_annotation_prefix: constr(min_length = 1) = None
    #: Prefix to use for annotations containing a secret checksum
    secret_annotation_prefix: constr(min_length = 1) = None

    @validator("annotation_prefix", pre = True, always = True)
    def default_annotation_prefix(cls, v, *, values, **kwargs):
        return v or values['api_group']

    @validator("cluster_label", pre = True, always = True)
    def default_cluster_label(cls, v, *, values, **kwargs):
        return v or f"{values['api_group']}/cluster"

    @validator("release_namespace_label", pre = True, always = True)
    def default_release_namespace_label(cls, v, *, values, **kwargs):
        return v or f"{values['api_group']}/release-namespace"

    @validator("release_name_label", pre = True, always = True)
    def default_release_name_label(cls, v, *, values, **kwargs):
        return v or f"{values['api_group']}/release-name"

    @validator("watch_label", pre = True, always = True)
    def default_watch_label(cls, v, *, values, **kwargs):
        return v or f"{values['api_group']}/watch"

    @validator("configmap_annotation_prefix", pre = True, always = True)
    def default_configmap_annotation_prefix(cls, v, *, values, **kwargs):
        return v or f"configmap.{values['annotation_prefix']}"

    @validator("secret_annotation_prefix", pre = True, always = True)
    def default_secret_annotation_prefix(cls, v, *, values, **kwargs):
        return v or f"secret.{values['annotation_prefix']}"


settings = Configuration()
