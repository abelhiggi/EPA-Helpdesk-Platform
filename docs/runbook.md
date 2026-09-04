# Deploying a change

How a code change gets from a laptop to production. Three parts: how the
pipeline authenticates to AWS, how to make and push a change, and how it is
merged and deployed.

## 1. How GitHub Actions authenticates to AWS

There are no AWS access keys in this repository or in GitHub secrets. GitHub
Actions proves its identity with a short-lived OIDC token, and AWS exchanges
that for temporary credentials that expire within the hour.

This is set up once per AWS account by `infra/bootstrap/github-oidc.yaml`:

1. An IAM **OIDC provider** trusting `token.actions.githubusercontent.com`.
   This tells AWS to accept identity tokens issued by GitHub.
2. Two IAM **roles**, `github-actions-cdk-deploy-dev` and
   `github-actions-cdk-deploy-prod`. Each role's trust policy requires the
   token's `repository` claim to equal `abelhiggi/EPA-Helpdesk-Platform`.
   Without that condition any GitHub repository could assume the role.
3. The role ARNs are stored as GitHub Actions secrets: `DEV_DEPLOY_ROLE_ARN`,
   `PROD_DEPLOY_ROLE_ARN`, plus `DEV_ACCOUNT_ID` and `PROD_ACCOUNT_ID`.
4. In `.github/workflows/deploy.yml`, the
   `aws-actions/configure-aws-credentials` step passes
   `role-to-assume: ${{ secrets.DEV_DEPLOY_ROLE_ARN }}` (or the prod
   equivalent). Everything after that step runs with the temporary
   credentials.

The dev role cannot touch prod resources and vice versa.

## 2. Making a change

Start from an up-to-date `main` and work on a branch:

```bash
git checkout main
git pull origin main
git checkout -b fix/short-description
```

Edit the code. Before pushing, run the same checks CI will run. This is
fully offline and takes about a minute:

```bash
make all      # ruff, pytest with coverage gate, cdk synth for both environments
```

Commit and push the branch:

```bash
git add <files>
git commit -m "fix: what changed and why, in one line"
git push -u origin fix/short-description
```

## 3. Merging and deploying

1. Open the pull request link that GitHub prints after the push, or go to
   **Pull requests → New pull request**. Base is `main`, compare is your
   branch. Give it a one-line title and create it.
2. Two required status checks run automatically: `CI / verify` (lint,
   tests, synth, checkov, pip-audit) and `CI / codeql`. The merge button
   stays disabled until both are green.
3. Choose **Squash and merge**. This keeps `main` at one commit per change.
   Delete the branch when prompted.
4. Merging to `main` triggers `deploy.yml`:
   - The **dev** job runs with no approval: `cdk deploy` to `Helpdesk-dev`,
     publish the frontend, run the smoke test.
   - The **prod** job waits for a reviewer. Go to **Actions → the running
     workflow → Review deployments → approve `production`**. It then
     deploys `Helpdesk-prod`, publishes the frontend and runs the smoke
     test. If the smoke test fails, the job checks out the previous commit
     and redeploys it.
5. Bring your local `main` up to date:

```bash
git checkout main
git pull origin main
```

## Related

- `docs/runbook.md` for first-time account setup and recovery.
- `docs/architecture.md` for what gets deployed and why it is shaped that way.