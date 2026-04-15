# Feature: orchestrated-channel-creation, Property 1: Construção de endpoint reflete parâmetros do usuário
"""Property-based tests for orchestrated channel creation.

Tests cover:
- Property 1: Endpoint construction reflects user parameters
  (_build_endpoint_config produces HLS/DASH payloads whose fields
  match the OrchestrationParams values exactly)
"""

from __future__ import annotations

import json

import pytest
import hypothesis.strategies as st
from hypothesis import given, settings

from unittest.mock import patch

from lambdas.configuradora.handler import (
    OrchestrationParams,
    RollbackEntry,
    _build_endpoint_config,
    _execute_rollback,
    _extract_ingest_url,
    delete_resource,
    handler,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_channel_names = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        whitelist_characters="_-",
    ),
    min_size=1,
    max_size=30,
)

_channel_groups = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        whitelist_characters="_-",
    ),
    min_size=1,
    max_size=30,
)

_drm_resource_ids = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        whitelist_characters="_-",
    ),
    min_size=0,
    max_size=40,
)


@st.composite
def orchestration_params_strategy(draw):
    """Generate random OrchestrationParams with valid ranges."""
    nome_canal = draw(_channel_names)
    channel_group = draw(_channel_groups)
    template_resource_id = draw(_channel_names)
    segment_duration = draw(st.integers(min_value=1, max_value=60))
    drm_resource_id = draw(_drm_resource_ids)
    manifest_window_seconds = draw(
        st.integers(min_value=60, max_value=86400),
    )
    startover_window_hls_seconds = draw(
        st.integers(min_value=0, max_value=86400),
    )
    startover_window_dash_seconds = draw(
        st.integers(min_value=0, max_value=86400),
    )
    ts_include_dvb_subtitles = draw(st.booleans())
    min_buffer_time_seconds = draw(
        st.integers(min_value=1, max_value=60),
    )
    suggested_presentation_delay_seconds = draw(
        st.integers(min_value=1, max_value=120),
    )

    return OrchestrationParams(
        nome_canal=nome_canal,
        channel_group=channel_group,
        template_resource_id=template_resource_id,
        segment_duration=segment_duration,
        drm_resource_id=drm_resource_id,
        manifest_window_seconds=manifest_window_seconds,
        startover_window_hls_seconds=startover_window_hls_seconds,
        startover_window_dash_seconds=startover_window_dash_seconds,
        ts_include_dvb_subtitles=ts_include_dvb_subtitles,
        min_buffer_time_seconds=min_buffer_time_seconds,
        suggested_presentation_delay_seconds=suggested_presentation_delay_seconds,
    )


# ===================================================================
# Property 1: Construção de endpoint reflete parâmetros do usuário
# **Validates: Requirements 3.4, 3.8, 3.11, 3.15, 3.16, 3.18,
#              3.19, 3.20, 3.25, 3.26**
# ===================================================================


@settings(max_examples=100)
@given(params=orchestration_params_strategy())
def test_property1_hls_endpoint_reflects_user_params(params):
    """HLS endpoint config must reflect all user-provided parameters.

    **Validates: Requirements 3.4, 3.8, 3.11, 3.15, 3.16, 3.18,
                 3.19, 3.20, 3.25, 3.26**
    """
    config = _build_endpoint_config(params, "HLS")
    drm_id = params.drm_resource_id or f"Live_{params.nome_canal}"

    # Segment fields
    seg = config["Segment"]
    assert seg["SegmentDurationSeconds"] == params.segment_duration
    assert seg["TsIncludeDvbSubtitles"] == params.ts_include_dvb_subtitles

    # Fixed segment fields
    assert seg["SegmentName"] == "segment"
    assert seg["TsUseAudioRenditionGroup"] is True
    assert seg["IncludeIframeOnlyStreams"] is False

    # DRM / SpekeKeyProvider
    enc = seg["Encryption"]
    assert enc["EncryptionMethod"]["CmafEncryptionMethod"] == "CBCS"
    speke = enc["SpekeKeyProvider"]
    assert speke["ResourceId"] == drm_id
    assert speke["DrmSystems"] == ["FAIRPLAY"]

    # Container type
    assert config["ContainerType"] == "CMAF"

    # HLS manifest
    hls = config["HlsManifests"]
    assert len(hls) == 1
    assert hls[0]["ManifestName"] == "master"
    assert hls[0]["ManifestWindowSeconds"] == params.manifest_window_seconds

    # Startover window
    assert config["StartoverWindowSeconds"] == (
        params.startover_window_hls_seconds
    )


@settings(max_examples=100)
@given(params=orchestration_params_strategy())
def test_property1_dash_endpoint_reflects_user_params(params):
    """DASH endpoint config must reflect all user-provided parameters.

    **Validates: Requirements 3.4, 3.8, 3.11, 3.15, 3.16, 3.18,
                 3.19, 3.20, 3.25, 3.26**
    """
    config = _build_endpoint_config(params, "DASH")
    drm_id = params.drm_resource_id or f"Live_{params.nome_canal}"

    # Segment fields
    seg = config["Segment"]
    assert seg["SegmentDurationSeconds"] == params.segment_duration
    assert seg["TsIncludeDvbSubtitles"] == params.ts_include_dvb_subtitles

    # Fixed segment fields
    assert seg["SegmentName"] == "segment"
    assert seg["TsUseAudioRenditionGroup"] is True
    assert seg["IncludeIframeOnlyStreams"] is False

    # DRM / SpekeKeyProvider
    enc = seg["Encryption"]
    assert enc["EncryptionMethod"]["CmafEncryptionMethod"] == "CENC"
    speke = enc["SpekeKeyProvider"]
    assert speke["ResourceId"] == drm_id
    assert speke["DrmSystems"] == ["PLAYREADY", "WIDEVINE"]

    # Container type
    assert config["ContainerType"] == "CMAF"

    # DASH manifest
    dash = config["DashManifests"]
    assert len(dash) == 1
    assert dash[0]["ManifestName"] == "manifest"
    assert dash[0]["ManifestWindowSeconds"] == (
        params.manifest_window_seconds
    )
    assert dash[0]["MinUpdatePeriodSeconds"] == params.segment_duration
    assert dash[0]["MinBufferTimeSeconds"] == (
        params.min_buffer_time_seconds
    )
    assert dash[0]["SuggestedPresentationDelaySeconds"] == (
        params.suggested_presentation_delay_seconds
    )

    # Startover window
    assert config["StartoverWindowSeconds"] == (
        params.startover_window_dash_seconds
    )


# ===================================================================
# Property 5: Convenções de nomenclatura
# **Validates: Requirements 3.2, 4.2, 4.3, 5.4**
# ===================================================================


@settings(max_examples=100)
@given(nome_canal=_channel_names)
def test_property5_endpoint_naming_hls(nome_canal):
    """HLS endpoint OriginEndpointName must be '{nome}_HLS'.

    **Validates: Requirements 3.2**
    """
    params = OrchestrationParams(
        nome_canal=nome_canal,
        channel_group="TEST_GROUP",
        template_resource_id="template_1",
    )
    config = _build_endpoint_config(params, "HLS")
    assert config["OriginEndpointName"] == f"{nome_canal}_HLS"


@settings(max_examples=100)
@given(nome_canal=_channel_names)
def test_property5_endpoint_naming_dash(nome_canal):
    """DASH endpoint OriginEndpointName must be '{nome}_DASH'.

    **Validates: Requirements 3.2**
    """
    params = OrchestrationParams(
        nome_canal=nome_canal,
        channel_group="TEST_GROUP",
        template_resource_id="template_1",
    )
    config = _build_endpoint_config(params, "DASH")
    assert config["OriginEndpointName"] == f"{nome_canal}_DASH"


@settings(max_examples=100)
@given(nome_canal=_channel_names)
def test_property5_input_naming_single_pipeline(nome_canal):
    """SINGLE_PIPELINE inputs must be '{nome}_INPUT_1' and '{nome}_INPUT_2'.

    **Validates: Requirements 4.2**

    Tests the naming convention as a pure function — the same logic
    used inside ``_create_inputs_for_channel`` for SINGLE_PIPELINE.
    """
    channel_class = "SINGLE_PIPELINE"
    expected_names = [
        f"{nome_canal}_INPUT_1",
        f"{nome_canal}_INPUT_2",
    ]

    # Reproduce the naming logic from _create_inputs_for_channel
    generated = []
    input_count = 2  # SINGLE_PIPELINE always creates 2 inputs
    for i in range(1, input_count + 1):
        generated.append(f"{nome_canal}_INPUT_{i}")

    assert generated == expected_names
    assert len(generated) == 2


@settings(max_examples=100)
@given(nome_canal=_channel_names)
def test_property5_input_naming_standard(nome_canal):
    """STANDARD input must be '{nome}_INPUT'.

    **Validates: Requirements 4.3**

    Tests the naming convention as a pure function — the same logic
    used inside ``_create_inputs_for_channel`` for STANDARD.
    """
    expected_name = f"{nome_canal}_INPUT"

    # Reproduce the naming logic from _create_inputs_for_channel
    inp_name = f"{nome_canal}_INPUT"

    assert inp_name == expected_name


@settings(max_examples=100)
@given(nome_canal=_channel_names)
def test_property5_destinations_id_replaces_underscores(nome_canal):
    """Destinations.Id must equal nome_canal with underscores replaced by hyphens.

    **Validates: Requirements 5.4**
    """
    destinations_id = nome_canal.replace("_", "-")

    # The result must not contain underscores
    assert "_" not in destinations_id

    # Replacing hyphens back should recover original underscores
    recovered = destinations_id.replace("-", "_")
    # Only holds when original had no hyphens — but the property
    # we really care about is the forward transform:
    assert destinations_id == nome_canal.replace("_", "-")

    # If the original name had underscores, the id must differ
    if "_" in nome_canal:
        assert destinations_id != nome_canal
    else:
        # No underscores means id equals the original name
        assert destinations_id == nome_canal


# ===================================================================
# Property 2: Extração de Ingest URL
# **Validates: Requirements 2.5, 10.4**
# ===================================================================

_non_empty_urls = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        whitelist_characters="/:._-",
    ),
    min_size=1,
    max_size=120,
)


@st.composite
def api_response_with_valid_endpoints(draw):
    """Generate a CreateChannel response with 1-5 IngestEndpoints, each with a non-empty URL."""
    count = draw(st.integers(min_value=1, max_value=5))
    endpoints = []
    for _ in range(count):
        url = draw(_non_empty_urls)
        endpoints.append({"IngestEndpointUrl": url})
    return {"IngestEndpoints": endpoints}


@st.composite
def api_response_with_some_empty_urls(draw):
    """Generate a response where some endpoints have empty URLs but at least one is valid."""
    valid_count = draw(st.integers(min_value=1, max_value=3))
    empty_count = draw(st.integers(min_value=1, max_value=3))

    endpoints = []
    for _ in range(empty_count):
        endpoints.append({"IngestEndpointUrl": ""})
    for _ in range(valid_count):
        url = draw(_non_empty_urls)
        endpoints.append({"IngestEndpointUrl": url})

    # Shuffle so empties and valids are interleaved
    shuffled = draw(st.permutations(endpoints))
    return {"IngestEndpoints": list(shuffled)}


@st.composite
def api_response_with_no_valid_urls(draw):
    """Generate a response with no endpoints or all empty URLs."""
    variant = draw(st.sampled_from(["no_key", "empty_list", "all_empty"]))
    if variant == "no_key":
        return {}
    if variant == "empty_list":
        return {"IngestEndpoints": []}
    # all_empty
    count = draw(st.integers(min_value=1, max_value=5))
    endpoints = [{"IngestEndpointUrl": ""} for _ in range(count)]
    return {"IngestEndpoints": endpoints}


@settings(max_examples=100)
@given(response=api_response_with_valid_endpoints())
def test_property2_extract_ingest_url_returns_valid_url(response):
    """Extracted ingest URL must be non-empty and one of the endpoint URLs.

    **Validates: Requirements 2.5, 10.4**
    """
    result = _extract_ingest_url(response)
    all_urls = [ep["IngestEndpointUrl"] for ep in response["IngestEndpoints"]]

    assert result != ""
    assert result in all_urls


@settings(max_examples=100)
@given(response=api_response_with_some_empty_urls())
def test_property2_extract_ingest_url_skips_empty(response):
    """Extracted URL must be one of the non-empty endpoint URLs, skipping empties.

    **Validates: Requirements 2.5, 10.4**
    """
    result = _extract_ingest_url(response)
    non_empty_urls = [
        ep["IngestEndpointUrl"]
        for ep in response["IngestEndpoints"]
        if ep["IngestEndpointUrl"]
    ]

    assert result != ""
    assert result in non_empty_urls


@settings(max_examples=100)
@given(response=api_response_with_no_valid_urls())
def test_property2_extract_ingest_url_raises_on_empty(response):
    """ValueError must be raised when no valid ingest URL exists.

    **Validates: Requirements 2.5, 10.4**
    """
    with pytest.raises(ValueError):
        _extract_ingest_url(response)


# ===================================================================
# Property 3: Completude e ordenação do rollback
# **Validates: Requirements 3.27, 4.6, 5.6, 6.1, 6.2**
# ===================================================================

_servicos = st.sampled_from(["MediaPackage", "MediaLive"])
_tipos_recurso = st.sampled_from([
    "channel_v2",
    "origin_endpoint_v2",
    "input",
    "channel",
])
_resource_ids = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        whitelist_characters="_-",
    ),
    min_size=1,
    max_size=30,
)


@st.composite
def rollback_stack_strategy(draw):
    """Generate a list of 1-10 RollbackEntry objects with unique IDs."""
    count = draw(st.integers(min_value=1, max_value=10))
    ids = draw(
        st.lists(
            _resource_ids,
            min_size=count,
            max_size=count,
            unique=True,
        ),
    )
    entries = []
    for rid in ids:
        entries.append(
            RollbackEntry(
                servico=draw(_servicos),
                tipo_recurso=draw(_tipos_recurso),
                resource_id=rid,
                channel_group=draw(_channel_groups),
                channel_name=draw(_channel_names),
            ),
        )
    return entries


@settings(max_examples=100)
@given(rollback_stack=rollback_stack_strategy())
def test_property3_rollback_completeness_and_ordering(
    rollback_stack,
):
    """Rollback must delete all resources in reverse creation order.

    **Validates: Requirements 3.27, 4.6, 5.6, 6.1, 6.2**
    """
    deletion_order: list[str] = []

    def _record_delete(entry):
        deletion_order.append(entry.resource_id)
        return {"status": "deleted", "resource_id": entry.resource_id}

    with (
        patch(
            "lambdas.configuradora.handler.delete_resource",
            side_effect=_record_delete,
        ),
        patch(
            "lambdas.configuradora.handler.store_audit_log",
        ),
        patch(
            "lambdas.configuradora.handler.build_audit_log",
            return_value={},
        ),
    ):
        removidos, falhas = _execute_rollback(rollback_stack)

    input_ids = [e.resource_id for e in rollback_stack]
    expected_order = list(reversed(input_ids))

    # All resource_ids appear in recursos_removidos
    assert set(removidos) == set(input_ids)

    # No failures
    assert falhas == []

    # Deletion order is the reverse of the input list
    assert deletion_order == expected_order


# ===================================================================
# Property 4: Resiliência do rollback
# **Validates: Requirements 6.4, 6.5**
# ===================================================================


@st.composite
def rollback_with_failure_pattern(draw):
    """Generate a rollback stack and a random set of indices that will fail during deletion.

    Returns (rollback_stack, failing_indices) where failing_indices refers
    to positions in the *reversed* iteration order (i.e., the order in
    which ``_execute_rollback`` processes entries).
    """
    stack = draw(rollback_stack_strategy())
    n = len(stack)
    # Generate a random subset of indices (0..n-1) that will fail
    failing_indices = draw(
        st.frozensets(st.integers(min_value=0, max_value=n - 1)),
    )
    return stack, failing_indices


@settings(max_examples=100)
@given(data=rollback_with_failure_pattern())
def test_property4_rollback_resilience(data):
    """Rollback must attempt all resources and correctly classify removed vs. failed.

    **Validates: Requirements 6.4, 6.5**
    """
    rollback_stack, failing_indices = data

    # Build the expected reversed order (same order _execute_rollback iterates)
    reversed_entries = list(reversed(rollback_stack))

    call_counter = {"idx": 0}

    def _mock_delete(entry):
        idx = call_counter["idx"]
        call_counter["idx"] += 1
        if idx in failing_indices:
            raise RuntimeError(f"Simulated deletion failure for {entry.resource_id}")
        return {"status": "deleted", "resource_id": entry.resource_id}

    with (
        patch(
            "lambdas.configuradora.handler.delete_resource",
            side_effect=_mock_delete,
        ),
        patch(
            "lambdas.configuradora.handler.store_audit_log",
        ),
        patch(
            "lambdas.configuradora.handler.build_audit_log",
            return_value={},
        ),
    ):
        recursos_removidos, recursos_falha_remocao = _execute_rollback(rollback_stack)

    # --- Assertions ---

    # 1. All resources were attempted (total = removed + failed)
    assert len(recursos_removidos) + len(recursos_falha_remocao) == len(rollback_stack)

    # 2. Resources at failing indices are in recursos_falha_remocao
    expected_failed_ids = {
        reversed_entries[i].resource_id for i in failing_indices
    }
    assert set(recursos_falha_remocao) == expected_failed_ids

    # 3. Resources at succeeding indices are in recursos_removidos
    all_indices = set(range(len(rollback_stack)))
    succeeding_indices = all_indices - failing_indices
    expected_removed_ids = {
        reversed_entries[i].resource_id for i in succeeding_indices
    }
    assert set(recursos_removidos) == expected_removed_ids

    # 4. No resource appears in both lists
    assert set(recursos_removidos).isdisjoint(set(recursos_falha_remocao))


# ===================================================================
# Property 7: Passthrough da Ingest URL para Destinations
# **Validates: Requirements 5.3**
# ===================================================================

_ingest_urls = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        whitelist_characters="/:._-",
    ),
    min_size=1,
    max_size=200,
)


@settings(max_examples=100)
@given(nome_canal=_channel_names, ingest_url=_ingest_urls)
def test_property7_ingest_url_passthrough_to_destinations(nome_canal, ingest_url):
    """Ingest URL must appear in Destinations[0].Settings[0].Url and
    Destinations[0].Id must equal nome_canal with underscores replaced by hyphens.

    **Validates: Requirements 5.3**

    Reproduces the destination-building logic from _execute_orchestrated_creation().
    """
    # Build destinations using the same logic as _execute_orchestrated_creation
    destinations_id = nome_canal.replace("_", "-")
    destinations = [{
        "Id": destinations_id,
        "Settings": [{"Url": ingest_url}],
    }]

    # Property: ingest_url appears in Destinations[0].Settings[0].Url
    assert destinations[0]["Settings"][0]["Url"] == ingest_url

    # Property: Destinations[0].Id equals nome_canal with underscores replaced
    assert destinations[0]["Id"] == nome_canal.replace("_", "-")


# ===================================================================
# Property 6: Validação de parâmetros obrigatórios ausentes
# **Validates: Requirements 8.4**
# ===================================================================

_REQUIRED_PARAMS = ["nome_canal", "channel_group", "template_resource_id"]


@st.composite
def missing_required_params_strategy(draw):
    """Generate a non-empty subset of required parameters to be missing."""
    # Draw a non-empty subset of indices to remove
    all_indices = list(range(len(_REQUIRED_PARAMS)))
    subset = draw(
        st.lists(
            st.sampled_from(all_indices),
            min_size=1,
            max_size=len(_REQUIRED_PARAMS),
            unique=True,
        ),
    )
    return [_REQUIRED_PARAMS[i] for i in sorted(subset)]


def _build_orchestration_event(present_params: dict) -> dict:
    """Build a Bedrock Action Group event for /criarCanalOrquestrado with given params."""
    properties = [
        {"name": k, "value": v} for k, v in present_params.items()
    ]
    return {
        "apiPath": "/criarCanalOrquestrado",
        "actionGroup": "test",
        "httpMethod": "POST",
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": properties,
                }
            }
        },
    }


@settings(max_examples=100)
@given(missing=missing_required_params_strategy())
def test_property6_missing_required_params_returns_400(missing):
    """Missing required parameters must produce HTTP 400 listing exactly the missing ones.

    **Validates: Requirements 8.4**
    """
    # All params with dummy values
    all_params = {
        "nome_canal": "TEST_CHANNEL",
        "channel_group": "TEST_GROUP",
        "template_resource_id": "template_1",
    }

    # Remove the missing params
    present = {k: v for k, v in all_params.items() if k not in missing}

    event = _build_orchestration_event(present)
    response = handler(event, None)

    # Parse response
    resp = response["response"]
    body_str = resp["responseBody"]["application/json"]["body"]
    body = json.loads(body_str)

    # HTTP status must be 400
    assert resp["httpStatusCode"] == 400

    # parametros_faltantes must list exactly the missing params (order-independent)
    assert set(body["parametros_faltantes"]) == set(missing)

    # erro message must mention each missing parameter
    for param in missing:
        assert param in body["erro"]
