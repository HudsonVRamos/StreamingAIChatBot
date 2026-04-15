"""CDK Stack for Lambda_Exportadora and Action_Group_Export.

Creates the Lambda_Exportadora with IAM permissions for:
- s3:GetObject / s3:ListBucket on KB_CONFIG and KB_LOGS buckets
- s3:PutObject / s3:GetObject on S3_Exports bucket

Requirements: 14.1, 14.5
"""

from aws_cdk import (
    Duration,
    Stack,
    aws_lambda as _lambda,
    aws_s3 as s3,
)
from constructs import Construct


class ExportadoraStack(Stack):
    """Stack for Lambda_Exportadora and its IAM role."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        kb_config_bucket: s3.IBucket,
        kb_logs_bucket: s3.IBucket,
        exports_bucket: s3.IBucket,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Lambda_Exportadora ---
        self.exportadora_fn = _lambda.Function(
            self,
            "ExportadoraFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/exportadora"),
            timeout=Duration.seconds(60),
            environment={
                "KB_CONFIG_BUCKET": kb_config_bucket.bucket_name,
                "KB_CONFIG_PREFIX": "kb-config/",
                "KB_LOGS_BUCKET": kb_logs_bucket.bucket_name,
                "KB_LOGS_PREFIX": "kb-logs/",
                "EXPORTS_BUCKET": exports_bucket.bucket_name,
                "EXPORTS_PREFIX": "exports/",
                "PRESIGNED_URL_EXPIRY": "3600",
            },
        )

        # --- IAM: s3:GetObject / s3:ListBucket on KB_CONFIG bucket ---
        kb_config_bucket.grant_read(self.exportadora_fn)

        # --- IAM: s3:GetObject / s3:ListBucket on KB_LOGS bucket ---
        kb_logs_bucket.grant_read(self.exportadora_fn)

        # --- IAM: s3:PutObject / s3:GetObject on S3_Exports bucket ---
        exports_bucket.grant_read_write(self.exportadora_fn)
