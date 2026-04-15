"""CDK Stack for S3 buckets used by the Streaming Chatbot infrastructure."""

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_s3 as s3,
)
from constructs import Construct


class S3Stack(Stack):
    """Stack that creates all S3 buckets for the Streaming Chatbot."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # S3_KBConfig — dedicated bucket for KB_CONFIG data
        # Stores enriched configurations under kb-config/ prefix
        # Req 4.1, 10.1
        self.kb_config_bucket = s3.Bucket(
            self,
            "KBConfigBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # S3_KBLogs — dedicated bucket for KB_LOGS data
        # Stores structured events under kb-logs/ prefix
        # Req 6.1, 10.2
        self.kb_logs_bucket = s3.Bucket(
            self,
            "KBLogsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # S3_Audit — audit log bucket with versioning and 365-day retention
        # Req 13.5
        self.audit_bucket = s3.Bucket(
            self,
            "AuditBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=Duration.days(365),
                ),
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # S3_Exports — temporary bucket for exported files with 24h lifecycle
        # Req 14.5
        self.exports_bucket = s3.Bucket(
            self,
            "ExportsBucket",
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

        # S3_Frontend — static website hosting for the chat frontend
        # Block Public Access enabled; access only via CloudFront OAC
        # Req 1.6
        self.frontend_bucket = s3.Bucket(
            self,
            "FrontendBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            website_index_document="index.html",
            website_error_document="index.html",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
