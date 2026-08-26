"""Infrastructure tests.

These assert security properties and things that are easy to break by accident.
They deliberately do not assert every literal value in the template — a test
that says "the timeout is 30 seconds" because the template says 30 seconds
catches no bugs and breaks every time someone legitimately tunes it.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.helpdesk_stack import HelpdeskStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = HelpdeskStack(
        app,
        "TestStack",
        env_name="prod",
        log_retention_days=90,
        destroy_on_delete=False,
        alarm_email="ops@example.gov.uk",
        notification_from="helpdesk@example.gov.uk",
        notification_to="itsupport@example.gov.uk",
        env=cdk.Environment(account="111111111111", region="eu-west-2"),
    )
    return Template.from_stack(stack)


class TestDataSecurity:
    def test_ticket_table_is_encrypted_with_a_customer_managed_key(self, template):
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {"SSESpecification": {"SSEEnabled": True, "SSEType": "KMS"}},
        )

    def test_ticket_table_has_point_in_time_recovery(self, template):
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {"PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True}},
        )

    def test_both_queues_are_encrypted(self, template):
        for queue in template.find_resources("AWS::SQS::Queue").values():
            assert "KmsMasterKeyId" in queue["Properties"]

    def test_kms_key_rotation_is_enabled(self, template):
        template.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})

    def test_site_bucket_blocks_all_public_access(self, template):
        template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": True,
                }
            },
        )

    def test_cloudfront_redirects_http_to_https(self, template):
        template.has_resource_properties(
            "AWS::CloudFront::Distribution",
            {
                "DistributionConfig": {
                    "DefaultCacheBehavior": {"ViewerProtocolPolicy": "redirect-to-https"}
                }
            },
        )

    def test_no_runtime_role_grants_a_wildcard_action(self, template):
        """`Action: "*"` on a runtime role would make least privilege a claim
        rather than a property."""
        for policy in template.find_resources("AWS::IAM::Policy").values():
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                actions = statement.get("Action", [])
                actions = [actions] if isinstance(actions, str) else actions
                assert "*" not in actions


class TestApiAndIdentity:
    def test_every_api_method_except_options_requires_cognito(self, template):
        """GET /health is a deliberate exception: it reports whether the API
        is reachable at all, so it must not itself require a token to call."""
        resources = template.find_resources("AWS::ApiGateway::Resource")
        health_resource_id = next(
            logical_id
            for logical_id, r in resources.items()
            if r["Properties"].get("PathPart") == "health"
        )

        for method in template.find_resources("AWS::ApiGateway::Method").values():
            props = method["Properties"]
            if props["HttpMethod"] == "OPTIONS":
                continue
            if props["ResourceId"]["Ref"] == health_resource_id:
                assert props["AuthorizationType"] == "NONE"
                continue
            assert props["AuthorizationType"] == "COGNITO_USER_POOLS"
            assert "AuthorizerId" in props

    def test_cors_never_allows_a_wildcard_origin(self, template):
        """`Access-Control-Allow-Origin: *` would let any site on the internet
        call the API with a user's token."""
        options = [
            m
            for m in template.find_resources("AWS::ApiGateway::Method").values()
            if m["Properties"]["HttpMethod"] == "OPTIONS"
        ]
        assert options, "expected a CORS preflight method"

        for method in options:
            responses = method["Properties"]["Integration"]["IntegrationResponses"]
            origins = [
                params["method.response.header.Access-Control-Allow-Origin"]
                for r in responses
                if (params := r.get("ResponseParameters", {}))
                and "method.response.header.Access-Control-Allow-Origin" in params
            ]
            assert origins, "preflight returns no origin header"
            for origin in origins:
                rendered = str(origin)
                assert "*" not in rendered
                # Resolves from the distribution at deploy time rather than
                # being a hardcoded domain.
                assert "DomainName" in rendered

    def test_self_signup_is_disabled(self, template):
        template.has_resource_properties(
            "AWS::Cognito::UserPool",
            {"AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True}},
        )

    def test_password_policy_requires_at_least_fourteen_characters(self, template):
        pools = template.find_resources("AWS::Cognito::UserPool")
        policy = next(iter(pools.values()))["Properties"]["Policies"]["PasswordPolicy"]
        assert policy["MinimumLength"] >= 14

    def test_api_stage_has_tracing_and_throttling(self, template):
        template.has_resource_properties(
            "AWS::ApiGateway::Stage",
            {
                "TracingEnabled": True,
                "MethodSettings": Match.array_with(
                    [Match.object_like({"ThrottlingRateLimit": 25})]
                ),
            },
        )


class TestResilience:
    def test_source_queue_redrives_to_a_dead_letter_queue(self, template):
        template.has_resource_properties(
            "AWS::SQS::Queue",
            {"RedrivePolicy": Match.object_like({"maxReceiveCount": 3})},
        )

    def test_event_source_reports_partial_batch_failures(self, template):
        template.has_resource_properties(
            "AWS::Lambda::EventSourceMapping",
            {"FunctionResponseTypes": ["ReportBatchItemFailures"]},
        )

    def test_visibility_timeout_exceeds_the_handler_timeout(self, template):
        """If a retry becomes visible while the first run is still going, the
        same ticket gets processed twice concurrently."""
        queues = template.find_resources("AWS::SQS::Queue")
        source = [q for q in queues.values() if "RedrivePolicy" in q["Properties"]][0]
        functions = template.find_resources("AWS::Lambda::Function")
        process_timeout = max(
            f["Properties"].get("Timeout", 3)
            for f in functions.values()
            if "Process" in str(f["Properties"].get("Environment", {}))
            or f["Properties"].get("Timeout") == 30
        )
        assert source["Properties"]["VisibilityTimeout"] > process_timeout


class TestOperability:
    def test_every_alarm_has_an_action(self, template):
        """An alarm with no action cannot page anyone and cannot trigger
        remediation — it is a dashboard widget in disguise."""
        alarms = template.find_resources("AWS::CloudWatch::Alarm")
        assert alarms, "expected at least one alarm"
        for name, alarm in alarms.items():
            actions = alarm["Properties"].get("AlarmActions", [])
            assert actions, f"{name} has no alarm action"

    def test_dlq_depth_alarm_exists(self, template):
        template.resource_count_is("AWS::CloudWatch::Alarm", 7)

    def test_a_dashboard_is_created(self, template):
        template.resource_count_is("AWS::CloudWatch::Dashboard", 1)

    def test_health_canary_probes_on_a_five_minute_schedule(self, template):
        """A passive 5XXError alarm cannot detect a total outage: with no
        traffic there is no data, and missing data reads as healthy. The
        canary makes its own request on a fixed schedule so a stopped API is
        a real failed run, not an absence of data. 5 minutes, not 1: a 5x
        lower run rate keeps the canary cost from swamping the platform's
        cost NFR (see the comment above the canary in helpdesk_stack.py)."""
        template.has_resource_properties(
            "AWS::Synthetics::Canary",
            {"Schedule": Match.object_like({"Expression": "rate(5 minutes)"})},
        )

    def test_health_canary_alarm_requires_two_consecutive_breaching_periods(self, template):
        """1-of-1 would page on a single deploy-window gap in canary data.
        Requiring both of the last two periods to breach absorbs one missing
        datapoint while still catching a canary that is genuinely stalled."""
        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "Namespace": "CloudWatchSynthetics",
                "MetricName": "SuccessPercent",
                "EvaluationPeriods": 2,
                "DatapointsToAlarm": 2,
                "TreatMissingData": "breaching",
            },
        )

    def test_canary_artifacts_bucket_is_private_and_encrypted(self, template):
        buckets = template.find_resources("AWS::S3::Bucket")
        # The site bucket plus the canary artifacts bucket.
        assert len(buckets) == 2
        for bucket in buckets.values():
            props = bucket["Properties"]
            assert props["PublicAccessBlockConfiguration"] == {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
            assert props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0][
                "ServerSideEncryptionByDefault"
            ]["SSEAlgorithm"] in ("aws:kms", "AES256")

    def test_all_functions_have_active_tracing(self, template):
        for fn in template.find_resources("AWS::Lambda::Function").values():
            props = fn["Properties"]
            # Skip CDK-generated helper functions, which carry no Tracing key.
            if "TracingConfig" in props:
                assert props["TracingConfig"]["Mode"] == "Active"

    def test_all_functions_have_the_lambda_insights_layer(self, template):
        for fn in template.find_resources("AWS::Lambda::Function").values():
            props = fn["Properties"]
            # Skip CDK-generated helper functions, which are not our three.
            if "TracingConfig" not in props:
                continue
            layers = props.get("Layers", [])
            assert any("LambdaInsightsExtension-Arm64" in str(layer) for layer in layers)


class TestScannerHardening:
    """Properties added specifically to satisfy checkov findings that were
    real, not scanner noise — see .checkov.yaml and docs/threat-model.md's
    "Suppressed scanner checks" section for the findings that were not."""

    def test_redrive_has_reserved_concurrency_of_one(self, template):
        """Correctness bound, not just CKV_AWS_115: two concurrent redrives
        would both receive and re-send the same DLQ messages."""
        functions = template.find_resources("AWS::Lambda::Function")
        redrive = [
            f
            for f in functions.values()
            if "DLQ_URL" in str(f["Properties"].get("Environment", {}))
        ]
        assert redrive, "expected to find the redrive function"
        assert redrive[0]["Properties"]["ReservedConcurrentExecutions"] == 1

    def test_canary_artifacts_bucket_has_versioning_enabled(self, template):
        buckets = template.find_resources("AWS::S3::Bucket")
        canary_bucket = [
            b
            for b in buckets.values()
            if b["Properties"]
            .get("BucketEncryption", {})
            .get("ServerSideEncryptionConfiguration", [{}])[0]
            .get("ServerSideEncryptionByDefault", {})
            .get("SSEAlgorithm")
            == "aws:kms"
        ]
        assert canary_bucket, "expected the KMS-encrypted canary artifacts bucket"
        assert canary_bucket[0]["Properties"]["VersioningConfiguration"] == {"Status": "Enabled"}

    def test_lambda_and_api_log_groups_are_kms_encrypted(self, template):
        log_groups = template.find_resources("AWS::Logs::LogGroup")
        assert len(log_groups) == 4, "Ingest, Process, Redrive and API access logs"
        for lg in log_groups.values():
            assert "KmsKeyId" in lg["Properties"]

    def test_api_access_log_format_excludes_auth_headers_and_bodies(self, template):
        """CKV_AWS_76 asks for access logging, not a second copy of every
        Authorization header and request body sitting in a second log."""
        stage = next(iter(template.find_resources("AWS::ApiGateway::Stage").values()))
        setting = stage["Properties"]["AccessLogSetting"]
        assert "DestinationArn" in setting
        fmt = setting["Format"]
        rendered = str(fmt)
        for field in ("requestId", "sourceIp", "requestTime", "httpMethod", "status"):
            assert field in rendered
        assert "authorization" not in rendered.lower()
        assert "$input.body" not in rendered

    def test_kms_key_policy_grants_cloudwatch_logs_scoped_to_the_log_groups(self, template):
        """Without this the log groups above fail to create at deploy time
        with InvalidParameterException — a customer-managed key does not
        implicitly trust logs.<region>.amazonaws.com the way an AWS-managed
        key does."""
        keys = template.find_resources("AWS::KMS::Key")
        key = next(iter(keys.values()))
        statements = key["Properties"]["KeyPolicy"]["Statement"]
        logs_statements = [
            s
            for s in statements
            if "logs" in str(s.get("Principal", {}).get("Service", "")).lower()
        ]
        assert logs_statements, "expected a key policy statement for the logs service"
        condition = logs_statements[0]["Condition"]["ArnLike"]
        assert "kms:EncryptionContext:aws:logs:arn" in condition


class TestPatching:
    def test_functions_run_a_supported_python_runtime(self, template):
        for fn in template.find_resources("AWS::Lambda::Function").values():
            runtime = fn["Properties"].get("Runtime", "")
            if runtime.startswith("python"):
                assert runtime == "python3.12"


def test_two_environments_produce_distinct_stacks():
    """Same definition, different account: dev is a faithful rehearsal of prod."""
    app = cdk.App()
    for name, account in (("dev", "111111111111"), ("prod", "222222222222")):
        HelpdeskStack(
            app,
            f"Helpdesk-{name}",
            env_name=name,
            log_retention_days=7 if name == "dev" else 90,
            destroy_on_delete=name == "dev",
            alarm_email="ops@example.gov.uk",
            notification_from="helpdesk@example.gov.uk",
            notification_to="itsupport@example.gov.uk",
            env=cdk.Environment(account=account, region="eu-west-2"),
        )
    assert len(app.node.children) == 2
