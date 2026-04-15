"""CDK Stack for the Pipeline de Ingestão de Configurações.

Creates a Lambda function triggered by EventBridge every 6 hours to extract
configurations from MediaLive, MediaPackage, MediaTailor and CloudFront,
normalize them and store in the KB_CONFIG S3 bucket.

Requirements: 5.1, 5.3
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


class PipelineConfigStack(Stack):
    """Stack for the Pipeline Config Lambda and EventBridge schedule."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        kb_config_bucket: s3.IBucket,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Lambda function ---
        self.pipeline_config_fn = _lambda.Function(
            self,
            "PipelineConfigFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/pipeline_config"),
            timeout=Duration.minutes(5),
            environment={
                "KB_CONFIG_BUCKET": kb_config_bucket.bucket_name,
                "KB_CONFIG_PREFIX": "kb-config/",
            },
        )

        # --- IAM: AWS media/CDN read permissions ---
        self.pipeline_config_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "medialive:ListChannels",
                    "medialive:DescribeChannel",
                    "mediapackage:ListChannels",
                    "mediapackage:ListOriginEndpoints",
                    "mediatailor:ListPlaybackConfigurations",
                    "mediatailor:GetPlaybackConfiguration",
                    "cloudfront:ListDistributions",
                    "cloudfront:GetDistribution",
                ],
                resources=["*"],
            )
        )

        # --- IAM: s3:PutObject on KB_CONFIG bucket ---
        kb_config_bucket.grant_put(self.pipeline_config_fn)

        # --- EventBridge rule: every 6 hours ---
        self.schedule_rule = events.Rule(
            self,
            "PipelineConfigSchedule",
            schedule=events.Schedule.rate(Duration.hours(6)),
            description="Triggers Pipeline Config Lambda every 6 hours",
        )
        self.schedule_rule.add_target(
            targets.LambdaFunction(self.pipeline_config_fn)
        )
