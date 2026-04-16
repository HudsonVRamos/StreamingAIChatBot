#!/usr/bin/env python3
"""CDK app entry point for the Streaming Chatbot infrastructure."""

import aws_cdk as cdk

from stacks.main_stack import MainStack
from stacks.pipeline_ads_stack import PipelineAdsStack

app = cdk.App()

main_stack = MainStack(app, "StreamingChatbotStack", env=cdk.Environment(region="us-east-1"))

# Pipeline Ads com SQS batch processing
PipelineAdsStack(
    app, "StreamingChatbotPipelineAdsStack",
    kb_config_bucket=main_stack.kb_config_bucket,
    configs_table=main_stack.configs_table,
    env=cdk.Environment(region="us-east-1")
)

app.synth()
