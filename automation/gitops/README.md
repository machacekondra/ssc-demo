# ssc-demo platform — GitOps install

Everything the supply-chain demo needs, other than the pipelines themselves,
installed by Argo CD onto a cluster that already has OpenShift GitOps running.

| Component | Where the chart comes from | What it gives the demo |
|---|---|---|
| `components/pipelines` | this repo | OpenShift Pipelines (Tekton) operator |
| `pipelines/` (repo root) | this repo, applied as plain YAML | the demo's 23 Tasks, 5 Pipelines, the issue-comment Triggers and `ai-agent-config` |
| `components/keycloak` | vendored from `redhat-ads-tech/ocp-app-platform-demo-helm` v1.4.2 | the `backstage` realm; `tpa-cli` / `tpa-frontend` / `trusted-artifact-signer` clients |
| `components/gitlab` | vendored from the same, heavily reworked | the application repository, the CVE issue board, the `/remediate` webhook, and the pipeline's SCM credential |
| `components/renovate` | this repo, pinned Renovate image | `/remediate-renovate` execution with draft MRs, Maven verification, and a read-only validation Task; no periodic scans |
| `components/quay` | vendored from the same, plus the robot-account wiring | the registry application images are published to, and the `quay-push` credential the pipelines push with |
| `components/rhtas` | vendored from the same | Fulcio / Rekor / CTlog / TSA / TUF for keyless signing |
| `components/rhtpa/tpa-prerequisites` | vendored from the same | RHTPA's Postgres, OIDC secret and S3 bucket |
| `components/rhtpa-operator` | RHTPA operator (`TrustedProfileAnalyzer`, `rhtpa.io/v1`), channel `stable-v3` | SBOM ingest, vulnerability analysis, backport/fix-version lookup. The only RHTPA install path — required for Exploit Intelligence |
| `artifactory/` | `https://charts.jfrog.io`, chart `artifactory-oss` | the Maven mirror. **Separate install — see `artifactory/INSTALL.md`.** |

`bootstrap-infra` is the app-of-apps. Artifactory is deliberately not part of
it: it has its own runbook, its own secrets and its own `oc apply` ordering,
and it already works.

## The Tekton layer

The operator and the pipelines it runs are installed separately, because they
are separate problems: `components/pipelines` is a Helm chart that subscribes
to the operator, and the demo's own Tekton resources are plain YAML applied
straight from `pipelines/` at the repo root — the same files
`pipelines/README.md` documents, not a copy. Four Applications, one per
directory:

| Application | Source | Wave |
|---|---|---|
| `ssc-tekton-tasks` | `pipelines/tasks/` — 23 Tasks | 3 |
| `ssc-tekton-config` | `pipelines/config/ai-agent-config.yaml` | 3 |
| `ssc-tekton-pipelines` | `pipelines/pipelines/` — the 4 agentic pipelines + one `maven-build-ci` | 4 |
| `ssc-tekton-triggers` | `pipelines/triggers/` — EventListener, bindings, templates, RBAC, Route | 5 |

Together they replace steps 3 and 5 of `pipelines/README.md`'s *One-time
setup* and step 4 of its trigger setup.

**Pick your `maven-build-ci`.** `pipelines/pipelines/` contains three files
that all declare `metadata.name: maven-build-ci`, and Argo CD fails an
Application whose directory yields two resources with the same identity. Set
`components.tekton.mavenBuildPipeline` to exactly one:

| Value | Pipeline |
|---|---|
| `maven-build-ci-pipeline-ai.yaml` (default) | build + sign + SBOM + the AI/RHTPA scan chain |
| `maven-build-ci-pipeline.yaml` | build + sign + SBOM + ACS gates |
| `maven-build-ci-pipeline-no-acs.yaml` | as above with the ACS gates removed |

All three reference only Tasks that `ssc-tekton-tasks` installs, so switching
is a values change and a re-sync.

**`pipelineNamespace` is effectively fixed at `tssc-app-ci`.**
`pipelines/triggers/agentic-issue-triggers.yaml` hardcodes that namespace as
the `ClusterRoleBinding` subject, and a plain-YAML Application cannot template
it. `bootstrap-infra` fails the render with an explanatory message if you
change one without the other, rather than letting you install an EventListener
that cannot reach the `cel` and `gitlab` ClusterInterceptors.

**Secrets stay out of it.** `pipelines/secrets/*.example.yaml` is never synced
— those are `REPLACE_ME` templates, and applying them would overwrite a live
credential with a placeholder. `ai-agent-secret`, `maven-settings` and
`tpa-secret` are created by hand (steps 5–7 below). `scm-auth-secret`,
`scm-auth-gitops-secret` and `gitlab-webhook-secret`, which used to be manual,
are now minted by the GitLab seeding job.

## Where this came from

The reference cluster this reproduces is not built from this repository. Its
platform comes from `redhat-ads-tech/ocp-app-platform-demo-helm` at tag
`v1.4.2` — a Red Hat workshop environment that also ships Developer Hub, Vault,
DevSpaces, Quay and an orchestrator. The charts here are that upstream with
everything ssc-demo does not use removed, the per-cluster values collapsed onto
a single `deployer.domain`, and three things added that the reference cluster
still had wired by hand:

* the GitLab group, imported application and gitops repositories, and the CVE
  labels,
* the issue-comment webhook that drives `agentic-cve-remediation`,
* the GitLab project access tokens the pipelines push and open MRs with,
  written straight into the pipeline namespace as `scm-auth-secret`
  (application repository) and `scm-auth-gitops-secret` (gitops repository).

Per-component deviations are documented at the top of each chart's
`values.yaml`.

## Prerequisites

* `oc` logged in as cluster-admin.
* OpenShift GitOps installed, with the default `openshift-gitops` Argo CD
  running and its application-controller holding cluster-admin (the default).
* A default RWO StorageClass. The install provisions PVCs for GitLab (10Gi),
  its Postgres and Redis, Keycloak's Postgres (5Gi) and RHTPA's Postgres
  (20Gi).
* **OpenShift Data Foundation.** Two components want NooBaa buckets: RHTPA's
  default S3 storage, and Quay's managed object storage. See *Storage* below
  for RHTPA's alternative; Quay has none short of turning it off.
* Egress to `charts.openshift.io`, `registry.redhat.io`, `quay.io`,
  `github.com` (the GitLab import and two of RHTPA's importers clone from
  there) and `redhat.com` (the CSAF importer).

## Install

> **Standing a cluster up from nothing?** Use **[`RUNBOOK.md`](RUNBOOK.md)**
> instead. It is the same install with Artifactory, the AI-backend choice, the
> RHTPA demo data and a smoke test folded into one ordered sequence. The steps
> below are the platform-only subset, kept here because the rest of this file
> explains them.

### 1. Collect the cluster domain

```sh
oc get ingresses.config/cluster -o jsonpath='{.spec.domain}'
```

### 2. Write an uncommitted overrides file

Four credentials have no sensible default and must not live in git. Put them,
with the domain, in a file you keep out of the repository:

```sh
cat > /tmp/ssc-demo-values.yaml <<EOF
deployer:
  domain: $(oc get ingresses.config/cluster -o jsonpath='{.spec.domain}')
credentials:
  keycloakDbPassword: $(openssl rand -hex 16)
  gitlabRootPassword: $(openssl rand -hex 16)
  tpaDbPassword: $(openssl rand -hex 16)
  tpaCliClientSecret: $(openssl rand -hex 16)
  quayAdminPassword: $(openssl rand -hex 16)
EOF
chmod 600 /tmp/ssc-demo-values.yaml
```

Keep this file. `tpaCliClientSecret` in particular is needed again in step 5,
`gitlabRootPassword` is how you log into GitLab as root, and
`quayAdminPassword` is how you log into Quay as `quayadmin`.

If you want a `maven-build-ci` other than the AI one, add it here too:

```yaml
components:
  tekton:
    mavenBuildPipeline: maven-build-ci-pipeline.yaml
```

### 3. Create the Applications

```sh
helm template ssc automation/gitops/bootstrap-infra \
  -f /tmp/ssc-demo-values.yaml | oc apply -f -
```

The waves then run themselves: pipelines operator (0) → Tekton tasks + config
(3) → Tekton pipelines (4) → keycloak + Tekton triggers (5) → gitlab (8) →
quay + rhtas + rhtpa-prerequisites (10) → rhtpa (15). A wave does not start
until the previous one is Healthy, so an Application stuck in Progressing
blocks everything after it.

The Tekton Applications usually fail their first sync or two — the operator's
CRDs are still being installed when wave 3 is reached. That is what the
30-attempt retry and `SkipDryRunOnMissingResource` are for; leave them alone
and they converge.

```sh
oc -n openshift-gitops get applications -l app.kubernetes.io/part-of=ssc-demo
```

Expect 20–30 minutes for the operators and GitLab, then hours for RHTPA's
first full ingest of the CVE, OSV and CSAF feeds.

### 4. Check what the seeding jobs wrote

The GitLab seeding job writes the SCM tokens and webhook secret into the
pipeline namespace. The optional Quay component also creates `quay-push` when
enabled; this install targets public Quay.io, so its push credential is created
separately as described in RUNBOOK §7.

```sh
oc -n gitlab        get job initialize-gitlab
# Only when the optional in-cluster Quay component is enabled:
oc -n quay-registry get job quay-noobaa quay-config
oc -n tssc-app-ci   get secret scm-auth-secret scm-auth-gitops-secret gitlab-webhook-secret
oc -n tssc-app-ci   get sa pipeline -o jsonpath='{.secrets[*].name}{"\n"}'
```

`scm-auth-secret` holds a GitLab **project** access token scoped to the one
application repository, not the root PAT. `scm-auth-gitops-secret` holds a
second one scoped to the gitops repository: a GitLab project-bot user is a
member of exactly one project and cannot be added to another, so the gitops
repo (which `gitops-cd-dev` pushes promotion branches to) needs its own token.
If either Secret already exists and holds a real token, the Job leaves it
alone — so re-running the seeding does not invalidate a credential a pipeline
is mid-run with. The seeding also imports the gitops repository and sets its
chart's `image.repository` to the same public Quay.io image path CI publishes;
the promotion pipeline only rewrites `image.tag`.

CI currently publishes to `quay.io/machacekondra`. Since the optional
in-cluster Quay chart is disabled by default, `quay-push` must be created with a
Quay.io robot token and linked to the `pipeline` ServiceAccount (see RUNBOOK
§7). If the in-cluster Quay chart is enabled instead, it creates its own
`quay-push` Secret and the `output-image` repository in the TriggerTemplate
must be changed to that registry.

### 5. Finish the RHTPA credential

`tpa-cli`'s client secret reaches Keycloak and RHTPA through the chart, but the
Tekton tasks read it from their own Secret, which is not managed here. The
pipelines pass `tpa-secret` as `TRUSTIFICATION_SECRET_NAME`, and
`upload-sbom-to-trustification` requires exactly these four keys:

```sh
DOMAIN=$(oc get ingresses.config/cluster -o jsonpath='{.spec.domain}')
oc -n tssc-app-ci create secret generic tpa-secret \
  --from-literal=bombastic_api_url="https://server-trusted-profile-analyzer.${DOMAIN}" \
  --from-literal=oidc_issuer_url="https://sso.${DOMAIN}/realms/backstage" \
  --from-literal=oidc_client_id=tpa-cli \
  --from-literal=oidc_client_secret='<tpaCliClientSecret from step 2>' \
  --dry-run=client -o yaml | oc apply -f -
```

A wrong `oidc_client_secret` here is invisible until a build reaches the
upload step and the token request comes back 401.

### 6. Create the AI backend credential

`ai-agent-config` (the provider/model switch) is synced by
`ssc-tekton-config`; the key it authenticates with is not.

```sh
oc -n tssc-app-ci create secret generic ai-agent-secret \
  --from-literal=ANTHROPIC_API_KEY='<your key>'
```

Use the key your `AI_PROVIDER` needs — see *Swapping the AI backend* in
`pipelines/README.md` if you are not on the default `anthropic`.

### 7. Create the Maven settings Secret

Every build resolves through the Artifactory `maven` virtual repo, and
Artifactory OSS has anonymous access off, so `settings.xml` carries a
credential and this must be a Secret rather than a ConfigMap. Point the mirror
at the in-cluster Service, not the Route:

```sh
oc -n tssc-app-ci create secret generic maven-settings \
  --from-file=settings.xml=./settings.xml
```

See `pipelines/secrets/maven-settings-secret.example.yaml`. Create it before
the first run: a secret-backed workspace whose Secret is missing leaves the
pod in `PodInitializing` indefinitely — it never errors, it just hangs.

### 8. Verify the Tekton layer

```sh
oc -n tssc-app-ci get tasks | wc -l           # 23 + header
oc -n tssc-app-ci get pipelines               # 4 agentic + maven-build-ci
oc -n tssc-app-ci get eventlistener agentic-issue-commands   # ADDRESS populated
oc get clusterinterceptors                    # at least: cel, gitlab
```

The webhook itself needs nothing further: the GitLab seeding job registered it
against the Route this layer creates, using the token in
`gitlab-webhook-secret`. Comment `/remediate` on a CVE issue to exercise it.

## Storage

RHTPA stores SBOMs and advisory documents in object storage, and the reference
cluster uses ODF: the prerequisites chart creates an `ObjectBucketClaim`
against `openshift-storage.noobaa.io`, and RHTPA reads the resulting Secret.
That is the default here.

Quay wants a NooBaa bucket too, raised by its operator from the managed
`objectstorage` component. Unlike RHTPA it has no filesystem fallback worth
having, so on a cluster without ODF the answer is
`components.quay.enabled: false` plus an `output-image` on some other registry.
Quay's Application also ships a pre-flight Job that raises the default
BackingStore's memory from ODF's 256Mi to 1Gi — below ~400Mi the agent pod is
rejected and no bucket is ever provisioned. See `components/quay/README.md`.

Without ODF, switch to filesystem storage **in both places at once** —
`components.rhtpa.storage.type` drives the RHTPA chart, and the prerequisites
chart only skips the bucket when it sees the same value:

```yaml
components:
  rhtpa:
    storage:
      type: filesystem
      storageClassName: <an RWX class>
```

Be aware of what you are accepting. Chart 1.2.6 mounts one PVC, named
`storage`, into **both** the server and the importer Deployment — and
hardcodes `accessModes: [ReadWriteOnce]` on it, with no value to override.
An RWX-capable storage class does not help: the claim still requests RWO, so
Kubernetes will only schedule the two pods together, and the second stays
Pending whenever it cannot land on the first one's node. Filesystem mode works
for a single-node-ish demo and is fragile anywhere else.

S3 is the path the reference cluster uses and the one to prefer.

## Timeouts

Two routes in this platform need HAProxy's `timeout server` raised from its
30 s default, for reasons documented where they are set:

* **RHTPA** (`bootstrap-infra/values.yaml`, `components.rhtpa.routeTimeout`) —
  `/purl/recommend` cannot complete inside 30 s at any batch size. RHTPA's
  Route is generated from an Ingress by the ingress-to-route controller and
  carries an `ownerReference`, so annotating the Route directly is reverted;
  the annotation goes on the Ingress through the chart's
  `ingress.additionalAnnotations`.
* **GitLab** (`components/gitlab/templates/rt-gitlab.yaml`) — the seeding job's
  repository import is a server-side clone of a GitHub repository held open on
  the API connection, and the pipeline pushes remediation branches through the
  same Route. This Route is created directly, so the annotation sits on it.

Artifactory has the same problem and solves it differently — the pipeline
talks to its in-cluster Service and skips the router entirely. See the
Artifactory note in `pipelines/README.md`.

## Things to know before you edit

* **Every Application has `selfHeal: true`.** An `oc edit` or `oc annotate`
  against a managed resource is reverted on the next reconcile. Change the
  chart.
* **The Keycloak realm re-randomises on every render.** Unset client secrets
  default to a fresh `randAlphaNum 32` and resource ids to a fresh `uuidv4`,
  so the rendered `KeycloakRealmImport` never matches the live one. The
  Application ignores differences on `.id`, `.containerId` and `.secret`;
  without that, selfHeal would rotate the secret of every client in use.
* **The GitLab and Quay seeding Jobs are `Replace=true`.** A Job's pod template
  is immutable, so editing a playbook would otherwise fail the sync with "field
  is immutable" and silently keep the old seeding. Replace is also how you
  re-seed: change the ConfigMap and sync. The corollary is that those playbooks
  re-run on every chart edit, so every API call in them has to tolerate its own
  previous success.
* **`quay-admin-token` is the only copy of a token that cannot be reissued.**
  Quay's `/api/v1/user/initialize` works exactly once, while the user table is
  empty. The seeding job stores the token it returns in that Secret; nothing
  renders it from the chart, so Argo does not track it and will not prune it.
  Delete it on a live registry and the job can no longer authenticate — it
  fails with instructions to mint a replacement in the Quay UI.
* **The Tekton Applications sync plain YAML from `pipelines/`**, so nothing
  about them is templated — editing a Task or Pipeline there changes what the
  cluster runs on the next reconcile, with no Helm values in between. The flip
  side is that `selfHeal` will revert a `oc edit task ...`, and anything that
  needs to vary per cluster (the hardcoded trigger namespace, the stale RHTAS
  defaults on `maven-build-ci-pipeline-ai.yaml`) has to be fixed in the file.
* **`components/lightwell-repo`** is the older Nexus-based artifact repository.
  It is not wired into `bootstrap-infra` — Artifactory replaced it — and is
  left in the tree only for reference.
