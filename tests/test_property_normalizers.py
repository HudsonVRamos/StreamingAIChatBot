# Feature: streaming-chatbot, Property 4: Normalização de configurações produz Config_Enriquecida válida
"""Property-based tests for configuration normalizers.

**Validates: Requirements 4.6, 5.2**

For any raw configuration extracted from MediaLive, MediaPackage,
MediaTailor or CloudFront APIs, the normalization function must
produce a valid Config_Enriquecida with all required fields
(channel_id, servico, tipo) and a dados dict.
"""

import hypothesis.strategies as st
from hypothesis import given, settings

from lambdas.shared.normalizers import (
    normalize_medialive_config,
    normalize_mediapackage_config,
    normalize_mediatailor_config,
    normalize_cloudfront_config,
)
from lambdas.shared.validators import validate_config_enriquecida


# -------------------------------------------------------------------
# Strategies — raw configs with at least one ID-like field
# -------------------------------------------------------------------

_non_empty_id = st.text(min_size=1, max_size=50).filter(
    lambda s: s.strip() != ""
)

_optional_text = st.one_of(st.none(), st.text(max_size=80))
_optional_int = st.one_of(st.none(), st.integers(min_value=0, max_value=10000))
_optional_dict = st.one_of(st.none(), st.fixed_dictionaries({}))


def _medialive_raw_config():
    """Strategy for random MediaLive DescribeChannel-like dicts."""
    return st.fixed_dictionaries(
        {"Id": _non_empty_id},
        optional={
            "Name": _optional_text,
            "State": st.one_of(
                st.none(),
                st.sampled_from([
                    "RUNNING", "IDLE", "STARTING", "STOPPING",
                    "CREATE_FAILED", "DELETED",
                ]),
            ),
            "Arn": st.one_of(
                st.none(),
                st.just(
                    "arn:aws:medialive:us-east-1:123456789:channel:1"
                ),
            ),
            "EncoderSettings": st.one_of(
                st.none(),
                st.fixed_dictionaries({
                    "VideoDescriptions": st.just([]),
                    "AudioDescriptions": st.just([]),
                    "OutputGroups": st.just([]),
                }),
            ),
            "InputAttachments": st.one_of(
                st.none(), st.just([]),
            ),
            "ChannelClass": st.one_of(
                st.none(),
                st.sampled_from(["STANDARD", "SINGLE_PIPELINE"]),
            ),
        },
    )


def _mediapackage_raw_config():
    """Strategy for random MediaPackage DescribeChannel-like dicts."""
    return st.fixed_dictionaries(
        {"Id": _non_empty_id},
        optional={
            "Arn": st.one_of(
                st.none(),
                st.just(
                    "arn:aws:mediapackage:us-east-1:123:channels/c1"
                ),
            ),
            "Description": _optional_text,
            "HlsIngest": st.one_of(
                st.none(),
                st.fixed_dictionaries({
                    "IngestEndpoints": st.just([]),
                }),
            ),
            "OriginEndpoints": st.one_of(
                st.none(), st.just([]),
            ),
        },
    )


def _mediatailor_raw_config():
    """Strategy for random MediaTailor GetPlaybackConfiguration-like dicts."""
    return st.fixed_dictionaries(
        {"Name": _non_empty_id},
        optional={
            "PlaybackConfigurationArn": st.one_of(
                st.none(),
                st.just(
                    "arn:aws:mediatailor:us-east-1:123:"
                    "playbackConfiguration/pc1"
                ),
            ),
            "AdDecisionServerUrl": _optional_text,
            "CdnConfiguration": st.one_of(
                st.none(),
                st.fixed_dictionaries({
                    "ContentSegmentUrlPrefix": _optional_text,
                }),
            ),
            "SlateAdUrl": _optional_text,
            "PersonalizationThresholdSeconds": _optional_int,
            "VideoContentSourceUrl": _optional_text,
        },
    )


def _cloudfront_raw_config():
    """Strategy for random CloudFront GetDistribution-like dicts."""
    dist_config = st.fixed_dictionaries(
        {},
        optional={
            "Comment": _optional_text,
            "Enabled": st.one_of(st.none(), st.booleans()),
            "PriceClass": st.one_of(
                st.none(),
                st.sampled_from([
                    "PriceClass_100",
                    "PriceClass_200",
                    "PriceClass_All",
                ]),
            ),
            "Origins": st.one_of(
                st.none(),
                st.fixed_dictionaries({"Items": st.just([])}),
            ),
            "DefaultCacheBehavior": st.one_of(
                st.none(), st.fixed_dictionaries({}),
            ),
        },
    )
    return st.fixed_dictionaries(
        {
            "Distribution": st.builds(
                lambda did, status, domain, dc: {
                    k: v for k, v in {
                        "Id": did,
                        "Status": status,
                        "DomainName": domain,
                        "DistributionConfig": dc,
                    }.items() if v is not None
                },
                did=_non_empty_id,
                status=st.one_of(
                    st.none(),
                    st.sampled_from(["Deployed", "InProgress"]),
                ),
                domain=_optional_text,
                dc=dist_config,
            ),
        },
    )


# -------------------------------------------------------------------
# Property tests
# -------------------------------------------------------------------

@settings(max_examples=100)
@given(raw=_medialive_raw_config())
def test_medialive_normalization_produces_valid_config(raw):
    """Any raw MediaLive config normalizes to a valid Config_Enriquecida."""
    result = normalize_medialive_config(raw)

    validation = validate_config_enriquecida(result)
    assert validation.is_valid, (
        f"Validation failed: {validation.errors} for raw={raw}"
    )
    assert result["servico"] == "MediaLive"
    assert result["tipo"] == "configuracao"


@settings(max_examples=100)
@given(raw=_mediapackage_raw_config())
def test_mediapackage_normalization_produces_valid_config(raw):
    """Any raw MediaPackage config normalizes to a valid Config_Enriquecida."""
    result = normalize_mediapackage_config(raw)

    validation = validate_config_enriquecida(result)
    assert validation.is_valid, (
        f"Validation failed: {validation.errors} for raw={raw}"
    )
    assert result["servico"] == "MediaPackage"
    assert result["tipo"] == "configuracao"


@settings(max_examples=100)
@given(raw=_mediatailor_raw_config())
def test_mediatailor_normalization_produces_valid_config(raw):
    """Any raw MediaTailor config normalizes to a valid Config_Enriquecida."""
    result = normalize_mediatailor_config(raw)

    validation = validate_config_enriquecida(result)
    assert validation.is_valid, (
        f"Validation failed: {validation.errors} for raw={raw}"
    )
    assert result["servico"] == "MediaTailor"
    assert result["tipo"] == "configuracao"


@settings(max_examples=100)
@given(raw=_cloudfront_raw_config())
def test_cloudfront_normalization_produces_valid_config(raw):
    """Any raw CloudFront config normalizes to a valid Config_Enriquecida."""
    result = normalize_cloudfront_config(raw)

    validation = validate_config_enriquecida(result)
    assert validation.is_valid, (
        f"Validation failed: {validation.errors} for raw={raw}"
    )
    assert result["servico"] == "CloudFront"
    assert result["tipo"] == "configuracao"
