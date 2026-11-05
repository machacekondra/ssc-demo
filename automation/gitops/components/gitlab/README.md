# gitlab

A self-contained GitLab CE (one pod, plus Postgres and Redis) and the Ansible
Job that seeds it with everything the demo expects to find.

Adapted from `redhat-ads-tech/ocp-app-platform-demo-helm` v1.4.2 (`gitlab`),
which is what the reference cluster runs.

## What the seeding Job does

`templates/cm-gitlab-init.yaml` renders an Ansible playbook; `job-gitlab-init`
runs it once GitLab is up. In order:

1. Waits for the pod, then for `/api/v4/projects` to answer 200.
2. Ensures the default project limit and root account are seeded, then mints a
   root personal access token with `./bin/rails runner` inside the GitLab pod.
   It stores the token in `root-user-personal-token`; there is no API for
   creating the first token because GitLab has no existing credential to
   authenticate that request with. Retries reuse the stored token instead of
   creating another root token.
3. Turns on `import_sources` (needed for step 5) and
   `allow_local_requests_from_web_hooks_and_services` (the webhook target is a
   Route on this same cluster, which GitLab otherwise refuses to call).
4. Creates the `development` group.
5. Imports `spring-boot-example-application` from GitHub, then waits for
   `import_status: finished` — `POST /projects` returns 201 as soon as the
   project row exists, long before the clone lands.
6. Creates the CVE severity labels, so the issues
   `open-cve-issues` files land on a readable board instead of auto-creating
   grey labels.
7. Creates the `agentic-issue-commands` webhook — the one that turns a
   `/remediate` issue comment into a PipelineRun — subscribed to `note_events`
   and nothing else. `push_events` defaults to true on a new hook and has to
   be switched off explicitly, or every push also fires the remediation
   trigger.
8. Mints a **project** access token and writes it into the pipeline namespace
   as `scm-auth-secret`.

Steps 5–8 are additions. Upstream imports repositories but has no concept of
labels on them, no webhooks, and no credential hand-off; on the reference
cluster all of that was done by hand.

## Credential handling

No token in this chart is committed, and none is templated into git.

* **`gitlab-webhook-secret`** (pipeline namespace, key `secretToken`) is the
  shared secret the Tekton GitLab interceptor validates `X-Gitlab-Token`
  against. The Job reuses the Secret if it already exists, and otherwise
  generates one — so the webhook and the interceptor always agree, and
  re-running the Job does not invalidate a webhook already in use.
* **`scm-auth-secret`** (pipeline namespace, keys `username`/`token`) is a
  GitLab project access token scoped to the one application repository, at
  Maintainer level so `open-pr` can push a branch. A project token's value is
  returned only once, at creation, so the Job skips the whole block if the
  Secret already holds a real token; if it does not, it revokes any stale
  token of the same name (unrecoverable, and they would otherwise pile up) and
  issues a fresh one.

Writing into another namespace needs rights there:
`templates/rbac-pipeline-ns.yaml` grants the `gitlab` ServiceAccount access to
Secrets in `pipeline.namespace`, and nothing else.

## Removed from upstream

| Removed | Why |
|---|---|
| Vault writes (root PAT, DevSpaces OAuth credentials) | ssc-demo has no Vault; the only consumer of a GitLab credential is the pipeline, which reads a Secret. |
| `rolebinding-vault.yaml` | Bound `edit` into the Vault namespace for the above. Replaced by the narrow Secrets-only Role in the pipeline namespace. |
| DevSpaces OAuth application + doorkeeper token-expiry patch | No DevSpaces. |
| `job-gitlab-template.yaml`, `cm-gitlab-templates.yaml` | Imported RHDH software templates. No Developer Hub here. |
| `gitlab.templates`, `quay.*`, `orchestrator.*` values | Consumed only by the above. |

## Changed

* **Route timeout 300 s.** The import in step 5 is a server-side clone held
  open on the API connection, and the pipeline's `git push` of a remediation
  branch goes through the same Route. Both run past HAProxy's 30 s default on
  a cold cluster, and surface as an opaque "failed to respond". This Route is
  created directly rather than generated from an Ingress, so the annotation
  sits on it and is not reconciled away.
* **`Replace=true` on the seeding Job.** A Job's pod template is immutable, so
  without it the first edit to the playbook fails the sync with "field is
  immutable" while the cluster quietly keeps the old seeding.
* **Hostnames derive from `cluster.subdomain`** rather than being passed in
  per-cluster.
* **Namespace references are templated.** Upstream hardcoded `gitlab` in the
  Postgres/Redis service names and in one `oc -n gitlab` call.

## Values you must set

| Value | Notes |
|---|---|
| `cluster.subdomain` | apps subdomain, no leading dot |
| `gitlab.rootPassword` | not committed; supply at install time |
| `pipeline.namespace` | where `scm-auth-secret` / `gitlab-webhook-secret` are written, and what the webhook URL is derived from |

Everything else — the group, the repository, the labels, the webhook, the
token — is in `values.yaml` under `gitlab.groups`.
