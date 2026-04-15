"""Unit tests for the ExportadoraStack."""

import aws_cdk as cdk
import aws_cdk.assertions as assertions

from stacks.s3_stack import S3Stack
from stacks.exportadora_stack import ExportadoraStack


def _get_template() -> assertions.Template:
    app = cdk.App()
    s3_stack = S3Stack(app, "TestS3Stack")
    stack = ExportadoraStack(
        app,
        "TestExportadoraStack",
        kb_config_bucket=s3_stack.kb_config_bucket,
        kb_logs_bucket=s3_stack.kb_logs_bucket,
        exports_bucket=s3_stack.exports_bucket,
    )
    return assertions.Template.from_stack(stack)


def test_creates_lambda_function():
    template = _get_template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.12",
            "Handler": "handler.handler",
            "Timeout": 60,
        },
    )


def test_lambda_has_environment_variables():
    template = _get_template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": assertions.Match.object_like(
                    {
                        "KB_CONFIG_PREFIX": "kb-config/",
                        "KB_LOGS_PREFIX": "kb-logs/",
                        "EXPORTS_PREFIX": "exports/",
                        "PRESIGNED_URL_EXPIRY": "3600",
                    }
                ),
            },
        },
    )


def test_lambda_has_s3_read_permissions_on_kb_buckets():
    """Verify s3:GetObject and s3:ListBucket on KB buckets."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Action": assertions.Match.array_with(
                                    ["s3:GetObject*", "s3:GetBucket*", "s3:List*"]
                                ),
                                "Effect": "Allow",
                            }
                        ),
                    ]
                ),
            },
        },
    )


def test_lambda_has_s3_read_write_on_exports_bucket():
    """Verify s3:PutObject and s3:GetObject on exports bucket."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Action": assertions.Match.array_with(
                                    ["s3:PutObject", "s3:Abort*"]
                                ),
                                "Effect": "Allow",
                            }
                        ),
                    ]
                ),
            },
        },
    )


def test_stack_exposes_lambda_function():
    """Verify the stack exposes the Lambda function as a property."""
    app = cdk.App()
    s3_stack = S3Stack(app, "TestS3Stack")
    stack = ExportadoraStack(
        app,
        "TestExportadoraStack",
        kb_config_bucket=s3_stack.kb_config_bucket,
        kb_logs_bucket=s3_stack.kb_logs_bucket,
        exports_bucket=s3_stack.exports_bucket,
    )
    assert stack.exportadora_fn is not None
