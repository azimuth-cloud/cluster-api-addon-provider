# cluster-api-addon-provider

The Cluster API addon provider is a
[Kubernetes operator](https://kubernetes.io/docs/concepts/extend-kubernetes/operator/)
that provides
[custom resources](https://kubernetes.io/docs/concepts/extend-kubernetes/api-extension/custom-resources/)
for defining addons for [Cluster API](https://cluster-api.sigs.k8s.io/) clusters.

The resources exposed by this provider differ from the
[Cluster API ClusterResourceSet](https://cluster-api.sigs.k8s.io/tasks/experimental-features/cluster-resource-set.html) in the following significant ways:

  1. They support reconciliation, i.e. the resources on the target cluster are updated
     when the addon resource is modified. `ClusterResourceSet` is a one-shot dump of manifests
     onto the target cluster.
  2. They can install addons using Helm charts as well as plain manifests.
  3. Addons are able to access information about the Cluster API cluster and use it to
     generate configuration (e.g. Helm values or manifests). This is especially useful
     for values that are unknown at the time the addon resources are created, e.g. the
     ID of a network that is created by an infrastructure provider.

The Cluster API addon provider currently supports two sources for addons:

  * A Helm chart from a [chart repository](https://helm.sh/docs/topics/chart_repository/)
  * Vanilla manifests

Additional addon sources (e.g. `Kustomization`s) and Helm chart sources (e.g. fixed URLs
and OCI repositories) will likely be added in the future.

## Installation

The Cluster API addon provider can be installed using [Helm](https://helm.sh):

```sh
helm repo add capi-addons https://stackhpc.github.io/cluster-api-addon-provider

# Use the latest version from the main branch
helm upgrade \
  cluster-api-addon-provider \
  capi-addons/cluster-api-addon-provider \
  --install \
  --version ">=0.1.0-dev.0.main.0,<0.1.0-dev.0.main.9999999999"
```

## Templates

Some elements of the addon resources, such as values sources for `HelmRelease` or
manifest sources for `Manifests`, are treated as templates that have access to information
about the target Cluster API cluster.

Templates use the [Jinja2](https://jinja.palletsprojects.com/en/3.1.x/templates/) syntax.

### Variables

Templates have access to the following variables:

  * `addon`  
    The addon object itself (e.g. the `HelmRelease` or `Manifests` object).
  * `cluster`  
    The target Cluster API cluster object.
  * `infra_cluster`  
    The infrastructure cluster for the target Cluster API cluster (i.e. the object referenced
    in the `spec.infrastructureRef` field of the cluster). The `kind` of the object depends on
    the infrastructure provider that is being used.
  * `cloud_identity`  
    The identity object for the infrastructure cluster, if one exists (i.e. the object referenced
    in the `spec.identityRef` field of the infrastructure cluster). The `kind` of the object, and
    whether it exists at all, depends on the infrastructure provider in use. In some cases, it is
    a `Secret` (e.g. for OpenStack) but in other cases it may be another CRD (e.g. AWS).

### Filters

The following custom filters are also made available to templates in addition to the
[Jinja2 builtin filters](https://jinja.palletsprojects.com/en/3.1.x/templates/#builtin-filters):

  * `mergeconcat`  
    Recursively merges two or more dictionaries, with lists being concatenated.
  * `fromyaml`  
    Parses a YAML document into an object.
  * `toyaml`  
    Renders the given Python object as YAML.
  * `b64encode`  
    Encodes the given value as base64, e.g. for secret data.
  * `b64decode`  
    Decodes the given base64-encoded data and returns a UTF-8 string.

## HelmRelease

To install a Helm chart onto a Cluster API cluster, you can create a `HelmRelease` in
the same namespace as the Cluster API `Cluster` resource:

```yaml
apiVersion: addons.stackhpc.com/v1alpha1
kind: HelmRelease
metadata:
  name: my-cluster-addon
spec:
  # The name of the target Cluster API cluster
  clusterName: example
  # Indicates whether the addon is part of cluster bootstrapping
  #   If true, the addon is installed as soon as the control plane is initialised
  #   If false, the addon is only installed once the cluster is ready
  #   Defaults to false if not given
  bootstrap: true
  # The namespace on the target cluster to create the Helm release in
  targetNamespace: addon-namespace
  # The name of the release on the target cluster
  releaseName: my-addon
  # The time to wait for the release components to become ready
  #   If not given, the default timeout of 1h will be used
  releaseTimeout: 30m
  # Details of the Helm chart to use
  chart:
    # The chart repository that contains the chart to use
    repo: https://my-project/charts
    # The name of the chart to use
    name: my-addon
    # The version of the chart to use (must be an exact version)
    version: 1.5.0
  # The sources of values to use for the Helm release
  #   The values from each source are recursively merged, and lists are concatenated
  #   Sources later in the list take precedence when there is a conflict
  valuesSources:
    # Read values from a key in a configmap in the same namespace
    #   The value is rendered as a template (see above) then parsed as YAML
    - configMap:
        # The name of the configmap
        name: my-cluster-addon-config
        # The key in the configmap to use, defaults to values.yaml if not given
        key: values.yaml
    # Read values from a key in a secret in the same namespace
    #   The value is rendered as a template (see above) then parsed as YAML
    - secret:
        # The name of the secret
        name: my-cluster-addon-secret-config
        # The key in the secret to use, defaults to values.yaml if not given
        key: values.yaml
    # Specify an inline template
    #   The template is rendered (see above) then parsed as YAML
    - template: |
        some:
          values:
            podCidrs:
              {{ cluster.spec.clusterNetwork.pods.cidrBlocks | toyaml | indent(6) }}
```

## Manifests

To install manifests onto a Cluster API cluster that are not packaged as a Helm chart,
you can create a `Manifests` object in the same namespace as the Cluster API `Cluster` object:

> **Ephemeral Helm charts**
>
> Even though a `Manifests` resource specifies vanilla manifests, we want to have
> "release semantics" for those manifests on the target cluster. In particular if a
> resource is removed from the `spec.manifestSources` of a `Manifests` object we want
> it to also be removed from the target cluster.
>
> These semantics are exactly what Helm releases give us. Rather than re-implement the
> revision and resource tracking in this operator, we instead use Helm to do this even
> when we do not have a chart to use.
>
> To do this, an "ephemeral" Helm chart is generated from the manifests which is then used
> to update a Helm release for the addon. In this way, the resources on the target cluster
> always match those generated by the manifests at any point in time. It also gives us
> the benefit of maintaining a history for the addon.

```yaml
apiVersion: addons.stackhpc.com/v1alpha1
kind: Manifests
metadata:
  name: my-cluster-addon
spec:
  # The name of the target Cluster API cluster
  clusterName: example
  # Indicates whether the addon is part of cluster bootstrapping
  #   If true, the addon is installed as soon as the control plane is initialised
  #   If false, the addon is only installed once the cluster is ready
  #   Defaults to false if not given
  bootstrap: true
  # The namespace on the target cluster to create the resources in
  targetNamespace: addon-namespace
  # The name of the Helm release for the ephemeral chart on the target cluster
  releaseName: my-addon
  # The time to wait for the release components to become ready
  #   If not given, the default timeout of 1h will be used
  releaseTimeout: 30m
  # The sources of manifests
  manifestSources:
    # Read manifests from the keys of a configmap
    #   Each key is rendered as a template (see above) then parsed as YAML
    - configMap:
        # The name of the configmap
        name: my-cluster-addon-config
        # A list of keys to use
        #   If not given, all keys are used
        keys: [resource1.yaml, resource2.yaml]
        # A list of keys to exclude
        #   If not given, all keys are used
        #   Takes precedence over "keys" if the same key appears in both
        exclude_keys: [not-a-resource.json]
    # Read manifests from the keys of a secret
    #   Each key is rendered as a template (see above) then parsed as YAML
    - secret:
        # The name of the configmap
        name: my-cluster-addon-config
        # A list of keys to use
        #   If not given, all keys are used
        keys: [resource1.yaml, resource2.yaml]
        # A list of keys to exclude
        #   If not given, all keys are used
        #   Takes precedence over "keys" if the same key appears in both
        exclude_keys: [not-a-resource.json]
    # Specify an inline template
    #   The template is rendered (see above) then parsed as YAML
    - template: |
        apiVersion: v1
        kind: Secret
        metadata:
          name: cloud-identity
        data:
          {{ cloud_identity.data | toyaml | indent(2) }}
```
