"""CDK Stack — Pipeline Ads.

Cria:
- Lambda Pipeline_Ads (orquestrador)
- EventBridge schedule
- Secrets Manager para credenciais SpringServe
"""

from aws_cdk import (
    Duration,
    Stack,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class PipelineAdsStack(Stack):

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        kb_config_bucket,
        configs_table,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------
        # 1. Secrets Manager para credenciais SpringServe
        # ---------------------------------------------------------------
        springserve_secret = secretsmanager.Secret(
            self, "SpringServeCredentials",
            secret_name="springserve/api-credentials",
            description="Credenciais da API SpringServe (email + password)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"email":"placeholder@empresa.com"}',
                generate_string_key="password",
            ),
        )

        # ---------------------------------------------------------------
        # 2. Lambda Pipeline_Ads
        # ---------------------------------------------------------------
        pipeline_ads_fn = _lambda.Function(
            self, "PipelineAdsFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/pipeline_ads"),
            timeout=Duration.minutes(15),
            memory_size=1024,
            environment={
                "KB_ADS_BUCKET": kb_config_bucket.bucket_name,
                "KB_ADS_PREFIX": "kb-ads/",
                "SPRINGSERVE_SECRET_NAME": springserve_secret.secret_name,
                "SPRINGSERVE_BASE_URL": "https://video.springserve.com",
                "CONFIGS_TABLE_NAME": configs_table.table_name,
                "MEDIATAILOR_REGION": "us-east-1",
                "WORKERS": "3",
            },
        )

        # ---------------------------------------------------------------
        # 3. Permissões IAM
        # ---------------------------------------------------------------
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

        # ---------------------------------------------------------------
        # 4. EventBridge Schedule
        # ---------------------------------------------------------------
        ads_schedule = events.Rule(
            self, "PipelineAdsSchedule",
            schedule=events.Schedule.rate(Duration.hours(6)),
            description="Executa Pipeline_Ads a cada 6 horas",
        )
        ads_schedule.add_target(targets.LambdaFunction(pipeline_ads_fn))

        # ---------------------------------------------------------------
        # 5. Outputs
        # ---------------------------------------------------------------
        self.pipeline_ads_function = pipeline_ads_fn
        self.springserve_secret = springserve_secret
