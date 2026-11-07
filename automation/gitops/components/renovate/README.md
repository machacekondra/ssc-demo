# Renovate on-demand component

This component implements on-demand `/remediate-renovate` remediation.
It installs `renovate-config`, the `renovate` ServiceAccount, an optional
`renovate-ca` ConfigMap, the `renovate-validate` and
`renovate-remediate-dependency` Tasks, and the `cve-remediation-renovate` Pipeline.
Renovate runs inside a TaskRun; there is no CronJob or resident API service.
The existing issue EventListener and the binding/template in
`pipelines/triggers/renovate-remediation-trigger.yaml` provide the slash command.

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

To validate a particular backport as well, add the `FIXED_VERSION` parameter
using a version from the metadata summary. The task requires both that metadata
entry and the version's POM to be available. Do not assume an old demo fixture's
version exists in the current Lightwell catalog.

## Run the remediation action

On an open generated CVE issue, comment `/remediate-renovate`. The existing
six-field `cve-decision` issue marker supplies the requested coordinate and
versions. As with `/remediate`, YAML or JSON immediately after the command can
override the decision fields:

```text
/remediate-renovate
FIXED_VERSION: "2.2"
JUSTIFICATION: Use the selected available fix.
```

The commands match exactly: `/remediate` remains the AI path, while
`/remediate-renovate` starts only `cve-remediation-renovate`. The bot checks the
actual GitLab note and requires its author to have Developer access or higher.
Each note has a deterministic PipelineRun name; duplicate webhook deliveries
cannot start another run. Post a new comment to intentionally retry.

The task checks the current POM and verifies that the exact target exists in
Artifactory. For a directly declared dependency, Renovate uses GitLab mode and
its native Maven manager. For a transitive-only dependency, the task first
confirms the current version in the base Maven tree, then adds an explicit
project `dependencyManagement` override through GitLab's repository API. Both
paths create or reuse a deterministic draft MR. Only the selected POM may
change, and only the requested version change is accepted. Maven then runs
`verify` and produces a JSON dependency tree to confirm that the requested
coordinate resolves only to the fixed version. The bot marks the MR ready only
if that exact MR commit and its target branch have not moved during verification.
Failures leave the MR draft.

Maven gets the candidate source and Maven settings, but does not mount Renovate's
runtime/cache or receive the GitLab token. Settings are not dumped to logs.
No AI credential is needed. Merge remains a human action; a successful build does
not itself prove that an advisory considers the CVE fixed. Use the normal
SBOM/RHTPA flow to check the security result.

The action supports a dependency declared in the selected project's POM, with a
literal version or a property defined and used solely there. For a transitive-only
dependency, it first confirms the issue's current version against the base
project's resolved Maven tree, then adds an explicit project-level
`dependencyManagement` version override. The candidate must pass `mvn verify`,
and the resolved dependency tree must contain only the requested fixed version.
It rejects stale issue versions, shared/inherited properties, ambiguous
multi-version transitive resolutions, and unrelated POM changes. Maven uses
`dependencyManagement` to control versions of transitive dependencies; merge
remains a human action after verification. `SUBDIRECTORY` defaults to `.` because
it is a path within the Git repository, not the `source` workspace prefix used
by the older clone pipeline.

Results are `STATUS` (`ready`, `already-fixed`, `skipped`, or `dry-run`), `MR_URL`,
`MR_IID`, `BRANCH`, and `COMMIT_SHA`. An already-fixed base needs no new MR.
Generated runtime configuration ignores application-provided Renovate config,
restricts the repository and coordinate, and uses an exact escaped version rule.

For a read-only full simulation, start `cve-remediation-renovate` manually with
`DRY_RUN=true`, the same coordinate/version decision, and the `maven-settings`
workspace bound to its Secret. It clones and checks the real POM and metadata,
then runs Renovate with `dryRun=full`; it creates no branch or MR and skips
candidate compilation. Leave `REQUESTER_ID` and `NOTE_ID` empty only for manual
Kubernetes-authorized runs. Webhook runs always supply and verify those fields.

The Lightwell connection here is the existing Artifactory virtual repository,
which aggregates Lightwell remediated/validated packages and Maven Central.
The Lightwell `java-remediated` discovery preset is not loaded: it disables the
native Maven manager, whereas the planned action applies a fixed version already
selected in the issue. Periodic discovery remains a separate future feature.

`SELECTED=0` is an explicit no-op and returns `STATUS=skipped` without GitLab or
Artifactory requests. There are no periodic scans.

## Local checks

```sh
python3 -m unittest discover -s automation/gitops/components/renovate/tests -v
helm lint automation/gitops/components/renovate \
  --set gitlab.endpoint=https://gitlab.example.test/api/v4/
```
