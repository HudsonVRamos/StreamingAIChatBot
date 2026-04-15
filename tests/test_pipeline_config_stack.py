"""Unit tests for the PipelineConfigStack."""

import aws_cdk as cdk
import aws_cdk.assertions as assertions

from stacks.s3_stack import S3Stack
from stacks.pipeline_config_stack import PipelineConfigStack


def _get_template() -> assertions.Template:
    app = cdk.App()
    s3_stack = S3Stack(app, "TestS3Stack")
    stack = PipelineConfigStack(
        app,
        "TestPipelineConfigStack",
        kb_config_bucket=s3_stack.kb_config_bucket,
    )
    return assertions.Template.from_stack(stack)


def test_creates_lambda_function():
    template = _get_template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.12",
            "Handler": "handler.handler",
            "Timeout": 300,
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
                    }
                ),
            },
        },
    )


def test_lambda_has_media_service_permissions():
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
                                    "medialive:ListChannels",
                                    "medialive:DescribeChannel",
                                    "mediapackage:ListChannels",
                                    "mediapackage:ListOriginEndpoints",
                                    "mediatailor:ListPlaybackConfigurations",
                                    "mediatailor:GetPlaybackConfiguration",
                                    "cloudfront:ListDistributions",
                                    "cloudfront:GetDistribution",
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


def test_creates_eventbridge_rule():
    template = _get_template()
    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "ScheduleExpression": "rate(6 hours)",
            "State": "ENABLED",
        },
    )


def test_eventbridge_rule_targets_lambda():
    template = _get_template()
    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "Targets": assertions.Match.array_with(
                [
                    assertions.Match.object_like(
                        {
                            "Arn": assertions.Match.any_value(),
                        }
                    )
                ]
            ),
        },
    )


def test_stack_exposes_lambda_and_rule():
    """Verify the stack exposes the Lambda function and schedule rule."""
    app = cdk.App()
    s3_stack = S3Stack(app, "TestS3Stack")
    stack = PipelineConfigStack(
        app,
        "TestPipelineConfigStack",
        kb_config_bucket=s3_stack.kb_config_bucket,
    )
    assert stack.pipeline_config_fn is not None
    assert stack.schedule_rule is not None
