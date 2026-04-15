"""Unit tests for Lambda_Exportadora handler.

Tests cover:
- Export configurations with filter (CSV)
- Export logs in JSON format
- Export combined (config + logs)
- Default format is CSV when not specified
- No results returns "sem resultados" message

Requirements: 14.1, 14.2, 14.3, 14.4, 14.8
"""

from __future__ import annotations

import io
import csv
import json
from unittest.mock import patch, MagicMock

import pytest

from lambdas.exportadora.handler import handler

# Environment variable overrides for tests
_ENV_PATCH = {
    "KB_CONFIG_BUCKET": "test-kb-config-bucket",
    "KB_CONFIG_PREFIX": "kb-config/",
    "KB_LOGS_BUCKET": "test-kb-logs-bucket",
    "KB_LOGS_PREFIX": "kb-logs/",
    "EXPORTS_BUCKET": "test-exports-bucket",
    "EXPORTS_PREFIX": "exports/",
    "PRESIGNED_URL_EXPIRY": "3600",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(api_path: str, properties: list[dict] | None = None) -> dict:
    """Build a Bedrock Action Group event."""
    event: dict = {
        "actionGroup": "Action_Group_Export",
        "apiPath": api_path,
        "httpMethod": "POST",
    }
    if properties is not None:
        event["requestBody"] = {
            "content": {
                "application/json": {
                    "properties": properties,
                }
            }
        }
    return event


def _parse_response_body(resp: dict) -> dict:
    """Extract the parsed JSON body from a Bedrock Action Group response."""
    raw = resp["response"]["responseBody"]["application/json"]["body"]
    return json.loads(raw)


def _build_s3_list_page(keys: list[str]) -> dict:
    """Build a single page response for list_objects_v2."""
    return {"Contents": [{"Key": k} for k in keys]}


def _build_s3_get_body(data) -> dict:
    """Build a get_object response with JSON body."""
    body_mock = MagicMock()
    body_mock.read.return_value = json.dumps(data).encode("utf-8")
    return {"Body": body_mock}


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_CONFIG_1 = {
    "channel_id": "ch-1001",
    "nome_canal": "Canal Esportes",
    "servico": "MediaLive",
    "estado": "RUNNING",
    "regiao": "us-east-1",
    "codec_video": "H.264",
    "resolucao": "1080p",
    "bitrate_video": "5000",
    "low_latency": True,
    "protocolo_ingest": "SRT",
}

SAMPLE_CONFIG_2 = {
    "channel_id": "ch-1002",
    "nome_canal": "Canal Noticias",
    "servico": "MediaLive",
    "estado": "RUNNING",
    "regiao": "us-east-1",
    "codec_video": "H.265",
    "resolucao": "720p",
    "bitrate_video": "3000",
    "low_latency": False,
    "protocolo_ingest": "RTMP",
}

SAMPLE_LOG_1 = {
    "timestamp": "2024-01-15T10:30:00Z",
    "canal": "ch-1001",
    "severidade": "ERROR",
    "tipo_erro": "INPUT_LOSS",
    "descricao": "Perda de sinal no input primário",
    "causa_provavel": "Falha de rede",
    "recomendacao_correcao": "Verificar conectividade",
    "servico_origem": "MediaLive",
}

SAMPLE_LOG_2 = {
    "timestamp": "2024-01-15T12:00:00Z",
    "canal": "ch-1002",
    "severidade": "WARNING",
    "tipo_erro": "BITRATE_DROP",
    "descricao": "Queda de bitrate detectada",
    "causa_provavel": "Congestionamento de rede",
    "recomendacao_correcao": "Monitorar largura de banda",
    "servico_origem": "MediaLive",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExportConfigWithFilter:
    """test_export_config_with_filter — Req 14.1, 14.7"""

    @patch.dict("os.environ", _ENV_PATCH)
    @patch("lambdas.exportadora.handler.s3_client")
    def test_export_config_with_low_latency_filter_csv(self, mock_s3):
        # Patch module-level bucket vars
        import lambdas.exportadora.handler as mod
        mod.KB_CONFIG_BUCKET = "test-kb-config-bucket"
        mod.KB_CONFIG_PREFIX = "kb-config/"
        mod.EXPORTS_BUCKET = "test-exports-bucket"
        mod.EXPORTS_PREFIX = "exports/"

        paginator = MagicMock()
        paginator.paginate.return_value = [
            _build_s3_list_page([
                "kb-config/ch-1001.json",
                "kb-config/ch-1002.json",
            ])
        ]
        mock_s3.get_paginator.return_value = paginator

        def get_object_side_effect(**kwargs):
            key = kwargs.get("Key", "")
            if "ch-1001" in key:
                return _build_s3_get_body(SAMPLE_CONFIG_1)
            return _build_s3_get_body(SAMPLE_CONFIG_2)

        mock_s3.get_object.side_effect = get_object_side_effect
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/export.csv"

        event = _make_event("/exportarConfiguracoes", [
            {"name": "filtros", "value": json.dumps({"low_latency": True})},
            {"name": "formato", "value": "CSV"},
        ])

        resp = handler(event, None)
        body = _parse_response_body(resp)

        assert resp["response"]["httpStatusCode"] == 200
        assert "download_url" in body
        assert body["resumo"]["total_registros"] == 1
        assert body["resumo"]["formato"] == "CSV"

        # Verify the CSV content uploaded to S3
        put_call = mock_s3.put_object.call_args
        csv_content = put_call.kwargs.get("Body", b"")
        if isinstance(csv_content, bytes):
            csv_content = csv_content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(csv_content))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["channel_id"] == "ch-1001"
        assert rows[0]["low_latency"] == "True"


class TestExportLogsInJson:
    """test_export_logs_in_json — Req 14.2"""

    @patch.dict("os.environ", _ENV_PATCH)
    @patch("lambdas.exportadora.handler.s3_client")
    def test_export_logs_json_format(self, mock_s3):
        import lambdas.exportadora.handler as mod
        mod.KB_LOGS_BUCKET = "test-kb-logs-bucket"
        mod.KB_LOGS_PREFIX = "kb-logs/"
        mod.EXPORTS_BUCKET = "test-exports-bucket"
        mod.EXPORTS_PREFIX = "exports/"

        paginator = MagicMock()
        paginator.paginate.return_value = [
            _build_s3_list_page([
                "kb-logs/log-001.json",
                "kb-logs/log-002.json",
            ])
        ]
        mock_s3.get_paginator.return_value = paginator

        def get_object_side_effect(**kwargs):
            key = kwargs.get("Key", "")
            if "log-001" in key:
                return _build_s3_get_body(SAMPLE_LOG_1)
            return _build_s3_get_body(SAMPLE_LOG_2)

        mock_s3.get_object.side_effect = get_object_side_effect
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/export.json"

        event = _make_event("/exportarLogs", [
            {"name": "formato", "value": "JSON"},
        ])

        resp = handler(event, None)
        body = _parse_response_body(resp)

        assert resp["response"]["httpStatusCode"] == 200
        assert body["resumo"]["formato"] == "JSON"
        assert body["resumo"]["total_registros"] == 2

        # Verify JSON content uploaded
        put_call = mock_s3.put_object.call_args
        json_content = put_call.kwargs.get("Body", b"")
        if isinstance(json_content, bytes):
            json_content = json_content.decode("utf-8")
        parsed = json.loads(json_content)
        assert len(parsed) == 2
        assert parsed[0]["canal"] == "ch-1001"
        assert parsed[0]["severidade"] == "ERROR"
        assert parsed[1]["canal"] == "ch-1002"


class TestExportCombined:
    """test_export_combined — Req 14.3"""

    @patch.dict("os.environ", _ENV_PATCH)
    @patch("lambdas.exportadora.handler.s3_client")
    def test_export_combined_config_and_logs(self, mock_s3):
        import lambdas.exportadora.handler as mod
        mod.KB_CONFIG_BUCKET = "test-kb-config-bucket"
        mod.KB_CONFIG_PREFIX = "kb-config/"
        mod.KB_LOGS_BUCKET = "test-kb-logs-bucket"
        mod.KB_LOGS_PREFIX = "kb-logs/"
        mod.EXPORTS_BUCKET = "test-exports-bucket"
        mod.EXPORTS_PREFIX = "exports/"

        # get_paginator is called twice — once for config, once for logs
        config_paginator = MagicMock()
        config_paginator.paginate.return_value = [
            _build_s3_list_page(["kb-config/ch-1001.json"])
        ]
        logs_paginator = MagicMock()
        logs_paginator.paginate.return_value = [
            _build_s3_list_page(["kb-logs/log-001.json"])
        ]

        paginators = iter([config_paginator, logs_paginator])
        mock_s3.get_paginator.side_effect = lambda _: next(paginators)

        def get_object_side_effect(**kwargs):
            key = kwargs.get("Key", "")
            if "ch-1001" in key:
                return _build_s3_get_body(SAMPLE_CONFIG_1)
            return _build_s3_get_body(SAMPLE_LOG_1)

        mock_s3.get_object.side_effect = get_object_side_effect
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/export.csv"

        event = _make_event("/exportarCombinado", [
            {"name": "filtros_config", "value": "{}"},
            {"name": "filtros_logs", "value": "{}"},
        ])

        resp = handler(event, None)
        body = _parse_response_body(resp)

        assert resp["response"]["httpStatusCode"] == 200
        # Combined should have 1 config + 1 log = 2 records
        assert body["resumo"]["total_registros"] == 2


class TestDefaultFormatIsCsv:
    """test_default_format_is_csv — Req 14.4"""

    @patch.dict("os.environ", _ENV_PATCH)
    @patch("lambdas.exportadora.handler.s3_client")
    def test_no_format_specified_defaults_to_csv(self, mock_s3):
        import lambdas.exportadora.handler as mod
        mod.KB_CONFIG_BUCKET = "test-kb-config-bucket"
        mod.KB_CONFIG_PREFIX = "kb-config/"
        mod.EXPORTS_BUCKET = "test-exports-bucket"
        mod.EXPORTS_PREFIX = "exports/"

        paginator = MagicMock()
        paginator.paginate.return_value = [
            _build_s3_list_page(["kb-config/ch-1001.json"])
        ]
        mock_s3.get_paginator.return_value = paginator

        def get_object_side_effect(**kwargs):
            return _build_s3_get_body(SAMPLE_CONFIG_1)

        mock_s3.get_object.side_effect = get_object_side_effect
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/export.csv"

        # No "formato" property at all
        event = _make_event("/exportarConfiguracoes", [])

        resp = handler(event, None)
        body = _parse_response_body(resp)

        assert resp["response"]["httpStatusCode"] == 200
        assert body["resumo"]["formato"] == "CSV"

        # Verify the uploaded file has CSV content type
        put_call = mock_s3.put_object.call_args
        content_type = put_call.kwargs.get("ContentType", "")
        assert content_type == "text/csv"


class TestNoResultsReturnsMessage:
    """test_no_results_returns_message — Req 14.8"""

    @patch.dict("os.environ", _ENV_PATCH)
    @patch("lambdas.exportadora.handler.s3_client")
    def test_empty_results_returns_sem_resultados(self, mock_s3):
        import lambdas.exportadora.handler as mod
        mod.KB_CONFIG_BUCKET = "test-kb-config-bucket"
        mod.KB_CONFIG_PREFIX = "kb-config/"

        paginator = MagicMock()
        # Return empty page — no objects
        paginator.paginate.return_value = [{"Contents": []}]
        mock_s3.get_paginator.return_value = paginator

        event = _make_event("/exportarConfiguracoes", [
            {"name": "filtros", "value": json.dumps({"servico": "NonExistent"})},
        ])

        resp = handler(event, None)
        body = _parse_response_body(resp)

        assert resp["response"]["httpStatusCode"] == 200
        assert body["total_registros"] == 0
        assert "Nenhum resultado" in body["mensagem"]
        # No file should be generated — put_object should NOT be called
        mock_s3.put_object.assert_not_called()
