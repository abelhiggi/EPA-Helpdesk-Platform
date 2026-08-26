#!/usr/bin/env python3
"""CDK application entry point.

Two environments from one stack definition. The only differences are the
account, the alarm recipient and the log retention period — everything else is
identical, which is the point: dev is a faithful rehearsal of prod.
"""

import os

import aws_cdk as cdk

from infra.helpdesk_stack import HelpdeskStack

app = cdk.App()

REGION = "eu-west-2"

ENVIRONMENTS = {
    "dev": {
        "account": os.environ.get("DEV_ACCOUNT_ID", "000000000000"),
        "log_retention_days": 7,
        "removal_policy_destroy": True,
    },
    "prod": {
        "account": os.environ.get("PROD_ACCOUNT_ID", "000000000000"),
        "log_retention_days": 90,
        "removal_policy_destroy": False,
    },
}

# Deploy one environment per pipeline job: cdk deploy -c env=dev
target = app.node.try_get_context("env") or "dev"
if target not in ENVIRONMENTS:
    raise ValueError(f"Unknown env '{target}'. Expected one of {list(ENVIRONMENTS)}.")

config = ENVIRONMENTS[target]

HelpdeskStack(
    app,
    f"Helpdesk-{target}",
    env_name=target,
    log_retention_days=config["log_retention_days"],
    destroy_on_delete=config["removal_policy_destroy"],
    alarm_email=os.environ.get("ALARM_EMAIL", ""),
    notification_from=os.environ.get("NOTIFICATION_FROM", ""),
    notification_to=os.environ.get("NOTIFICATION_TO", ""),
    env=cdk.Environment(account=config["account"], region=REGION),
    tags={
        "Service": "helpdesk",
        "Environment": target,
        "Owner": "DDaT",
        "Repository": "epa-helpdesk",
    },
)

app.synth()
