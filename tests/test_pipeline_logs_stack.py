"""Unit tests for the PipelineLogsStack."""

import aws_cdk as cdk
import aws_cdk.assertions as assertions

from stacks.s3_stack import S3Stack
from stacks.pipeline_logs_stack import PipelineLogsStack


def _get_template() -> assertions.Template:
    app = cdk.App()
    s3_stack = S3Stack(app, "TestS3Stack")
    stack = PipelineLogsStack(
        app,
        "TestPipelineLogsStack",
        kb_logs_bucket=s3_stack.kb_logs_bucket,
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
                        "KB_LOGS_PREFIX": "kb-logs/",
                    }
                ),
            },
        },
    )


def test_lambda_has_mediatailor_region_env_var():
    """Verify MEDIATAILOR_REGION environment variable is set."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": assertions.Match.object_like(
                    {
                        "MEDIATAILOR_REGION": "us-east-1",
                    }
                ),
            },
        },
    )


def test_lambda_has_cloudfront_region_env_var():
    """Verify CLOUDFRONT_REGION environment variable is set."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": assertions.Match.object_like(
                    {
                        "CLOUDFRONT_REGION": "us-east-1",
                    }
                ),
            },
        },
    )


def test_lambda_has_cloudwatch_metrics_permissions():
    """Verify CloudWatch Metrics permissions are present."""
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
                                    [
                                        "cloudwatch:GetMetricData",
                                        "cloudwatch:ListMetrics",
                                    ]
                                ),
                                "Effect": "Allow",
                                "Resource": "*",
                            }
                        )
                    ]
                ),
            },
        },
    )


def test_lambda_has_medialive_permissions():
    """Verify MediaLive listing permissions are present."""
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
                                    [
                                        "medialive:ListChannels",
                                    ]
                                ),
                                "Effect": "Allow",
                                "Resource": "*",
                            }
                        )
                    ]
                ),
            },
        },
    )


def test_lambda_has_mediapackagev2_permissions():
    """Verify MediaPackage V2 listing permissions are present."""
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
                                    [
                                        "mediapackagev2:ListChannelGroups",
                                        "mediapackagev2:ListChannels",
                                        "mediapackagev2:ListOriginEndpoints",
                                    ]
                                ),
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
    """Verify MediaTailor listing permissions are present."""
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
                                    [
                                        "mediatailor:ListPlaybackConfigurations",
                                    ]
                                ),
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
    """Verify CloudFront listing permissions are present."""
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
                                    [
                                        "cloudfront:ListDistributions",
                                    ]
                                ),
                                "Effect": "Allow",
                                "Resource": "*",
                            }
                        )
                    ]
                ),
            },
        },
    )


def test_lambda_does_not_have_logs_star_permission():
    """Verify that the old logs:* permission was removed."""
    import json

    template = _get_template()
    # Get all IAM policies from the template
    policies = template.find_resources("AWS::IAM::Policy")
    for _logical_id, resource in policies.items():
        statements = resource["Properties"]["PolicyDocument"]["Statement"]
        for stmt in statements:
            action = stmt.get("Action", [])
            if isinstance(action, str):
                action = [action]
            assert "logs:*" not in action, (
                "logs:* permission should have been removed"
            )
            assert "logs:FilterLogEvents" not in action, (
                "logs:FilterLogEvents permission should have been removed"
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
            "ScheduleExpression": "rate(1 hour)",
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
    stack = PipelineLogsStack(
        app,
        "TestPipelineLogsStack",
        kb_logs_bucket=s3_stack.kb_logs_bucket,
    )
    assert stack.pipeline_logs_fn is not None
    assert stack.schedule_rule is not None
