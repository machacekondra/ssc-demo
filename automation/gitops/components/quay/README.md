# quay

Red Hat Quay: an optional in-cluster registry for the demo's **application images**.

CI currently publishes application images to `quay.io/machacekondra`, as set
directly in `pipelines/triggers/ci-build-triggers.yaml`. To use this in-cluster
registry instead, enable the component, change that file's `output-image`
repository, and ensure `quay-push` is seeded with this registry's credentials
and linked to the `pipeline` ServiceAccount.

Not to be confused with `quay.io/sscdemo/...`, which is where the pipelines'
*agent* images (`ai-python`, `ai-agent-maven-aider`) are pulled from. Those stay
on public quay.io. When selected, this chart deploys an in-cluster registry
that receives the images the pipelines build:

```
quay.<domain>/tssc/<component>:<revision>
```

Vendored from `redhat-ads-tech/ocp-app-platform-demo-helm` v1.4.2 (`quay`),
which is what the reference cluster runs.

## Layout

Two subcharts, applied by sync wave *within* the Application:

| Wave | What |
|---|---|
| `-2` | the `quay-operator` Subscription, and the NooBaa pre-flight Job |
| `-1` | the config job's ServiceAccount and RoleBindings |
| `0` | `quay-config-bundle` and the `QuayRegistry` CR |
| `1` | the seeding Job |

The `QuayRegistry` carries `SkipDryRunOnMissingResource=true`, because on a
first install the `quayregistries.quay.redhat.com` CRD does not exist yet when
Argo renders wave 0 — the Subscription two waves earlier is still resolving.
The Application's 30-attempt retry covers the gap.

## What the seeding job does

`quay-config` runs an Ansible playbook once the registry is serving:

1. Waits for `QuayRegistry.status.currentVersion`, then for
   `/api/v1/discovery` to answer.
2. `POST /api/v1/user/initialize` to create the `quayadmin` superuser and mint
   an OAuth token, stored as the `quay-admin-token` Secret.
3. Pushes that token's expiry out to the year 2300 via `psql` against the Quay
   Postgres pod. Without this the token lapses and every later re-sync fails
   with an unexplained 401.
4. Creates the `tssc` organization.
5. Creates the robot account `tssc+remediation`, puts it in a `pipeline` team
   with the `creator` role, and registers a default permission prototype
   granting it `write`.
6. Writes `quay-push` — a `kubernetes.io/dockerconfigjson` Secret holding the
   robot's token — into the **pipeline namespace**.
7. Adds `quay-push` to the `pipeline` ServiceAccount's `secrets` and
   `imagePullSecrets`.

Steps 5–7 are the point of this chart. They are why a PipelineRun can set
`output-image=quay.<domain>/tssc/<app>:<sha>` and have `buildah-rhtap` push it
without naming a credential anywhere.

## Why the robot needs both a team and a prototype

The `creator` team role lets the robot make new repositories in the
organization, and it becomes admin of the ones it creates — which covers the
first push of a new component. It does **not** cover a repository someone else
created, say in the UI: there the robot has read only, and the push fails with
a bare 403 that looks like a bad credential.

The default permission prototype closes that gap by granting the robot `write`
on every repository created in the organization from then on. Neither mechanism
alone is sufficient; `grantWritePrototype: false` turns the second one off if
you want to see the failure.

## Idempotency

Both Jobs are `Replace=true`, because a Job's pod template is immutable — any
edit to a playbook would otherwise fail the sync with "field is immutable" and
leave the old seeding running. The flip side is that **the jobs re-run on every
chart change**, so every API call has to tolerate its own previous success:

| Call | Re-run answer | Treated as |
|---|---|---|
| `POST /user/initialize` | 400 | already initialized — fall back to the stored token |
| `POST /organization/` | 400 | already exists |
| `PUT /organization/{org}/robots/{name}` | 400 | already exists — `GET` it for the token |
| `POST /organization/{org}/prototypes` | 400 | already granted |
| `POST /superuser/users/` | 400 | already exists |

Upstream pinned exact status codes on all of these, so its job failed on every
run after the first. That is fixed here.

The `/user/initialize` case is the sharp one. It is Quay's bootstrap hatch, not
an API — it works exactly once, while the user table is empty, and there is no
way to ask it again. If `quay-admin-token` is ever lost on a registry that is
already initialized, the job cannot re-authenticate; it fails with a message
telling you to mint a token in the UI and recreate the Secret by hand. Nothing
in this chart renders that Secret, so Argo does not track it and `selfHeal`
will not delete it.

## Storage: this needs ODF

`component.objectstorage.managed: true` makes the Quay operator raise an
`ObjectBucketClaim` against NooBaa in `openshift-storage`. That is the same
dependency RHTPA's default `s3` storage has, so on a cluster built for this
demo it is already satisfied.

The `quay-noobaa` pre-flight Job exists because ODF creates its default
BackingStore asking for 256Mi while the agent pod needs at least 400Mi — left
alone it sits in `Rejected` and the registry never gets a bucket. The job waits
for NooBaa, patches the BackingStore's resources, then waits for it to go
Ready.

Its `backoffLimit` is 6, not upstream's 100. Each attempt spends up to 20
minutes retrying, so on a cluster with no ODF at all a high limit turns a clear
failure into an Application that sits Progressing for the rest of the day and
blocks sync wave 15 behind it.

Without ODF, set `components.quay.enabled: false` in `bootstrap-infra` and give
the pipelines an `output-image` on some other registry, plus a push secret you
create yourself.

## Routes and TLS

Nothing to do. The operator creates an edge-terminated Route using the
cluster's default ingress certificate and **already annotates it with
`haproxy.router.openshift.io/timeout: 30m`** — so unlike GitLab and RHTPA, this
component needs no timeout work. On a cluster whose ingress certificate is
publicly trusted (as the workshop clusters' are), `buildah-rhtap` pushes with
its default `TLSVERIFY: 'true'` without further configuration.

## Removed from upstream

| Removed | Why |
|---|---|
| the Vault tasks in the config playbook | ssc-demo has no Vault; upstream wrote the Quay credentials into a `vault-0` pod |
| `rolebinding-config-vault.yaml` | bound `edit` into the Vault namespace for those tasks |
| the `vault` values block | nothing reads it |
| the `parasol` organization and the `dev1`/`dev2` users | they belong to the upstream RHDH workshop and hold none of this demo's images |
| `quay-registry.serviceAccountName` in `_helpers.tpl` | referenced `.Values.serviceAccount.create`, which does not exist in `values.yaml` — a nil-pointer render failure waiting for its first caller |

## Changed from upstream

* Catalog source `redhat-operators-snapshot` → `redhat-operators`. The
  snapshot catalog only exists inside the Red Hat workshop environment.
* The cross-namespace RBAC is a two-verb `Role` on Secrets and
  ServiceAccounts, not `edit` — the same narrowing the GitLab chart applies for
  the same kind of write.
* `SERVER_HOSTNAME` derives from `global.cluster.subdomain` rather than an
  IngressController `lookup`, which returns empty under Argo CD and silently
  produces a registry that issues unusable URLs.
* Every seeding call is idempotent (see above).

## Values

| Value | Notes |
|---|---|
| `global.cluster.subdomain` | required; the registry is published at `quay.<subdomain>` |
| `quay-registry.quay.adminUserPassword` | the `quayadmin` password — set from `bootstrap-infra`'s `credentials.quayAdminPassword`, never committed |
| `quay-registry.quay.organizations` | organizations to create; `tssc` is the one the demo pushes to |
| `quay-registry.quay.users` | Quay users; empty, since the demo authenticates as the robot |
| `quay-registry.quay.pipeline.namespace` | where `quay-push` is written; set from `pipelineNamespace` |
| `quay-registry.quay.pipeline.robotName` | short name; the account becomes `<organization>+<robotName>` |
| `quay-registry.quay.createPrivateRepoOnPush` | `false`, so pushed repositories are public and pull needs no secret |
| `quay-registry.noobaa.enabled` | the BackingStore pre-flight; turn off only with unmanaged object storage |

## Verifying

```sh
oc -n quay-registry get quayregistry quay
oc -n quay-registry get job quay-noobaa quay-config
oc -n tssc-app-ci get secret quay-push
oc -n tssc-app-ci get sa pipeline -o jsonpath='{.secrets[*].name}{"\n"}'
```

The last two are the ones that matter — if `quay-push` is missing or not on the
ServiceAccount, the build pushes will fail with a 401 that looks like a Quay
problem and is not.
