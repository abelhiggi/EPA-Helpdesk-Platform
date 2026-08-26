"""The whole platform, in one stack.

Six CloudFormation stacks with a bespoke ordering script was the wrong shape for
a system this size: CDK resolves dependency order from the construct graph, so
splitting buys nothing and costs an orchestration layer to maintain.

65 resources in prod, 69 in dev (dev also deploys the auto-delete-on-destroy
custom resource for its two S3 buckets, since prod buckets are retained, not
destroyed). Confirmed by synthesising both stacks with `cdk synth`, not
estimated. Every one of them is here because a pass or distinction criterion
needs it, or because the thing genuinely will not run without it.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_synthetics as synthetics
from constructs import Construct

# Inference profile: Haiku 4.5 is the cheapest model in eu-west-2 that returns
# reliable structured JSON for a two-class problem. Parameterised so the model
# can be swapped without touching handler code.
BEDROCK_MODEL_ID = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
METRIC_NAMESPACE = "EPA/Helpdesk"

# An active prober, not a passive log scrape: it makes its own request every
# minute, so a stopped API produces a real failed run rather than an absence
# of data. HEALTH_CHECK_URL is supplied as a canary environment variable.
HEALTH_CANARY_SCRIPT = """\
const synthetics = require('Synthetics');

const checkHealth = async function () {
    const target = new URL(process.env.HEALTH_CHECK_URL);
    const requestOptions = {
        hostname: target.hostname,
        method: 'GET',
        path: target.pathname,
        port: 443,
        protocol: 'https:',
        headers: {},
    };
    requestOptions['headers']['User-Agent'] = [
        synthetics.getCanaryUserAgentString(),
        requestOptions['headers']['User-Agent'],
    ].join(' ');

    const stepConfig = {
        includeRequestHeaders: true,
        includeResponseHeaders: true,
        includeRequestBody: false,
        includeResponseBody: true,
        continueOnHttpStepFailure: false,
    };

    await synthetics.executeHttpStep(
        'GET /health',
        requestOptions,
        async (res) => {
            return new Promise((resolve, reject) => {
                if (res.statusCode !== 200) {
                    reject(new Error(`/health returned ${res.statusCode}`));
                    return;
                }
                let body = '';
                res.on('data', (chunk) => (body += chunk));
                res.on('end', () => {
                    try {
                        if (JSON.parse(body).status !== 'ok') {
                            reject(new Error(`unexpected /health body: ${body}`));
                            return;
                        }
                        resolve();
                    } catch (err) {
                        reject(new Error(`/health body was not JSON: ${body}`));
                    }
                });
            });
        },
        stepConfig,
    );
};

exports.handler = async () => {
    return await checkHealth();
};
"""


class HelpdeskStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        log_retention_days: int,
        destroy_on_delete: bool,
        alarm_email: str,
        notification_from: str,
        notification_to: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.env_name = env_name
        removal = RemovalPolicy.DESTROY if destroy_on_delete else RemovalPolicy.RETAIN
        retention = (
            logs.RetentionDays.ONE_WEEK
            if log_retention_days <= 7
            else logs.RetentionDays.THREE_MONTHS
        )

        # ------------------------------------------------------------------
        # Encryption. One customer-managed key, not three. Ticket descriptions
        # are the only sensitive payload and they live in exactly one table and
        # one queue, so a single key with rotation is proportionate. Separate
        # keys per service would be blast-radius theatre at this scale.
        # ------------------------------------------------------------------
        key = kms.Key(
            self,
            "DataKey",
            alias=f"alias/helpdesk-{env_name}",
            enable_key_rotation=True,
            removal_policy=removal,
            description="Encrypts helpdesk tickets at rest and in queue.",
        )

        # ------------------------------------------------------------------
        # Persistence. DynamoDB: the access pattern is a single-item read by
        # ticketId and a status-ordered list for the staff view. Key-value with
        # one GSI, no joins, no relational needs — so no RDS to patch or scale.
        # ------------------------------------------------------------------
        table = dynamodb.Table(
            self,
            "Tickets",
            partition_key=dynamodb.Attribute(name="ticketId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=key,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=removal,
        )
        table.add_global_secondary_index(
            index_name="status-createdAt",
            partition_key=dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="createdAt", type=dynamodb.AttributeType.STRING),
        )

        # ------------------------------------------------------------------
        # Async decoupling. The DLQ is what makes the redrive automation
        # possible, and maxReceiveCount 3 gives transient Bedrock throttling a
        # chance to clear before a message is parked.
        # ------------------------------------------------------------------
        dlq = sqs.Queue(
            self,
            "TicketDlq",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=key,
            retention_period=Duration.days(14),
        )
        queue = sqs.Queue(
            self,
            "TicketQueue",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=key,
            retention_period=Duration.days(14),
            # Six times the handler timeout, so a retry never overlaps a run
            # still in flight.
            visibility_timeout=Duration.seconds(180),
            receive_message_wait_time=Duration.seconds(20),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq),
        )

        # ------------------------------------------------------------------
        # Identity
        # ------------------------------------------------------------------
        user_pool = cognito.UserPool(
            self,
            "Users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False)
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=14,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=removal,
        )

        # ------------------------------------------------------------------
        # Frontend. Built before the API so the CloudFront domain can seed the
        # Cognito callback URL and the API's CORS origin in one pass — the
        # circular dependency that forced a two-pass deploy last time only
        # exists if the CSP is pinned in the same template as the origin.
        # ------------------------------------------------------------------
        site_bucket = s3.Bucket(
            self,
            "Site",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=removal,
            auto_delete_objects=destroy_on_delete,
        )
        distribution = cloudfront.Distribution(
            self,
            "Cdn",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                response_headers_policy=cloudfront.ResponseHeadersPolicy(
                    self,
                    "SecurityHeaders",
                    security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                        strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                            access_control_max_age=Duration.days(365),
                            include_subdomains=True,
                            override=True,
                        ),
                        content_type_options=cloudfront.ResponseHeadersContentTypeOptions(
                            override=True
                        ),
                        frame_options=cloudfront.ResponseHeadersFrameOptions(
                            frame_option=cloudfront.HeadersFrameOption.DENY,
                            override=True,
                        ),
                        referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                            referrer_policy=cloudfront.HeadersReferrerPolicy.SAME_ORIGIN,
                            override=True,
                        ),
                    ),
                ),
            ),
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
        )
        site_origin = f"https://{distribution.distribution_domain_name}"

        client = user_pool.add_client(
            "WebClient",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"{site_origin}/"],
                logout_urls=[f"{site_origin}/"],
            ),
            access_token_validity=Duration.minutes(60),
            id_token_validity=Duration.minutes(60),
        )
        domain = user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"helpdesk-{env_name}-{self.account[-6:]}"
            ),
        )

        # ------------------------------------------------------------------
        # Compute. Three functions, each with one job.
        # ------------------------------------------------------------------
        common_env = {
            "TABLE_NAME": table.table_name,
            "ENVIRONMENT": env_name,
            "METRIC_NAMESPACE": METRIC_NAMESPACE,
            "SERVICE_NAME": "helpdesk",
        }

        # Manually pinned dependency: this is an AWS-published layer, not a pip
        # or npm package, so Dependabot cannot see or patch it. Verified
        # against the account's own eu-west-2 API (not just the docs page,
        # which lags) with:
        #   aws lambda get-layer-version-by-arn --region eu-west-2 --arn \
        #     arn:aws:lambda:eu-west-2:580247275435:layer:LambdaInsightsExtension-Arm64:<n>
        # Version 35 confirmed current as of 2026-08-26. Review quarterly.
        insights_layer = lambda_.LayerVersion.from_layer_version_arn(
            self,
            "LambdaInsightsLayer",
            "arn:aws:lambda:eu-west-2:580247275435:layer:LambdaInsightsExtension-Arm64:35",
        )

        def make_fn(name: str, entrypoint: str, timeout: int, memory: int, env: dict):
            # An explicit log group, rather than the retention helper, because
            # the helper deploys a custom resource Lambda purely to set a
            # retention value CloudFormation can set directly.
            log_group = logs.LogGroup(
                self,
                f"{name}Logs",
                retention=retention,
                removal_policy=removal,
            )
            fn = lambda_.Function(
                self,
                name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                # One asset for all three functions. The alternative — a copy of
                # common.py per handler directory, or a Lambda layer — costs
                # either a build step that can be forgotten or an extra
                # resource. A few unused KB per bundle is the cheaper trade.
                code=lambda_.Code.from_asset("src"),
                handler=entrypoint,
                timeout=Duration.seconds(timeout),
                memory_size=memory,
                # X-Ray gives the end-to-end trace that S7 troubleshooting
                # evidence depends on.
                tracing=lambda_.Tracing.ACTIVE,
                log_group=log_group,
                environment={**common_env, **env},
                layers=[insights_layer],
            )
            fn.role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "CloudWatchLambdaInsightsExecutionRolePolicy"
                )
            )
            return fn

        ingest = make_fn(
            "Ingest",
            "ingest.handler",
            10,
            256,
            {"QUEUE_URL": queue.queue_url},
        )
        process = make_fn(
            "Process",
            "process.handler",
            30,
            512,
            {
                "BEDROCK_MODEL_ID": BEDROCK_MODEL_ID,
                "NOTIFICATION_FROM": notification_from,
                "NOTIFICATION_TO": notification_to,
                # Feature toggle: lets the Bedrock integration merge to main
                # behind an abstraction while it is still being tuned (S20).
                "AI_CATEGORISATION_ENABLED": "true",
            },
        )
        redrive = make_fn(
            "Redrive",
            "redrive.handler",
            60,
            256,
            {"DLQ_URL": dlq.queue_url, "QUEUE_URL": queue.queue_url},
        )

        table.grant_read_write_data(ingest)
        table.grant_read_data(ingest)
        table.grant_read_write_data(process)
        queue.grant_send_messages(ingest)
        queue.grant_send_messages(redrive)
        dlq.grant_consume_messages(redrive)
        key.grant_encrypt_decrypt(ingest)
        key.grant_encrypt_decrypt(process)
        key.grant_encrypt_decrypt(redrive)

        process.add_event_source(
            sources.SqsEventSource(
                queue,
                batch_size=1,
                report_batch_item_failures=True,
                max_concurrency=5,
            )
        )

        process.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{BEDROCK_MODEL_ID}",
                    "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-*",
                ],
            )
        )
        process.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail"],
                resources=["*"],
                conditions={"StringEquals": {"ses:FromAddress": notification_from}}
                if notification_from
                else None,
            )
        )

        # ------------------------------------------------------------------
        # API. Two routes: submit a ticket, read one back. The GET is the
        # should-have that makes status visible to the person who raised it.
        # ------------------------------------------------------------------
        api = apigw.RestApi(
            self,
            "Api",
            # Both stacks share one account: without this, both APIs would
            # show up in the API Gateway console under the identical name
            # "Api", indistinguishable except by ID.
            rest_api_name=f"helpdesk-{env_name}-api",
            deploy_options=apigw.StageOptions(
                stage_name=env_name,
                tracing_enabled=True,
                metrics_enabled=True,
                throttling_rate_limit=25,
                throttling_burst_limit=50,
                logging_level=apigw.MethodLoggingLevel.INFO,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=[site_origin],
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type"],
            ),
        )
        authorizer = apigw.CognitoUserPoolsAuthorizer(
            self, "Authorizer", cognito_user_pools=[user_pool]
        )
        tickets = api.root.add_resource("tickets")
        tickets.add_method(
            "POST",
            apigw.LambdaIntegration(ingest),
            authorizer=authorizer,
            authorization_type=apigw.AuthorizationType.COGNITO,
        )
        tickets.add_resource("{ticketId}").add_method(
            "GET",
            apigw.LambdaIntegration(ingest),
            authorizer=authorizer,
            authorization_type=apigw.AuthorizationType.COGNITO,
        )

        # Unauthenticated on purpose: a reachability probe that needs a token
        # to answer isn't a health check.
        health = api.root.add_resource("health")
        health.add_method(
            "GET",
            apigw.LambdaIntegration(ingest),
            authorization_type=apigw.AuthorizationType.NONE,
        )

        # ------------------------------------------------------------------
        # Active availability probe. AWS/ApiGateway 5XXError only publishes a
        # datapoint when something calls the API, and NOT_BREACHING treats
        # missing data as healthy — so a total outage with zero traffic stays
        # green. A canary generates its own traffic regardless of whether
        # anyone else is calling the API.
        #
        # Cost (CloudWatch Synthetics bills per canary run — no separate
        # Lambda charge, that's bundled into the run price): at this 5-minute
        # schedule, one canary runs 12/hour × 24 × 30 ≈ 8,640 times a month.
        # eu-west-2's exact per-run rate could not be pulled from the AWS
        # Pricing API in this environment (SSL interception on that endpoint
        # blocked it); using the published us-east-1 rate of $0.0012/run as a
        # floor and eu-central-1's $0.0016/run as a same-region-family
        # ceiling: ~$10–14/month for ONE canary, ~$21–28/month (~£15–20 at
        # ~$1.36/£) for BOTH the dev and prod canaries running continuously,
        # as this stack does. That is at or above the entire platform's
        # "under £15/month" NFR (docs/user-needs.md) from the canaries alone,
        # before DynamoDB, Lambda, Bedrock, SES, CloudFront or anything else
        # is counted — it does NOT cleanly fit. Verify the real number in
        # Cost Explorer after first deploy; if it doesn't fit, the fix is
        # either a longer interval or running the canary in prod only, not a
        # louder comment.
        # ------------------------------------------------------------------
        canary_artifacts_bucket = s3.Bucket(
            self,
            "CanaryArtifacts",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=key,
            enforce_ssl=True,
            removal_policy=removal,
            auto_delete_objects=destroy_on_delete,
        )
        health_canary = synthetics.Canary(
            self,
            "HealthCanary",
            canary_name=f"helpdesk-{env_name}-health",
            runtime=synthetics.Runtime.SYNTHETICS_NODEJS_PUPPETEER_13_0,
            schedule=synthetics.Schedule.rate(Duration.minutes(5)),
            artifacts_bucket_location=synthetics.ArtifactsBucketLocation(
                bucket=canary_artifacts_bucket
            ),
            environment_variables={"HEALTH_CHECK_URL": f"{api.url}health"},
            test=synthetics.Test.custom(
                code=synthetics.Code.from_inline(HEALTH_CANARY_SCRIPT),
                handler="index.handler",
            ),
        )

        # ------------------------------------------------------------------
        # Operability. Alarms, all of which do something. An alarm with no
        # action is a dashboard widget wearing a costume.
        # ------------------------------------------------------------------
        alarm_topic = sns.Topic(self, "Alarms", master_key=key)
        if alarm_email:
            alarm_topic.add_subscription(subs.EmailSubscription(alarm_email))
        action = cw_actions.SnsAction(alarm_topic)

        # SuccessPercent, not the 5XXError alarm above: this is the signal
        # that answers "is /health actually reachable right now", and missing
        # data (the canary itself failing to run) is treated as a breach
        # rather than silently healthy.
        #
        # 2 datapoints out of 2 evaluation periods, not 1 of 1: a deploy that
        # briefly interrupts the canary's own Lambda produces one missing
        # (and therefore breaching, per treat_missing_data above) datapoint —
        # with 1-of-1 that alone would page. Requiring both of the last two
        # 5-minute periods to breach absorbs a single deploy-window gap while
        # still catching a canary that is genuinely stalled, since a stall
        # produces missing data in every period, not just one.
        canary_availability_alarm = cw.Alarm(
            self,
            "HealthCanaryAlarm",
            metric=health_canary.metric_success_percent(period=Duration.minutes(5)),
            threshold=100,
            evaluation_periods=2,
            datapoints_to_alarm=2,
            comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.BREACHING,
        )
        canary_availability_alarm.add_alarm_action(action)

        alarms = {
            "DlqDepth": dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(1), statistic="Maximum"
            ),
            "ProcessErrors": process.metric_errors(period=Duration.minutes(5)),
            "IngestErrors": ingest.metric_errors(period=Duration.minutes(5)),
            "ApiServerErrors": api.metric_server_error(period=Duration.minutes(5)),
            # /health specifically, not the whole API: this is the signal
            # that the officer-facing site itself is unreachable.
            "HealthCheckErrors": cw.Metric(
                namespace="AWS/ApiGateway",
                metric_name="5XXError",
                dimensions_map={
                    "ApiName": api.rest_api_name,
                    "Stage": env_name,
                    "Resource": "/health",
                    "Method": "GET",
                },
                statistic="Sum",
                period=Duration.minutes(5),
            ),
        }
        created_alarms: dict[str, cw.Alarm] = {}
        for name, metric in alarms.items():
            alarm = cw.Alarm(
                self,
                f"{name}Alarm",
                metric=metric,
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            alarm.add_alarm_action(action)
            created_alarms[name] = alarm

        # The DLQ alarm also triggers the redrive function: automation that
        # removes a manual triage step rather than just reporting one.
        dlq_alarm_topic = sns.Topic(self, "DlqRemediation", master_key=key)
        dlq_alarm_topic.add_subscription(subs.LambdaSubscription(redrive))
        cw.Alarm(
            self,
            "DlqRemediationAlarm",
            metric=dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(1), statistic="Maximum"
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(cw_actions.SnsAction(dlq_alarm_topic))

        cw.Dashboard(
            self,
            "Dashboard",
            dashboard_name=f"helpdesk-{env_name}",
            widgets=[
                [
                    cw.GraphWidget(
                        title="API requests and errors",
                        left=[api.metric_count(), api.metric_server_error()],
                        width=12,
                    ),
                    cw.GraphWidget(
                        title="Lambda duration and memory headroom",
                        left=[process.metric_duration(), ingest.metric_duration()],
                        right=[
                            cw.Metric(
                                namespace="LambdaInsights",
                                metric_name="memory_utilization",
                                dimensions_map={"function_name": fn.function_name},
                                statistic="Maximum",
                                label=label,
                            )
                            for fn, label in ((process, "process"), (ingest, "ingest"))
                        ],
                        width=12,
                    ),
                ],
                [
                    cw.AlarmWidget(
                        title="API 5XXError rate on /health (passive)",
                        alarm=created_alarms["HealthCheckErrors"],
                        width=12,
                    ),
                    cw.AlarmWidget(
                        title="Health check canary success (active)",
                        alarm=canary_availability_alarm,
                        width=12,
                    ),
                ],
                [
                    cw.GraphWidget(
                        title="Tickets by category (custom)",
                        left=[
                            cw.Metric(
                                namespace=METRIC_NAMESPACE,
                                metric_name="TicketsSubmitted",
                                dimensions_map={
                                    "Service": "helpdesk",
                                    "Environment": env_name,
                                    "Category": c,
                                },
                                statistic="Sum",
                                label=c,
                            )
                            for c in ("network", "software")
                        ],
                        width=8,
                    ),
                    cw.GraphWidget(
                        title="Categorisation confidence (custom)",
                        left=[
                            cw.Metric(
                                namespace=METRIC_NAMESPACE,
                                metric_name="CategorisationConfidence",
                                statistic="Average",
                            )
                        ],
                        width=8,
                    ),
                    cw.GraphWidget(
                        title="Time to route, seconds (custom)",
                        left=[
                            cw.Metric(
                                namespace=METRIC_NAMESPACE,
                                metric_name="TimeToRouteSeconds",
                                statistic="p90",
                            )
                        ],
                        width=8,
                    ),
                ],
                [
                    cw.GraphWidget(
                        title="Queue depth and DLQ",
                        left=[
                            queue.metric_approximate_number_of_messages_visible(),
                            dlq.metric_approximate_number_of_messages_visible(),
                        ],
                        width=24,
                    )
                ],
            ],
        )

        # ------------------------------------------------------------------
        # Outputs consumed by the frontend config generator and the pipeline.
        # ------------------------------------------------------------------
        CfnOutput(self, "ApiUrl", value=api.url)
        CfnOutput(self, "SiteUrl", value=site_origin)
        CfnOutput(self, "SiteBucketName", value=site_bucket.bucket_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        CfnOutput(self, "CognitoClientId", value=client.user_pool_client_id)
        CfnOutput(self, "CognitoDomain", value=domain.base_url())
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
