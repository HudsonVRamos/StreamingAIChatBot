"""Unit tests for the BedrockAgentStack."""

import aws_cdk as cdk
import aws_cdk.assertions as assertions

from stacks.s3_stack import S3Stack
from stacks.bedrock_kb_stack import BedrockKbStack
from stacks.bedrock_agent_stack import BedrockAgentStack


def _get_template() -> assertions.Template:
    app = cdk.App()
    s3_stack = S3Stack(app, "TestS3Stack")
    kb_stack = BedrockKbStack(
        app,
        "TestBedrockKbStack",
        kb_config_bucket=s3_stack.kb_config_bucket,
        kb_logs_bucket=s3_stack.kb_logs_bucket,
    )
    agent_stack = BedrockAgentStack(
        app,
        "TestBedrockAgentStack",
        kb_config_id=kb_stack.kb_config_id,
        kb_logs_id=kb_stack.kb_logs_id,
    )
    return assertions.Template.from_stack(agent_stack)


def _get_agent_stack() -> BedrockAgentStack:
    app = cdk.App()
    s3_stack = S3Stack(app, "TestS3Stack")
    kb_stack = BedrockKbStack(
        app,
        "TestBedrockKbStack",
        kb_config_bucket=s3_stack.kb_config_bucket,
        kb_logs_bucket=s3_stack.kb_logs_bucket,
    )
    return BedrockAgentStack(
        app,
        "TestBedrockAgentStack",
        kb_config_id=kb_stack.kb_config_id,
        kb_logs_id=kb_stack.kb_logs_id,
    )


def test_creates_one_bedrock_agent():
    """Should create exactly one Bedrock Agent."""
    template = _get_template()
    template.resource_count_is("AWS::Bedrock::Agent", 1)


def test_creates_one_agent_alias():
    """Should create exactly one Bedrock Agent Alias."""
    template = _get_template()
    template.resource_count_is("AWS::Bedrock::AgentAlias", 1)


def test_agent_uses_claude_3_sonnet():
    """Agent should use Claude 3 Sonnet as foundation model."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::Bedrock::Agent",
        {
            "FoundationModel": "anthropic.claude-3-sonnet-20240229-v1:0",
        },
    )


def test_agent_instructions_in_portuguese():
    """Agent instructions should be in Portuguese Brazilian."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::Bedrock::Agent",
        {
            "Instruction": assertions.Match.string_like_regexp(
                ".*português brasileiro.*"
            ),
        },
    )


def test_agent_instructions_contain_intent_classification():
    """Agent instructions should contain all five intent types."""
    template = _get_template()
    for intent in ["configuração", "configuração_acao", "logs", "ambos", "exportação"]:
        template.has_resource_properties(
            "AWS::Bedrock::Agent",
            {
                "Instruction": assertions.Match.string_like_regexp(
                    f".*{intent}.*"
                ),
            },
        )


def test_agent_has_two_knowledge_bases():
    """Agent should have two knowledge bases associated."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::Bedrock::Agent",
        {
            "KnowledgeBases": assertions.Match.array_with(
                [
                    assertions.Match.object_like(
                        {"KnowledgeBaseState": "ENABLED"}
                    ),
                    assertions.Match.object_like(
                        {"KnowledgeBaseState": "ENABLED"}
                    ),
                ]
            ),
        },
    )


def test_agent_kb_config_description():
    """KB_CONFIG association should have a meaningful description."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::Bedrock::Agent",
        {
            "KnowledgeBases": assertions.Match.array_with(
                [
                    assertions.Match.object_like(
                        {
                            "Description": assertions.Match.string_like_regexp(
                                ".*configura.*"
                            ),
                        }
                    ),
                ]
            ),
        },
    )


def test_agent_kb_logs_description():
    """KB_LOGS association should have a meaningful description."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::Bedrock::Agent",
        {
            "KnowledgeBases": assertions.Match.array_with(
                [
                    assertions.Match.object_like(
                        {
                            "Description": assertions.Match.string_like_regexp(
                                ".*erros.*falhas.*"
                            ),
                        }
                    ),
                ]
            ),
        },
    )


def test_agent_iam_role_assumed_by_bedrock():
    """Agent IAM role should be assumed by bedrock.amazonaws.com."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Principal": {
                                    "Service": "bedrock.amazonaws.com"
                                },
                            }
                        )
                    ]
                ),
            },
        },
    )


def test_agent_iam_role_has_invoke_model_permission():
    """Agent IAM role should have bedrock:InvokeModel permission."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Action": "bedrock:InvokeModel",
                                "Effect": "Allow",
                            }
                        )
                    ]
                ),
            },
        },
    )


def test_agent_alias_name():
    """Agent alias should have the expected name."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::Bedrock::AgentAlias",
        {
            "AgentAliasName": "live",
        },
    )


def test_stack_exposes_agent_id():
    """Stack should expose agent_id property."""
    stack = _get_agent_stack()
    assert stack.agent_id is not None


def test_stack_exposes_agent_alias_id():
    """Stack should expose agent_alias_id property."""
    stack = _get_agent_stack()
    assert stack.agent_alias_id is not None
