"""CDK Main Stack — all infra except Bedrock KBs and Agent.

Bedrock Knowledge Bases and Agent are created manually via console
because CloudFormation support for S3 Vectors is unstable.

After deploy, create KBs and Agent in the Bedrock console, then
update the Lambda env vars AGENT_ID and AGENT_ALIAS_ID.

Requirements: 1.7, 2.1, 3.1
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_sns as sns,
)
from constructs import Construct


class MainStack(Stack):

    def __init__(
        self, scope: Construct, construct_id: str, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------
        # 1. S3 Buckets
        # ---------------------------------------------------------------
        kb_config_bucket = s3.Bucket(
            self, "KBConfigBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        kb_logs_bucket = s3.Bucket(
            self, "KBLogsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    prefix="kb-logs/",
                    expiration=Duration.days(7),
                ),
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        audit_bucket = s3.Bucket(
            self, "AuditBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=True,
            lifecycle_rules=[
                s3.LifecycleRule(expiration=Duration.days(365)),
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        exports_bucket = s3.Bucket(
            self, "ExportsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=False,
            lifecycle_rules=[
                s3.LifecycleRule(
                    prefix="exports/",
                    expiration=Duration.hours(24),
                ),
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        frontend_bucket = s3.Bucket(
            self, "FrontendBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            website_index_document="index.html",
            website_error_document="index.html",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ---------------------------------------------------------------
        # 1b. DynamoDB Tables
        # ---------------------------------------------------------------
        configs_table = dynamodb.Table(
            self,
            "StreamingConfigs",
            partition_key=dynamodb.Attribute(
                name="PK",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="SK",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )
        configs_table.add_global_secondary_index(
            index_name="GSI_NomeCanal",
            partition_key=dynamodb.Attribute(
                name="servico",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="nome_canal",
                type=dynamodb.AttributeType.STRING,
            ),
        )

        logs_table = dynamodb.Table(
            self,
            "StreamingLogs",
            partition_key=dynamodb.Attribute(
                name="PK",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="SK",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )
        logs_table.add_global_secondary_index(
            index_name="GSI_Severidade",
            partition_key=dynamodb.Attribute(
                name="severidade",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="SK",
                type=dynamodb.AttributeType.STRING,
            ),
        )

        # ---------------------------------------------------------------
        # 2. Cognito User Pool
        # ---------------------------------------------------------------
        user_pool = cognito.UserPool(
            self, "ChatUserPool",
            user_pool_name="StreamingChatbotUsers",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(
                sms=False,
                otp=True,
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        user_pool_client = user_pool.add_client(
            "ChatWebClient",
            user_pool_client_name="streaming-chatbot-web",
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True,
            ),
            generate_secret=False,
        )

        # ---------------------------------------------------------------
        # 3. Frontend — CloudFront + S3 OAC
        # ---------------------------------------------------------------
        self.distribution = cloudfront.Distribution(
            self, "FrontendDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=(
                    origins.S3BucketOrigin
                    .with_origin_access_control(frontend_bucket)
                ),
                viewer_protocol_policy=(
                    cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS
                ),
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            default_root_object="index.html",
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
        )

        cf_domain = self.distribution.distribution_domain_name

        # ---------------------------------------------------------------
        # 3. Lambda_Orquestradora + API Gateway
        # ---------------------------------------------------------------
        orquestradora_fn = _lambda.Function(
            self, "OrquestradoraFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/orquestradora"),
            timeout=Duration.minutes(5),
            environment={
                "AGENT_ID": "AM81CBRWNE",
                "AGENT_ALIAS_ID": "TSTALIASID",
            },
        )
        orquestradora_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:*", "bedrock-agent-runtime:*", "bedrock-agent:*"],
            resources=["*"],
        ))

        # Lambda Function URL — bypasses API Gateway 29s timeout
        fn_url = orquestradora_fn.add_function_url(
            auth_type=_lambda.FunctionUrlAuthType.NONE,
            cors=_lambda.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[_lambda.HttpMethod.POST],
                allowed_headers=["Content-Type"],
            ),
        )

        self.api = apigw.RestApi(
            self, "ChatApi",
            rest_api_name="StreamingChatbotApi",
            description="REST API for the Streaming Chatbot",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["POST", "OPTIONS"],
                allow_headers=["Content-Type"],
            ),
        )

        chat_resource = self.api.root.add_resource("chat")
        chat_resource.add_method(
            "POST",
            apigw.LambdaIntegration(
                orquestradora_fn,
                timeout=Duration.seconds(29),
            ),
        )

        # ---------------------------------------------------------------
        # 4. Lambda_Configuradora
        # ---------------------------------------------------------------
        configuradora_fn = _lambda.Function(
            self, "ConfiguradoraFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/configuradora"),
            timeout=Duration.seconds(300),
            environment={
                "AUDIT_BUCKET": audit_bucket.bucket_name,
                "AUDIT_PREFIX": "audit/",
                "STREAMING_REGION": "sa-east-1",
                "EXPORTS_BUCKET": exports_bucket.bucket_name,
                "EXPORTS_PREFIX": "exports/",
                "PRESIGNED_URL_EXPIRY": "3600",
                "SPEKE_ROLE_ARN": "arn:aws:iam::761018874615:role/APIInvokeMediaPackageV2",
                "SPEKE_URL": "https://wgz3208af4.execute-api.sa-east-1.amazonaws.com/PROD_V2/NOKR",
                "CONFIGS_TABLE_NAME": (
                    configs_table.table_name
                ),
            },
        )
        configuradora_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "medialive:*", "mediapackage:*",
                "mediapackagev2:*",
                "mediatailor:*", "cloudfront:*",
            ],
            resources=["*"],
        ))
        configuradora_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "cloudwatch:GetMetricData",
                "cloudwatch:ListMetrics",
            ],
            resources=["*"],
        ))
        audit_bucket.grant_put(configuradora_fn)
        audit_bucket.grant_read(configuradora_fn)
        exports_bucket.grant_read_write(configuradora_fn)
        configs_table.grant_read_data(configuradora_fn)

        # Wire Orquestradora → Configuradora for direct config downloads
        orquestradora_fn.add_environment(
            "CONFIG_FUNCTION_NAME",
            configuradora_fn.function_name,
        )
        configuradora_fn.grant_invoke(orquestradora_fn)

        # ---------------------------------------------------------------
        # 5. Lambda_Exportadora
        # ---------------------------------------------------------------
        exportadora_fn = _lambda.Function(
            self, "ExportadoraFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/exportadora"),
            timeout=Duration.seconds(120),
            environment={
                "KB_CONFIG_BUCKET": kb_config_bucket.bucket_name,
                "KB_CONFIG_PREFIX": "kb-config/",
                "KB_LOGS_BUCKET": kb_logs_bucket.bucket_name,
                "KB_LOGS_PREFIX": "kb-logs/",
                "KB_ADS_BUCKET": kb_config_bucket.bucket_name,
                "KB_ADS_PREFIX": "kb-ads/",
                "EXPORTS_BUCKET": exports_bucket.bucket_name,
                "EXPORTS_PREFIX": "exports/",
                "PRESIGNED_URL_EXPIRY": "3600",
                "CONFIGS_TABLE_NAME": (
                    configs_table.table_name
                ),
                "LOGS_TABLE_NAME": (
                    logs_table.table_name
                ),
            },
        )
        kb_config_bucket.grant_read(exportadora_fn)
        kb_logs_bucket.grant_read(exportadora_fn)
        exports_bucket.grant_read_write(exportadora_fn)
        configs_table.grant_read_data(exportadora_fn)
        logs_table.grant_read_data(exportadora_fn)

        # Wire Orquestradora → Exportadora for direct export downloads
        orquestradora_fn.add_environment(
            "EXPORT_FUNCTION_NAME",
            exportadora_fn.function_name,
        )
        exportadora_fn.grant_invoke(orquestradora_fn)

        # ---------------------------------------------------------------
        # 6. Pipeline Config Lambda + EventBridge
        # ---------------------------------------------------------------
        pipeline_config_fn = _lambda.Function(
            self, "PipelineConfigFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/pipeline_config"),
            timeout=Duration.minutes(5),
            environment={
                "KB_CONFIG_BUCKET": kb_config_bucket.bucket_name,
                "KB_CONFIG_PREFIX": "kb-config/",
                "STREAMING_REGION": "sa-east-1",
                "MEDIATAILOR_REGION": "us-east-1",
                "CONFIGS_TABLE_NAME": (
                    configs_table.table_name
                ),
            },
        )
        pipeline_config_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "medialive:*", "mediapackage:*", "mediapackagev2:*",
                "mediatailor:*", "cloudfront:*",
            ],
            resources=["*"],
        ))
        kb_config_bucket.grant_put(pipeline_config_fn)
        configs_table.grant_write_data(pipeline_config_fn)

        config_schedule = events.Rule(
            self, "PipelineConfigSchedule",
            schedule=events.Schedule.rate(Duration.hours(6)),
        )
        config_schedule.add_target(
            targets.LambdaFunction(pipeline_config_fn)
        )

        # ---------------------------------------------------------------
        # 7. Pipeline Logs Lambda + EventBridge
        # ---------------------------------------------------------------
        pipeline_logs_fn = _lambda.Function(
            self, "PipelineLogsFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/pipeline_logs"),
            timeout=Duration.minutes(15),
            memory_size=512,
            environment={
                "KB_LOGS_BUCKET": kb_logs_bucket.bucket_name,
                "KB_LOGS_PREFIX": "kb-logs/",
                "STREAMING_REGION": "sa-east-1",
                "MEDIATAILOR_REGION": "us-east-1",
                "CLOUDFRONT_REGION": "us-east-1",
                "LOGS_TABLE_NAME": (
                    logs_table.table_name
                ),
            },
        )
        pipeline_logs_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "cloudwatch:GetMetricData",
                "cloudwatch:ListMetrics",
                "medialive:ListChannels",
                "medialive:DescribeChannel",
                "mediapackagev2:ListChannelGroups",
                "mediapackagev2:ListChannels",
                "mediapackagev2:ListOriginEndpoints",
                "mediatailor:ListPlaybackConfigurations",
                "cloudfront:ListDistributions",
            ],
            resources=["*"],
        ))
        kb_logs_bucket.grant_put(pipeline_logs_fn)
        logs_table.grant_write_data(pipeline_logs_fn)

        logs_schedule = events.Rule(
            self, "PipelineLogsSchedule",
            schedule=events.Schedule.rate(Duration.hours(1)),
        )
        logs_schedule.add_target(
            targets.LambdaFunction(pipeline_logs_fn)
        )

        # ---------------------------------------------------------------
        # 7b. Pipeline Ads Lambda + EventBridge + Secrets Manager
        # ---------------------------------------------------------------

        # Secret para credenciais SpringServe
        springserve_secret = secretsmanager.Secret(
            self, "SpringServeCredentials",
            secret_name="springserve/api-credentials",
            description="Credenciais da API SpringServe (email + password)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"email":"placeholder@empresa.com"}',
                generate_string_key="password",
            ),
        )

        pipeline_ads_fn = _lambda.Function(
            self, "PipelineAdsFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/pipeline_ads"),
            timeout=Duration.minutes(15),
            memory_size=512,
            environment={
                "KB_ADS_BUCKET": kb_config_bucket.bucket_name,
                "KB_ADS_PREFIX": "kb-ads/",
                "SPRINGSERVE_SECRET_NAME": springserve_secret.secret_name,
                "SPRINGSERVE_BASE_URL": "https://video.springserve.com",
                "CONFIGS_TABLE_NAME": configs_table.table_name,
                "MEDIATAILOR_REGION": "us-east-1",
                "WORKERS": "5",
            },
        )

        springserve_secret.grant_read(pipeline_ads_fn)
        kb_config_bucket.grant_put(pipeline_ads_fn)
        configs_table.grant_write_data(pipeline_ads_fn)
        pipeline_ads_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "mediatailor:ListPlaybackConfigurations",
                "mediatailor:GetPlaybackConfiguration",
            ],
            resources=["*"],
        ))

        ads_schedule = events.Rule(
            self, "PipelineAdsSchedule",
            schedule=events.Schedule.rate(Duration.hours(6)),
        )
        ads_schedule.add_target(targets.LambdaFunction(pipeline_ads_fn))

        # ---------------------------------------------------------------
        # 8. Proactive Alerts — SNS Topic + Permissions
        # ---------------------------------------------------------------
        alerts_topic = sns.Topic(
            self, "AlertsTopic",
            topic_name="StreamingAlertsNotifications",
        )
        alerts_topic.grant_publish(pipeline_logs_fn)
        kb_logs_bucket.grant_read(pipeline_logs_fn)

        pipeline_logs_fn.add_environment(
            "SNS_TOPIC_ARN", alerts_topic.topic_arn,
        )
        pipeline_logs_fn.add_environment(
            "ALERT_SEVERITY_THRESHOLD", "ERROR",
        )
        pipeline_logs_fn.add_environment(
            "ALERT_SUPPRESSION_MINUTES", "60",
        )

        # ---------------------------------------------------------------
        # Outputs
        # ---------------------------------------------------------------
        CfnOutput(self, "ApiUrl", value=self.api.url)
        CfnOutput(self, "FrontendUrl", value=f"https://{cf_domain}")
        CfnOutput(self, "KBConfigBucketName", value=kb_config_bucket.bucket_name)
        CfnOutput(self, "KBLogsBucketName", value=kb_logs_bucket.bucket_name)
        CfnOutput(self, "FrontendBucketName", value=frontend_bucket.bucket_name)
        CfnOutput(self, "OrquestradoraFunctionName", value=orquestradora_fn.function_name)
        CfnOutput(self, "OrquestradoraFunctionUrl", value=fn_url.url)
        CfnOutput(self, "CognitoUserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "CognitoClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(
            self, "AlertsTopicArn",
            value=alerts_topic.topic_arn,
        )
        CfnOutput(
            self, "ConfigsTableName",
            value=configs_table.table_name,
        )
        CfnOutput(
            self, "LogsTableName",
            value=logs_table.table_name,
        )
