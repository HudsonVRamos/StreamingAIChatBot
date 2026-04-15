"""Integration tests for the complete Streaming Chatbot CDK infrastructure.

from __future__ import annotations is used for Python 3.9 compatibility.

Tests verify that:
- All CDK stacks synthesize together without errors
- Cross-stack references are properly wired (bucket names in env vars, etc.)
- CORS is configured on the API Gateway
- CloudFront distribution is created with correct settings

Requisitos: 2.1, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 8.1, 8.5, 8.6, 9.5, 9.6
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import aws_cdk.assertions as assertions

from stacks.main_stack import MainStack


def _get_template() -> assertions.Template:
    """Synthesize the MainStack and return its template."""
    app = cdk.App()
    stack = MainStack(app, "IntegrationTestStack")
    return assertions.Template.from_stack(stack)


# ===================================================================
# Full synthesis test
# ===================================================================


def test_main_stack_synthesizes_without_errors():
    """All resources synthesize together in a single stack.

    Requisitos: 2.1, 3.1
    """
    template = _get_template()
    # Verify key resource types exist
    template.resource_count_is(
        "AWS::CloudFront::Distribution", 1
    )
    template.resource_count_is("AWS::ApiGateway::RestApi", 1)
    template.resource_count_is("AWS::Bedrock::KnowledgeBase", 2)
    template.resource_count_is("AWS::Bedrock::Agent", 1)


# ===================================================================
# Cross-stack references: Lambda env vars wired to bucket names
# ===================================================================


def test_orquestradora_env_vars_reference_agent():
    """Lambda_Orquestradora env vars reference the Bedrock Agent.

    Requisitos: 2.1, 3.1
    """
    template = _get_template()
    resources = template.to_json()["Resources"]

    orq_fn = _find_lambda_by_handler(resources, "orquestradora")
    assert orq_fn is not None, "OrquestradoraFunction not found"

    env_vars = (
        orq_fn["Properties"]["Environment"]["Variables"]
    )
    # AGENT_ID and AGENT_ALIAS_ID should reference the agent
    assert "AGENT_ID" in env_vars
    assert "AGENT_ALIAS_ID" in env_vars
    # They should be Ref or GetAtt (not placeholder strings)
    agent_id_val = env_vars["AGENT_ID"]
    assert not isinstance(agent_id_val, str) or (
        "PLACEHOLDER" not in agent_id_val
    ), "AGENT_ID should reference actual agent, not placeholder"


def test_exportadora_env_vars_reference_buckets():
    """Lambda_Exportadora env vars reference the correct S3 buckets.

    Requisitos: 3.6
    """
    template = _get_template()
    resources = template.to_json()["Resources"]

    exp_fn = _find_lambda_by_handler(resources, "exportadora")
    assert exp_fn is not None, "ExportadoraFunction not found"

    env_vars = (
        exp_fn["Properties"]["Environment"]["Variables"]
    )
    assert "KB_CONFIG_BUCKET" in env_vars
    assert "KB_LOGS_BUCKET" in env_vars
    assert "EXPORTS_BUCKET" in env_vars
    assert "PRESIGNED_URL_EXPIRY" in env_vars

    # Bucket names should be Ref (dynamic), not hardcoded
    for key in ["KB_CONFIG_BUCKET", "KB_LOGS_BUCKET", "EXPORTS_BUCKET"]:
        val = env_vars[key]
        assert not isinstance(val, str) or val.startswith("{"), (
            f"{key} should reference a bucket resource, got: {val}"
        )


def test_configuradora_env_vars_reference_audit_bucket():
    """Lambda_Configuradora env vars reference the audit bucket.

    Requisitos: 3.5
    """
    template = _get_template()
    resources = template.to_json()["Resources"]

    cfg_fn = _find_lambda_by_handler(resources, "configuradora")
    assert cfg_fn is not None, "ConfiguradoraFunction not found"

    env_vars = (
        cfg_fn["Properties"]["Environment"]["Variables"]
    )
    assert "AUDIT_BUCKET" in env_vars
    assert "AUDIT_PREFIX" in env_vars


def test_pipeline_config_env_vars_reference_kb_config_bucket():
    """Pipeline Config Lambda env vars reference KB_CONFIG bucket.

    Requisitos: 8.1
    """
    template = _get_template()
    resources = template.to_json()["Resources"]

    fn = _find_lambda_by_handler(resources, "pipeline_config")
    assert fn is not None, "PipelineConfigFunction not found"

    env_vars = fn["Properties"]["Environment"]["Variables"]
    assert "KB_CONFIG_BUCKET" in env_vars
    assert "KB_CONFIG_PREFIX" in env_vars


def test_pipeline_logs_env_vars_reference_kb_logs_bucket():
    """Pipeline Logs Lambda env vars reference KB_LOGS bucket.

    Requisitos: 9.5
    """
    template = _get_template()
    resources = template.to_json()["Resources"]

    fn = _find_lambda_by_handler(resources, "pipeline_logs")
    assert fn is not None, "PipelineLogsFunction not found"

    env_vars = fn["Properties"]["Environment"]["Variables"]
    assert "KB_LOGS_BUCKET" in env_vars
    assert "KB_LOGS_PREFIX" in env_vars


# ===================================================================
# CORS configuration on API Gateway
# ===================================================================


def test_api_gateway_cors_configured():
    """API Gateway has CORS configured for CloudFront domain.

    Requisitos: 2.1
    """
    template = _get_template()
    # CORS preflight creates an OPTIONS method on the chat resource
    template.has_resource_properties(
        "AWS::ApiGateway::Method",
        {
            "HttpMethod": "OPTIONS",
        },
    )


def test_api_gateway_has_post_chat_method():
    """API Gateway has POST method on /chat resource.

    Requisitos: 2.1
    """
    template = _get_template()
    template.has_resource_properties(
        "AWS::ApiGateway::Method",
        {
            "HttpMethod": "POST",
        },
    )


# ===================================================================
# CloudFront Frontend distribution
# ===================================================================


def test_cloudfront_distribution_redirect_to_https():
    """CloudFront distribution uses redirect-to-https.

    Requisitos: 8.5
    """
    template = _get_template()
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": {
                "DefaultCacheBehavior": {
                    "ViewerProtocolPolicy": "redirect-to-https",
                },
            },
        },
    )


def test_cloudfront_distribution_default_root_object():
    """CloudFront distribution has index.html as default root object.

    Requisitos: 8.6
    """
    template = _get_template()
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": {
                "DefaultRootObject": "index.html",
            },
        },
    )


def test_cloudfront_distribution_spa_error_responses():
    """CloudFront distribution has SPA error responses (403/404 → index.html).

    Requisitos: 8.6
    """
    template = _get_template()
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": {
                "CustomErrorResponses": [
                    {
                        "ErrorCode": 403,
                        "ResponseCode": 200,
                        "ResponsePagePath": "/index.html",
                    },
                    {
                        "ErrorCode": 404,
                        "ResponseCode": 200,
                        "ResponsePagePath": "/index.html",
                    },
                ],
            },
        },
    )


def test_cloudfront_uses_origin_access_control():
    """CloudFront distribution uses OAC for S3 origin.

    Requisitos: 8.5
    """
    template = _get_template()
    template.resource_count_is(
        "AWS::CloudFront::OriginAccessControl", 1
    )
    template.has_resource_properties(
        "AWS::CloudFront::OriginAccessControl",
        {
            "OriginAccessControlConfig": {
                "OriginAccessControlOriginType": "s3",
                "SigningBehavior": "always",
                "SigningProtocol": "sigv4",
            },
        },
    )


def test_cloudfront_caching_optimized_policy():
    """CloudFront uses Managed-CachingOptimized cache policy.

    Requisitos: 8.6
    """
    template = _get_template()
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": {
                "DefaultCacheBehavior": {
                    "CachePolicyId": (
                        "658327ea-f89d-4fab-a63d-7e88639e58f6"
                    ),
                },
            },
        },
    )


# ===================================================================
# Bedrock Agent and Knowledge Bases wiring
# ===================================================================


def test_bedrock_agent_has_two_knowledge_bases():
    """Bedrock Agent is associated with KB_CONFIG and KB_LOGS.

    Requisitos: 3.1, 3.2, 3.3
    """
    template = _get_template()
    template.has_resource_properties(
        "AWS::Bedrock::Agent",
        {
            "KnowledgeBases": assertions.Match.array_with([
                assertions.Match.object_like({
                    "KnowledgeBaseState": "ENABLED",
                }),
                assertions.Match.object_like({
                    "KnowledgeBaseState": "ENABLED",
                }),
            ]),
        },
    )


def test_bedrock_agent_has_instructions():
    """Bedrock Agent has instructions configured.

    Requisitos: 3.1, 3.4
    """
    template = _get_template()
    template.has_resource_properties(
        "AWS::Bedrock::Agent",
        {
            "Instruction": assertions.Match.string_like_regexp(
                ".*português brasileiro.*"
            ),
        },
    )


def test_knowledge_bases_have_s3_data_sources():
    """Both KBs have S3 data sources with correct prefixes.

    Requisitos: 3.2, 3.3
    """
    template = _get_template()
    resources = template.to_json()["Resources"]

    ds_resources = {
        k: v for k, v in resources.items()
        if v["Type"] == "AWS::Bedrock::DataSource"
    }
    assert len(ds_resources) == 2, (
        f"Expected 2 data sources, found {len(ds_resources)}"
    )

    prefixes = set()
    for ds in ds_resources.values():
        props = ds["Properties"]["DataSourceConfiguration"]
        s3_config = props["S3Configuration"]
        for prefix in s3_config.get("InclusionPrefixes", []):
            prefixes.add(prefix)

    assert "kb-config/" in prefixes, "Missing kb-config/ prefix"
    assert "kb-logs/" in prefixes, "Missing kb-logs/ prefix"


# ===================================================================
# S3 Buckets
# ===================================================================


def test_all_s3_buckets_created():
    """All required S3 buckets are created in the stack.

    Requisitos: 8.1, 8.5
    """
    template = _get_template()
    # 5 buckets + custom resource provider bucket (auto_delete_objects)
    resources = template.to_json()["Resources"]
    bucket_count = sum(
        1 for v in resources.values()
        if v["Type"] == "AWS::S3::Bucket"
    )
    assert bucket_count >= 5, (
        f"Expected at least 5 S3 buckets, found {bucket_count}"
    )


def test_audit_bucket_has_versioning():
    """Audit bucket has versioning enabled.

    Requisitos: 9.6
    """
    template = _get_template()
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "VersioningConfiguration": {
                "Status": "Enabled",
            },
        },
    )


# ===================================================================
# EventBridge schedules
# ===================================================================


def test_eventbridge_schedules_created():
    """EventBridge rules for pipeline schedules are created.

    Requisitos: 8.1, 9.5
    """
    template = _get_template()
    resources = template.to_json()["Resources"]
    rule_count = sum(
        1 for v in resources.values()
        if v["Type"] == "AWS::Events::Rule"
    )
    assert rule_count >= 2, (
        f"Expected at least 2 EventBridge rules, found {rule_count}"
    )


# ===================================================================
# Lambda function count
# ===================================================================


def test_all_lambda_functions_created():
    """All required Lambda functions are created.

    Requisitos: 2.1, 3.1
    """
    template = _get_template()
    resources = template.to_json()["Resources"]
    fn_count = sum(
        1 for v in resources.values()
        if v["Type"] == "AWS::Lambda::Function"
    )
    # orquestradora, configuradora, exportadora, pipeline_config,
    # pipeline_logs = 5 + custom resource lambdas
    assert fn_count >= 5, (
        f"Expected at least 5 Lambda functions, found {fn_count}"
    )


# ===================================================================
# Outputs
# ===================================================================


def test_stack_outputs():
    """Stack has required outputs (ApiUrl, FrontendUrl, AgentId)."""
    template = _get_template()
    outputs = template.to_json().get("Outputs", {})
    output_keys = set(outputs.keys())

    for expected in ["ApiUrl", "FrontendUrl", "AgentId"]:
        matches = [
            k for k in output_keys
            if expected.lower() in k.lower()
        ]
        assert len(matches) > 0, (
            f"Missing output containing '{expected}'"
        )


# ===================================================================
# Helper
# ===================================================================


def _find_lambda_by_handler(
    resources: dict, handler_path: str
) -> dict | None:
    """Find a Lambda function resource by handler code path."""
    for resource in resources.values():
        if resource["Type"] != "AWS::Lambda::Function":
            continue
        props = resource.get("Properties", {})
        code = props.get("Code", {})
        s3_key = code.get("S3Key", "")
        # CDK asset keys are hashes; check handler instead
        handler = props.get("Handler", "")
        if handler == "handler.handler":
            env = props.get("Environment", {}).get("Variables", {})
            # Identify by env vars
            if handler_path == "orquestradora":
                if "AGENT_ID" in env:
                    return resource
            elif handler_path == "configuradora":
                if "AUDIT_BUCKET" in env:
                    return resource
            elif handler_path == "exportadora":
                if "EXPORTS_BUCKET" in env:
                    return resource
            elif handler_path == "pipeline_config":
                if (
                    "KB_CONFIG_BUCKET" in env
                    and "KB_CONFIG_PREFIX" in env
                    and "KB_LOGS_BUCKET" not in env
                ):
                    return resource
            elif handler_path == "pipeline_logs":
                if (
                    "KB_LOGS_BUCKET" in env
                    and "KB_LOGS_PREFIX" in env
                    and "KB_CONFIG_BUCKET" not in env
                ):
                    return resource
    return None
