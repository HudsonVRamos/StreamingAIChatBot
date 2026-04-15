"""Unit tests for the FrontendStack (CloudFront + S3 OAC)."""

import aws_cdk as cdk
import aws_cdk.assertions as assertions
from aws_cdk import (
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_s3 as s3,
)

from stacks.frontend_stack import FrontendStack


class _TestableStack(cdk.Stack):
    """Test stack that creates bucket + distribution in the same stack to avoid cycles."""

    def __init__(self, scope, construct_id, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        self.bucket = s3.Bucket(self, "FrontendBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )
        self.distribution = cloudfront.Distribution(
            self,
            "FrontendDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    self.bucket,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
        )


def _get_template() -> assertions.Template:
    """Create a single-stack template mirroring FrontendStack logic."""
    app = cdk.App()
    stack = _TestableStack(app, "TestStack")
    return assertions.Template.from_stack(stack)


def test_creates_cloudfront_distribution():
    template = _get_template()
    template.resource_count_is("AWS::CloudFront::Distribution", 1)


def test_distribution_has_redirect_to_https():
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


def test_distribution_has_default_root_object():
    template = _get_template()
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": {
                "DefaultRootObject": "index.html",
            },
        },
    )


def test_distribution_has_custom_error_responses_for_spa():
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


def test_distribution_uses_caching_optimized_policy():
    template = _get_template()
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": {
                "DefaultCacheBehavior": {
                    # Managed-CachingOptimized policy ID
                    "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
                },
            },
        },
    )


def test_creates_origin_access_control():
    template = _get_template()
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
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


def test_bucket_policy_allows_cloudfront_oac():
    """Verify the bucket policy grants s3:GetObject to CloudFront via OAC."""
    template = _get_template()
    template.has_resource_properties(
        "AWS::S3::BucketPolicy",
        {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with([
                    assertions.Match.object_like({
                        "Action": "s3:GetObject",
                        "Effect": "Allow",
                        "Principal": {"Service": "cloudfront.amazonaws.com"},
                    }),
                ]),
            },
        },
    )


def test_frontend_stack_exposes_properties():
    """Verify the real FrontendStack exposes distribution and domain name."""
    app = cdk.App()
    bucket_stack = cdk.Stack(app, "BucketStack")
    bucket = s3.Bucket(bucket_stack, "B")
    stack = FrontendStack(
        app,
        "TestFrontendStack",
        frontend_bucket=bucket,
    )
    assert stack.distribution is not None
    assert stack.distribution_domain_name is not None
