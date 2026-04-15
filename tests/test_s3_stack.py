"""Unit tests for the S3Stack."""

import aws_cdk as cdk
import aws_cdk.assertions as assertions

from stacks.s3_stack import S3Stack


def _get_template() -> assertions.Template:
    app = cdk.App()
    stack = S3Stack(app, "TestS3Stack")
    return assertions.Template.from_stack(stack)


def test_creates_five_s3_buckets():
    template = _get_template()
    template.resource_count_is("AWS::S3::Bucket", 5)


def test_kb_config_bucket_has_block_public_access_and_sse():
    template = _get_template()
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            },
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        },
    )


def test_audit_bucket_has_versioning():
    template = _get_template()
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "VersioningConfiguration": {"Status": "Enabled"},
        },
    )


def test_audit_bucket_has_365_day_lifecycle():
    template = _get_template()
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "VersioningConfiguration": {"Status": "Enabled"},
            "LifecycleConfiguration": {
                "Rules": [
                    {
                        "ExpirationInDays": 365,
                        "Status": "Enabled",
                    }
                ]
            },
        },
    )


def test_exports_bucket_has_24h_lifecycle_for_exports_prefix():
    template = _get_template()
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "LifecycleConfiguration": {
                "Rules": [
                    {
                        "ExpirationInDays": 1,
                        "Prefix": "exports/",
                        "Status": "Enabled",
                    }
                ]
            },
        },
    )


def test_exports_bucket_has_no_versioning():
    """S3_Exports should not have versioning enabled."""
    template = _get_template()
    # The exports bucket should exist with lifecycle but without versioning
    # We verify there's a bucket with the exports lifecycle that does NOT have versioning
    resources = template.find_resources(
        "AWS::S3::Bucket",
        {
            "Properties": {
                "LifecycleConfiguration": {
                    "Rules": [
                        {
                            "Prefix": "exports/",
                        }
                    ]
                }
            },
        },
    )
    assert len(resources) == 1
    bucket_props = list(resources.values())[0]["Properties"]
    assert "VersioningConfiguration" not in bucket_props


def test_frontend_bucket_has_website_hosting():
    template = _get_template()
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "WebsiteConfiguration": {
                "IndexDocument": "index.html",
                "ErrorDocument": "index.html",
            },
        },
    )


def test_frontend_bucket_has_block_public_access():
    template = _get_template()
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "WebsiteConfiguration": {
                "IndexDocument": "index.html",
            },
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        },
    )


def test_stack_exposes_bucket_properties():
    """Verify the stack exposes all bucket references as properties."""
    app = cdk.App()
    stack = S3Stack(app, "TestS3Stack")
    assert stack.kb_config_bucket is not None
    assert stack.kb_logs_bucket is not None
    assert stack.audit_bucket is not None
    assert stack.exports_bucket is not None
    assert stack.frontend_bucket is not None
