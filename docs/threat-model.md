# Threat model

STRIDE across the four trust boundaries: browser to CloudFront, browser to API
Gateway, Lambda to AWS services, and pipeline to AWS account. Ranked by
likelihood against impact, because treating every threat as equally urgent is
the same as having no ranking at all.

| # | Threat | STRIDE | Likelihood | Impact | Mitigation | Residual |
|---|---|---|---|---|---|---|
| 1 | Ticket raised in another person's name | Spoofing | Med | Med | Requester read from verified token claims; body-supplied email ignored | Low |
| 2 | One user reads another's ticket | Info disclosure | Med | High | Ownership check on `GET`; 404 identical for foreign and missing tickets | Low |
| 3 | Reference enumeration to harvest tickets | Info disclosure | Med | Med | UUIDv4 references; identical 404; 25 req/s throttle | Low |
| 4 | Prompt injection in a description steering categorisation | Tampering | High | Low | Category and priority validated against a closed taxonomy before any write; invalid output falls back to a keyword rule | Low |
| 5 | Prompt injection causing data exfiltration via the model | Info disclosure | Low | High | Model has no tools and no data access; it receives one string and returns one string | Low |
| 6 | Stolen long-lived AWS credentials in CI | Elevation | Med | High | GitHub OIDC federation, no static keys anywhere; trust policy scoped to repo and branch | Low |
| 7 | Compromised third-party action exfiltrating secrets | Elevation | Low | High | Minimal `permissions:` per job; `id-token: write` only on deploy jobs | Med — actions pinned by major tag, not SHA. Accepted: Dependabot updates them weekly, and SHA pinning would defeat that automation. Reconsider if the threat model ever includes a targeted supply-chain attacker |
| 8 | Ticket data read from storage by an unauthorised principal | Info disclosure | Low | High | Customer-managed KMS key; grants scoped per function | Low |
| 9 | Ticket data intercepted in transit | Info disclosure | Low | High | TLS throughout; HTTP redirected at CloudFront; HSTS one year | Low |
| 10 | Flooding the API to drive Bedrock cost | DoS | Med | Med | Cognito required before any Bedrock call; API throttle; SQS max concurrency 5 | Med — no per-user quota. Accepted for an authenticated internal service |
| 11 | Silent loss of a request | Repudiation | Med | High | DynamoDB write before enqueue; DLQ; automated redrive; alarm | Low |
| 12 | Denial of a submission having been made | Repudiation | Low | Low | Correlation ID in structured logs, 90-day retention | Low |
| 13 | Unauthenticated `GET /health` probed or abused | Info disclosure / DoS | Med | Low | Short-circuits before the Cognito claims check and before any table, queue or Bedrock call; returns a fixed `{"status": "ok"}` with no ticket or account data; bounded by the same 25 req/s stage throttle as every other route | Low |

## Encryption at rest: the decision and why

Ticket descriptions are encrypted at rest with a **customer-managed** KMS key
rather than AWS-managed default encryption. Both encrypt; the difference is who
controls the key and what the audit trail looks like.

The reason is the content, not the compliance checkbox. A description of what
broke frequently carries information the requester did not think of as
sensitive — a service user's name in the context of a failing case-management
screen, a building and a room, sometimes a password typed into the wrong field.
Under UK GDPR that is personal data processed by a public authority, so a key
whose use is logged in CloudTrail and whose access can be revoked independently
of the table is proportionate.

**Where I chose not to encrypt with a CMK:** the static site bucket uses
SSE-S3. Its contents are three public files served to anonymous browsers via
CloudFront. A customer-managed key there would add cost and a rotation
obligation to protect data that is, by design, world-readable.

**One key, not three.** The previous iteration of this platform used separate
keys for application data, logs and artefacts. At this scale that is
blast-radius theatre: the same pipeline role can use all three, so compromising
one compromises all of them. One key with rotation and per-function grants
gives the same practical isolation with a third of the surface to reason about.

## Suppressed scanner checks

`checkov` runs in CI against the synthesised CloudFormation, with
`.checkov.yaml` at the repo root as its config. Every entry there was verified
to actually fail first — nothing here is a speculative or default suppression
— and every one is a deliberate design decision, not an unaddressed gap. Full
reasoning is in `.checkov.yaml` itself, right next to each suppression;
summarised here so it's readable without opening a scanner config.

| Check | What it wants | Why it's suppressed here |
|---|---|---|
| CKV_AWS_117 | Lambda functions inside a VPC | No dependency (DynamoDB, SQS, Bedrock, SES, CloudWatch) is inside a VPC. A VPC here adds a NAT gateway and cold-start ENI attachment latency for no dependency that needs it |
| CKV_AWS_116 | Lambda-level `DeadLetterConfig` | That mechanism is for async invokes and doesn't apply to an SQS event source. The real DLQ is at the SQS layer: `maxReceiveCount` 3 plus `ReportBatchItemFailures` |
| CKV_AWS_59 | Every API method behind an authorizer | `GET /health` is a deliberate exception — an unauthenticated availability probe that short-circuits before the Cognito claims check, makes no downstream calls, and returns no ticket or account data. See row 13 above |
| CKV_AWS_120 | API Gateway response caching | Every response is per-user authenticated ticket data (`GET /tickets/{id}`); a cache-key mistake risks serving one officer's ticket to another |
| CKV_AWS_173 | A second, function-specific KMS layer on Lambda environment variables | The env vars are a table name, a queue URL, a Bedrock model ID and a boolean toggle — no secrets, nothing a CMK on top of Lambda's own encryption protects |
| CKV_AWS_18 | S3 access logging, on the site bucket and the canary artifacts bucket | Would need a second bucket purely to receive logs nobody has a use case for yet. CloudTrail data events are the right control if that need ever appears |
| CKV_AWS_115 | Lambda reserved concurrency, on Ingest and Process (fixed properly on Redrive — see below) | Fixed on Redrive, where it's a correctness bound against racing DLQ redrives. On Ingest (the customer-facing submission path) and Process, it would just be an arbitrary throughput ceiling with real 429-throttling risk under real load, not a correctness requirement |
| CKV_AWS_174 | A minimum TLS version on the CloudFront viewer certificate | This distribution has no custom domain, so it uses the default `*.cloudfront.net` certificate — AWS does not allow a minimum protocol version to be set on that certificate at all. Only configurable with a custom domain and ACM certificate, which is a real architecture addition, not a config change |
| CKV_AWS_68 | A WAF WebACL on the CloudFront distribution | One write-capable route exists behind it, `POST /tickets`; `GET /health` is the only unauthenticated one. Both are already bounded by the 25 req/s stage throttle, with Cognito required on every route except the deliberately-open health check. A WAF is real ongoing cost for a threat this architecture doesn't have an unmitigated case of |
| CKV_AWS_86 | CloudFront access logging | Same reasoning as CKV_AWS_18: a logging destination bucket with no current use case for its contents |

### pip-audit (dependency vulnerabilities)

**The Lambda functions have a zero-dependency runtime.** `src/` is four files
— `common.py`, `ingest.py`, `process.py`, `redrive.py` — with no
`requirements.txt` and nothing vendored into it. `lambda_.Code.from_asset("src")`
zips exactly that directory with no build/bundling step, so the deployed
asset genuinely is those four files, nothing more. Every import across all
four is either the standard library or `boto3`, which the Lambda Python 3.12
runtime provides — `grep -n "^import\|^from" src/*.py` shows no third-party
import anywhere. That means a vulnerability in a *dev-tooling* dependency
(checkov, pip-audit itself, anything only imported by them) cannot reach
deployed code by construction, not by policy — there is no path from a
`requirements-dev.txt` package to a running Lambda.

That's the basis for the three `pip-audit` findings ignored in
`.github/workflows/ci.yml`'s "Dependency vulnerabilities" step, all
transitive dependencies of checkov:

| Finding | Package | Why it's ignored |
|---|---|---|
| GHSA-9w56-46f6-3qhx / CVE-2026-55244 (one vulnerability, two IDs) | asteval 1.0.6 | Fixed at 1.0.9, but checkov hard-pins asteval to an *exact* version at every release, including latest (3.3.16: `==1.0.6`). `requirements-dev.txt` raises the floor to 1.0.6 — everything checkov itself has adopted — pending checkov relaxing its own pin upstream. Dev-tooling only; see above |
| PYSEC-2026-1325 (aka CVE-2024-23342, GHSA-wj6h-64fc-37mp — the Minerva timing attack) | ecdsa 0.19.2 | A direct dependency of checkov 3.3.16, pulled in when raising the checkov floor to resolve the asteval/urllib3 pins above. Not pending anything: the ecdsa project has stated side-channel attacks are out of scope and there is no planned fix, so this is accepted permanently, not a version to wait for. Dev-tooling only; see above |

**Fixed, not suppressed, in the same pass:** API Gateway access logging
(CKV_AWS_76 — a dedicated, KMS-encrypted log group with a structured JSON
format covering `requestId`, `sourceIp`, `requestTime`, `httpMethod`,
`resourcePath`, `status` and `responseLatency`, deliberately excluding the
`Authorization` header and any request/response body); canary artifacts
bucket versioning (CKV_AWS_21); Redrive's reserved concurrency of 1
(CKV_AWS_115, as a correctness bound — see above); and CloudWatch Logs KMS
encryption on all four log groups this stack creates (CKV_AWS_158), which
needed an explicit key-policy statement granting `logs.<region>.amazonaws.com`
access scoped to exactly those log group ARNs — without it, log group
creation fails at deploy time with `InvalidParameterException`, a failure
`cdk synth` cannot catch. Getting that policy statement right also meant
naming the log groups explicitly rather than letting CloudFormation
auto-generate their names: an auto-generated name only exists as a `Ref` to
the log group resource, and putting that `Ref` in the key's policy while the
log group also references the key for encryption is a literal CloudFormation
dependency cycle.

### Dependabot ignore: asteval

**What's suppressed:** All Dependabot updates for `asteval`
(`.github/dependabot.yml`, pip block).

**Why:** `checkov` hard-pins `asteval` to an exact version at every
release checked, including latest (3.3.16: `asteval==1.0.6`). Any bump
Dependabot raises produces an unsatisfiable dependency set and fails at
install, so the PR can never merge. Verified against PyPI metadata, not
just checkov's changelog.

**Residual risk:** GHSA-9w56-46f6-3qhx / CVE-2026-55244 (one
vulnerability, two IDs), fixed upstream at asteval 1.0.9. Accepted:
asteval reaches this repo only as a transitive dependency of checkov, a
dev-time and CI-time tool. The Lambda functions bundle no third-party
packages, so no deployed code path reaches it. The same IDs are
suppressed explicitly in `ci.yml`'s `pip-audit` step rather than
silently dropped.

**Detection retained:** GitHub security alerts for asteval still surface
in the Security tab; only PR creation is suppressed.

**Review trigger:** Remove this ignore, and the matching `pip-audit`
`--ignore-vuln` flags, when checkov relaxes its asteval pin to a floor, or
when checkov moves into its own isolated environment so its pins stop
constraining our test dependencies.

### Dependabot ignore: boto3 and botocore

**What's suppressed:** All Dependabot updates for `boto3` and `botocore`
(`.github/dependabot.yml`, pip block).

**Why:** `checkov` 3.3.16 hard-pins `boto3` to an exact version
(`boto3==1.35.49`), with `botocore` following that pin transitively. Any
bump Dependabot raises for either package produces an unsatisfiable
dependency set and fails at `pip install` with `ResolutionImpossible`, so
the PR can never merge. `requirements-dev.txt`'s `boto3>=1.35` floor is
deliberately set to a version checkov's own pin satisfies. Unlike the
asteval ignore above, this is a version-bump suppression only — there is
no advisory behind it.

**Residual risk:** None specific to this suppression. boto3/botocore
security advisories still reach this repo through the normal `pip-audit`
step in CI; only the Dependabot PR for a routine version bump is
suppressed, and only because it would fail to install regardless.

**Detection retained:** GitHub security alerts for boto3 and botocore
still surface in the Security tab; only PR creation is suppressed.

**Review trigger:** Remove this ignore when checkov relaxes its boto3 pin
to a floor, or when checkov moves into its own isolated environment so
its pins stop constraining our test dependencies.
