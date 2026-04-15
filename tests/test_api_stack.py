"""Unit tests for the ApiStack."""

import aws_cdk as cdk
import aws_cdk.assertions as assertions

from stacks.api_stack import ApiStack


def _get_template() -> assertions.Template:
    app = cdk.App()
    stack = ApiStack(app, "TestApiStack")
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
                "Variables": {
                    "AGENT_ID": "PLACEHOLDER_AGENT_ID",
                    "AGENT_ALIAS_ID": "PLACEHOLDER_AGENT_ALIAS_ID",
                },
            },
        },
    )


def test_lambda_has_bedrock_invoke_agent_policy():
    template = _get_template()
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Action": "bedrock:InvokeAgent",
                                "Effect": "Allow",
                                "Resource": "*",
                            }
                        )
                    ]
                ),
            },
        },
    )


def test_creates_rest_api():
    template = _get_template()
    template.has_resource_properties(
        "AWS::ApiGateway::RestApi",
        {
            "Name": "StreamingChatbotApi",
        },
    )


def test_api_has_post_chat_method():
    template = _get_template()
    template.has_resource_properties(
        "AWS::ApiGateway::Method",
        {
            "HttpMethod": "POST",
        },
    )


def test_api_has_cors_options_method():
    template = _get_template()
    template.has_resource_properties(
        "AWS::ApiGateway::Method",
        {
            "HttpMethod": "OPTIONS",
        },
    )


def test_api_has_chat_resource():
    template = _get_template()
    template.has_resource_properties(
        "AWS::ApiGateway::Resource",
        {
            "PathPart": "chat",
        },
    )


def test_api_url_output_exists():
    template = _get_template()
    outputs = template.find_outputs("*")
    assert any("ApiUrl" in key for key in outputs)


def test_stack_exposes_api_and_lambda():
    """Verify the stack exposes api and lambda as properties."""
    app = cdk.App()
    stack = ApiStack(app, "TestApiStack")
    assert stack.api is not None
    assert stack.orquestradora_fn is not None
