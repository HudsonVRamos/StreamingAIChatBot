"""Unit tests for the DynamoDBStack."""

import aws_cdk as cdk
import aws_cdk.assertions as assertions

from stacks.dynamodb_stack import DynamoDBStack


def _get_template() -> assertions.Template:
    app = cdk.App()
    stack = DynamoDBStack(app, "TestDynamoDBStack")
    return assertions.Template.from_stack(stack)


def test_creates_streaming_configs_table():
    template = _get_template()
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "PointInTimeRecoverySpecification": {
                "PointInTimeRecoveryEnabled": True,
            },
            "SSESpecification": {"SSEEnabled": True},
        },
    )


def test_streaming_configs_has_gsi_nome_canal():
    template = _get_template()
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "GlobalSecondaryIndexes": assertions.Match.array_with(
                [
                    assertions.Match.object_like(
                        {
                            "IndexName": "GSI_NomeCanal",
                            "KeySchema": [
                                {
                                    "AttributeName": "servico",
                                    "KeyType": "HASH",
                                },
                                {
                                    "AttributeName": "nome_canal",
                                    "KeyType": "RANGE",
                                },
                            ],
                        }
                    )
                ]
            ),
        },
    )


def test_creates_streaming_logs_table_with_ttl():
    template = _get_template()
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "TimeToLiveSpecification": {
                "AttributeName": "ttl",
                "Enabled": True,
            },
        },
    )


def test_streaming_logs_has_gsi_severidade():
    template = _get_template()
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "GlobalSecondaryIndexes": assertions.Match.array_with(
                [
                    assertions.Match.object_like(
                        {
                            "IndexName": "GSI_Severidade",
                            "KeySchema": [
                                {
                                    "AttributeName": "severidade",
                                    "KeyType": "HASH",
                                },
                                {
                                    "AttributeName": "SK",
                                    "KeyType": "RANGE",
                                },
                            ],
                        }
                    )
                ]
            ),
        },
    )


def test_tables_have_destroy_removal_policy():
    template = _get_template()
    # Both tables should have DeletionPolicy: Delete
    resources = template.find_resources("AWS::DynamoDB::Table")
    for _name, resource in resources.items():
        assert resource.get("DeletionPolicy") == "Delete"


def test_stack_exposes_table_properties():
    """Verify the stack exposes configs_table and logs_table."""
    app = cdk.App()
    stack = DynamoDBStack(app, "TestDynamoDBStack")
    assert stack.configs_table is not None
    assert stack.logs_table is not None
