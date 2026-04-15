#!/usr/bin/env python3
"""CDK app entry point for the Streaming Chatbot infrastructure."""

import aws_cdk as cdk

from stacks.main_stack import MainStack

app = cdk.App()

MainStack(app, "StreamingChatbotStack", env=cdk.Environment(region="us-east-1"))

app.synth()
