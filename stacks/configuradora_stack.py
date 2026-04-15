"""CDK Stack for Lambda_Configuradora and Action_Group_Config.

Creates the Lambda_Configuradora with IAM permissions for MediaLive,
MediaPackage, MediaTailor, CloudFront CRUD operations and s3:PutObject
on the S3_Audit bucket.

Requirements: 12.4, 12.5, 13.5
"""

from aws_cdk import (
    Duration,
    Stack,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
)
from constructs import Construct


class ConfiguradoraStack(Stack):
    """Stack for Lambda_Configuradora and its IAM role."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        audit_bucket: s3.IBucket,
        agent_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Lambda_Configuradora ---
        self.configuradora_fn = _lambda.Function(
            self,
            "ConfiguradoraFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/configuradora"),
            timeout=Duration.seconds(30),
            environment={
                "AUDIT_BUCKET": audit_bucket.bucket_name,
                "AUDIT_PREFIX": "audit/",
            },
        )

        # --- IAM: MediaLive CRUD operations ---
        self.configuradora_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "medialive:CreateChannel",
                    "medialive:UpdateChannel",
                    "medialive:CreateInput",
                    "medialive:UpdateInput",
                    "medialive:DescribeChannel",
                    "medialive:DescribeInput",
                ],
                resources=["*"],
            )
        )

        # --- IAM: MediaPackage CRUD operations ---
        self.configuradora_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "mediapackage:CreateChannel",
                    "mediapackage:CreateOriginEndpoint",
                    "mediapackage:UpdateOriginEndpoint",
                    "mediapackage:DescribeChannel",
                    "mediapackage:DescribeOriginEndpoint",
                ],
                resources=["*"],
            )
        )

        # --- IAM: MediaTailor operations ---
        self.configuradora_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "mediatailor:PutPlaybackConfiguration",
                    "mediatailor:GetPlaybackConfiguration",
                ],
                resources=["*"],
            )
        )

        # --- IAM: CloudFront CRUD operations ---
        self.configuradora_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudfront:CreateDistribution",
                    "cloudfront:UpdateDistribution",
                    "cloudfront:GetDistribution",
                    "cloudfront:GetDistributionConfig",
                ],
                resources=["*"],
            )
        )

        # --- IAM: CloudWatch Metrics read permissions (for /consultarMetricas) ---
        self.configuradora_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:GetMetricData",
                    "cloudwatch:ListMetrics",
                ],
                resources=["*"],
            )
        )

        # --- IAM: s3:PutObject on audit bucket ---
        audit_bucket.grant_put(self.configuradora_fn)
