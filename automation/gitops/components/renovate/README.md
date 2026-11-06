# Renovate on-demand component

This component prepares Renovate for the planned `/remediate-renovate` action.
It installs `renovate-config`, the `renovate` ServiceAccount, an optional
`renovate-ca` ConfigMap, and the `renovate-validate` Tekton Task in the pipeline
namespace. There is no CronJob, Deployment, webhook, or automatic MR creation.
Renovate is a CLI run inside a TaskRun, not a resident API service.

The bootstrap chart enables the `ssc-renovate` Application at wave 9, after
GitLab has seeded the application and SCM Secret. As with the other components,
bootstrap must be rendered again to create the new Application; the resulting
Application then follows Git with automated sync.

## Configuration and credentials

`components.renovate` in `bootstrap-infra/values.yaml` controls the GitLab
endpoint, repository allowlist, Artifactory mirror, images, and public CA bundle.
The application endpoint derives from the cluster domain unless explicitly set.

The existing `scm-auth-secret` supplies `token` and optionally `username`.
The existing `maven-settings` Secret supplies `settings.xml`. The helper parses
the configured mirror ID and the **matching server ID**, including XML entities
and namespaces. Its mirror URL must agree with the configured registry URL.
Credentials are written only to pod-local `emptyDir` storage with mode 0600.
They are never rendered into Git, a ConfigMap, or a log dump.

For a self-signed ingress certificate, supply the public router CA under
`components.renovate.trustedCA.bundle` in the private bootstrap values file, or
set `existingConfigMap` and `key` to an existing CA bundle in the pipeline
namespace. The component configures both `NODE_EXTRA_CA_CERTS` and
`GIT_SSL_CAINFO`. It does not disable certificate verification.

The ServiceAccount has no Kubernetes API permissions and does not mount an API
token. Images are digest-pinned, workloads have no fixed UID, and filesystem
writes go to pod-local volumes for OpenShift restricted SCC compatibility.

## Validate the deployed configuration

The validation Task first checks Artifactory Maven metadata and, when supplied,
the requested version's POM. It then runs Renovate in **GitLab lookup dry-run
mode** against only the allowed repository and requested Maven coordinate.
This checks GitLab authentication/CA trust and Renovate's Maven lookup path;
it does not demonstrate POM editing, compile verification, or remediation.

```sh
oc -n tssc-app-ci create -f - <<'EOF'
apiVersion: tekton.dev/v1
kind: TaskRun
metadata:
  generateName: renovate-validate-
spec:
  serviceAccountName: renovate
  taskRef:
    name: renovate-validate
  timeout: 20m
  params:
    - name: REPOSITORY
      value: development/spring-boot-example-application
    - name: PACKAGE
      value: org.apache.commons:commons-lang3
    # Optional: require this exact Lightwell version and its POM to exist.
    - name: FIXED_VERSION
      value: 3.14.0.rhlw-00001
  workspaces:
    - name: maven-settings
      secret:
        secretName: maven-settings
EOF

tkn -n tssc-app-ci taskrun logs <taskrun-name> -f
```

Success produces `STATUS=validated`, an Artifactory metadata summary, and
Renovate's lookup output. No branches, dashboards, or merge requests are created.
The Task fails for an unknown repository, missing/mismatched credentials,
unavailable target version, TLS errors, or Renovate errors.

## Reuse for the remediation action

The future action can mount `renovate-config`, run `prepare.py`, and load
`base-config.cjs` with `{repository, packageName, fixedVersion}`. The base module
uses GitLab mode, the native Maven manager, Artifactory-only registry URLs,
draft MRs, no automerge, and an exact escaped version constraint when supplied.
It ignores application-provided Renovate configuration to keep the request
scope under the pipeline's control.

The Lightwell connection here is the existing Artifactory virtual repository,
which aggregates Lightwell remediated/validated packages and Maven Central.
The Lightwell `java-remediated` discovery preset is not loaded: it disables the
native Maven manager, whereas the planned action applies a fixed version already
selected in the issue. Periodic discovery remains a separate future feature.

The `/remediate-renovate` webhook, draft-MR execution, verification, and marking
the MR ready are subsequent work. Deploying this foundation does not activate
those actions.

## Local checks

```sh
python3 -m unittest discover -s automation/gitops/components/renovate/tests -v
helm lint automation/gitops/components/renovate \
  --set gitlab.endpoint=https://gitlab.example.test/api/v4/
```
