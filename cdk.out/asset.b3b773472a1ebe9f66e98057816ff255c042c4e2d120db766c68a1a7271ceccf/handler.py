"""Lambda Configuradora — creates and modifies streaming resources.

Invoked by the Bedrock Action_Group_Config to execute AWS API calls for
MediaLive, MediaPackage, MediaTailor and CloudFront resources.  Every
operation (success or failure) is recorded as an audit log entry in S3.
"""

from __future__ import annotations

import json
import os
import uuid
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AUDIT_BUCKET = os.environ.get("AUDIT_BUCKET", "")
AUDIT_PREFIX = os.environ.get("AUDIT_PREFIX", "audit/")

s3_client = boto3.client("s3")
medialive_client = boto3.client("medialive")
mediapackage_client = boto3.client("mediapackage")
mediatailor_client = boto3.client("mediatailor")
cloudfront_client = boto3.client("cloudfront")

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

VALID_SERVICOS = {"MediaLive", "MediaPackage", "MediaTailor", "CloudFront"}

VALID_TIPOS_RECURSO = {
    "MediaLive": {"channel", "input"},
    "MediaPackage": {"channel", "origin_endpoint"},
    "MediaTailor": {"playback_configuration"},
    "CloudFront": {"distribution"},
}

# Required fields per (servico, tipo_recurso) for creation
REQUIRED_FIELDS: dict[tuple[str, str], list[str]] = {
    ("MediaLive", "channel"): ["Name", "InputAttachments", "Destinations", "EncoderSettings"],
    ("MediaLive", "input"): ["Name", "Type"],
    ("MediaPackage", "channel"): ["Id"],
    ("MediaPackage", "origin_endpoint"): ["ChannelId", "Id"],
    ("MediaTailor", "playback_configuration"): ["Name", "AdDecisionServerUrl", "VideoContentSourceUrl"],
    ("CloudFront", "distribution"): ["DistributionConfig"],
}

ENUM_FIELDS: dict[str, list[str]] = {
    "Type": [
        "UDP_PUSH", "RTP_PUSH", "RTMP_PUSH", "RTMP_PULL",
        "URL_PULL", "MP4_FILE", "MEDIACONNECT", "INPUT_DEVICE",
        "AWS_CDI", "TS_FILE", "SRT_CALLER",
    ],
    "Codec": ["H_264", "H_265", "MPEG2"],
    "Resolution": ["SD", "HD", "FHD", "UHD"],
}


@dataclass
class ValidationResult:
    is_valid: bool = True
    errors: list[str] = field(default_factory=list)


def validate_config_json(
    servico: str,
    tipo_recurso: str,
    config_json: dict[str, Any],
    *,
    check_required: bool = True,
) -> ValidationResult:
    """Validate configuration JSON for the target service/resource type.

    Args:
        check_required: When False, skip required-field checks (useful
            for update operations where only changed fields are sent).
    """
    result = ValidationResult()

    if servico not in VALID_SERVICOS:
        result.is_valid = False
        result.errors.append(f"Serviço inválido: '{servico}'. Válidos: {sorted(VALID_SERVICOS)}")
        return result

    valid_tipos = VALID_TIPOS_RECURSO.get(servico, set())
    if tipo_recurso not in valid_tipos:
        result.is_valid = False
        result.errors.append(
            f"Tipo de recurso inválido '{tipo_recurso}' para {servico}. "
            f"Válidos: {sorted(valid_tipos)}"
        )
        return result

    if not isinstance(config_json, dict):
        result.is_valid = False
        result.errors.append("configuracao_json deve ser um objeto JSON (dict)")
        return result

    # Check required fields (only for creation)
    if check_required:
        key = (servico, tipo_recurso)
        required = REQUIRED_FIELDS.get(key, [])
        for field_name in required:
            if field_name not in config_json:
                result.is_valid = False
                result.errors.append(
                    f"Campo obrigatório ausente: '{field_name}'"
                )

    # Check enum values
    for field_name, valid_values in ENUM_FIELDS.items():
        if field_name in config_json:
            val = config_json[field_name]
            if isinstance(val, str) and val not in valid_values:
                result.is_valid = False
                result.errors.append(
                    f"Valor inválido para '{field_name}': '{val}'. "
                    f"Válidos: {valid_values}"
                )

    return result


# ---------------------------------------------------------------------------
# AWS API helpers — create
# ---------------------------------------------------------------------------


def create_resource(
    servico: str, tipo_recurso: str, config_json: dict[str, Any]
) -> dict[str, Any]:
    """Dispatch creation to the correct boto3 client method."""
    if servico == "MediaLive":
        if tipo_recurso == "channel":
            resp = medialive_client.create_channel(**config_json)
            channel = resp.get("Channel", {})
            return {"resource_id": channel.get("Id", ""), "details": channel}
        elif tipo_recurso == "input":
            resp = medialive_client.create_input(**config_json)
            inp = resp.get("Input", {})
            return {"resource_id": inp.get("Id", ""), "details": inp}

    elif servico == "MediaPackage":
        if tipo_recurso == "channel":
            resp = mediapackage_client.create_channel(**config_json)
            return {"resource_id": resp.get("Id", ""), "details": resp}
        elif tipo_recurso == "origin_endpoint":
            resp = mediapackage_client.create_origin_endpoint(**config_json)
            return {"resource_id": resp.get("Id", ""), "details": resp}

    elif servico == "MediaTailor":
        if tipo_recurso == "playback_configuration":
            resp = mediatailor_client.put_playback_configuration(**config_json)
            return {"resource_id": resp.get("Name", ""), "details": resp}

    elif servico == "CloudFront":
        if tipo_recurso == "distribution":
            resp = cloudfront_client.create_distribution(**config_json)
            dist = resp.get("Distribution", {})
            return {"resource_id": dist.get("Id", ""), "details": dist}

    raise ValueError(f"Operação de criação não suportada: {servico}/{tipo_recurso}")


# ---------------------------------------------------------------------------
# AWS API helpers — get current config (for rollback)
# ---------------------------------------------------------------------------


def get_current_config(
    servico: str, tipo_recurso: str, resource_id: str
) -> dict[str, Any]:
    """Retrieve the current configuration of a resource before modification."""
    if servico == "MediaLive":
        if tipo_recurso == "channel":
            return medialive_client.describe_channel(ChannelId=resource_id)
        elif tipo_recurso == "input":
            return medialive_client.describe_input(InputId=resource_id)

    elif servico == "MediaPackage":
        if tipo_recurso == "channel":
            return mediapackage_client.describe_channel(Id=resource_id)
        elif tipo_recurso == "origin_endpoint":
            return mediapackage_client.describe_origin_endpoint(Id=resource_id)

    elif servico == "MediaTailor":
        if tipo_recurso == "playback_configuration":
            return mediatailor_client.get_playback_configuration(Name=resource_id)

    elif servico == "CloudFront":
        if tipo_recurso == "distribution":
            return cloudfront_client.get_distribution_config(Id=resource_id)

    return {}


# ---------------------------------------------------------------------------
# AWS API helpers — update
# ---------------------------------------------------------------------------


def update_resource(
    servico: str, tipo_recurso: str, resource_id: str, config_json: dict[str, Any]
) -> dict[str, Any]:
    """Dispatch update to the correct boto3 client method."""
    if servico == "MediaLive":
        if tipo_recurso == "channel":
            resp = medialive_client.update_channel(
                ChannelId=resource_id, **config_json
            )
            return {"resource_id": resource_id, "details": resp.get("Channel", {})}
        elif tipo_recurso == "input":
            resp = medialive_client.update_input(
                InputId=resource_id, **config_json
            )
            return {"resource_id": resource_id, "details": resp.get("Input", {})}

    elif servico == "MediaPackage":
        if tipo_recurso == "origin_endpoint":
            resp = mediapackage_client.update_origin_endpoint(
                Id=resource_id, **config_json
            )
            return {"resource_id": resource_id, "details": resp}

    elif servico == "MediaTailor":
        if tipo_recurso == "playback_configuration":
            config_json["Name"] = resource_id
            resp = mediatailor_client.put_playback_configuration(**config_json)
            return {"resource_id": resource_id, "details": resp}

    elif servico == "CloudFront":
        if tipo_recurso == "distribution":
            # UpdateDistribution requires the ETag from GetDistributionConfig
            current = cloudfront_client.get_distribution_config(Id=resource_id)
            etag = current.get("ETag", "")
            resp = cloudfront_client.update_distribution(
                Id=resource_id, IfMatch=etag, **config_json
            )
            return {"resource_id": resource_id, "details": resp.get("Distribution", {})}

    raise ValueError(f"Operação de modificação não suportada: {servico}/{tipo_recurso}")


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def build_audit_log(
    *,
    operacao: str,
    servico: str,
    tipo_recurso: str = "",
    resource_id: str | None = None,
    config_aplicada: dict[str, Any] | None = None,
    resultado: str,
    erro: dict[str, str] | None = None,
    rollback_info: dict[str, Any] | None = None,
    usuario_id: str = "bedrock-agent",
) -> dict[str, Any]:
    """Build a structured audit log entry."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usuario_id": usuario_id,
        "tipo_operacao": operacao,
        "servico_aws": servico,
        "tipo_recurso": tipo_recurso,
        "resource_id": resource_id or "",
        "configuracao_json_aplicada": config_aplicada or {},
        "resultado": resultado,
        "erro": erro,
        "rollback_info": rollback_info,
    }


def store_audit_log(entry: dict[str, Any]) -> None:
    """Persist an audit log entry to S3_Audit."""
    now = datetime.now(timezone.utc)
    date_prefix = now.strftime("%Y/%m/%d")
    operation_id = uuid.uuid4().hex[:12]
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    key = f"{AUDIT_PREFIX}{date_prefix}/{ts}-{operation_id}.json"

    try:
        s3_client.put_object(
            Bucket=AUDIT_BUCKET,
            Key=key,
            Body=json.dumps(entry, ensure_ascii=False, default=str),
            ContentType="application/json",
        )
        logger.info("Audit log stored: s3://%s/%s", AUDIT_BUCKET, key)
    except ClientError as exc:
        logger.error("Failed to store audit log: %s", exc)


# ---------------------------------------------------------------------------
# Bedrock Action Group response helpers
# ---------------------------------------------------------------------------


def _bedrock_response(event: dict, status: int, body: dict) -> dict:
    """Build a response in the Bedrock Action Group format."""
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup", ""),
            "apiPath": event.get("apiPath", ""),
            "httpMethod": event.get("httpMethod", "POST"),
            "httpStatusCode": status,
            "responseBody": {
                "application/json": {
                    "body": json.dumps(body, ensure_ascii=False, default=str)
                }
            },
        },
    }


def _parse_parameters(event: dict) -> dict[str, Any]:
    """Extract parameters from a Bedrock Action Group event.

    Parameters may arrive as a list of {name, value} dicts under
    ``requestBody.content['application/json'].properties`` or directly
    under ``parameters``.
    """
    params: dict[str, Any] = {}

    # Try requestBody first (POST actions)
    try:
        props = (
            event.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("properties", [])
        )
        if isinstance(props, list):
            for prop in props:
                name = prop.get("name", "")
                value = prop.get("value")
                if name:
                    params[name] = value
    except (AttributeError, TypeError):
        pass

    # Also check top-level parameters (query/path params)
    top_params = event.get("parameters", [])
    if isinstance(top_params, list):
        for p in top_params:
            name = p.get("name", "")
            value = p.get("value")
            if name and name not in params:
                params[name] = value

    # Parse configuracao_json if it's a string
    if "configuracao_json" in params and isinstance(params["configuracao_json"], str):
        try:
            params["configuracao_json"] = json.loads(params["configuracao_json"])
        except (json.JSONDecodeError, TypeError):
            pass  # will be caught by validation

    return params


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    """Lambda handler invoked by Bedrock Action_Group_Config."""
    logger.info("Received event: %s", json.dumps(event, default=str))

    api_path = event.get("apiPath", "")
    parameters = _parse_parameters(event)

    servico = parameters.get("servico", "")
    tipo_recurso = parameters.get("tipo_recurso", "")
    config_json = parameters.get("configuracao_json", {})

    # --- Validate config JSON ---
    if not isinstance(config_json, dict):
        return _bedrock_response(event, 400, {
            "erro": "configuracao_json deve ser um objeto JSON válido"
        })

    is_modification = api_path == "/modificarRecurso"
    validation = validate_config_json(
        servico, tipo_recurso, config_json,
        check_required=not is_modification,
    )
    if not validation.is_valid:
        audit_entry = build_audit_log(
            operacao=api_path,
            servico=servico,
            tipo_recurso=tipo_recurso,
            resource_id=parameters.get("resource_id"),
            config_aplicada=config_json,
            resultado="falha",
            erro={"codigo": "VALIDATION_ERROR", "mensagem": "; ".join(validation.errors)},
        )
        store_audit_log(audit_entry)
        return _bedrock_response(event, 400, {
            "erro": f"JSON inválido: {'; '.join(validation.errors)}"
        })

    # --- Execute operation ---
    try:
        if api_path == "/criarRecurso":
            result = create_resource(servico, tipo_recurso, config_json)
            audit_entry = build_audit_log(
                operacao="criacao",
                servico=servico,
                tipo_recurso=tipo_recurso,
                resource_id=result.get("resource_id", ""),
                config_aplicada=config_json,
                resultado="sucesso",
                rollback_info={
                    "resource_id": result.get("resource_id", ""),
                    "acao_reversao": "delete",
                },
            )
            store_audit_log(audit_entry)
            return _bedrock_response(event, 200, {
                "mensagem": f"Recurso criado com sucesso: {result.get('resource_id', '')}",
                "resource_id": result.get("resource_id", ""),
            })

        elif api_path == "/modificarRecurso":
            resource_id = parameters.get("resource_id", "")
            if not resource_id:
                return _bedrock_response(event, 400, {
                    "erro": "resource_id é obrigatório para modificação"
                })

            # Get current config for rollback
            config_anterior = get_current_config(servico, tipo_recurso, resource_id)

            result = update_resource(servico, tipo_recurso, resource_id, config_json)
            audit_entry = build_audit_log(
                operacao="modificacao",
                servico=servico,
                tipo_recurso=tipo_recurso,
                resource_id=resource_id,
                config_aplicada=config_json,
                resultado="sucesso",
                rollback_info={"config_anterior": config_anterior},
            )
            store_audit_log(audit_entry)
            return _bedrock_response(event, 200, {
                "mensagem": f"Recurso modificado com sucesso: {resource_id}",
                "resource_id": resource_id,
            })

        else:
            return _bedrock_response(event, 400, {
                "erro": f"apiPath não reconhecido: {api_path}"
            })

    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "UnknownError")
        error_msg = exc.response.get("Error", {}).get("Message", str(exc))
        logger.error("AWS API error [%s]: %s", error_code, error_msg)

        audit_entry = build_audit_log(
            operacao=api_path,
            servico=servico,
            tipo_recurso=tipo_recurso,
            resource_id=parameters.get("resource_id"),
            config_aplicada=config_json,
            resultado="falha",
            erro={"codigo": error_code, "mensagem": error_msg},
        )
        store_audit_log(audit_entry)
        return _bedrock_response(event, 500, {
            "erro": f"Erro na API AWS: [{error_code}] {error_msg}"
        })

    except Exception as exc:
        logger.error("Unexpected error: %s", exc)
        audit_entry = build_audit_log(
            operacao=api_path,
            servico=servico,
            tipo_recurso=tipo_recurso,
            resource_id=parameters.get("resource_id"),
            config_aplicada=config_json,
            resultado="falha",
            erro={"codigo": "INTERNAL_ERROR", "mensagem": str(exc)},
        )
        store_audit_log(audit_entry)
        return _bedrock_response(event, 500, {
            "erro": f"Erro interno: {exc}"
        })
