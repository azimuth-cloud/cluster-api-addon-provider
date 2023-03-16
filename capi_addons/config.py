import typing as t

from pydantic import Field, conint, constr, validator

from configomatic import Configuration as BaseConfiguration, Section, LoggingConfiguration


class HelmClientConfiguration(Section):
    """
    Configuration for the Helm client.
    """
    #: The default timeout to use with Helm releases
    #: Can be an integer number of seconds or a duration string like 5m, 5h
    default_timeout: t.Union[int, constr(min_length = 1)] = "1h"
    #: The executable to use
    #: By default, we assume Helm is on the PATH
    executable: constr(min_length = 1) = "helm"
    #: The maximum number of revisions to retain in the history of releases
    history_max_revisions: int = 10
    #: Indicates whether to verify TLS when pulling charts
    insecure_skip_tls_verify: bool = False
    #: The directory to use for unpacking charts
    #: By default, the system temporary directory is used
    unpack_directory: t.Optional[str] = None


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

    #: The Helm client configuration
    helm_client: HelmClientConfiguration = Field(default_factory = HelmClientConfiguration)

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
    #: Annotation to use to trigger restarts of workloads in lifecycle hooks
    lifecycle_hook_restart_annotation: constr(min_length = 1) = None

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

    @validator("lifecycle_hook_restart_annotation", pre = True, always = True)
    def default_lifecycle_hook_restart_annotation(cls, v, *, values, **kwargs):
        return v or f"{values['annotation_prefix']}/restarted-at"


settings = Configuration()
