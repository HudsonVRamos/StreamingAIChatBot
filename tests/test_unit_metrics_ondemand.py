"""Unit tests for the /consultarMetricas endpoint.

Tests fuzzy resolution, multiple candidates, missing parameters (400),
and invalid service name.

Requirements: 13.2, 13.6, 13.7, 13.8
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

import pytest
from botocore.exceptions import ClientError

from lambdas.configuradora.handler import handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(api_path: str, properties: list[dict]) -> dict:
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


def _parse_body(resp: dict) -> dict:
    """Extract the JSON body from a Bedrock response."""
    return json.loads(
        resp["response"]["responseBody"]["application/json"]["body"]
    )


def _status_code(resp: dict) -> int:
    return resp["response"]["httpStatusCode"]


def _mock_ml_paginator(channels: list[dict]):
    """Create a mock paginator for medialive_client.get_paginator('list_channels')."""
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [
        {"Channels": channels},
    ]
    return mock_paginator


# ---------------------------------------------------------------------------
# Missing parameters → 400
# ---------------------------------------------------------------------------


class TestMissingParameters:
    def test_missing_servico_returns_400(self):
        event = _make_event("/consultarMetricas", [
            {"name": "resource_id", "value": "Warner"},
        ])
        resp = handler(event, None)
        assert _status_code(resp) == 400
        body = _parse_body(resp)
        assert "obrigat" in body["erro"].lower()

    def test_missing_resource_id_returns_400(self):
        event = _make_event("/consultarMetricas", [
            {"name": "servico", "value": "MediaLive"},
        ])
        resp = handler(event, None)
        assert _status_code(resp) == 400
        body = _parse_body(resp)
        assert "obrigat" in body["erro"].lower()

    def test_both_missing_returns_400(self):
        event = _make_event("/consultarMetricas", [])
        resp = handler(event, None)
        assert _status_code(resp) == 400


# ---------------------------------------------------------------------------
# Invalid service → 400
# ---------------------------------------------------------------------------


class TestInvalidService:
    def test_invalid_service_name_returns_400(self):
        event = _make_event("/consultarMetricas", [
            {"name": "servico", "value": "InvalidService"},
            {"name": "resource_id", "value": "some-id"},
        ])
        resp = handler(event, None)
        assert _status_code(resp) == 400
        body = _parse_body(resp)
        assert "inválido" in body["erro"].lower()

    def test_empty_service_returns_400(self):
        event = _make_event("/consultarMetricas", [
            {"name": "servico", "value": ""},
            {"name": "resource_id", "value": "some-id"},
        ])
        resp = handler(event, None)
        assert _status_code(resp) == 400


# ---------------------------------------------------------------------------
# Fuzzy resolution with mock — single match
# ---------------------------------------------------------------------------


class TestFuzzyResolution:
    @patch("lambdas.configuradora.handler.boto3")
    @patch("lambdas.configuradora.handler.medialive_client")
    def test_fuzzy_resolution_medialive_success(self, mock_ml, mock_boto3):
        """Fuzzy search resolves to a single channel and returns metrics."""
        # Mock paginator for list_channels
        mock_ml.get_paginator.return_value = _mock_ml_paginator([
            {"Id": "12345", "Name": "Warner_HD", "State": "RUNNING"},
        ])
        mock_ml.describe_channel.return_value = {
            "Id": "12345",
            "Name": "Warner_HD",
            "Arn": "arn:aws:medialive:sa-east-1:123:channel:12345",
            "ChannelClass": "SINGLE_PIPELINE",
            "State": "RUNNING",
        }

        # Mock CloudWatch client
        mock_cw = MagicMock()
        mock_cw.get_metric_data.return_value = {
            "MetricDataResults": [
                {
                    "Id": "activealerts_0",
                    "Values": [0.0],
                    "Timestamps": [datetime.now(timezone.utc)],
                    "StatusCode": "Complete",
                },
                {
                    "Id": "activealerts_1",
                    "Values": [0.0],
                    "Timestamps": [datetime.now(timezone.utc)],
                    "StatusCode": "Complete",
                },
            ],
        }
        mock_boto3.client.return_value = mock_cw

        event = _make_event("/consultarMetricas", [
            {"name": "servico", "value": "MediaLive"},
            {"name": "resource_id", "value": "Warner"},
            {"name": "metricas", "value": '["ActiveAlerts"]'},
        ])
        resp = handler(event, None)
        assert _status_code(resp) == 200
        body = _parse_body(resp)
        assert "resumo" in body
        resumo = body["resumo"]
        assert "severidade_geral" in resumo
        assert "alertas" in resumo
        assert "metricas" in resumo


# ---------------------------------------------------------------------------
# Multiple candidates
# ---------------------------------------------------------------------------


class TestMultipleCandidates:
    @patch("lambdas.configuradora.handler.medialive_client")
    def test_multiple_candidates_returns_list(self, mock_ml):
        """When fuzzy search finds multiple channels, return candidates."""
        mock_ml.get_paginator.return_value = _mock_ml_paginator([
            {"Id": "111", "Name": "Warner_HD", "State": "RUNNING"},
            {"Id": "222", "Name": "Warner_SD", "State": "RUNNING"},
            {"Id": "333", "Name": "Warner_4K", "State": "RUNNING"},
        ])

        event = _make_event("/consultarMetricas", [
            {"name": "servico", "value": "MediaLive"},
            {"name": "resource_id", "value": "Warner"},
        ])
        resp = handler(event, None)
        assert _status_code(resp) == 200
        body = _parse_body(resp)
        assert body.get("multiplos_resultados") is True
        assert "candidatos" in body
        assert len(body["candidatos"]) == 3

    @patch("lambdas.configuradora.handler.mediatailor_client")
    def test_multiple_mediatailor_candidates(self, mock_mt):
        """Multiple MediaTailor configs matching fuzzy search."""
        mock_mt.list_playback_configurations.return_value = {
            "Items": [
                {"Name": "Config_Warner_HLS"},
                {"Name": "Config_Warner_DASH"},
            ],
            "NextToken": "",
        }

        event = _make_event("/consultarMetricas", [
            {"name": "servico", "value": "MediaTailor"},
            {"name": "resource_id", "value": "Warner"},
        ])
        resp = handler(event, None)
        assert _status_code(resp) == 200
        body = _parse_body(resp)
        assert body.get("multiplos_resultados") is True
        assert len(body["candidatos"]) == 2


# ---------------------------------------------------------------------------
# Successful query — response structure
# ---------------------------------------------------------------------------


class TestSuccessfulQuery:
    @patch("lambdas.configuradora.handler.boto3")
    @patch("lambdas.configuradora.handler.medialive_client")
    def test_successful_query_response_structure(self, mock_ml, mock_boto3):
        """A successful query returns mensagem, recurso, servico, periodo, resumo."""
        mock_ml.get_paginator.return_value = _mock_ml_paginator([
            {"Id": "12345", "Name": "Canal_Teste", "State": "RUNNING"},
        ])
        mock_ml.describe_channel.return_value = {
            "Id": "12345",
            "Name": "Canal_Teste",
            "Arn": "arn:aws:medialive:sa-east-1:123:channel:12345",
            "ChannelClass": "STANDARD",
            "State": "RUNNING",
        }

        mock_cw = MagicMock()
        now = datetime.now(timezone.utc)
        mock_cw.get_metric_data.return_value = {
            "MetricDataResults": [
                {"Id": "activealerts_0", "Values": [1.0], "Timestamps": [now], "StatusCode": "Complete"},
                {"Id": "activealerts_1", "Values": [0.0], "Timestamps": [now], "StatusCode": "Complete"},
                {"Id": "inputlossseconds_0", "Values": [5.0, 3.0], "Timestamps": [now, now - timedelta(minutes=5)], "StatusCode": "Complete"},
                {"Id": "inputlossseconds_1", "Values": [0.0], "Timestamps": [now], "StatusCode": "Complete"},
            ],
        }
        mock_boto3.client.return_value = mock_cw

        event = _make_event("/consultarMetricas", [
            {"name": "servico", "value": "MediaLive"},
            {"name": "resource_id", "value": "Canal_Teste"},
            {"name": "metricas", "value": '["ActiveAlerts", "InputLossSeconds"]'},
        ])
        resp = handler(event, None)
        assert _status_code(resp) == 200
        body = _parse_body(resp)

        assert "mensagem" in body
        assert body["recurso"] == "Canal_Teste"
        assert body["servico"] == "MediaLive"
        assert "periodo" in body

        resumo = body["resumo"]
        assert resumo["severidade_geral"] in {"INFO", "WARNING", "ERROR", "CRITICAL"}
        assert isinstance(resumo["alertas"], list)
        assert isinstance(resumo["metricas"], dict)

        # ActiveAlerts > 0 should generate an alert
        alert_metrics = [a["metrica"] for a in resumo["alertas"]]
        assert "ActiveAlerts" in alert_metrics
