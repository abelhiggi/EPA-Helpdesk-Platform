# Runbook

## Account layout: one account, two stacks

`Helpdesk-dev` and `Helpdesk-prod` currently deploy side by side into a single
AWS account (512738512057), not into separate accounts. That is a sandbox
constraint, not the intended production pattern.

The mitigation is that every physical resource name includes the environment
— KMS alias, S3 buckets, Cognito domain prefix, dashboard, canary, API
Gateway name — so the two stacks cannot collide with each other in this
account (see the naming choices throughout `infra/helpdesk_stack.py`).
Separate deploy roles per environment (below) further mean a compromised dev
credential cannot touch prod's resources.

What that mitigation does **not** give you is a hard boundary: both stacks
still share one account's IAM trust boundary and one account's service
quotas, so a misconfigured dev-role policy or a runaway resource in dev can
still degrade prod's headroom in ways that fully separate accounts would rule
out entirely. **Account-level isolation is the production pattern** — it is
enforced by AWS itself (a role in one account cannot act on another account's
resources without an explicit cross-account trust) rather than by naming
discipline, and it is what every step below should move to outside this
sandbox.

## One-time setup

1. Bootstrap the account once (bootstrap is account+region scoped, not
   per-stack):
   ```bash
   cdk bootstrap aws://512738512057/eu-west-2
   ```
2. Create the GitHub OIDC provider once, and **two** deploy roles in this same
   account — one scoped to `Helpdesk-dev/*` resources, one to
   `Helpdesk-prod/*` — each trusting `repo:<owner>/<repo>:ref:refs/heads/main`.
   Two roles even in one account: the point is that a dev deploy credential
   still cannot touch prod's resources, which is as close to the multi-account
   pattern as a single account can get. No static access keys, ever.
3. Repository secrets: `DEV_ACCOUNT_ID` and `PROD_ACCOUNT_ID` (both
   `512738512057` while dev and prod share an account), `DEV_DEPLOY_ROLE_ARN`,
   `PROD_DEPLOY_ROLE_ARN`. Repository variables: `ALARM_EMAIL`,
   `NOTIFICATION_FROM`, `NOTIFICATION_TO`.
4. Settings → Environments → `production`: add yourself as a required reviewer.
   This is the approval gate that makes prod Continuous Delivery rather than
   Continuous Deployment.
5. Verify the SES domain identity and enable Bedrock model access for
   Haiku 4.5 in eu-west-2. Both are one-time console actions outside the
   pipeline's control.
6. Create the first Cognito user, once per stack (dev and prod each have their
   own user pool):
   ```bash
   aws cognito-idp admin-create-user \
     --user-pool-id <pool-id> --username you@example.gov.uk \
     --user-attributes Name=email,Value=you@example.gov.uk Name=email_verified,Value=true
   ```
7. Confirm the alarm email subscription from your inbox.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
make all          # lint, tests, synth — everything CI runs, offline
```

## Deploy

```bash
make deploy-dev
make deploy-prod
```

Or push to `main`: dev deploys automatically, prod waits for the approval.

## Recovery

| Situation | Action | Expected MTTR |
|---|---|---|
| Bad deploy in prod | Re-run the deploy workflow on the previous commit | Under 10 min |
| Smoke gate fails after deploy | Automatic — the workflow redeploys `HEAD~1` | Under 10 min |
| DLQ has messages | Automatic — the alarm triggers redrive | Under 5 min |
| DLQ messages abandoned after redrive | Read the parked message, fix the cause, redeploy | Varies |
| Ticket data corrupted | DynamoDB point-in-time restore to a new table, then repoint | Under 30 min |

## Where to look when something is wrong

1. Dashboard `helpdesk-prod` — API errors, queue depth, DLQ depth, confidence.
2. The correlation ID from the API response, across all log groups:
   ```bash
   aws logs start-query --log-group-names /aws/lambda/Helpdesk-prod-Ingest... \
     --query-string 'fields @message | filter correlationId = "<id>"' \
     --start-time ... --end-time ...
   ```
3. X-Ray trace map for the same request — shows which hop failed and how long
   each took.
