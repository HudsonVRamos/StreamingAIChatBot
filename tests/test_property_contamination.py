# Feature: streaming-chatbot, Property 7: Prevenção de contaminação cruzada entre bases de conhecimento
"""Property-based tests for cross-contamination prevention.

**Validates: Requirements 6.4, 10.4, 10.5**

For any record, if it has the structure of an Evento_Estruturado
(log), it must be rejected when ingested into the kb-config bucket;
and if it has the structure of a Config_Enriquecida (configuration),
it must be rejected when ingested into the kb-logs bucket. In both
cases an alert message must be produced.
"""

import hypothesis.strategies as st
from hypothesis import given, settings

from lambdas.shared.validators import detect_cross_contamination

# -----------------------------------------------------------------
# Strategies — generate valid records with all required fields
# -----------------------------------------------------------------

_servicos = st.sampled_from([
    "MediaLive", "MediaPackage", "MediaTailor", "CloudFront",
])
_tipos = st.sampled_from(["configuracao", "documentacao", "pratica"])
_severidades = st.sampled_from([
    "INFO", "WARNING", "ERROR", "CRITICAL",
])
_non_empty_str = st.text(min_size=1, max_size=50).filter(
    lambda s: s.strip() != ""
)


@st.composite
def evento_estruturado(draw):
    """Generate a valid Evento_Estruturado with all 5 required fields."""
    return {
        "timestamp": draw(_non_empty_str),
        "canal": draw(_non_empty_str),
        "severidade": draw(_severidades),
        "tipo_erro": draw(_non_empty_str),
        "descricao": draw(_non_empty_str),
    }


@st.composite
def config_enriquecida(draw):
    """Generate a valid Config_Enriquecida with all 4 required fields."""
    return {
        "channel_id": draw(_non_empty_str),
        "servico": draw(_servicos),
        "tipo": draw(_tipos),
        "dados": {"key": draw(_non_empty_str)},
    }


# -----------------------------------------------------------------
# Property tests
# -----------------------------------------------------------------

@settings(max_examples=100)
@given(record=evento_estruturado())
def test_evento_in_config_bucket_detected(record):
    """An Evento_Estruturado in kb-config must be flagged as contaminated.

    **Validates: Requirements 10.4**
    """
    result = detect_cross_contamination(record, "kb-config")

    assert result.is_contaminated is True, (
        "Expected contamination when Evento_Estruturado "
        "targets kb-config"
    )
    assert result.alert_message, (
        "Expected non-empty alert_message for contamination"
    )


@settings(max_examples=100)
@given(record=config_enriquecida())
def test_config_in_logs_bucket_detected(record):
    """A Config_Enriquecida in kb-logs must be flagged as contaminated.

    **Validates: Requirements 10.5**
    """
    result = detect_cross_contamination(record, "kb-logs")

    assert result.is_contaminated is True, (
        "Expected contamination when Config_Enriquecida "
        "targets kb-logs"
    )
    assert result.alert_message, (
        "Expected non-empty alert_message for contamination"
    )


@settings(max_examples=100)
@given(record=config_enriquecida())
def test_config_in_config_bucket_not_contaminated(record):
    """A Config_Enriquecida in kb-config must NOT be flagged.

    **Validates: Requirements 6.4**
    """
    result = detect_cross_contamination(record, "kb-config")

    assert result.is_contaminated is False, (
        "Config_Enriquecida in kb-config should not be "
        "flagged as contaminated"
    )


@settings(max_examples=100)
@given(record=evento_estruturado())
def test_evento_in_logs_bucket_not_contaminated(record):
    """An Evento_Estruturado in kb-logs must NOT be flagged.

    **Validates: Requirements 6.4**
    """
    result = detect_cross_contamination(record, "kb-logs")

    assert result.is_contaminated is False, (
        "Evento_Estruturado in kb-logs should not be "
        "flagged as contaminated"
    )
