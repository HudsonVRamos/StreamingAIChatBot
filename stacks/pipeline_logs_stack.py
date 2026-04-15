"""CDK Stack for the Pipeline de Ingestão de Logs.

Creates a Lambda function triggered by EventBridge every 1 hour to collect
logs from CloudWatch, normalize them into Evento_Estruturado and store
in the KB_LOGS S3 bucket.

Requirements: 7.1, 7.4
"""

from aws_cdk import (
    Duration,
    Stack,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
)
from constructs import Construct


class PipelineLogsStack(Stack):
    """Stack for the Pipeline Logs Lambda and EventBridge schedule."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        kb_logs_bucket: s3.IBucket,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Lambda function ---
        self.pipeline_logs_fn = _lambda.Function(
            self,
            "PipelineLogsFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/pipeline_logs"),
            timeout=Duration.minutes(5),
            environment={
                "KB_LOGS_BUCKET": kb_logs_bucket.bucket_name,
                "KB_LOGS_PREFIX": "kb-logs/",
                "MEDIATAILOR_REGION": "us-east-1",
                "CLOUDFRONT_REGION": "us-east-1",
            },
        )

        # --- IAM: CloudWatch Metrics + resource listing permissions ---
        self.pipeline_logs_fn.add_to_role_policy(
            iam.PolicyStatement(
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
            )
        )

        # --- IAM: s3:PutObject on KB_LOGS bucket ---
        kb_logs_bucket.grant_put(self.pipeline_logs_fn)

        # --- EventBridge rule: every 1 hour ---
        self.schedule_rule = events.Rule(
            self,
            "PipelineLogsSchedule",
            schedule=events.Schedule.rate(Duration.hours(1)),
            description="Triggers Pipeline Logs Lambda every 1 hour",
        )
        self.schedule_rule.add_target(
            targets.LambdaFunction(self.pipeline_logs_fn)
        )
