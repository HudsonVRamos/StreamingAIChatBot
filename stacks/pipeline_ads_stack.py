"""CDK Stack — Pipeline Ads com SQS para processamento em batch.

Cria:
- Lambda Pipeline_Ads (orquestrador)
- SQS Queue para batches de supply tags
- Lambda Pipeline_Ads_Batch (processador)
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
    aws_lambda_event_sources as lambda_events,
    aws_secretsmanager as secretsmanager,
    aws_sqs as sqs,
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
        # 2. SQS Queue para batches de supply tags
        # ---------------------------------------------------------------
        # Dead Letter Queue para mensagens que falham repetidamente
        supply_tags_dlq = sqs.Queue(
            self, "SupplyTagsDLQ",
            queue_name="pipeline-ads-supply-tags-dlq",
            retention_period=Duration.days(3),
        )

        supply_tags_queue = sqs.Queue(
            self, "SupplyTagsQueue",
            queue_name="pipeline-ads-supply-tags",
            visibility_timeout=Duration.minutes(15),
            retention_period=Duration.days(1),
            receive_message_wait_time=Duration.seconds(20),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=2,  # Tenta 2x antes de ir para DLQ
                queue=supply_tags_dlq,
            ),
        )

        # ---------------------------------------------------------------
        # 3. Lambda Pipeline_Ads (orquestrador)
        # ---------------------------------------------------------------
        pipeline_ads_fn = _lambda.Function(
            self, "PipelineAdsFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/pipeline_ads"),
            timeout=Duration.minutes(15),  # Increased for inline processing
            memory_size=1024,  # Increased memory
            environment={
                "KB_ADS_BUCKET": kb_config_bucket.bucket_name,
                "KB_ADS_PREFIX": "kb-ads/",
                "SPRINGSERVE_SECRET_NAME": springserve_secret.secret_name,
                "SPRINGSERVE_BASE_URL": "https://video.springserve.com",
                "CONFIGS_TABLE_NAME": configs_table.table_name,
                "MEDIATAILOR_REGION": "us-east-1",
                "WORKERS": "3",
                "SUPPLY_TAGS_QUEUE_URL": supply_tags_queue.queue_url,
            },
        )

        # ---------------------------------------------------------------
        # 4. Lambda Pipeline_Ads_Batch (processador)
        # ---------------------------------------------------------------
        pipeline_ads_batch_fn = _lambda.Function(
            self, "PipelineAdsBatchFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/pipeline_ads_batch"),
            timeout=Duration.minutes(15),  # Mais tempo para processar batch
            memory_size=1024,
            reserved_concurrent_executions=5,  # Limita concorrência
            environment={
                "KB_ADS_BUCKET": kb_config_bucket.bucket_name,
                "KB_ADS_PREFIX": "kb-ads/",
                "SPRINGSERVE_SECRET_NAME": springserve_secret.secret_name,
                "SPRINGSERVE_BASE_URL": "https://video.springserve.com",
                "CONFIGS_TABLE_NAME": configs_table.table_name,
            },
        )

        # ---------------------------------------------------------------
        # 5. SQS Event Source para Pipeline_Ads_Batch
        # ---------------------------------------------------------------
        pipeline_ads_batch_fn.add_event_source(
            lambda_events.SqsEventSource(
                supply_tags_queue,
                batch_size=1,  # Processa 1 mensagem por vez
                max_batching_window=Duration.seconds(5),
            )
        )

        # ---------------------------------------------------------------
        # 6. Permissões IAM
        # ---------------------------------------------------------------
        
        # Pipeline_Ads permissions
        springserve_secret.grant_read(pipeline_ads_fn)
        kb_config_bucket.grant_put(pipeline_ads_fn)
        configs_table.grant_write_data(pipeline_ads_fn)
        supply_tags_queue.grant_send_messages(pipeline_ads_fn)
        
        pipeline_ads_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "mediatailor:ListPlaybackConfigurations",
                "mediatailor:GetPlaybackConfiguration"
            ],
            resources=["*"],
        ))

        # Pipeline_Ads_Batch permissions
        springserve_secret.grant_read(pipeline_ads_batch_fn)
        kb_config_bucket.grant_put(pipeline_ads_batch_fn)
        configs_table.grant_write_data(pipeline_ads_batch_fn)
        supply_tags_queue.grant_consume_messages(pipeline_ads_batch_fn)

        # ---------------------------------------------------------------
        # 7. EventBridge Schedule
        # ---------------------------------------------------------------
        ads_schedule = events.Rule(
            self, "PipelineAdsSchedule",
            schedule=events.Schedule.rate(Duration.hours(6)),
            description="Executa Pipeline_Ads a cada 6 horas",
        )
        ads_schedule.add_target(targets.LambdaFunction(pipeline_ads_fn))

        # ---------------------------------------------------------------
        # 8. Outputs
        # ---------------------------------------------------------------
        self.pipeline_ads_function = pipeline_ads_fn
        self.pipeline_ads_batch_function = pipeline_ads_batch_fn
        self.supply_tags_queue = supply_tags_queue
        self.springserve_secret = springserve_secret