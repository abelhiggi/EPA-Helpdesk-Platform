# IT Helpdesk Platform

AI-triaged IT helpdesk for Salford City Council. A council officer submits a
support request; Bedrock categorises it and assigns a team; the team is notified;
the officer can check where it went. Serverless, on AWS, in `eu-west-2`.

Built as AWS CDK (Python) — one stack definition, 67 resources in prod and 71
in dev (confirmed by `cdk synth`, not estimated), deployed as two stacks side
by side in a single AWS account by GitHub Actions with OIDC. No static
credentials, no console steps after bootstrap.

**Single account is a sandbox constraint, not the target pattern.** This
account (512738512057) holds both `Helpdesk-dev` and `Helpdesk-prod`,
separated by fully distinct physical resource names (see `infra/helpdesk_stack.py`
— every KMS alias, S3 bucket, Cognito domain, dashboard and canary name
includes the environment). That stops the two stacks colliding, but it does
not stop one environment's blast radius (an over-permissive IAM change, a
runaway Bedrock loop, a service-quota exhaustion) from reaching the other —
they still share one account's IAM boundary and one account's quotas.
Production isolation is a separate AWS account per environment — a hard IAM
and service-quota boundary that stack-level name separation cannot give you,
because it is enforced by AWS itself rather than by naming discipline. That
is the pattern to move to outside this sandbox; see `docs/runbook.md`.

```
Officer ─▶ CloudFront/S3 ─▶ Cognito ─▶ API Gateway ─▶ ingest ─▶ DynamoDB
                                                          │
                                                        SQS ─▶ process ─▶ Bedrock
                                                          │            ─▶ SES
                                                          │            ─▶ metrics
                                                         DLQ ─▶ redrive ─┘
```

Full diagram and design rationale: [`docs/architecture.md`](docs/architecture.md)

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
make all          # lint, 56 tests, synth both environments — all offline
```

Deploy: `make deploy-dev`, then `make deploy-prod`. First-time account setup is
in [`docs/runbook.md`](docs/runbook.md).

## Where the evidence lives

| Criterion | Evidence |
|---|---|
| Code quality | `src/`, `infra/`, 56 tests at 98% coverage, `docs/troubleshooting.md` |
| **Should-have user needs** | `docs/user-needs.md` — S1–S4 all delivered; `GET /tickets/{id}`, priority, dashboard, WCAG AA |
| CI-CD pipeline | `.github/workflows/ci.yml`, `deploy.yml` |
| **Fully automated patching** | `.github/dependabot.yml` + auto-merge job + weekly scheduled deploy |
| **Custom metrics** | `docs/metrics-improvement.md` — three business metrics and the change they drove |
| Data persistence | DynamoDB with one GSI; `docs/architecture.md` for why not RDS |
| **Additional automation** | `src/redrive.py` — removed ~15 min of manual DLQ triage per incident |
| Data security | `docs/threat-model.md` — STRIDE, and the encryption-at-rest decision |

Bold rows are the four distinction criteria for Method 1.

## Things worth knowing

**`AI_CATEGORISATION_ENABLED` is a real toggle.** Switch it off and the flow
still works end to end via a keyword rule. That is branching by abstraction: the
Bedrock integration merged to `main` while it was still being tuned, with no
long-lived branch.

**Rollback is redeploying the previous commit.** CloudFormation restores prior
state; there is no bespoke release-manifest machinery, because a rollback path
you never exercise is not a rollback path.

**Every alarm has an action.** All five route to SNS, and the DLQ alarm also
triggers the redrive function.

**One KMS key, not three.** Reasoning in `docs/threat-model.md`.

## Layout

```
app.py                    CDK entry point: one stack, two environments
infra/helpdesk_stack.py   the whole platform, 67 resources in prod (71 in dev)
src/                      one Lambda asset — three handlers plus shared code
  common.py               logging and EMF metrics
  ingest.py               POST /tickets, GET /tickets/{id}
  process.py              categorise, route, notify, emit metrics
  redrive.py              automated DLQ recovery
frontend/                 static UI, PKCE auth, no hardcoded identifiers
scripts/                  publish frontend, smoke gates
tests/                    handler tests + CDK template assertions
docs/                     architecture, runbook, threat model, user needs
epa/                      assessment artefacts and evidence pack
```

All three functions share `src/` as a single asset, so `common.py` is importable
without being copied or layered. The cost is a few unused KB per bundle and a
change to one handler versioning all three — worth it to remove a build step
that could be forgotten.

## Before submission

- [ ] Run the metrics experiment and fill in `docs/metrics-improvement.md`
- [ ] Record at least one real incident in `docs/troubleshooting.md`
- [ ] Confirm the CDK-vs-CloudFormation change with BCS in writing (the signed-off
      mapping says CDK TypeScript; this is CDK Python — same tool, single
      toolchain, one test harness)
- [ ] Verify a Dependabot PR has auto-merged and reached prod; screenshot the chain
- [ ] Run axe DevTools over the UI and keep the report
