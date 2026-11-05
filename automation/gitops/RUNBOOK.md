# Fresh-cluster runbook — the whole ssc-demo environment

Every step, in order, to stand the demo up on a cluster that has nothing on it
but OpenShift GitOps. You run all of it; nothing here assumes anything was done
for you.

`README.md` next to this file explains *why* each component is shaped the way it
is. This file is the ordered list of commands.

**Read §0 and §2 before you start.** Two decisions (the AI backend and which
fork Argo CD pulls from) are git edits, and making them after the install means
re-syncing rather than installing.

## What you end up with

| | Hostname | Signed in as |
|---|---|---|
| Keycloak | `https://sso.<domain>` | `keycloak-initial-admin` Secret (operator-generated) |
| GitLab | `https://gitlab-gitlab.<domain>` | `root` / `gitlabRootPassword` |
| Quay | `https://quay.<domain>` | `quayadmin` / `quayAdminPassword` |
| Artifactory | `https://artifactory-artifactory.<domain>` | `admin` / §1 password |
| RHTPA | `https://server-trusted-profile-analyzer.<domain>` | OIDC via Keycloak |
| RHTAS | — (Fulcio/Rekor/TUF Services, no UI) | — |
| Tekton | namespace `tssc-app-ci` | — |

Plus: the `development/spring-boot-example-application` repository imported into
GitLab with the CVE issue board, the `/remediate` and `/generate-tests*` webhook,
23 Tasks, 5 Pipelines, and every credential the pipelines need.

Budget **2–3 hours of wall clock**, most of it unattended. RHTPA's first full
ingest of the CVE/OSV/CSAF feeds runs for several hours *after* that, in the
background — you do not have to wait for it before §9.

## 0. Preflight

```sh
oc whoami                                      # must be cluster-admin
oc get ingresses.config/cluster -o jsonpath='{.spec.domain}{"\n"}'
oc -n openshift-gitops get pods | grep application-controller      # Running
oc get storageclass                            # one must be (default)
oc get storagecluster -A                       # ODF — see below
```

**Grant the Argo CD controller cluster-admin before anything else.** On a
fresh cluster the application-controller's service account has no write
rights outside its own namespace — the first Application (Artifactory, §1)
stalls with `cannot patch resource ... in the namespace "artifactory"`
against every kind except ConfigMaps. The demo genuinely needs the grant, not
just this Application: the app-of-apps syncs namespaces (`CreateNamespace`),
SCCs (`artifactory-scc`), ClusterRoleBindings (trigger RBAC, GitLab seeding
jobs) and OLM Operators (Pipelines, RHTPA) through the same controller. It
must be re-done if the GitOps instance is ever recreated:

```sh
oc adm policy add-cluster-role-to-user cluster-admin \
  system:serviceaccount:openshift-gitops:openshift-gitops-argocd-application-controller
oc auth can-i patch services -n artifactory \
  --as=system:serviceaccount:openshift-gitops:openshift-gitops-argocd-application-controller   # must print yes
```

**Raise the Argo CD application-controller's memory limit before §5.** The
default `openshift-gitops` instance ships the controller with a 2Gi limit —
not enough to reconcile this app-of-apps. The initial all-at-once bootstrap
(GitLab + Quay + RHTAS + RHTPA + Tekton charts, plus a full cluster's worth
of watched objects) OOM-kills it repeatedly, and the failure looks nothing
like a memory problem: Applications sit `OutOfSync`/`Running` with
`waiting for healthy state of ...` messages naming trivially-healthy
resources (a ConfigMap, a Namespace, a ServiceAccount), later-wave resources
show `Missing`, GitLab never deploys a single pod (so anything tracking the
GitLab-hosted gitops repo stays `Unknown` with a 503), and progress only
inches forward between crashes. Check for the signature first:

```sh
oc -n openshift-gitops get pod openshift-gitops-application-controller-0 \
  -o jsonpath='{"restarts="}{.status.containerStatuses[0].restartCount}{" "}{.status.containerStatuses[0].lastState}{"\n"}'
# restarts>0 with {"exitCode":137,"reason":"OOMKilled"} means yes
```

Fix (4–8Gi is comfortable; the operator updates the StatefulSet in place):

```sh
oc -n openshift-gitops patch argocd openshift-gitops --type=merge \
  -p '{"spec":{"controller":{"resources":{"requests":{"memory":"2Gi"},"limits":{"memory":"8Gi"}}}}}'
```

**ODF is a hard requirement** for two components: RHTPA's default `s3` storage
and Quay's managed object storage both raise an `ObjectBucketClaim` against
NooBaa in `openshift-storage`.

If `oc get storagecluster -A` comes back empty — or errors with `the server
doesn't have a resource type "storagecluster"`, which is the same answer on a
cluster that has never had ODF — you have three options:

1. **Let the app-of-apps install it.** Set `components.odf.enabled: true` in
   your §4 overrides, with `storageClassName` pointing at a *block* class
   (`gp3-csi` on AWS, `thin-csi` on vSphere) and `channel` matching the
   cluster's OpenShift minor. See `components/odf/README.md`. Budget an extra
   15–25 minutes, and read the note in `bootstrap-infra/values.yaml` about what
   that does to the wave ordering before you decide where to put it.
2. **Install ODF separately**, ahead of §5 — the same chart, staged by hand.
   `components/odf/README.md` has the commands.
3. **Go without it**: read *Storage* in `README.md` for RHTPA's filesystem
   fallback and turn Quay off (`components.quay.enabled: false`). Be aware the
   fallback mounts one RWO PVC into two Deployments.

On a cluster that already has ODF — most Red Hat workshop clusters, including
the reference cluster — leave `components.odf.enabled` at its default `false`.
A second StorageCluster is not something you back out of cleanly.

**Check whether the cluster already has a Keycloak, before anything else.**

```sh
oc get ns keycloak
oc get oauth cluster -o jsonpath='{range .spec.identityProviders[*]}{.name}{" "}{.type}{"\n"}{end}'
oc get user $(oc whoami) -o jsonpath='{.identities}{"\n"}'
oc -n kube-system get secret kubeadmin
```

If an `OpenID` identity provider exists, your own user resolves through it, and
there is no `kubeadmin` secret, then that Keycloak **is the cluster's login** —
deleting it locks everyone out, and installing a second one alongside does not
work because `applications.yaml` derives the issuer host as `sso.<domain>`,
which its Route already holds. Use the bring-your-own path instead:

```yaml
components:
  keycloak:
    enabled: false
    realm: <the realm it already serves>
  keycloakClients:
    enabled: true
```

See `components/keycloak-clients/README.md`. On a clean cluster, leave both at
their defaults and the `keycloak` component installs its own.

Egress needed: `charts.openshift.io`, `charts.jfrog.io`, `registry.redhat.io`,
`quay.io`, `releases-docker.jfrog.io`, `github.com`, `redhat.com`,
`packages.redhat.com`, and whatever endpoint your AI backend lives behind.

Set this once; every later step uses it:

```sh
export DOMAIN=$(oc get ingresses.config/cluster -o jsonpath='{.spec.domain}')
export NS=tssc-app-ci
```

## 1. Artifactory

Artifactory is **not** part of the app-of-apps — it has its own Application,
its own secrets and its own ordering. Do it first: the `maven-settings` Secret
in §8 needs its admin password, and every build resolves dependencies through
it.

Follow `artifactory/INSTALL.md` end to end (§1–§6). It is four `oc apply`s and
three `oc create secret`s. Keep the admin password.

```sh
oc -n artifactory get pods            # artifactory-0 8/8 Running
curl -sk "https://$(oc -n artifactory get route artifactory -o jsonpath='{.spec.host}')/artifactory/api/system/ping"
```

## 2. Point Argo CD at a repository you can push to

`bootstrap-infra/values.yaml` ships:

```yaml
gitops:
  repoUrl: "https://github.com/lzspangler/ssc-demo.git"
  revision: "ssc-demo"
```

Argo CD pulls the charts **and the Tekton YAML** from there, not from your
working copy. Anything you change locally has no effect until it is pushed to
the branch Argo is watching. If you are going to edit the pipelines — and §3
says you probably are — fork or push a branch now and override both values in
the file you create in §4.

## 3. Choose the AI backend  ← decide before installing

The pipelines read their backend from the `ai-agent-config` ConfigMap, which
`ssc-tekton-config` syncs **as plain YAML from `pipelines/config/` with
`selfHeal: true`**. There is no Helm value for it. An `oc edit` on the live
ConfigMap is reverted within minutes, so this choice is a commit.

**aider + an OpenAI-compatible endpoint** (gpt-oss, Granite, vLLM, Ollama,
OpenShift AI, watsonx) — **this is the committed default**:

```yaml
data:
  AI_PROVIDER: "openai"
  AI_AGENT:    "aider"
  AI_MODEL:    "gpt-oss-120b"
  AI_BASE_URL: "https://maas-rhdp.apps.maas.redhatworkshops.io/v1"
```

Change `AI_MODEL`/`AI_BASE_URL` for a different endpoint. `AI_MODEL` may be a
bare id (litellm assumes the `openai/` provider) or a full litellm string such
as `ollama_chat/granite3.3:8b`. `agent-image` already defaults to
`ai-agent-maven-aider`, so no pipeline edit is needed.

The shipped default also sets `AI_TLS_INSECURE: "true"` and
`SSL_VERIFY: "False"`. That is **demo-only**. It is on because the SDKs verify
against certifi's public roots only, and the `ai-python` image fails the
handshake with `CERTIFICATE_VERIFY_FAILED: unable to get local issuer
certificate` against this endpoint even where plain `curl` from the same UBI9
base succeeds. For anything beyond a demo, trust the CA instead: populate the
`ai-endpoint-ca` ConfigMap, set `AI_CA_BUNDLE` to the mounted path, and set
`SSL_VERIFY` to the same path for the aider tasks. Plain in-cluster
`http://…svc` sidesteps TLS entirely.

**Anthropic + Claude Code** — set `AI_PROVIDER: anthropic`,
`AI_AGENT: claude-code`, `AI_MODEL: claude-opus-5`, clear `AI_BASE_URL`, and set
`agent-image` to an `ai-agent-maven-claude` build on the PipelineRun (or edit
the default in the pipelines).

**Whichever you pick, the companion Secret must NOT contain an `AI_PROVIDER`
key.** The AI tasks use
`envFrom: [configMapRef: ai-agent-config, secretRef: ai-agent-secret]`, and the
last source wins — so an `AI_PROVIDER` in the Secret silently overrides this
ConfigMap, and the only symptom is the wrong SDK being used.

Either way, commit and push before §5:

```sh
git -C <your clone> commit -am "pick AI backend" && git push
```

## 3b. RHTPA is installed by its operator

There is no choice to make here any more, but it is worth knowing: RHTPA is
installed by the RHTPA operator (`TrustedProfileAnalyzer`, `rhtpa.io/v1`) via
`components.rhtpaOperator`, which is enabled by default. The
`charts.openshift.io` Helm path has been removed — Exploit Intelligence
integrates through `spec.exploitIntelligence` on that CR and has no
Helm-chart equivalent.

`components.rhtpaPrerequisites` stays enabled and owns Postgres and
`oidc-tpa-cli`. It creates the S3 ObjectBucketClaim and `trustify` Secret only
in S3 mode. The CR reads `storage` from `components.rhtpaOperator`; filesystem
mode also requires `components.rhtpaOperator.storage.size` for its PVC.

Two things that will bite if you edit the CR, both in
`components/rhtpa-operator/README.md`:

* The CRD declares `spec` with `x-kubernetes-preserve-unknown-fields` and no
  property schema, so the API server accepts **any** shape. A misspelt key is
  not rejected — it silently has no effect.
* Several chart templates call `deepCopy` on top-level keys such as
  `migrateDatabase` and `createDatabase`. If those keys are absent the whole
  render aborts with `reflect: call of reflect.Value.Type on zero Value`, the
  CR is accepted, and the operator creates nothing — visible only as
  `Irreconcilable` on the CR status. The empty-object scaffolding in the CR
  template is there for this; do not tidy it away.

If you are migrating a cluster that already ran the Helm chart, follow the
migration section of that README. Budget time: an in-place 2.x -> 3.x data
migration of a ~2.7 GB database measured around 90 minutes, and RHTPA is down
for all of it.

## 4. Write an uncommitted overrides file

Five credentials have no default and must not reach git.

```sh
cat > /tmp/ssc-demo-values.yaml <<EOF
deployer:
  domain: ${DOMAIN}
credentials:
  keycloakDbPassword: $(openssl rand -hex 16)
  gitlabRootPassword: $(openssl rand -hex 16)
  tpaDbPassword: $(openssl rand -hex 16)
  tpaCliClientSecret: $(openssl rand -hex 16)
  quayAdminPassword: $(openssl rand -hex 16)
EOF
chmod 600 /tmp/ssc-demo-values.yaml
```

Add your fork from §2 if you made one:

```yaml
gitops:
  repoUrl: "https://github.com/<you>/ssc-demo.git"
  revision: "<your branch>"
```

And ODF, if §0 said you need it:

```yaml
components:
  odf:
    enabled: true
    channel: stable-4.21      # must match the cluster's OpenShift minor
    storageClassName: gp3-csi # a BLOCK class, not the demo's general-purpose one
```

**Keep this file.** `tpaCliClientSecret` is needed again in §8, and the other
three are the only copies of the GitLab, Quay and database passwords.

## 5. Create the Applications

```sh
helm template ssc automation/gitops/bootstrap-infra \
  -f /tmp/ssc-demo-values.yaml | oc apply -f -
```

**Pushing chart changes does NOT update the cluster.** No Argo Application
tracks `bootstrap-infra` itself — it is the layer that *creates* the Argo
Applications, and it is only ever read at this `helm template` step. So after
changing anything under `automation/gitops/bootstrap-infra/`, re-run the §5
command to converge the cluster. `oc apply` makes it idempotent and adopts
existing objects (e.g. a hand-made `spring-boot-example-dev`) in place; keep
`/tmp/ssc-demo-values.yaml` — the private override from §4 is needed for every
re-render, not just the first. To apply only a slice of the chart, render with
the full values and filter the output (e.g. just the `spring-boot-example-*`
Applications) before piping to `oc apply`.

Eleven Applications land at once; the sync waves serialise them.

```
 0  pipelines            OpenShift Pipelines operator
 0  odf                  ONLY if components.odf.enabled — see §0
 3  tekton-tasks, tekton-config
 4  tekton-pipelines
 5  keycloak, tekton-triggers
 8  gitlab               (imports the app repo from GitHub — slow)
10  quay, rhtas, rhtpa-prerequisites
15  rhtpa
```

Eleven, or twelve with ODF.

A wave does not start until the previous one is Healthy, so one Application
stuck in `Progressing` blocks everything behind it.

## 6. Watch it converge

```sh
watch "oc -n openshift-gitops get applications -l app.kubernetes.io/part-of=ssc-demo \
  -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'"
```

Expected behaviour while you wait:

* **The Tekton Applications fail their first sync or two.** Wave 3 is reached
  before the operator has finished installing its CRDs. The 30-attempt retry and
  `SkipDryRunOnMissingResource` exist for exactly this; leave them alone.
* **GitLab sits in `Progressing` for 10–20 minutes.** The seeding job is doing a
  server-side clone of a GitHub repository.
* **Quay takes ~15 minutes** and its `quay-noobaa` Job runs first, patching
  ODF's default BackingStore from 256Mi to 1Gi. Below ~400Mi the NooBaa agent
  pod is rejected and no bucket is ever provisioned.

If many Applications are stuck at once — each `Running` with a
`waiting for healthy state of ...` message that never resolves — suspect the
controller before the components: check the application-controller pod's
restart count and `lastState` (see the §0 note; OOMKilled there stalls every
in-flight sync at once).

If something is stuck, the Application's `.status.operationState.message` and
then the Job logs are where the answer is:

```sh
oc -n gitlab        logs job/initialize-gitlab
oc -n quay-registry logs job/quay-noobaa
oc -n quay-registry logs job/quay-config
```

## 7. Check the seeded credentials and configure Quay.io push access

The GitLab seeding job creates the two SCM tokens and webhook secret. Confirm
those before going further — later pipeline failures can otherwise look
unrelated.

```sh
oc -n $NS get secret scm-auth-secret scm-auth-gitops-secret gitlab-webhook-secret
```

`scm-auth-secret` holds a GitLab **project** access token scoped to the one
application repository, not the root PAT. `scm-auth-gitops-secret` holds a
second one scoped to the gitops repository — a GitLab project-bot user cannot
be shared across projects, so `gitops-cd-dev` needs its own token there.

This install publishes to `quay.io/machacekondra`. Because the optional
in-cluster Quay component is disabled, create `quay-push` with a Quay.io robot
token that can create and push repositories in the `machacekondra` organization,
then link it to the pipeline ServiceAccount:

```sh
read -r -s -p 'Quay.io robot token: ' QUAY_TOKEN
printf '\n'
oc -n "$NS" create secret docker-registry quay-push \
  --docker-server=quay.io \
  --docker-username='machacekondra+<robot-name>' \
  --docker-password="$QUAY_TOKEN" \
  --docker-email='unused@example.com' \
  --dry-run=client -o yaml | oc apply -f -
unset QUAY_TOKEN
oc -n "$NS" secrets link pipeline quay-push --for=pull
oc -n "$NS" secrets link pipeline quay-push --for=mount
oc -n "$NS" get secret quay-push
oc -n "$NS" get sa pipeline -o jsonpath='{.secrets[*].name}{"\n"}'
```

Keep the robot token out of this repository. The `quay-push` Secret's auth
entry must be for `quay.io`; the build can finish locally and then fail its
push with 401/403 if the registry host or robot permissions do not match.

## 8. Create the three remaining secrets by hand

### a. `tpa-secret` — RHTPA credentials for the Tekton tasks

The same `tpa-cli` client secret from §4, in the shape
`upload-sbom-to-rhtpa` requires. The four URL and OIDC keys are mandatory;
`oidc_ca_crt` supplies the cluster ingress CA for the self-signed Keycloak
route certificate.

```sh
oc -n openshift-ingress-operator get secret router-ca \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > /tmp/rhtpa-ingress-ca.crt

oc -n $NS create secret generic tpa-secret \
  --from-literal=bombastic_api_url="https://server-trusted-profile-analyzer.${DOMAIN}" \
  --from-literal=oidc_issuer_url="https://sso.${DOMAIN}/realms/backstage" \
  --from-literal=oidc_client_id=tpa-cli \
  --from-literal=oidc_client_secret='<tpaCliClientSecret from §4>' \
  --from-file=oidc_ca_crt=/tmp/rhtpa-ingress-ca.crt \
  --dry-run=client -o yaml | oc apply -f -
```

A wrong secret here is invisible until a build reaches the upload step and the
token request comes back 401.

### b. `ai-agent-secret` — the AI backend key

Match the key name to the `AI_PROVIDER` you chose in §3. Put **only** the key in
here — adding an `AI_PROVIDER` key to this Secret overrides the ConfigMap (see
§3), which is a genuinely confusing failure: the task uses the wrong SDK and
reports a connection error rather than anything about configuration.

```sh
# AI_PROVIDER=openai  (the default: gpt-oss on the MaaS endpoint)
oc -n $NS create secret generic ai-agent-secret \
  --from-literal=OPENAI_API_KEY='<key, or any placeholder for a server that ignores it>' \
  --dry-run=client -o yaml | oc apply -f -

# AI_PROVIDER=anthropic
oc -n $NS create secret generic ai-agent-secret \
  --from-literal=ANTHROPIC_API_KEY='<key>' \
  --dry-run=client -o yaml | oc apply -f -
```

Verify afterwards that the Secret has exactly one key:

```sh
oc -n $NS get secret ai-agent-secret -o jsonpath='{.data}' \
  | python3 -c 'import sys,json;print(sorted(json.load(sys.stdin)))'
```

### c. `maven-settings` — the Artifactory credential

Artifactory OSS has anonymous access off, so `settings.xml` carries a
credential and therefore has to be a Secret. Point the mirror at the in-cluster
Service, not the Route — that also sidesteps the router's 30 s timeout.

```sh
AF="http://artifactory.artifactory.svc.cluster.local:8082/artifactory"
ADMIN=$(oc -n artifactory get secret artifactory-admin \
        -o jsonpath='{.data.password}' | base64 -d | sed 's/^admin@\*=//')
python3 - "$AF" "$ADMIN" <<'PY' > /tmp/settings.xml
import sys; af, pw = sys.argv[1], sys.argv[2]
print(f'''<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <servers><server><id>artifactory</id><username>admin</username><password>{pw}</password></server></servers>
  <mirrors><mirror><id>artifactory</id><name>Artifactory maven virtual</name>
    <url>{af}/maven</url><mirrorOf>*</mirrorOf></mirror></mirrors>
</settings>''')
PY
oc -n $NS create secret generic maven-settings \
  --from-file=settings.xml=/tmp/settings.xml --dry-run=client -o yaml | oc apply -f -
rm -f /tmp/settings.xml
```

Create it **before** the first run. A secret-backed workspace whose Secret is
missing leaves the pod in `PodInitializing` forever — it never errors, it just
hangs.

## 9. Load the Lightwell remediation data into RHTPA

Without this the demo still runs, but there is no backport for the AI to
recommend — the two seeded CVEs have no fix version and the remediation step has
nothing to do.

```sh
pipelines/ops/rhtpa-load-lightwell-data.sh            # load, then verify
pipelines/ops/rhtpa-load-lightwell-data.sh --verify   # verify only
```

It loads the one-component CycloneDX catalogs (which make
`3.14.0.rhlw-00001` / `6.0.3.rhlw-00001` *known versions* of their base PURLs)
and the OSV records that declare them `fixed`. `--verify` exits non-zero on any
`[FAIL]`.

The script authenticates by reading `tpa-secret` out of `tssc-app-ci`, so §8a
has to be done first. RHTPA must be Healthy; its feed importers do **not** have
to have finished.

## 10. Verify

```sh
# Platform
oc -n openshift-gitops get applications -l app.kubernetes.io/part-of=ssc-demo
# Only when the optional in-cluster Quay component is enabled:
oc -n quay-registry get quayregistry quay
oc -n trusted-artifact-signer get securesign securesign
oc -n keycloak get secret keycloak-initial-admin \
  -o go-template='{{index .data "username"|base64decode}}{{"\n"}}'

# Tekton layer
oc -n $NS get tasks | wc -l                                  # 23 + header
oc -n $NS get pipelines                                      # 4 agentic + maven-build-ci
oc -n $NS get eventlistener agentic-issue-commands           # ADDRESS populated
oc get clusterinterceptors                                   # at least cel, gitlab

# Pipeline and task credentials — all must exist
oc -n $NS get secret scm-auth-secret scm-auth-gitops-secret gitlab-webhook-secret quay-push \
                     tpa-secret ai-agent-secret maven-settings
```

Then log into GitLab at `https://gitlab-gitlab.${DOMAIN}` as `root` and confirm
`development/spring-boot-example-application` is there with the CVE labels and a
project webhook pointing at `agentic-issue-commands-tssc-app-ci.${DOMAIN}`.

## 11. Smoke test

Build and scan, which is what produces the CVE issues the rest of the demo
reacts to. The `workspace` workspace is a per-run PVC, so give `tkn` a claim
template:

```sh
cat > /tmp/ws.yaml <<'EOF'
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 5Gi
EOF

tkn -n $NS pipeline start agentic-cve-analysis \
  -p component-name=spring-boot-example-application \
  -p git-url=https://gitlab-gitlab.${DOMAIN}/development/spring-boot-example-application.git \
  -p revision=main \
  -p output-image=quay.io/machacekondra/spring-boot-example-application:smoke \
  -p git-host=gitlab-gitlab.${DOMAIN} \
  -w name=workspace,volumeClaimTemplateFile=/tmp/ws.yaml \
  -w name=maven-settings,secret=maven-settings \
  -w name=git-auth,secret=scm-auth-secret \
  --showlog
```

Success looks like: the image appears at
`quay.io/machacekondra/spring-boot-example-application:smoke`, the
SBOM uploads to RHTPA, and GitLab issues are opened with the recommended
Lightwell backport.

Then exercise the webhook: comment **`/remediate`** on one of those issues. The
EventListener starts `agentic-cve-remediation`, which pushes a branch and opens
a merge request. **`/generate-tests`** does the same through
`agentic-test-generation`, **`/generate-tests-functional`** through
`agentic-test-generation-functional`, and **`/generate-tests-integration`**
through `agentic-test-generation-integration` — one webhook, four slash
commands; each trigger's cel filter picks its command.

## Known rough edges

* **`base-branch` defaults to `master`**, the TriggerTemplates default to
  `main`, and the imported repository uses `main`. Manual runs of
  `agentic-cve-remediation` / `agentic-test-generation` need
  `-p base-branch=main`; webhook-driven runs are already correct.
* **The RHTAS defaults in the pipelines are stale** — `rekor-url`,
  `tuf-mirror` and `oidc-issuer` name the `tssc-tas` namespace and a
  `trusted-artifact-signer` realm, neither of which this install creates (they
  are `trusted-artifact-signer` and `backstage`). They are inert while
  `verify-commit: 'false'`, which is the default. Fix them in the pipeline files
  before turning commit verification on.
* **`pipelineNamespace` is effectively fixed at `tssc-app-ci`.**
  `pipelines/triggers/agentic-issue-triggers.yaml` **and**
  `pipelines/tasks/deploy-app-under-test.yaml` (the ephemeral app-under-test
  RBAC) hardcode it — as a `ClusterRoleBinding` subject and a namespaced
  `Role`/`RoleBinding` respectively — and plain-YAML Applications cannot
  template it. `bootstrap-infra` fails the render with an explanation if you
  change one without the other.
* **Ephemeral test resources can outlive a hard PipelineRun timeout.** The
  functional test-generation pipeline deploys the app under test as ephemeral
  Deployment/Service/ImageStream in `tssc-app-ci`; `cleanup-app-under-test`
  removes them as a `finally` task (success or failure). But a PipelineRun
  **timeout** cancels finally tasks too, so on that path they linger until the
  PipelineRun object is deleted (ownerReference GC) — or sweep manually:
  `oc -n tssc-app-ci delete deployment,service,imagestream -l
  app.kubernetes.io/part-of=ephemeral-test-resources`.
* **Everything has `selfHeal: true`.** An `oc edit` against any managed resource
  is reverted on the next reconcile — change the chart, or for the Tekton layer,
  the file in `pipelines/`, and push.
* **`quay-admin-token` cannot be reissued.** Quay's `/api/v1/user/initialize`
  works once, while the user table is empty. Nothing renders that Secret, so
  Argo will not prune it — but if you delete it, the seeding job can no longer
  authenticate and you must mint a replacement token in the Quay UI by hand.

## Tearing it down

```sh
helm template ssc automation/gitops/bootstrap-infra \
  -f /tmp/ssc-demo-values.yaml | oc delete -f - --ignore-not-found
oc delete -f automation/gitops/artifactory/application-artifactory.yaml --ignore-not-found
oc delete ns gitlab keycloak quay-registry trusted-artifact-signer \
             trusted-profile-analyzer artifactory tssc-app-ci --ignore-not-found
```

Deleting the Applications leaves the operator Subscriptions in
`openshift-operators` and the ODF BackingStore patch in place; both are
harmless and both are reapplied by a reinstall.
