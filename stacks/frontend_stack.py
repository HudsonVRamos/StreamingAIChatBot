"""CDK Stack for Frontend hosting via S3 + CloudFront with OAC.

Creates a CloudFront distribution dedicated to serving the Frontend_Chat
application from S3_Frontend using Origin Access Control (OAC).

Requirements: 1.6, 1.7, 1.8, 1.9
"""

from aws_cdk import (
    Stack,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_s3 as s3,
)
from constructs import Construct


class FrontendStack(Stack):
    """Stack for CloudFront_Frontend distribution with S3 origin via OAC."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        frontend_bucket: s3.IBucket,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # CloudFront_Frontend distribution with S3 origin via OAC
        self.distribution = cloudfront.Distribution(
            self,
            "FrontendDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    frontend_bucket,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            default_root_object="index.html",
            # SPA routing: redirect 403/404 to index.html
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

    @property
    def distribution_domain_name(self) -> str:
        """The domain name of the CloudFront distribution."""
        return self.distribution.distribution_domain_name
