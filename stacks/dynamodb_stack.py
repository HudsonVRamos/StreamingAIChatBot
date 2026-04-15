"""CDK Stack for DynamoDB tables used by the Streaming Chatbot.

Creates StreamingConfigs and StreamingLogs tables with GSIs for
fast querying, replacing slow S3 list+read operations.

Requirements: 1.1–1.12
"""

from aws_cdk import (
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
)
from constructs import Construct


class DynamoDBStack(Stack):
    """Stack that creates DynamoDB tables for streaming data."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # StreamingConfigs — stores enriched channel/resource configs
        # PK = {servico}#{tipo_recurso}, SK = {resource_id}
        # Req 1.1–1.6
        self.configs_table = dynamodb.Table(
            self,
            "StreamingConfigs",
            partition_key=dynamodb.Attribute(
                name="PK",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="SK",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # GSI_NomeCanal — search configs by servico + nome_canal
        # Req 1.4
        self.configs_table.add_global_secondary_index(
            index_name="GSI_NomeCanal",
            partition_key=dynamodb.Attribute(
                name="servico",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="nome_canal",
                type=dynamodb.AttributeType.STRING,
            ),
        )

        # StreamingLogs — stores structured metric events with TTL
        # PK = {servico}#{canal}, SK = {timestamp}#{metrica_nome}
        # Req 1.7–1.12
        self.logs_table = dynamodb.Table(
            self,
            "StreamingLogs",
            partition_key=dynamodb.Attribute(
                name="PK",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="SK",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # GSI_Severidade — filter logs by severity level
        # Req 1.10
        self.logs_table.add_global_secondary_index(
            index_name="GSI_Severidade",
            partition_key=dynamodb.Attribute(
                name="severidade",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="SK",
                type=dynamodb.AttributeType.STRING,
            ),
        )
