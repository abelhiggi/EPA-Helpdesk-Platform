# Architecture

```
                  ┌──────────────┐
   Officer ──────▶│  CloudFront  │──▶ S3 (static UI, OAC only)
                  └──────┬───────┘
                         │  runtime-config.json (from stack outputs)
                         ▼
                  ┌──────────────┐
                  │   Cognito    │  PKCE S256, admin-create only
                  └──────┬───────┘
                         │ ID token
                         ▼
              ┌────────────────────┐
              │   API Gateway      │  POST /tickets
              │  Cognito authorizer│  GET  /tickets/{id}
              └─────────┬──────────┘
                        ▼
                 ┌─────────────┐        ┌──────────────┐
                 │   ingest    │───────▶│  DynamoDB    │  CMK, PITR
                 └──────┬──────┘        │  + GSI       │
                        │               └──────▲───────┘
                        ▼                      │
                 ┌─────────────┐               │
                 │  SQS queue  │               │
                 └──────┬──────┘               │
                        │ batch 1, mrc 3       │
                        ▼                      │
                 ┌─────────────┐               │
                 │   process   │───────────────┘
                 └──┬───┬───┬──┘
                    │   │   └──▶ SES  (team notification)
                    │   └──────▶ Bedrock (Haiku 4.5, Converse)
                    └──────────▶ EMF custom metrics
                        │
                   (3 failures)
                        ▼
                 ┌─────────────┐
                 │     DLQ     │
                 └──────┬──────┘
                        │ depth ≥ 1
                        ▼
                 ┌─────────────┐
                 │   redrive   │──▶ back to SQS queue
                 └─────────────┘      (max 2 attempts, then parked)

Observability: 5 alarms → SNS, 1 dashboard, X-Ray active on all functions
Deployment:    GitHub Actions → OIDC → cdk deploy → dev, then gated prod
```

## Why this shape

**Event-driven, not synchronous.** Bedrock takes one to three seconds. Putting
that in the request path means the officer waits, and a Bedrock throttle becomes
a failed submission. The queue makes the API fast and makes AI failure
recoverable rather than user-visible.

**One stack, not six.** CDK resolves deployment order from the construct graph.
Splitting into stacks would require an orchestration layer to maintain and gains
nothing at 28 resources.

**Three functions, not four.** `POST` and `GET` share validation, identity
extraction and table access, so they share a function. Splitting them would mean
two cold starts and two roles for forty lines of logic.

**Rollback is redeploying the previous commit.** CloudFormation already restores
prior state. A bespoke release-manifest and restore script is a second thing to
test, and a rollback path you never exercise is not a rollback path.
