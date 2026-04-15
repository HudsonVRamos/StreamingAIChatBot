# Feature: streaming-chatbot, Property 5: Normalização de logs produz Evento_Estruturado válido
"""Property-based tests for log normalization.

**Validates: Requirements 7.2**

For any raw CloudWatch log entry, the normalization and enrichment
pipeline must produce a valid Evento_Estruturado with all required
fields (timestamp, canal, severidade, tipo_erro, descricao) plus
the three enrichment fields (causa_provavel, impacto_estimado,
recomendacao_correcao).
"""

import hypothesis.strategies as st
from hypothesis import given, settings

from lambdas.shared.normalizers import (
    normalize_cloudwatch_log,
    enrich_evento,
)
from lambdas.shared.validators import (
    validate_evento_estruturado,
    SERVICOS_ORIGEM_VALIDOS,
)


# -------------------------------------------------------------------
# Strategies — raw CloudWatch log entries
# -------------------------------------------------------------------

_ERROR_KEYWORDS = [
    "INPUT_LOSS", "BITRATE_DROP", "LATENCY_SPIKE",
    "OUTPUT_FAIL", "ENCODER_ERROR",
    "AD_INSERTION_FAIL", "CDN_DISTRIBUTION_ERROR",
    "CDN_ORIGIN_ERROR", "CDN_CACHE_ERROR",
]

_SEVERITY_KEYWORDS = ["ERROR", "WARNING", "CRITICAL", "INFO"]

_servico_origem = st.sampled_from(sorted(SERVICOS_ORIGEM_VALIDOS))


def _log_message():
    """Strategy for random log messages.

    May contain error keywords and/or severity keywords mixed
    with arbitrary text.
    """
    prefix = st.one_of(
        st.just(""),
        st.sampled_from(_SEVERITY_KEYWORDS),
    )
    body = st.text(min_size=0, max_size=120)
    suffix = st.one_of(
        st.just(""),
        st.sampled_from(_ERROR_KEYWORDS),
    )
    return st.builds(
        lambda p, b, s: f"{p} {b} {s}".strip(),
        prefix, body, suffix,
    )


def _log_stream_name():
    """Strategy for logStreamName with '/' separators."""
    parts = st.lists(
        st.text(min_size=1, max_size=20).filter(
            lambda s: "/" not in s
        ),
        min_size=0,
        max_size=4,
    )
    return parts.map(lambda ps: "/".join(ps))


def _timestamp_value():
    """Strategy for timestamp: epoch ms (int) or ISO string."""
    epoch_ms = st.integers(
        min_value=0,
        max_value=4_102_444_800_000,  # ~2100-01-01
    )
    iso_str = st.sampled_from([
        "2024-01-15T10:30:00Z",
        "2023-12-01T00:00:00Z",
        "2025-06-20T18:45:12Z",
    ])
    return st.one_of(epoch_ms, iso_str)


def _raw_cloudwatch_log():
    """Strategy for a raw CloudWatch log entry dict."""
    base = st.fixed_dictionaries(
        {
            "timestamp": _timestamp_value(),
            "message": _log_message(),
            "logStreamName": _log_stream_name(),
            "logGroupName": _log_stream_name(),
        },
        optional={
            "channel_id": st.one_of(
                st.none(),
                st.text(min_size=1, max_size=30),
            ),
        },
    )
    return base


# -------------------------------------------------------------------
# Property test
# -------------------------------------------------------------------

@settings(max_examples=100)
@given(raw=_raw_cloudwatch_log(), servico=_servico_origem)
def test_log_normalization_produces_valid_evento(raw, servico):
    """Any raw CloudWatch log normalizes + enriches to a valid Evento_Estruturado.

    Steps:
    1. Normalize the raw log via normalize_cloudwatch_log
    2. Enrich the result via enrich_evento
    3. Validate with validate_evento_estruturado
    4. Assert is_valid=True and enrichment fields present
    """
    normalized = normalize_cloudwatch_log(raw, servico)
    enriched = enrich_evento(normalized)

    validation = validate_evento_estruturado(enriched)
    assert validation.is_valid, (
        f"Validation failed: {validation.errors} "
        f"for raw={raw}, servico={servico}"
    )

    # Enrichment fields must be present and non-empty
    assert "causa_provavel" in enriched
    assert "impacto_estimado" in enriched
    assert "recomendacao_correcao" in enriched
    assert isinstance(enriched["causa_provavel"], str)
    assert isinstance(enriched["impacto_estimado"], str)
    assert isinstance(enriched["recomendacao_correcao"], str)
    assert enriched["causa_provavel"]
    assert enriched["impacto_estimado"]
    assert enriched["recomendacao_correcao"]
