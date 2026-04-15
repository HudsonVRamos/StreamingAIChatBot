"""Unit tests for Lambda_Configuradora handler."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

from botocore.exceptions import ClientError
import pytest

from lambdas.configuradora.handler import (
    validate_config_json,
    build_audit_log,
    _parse_parameters,
    handler,
)


# ---------------------------------------------------------------------------
# validate_config_json
# ---------------------------------------------------------------------------


class TestValidateConfigJson:
    def test_valid_medialive_channel(self):
        config = {
            "Name": "test-channel",
            "InputAttachments": [],
            "Destinations": [],
            "EncoderSettings": {},
        }
        result = validate_config_json("MediaLive", "channel", config)
        assert result.is_valid
        assert result.errors == []

    def test_missing_required_field(self):
        config = {"Name": "test-channel"}
        result = validate_config_json("MediaLive", "channel", config)
        assert not result.is_valid
        assert any("InputAttachments" in e for e in result.errors)

    def test_invalid_servico(self):
        result = validate_config_json("InvalidService", "channel", {})
        assert not result.is_valid
        assert any("Serviço inválido" in e for e in result.errors)

    def test_invalid_tipo_recurso(self):
        result = validate_config_json("MediaLive", "invalid_type", {})
        assert not result.is_valid
        assert any("Tipo de recurso inválido" in e for e in result.errors)

    def test_invalid_enum_value(self):
        config = {
            "Name": "test",
            "Type": "INVALID_TYPE",
        }
        result = validate_config_json("MediaLive", "input", config)
        assert not result.is_valid
        assert any("Valor inválido" in e for e in result.errors)

    def test_valid_enum_value(self):
        config = {"Name": "test", "Type": "SRT_CALLER"}
        result = validate_config_json("MediaLive", "input", config)
        assert result.is_valid

    def test_non_dict_config(self):
        result = validate_config_json("MediaLive", "channel", "not a dict")
        assert not result.is_valid
        assert any("objeto JSON" in e for e in result.errors)

    def test_valid_cloudfront_distribution(self):
        config = {"DistributionConfig": {"Origins": {}}}
        result = validate_config_json("CloudFront", "distribution", config)
        assert result.is_valid

    def test_valid_mediatailor_playback(self):
        config = {
            "Name": "test-config",
            "AdDecisionServerUrl": "https://ads.example.com",
            "VideoContentSourceUrl": "https://video.example.com",
        }
        result = validate_config_json(
            "MediaTailor", "playback_configuration", config
        )
        assert result.is_valid


# ---------------------------------------------------------------------------
# build_audit_log
# ---------------------------------------------------------------------------


class TestBuildAuditLog:
    def test_success_audit_log(self):
        entry = build_audit_log(
            operacao="criacao",
            servico="MediaLive",
            tipo_recurso="channel",
            resource_id="ch-123",
            config_aplicada={"Name": "test"},
            resultado="sucesso",
            rollback_info={"resource_id": "ch-123", "acao_reversao": "delete"},
        )
        assert entry["resultado"] == "sucesso"
        assert entry["resource_id"] == "ch-123"
        assert entry["tipo_operacao"] == "criacao"
        assert entry["servico_aws"] == "MediaLive"
        assert entry["timestamp"]
        assert entry["usuario_id"]
        assert entry["rollback_info"]["acao_reversao"] == "delete"
        assert entry["erro"] is None

    def test_failure_audit_log(self):
        entry = build_audit_log(
            operacao="/criarRecurso",
            servico="MediaLive",
            tipo_recurso="channel",
            config_aplicada={"Name": "test"},
            resultado="falha",
            erro={"codigo": "ValidationException", "mensagem": "Invalid"},
        )
        assert entry["resultado"] == "falha"
        assert entry["erro"]["codigo"] == "ValidationException"
        assert entry["rollback_info"] is None


# ---------------------------------------------------------------------------
# _parse_parameters
# ---------------------------------------------------------------------------


class TestParseParameters:
    def test_parse_from_request_body(self):
        event = {
            "requestBody": {
                "content": {
                    "application/json": {
                        "properties": [
                            {"name": "servico", "value": "MediaLive"},
                            {"name": "tipo_recurso", "value": "channel"},
                            {
                                "name": "configuracao_json",
                                "value": '{"Name": "test"}',
                            },
                        ]
                    }
                }
            }
        }
        params = _parse_parameters(event)
        assert params["servico"] == "MediaLive"
        assert params["configuracao_json"] == {"Name": "test"}

    def test_parse_from_top_level_parameters(self):
        event = {
            "parameters": [
                {"name": "resource_id", "value": "ch-123"},
            ]
        }
        params = _parse_parameters(event)
        assert params["resource_id"] == "ch-123"


# ---------------------------------------------------------------------------
# handler integration tests (mocked AWS)
# ---------------------------------------------------------------------------


def _make_event(api_path, properties):
    """Build a Bedrock Action Group event."""
    return {
        "actionGroup": "Action_Group_Config",
        "apiPath": api_path,
        "httpMethod": "POST",
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": properties,
                }
            }
        },
    }


class TestHandlerCreateResource:
    @patch("lambdas.configuradora.handler.store_audit_log")
    @patch("lambdas.configuradora.handler.medialive_client")
    def test_create_medialive_channel_success(self, mock_ml, mock_audit):
        mock_ml.create_channel.return_value = {
            "Channel": {"Id": "ch-new-123", "Name": "test"}
        }
        config = {
            "Name": "test",
            "InputAttachments": [],
            "Destinations": [],
            "EncoderSettings": {},
        }
        event = _make_event("/criarRecurso", [
            {"name": "servico", "value": "MediaLive"},
            {"name": "tipo_recurso", "value": "channel"},
            {"name": "configuracao_json", "value": json.dumps(config)},
        ])

        resp = handler(event, None)
        body = json.loads(
            resp["response"]["responseBody"]["application/json"]["body"]
        )
        assert resp["response"]["httpStatusCode"] == 200
        assert "ch-new-123" in body["resource_id"]
        mock_audit.assert_called_once()
        audit_arg = mock_audit.call_args[0][0]
        assert audit_arg["resultado"] == "sucesso"
        assert audit_arg["rollback_info"]["acao_reversao"] == "delete"


class TestHandlerModifyResource:
    @patch("lambdas.configuradora.handler.store_audit_log")
    @patch("lambdas.configuradora.handler.medialive_client")
    def test_modify_medialive_channel_gets_previous_config(
        self, mock_ml, mock_audit
    ):
        mock_ml.describe_channel.return_value = {
            "Id": "ch-123", "Name": "old-name"
        }
        mock_ml.update_channel.return_value = {
            "Channel": {"Id": "ch-123", "Name": "new-name"}
        }
        config = {"Name": "new-name"}
        event = _make_event("/modificarRecurso", [
            {"name": "servico", "value": "MediaLive"},
            {"name": "tipo_recurso", "value": "channel"},
            {"name": "resource_id", "value": "ch-123"},
            {"name": "configuracao_json", "value": json.dumps(config)},
        ])
        # resource_id comes from top-level parameters too
        event["parameters"] = [
            {"name": "resource_id", "value": "ch-123"},
        ]

        resp = handler(event, None)
        body = json.loads(
            resp["response"]["responseBody"]["application/json"]["body"]
        )
        assert resp["response"]["httpStatusCode"] == 200
        mock_ml.describe_channel.assert_called_once_with(ChannelId="ch-123")
        audit_arg = mock_audit.call_args[0][0]
        assert audit_arg["resultado"] == "sucesso"
        assert "config_anterior" in audit_arg["rollback_info"]


class TestHandlerValidationFailure:
    @patch("lambdas.configuradora.handler.store_audit_log")
    def test_reject_missing_required_fields(self, mock_audit):
        event = _make_event("/criarRecurso", [
            {"name": "servico", "value": "MediaLive"},
            {"name": "tipo_recurso", "value": "channel"},
            {"name": "configuracao_json", "value": '{"Name": "test"}'},
        ])

        resp = handler(event, None)
        body = json.loads(
            resp["response"]["responseBody"]["application/json"]["body"]
        )
        assert resp["response"]["httpStatusCode"] == 400
        assert "JSON inválido" in body["erro"]
        mock_audit.assert_called_once()
        audit_arg = mock_audit.call_args[0][0]
        assert audit_arg["resultado"] == "falha"


class TestHandlerAWSError:
    @patch("lambdas.configuradora.handler.store_audit_log")
    @patch("lambdas.configuradora.handler.medialive_client")
    def test_aws_api_failure_records_audit(self, mock_ml, mock_audit):
        mock_ml.create_channel.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Bad input"}},
            "CreateChannel",
        )
        config = {
            "Name": "test",
            "InputAttachments": [],
            "Destinations": [],
            "EncoderSettings": {},
        }
        event = _make_event("/criarRecurso", [
            {"name": "servico", "value": "MediaLive"},
            {"name": "tipo_recurso", "value": "channel"},
            {"name": "configuracao_json", "value": json.dumps(config)},
        ])

        resp = handler(event, None)
        body = json.loads(
            resp["response"]["responseBody"]["application/json"]["body"]
        )
        assert resp["response"]["httpStatusCode"] == 500
        assert "ValidationException" in body["erro"]
        assert "Bad input" in body["erro"]
        mock_audit.assert_called_once()
        audit_arg = mock_audit.call_args[0][0]
        assert audit_arg["resultado"] == "falha"
        assert audit_arg["erro"]["codigo"] == "ValidationException"


# ---------------------------------------------------------------------------
# _extract_resource_fields / _list_resources_dynamodb / list_resources
# ---------------------------------------------------------------------------

from lambdas.configuradora.handler import (
    _extract_resource_fields,
    _list_resources_dynamodb,
    _list_resources_aws,
    list_resources,
)


class TestExtractResourceFields:
    """Unit tests for _extract_resource_fields."""

    def test_medialive_channel(self):
        data = {
            "channel_id": "123",
            "nome_canal": "WARNER",
            "estado": "RUNNING",
            "dados": {"ChannelClass": "STANDARD"},
        }
        result = _extract_resource_fields(
            "MediaLive", "channel", data,
        )
        assert result == {
            "id": "123",
            "nome": "WARNER",
            "estado": "RUNNING",
            "classe": "STANDARD",
        }

    def test_medialive_input(self):
        data = {
            "channel_id": "456",
            "nome_canal": "INPUT_1",
            "estado": "ATTACHED",
            "dados": {"Type": "RTMP_PUSH"},
        }
        result = _extract_resource_fields(
            "MediaLive", "input", data,
        )
        assert result == {
            "id": "456",
            "nome": "INPUT_1",
            "tipo": "RTMP_PUSH",
            "estado": "ATTACHED",
        }

    def test_mediapackage_channel_v2(self):
        data = {
            "nome_canal": "VRIO_CH",
            "dados": {
                "ChannelGroupName": "VRIO_CHANNELS",
                "InputType": "HLS",
            },
        }
        result = _extract_resource_fields(
            "MediaPackage", "channel_v2", data,
        )
        assert result == {
            "channel_group": "VRIO_CHANNELS",
            "nome": "VRIO_CH",
            "input_type": "HLS",
        }

    def test_mediapackage_origin_endpoint_v2(self):
        data = {
            "dados": {
                "ChannelGroupName": "GRP",
                "ChannelName": "CH1",
                "OriginEndpointName": "EP1",
                "ContainerType": "TS",
            },
        }
        result = _extract_resource_fields(
            "MediaPackage", "origin_endpoint_v2", data,
        )
        assert result == {
            "channel_group": "GRP",
            "canal": "CH1",
            "endpoint": "EP1",
            "container": "TS",
        }

    def test_mediatailor_playback(self):
        data = {
            "nome_canal": "MY_CONFIG",
            "dados": {
                "VideoContentSourceUrl": "https://example.com",
                "AdDecisionServerUrl": "https://ads.example.com",
            },
        }
        result = _extract_resource_fields(
            "MediaTailor", "playback_configuration", data,
        )
        assert result["nome"] == "MY_CONFIG"
        assert result["video_source"] == "https://example.com"
        assert result["ad_server"] == "https://ads.example.com"

    def test_cloudfront_distribution(self):
        data = {
            "channel_id": "E123",
            "dados": {
                "DomainName": "d123.cloudfront.net",
                "Status": "Deployed",
                "Enabled": True,
                "Comment": "Test dist",
            },
        }
        result = _extract_resource_fields(
            "CloudFront", "distribution", data,
        )
        assert result == {
            "id": "E123",
            "domain": "d123.cloudfront.net",
            "status": "Deployed",
            "enabled": True,
            "comment": "Test dist",
        }

    def test_unknown_service_returns_basic(self):
        data = {"channel_id": "X", "nome_canal": "Y"}
        result = _extract_resource_fields(
            "Unknown", "thing", data,
        )
        assert result == {"id": "X", "nome": "Y"}


class TestListResourcesDynamoDBFallback:
    """Test that list_resources tries DynamoDB first, falls back to AWS."""

    @patch(
        "lambdas.configuradora.handler._CONFIGS_TABLE_NAME",
        "StreamingConfigs",
    )
    @patch(
        "lambdas.configuradora.handler._list_resources_dynamodb",
    )
    def test_uses_dynamodb_when_configured(self, mock_ddb):
        mock_ddb.return_value = {
            "servico": "MediaLive",
            "tipo_recurso": "channel",
            "total": 2,
            "recursos": [
                {"id": "1", "nome": "CH1", "estado": "RUNNING", "classe": "STANDARD"},
                {"id": "2", "nome": "CH2", "estado": "IDLE", "classe": "SINGLE"},
            ],
        }
        result = list_resources("MediaLive", "channel")
        assert result["total"] == 2
        mock_ddb.assert_called_once_with("MediaLive", "channel")

    @patch(
        "lambdas.configuradora.handler._CONFIGS_TABLE_NAME",
        "StreamingConfigs",
    )
    @patch(
        "lambdas.configuradora.handler._list_resources_aws",
    )
    @patch(
        "lambdas.configuradora.handler._list_resources_dynamodb",
    )
    def test_falls_back_on_dynamodb_error(
        self, mock_ddb, mock_aws,
    ):
        mock_ddb.side_effect = Exception("DynamoDB down")
        mock_aws.return_value = {
            "servico": "MediaLive",
            "tipo_recurso": "channel",
            "total": 1,
            "recursos": [{"id": "1", "nome": "CH1", "estado": "RUNNING", "classe": "STANDARD"}],
        }
        result = list_resources("MediaLive", "channel")
        assert result["total"] == 1
        mock_aws.assert_called_once_with("MediaLive", "channel")

    @patch(
        "lambdas.configuradora.handler._CONFIGS_TABLE_NAME",
        "StreamingConfigs",
    )
    @patch(
        "lambdas.configuradora.handler._list_resources_aws",
    )
    @patch(
        "lambdas.configuradora.handler._list_resources_dynamodb",
    )
    def test_falls_back_on_empty_dynamodb(
        self, mock_ddb, mock_aws,
    ):
        mock_ddb.return_value = {
            "servico": "MediaLive",
            "tipo_recurso": "channel",
            "total": 0,
            "recursos": [],
        }
        mock_aws.return_value = {
            "servico": "MediaLive",
            "tipo_recurso": "channel",
            "total": 3,
            "recursos": [{"id": "1"}, {"id": "2"}, {"id": "3"}],
        }
        result = list_resources("MediaLive", "channel")
        assert result["total"] == 3
        mock_aws.assert_called_once()

    @patch(
        "lambdas.configuradora.handler._CONFIGS_TABLE_NAME",
        "",
    )
    @patch(
        "lambdas.configuradora.handler._list_resources_aws",
    )
    def test_skips_dynamodb_when_not_configured(self, mock_aws):
        mock_aws.return_value = {
            "servico": "MediaLive",
            "tipo_recurso": "channel",
            "total": 1,
            "recursos": [{"id": "1"}],
        }
        result = list_resources("MediaLive", "channel")
        assert result["total"] == 1
        mock_aws.assert_called_once()


class TestListResourcesDynamoDB:
    """Test _list_resources_dynamodb with mocked DynamoDB table."""

    @patch("lambdas.configuradora.handler._dynamodb_resource")
    @patch(
        "lambdas.configuradora.handler._CONFIGS_TABLE_NAME",
        "StreamingConfigs",
    )
    def test_queries_and_parses_items(self, mock_ddb_res):
        mock_table = MagicMock()
        mock_ddb_res.Table.return_value = mock_table
        mock_table.query.return_value = {
            "Items": [
                {
                    "PK": "MediaLive#channel",
                    "SK": "123",
                    "data": json.dumps({
                        "channel_id": "123",
                        "nome_canal": "WARNER",
                        "estado": "RUNNING",
                        "dados": {"ChannelClass": "STANDARD"},
                    }),
                },
            ],
        }
        result = _list_resources_dynamodb(
            "MediaLive", "channel",
        )
        assert result["total"] == 1
        assert result["recursos"][0]["id"] == "123"
        assert result["recursos"][0]["nome"] == "WARNER"
        assert result["recursos"][0]["classe"] == "STANDARD"

    @patch("lambdas.configuradora.handler._dynamodb_resource")
    @patch(
        "lambdas.configuradora.handler._CONFIGS_TABLE_NAME",
        "StreamingConfigs",
    )
    def test_handles_pagination(self, mock_ddb_res):
        mock_table = MagicMock()
        mock_ddb_res.Table.return_value = mock_table
        mock_table.query.side_effect = [
            {
                "Items": [
                    {
                        "PK": "MediaLive#channel",
                        "SK": "1",
                        "data": json.dumps({
                            "channel_id": "1",
                            "nome_canal": "CH1",
                            "estado": "RUNNING",
                            "dados": {"ChannelClass": "STANDARD"},
                        }),
                    },
                ],
                "LastEvaluatedKey": {"PK": "MediaLive#channel", "SK": "1"},
            },
            {
                "Items": [
                    {
                        "PK": "MediaLive#channel",
                        "SK": "2",
                        "data": json.dumps({
                            "channel_id": "2",
                            "nome_canal": "CH2",
                            "estado": "IDLE",
                            "dados": {"ChannelClass": "SINGLE"},
                        }),
                    },
                ],
            },
        ]
        result = _list_resources_dynamodb(
            "MediaLive", "channel",
        )
        assert result["total"] == 2
        assert mock_table.query.call_count == 2

    @patch("lambdas.configuradora.handler._dynamodb_resource")
    @patch(
        "lambdas.configuradora.handler._CONFIGS_TABLE_NAME",
        "StreamingConfigs",
    )
    def test_handles_invalid_json_data(self, mock_ddb_res):
        mock_table = MagicMock()
        mock_ddb_res.Table.return_value = mock_table
        mock_table.query.return_value = {
            "Items": [
                {
                    "PK": "MediaLive#channel",
                    "SK": "1",
                    "data": "not-valid-json{{{",
                },
            ],
        }
        result = _list_resources_dynamodb(
            "MediaLive", "channel",
        )
        assert result["total"] == 1
        # Falls back to empty data extraction
        assert result["recursos"][0]["id"] == ""
