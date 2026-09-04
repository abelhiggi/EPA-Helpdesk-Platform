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

## Architecture

High-level view:

![High-level architecture](docs/High-level-EPA-diagram.png)

<details>
<summary>Full detail (every resource, all data flows)</summary>

![Detailed AWS architecture](docs/AWS-HELPDESK-EPA-DIAGRAM.png)

</details>

Officer → CloudFront/S3 → Cognito → API Gateway → ingest → DynamoDB → SQS →
process → Bedrock / SES / metrics, with a DLQ and automated redrive on
repeated failure.

Full diagram and design rationale: [`docs/architecture.md`](docs/architecture.md)

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
make all          # lint, 71 tests, synth both environments — all offline
```

Deploy: `make deploy-dev`, then `make deploy-prod`. First-time account setup is
in [`docs/runbook.md`](docs/runbook.md).

## Shipping a change: VS Code → dev → prod

There is no `dev` or `prod` git branch. One trunk (`main`), two deploy
*environments*, both driven from the same push — the pipeline, not the
branch name, decides where code lands.

1. Edit in VS Code. Run `make all` locally before pushing — it's the same
   lint, test and synth CI runs, entirely offline.
2. Commit, push a branch, open a PR against `main`.
3. `.github/workflows/ci.yml` runs on the PR:
   - **`verify`** — `ruff check .` and `ruff format --check .`, `make test`
     (pytest with an 80% coverage gate), `cdk synth` for both dev and prod,
     `checkov` against the synthesised CloudFormation, and `pip-audit`
     against dependencies.
   - **`codeql`** — static analysis, runs in parallel with `verify`.
   - Both are required status checks (set in GitHub Settings → Branches;
     there's no branch-protection-as-code file) — the PR can't merge until
     they're green.
4. Merge to `main` triggers `.github/workflows/deploy.yml`.
5. **`dev` job** runs first, no approval needed: assumes `DEV_DEPLOY_ROLE_ARN`
   over OIDC, runs `make deploy-dev`, publishes the frontend
   (`scripts/publish-frontend.sh Helpdesk-dev`), then smoke-tests it
   (`scripts/smoke-test.sh Helpdesk-dev`).
6. **`prod` job** runs only if `dev` succeeds, and only after a human
   approves it in GitHub's `production` environment (the required-reviewer
   gate configured per `docs/runbook.md`). It then assumes
   `PROD_DEPLOY_ROLE_ARN` over OIDC, runs `make deploy-prod`, publishes the
   frontend, and smoke-tests it. If the smoke test fails, the job checks out
   the previous commit and redeploys automatically — rollback is a
   redeploy, not a separate release-manifest process.

No static AWS credentials exist anywhere in this pipeline — both jobs assume
a role over OIDC, and the two roles are scoped so a dev deploy credential
cannot touch prod resources.

Two more things ride the same pipeline: `deploy.yml` also runs on a weekly
schedule (Mondays 03:00) so both environments redeploy — and pick up patched
dependencies — even with no code change; and Dependabot PRs go through the
identical `verify`/`codeql` gate, then auto-merge via the `auto-merge` job
inside `ci.yml` (not a separate workflow) once both pass.

## Where the evidence lives

| Criterion | Evidence |
|---|---|
| Code quality | `src/`, `infra/`, 71 tests at 98% coverage, `docs/troubleshooting.md` |
| **Should-have user needs** | `docs/user-needs.md` — S1–S4 all delivered; `GET /tickets/{id}`, priority, dashboard, WCAG AA |
| CI-CD pipeline | `.github/workflows/ci.yml`, `deploy.yml` |
| **Fully automated patching** | `.github/dependabot.yml` + auto-merge job + weekly scheduled deploy |
| **Custom metrics** | `docs/metrics-improvement.md` — business metrics and the change they drove |
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

**Seven alarms, every one has an action.** Six route to the main alarm SNS
topic (canary availability, DLQ depth, process errors, ingest errors, API
server errors, `/health` errors); the DLQ depth condition also fires a
separate `DlqRemediationAlarm`, whose action is the `redrive` Lambda via its
own SNS topic — so a stuck DLQ triggers automated recovery, not just a page.

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
