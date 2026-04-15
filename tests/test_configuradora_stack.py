"""Unit tests for the ConfiguradoraStack."""

import aws_cdk as cdk
import aws_cdk.assertions as assertions

from stacks.s3_stack import S3Stack
from stacks.configuradora_stack import ConfiguradoraStack


def _get_template() -> assertions.Template:
    app = cdk.App()
    s3_stack = S3Stack(app, "TestS3Stack")
    stack = ConfiguradoraStack(
        app,
        "TestConfiguradoraStack",
        audit_bucket=s3_stack.audit_bucket,
        agent_id="test-agent-id",
    )
    return assertions.Template.from_stack(stack)


def test_creates_lambda_function():
    template = _get_template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.12",
            "Handler": "handler.handler",
            "Timeout": 30,
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
                        "AUDIT_PREFIX": "audit/",
                    }
                ),
            },
        },
    )


def test_lambda_has_medialive_permissions():
    template = _get_template()
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Action": [
                                    "medialive:CreateChannel",
                                    "medialive:UpdateChannel",
                                    "medialive:CreateInput",
                                    "medialive:UpdateInput",
                                    "medialive:DescribeChannel",
                                    "medialive:DescribeInput",
                                ],
                                "Effect": "Allow",
                                "Resource": "*",
                            }
                        )
                    ]
                ),
            },
        },
    )


def test_lambda_has_mediapackage_permissions():
    template = _get_template()
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Action": [
                                    "mediapackage:CreateChannel",
                                    "mediapackage:CreateOriginEndpoint",
                                    "mediapackage:UpdateOriginEndpoint",
                                    "mediapackage:DescribeChannel",
                                    "mediapackage:DescribeOriginEndpoint",
                                ],
                                "Effect": "Allow",
                                "Resource": "*",
                            }
                        )
                    ]
                ),
            },
        },
    )


def test_lambda_has_mediatailor_permissions():
    template = _get_template()
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Action": [
                                    "mediatailor:PutPlaybackConfiguration",
                                    "mediatailor:GetPlaybackConfiguration",
                                ],
                                "Effect": "Allow",
                                "Resource": "*",
                            }
                        )
                    ]
                ),
            },
        },
    )


def test_lambda_has_cloudfront_permissions():
    template = _get_template()
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Action": [
                                    "cloudfront:CreateDistribution",
                                    "cloudfront:UpdateDistribution",
                                    "cloudfront:GetDistribution",
                                    "cloudfront:GetDistributionConfig",
                                ],
                                "Effect": "Allow",
                                "Resource": "*",
                            }
                        )
                    ]
                ),
            },
        },
    )


def test_lambda_has_s3_put_permission():
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
                                    ["s3:PutObject"]
                                ),
                                "Effect": "Allow",
                            }
                        )
                    ]
                ),
            },
        },
    )


def test_stack_exposes_lambda_function():
    """Verify the stack exposes the Lambda function as a property."""
    app = cdk.App()
    s3_stack = S3Stack(app, "TestS3Stack")
    stack = ConfiguradoraStack(
        app,
        "TestConfiguradoraStack",
        audit_bucket=s3_stack.audit_bucket,
        agent_id="test-agent-id",
    )
    assert stack.configuradora_fn is not None
