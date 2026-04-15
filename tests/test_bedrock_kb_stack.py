"""Unit tests for the BedrockKbStack."""

import aws_cdk as cdk
import aws_cdk.assertions as assertions

from stacks.s3_stack import S3Stack
from stacks.bedrock_kb_stack import BedrockKbStack


def _get_template() -> assertions.Template:
    app = cdk.App()
    s3_stack = S3Stack(app, "TestS3Stack")
    kb_stack = BedrockKbStack(
        app,
        "TestBedrockKbStack",
        kb_config_bucket=s3_stack.kb_config_bucket,
        kb_logs_bucket=s3_stack.kb_logs_bucket,
    )
    return assertions.Template.from_stack(kb_stack)


def test_creates_two_knowledge_bases():
    """Should create exactly two Bedrock Knowledge Bases."""
    template = _get_template()
    template.resource_count_is(
        "AWS::Bedrock::KnowledgeBase", 2
    )


def test_creates_two_data_sources():
    """Should create exactly two Bedrock Data Sources."""
    template = _get_template()
    template.resource_count_is(
        "AWS::Bedrock::DataSource", 2
    )


def test_kb_config_uses_vector_type():
    """KB_CONFIG should use VECTOR knowledge base configuration."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase",
        {
            "Name": "KB_CONFIG",
            "KnowledgeBaseConfiguration": {
                "Type": "VECTOR",
                "VectorKnowledgeBaseConfiguration": {
                    "EmbeddingModelArn": assertions.Match.any_value(),
                },
            },
        },
    )


def test_kb_logs_uses_vector_type():
    """KB_LOGS should use VECTOR knowledge base configuration."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase",
        {
            "Name": "KB_LOGS",
            "KnowledgeBaseConfiguration": {
                "Type": "VECTOR",
                "VectorKnowledgeBaseConfiguration": {
                    "EmbeddingModelArn": assertions.Match.any_value(),
                },
            },
        },
    )


def test_embedding_model_arn_contains_titan_v2():
    """Both KBs should reference amazon.titan-embed-text-v2:0 in the ARN."""
    template = _get_template()
    kbs = template.find_resources("AWS::Bedrock::KnowledgeBase")
    for _id, resource in kbs.items():
        arn = resource["Properties"]["KnowledgeBaseConfiguration"][
            "VectorKnowledgeBaseConfiguration"
        ]["EmbeddingModelArn"]
        # ARN is a Fn::Join intrinsic containing the model name
        assert "Fn::Join" in arn
        parts = arn["Fn::Join"][1]
        joined = "".join(str(p) for p in parts)
        assert "amazon.titan-embed-text-v2:0" in joined


def test_kb_config_data_source_has_s3_config_prefix():
    """KB_CONFIG data source should point to kb-config/ prefix."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::Bedrock::DataSource",
        {
            "Name": "kb-config-s3-source",
            "DataSourceConfiguration": {
                "Type": "S3",
                "S3Configuration": {
                    "InclusionPrefixes": ["kb-config/"],
                },
            },
        },
    )


def test_kb_logs_data_source_has_s3_logs_prefix():
    """KB_LOGS data source should point to kb-logs/ prefix."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::Bedrock::DataSource",
        {
            "Name": "kb-logs-s3-source",
            "DataSourceConfiguration": {
                "Type": "S3",
                "S3Configuration": {
                    "InclusionPrefixes": ["kb-logs/"],
                },
            },
        },
    )


def test_creates_two_iam_roles_for_bedrock():
    """Should create IAM roles assumed by bedrock.amazonaws.com."""
    template = _get_template()
    roles = template.find_resources(
        "AWS::IAM::Role",
        {
            "Properties": {
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
        },
    )
    assert len(roles) == 2


def test_iam_roles_have_bedrock_invoke_model_permission():
    """IAM roles should have bedrock:InvokeModel permission."""
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


def test_stack_exposes_kb_ids():
    """Stack should expose kb_config_id and kb_logs_id properties."""
    app = cdk.App()
    s3_stack = S3Stack(app, "TestS3Stack")
    kb_stack = BedrockKbStack(
        app,
        "TestBedrockKbStack",
        kb_config_bucket=s3_stack.kb_config_bucket,
        kb_logs_bucket=s3_stack.kb_logs_bucket,
    )
    assert kb_stack.kb_config_id is not None
    assert kb_stack.kb_logs_id is not None


def test_kb_config_has_description():
    """KB_CONFIG should have a meaningful description."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase",
        {
            "Name": "KB_CONFIG",
            "Description": assertions.Match.string_like_regexp(
                ".*configura.*"
            ),
        },
    )


def test_kb_logs_has_description():
    """KB_LOGS should have a meaningful description."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase",
        {
            "Name": "KB_LOGS",
            "Description": assertions.Match.string_like_regexp(
                ".*logs.*"
            ),
        },
    )
