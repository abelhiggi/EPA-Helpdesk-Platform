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
