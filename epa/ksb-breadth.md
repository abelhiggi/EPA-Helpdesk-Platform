# Technical breadth analysis

*The assessment plan asks for a maximum of 300 words on which project areas
provide evidence against which KSBs. This is 287.*

**Code quality (K2, K5, K7, K14, S9, S11, S14, S17, S18, S20, S22).**
`src/` holds Python handlers; `infra/helpdesk_stack.py` specifies the same
platform as infrastructure-as-code. Tests were written before handlers, using
`unittest.mock` doubles for DynamoDB, SQS, SES and Bedrock so the suite runs
offline in under a second — the base of the test pyramid, with one end-to-end
smoke gate at the top. `ruff`, `checkov` against synthesised CloudFormation,
`pip-audit`, CodeQL and Dependabot run on every pull request. `AI_CATEGORISATION_ENABLED`
is branching by abstraction: the Bedrock path merged to main switched off.
`../docs/troubleshooting.md` records one defect found and fixed.

**Meeting user needs (K4, K10, K21, S3).** `../docs/user-needs.md` carries three
personas, MoSCoW-prioritised stories and acceptance criteria; pytest case names
map onto them. All four should-haves are delivered. The frontend meets WCAG 2.2 AA.

**CI-CD (K1, K15, S15).** `ci.yml` gates every pull request; `deploy.yml` ships
to dev automatically and to prod behind an environment approval — Continuous
Deployment and Continuous Delivery demonstrated side by side.

**Refreshing and patching (K8, S5).** Dependabot raises PRs, CI gates them,
auto-merge lands them, and a weekly scheduled deploy rebuilds even without a
code change. Nothing is patched in place.

**Operability (K11, S6, S19, B3).** Five alarms, all with actions, one
dashboard, X-Ray tracing, and three custom business metrics whose improvement
loop is in `../docs/metrics-improvement.md`.

**Data persistence (K12, S7).** DynamoDB with one GSI, chosen for a key-value
access pattern; PITR for recovery. Correlation IDs plus X-Ray locate faults
across API Gateway, Lambda, SQS and Bedrock.

**Automation (K13, K17, S12).** `cdk deploy` builds everything from nothing;
the redrive function removes a manual triage step.

**Data security (K16, S10).** Customer-managed KMS at rest, TLS in transit,
Cognito plus per-record ownership checks, and a STRIDE model in
`../docs/threat-model.md`.
