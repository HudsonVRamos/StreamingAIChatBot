"""Unit tests for Pipeline de Configurações handler.

Validates: Requirements 5.1, 5.4, 11.2

Tests cover:
1. MediaLive extraction — mock client returns channel list + describe, verify S3 storage
2. MediaPackage extraction — mock client returns channels + endpoints, verify S3 storage
3. MediaTailor extraction — mock client returns playback configs, verify S3 storage
4. CloudFront extraction — mock client returns distributions, verify S3 storage
5. Individual API failure continues pipeline — one service raises, others proceed
6. Validation failure skips storage — invalid Config_Enriquecida is skipped and counted
7. Handler returns summary with stored count and errors
"""

import json
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_boto3_client_factory(**service_mocks):
    """Return a function that dispatches boto3.client(service) to mocks."""
    def _factory(service_name, **kwargs):
        return service_mocks.get(service_name, MagicMock())
    return _factory


def _make_s3_mock():
    """Return a fresh S3 mock client."""
    return MagicMock()


def _make_medialive_mock(channels=None, describe_response=None):
    """Build a MediaLive mock with list_channels and describe_channel."""
    mock = MagicMock()
    mock.list_channels.return_value = {
        "Channels": channels or [],
    }
    if describe_response is not None:
        mock.describe_channel.return_value = describe_response
    return mock


def _make_mediapackage_mock(channels=None, endpoints=None):
    """Build a MediaPackage mock with list_channels and list_origin_endpoints."""
    mock = MagicMock()
    mock.list_channels.return_value = {
        "Channels": channels or [],
    }
    mock.list_origin_endpoints.return_value = {
        "OriginEndpoints": endpoints or [],
    }
    return mock


def _make_mediatailor_mock(items=None, get_response=None):
    """Build a MediaTailor mock with list_playback_configurations and get."""
    mock = MagicMock()
    mock.list_playback_configurations.return_value = {
        "Items": items or [],
    }
    if get_response is not None:
        mock.get_playback_configuration.return_value = get_response
    return mock


def _make_cloudfront_mock(distributions=None, get_response=None):
    """Build a CloudFront mock with list_distributions and get_distribution."""
    mock = MagicMock()
    mock.list_distributions.return_value = {
        "DistributionList": {
            "Items": distributions or [],
            "IsTruncated": False,
        },
    }
    if get_response is not None:
        mock.get_distribution.return_value = get_response
    return mock


def _empty_service_mock(service):
    """Return a mock that returns empty lists for any service."""
    if service == "medialive":
        return _make_medialive_mock()
    if service == "mediapackage":
        return _make_mediapackage_mock()
    if service == "mediatailor":
        return _make_mediatailor_mock()
    if service == "cloudfront":
        return _make_cloudfront_mock()
    return MagicMock()


# ---------------------------------------------------------------------------
# 1. test_medialive_extraction
# ---------------------------------------------------------------------------

@patch.dict("os.environ", {"KB_CONFIG_BUCKET": "test-bucket", "KB_CONFIG_PREFIX": "kb-config/"})
@patch("boto3.client")
def test_medialive_extraction(mock_boto3_client):
    """Mock MediaLive client to return a channel list and describe_channel
    response. Verify the handler stores a normalized config in S3."""
    s3_mock = _make_s3_mock()
    medialive_mock = _make_medialive_mock(
        channels=[{"Id": "ch-100", "Name": "Canal100"}],
        describe_response={
            "Id": "ch-100",
            "Name": "Canal100",
            "State": "RUNNING",
            "Arn": "arn:aws:medialive:us-east-1:123456789012:channel:ch-100",
            "ChannelClass": "STANDARD",
            "EncoderSettings": {
                "VideoDescriptions": [{"Width": 1920, "Height": 1080, "CodecSettings": {"H264Settings": {"Bitrate": 5000000, "GopSize": 2, "GopSizeUnits": "SECONDS", "FramerateNumerator": 30, "FramerateDenominator": 1}}}],
                "AudioDescriptions": [{"CodecSettings": {"AacSettings": {}}}],
                "OutputGroups": [{"OutputGroupSettings": {"HlsGroupSettings": {"Destination": {"DestinationRefId": "dest1"}, "SegmentLength": 6}}}],
            },
            "InputAttachments": [],
            "ResponseMetadata": {"RequestId": "abc"},
        },
    )

    mock_boto3_client.side_effect = _make_boto3_client_factory(
        s3=s3_mock,
        medialive=medialive_mock,
        mediapackage=_empty_service_mock("mediapackage"),
        mediatailor=_empty_service_mock("mediatailor"),
        cloudfront=_empty_service_mock("cloudfront"),
    )

    from lambdas.pipeline_config.handler import handler
    handler({}, None)

    # Verify S3 put_object was called at least once for the MediaLive config
    assert s3_mock.put_object.call_count >= 1
    put_call = s3_mock.put_object.call_args_list[0]
    assert put_call.kwargs["Bucket"] == "test-bucket"
    assert "MediaLive" in put_call.kwargs["Key"]
    assert put_call.kwargs["ContentType"] == "application/json"

    # Verify the stored body is valid JSON with Config_Enriquecida structure
    stored_body = json.loads(put_call.kwargs["Body"])
    assert stored_body["channel_id"] == "ch-100"
    assert stored_body["servico"] == "MediaLive"
    assert stored_body["tipo"] == "configuracao"
    assert "dados" in stored_body
    assert stored_body["dados"]["codec_video"] == "H.264"


# ---------------------------------------------------------------------------
# 2. test_mediapackage_extraction
# ---------------------------------------------------------------------------

@patch.dict("os.environ", {"KB_CONFIG_BUCKET": "test-bucket", "KB_CONFIG_PREFIX": "kb-config/"})
@patch("boto3.client")
def test_mediapackage_extraction(mock_boto3_client):
    """Mock MediaPackage client to return channels and endpoints.
    Verify the handler stores a normalized config in S3."""
    s3_mock = _make_s3_mock()
    mediapackage_mock = _make_mediapackage_mock(
        channels=[{
            "Id": "mp-chan-1",
            "Arn": "arn:aws:mediapackage:us-east-1:123456789012:channels/mp-chan-1",
            "Description": "Test MP Channel",
            "HlsIngest": {"IngestEndpoints": [{"Id": "ep1", "Url": "https://ingest.example.com"}]},
        }],
        endpoints=[{
            "Id": "ep-1",
            "Url": "https://output.example.com/hls",
            "HlsPackage": {"SegmentDurationSeconds": 6},
        }],
    )

    mock_boto3_client.side_effect = _make_boto3_client_factory(
        s3=s3_mock,
        medialive=_empty_service_mock("medialive"),
        mediapackage=mediapackage_mock,
        mediatailor=_empty_service_mock("mediatailor"),
        cloudfront=_empty_service_mock("cloudfront"),
    )

    from lambdas.pipeline_config.handler import handler
    handler({}, None)

    assert s3_mock.put_object.call_count >= 1
    put_call = s3_mock.put_object.call_args_list[0]
    stored_body = json.loads(put_call.kwargs["Body"])
    assert stored_body["servico"] == "MediaPackage"
    assert stored_body["tipo"] == "configuracao"
    assert stored_body["channel_id"] == "mp-chan-1"


# ---------------------------------------------------------------------------
# 3. test_mediatailor_extraction
# ---------------------------------------------------------------------------

@patch.dict("os.environ", {"KB_CONFIG_BUCKET": "test-bucket", "KB_CONFIG_PREFIX": "kb-config/"})
@patch("boto3.client")
def test_mediatailor_extraction(mock_boto3_client):
    """Mock MediaTailor client to return playback configurations.
    Verify the handler stores a normalized config in S3."""
    s3_mock = _make_s3_mock()
    mediatailor_mock = _make_mediatailor_mock(
        items=[{"Name": "mt-config-1"}],
        get_response={
            "Name": "mt-config-1",
            "PlaybackConfigurationArn": "arn:aws:mediatailor:us-east-1:123456789012:playbackConfiguration/mt-config-1",
            "AdDecisionServerUrl": "https://ads.example.com",
            "VideoContentSourceUrl": "https://content.example.com",
            "CdnConfiguration": {"ContentSegmentUrlPrefix": "https://cdn.example.com"},
            "ResponseMetadata": {"RequestId": "xyz"},
        },
    )

    mock_boto3_client.side_effect = _make_boto3_client_factory(
        s3=s3_mock,
        medialive=_empty_service_mock("medialive"),
        mediapackage=_empty_service_mock("mediapackage"),
        mediatailor=mediatailor_mock,
        cloudfront=_empty_service_mock("cloudfront"),
    )

    from lambdas.pipeline_config.handler import handler
    handler({}, None)

    assert s3_mock.put_object.call_count >= 1
    put_call = s3_mock.put_object.call_args_list[0]
    stored_body = json.loads(put_call.kwargs["Body"])
    assert stored_body["servico"] == "MediaTailor"
    assert stored_body["tipo"] == "configuracao"
    assert stored_body["channel_id"] == "mt-config-1"
    assert stored_body["dados"]["ad_insertion"]["ad_decision_server_url"] == "https://ads.example.com"


# ---------------------------------------------------------------------------
# 4. test_cloudfront_extraction
# ---------------------------------------------------------------------------

@patch.dict("os.environ", {"KB_CONFIG_BUCKET": "test-bucket", "KB_CONFIG_PREFIX": "kb-config/"})
@patch("boto3.client")
def test_cloudfront_extraction(mock_boto3_client):
    """Mock CloudFront client to return distributions.
    Verify the handler stores a normalized config in S3."""
    s3_mock = _make_s3_mock()
    cloudfront_mock = _make_cloudfront_mock(
        distributions=[{"Id": "E1234ABCDEF"}],
        get_response={
            "Distribution": {
                "Id": "E1234ABCDEF",
                "Status": "Deployed",
                "DomainName": "d111111abcdef8.cloudfront.net",
                "DistributionConfig": {
                    "Comment": "Streaming CDN",
                    "Enabled": True,
                    "PriceClass": "PriceClass_100",
                    "Origins": {
                        "Items": [
                            {"Id": "origin-1", "DomainName": "origin.example.com", "OriginPath": "/live"},
                        ],
                    },
                    "DefaultCacheBehavior": {
                        "ViewerProtocolPolicy": "redirect-to-https",
                        "AllowedMethods": {"Items": ["GET", "HEAD"]},
                        "CachePolicyId": "policy-123",
                    },
                },
            },
            "ResponseMetadata": {"RequestId": "resp-1"},
        },
    )

    mock_boto3_client.side_effect = _make_boto3_client_factory(
        s3=s3_mock,
        medialive=_empty_service_mock("medialive"),
        mediapackage=_empty_service_mock("mediapackage"),
        mediatailor=_empty_service_mock("mediatailor"),
        cloudfront=cloudfront_mock,
    )

    from lambdas.pipeline_config.handler import handler
    handler({}, None)

    assert s3_mock.put_object.call_count >= 1
    put_call = s3_mock.put_object.call_args_list[0]
    stored_body = json.loads(put_call.kwargs["Body"])
    assert stored_body["servico"] == "CloudFront"
    assert stored_body["tipo"] == "configuracao"
    assert stored_body["channel_id"] == "E1234ABCDEF"
    assert stored_body["dados"]["cdn_distribution"]["domain_name"] == "d111111abcdef8.cloudfront.net"


# ---------------------------------------------------------------------------
# 5. test_individual_api_failure_continues
# ---------------------------------------------------------------------------

@patch.dict("os.environ", {"KB_CONFIG_BUCKET": "test-bucket", "KB_CONFIG_PREFIX": "kb-config/"})
@patch("boto3.client")
def test_individual_api_failure_continues(mock_boto3_client):
    """Mock one service (MediaLive) to raise an exception.
    Verify the handler continues with other services and records the error."""
    s3_mock = _make_s3_mock()

    # MediaLive will fail
    medialive_mock = MagicMock()
    medialive_mock.list_channels.side_effect = Exception("MediaLive API unavailable")

    # MediaPackage will succeed with one channel
    mediapackage_mock = _make_mediapackage_mock(
        channels=[{
            "Id": "mp-ok-1",
            "Arn": "arn:aws:mediapackage:us-east-1:123456789012:channels/mp-ok-1",
            "Description": "Working channel",
            "HlsIngest": {"IngestEndpoints": []},
        }],
        endpoints=[],
    )

    mock_boto3_client.side_effect = _make_boto3_client_factory(
        s3=s3_mock,
        medialive=medialive_mock,
        mediapackage=mediapackage_mock,
        mediatailor=_empty_service_mock("mediatailor"),
        cloudfront=_empty_service_mock("cloudfront"),
    )

    from lambdas.pipeline_config.handler import handler
    result = handler({}, None)

    body = result["body"]
    # MediaLive error should be recorded
    assert len(body["errors"]) >= 1
    ml_error = next(e for e in body["errors"] if e["service"] == "MediaLive")
    assert "MediaLive API unavailable" in ml_error["reason"]

    # MediaPackage config should still be stored
    assert body["stored"] >= 1


# ---------------------------------------------------------------------------
# 6. test_validation_failure_skips_storage
# ---------------------------------------------------------------------------

@patch.dict("os.environ", {"KB_CONFIG_BUCKET": "test-bucket", "KB_CONFIG_PREFIX": "kb-config/"})
@patch("boto3.client")
@patch("lambdas.pipeline_config.handler.normalize_medialive_config")
def test_validation_failure_skips_storage(mock_normalize, mock_boto3_client):
    """Mock a service to return data that produces an invalid Config_Enriquecida.
    Verify it's skipped and counted in skipped_validation."""
    s3_mock = _make_s3_mock()
    medialive_mock = _make_medialive_mock(
        channels=[{"Id": "ch-bad"}],
        describe_response={"Id": "ch-bad", "Name": "BadChannel"},
    )

    # Return an invalid Config_Enriquecida (missing required fields)
    mock_normalize.return_value = {
        "channel_id": "",  # empty channel_id -> validation fails
        "servico": "MediaLive",
        "tipo": "configuracao",
        "dados": {},
    }

    mock_boto3_client.side_effect = _make_boto3_client_factory(
        s3=s3_mock,
        medialive=medialive_mock,
        mediapackage=_empty_service_mock("mediapackage"),
        mediatailor=_empty_service_mock("mediatailor"),
        cloudfront=_empty_service_mock("cloudfront"),
    )

    from lambdas.pipeline_config.handler import handler
    result = handler({}, None)

    body = result["body"]
    assert body["skipped_validation"] >= 1
    # S3 should NOT have been called for this invalid record
    assert body["stored"] == 0


# ---------------------------------------------------------------------------
# 7. test_handler_returns_summary
# ---------------------------------------------------------------------------

@patch.dict("os.environ", {"KB_CONFIG_BUCKET": "test-bucket", "KB_CONFIG_PREFIX": "kb-config/"})
@patch("boto3.client")
def test_handler_returns_summary(mock_boto3_client):
    """Verify the handler returns a summary with stored count and errors."""
    s3_mock = _make_s3_mock()

    # One working MediaLive channel, one failing MediaTailor
    medialive_mock = _make_medialive_mock(
        channels=[{"Id": "ch-200"}],
        describe_response={
            "Id": "ch-200",
            "Name": "Canal200",
            "State": "IDLE",
            "Arn": "arn:aws:medialive:us-east-1:123456789012:channel:ch-200",
            "EncoderSettings": {"VideoDescriptions": [], "AudioDescriptions": [], "OutputGroups": []},
            "InputAttachments": [],
        },
    )

    mediatailor_mock = MagicMock()
    mediatailor_mock.list_playback_configurations.side_effect = Exception("Throttled")

    mock_boto3_client.side_effect = _make_boto3_client_factory(
        s3=s3_mock,
        medialive=medialive_mock,
        mediapackage=_empty_service_mock("mediapackage"),
        mediatailor=mediatailor_mock,
        cloudfront=_empty_service_mock("cloudfront"),
    )

    from lambdas.pipeline_config.handler import handler
    result = handler({}, None)

    assert result["statusCode"] == 200
    body = result["body"]
    assert "stored" in body
    assert "errors" in body
    assert "skipped_validation" in body
    assert "skipped_contamination" in body
    assert isinstance(body["stored"], int)
    assert isinstance(body["errors"], list)
    # At least one stored (MediaLive) and one error (MediaTailor)
    assert body["stored"] >= 1
    assert len(body["errors"]) >= 1
