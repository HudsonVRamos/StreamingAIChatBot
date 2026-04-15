"""Unit tests for Pipeline de Logs handler.

Validates: Requirements 7.1, 7.2, 7.3, 7.5, 10.5

Tests cover:
1. test_collects_and_normalizes_logs — Mock CloudWatch logs client
   to return log events. Verify the handler normalizes and stores
   them in S3.
2. test_enriches_events — Verify stored events contain
   causa_provavel, impacto_estimado, recomendacao_correcao fields.
3. test_rejects_config_data_in_logs_bucket — Mock a scenario where
   a normalized event looks like a Config_Enriquecida. Verify it's
   rejected by contamination check.
4. test_individual_service_failure_continues — Mock one log group
   to fail. Verify the handler continues with other log groups and
   records the error.
5. test_handler_returns_summary — Verify the handler returns a
   summary with stored count and errors.
"""

import json
import sys
import os
from unittest.mock import MagicMock, patch

# Add lambdas/pipeline_logs to sys.path so that the
# ``from shared.normalizers import ...`` inside handler.py
# resolves correctly (mirrors the Lambda runtime layout).
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        os.pardir,
        "lambdas",
        "pipeline_logs",
    ),
)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _make_logs_mock(events=None, fail=False, fail_message=""):
    """Build a CloudWatch Logs mock with filter_log_events."""
    mock = MagicMock()
    if fail:
        mock.filter_log_events.side_effect = Exception(
            fail_message or "CloudWatch API error"
        )
    else:
        mock.filter_log_events.return_value = {
            "events": events or [],
        }
    return mock


def _make_s3_mock():
    """Return a fresh S3 mock client."""
    return MagicMock()


def _make_boto3_factory(logs_mock, s3_mock):
    """Return a function that dispatches boto3.client calls."""
    def _factory(service_name, **kwargs):
        if service_name == "logs":
            return logs_mock
        if service_name == "s3":
            return s3_mock
        return MagicMock()
    return _factory


def _sample_log_event(
    message="ERROR input loss detected on channel-42",
    timestamp=1700000000000,
):
    """Return a minimal CloudWatch log event dict."""
    return {
        "timestamp": timestamp,
        "message": message,
        "logStreamName": "stream/channel-42",
        "logGroupName": "/aws/medialive/channel",
    }


# -------------------------------------------------------------------
# 1. test_collects_and_normalizes_logs
# -------------------------------------------------------------------

ENV = {
    "KB_LOGS_BUCKET": "test-logs-bucket",
    "KB_LOGS_PREFIX": "kb-logs/",
    "LOG_GROUPS": (
        "/aws/medialive/channel,"
        "/aws/mediapackage/channel"
    ),
}


@patch.dict("os.environ", ENV)
@patch("lambdas.pipeline_logs.handler.KB_LOGS_BUCKET", "test-logs-bucket")
@patch("boto3.client")
def test_collects_and_normalizes_logs(mock_boto3):
    """CloudWatch returns log events for two log groups.
    Verify the handler normalizes and stores them in S3."""
    s3_mock = _make_s3_mock()
    logs_mock = MagicMock()

    # Both log groups return one event each (single page)
    logs_mock.filter_log_events.return_value = {
        "events": [_sample_log_event()],
    }

    mock_boto3.side_effect = _make_boto3_factory(
        logs_mock, s3_mock,
    )

    from lambdas.pipeline_logs.handler import handler
    result = handler({}, None)

    body = result["body"]
    # Two log groups × 1 event each = 2 stored
    assert body["stored"] == 2
    assert s3_mock.put_object.call_count == 2

    # Verify S3 put_object args for the first call
    first_put = s3_mock.put_object.call_args_list[0]
    assert first_put.kwargs["Bucket"] == "test-logs-bucket"
    assert first_put.kwargs["ContentType"] == "application/json"
    assert "kb-logs/" in first_put.kwargs["Key"]

    # Verify stored body is valid JSON with Evento_Estruturado
    stored = json.loads(first_put.kwargs["Body"])
    assert "timestamp" in stored
    assert "canal" in stored
    assert "severidade" in stored
    assert "tipo_erro" in stored
    assert "descricao" in stored


# -------------------------------------------------------------------
# 2. test_enriches_events
# -------------------------------------------------------------------

@patch.dict("os.environ", ENV)
@patch("boto3.client")
def test_enriches_events(mock_boto3):
    """Verify stored events contain enrichment fields:
    causa_provavel, impacto_estimado, recomendacao_correcao."""
    s3_mock = _make_s3_mock()
    logs_mock = MagicMock()
    logs_mock.filter_log_events.return_value = {
        "events": [
            _sample_log_event(
                message="ERROR input loss on channel-7",
            ),
        ],
    }

    mock_boto3.side_effect = _make_boto3_factory(
        logs_mock, s3_mock,
    )

    from lambdas.pipeline_logs.handler import handler
    handler({}, None)

    assert s3_mock.put_object.call_count >= 1
    stored = json.loads(
        s3_mock.put_object.call_args_list[0].kwargs["Body"],
    )

    # Enrichment fields must be present (Req 7.3)
    assert "causa_provavel" in stored
    assert "impacto_estimado" in stored
    assert "recomendacao_correcao" in stored
    # For INPUT_LOSS the enrichment should be specific
    assert stored["causa_provavel"] != ""
    assert stored["impacto_estimado"] != ""
    assert stored["recomendacao_correcao"] != ""


# -------------------------------------------------------------------
# 3. test_rejects_config_data_in_logs_bucket
# -------------------------------------------------------------------

@patch.dict("os.environ", ENV)
@patch("boto3.client")
@patch("lambdas.pipeline_logs.handler.enrich_evento")
@patch("lambdas.pipeline_logs.handler.normalize_cloudwatch_log")
def test_rejects_config_data_in_logs_bucket(
    mock_normalize, mock_enrich, mock_boto3,
):
    """If a normalized event looks like a Config_Enriquecida
    (has channel_id, servico, tipo, dados), the contamination
    check should reject it."""
    s3_mock = _make_s3_mock()
    logs_mock = MagicMock()
    logs_mock.filter_log_events.return_value = {
        "events": [_sample_log_event()],
    }

    # Return a record that has Config_Enriquecida fields
    # (cross-contamination scenario)
    config_like_record = {
        "channel_id": "ch-999",
        "servico": "MediaLive",
        "tipo": "configuracao",
        "dados": {"some": "config"},
        # Also include Evento fields so validation passes
        "timestamp": "2024-01-01T00:00:00Z",
        "canal": "channel-42",
        "severidade": "ERROR",
        "tipo_erro": "INPUT_LOSS",
        "descricao": "test",
        "causa_provavel": "test",
        "impacto_estimado": "test",
        "recomendacao_correcao": "test",
    }
    mock_normalize.return_value = config_like_record
    mock_enrich.return_value = config_like_record

    mock_boto3.side_effect = _make_boto3_factory(
        logs_mock, s3_mock,
    )

    from lambdas.pipeline_logs.handler import handler
    result = handler({}, None)

    body = result["body"]
    # Should be rejected by contamination check (Req 10.5)
    assert body["skipped_contamination"] >= 1
    assert body["stored"] == 0


# -------------------------------------------------------------------
# 4. test_individual_service_failure_continues
# -------------------------------------------------------------------

@patch.dict("os.environ", {
    "KB_LOGS_BUCKET": "test-logs-bucket",
    "KB_LOGS_PREFIX": "kb-logs/",
    "LOG_GROUPS": (
        "/aws/medialive/channel,"
        "/aws/mediapackage/channel"
    ),
})
@patch("boto3.client")
def test_individual_service_failure_continues(mock_boto3):
    """Mock one log group to fail. Verify the handler continues
    with other log groups and records the error."""
    s3_mock = _make_s3_mock()
    logs_mock = MagicMock()

    call_count = {"n": 0}

    def _filter_side_effect(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First log group fails
            raise Exception("Throttled by CloudWatch")
        # Second log group succeeds
        return {
            "events": [
                _sample_log_event(
                    message="WARNING latency spike detected",
                ),
            ],
        }

    logs_mock.filter_log_events.side_effect = _filter_side_effect

    mock_boto3.side_effect = _make_boto3_factory(
        logs_mock, s3_mock,
    )

    from lambdas.pipeline_logs.handler import handler
    result = handler({}, None)

    body = result["body"]
    # First log group error should be recorded (Req 7.5)
    assert len(body["errors"]) >= 1
    error = body["errors"][0]
    assert "Throttled" in error["reason"]
    assert error["service"] == "MediaLive"

    # Second log group should still be processed
    assert body["stored"] >= 1


# -------------------------------------------------------------------
# 5. test_handler_returns_summary
# -------------------------------------------------------------------

@patch.dict("os.environ", ENV)
@patch("boto3.client")
def test_handler_returns_summary(mock_boto3):
    """Verify the handler returns a summary with stored count
    and errors."""
    s3_mock = _make_s3_mock()
    logs_mock = MagicMock()
    logs_mock.filter_log_events.return_value = {
        "events": [_sample_log_event()],
    }

    mock_boto3.side_effect = _make_boto3_factory(
        logs_mock, s3_mock,
    )

    from lambdas.pipeline_logs.handler import handler
    result = handler({}, None)

    assert result["statusCode"] == 200
    body = result["body"]
    assert "stored" in body
    assert "errors" in body
    assert "skipped_validation" in body
    assert "skipped_contamination" in body
    assert isinstance(body["stored"], int)
    assert isinstance(body["errors"], list)
    assert isinstance(body["skipped_validation"], int)
    assert isinstance(body["skipped_contamination"], int)


# ===================================================================
# Tests for discover_resources()
# Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.6
# ===================================================================


def _discover_client_factory(
    ml_mock, mp_mock, mt_mock, cf_mock,
):
    """Return a factory that dispatches boto3.client calls
    for discover_resources tests."""
    def _factory(service_name, **kwargs):
        if service_name == "medialive":
            return ml_mock
        if service_name == "mediapackagev2":
            return mp_mock
        if service_name == "mediatailor":
            return mt_mock
        if service_name == "cloudfront":
            return cf_mock
        return MagicMock()
    return _factory


@patch("boto3.client")
def test_discover_resources_happy_path(mock_boto3):
    """All four services return resources successfully."""
    ml_mock = MagicMock()
    ml_mock.list_channels.return_value = {
        "Channels": [
            {"Id": "111", "Name": "Warner_HD"},
            {"Id": "222", "Name": "ESPN_4K"},
        ],
    }

    mp_mock = MagicMock()
    mp_mock.list_channel_groups.return_value = {
        "Items": [
            {"ChannelGroupName": "grp-1"},
        ],
    }
    mp_mock.list_channels.return_value = {
        "Items": [
            {"ChannelName": "ch-a"},
        ],
    }
    mp_mock.list_origin_endpoints.return_value = {
        "Items": [
            {"OriginEndpointName": "ep-1"},
        ],
    }

    mt_mock = MagicMock()
    mt_mock.list_playback_configurations.return_value = {
        "Items": [
            {"Name": "config-live"},
        ],
    }

    cf_mock = MagicMock()
    cf_mock.list_distributions.return_value = {
        "DistributionList": {
            "Items": [
                {"Id": "E1234ABC"},
            ],
            "IsTruncated": False,
        },
    }

    mock_boto3.side_effect = _discover_client_factory(
        ml_mock, mp_mock, mt_mock, cf_mock,
    )

    from lambdas.pipeline_logs.handler import (
        discover_resources,
    )
    result = discover_resources()

    # MediaLive
    assert len(result["MediaLive"]) == 2
    assert result["MediaLive"][0]["ChannelId"] == "111"
    assert result["MediaLive"][0]["Name"] == "Warner_HD"
    assert result["MediaLive"][1]["ChannelId"] == "222"

    # MediaPackage
    assert len(result["MediaPackage"]) == 1
    assert (
        result["MediaPackage"][0]["ChannelGroup"]
        == "grp-1"
    )
    assert (
        result["MediaPackage"][0]["ChannelName"] == "ch-a"
    )
    assert result["MediaPackage"][0][
        "OriginEndpoints"
    ] == ["ep-1"]

    # MediaTailor
    assert len(result["MediaTailor"]) == 1
    assert (
        result["MediaTailor"][0]["Name"] == "config-live"
    )

    # CloudFront
    assert len(result["CloudFront"]) == 1
    assert (
        result["CloudFront"][0]["DistributionId"]
        == "E1234ABC"
    )


@patch("boto3.client")
def test_discover_resources_partial_failure(mock_boto3):
    """One service (MediaLive) fails; others still return
    resources."""
    ml_mock = MagicMock()
    ml_mock.list_channels.side_effect = Exception(
        "AccessDeniedException"
    )

    mp_mock = MagicMock()
    mp_mock.list_channel_groups.return_value = {
        "Items": [{"ChannelGroupName": "grp-1"}],
    }
    mp_mock.list_channels.return_value = {
        "Items": [{"ChannelName": "ch-a"}],
    }
    mp_mock.list_origin_endpoints.return_value = {
        "Items": [],
    }

    mt_mock = MagicMock()
    mt_mock.list_playback_configurations.return_value = {
        "Items": [{"Name": "cfg-1"}],
    }

    cf_mock = MagicMock()
    cf_mock.list_distributions.return_value = {
        "DistributionList": {
            "Items": [{"Id": "EABC"}],
            "IsTruncated": False,
        },
    }

    mock_boto3.side_effect = _discover_client_factory(
        ml_mock, mp_mock, mt_mock, cf_mock,
    )

    from lambdas.pipeline_logs.handler import (
        discover_resources,
    )
    result = discover_resources()

    # MediaLive failed → empty list
    assert result["MediaLive"] == []

    # Others succeeded
    assert len(result["MediaPackage"]) == 1
    assert len(result["MediaTailor"]) == 1
    assert len(result["CloudFront"]) == 1


@patch("boto3.client")
def test_discover_resources_pagination(mock_boto3):
    """Services return paginated results with NextToken /
    Marker."""
    # MediaLive: two pages
    ml_mock = MagicMock()
    ml_mock.list_channels.side_effect = [
        {
            "Channels": [{"Id": "1", "Name": "ch-1"}],
            "NextToken": "page2",
        },
        {
            "Channels": [{"Id": "2", "Name": "ch-2"}],
        },
    ]

    # MediaPackage: two pages of channel groups
    mp_mock = MagicMock()
    mp_mock.list_channel_groups.side_effect = [
        {
            "Items": [{"ChannelGroupName": "g1"}],
            "NextToken": "grp-page2",
        },
        {
            "Items": [{"ChannelGroupName": "g2"}],
        },
    ]
    mp_mock.list_channels.return_value = {
        "Items": [{"ChannelName": "ch-x"}],
    }
    mp_mock.list_origin_endpoints.return_value = {
        "Items": [],
    }

    # MediaTailor: two pages
    mt_mock = MagicMock()
    mt_mock.list_playback_configurations.side_effect = [
        {
            "Items": [{"Name": "cfg-a"}],
            "NextToken": "mt-page2",
        },
        {
            "Items": [{"Name": "cfg-b"}],
        },
    ]

    # CloudFront: two pages via IsTruncated + Marker
    cf_mock = MagicMock()
    cf_mock.list_distributions.side_effect = [
        {
            "DistributionList": {
                "Items": [{"Id": "E1"}],
                "IsTruncated": True,
                "NextMarker": "marker2",
            },
        },
        {
            "DistributionList": {
                "Items": [{"Id": "E2"}],
                "IsTruncated": False,
            },
        },
    ]

    mock_boto3.side_effect = _discover_client_factory(
        ml_mock, mp_mock, mt_mock, cf_mock,
    )

    from lambdas.pipeline_logs.handler import (
        discover_resources,
    )
    result = discover_resources()

    # MediaLive: 2 channels across 2 pages
    assert len(result["MediaLive"]) == 2
    ids = [
        c["ChannelId"] for c in result["MediaLive"]
    ]
    assert ids == ["1", "2"]

    # MediaPackage: 2 groups × 1 channel each
    assert len(result["MediaPackage"]) == 2

    # MediaTailor: 2 configs across 2 pages
    assert len(result["MediaTailor"]) == 2
    names = [
        c["Name"] for c in result["MediaTailor"]
    ]
    assert names == ["cfg-a", "cfg-b"]

    # CloudFront: 2 distributions across 2 pages
    assert len(result["CloudFront"]) == 2
    dist_ids = [
        d["DistributionId"]
        for d in result["CloudFront"]
    ]
    assert dist_ids == ["E1", "E2"]


@patch("boto3.client")
def test_discover_resources_empty_results(mock_boto3):
    """All services return empty lists of resources."""
    ml_mock = MagicMock()
    ml_mock.list_channels.return_value = {
        "Channels": [],
    }

    mp_mock = MagicMock()
    mp_mock.list_channel_groups.return_value = {
        "Items": [],
    }

    mt_mock = MagicMock()
    mt_mock.list_playback_configurations.return_value = {
        "Items": [],
    }

    cf_mock = MagicMock()
    cf_mock.list_distributions.return_value = {
        "DistributionList": {
            "Items": [],
            "IsTruncated": False,
        },
    }

    mock_boto3.side_effect = _discover_client_factory(
        ml_mock, mp_mock, mt_mock, cf_mock,
    )

    from lambdas.pipeline_logs.handler import (
        discover_resources,
    )
    result = discover_resources()

    assert result["MediaLive"] == []
    assert result["MediaPackage"] == []
    assert result["MediaTailor"] == []
    assert result["CloudFront"] == []
    # All four keys must be present
    assert set(result.keys()) == {
        "MediaLive",
        "MediaPackage",
        "MediaTailor",
        "CloudFront",
    }
