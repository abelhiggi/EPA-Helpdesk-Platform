# IT Helpdesk Platform

AI triaged IT helpdesk for Salford City Council. An officer submits a support
request, Bedrock categorises it and assigns a team, the team gets an email,
and the officer can check where it went. Serverless on AWS, `eu-west-2`.

Built with AWS CDK in Python. One stack definition, deployed twice
(`Helpdesk-dev` and `Helpdesk-prod`) in a single account by GitHub Actions
over OIDC. No access keys anywhere.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
make all          # lint, tests, cdk synth for both environments. All offline.
```

Deploy with `make deploy-dev` or `make deploy-prod`. First time account setup
(OIDC provider, deploy roles, CDK bootstrap) is in
[`docs/runbook.md`](docs/runbook.md).

## Architecture

![Detailed architecture](docs/AWS-HELPDESK-EPA-DIAGRAM.png)

Officer → CloudFront/S3 → Cognito → API Gateway → `ingest` Lambda → DynamoDB
→ SQS → `process` Lambda → Bedrock, SES, custom metrics. Failed messages go
to a dead letter queue and the `redrive` Lambda retries them.

The reasoning behind the design (DynamoDB over RDS, one stack, batch size 1,
one account) is in [`docs/architecture.md`](docs/architecture.md).

## How a change gets to production

There is one branch, `main`. Where code lands is decided by the pipeline,
not by branch name.

1. Branch, edit, run `make all` locally.
2. Push and open a PR. `ci.yml` runs `ruff`, `pytest` with an 80% coverage
   gate, `cdk synth` for dev and prod, `checkov` on the synthesised
   templates, `pip-audit`, and CodeQL. Both jobs are required checks.
3. Merge. `deploy.yml` deploys to dev, publishes the frontend and runs a
   smoke test. Prod runs only after a reviewer approves it in the
   `production` environment, then does the same steps. If the prod smoke
   test fails the job redeploys the previous commit.

The dev and prod jobs assume different IAM roles, so a dev deploy cannot
touch prod. Step by step commands are in
[`docs/deploying.md`](docs/deploying.md).

Two other things ride the same pipeline. Dependabot PRs go through the
same checks and auto merge when they pass. `deploy.yml` also runs every
Monday at 03:00 so both environments are rebuilt from source even in a week
with no commits.

## Notes

`AI_CATEGORISATION_ENABLED` switches the classifier between Bedrock and a
keyword rule. The Bedrock code was merged to `main` with the toggle off while
the prompt was still being tuned. It also works as a kill switch.

Rollback is redeploying the previous commit. CloudFormation restores the
previous state; there is no separate rollback tooling.

Every alarm has an action. Most notify an SNS topic. The DLQ depth alarm
also invokes the `redrive` Lambda through its own topic, so a stuck queue is
retried before anyone is paged.

Both environments share one AWS account. That is a sandbox constraint and
the runbook says what the proper pattern is.

## Layout

```
app.py                    CDK entry point, one stack class, two environments
infra/helpdesk_stack.py   every resource in the platform
infra/bootstrap/          one-off CloudFormation for the GitHub OIDC roles
src/                      one Lambda asset, three handlers plus shared code
  common.py               logging and EMF metrics
  ingest.py               POST /tickets, GET /tickets/{id}
  process.py              categorise, route, notify, emit metrics
  redrive.py              automated DLQ recovery
frontend/                 static UI with PKCE sign in
scripts/                  publish frontend, smoke test
tests/                    handler tests and CDK template assertions
docs/                     architecture, deploying, runbook, threat model,
                          user needs, troubleshooting, metrics improvement
epa/                      apprenticeship assessment artefacts
```

## Docs

- [`docs/user-needs.md`](docs/user-needs.md): personas and MoSCoW stories
- [`docs/threat-model.md`](docs/threat-model.md): STRIDE table, accepted risks, suppressed scanner checks
- [`docs/troubleshooting.md`](docs/troubleshooting.md): two incidents worked through
- [`docs/metrics-improvement.md`](docs/metrics-improvement.md): the three custom metrics and the prompt change they drove
