"""Validation module for Config_Enriquecida and Evento_Estruturado.

Validates required fields, data types, and enum values before
records are stored in the KB_CONFIG or KB_LOGS S3 buckets.

Validates: Requirements 5.2, 7.2, 7.3, 10.4, 10.5, 11.1, 11.3, 11.4
"""

import logging
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)


SERVICOS_VALIDOS = {
    "MediaLive", "MediaPackage", "MediaTailor", "CloudFront",
}
TIPOS_VALIDOS = {"configuracao", "documentacao", "pratica"}
SEVERIDADES_VALIDAS = {"INFO", "WARNING", "ERROR", "CRITICAL"}
SERVICOS_ORIGEM_VALIDOS = {
    "MediaLive", "MediaPackage", "MediaTailor",
    "CloudFront", "CloudWatch",
}


@dataclass
class ValidationResult:
    """Result of a Config_Enriquecida validation."""

    is_valid: bool
    errors: List[str] = field(default_factory=list)


def validate_config_enriquecida(record: dict) -> ValidationResult:
    """Validate a Config_Enriquecida record.

    Checks:
    - channel_id: must be a non-empty string
    - servico: must be one of MediaLive, MediaPackage, MediaTailor, CloudFront
    - tipo: must be one of configuracao, documentacao, pratica
    - dados: must be a dict

    Returns a ValidationResult with is_valid=True when all checks pass,
    or is_valid=False with a list of error descriptions.
    """
    errors: List[str] = []

    if not isinstance(record, dict):
        return ValidationResult(is_valid=False, errors=["record must be a dict"])

    # --- channel_id ---
    channel_id = record.get("channel_id")
    if channel_id is None:
        errors.append("campo obrigatório ausente: channel_id")
    elif not isinstance(channel_id, str):
        errors.append("channel_id deve ser uma string")
    elif not channel_id.strip():
        errors.append("channel_id não pode ser vazio")

    # --- servico ---
    servico = record.get("servico")
    if servico is None:
        errors.append("campo obrigatório ausente: servico")
    elif not isinstance(servico, str):
        errors.append("servico deve ser uma string")
    elif servico not in SERVICOS_VALIDOS:
        errors.append(
            f"servico inválido: '{servico}'. Valores aceitos: {sorted(SERVICOS_VALIDOS)}"
        )

    # --- tipo ---
    tipo = record.get("tipo")
    if tipo is None:
        errors.append("campo obrigatório ausente: tipo")
    elif not isinstance(tipo, str):
        errors.append("tipo deve ser uma string")
    elif tipo not in TIPOS_VALIDOS:
        errors.append(
            f"tipo inválido: '{tipo}'. Valores aceitos: {sorted(TIPOS_VALIDOS)}"
        )

    # --- nome_canal (optional but useful) ---
    # No more 'dados' check — format is now flat

    return ValidationResult(is_valid=len(errors) == 0, errors=errors)


def validate_evento_estruturado(record: dict) -> ValidationResult:
    """Validate an Evento_Estruturado record.

    Checks:
    - timestamp: must be a non-empty string
    - canal: must be a non-empty string
    - severidade: must be INFO, WARNING, ERROR or CRITICAL
    - tipo_erro: must be a non-empty string
    - descricao: must be a non-empty string

    Returns a ValidationResult with is_valid=True when all checks
    pass, or is_valid=False with a list of error descriptions.
    """
    errors: List[str] = []

    if not isinstance(record, dict):
        return ValidationResult(
            is_valid=False,
            errors=["record must be a dict"],
        )

    # --- timestamp ---
    ts = record.get("timestamp")
    if ts is None:
        errors.append("campo obrigatório ausente: timestamp")
    elif not isinstance(ts, str):
        errors.append("timestamp deve ser uma string")
    elif not ts.strip():
        errors.append("timestamp não pode ser vazio")

    # --- canal ---
    canal = record.get("canal")
    if canal is None:
        errors.append("campo obrigatório ausente: canal")
    elif not isinstance(canal, str):
        errors.append("canal deve ser uma string")
    elif not canal.strip():
        errors.append("canal não pode ser vazio")

    # --- severidade ---
    sev = record.get("severidade")
    if sev is None:
        errors.append("campo obrigatório ausente: severidade")
    elif not isinstance(sev, str):
        errors.append("severidade deve ser uma string")
    elif sev not in SEVERIDADES_VALIDAS:
        errors.append(
            f"severidade inválida: '{sev}'. "
            f"Valores aceitos: {sorted(SEVERIDADES_VALIDAS)}"
        )

    # --- tipo_erro ---
    tipo_erro = record.get("tipo_erro")
    if tipo_erro is None:
        errors.append("campo obrigatório ausente: tipo_erro")
    elif not isinstance(tipo_erro, str):
        errors.append("tipo_erro deve ser uma string")
    elif not tipo_erro.strip():
        errors.append("tipo_erro não pode ser vazio")

    # --- descricao ---
    descricao = record.get("descricao")
    if descricao is None:
        errors.append("campo obrigatório ausente: descricao")
    elif not isinstance(descricao, str):
        errors.append("descricao deve ser uma string")
    elif not descricao.strip():
        errors.append("descricao não pode ser vazio")

    return ValidationResult(
        is_valid=len(errors) == 0, errors=errors,
    )


# --- Cross-contamination detection ---
# Validates: Requirements 10.4, 10.5

EVENTO_ESTRUTURADO_FIELDS = {
    "timestamp", "canal", "severidade", "tipo_erro", "descricao",
}
CONFIG_ENRIQUECIDA_FIELDS = {
    "channel_id", "servico", "tipo", "dados",
}


@dataclass
class CrossContaminationResult:
    """Result of a cross-contamination check."""

    is_contaminated: bool
    alert_message: str


def detect_cross_contamination(
    record: dict,
    target_bucket: str,
) -> CrossContaminationResult:
    """Detect if a record is being ingested into the wrong bucket.

    Args:
        record: The data record to check.
        target_bucket: Either ``"kb-config"`` or ``"kb-logs"``.

    Returns:
        A :class:`CrossContaminationResult` indicating whether
        contamination was detected and an alert message.
    """
    if not isinstance(record, dict):
        return CrossContaminationResult(
            is_contaminated=False,
            alert_message="",
        )

    record_keys = set(record.keys())

    if target_bucket == "kb-config":
        # Log data should NOT go into the config bucket.
        if EVENTO_ESTRUTURADO_FIELDS.issubset(record_keys):
            msg = (
                "Contaminação cruzada detectada: registro de "
                "Evento_Estruturado detectado no bucket kb-config"
            )
            logger.warning(msg)
            return CrossContaminationResult(
                is_contaminated=True,
                alert_message=msg,
            )

    elif target_bucket == "kb-logs":
        # Config data should NOT go into the logs bucket.
        if CONFIG_ENRIQUECIDA_FIELDS.issubset(record_keys):
            msg = (
                "Contaminação cruzada detectada: registro de "
                "Config_Enriquecida detectado no bucket kb-logs"
            )
            logger.warning(msg)
            return CrossContaminationResult(
                is_contaminated=True,
                alert_message=msg,
            )

    return CrossContaminationResult(
        is_contaminated=False,
        alert_message="",
    )
