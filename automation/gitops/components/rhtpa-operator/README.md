# RHTPA via the operator (3.x)

Installs Red Hat Trusted Profile Analyzer with the **RHTPA operator**
(`TrustedProfileAnalyzer`, `rhtpa.io/v1`) instead of the `charts.openshift.io`
Helm chart that `components/rhtpa` uses.

This is the **only** way the automation installs RHTPA. The
`charts.openshift.io` Helm path that used to live at `components.rhtpa` has
been removed.

## Why the Helm path went away

Exploit Intelligence integrates through `spec.exploitIntelligence` on this CR
and has no Helm-chart equivalent, so a chart-installed RHTPA could never grow
the feature. Keeping both meant maintaining a path that was a dead end.

## What it reuses

Nothing here provisions infrastructure. `components.rhtpaPrerequisites` stays
enabled and owns the database and OIDC client secret. In S3 mode it also owns
the ObjectBucketClaim and its credentials:

| Resource | Provides |
|---|---|
| `tpa-postgresql` (Deployment + Secret) | the database |
| `trustify` (Secret from the ObjectBucketClaim) | S3 credentials (S3 mode only) |
| `oidc-tpa-cli` (Secret) | the `tpa-cli` client secret |

Filesystem mode does not create `trustify`; it stores documents on a PVC. Set
`trustedProfileAnalyzer.storage.size` for that PVC (the demo defaults to
`50Gi`). That is what makes switching a swap rather than a rebuild.

## Things worth knowing before you edit it

**The CR is unvalidated.** The CRD declares `spec` as a bare object with
`x-kubernetes-preserve-unknown-fields: true` and no property schema — this is
a Helm-based operator and `spec` passes straight through to the bundled
chart's values. The API server will therefore accept **any** shape, including
typos. A misspelt key does not get rejected; it silently has no effect. After
changing a field, check the rendered Deployment rather than trusting that the
resource applied cleanly.

**`appDomain` is a suffix, not a hostname.** The server is published at
`server<appDomain>`, so it must begin with a separator —
`-trusted-profile-analyzer.<domain>`.

**The Route timeout annotation goes on the Ingress.** RHTPA's Route is
generated from an Ingress by the ingress-to-route controller and carries an
`ownerReference`, so annotating the Route directly is reverted. It is set via
`ingress.additionalAnnotations` here.

**OperatorGroup.** The CSV supports only `OwnNamespace`/`SingleNamespace`, so
it cannot live in `openshift-operators` the way the Quay and Pipelines
operators do. This chart creates one — but set `createOperatorGroup: false` if
the namespace already has one. Two OperatorGroups in a namespace put every
operator in it into an error state, and the symptom never mentions
OperatorGroups.

## Verifying

```sh
oc -n trusted-profile-analyzer get trustedprofileanalyzer
oc -n trusted-profile-analyzer get pods
curl -sk https://server-trusted-profile-analyzer.<domain>/.well-known/trustify
```

The version in `/.well-known/trustify` should report 3.x. Then re-check an
authenticated call with the pipeline's own credentials, because the OIDC
wiring is the part most likely to differ:

```sh
oc -n tssc-app-ci get secret tpa-secret -o jsonpath='{.data.oidc_issuer_url}' | base64 -d
```
