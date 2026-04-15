"""Unit tests for DynamoDB query and S3 fallback in SLA tracking.

Tests for _consultar_ddb_sla and _consultar_s3_sla_fallback functions.

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 13.5
"""

import sys
import os
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call
from io import BytesIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas", "configuradora"))

import pytest
from botocore.exceptions import ClientError

from handler import _consultar_ddb_sla, _consultar_s3_sla_fallback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}},
        "Query",
    )


# ---------------------------------------------------------------------------
# test_ddb_pagination
# Validates: Requirement 2.3
# ---------------------------------------------------------------------------

def test_ddb_pagination():
    """Mock DynamoDB table.query() to return two pages (first with LastEvaluatedKey,
    second without). Verify all items from both pages are returned."""

    page1_items = [{"PK": "MediaLive#WARNER", "SK": "2024-01-01T00:00:00Z#ActiveAlerts", "severidade": "ERROR"}]
    page2_items = [{"PK": "MediaLive#WARNER", "SK": "2024-01-02T00:00:00Z#InputLoss", "severidade": "CRITICAL"}]

    mock_table = MagicMock()
    mock_table.query.side_effect = [
        {"Items": page1_items, "LastEvaluatedKey": {"PK": "MediaLive#WARNER", "SK": "2024-01-01T00:00:00Z#ActiveAlerts"}},
        {"Items": page2_items},
    ]

    result = _consultar_ddb_sla(
        canal="WARNER",
        servico="MediaLive",
        timestamp_inicio="2024-01-01T00:00:00Z",
        timestamp_fim="2024-01-31T23:59:59Z",
        ddb_table=mock_table,
    )

    assert len(result) == 2
    assert result[0] == page1_items[0]
    assert result[1] == page2_items[0]
    assert mock_table.query.call_count == 2

    # Second call must include ExclusiveStartKey
    second_call_kwargs = mock_table.query.call_args_list[1][1]
    assert "ExclusiveStartKey" in second_call_kwargs


# ---------------------------------------------------------------------------
# test_ddb_throttling_backoff
# Validates: Requirement 13.5
# ---------------------------------------------------------------------------

def test_ddb_throttling_backoff():
    """Mock DynamoDB to raise ProvisionedThroughputExceededException twice,
    then succeed on 3rd attempt. Verify 3 calls were made and result is correct."""

    success_items = [{"PK": "MediaLive#WARNER", "SK": "2024-01-01T00:00:00Z#ActiveAlerts", "severidade": "ERROR"}]
    throttle_error = _make_client_error("ProvisionedThroughputExceededException")

    mock_table = MagicMock()
    mock_table.query.side_effect = [
        throttle_error,
        throttle_error,
        {"Items": success_items},
    ]

    with patch("handler.time.sleep") as mock_sleep:
        result = _consultar_ddb_sla(
            canal="WARNER",
            servico="MediaLive",
            timestamp_inicio="2024-01-01T00:00:00Z",
            timestamp_fim="2024-01-31T23:59:59Z",
            ddb_table=mock_table,
        )

    assert mock_table.query.call_count == 3
    assert result == success_items

    # Verify exponential backoff sleeps were called
    assert mock_sleep.call_count == 2
    sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
    assert sleep_calls[0] < sleep_calls[1], "Backoff should increase between retries"


# ---------------------------------------------------------------------------
# test_ddb_failure_triggers_s3_fallback
# Validates: Requirement 2.5, 3.1
# ---------------------------------------------------------------------------

def test_ddb_failure_triggers_s3_fallback():
    """Placeholder: This test is for _calcular_sla (not yet implemented).

    When DynamoDB fails, _calcular_sla should call _consultar_s3_sla_fallback
    with the correct prefix. This test will be implemented once _calcular_sla
    is available.
    """
    # TODO: Implement when _calcular_sla is available.
    # Mock DynamoDB to raise an exception, then verify that
    # _consultar_s3_sla_fallback is called with the correct prefix.
    pass


# ---------------------------------------------------------------------------
# test_ddb_projection_expression
# Validates: Requirement 2.4
# ---------------------------------------------------------------------------

def test_ddb_projection_expression():
    """Mock DynamoDB table.query() and verify that ProjectionExpression
    is in the kwargs passed to query()."""

    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": []}

    _consultar_ddb_sla(
        canal="WARNER",
        servico="MediaLive",
        timestamp_inicio="2024-01-01T00:00:00Z",
        timestamp_fim="2024-01-31T23:59:59Z",
        ddb_table=mock_table,
    )

    assert mock_table.query.call_count == 1
    call_kwargs = mock_table.query.call_args[1]
    assert "ProjectionExpression" in call_kwargs
    projection = call_kwargs["ProjectionExpression"]
    # Must include the required fields for SLA calculation
    assert "severidade" in projection
    assert "metrica_nome" in projection
    assert "metrica_valor" in projection


# ---------------------------------------------------------------------------
# test_s3_fallback_prefix_with_servico
# Validates: Requirement 3.1
# ---------------------------------------------------------------------------

def test_s3_fallback_prefix_with_servico():
    """Mock s3_client.list_objects_v2 and s3_client.get_object.
    Verify the prefix used is {KB_LOGS_PREFIX}/{servico}/{canal}."""

    kb_bucket = "my-kb-bucket"
    kb_prefix = "kb-logs"
    canal = "WARNER"
    servico = "MediaLive"

    now = datetime.now(tz=timezone.utc)
    ts_inicio = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ts_fim = now

    event_data = {
        "timestamp": ts_inicio.isoformat(),
        "severidade": "ERROR",
        "canal": canal,
        "tipo_erro": "InputLoss",
        "metrica_nome": "ActiveAlerts",
    }

    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {
        "Contents": [
            {"Key": f"{kb_prefix}/{servico}/{canal}/event1.json", "LastModified": now},
        ],
        "IsTruncated": False,
    }
    mock_s3.get_object.return_value = {
        "Body": BytesIO(json.dumps(event_data).encode()),
    }

    with patch("handler.s3_client", mock_s3):
        with patch.dict(os.environ, {"KB_LOGS_BUCKET": kb_bucket, "KB_LOGS_PREFIX": kb_prefix}):
            result = _consultar_s3_sla_fallback(
                canal=canal,
                servico=servico,
                timestamp_inicio=ts_inicio,
                timestamp_fim=ts_fim,
            )

    mock_s3.list_objects_v2.assert_called_once()
    call_kwargs = mock_s3.list_objects_v2.call_args[1]
    expected_prefix = f"{kb_prefix}/{servico}/{canal}"
    assert call_kwargs["Prefix"] == expected_prefix
    assert call_kwargs["Bucket"] == kb_bucket
    assert len(result) == 1


# ---------------------------------------------------------------------------
# test_s3_fallback_prefix_without_servico
# Validates: Requirement 3.1
# ---------------------------------------------------------------------------

def test_s3_fallback_prefix_without_servico():
    """Same as above but with servico=None.
    Verify prefix is {KB_LOGS_PREFIX}/{canal}."""

    kb_bucket = "my-kb-bucket"
    kb_prefix = "kb-logs"
    canal = "WARNER"

    now = datetime.now(tz=timezone.utc)
    ts_inicio = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ts_fim = now

    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {
        "Contents": [],
        "IsTruncated": False,
    }

    with patch("handler.s3_client", mock_s3):
        with patch.dict(os.environ, {"KB_LOGS_BUCKET": kb_bucket, "KB_LOGS_PREFIX": kb_prefix}):
            result = _consultar_s3_sla_fallback(
                canal=canal,
                servico=None,
                timestamp_inicio=ts_inicio,
                timestamp_fim=ts_fim,
            )

    mock_s3.list_objects_v2.assert_called_once()
    call_kwargs = mock_s3.list_objects_v2.call_args[1]
    expected_prefix = f"{kb_prefix}/{canal}"
    assert call_kwargs["Prefix"] == expected_prefix
    assert result == []


# ---------------------------------------------------------------------------
# test_s3_fallback_filters_by_date
# Validates: Requirement 3.1
# ---------------------------------------------------------------------------

def test_s3_fallback_filters_by_date():
    """Verify that objects outside the date range are not included."""

    kb_bucket = "my-kb-bucket"
    kb_prefix = "kb-logs"
    canal = "WARNER"
    servico = "MediaLive"

    ts_inicio = datetime(2024, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
    ts_fim = datetime(2024, 1, 20, 23, 59, 59, tzinfo=timezone.utc)

    inside_range = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    before_range = datetime(2024, 1, 5, 12, 0, 0, tzinfo=timezone.utc)
    after_range = datetime(2024, 1, 25, 12, 0, 0, tzinfo=timezone.utc)

    event_inside = {"timestamp": inside_range.isoformat(), "severidade": "ERROR", "canal": canal, "tipo_erro": "X", "metrica_nome": "M"}
    event_before = {"timestamp": before_range.isoformat(), "severidade": "ERROR", "canal": canal, "tipo_erro": "X", "metrica_nome": "M"}
    event_after = {"timestamp": after_range.isoformat(), "severidade": "ERROR", "canal": canal, "tipo_erro": "X", "metrica_nome": "M"}

    def fake_get_object(Bucket, Key):
        if "inside" in Key:
            return {"Body": BytesIO(json.dumps(event_inside).encode())}
        elif "before" in Key:
            return {"Body": BytesIO(json.dumps(event_before).encode())}
        else:
            return {"Body": BytesIO(json.dumps(event_after).encode())}

    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {
        "Contents": [
            {"Key": f"{kb_prefix}/{servico}/{canal}/inside.json", "LastModified": inside_range},
            {"Key": f"{kb_prefix}/{servico}/{canal}/before.json", "LastModified": before_range},
            {"Key": f"{kb_prefix}/{servico}/{canal}/after.json", "LastModified": after_range},
        ],
        "IsTruncated": False,
    }
    mock_s3.get_object.side_effect = fake_get_object

    with patch("handler.s3_client", mock_s3):
        with patch.dict(os.environ, {"KB_LOGS_BUCKET": kb_bucket, "KB_LOGS_PREFIX": kb_prefix}):
            result = _consultar_s3_sla_fallback(
                canal=canal,
                servico=servico,
                timestamp_inicio=ts_inicio,
                timestamp_fim=ts_fim,
            )

    # Only the object within the date range should be included
    assert len(result) == 1
    assert result[0]["timestamp"] == inside_range.isoformat()
    # get_object should only be called once (for the in-range object)
    assert mock_s3.get_object.call_count == 1
