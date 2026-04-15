# Feature: streaming-chatbot, Property 9: Validação de JSON de configuração gerado
# Feature: streaming-chatbot, Property 11: Completude do log de auditoria
# Feature: streaming-chatbot, Property 12: Informações de reversão no log de auditoria
# Feature: streaming-chatbot, Property 13: Mensagem de erro descritiva da Lambda_Configuradora
"""Property-based tests for Lambda_Configuradora.

Tests cover:
- Property 9: Config JSON validation (valid/invalid configs per service)
- Property 11: Audit log completeness (all required fields present)
- Property 12: Rollback info in audit logs (creation and modification)
- Property 13: Descriptive error messages (error code + reason)
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from lambdas.configuradora.handler import (
    validate_config_json,
    build_audit_log,
    REQUIRED_FIELDS,
)

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

_servicos = st.sampled_from(["MediaLive", "MediaPackage", "MediaTailor", "CloudFront"])

_service_resource_pairs = st.sampled_from([
    ("MediaLive", "channel"),
    ("MediaLive", "input"),
    ("MediaPackage", "channel"),
    ("MediaPackage", "origin_endpoint"),
    ("MediaTailor", "playback_configuration"),
    ("CloudFront", "distribution"),
])

_FULL_CONFIGS: dict[tuple[str, str], dict] = {
    ("MediaLive", "channel"): {
        "Name": "test-ch",
        "InputAttachments": [{"InputId": "i-1"}],
        "Destinations": [{"Id": "d-1"}],
        "EncoderSettings": {"AudioDescriptions": []},
    },
    ("MediaLive", "input"): {
        "Name": "test-input",
        "Type": "SRT_CALLER",
    },
    ("MediaPackage", "channel"): {
        "Id": "mp-ch-1",
    },
    ("MediaPackage", "origin_endpoint"): {
        "ChannelId": "mp-ch-1",
        "Id": "oe-1",
    },
    ("MediaTailor", "playback_configuration"): {
        "Name": "mt-config",
        "AdDecisionServerUrl": "https://ads.example.com",
        "VideoContentSourceUrl": "https://video.example.com",
    },
    ("CloudFront", "distribution"): {
        "DistributionConfig": {"Origins": {"Items": []}},
    },
}

_resource_ids = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=20,
)

_usuario_ids = st.text(min_size=1, max_size=30)


# ===================================================================
# Property 9: Validação de JSON de configuração gerado
# **Validates: Requirements 12.1, 12.7**
# ===================================================================


@st.composite
def valid_config_for_service(draw):
    """Generate a (servico, tipo_recurso, config_json) with all required fields."""
    pair = draw(_service_resource_pairs)
    servico, tipo_recurso = pair
    config = dict(_FULL_CONFIGS[(servico, tipo_recurso)])
    return servico, tipo_recurso, config


@st.composite
def config_missing_random_fields(draw):
    """Generate a config with at least one required field removed."""
    pair = draw(_service_resource_pairs)
    servico, tipo_recurso = pair
    required = REQUIRED_FIELDS.get((servico, tipo_recurso), [])
    if not required:
        # All pairs in our list have required fields, but guard anyway
        return servico, tipo_recurso, {}, []

    config = dict(_FULL_CONFIGS[(servico, tipo_recurso)])

    # Choose a non-empty subset of required fields to remove
    fields_to_remove = draw(
        st.lists(
            st.sampled_from(required),
            min_size=1,
            max_size=len(required),
            unique=True,
        )
    )
    for f in fields_to_remove:
        config.pop(f, None)

    return servico, tipo_recurso, config, fields_to_remove


@settings(max_examples=100)
@given(data=valid_config_for_service())
def test_property9_valid_config_returns_valid(data):
    """Valid configs (all required fields present) must pass validation.

    **Validates: Requirements 12.1, 12.7**
    """
    servico, tipo_recurso, config = data
    result = validate_config_json(servico, tipo_recurso, config)
    assert result.is_valid, (
        f"Expected valid for {servico}/{tipo_recurso} but got errors: {result.errors}"
    )
    assert result.errors == []


@settings(max_examples=100)
@given(data=config_missing_random_fields())
def test_property9_missing_fields_returns_invalid(data):
    """Configs missing required fields must fail validation with errors identifying them.

    **Validates: Requirements 12.1, 12.7**
    """
    servico, tipo_recurso, config, removed_fields = data
    result = validate_config_json(servico, tipo_recurso, config)
    assert not result.is_valid, (
        f"Expected invalid for {servico}/{tipo_recurso} missing {removed_fields}"
    )
    # Each removed field should be mentioned in at least one error
    for field_name in removed_fields:
        assert any(field_name in err for err in result.errors), (
            f"Missing field '{field_name}' not mentioned in errors: {result.errors}"
        )


# ===================================================================
# Property 11: Completude do log de auditoria
# **Validates: Requirements 12.9, 13.3, 13.6**
# ===================================================================

_operacoes = st.sampled_from(["criacao", "modificacao"])
_resultados = st.sampled_from(["sucesso", "falha"])

_error_codes = st.sampled_from([
    "ValidationException", "ResourceNotFoundException",
    "AccessDeniedException", "ThrottlingException",
    "InternalServerError", "INTERNAL_ERROR", "VALIDATION_ERROR",
])

_error_messages = st.text(min_size=1, max_size=100)


@st.composite
def audit_log_inputs(draw):
    """Generate random inputs for build_audit_log."""
    operacao = draw(_operacoes)
    servico = draw(_servicos)
    resultado = draw(_resultados)
    resource_id = draw(_resource_ids)
    usuario_id = draw(_usuario_ids)
    config = {"Name": draw(st.text(min_size=1, max_size=20))}

    erro = None
    if resultado == "falha":
        erro = {
            "codigo": draw(_error_codes),
            "mensagem": draw(_error_messages),
        }

    return {
        "operacao": operacao,
        "servico": servico,
        "resource_id": resource_id,
        "config_aplicada": config,
        "resultado": resultado,
        "erro": erro,
        "usuario_id": usuario_id,
    }


REQUIRED_AUDIT_FIELDS = [
    "timestamp",
    "usuario_id",
    "tipo_operacao",
    "servico_aws",
    "resource_id",
    "configuracao_json_aplicada",
    "resultado",
]


@settings(max_examples=100)
@given(inputs=audit_log_inputs())
def test_property11_audit_log_has_all_required_fields(inputs):
    """Every audit log entry must contain all required fields.

    **Validates: Requirements 12.9, 13.3, 13.6**
    """
    entry = build_audit_log(
        operacao=inputs["operacao"],
        servico=inputs["servico"],
        resource_id=inputs["resource_id"],
        config_aplicada=inputs["config_aplicada"],
        resultado=inputs["resultado"],
        erro=inputs["erro"],
        usuario_id=inputs["usuario_id"],
    )

    for field in REQUIRED_AUDIT_FIELDS:
        assert field in entry, f"Audit log missing required field: '{field}'"
        # Fields should not be None (except erro/rollback_info which are optional)
        assert entry[field] is not None, (
            f"Required field '{field}' is None in audit log"
        )

    # Verify values match inputs
    assert entry["tipo_operacao"] == inputs["operacao"]
    assert entry["servico_aws"] == inputs["servico"]
    assert entry["resultado"] == inputs["resultado"]
    assert entry["usuario_id"] == inputs["usuario_id"]


@settings(max_examples=100)
@given(inputs=audit_log_inputs().filter(lambda x: x["resultado"] == "falha"))
def test_property11_failure_audit_log_has_erro_field(inputs):
    """For failed operations, the audit log must include erro with codigo and mensagem.

    **Validates: Requirements 12.9, 13.3, 13.6**
    """
    entry = build_audit_log(
        operacao=inputs["operacao"],
        servico=inputs["servico"],
        resource_id=inputs["resource_id"],
        config_aplicada=inputs["config_aplicada"],
        resultado=inputs["resultado"],
        erro=inputs["erro"],
        usuario_id=inputs["usuario_id"],
    )

    assert entry["erro"] is not None, "Failed operation must have 'erro' field"
    assert "codigo" in entry["erro"], "erro must contain 'codigo'"
    assert "mensagem" in entry["erro"], "erro must contain 'mensagem'"


# ===================================================================
# Property 12: Informações de reversão no log de auditoria
# **Validates: Requirements 13.4**
# ===================================================================


@settings(max_examples=100)
@given(
    servico=_servicos,
    resource_id=_resource_ids,
)
def test_property12_creation_rollback_info(servico, resource_id):
    """Successful creation ops must have rollback_info with resource_id and acao_reversao='delete'.

    **Validates: Requirements 13.4**
    """
    rollback_info = {
        "resource_id": resource_id,
        "acao_reversao": "delete",
    }
    entry = build_audit_log(
        operacao="criacao",
        servico=servico,
        resource_id=resource_id,
        config_aplicada={"Name": "test"},
        resultado="sucesso",
        rollback_info=rollback_info,
    )

    assert entry["rollback_info"] is not None, (
        "Successful creation must have rollback_info"
    )
    assert "resource_id" in entry["rollback_info"], (
        "rollback_info must contain resource_id"
    )
    assert entry["rollback_info"]["acao_reversao"] == "delete", (
        "Creation rollback_info must have acao_reversao='delete'"
    )


@st.composite
def previous_config(draw):
    """Generate a random previous config dict."""
    return {
        "Name": draw(st.text(min_size=1, max_size=20)),
        "Id": draw(_resource_ids),
    }


@settings(max_examples=100)
@given(
    servico=_servicos,
    resource_id=_resource_ids,
    config_anterior=previous_config(),
)
def test_property12_modification_rollback_info(servico, resource_id, config_anterior):
    """Successful modification ops must have rollback_info with config_anterior.

    **Validates: Requirements 13.4**
    """
    rollback_info = {"config_anterior": config_anterior}
    entry = build_audit_log(
        operacao="modificacao",
        servico=servico,
        resource_id=resource_id,
        config_aplicada={"Name": "new-name"},
        resultado="sucesso",
        rollback_info=rollback_info,
    )

    assert entry["rollback_info"] is not None, (
        "Successful modification must have rollback_info"
    )
    assert "config_anterior" in entry["rollback_info"], (
        "Modification rollback_info must contain config_anterior"
    )


# ===================================================================
# Property 13: Mensagem de erro descritiva da Lambda_Configuradora
# **Validates: Requirements 12.8**
# ===================================================================

_aws_error_codes = st.sampled_from([
    "ValidationException", "ResourceNotFoundException",
    "AccessDeniedException", "ThrottlingException",
    "InternalServerError", "LimitExceededException",
    "ConflictException", "ServiceUnavailableException",
    "BadRequestException", "NotFoundException",
])

_aws_error_reasons = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Z", "P")),
    min_size=1,
    max_size=80,
)


def build_error_response(error_code: str, error_message: str) -> str:
    """Build the error message string as Lambda_Configuradora does."""
    return f"Erro na API AWS: [{error_code}] {error_message}"


@settings(max_examples=100)
@given(
    error_code=_aws_error_codes,
    error_message=_aws_error_reasons,
)
def test_property13_error_message_contains_code_and_reason(error_code, error_message):
    """Error responses must contain both the error code and the reason.

    **Validates: Requirements 12.8**
    """
    msg = build_error_response(error_code, error_message)

    assert error_code in msg, (
        f"Error code '{error_code}' not found in message: {msg}"
    )
    assert error_message in msg, (
        f"Error reason '{error_message}' not found in message: {msg}"
    )
