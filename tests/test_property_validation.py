# Feature: streaming-chatbot, Property 8: Validação de campos obrigatórios com log de rejeição
"""Property-based tests for required field validation with rejection logging.

**Validates: Requirements 11.3, 11.4, 11.5**

For any Config_Enriquecida or Evento_Estruturado with one or more
required fields removed, the validator must reject the record
(is_valid=False) and the errors list must identify every missing field.
"""

import hypothesis.strategies as st
from hypothesis import given, settings

from lambdas.shared.validators import (
    validate_config_enriquecida,
    validate_evento_estruturado,
)

# -----------------------------------------------------------------
# Valid base records
# -----------------------------------------------------------------

CONFIG_REQUIRED_FIELDS = {"channel_id", "servico", "tipo", "dados"}

EVENTO_REQUIRED_FIELDS = {
    "timestamp", "canal", "severidade", "tipo_erro", "descricao",
}


def _valid_config():
    """Return a fully valid Config_Enriquecida dict."""
    return {
        "channel_id": "ch-1057",
        "servico": "MediaLive",
        "tipo": "configuracao",
        "dados": {"nome": "Canal 1057"},
    }


def _valid_evento():
    """Return a fully valid Evento_Estruturado dict."""
    return {
        "timestamp": "2024-01-15T10:30:00Z",
        "canal": "1057",
        "severidade": "ERROR",
        "tipo_erro": "INPUT_LOSS",
        "descricao": "Input signal lost on channel",
    }


# -----------------------------------------------------------------
# Strategies — random non-empty subsets of required fields to remove
# -----------------------------------------------------------------

_config_fields_to_remove = st.sets(
    st.sampled_from(sorted(CONFIG_REQUIRED_FIELDS)),
    min_size=1,
)

_evento_fields_to_remove = st.sets(
    st.sampled_from(sorted(EVENTO_REQUIRED_FIELDS)),
    min_size=1,
)


# -----------------------------------------------------------------
# Property tests
# -----------------------------------------------------------------

@settings(max_examples=100)
@given(fields_to_remove=_config_fields_to_remove)
def test_config_with_missing_fields_is_rejected(fields_to_remove):
    """A Config_Enriquecida missing any required field is rejected.

    **Validates: Requirements 11.3**
    """
    record = _valid_config()
    for f in fields_to_remove:
        del record[f]

    result = validate_config_enriquecida(record)

    assert result.is_valid is False, (
        f"Expected rejection when removing {fields_to_remove}, "
        f"but got is_valid=True"
    )
    assert len(result.errors) >= len(fields_to_remove), (
        f"Expected at least {len(fields_to_remove)} errors "
        f"for missing {fields_to_remove}, got {result.errors}"
    )


@settings(max_examples=100)
@given(fields_to_remove=_evento_fields_to_remove)
def test_evento_with_missing_fields_is_rejected(fields_to_remove):
    """An Evento_Estruturado missing any required field is rejected.

    **Validates: Requirements 11.4**
    """
    record = _valid_evento()
    for f in fields_to_remove:
        del record[f]

    result = validate_evento_estruturado(record)

    assert result.is_valid is False, (
        f"Expected rejection when removing {fields_to_remove}, "
        f"but got is_valid=True"
    )
    assert len(result.errors) >= len(fields_to_remove), (
        f"Expected at least {len(fields_to_remove)} errors "
        f"for missing {fields_to_remove}, got {result.errors}"
    )


@settings(max_examples=100)
@given(fields_to_remove=_config_fields_to_remove)
def test_config_errors_identify_missing_fields(fields_to_remove):
    """Each removed Config field must appear in the error messages.

    **Validates: Requirements 11.5**
    """
    record = _valid_config()
    for f in fields_to_remove:
        del record[f]

    result = validate_config_enriquecida(record)
    all_errors = " ".join(result.errors)

    for field_name in fields_to_remove:
        assert field_name in all_errors, (
            f"Missing field '{field_name}' not mentioned in "
            f"errors: {result.errors}"
        )


@settings(max_examples=100)
@given(fields_to_remove=_evento_fields_to_remove)
def test_evento_errors_identify_missing_fields(fields_to_remove):
    """Each removed Evento field must appear in the error messages.

    **Validates: Requirements 11.5**
    """
    record = _valid_evento()
    for f in fields_to_remove:
        del record[f]

    result = validate_evento_estruturado(record)
    all_errors = " ".join(result.errors)

    for field_name in fields_to_remove:
        assert field_name in all_errors, (
            f"Missing field '{field_name}' not mentioned in "
            f"errors: {result.errors}"
        )
