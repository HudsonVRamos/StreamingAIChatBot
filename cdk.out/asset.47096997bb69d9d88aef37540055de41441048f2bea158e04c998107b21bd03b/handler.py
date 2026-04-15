"""Pipeline de Ingestão de Logs.

Coleta logs do CloudWatch (MediaLive, MediaPackage, MediaTailor,
CloudFront), normaliza em Evento_Estruturado, enriquece com causa
provável / impacto / recomendação, valida campos obrigatórios,
verifica contaminação cruzada e armazena no S3 (kb-logs/).

Triggered by EventBridge scheduled event.

Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

import boto3

from lambdas.shared.normalizers import (
    normalize_cloudwatch_log,
    enrich_evento,
)
from lambdas.shared.validators import (
    detect_cross_contamination,
    validate_evento_estruturado,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

KB_LOGS_BUCKET = os.environ.get("KB_LOGS_BUCKET", "")
KB_LOGS_PREFIX = os.environ.get("KB_LOGS_PREFIX", "kb-logs/")

# Comma-separated list of CloudWatch log group names.
# Falls back to conventional defaults when not set.
_DEFAULT_LOG_GROUPS = (
    "/aws/medialive/channel,"
    "/aws/mediapackage/channel,"
    "/aws/mediatailor/config,"
    "/aws/cloudfront/distribution"
)

# Map log-group prefix → service name used in Evento_Estruturado
_SERVICE_MAP: Dict[str, str] = {
    "medialive": "MediaLive",
    "mediapackage": "MediaPackage",
    "mediatailor": "MediaTailor",
    "cloudfront": "CloudFront",
}


def _resolve_log_groups() -> List[str]:
    """Return the list of CloudWatch log groups to query."""
    raw = os.environ.get("LOG_GROUPS", "")
    if raw.strip():
        return [g.strip() for g in raw.split(",") if g.strip()]
    return [g.strip() for g in _DEFAULT_LOG_GROUPS.split(",") if g.strip()]


def _detect_service(log_group: str) -> str:
    """Derive the service name from a log group path."""
    lower = log_group.lower()
    for key, service in _SERVICE_MAP.items():
        if key in lower:
            return service
    return "CloudWatch"


def handler(event: dict, context: Any) -> Dict[str, Any]:
    """Lambda handler triggered by EventBridge scheduled event.

    Collects logs from CloudWatch for MediaLive, MediaPackage,
    MediaTailor and CloudFront, normalizes, enriches, validates
    and stores in S3.
    """
    logger.info("Pipeline de logs iniciado")

    logs_client = boto3.client("logs")
    s3_client = boto3.client("s3")

    results: Dict[str, Any] = {
        "stored": 0,
        "errors": [],
        "skipped_validation": 0,
        "skipped_contamination": 0,
    }

    log_groups = _resolve_log_groups()

    for log_group in log_groups:
        service = _detect_service(log_group)
        try:
            _process_log_group(
                logs_client, s3_client,
                log_group, service, results,
            )
        except Exception as exc:
            _record_error(results, service, log_group, str(exc))

    logger.info(
        "Pipeline de logs finalizado: %d armazenados, %d erros, "
        "%d rejeitados por validação, "
        "%d rejeitados por contaminação",
        results["stored"],
        len(results["errors"]),
        results["skipped_validation"],
        results["skipped_contamination"],
    )

    return {
        "statusCode": 200,
        "body": {
            "stored": results["stored"],
            "errors": results["errors"],
            "skipped_validation": results["skipped_validation"],
            "skipped_contamination": results["skipped_contamination"],
        },
    }


# -------------------------------------------------------------------
# Log group processing
# -------------------------------------------------------------------


def _process_log_group(
    logs_client: Any,
    s3_client: Any,
    log_group: str,
    service: str,
    results: Dict[str, Any],
) -> None:
    """Collect recent logs from a single CloudWatch log group."""
    now = datetime.now(timezone.utc)
    start_ms = int((now - timedelta(hours=1)).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    events = _paginate_log_events(
        logs_client, log_group, start_ms, end_ms,
    )

    for raw_event in events:
        try:
            # Attach log group / stream metadata
            raw_event.setdefault("logGroupName", log_group)
            normalized = normalize_cloudwatch_log(
                raw_event, service,
            )
            enriched = enrich_evento(normalized)
            _validate_and_store(
                s3_client, enriched, service,
                log_group, results,
            )
        except Exception as exc:
            _record_error(
                results, service, log_group, str(exc),
            )


def _paginate_log_events(
    logs_client: Any,
    log_group: str,
    start_ms: int,
    end_ms: int,
) -> List[dict]:
    """Paginate through filter_log_events for a log group."""
    events: List[dict] = []
    params: Dict[str, Any] = {
        "logGroupName": log_group,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 100,
    }
    while True:
        resp = logs_client.filter_log_events(**params)
        events.extend(resp.get("events", []))
        next_token = resp.get("nextToken")
        if not next_token:
            break
        params["nextToken"] = next_token
    return events


# -------------------------------------------------------------------
# Validation, contamination check and S3 storage
# -------------------------------------------------------------------


def _validate_and_store(
    s3_client: Any,
    enriched: Dict[str, Any],
    service: str,
    log_group: str,
    results: Dict[str, Any],
) -> None:
    """Validate an enriched event, check contamination, store."""
    validation = validate_evento_estruturado(enriched)
    if not validation.is_valid:
        logger.warning(
            "Validação falhou para %s/%s: %s",
            service, log_group, validation.errors,
        )
        results["skipped_validation"] += 1
        return

    contamination = detect_cross_contamination(
        enriched, "kb-logs",
    )
    if contamination.is_contaminated:
        logger.warning(
            "Contaminação cruzada detectada para %s/%s: %s",
            service, log_group, contamination.alert_message,
        )
        results["skipped_contamination"] += 1
        return

    _store_event(s3_client, enriched, service, log_group)
    results["stored"] += 1


def _store_event(
    s3_client: Any,
    event: Dict[str, Any],
    service: str,
    log_group: str,
) -> None:
    """Store a validated Evento_Estruturado as JSON in S3."""
    timestamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ",
    )
    safe_group = log_group.replace("/", "_").lstrip("_")
    key = (
        f"{KB_LOGS_PREFIX}{service}/"
        f"{safe_group}_{timestamp}.json"
    )

    s3_client.put_object(
        Bucket=KB_LOGS_BUCKET,
        Key=key,
        Body=json.dumps(
            event, ensure_ascii=False, default=str,
        ),
        ContentType="application/json",
    )
    logger.info(
        "Evento armazenado: s3://%s/%s",
        KB_LOGS_BUCKET, key,
    )


# -------------------------------------------------------------------
# Error recording
# -------------------------------------------------------------------


def _record_error(
    results: Dict[str, Any],
    service: str,
    resource_id: str,
    reason: str,
) -> None:
    """Log and record a collection error."""
    logger.error(
        "Falha na coleta de logs - servico=%s, "
        "recurso=%s, motivo=%s",
        service, resource_id, reason,
    )
    results["errors"].append(
        {
            "service": service,
            "resource_id": resource_id,
            "reason": reason,
        }
    )
