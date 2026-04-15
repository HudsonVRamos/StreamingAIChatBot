"""CDK Stack for API Gateway and Lambda_Orquestradora."""

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    aws_apigateway as apigw,
    aws_iam as iam,
    aws_lambda as _lambda,
)
from constructs import Construct


class ApiStack(Stack):
    """Stack that creates the REST API Gateway and Lambda_Orquestradora.

    Requirements: 2.1, 2.2
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Lambda_Orquestradora ---
        self.orquestradora_fn = _lambda.Function(
            self,
            "OrquestradoraFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/orquestradora"),
            timeout=Duration.seconds(30),
            environment={
                "AGENT_ID": "PLACEHOLDER_AGENT_ID",
                "AGENT_ALIAS_ID": "PLACEHOLDER_AGENT_ALIAS_ID",
            },
        )

        # Grant bedrock:InvokeAgent permission
        self.orquestradora_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeAgent"],
                resources=["*"],
            )
        )

        # --- REST API Gateway ---
        self.api = apigw.RestApi(
            self,
            "ChatApi",
            rest_api_name="StreamingChatbotApi",
            description="REST API for the Streaming Chatbot",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["POST", "OPTIONS"],
                allow_headers=["Content-Type"],
            ),
        )

        # POST /chat with Lambda Proxy Integration (29s timeout)
        chat_resource = self.api.root.add_resource("chat")
        chat_resource.add_method(
            "POST",
            apigw.LambdaIntegration(
                self.orquestradora_fn,
                timeout=Duration.seconds(29),
            ),
        )

        # --- Outputs ---
        CfnOutput(
            self,
            "ApiUrl",
            value=self.api.url,
            description="URL of the Chat REST API",
        )
