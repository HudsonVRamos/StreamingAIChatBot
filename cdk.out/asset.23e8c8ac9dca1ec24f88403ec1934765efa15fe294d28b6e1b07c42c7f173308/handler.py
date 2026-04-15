"""Pipeline de Ingestão de Configurações.

Extrai configurações de canais e recursos de streaming via APIs AWS
(MediaLive, MediaPackage, MediaTailor, CloudFront), normaliza em
Config_Enriquecida, valida campos obrigatórios, verifica contaminação
cruzada e armazena no S3 (kb-config/).

Triggered by EventBridge scheduled event.

Validates: Requirements 5.1, 5.2, 5.3, 5.4
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3

from lambdas.shared.normalizers import (
    normalize_cloudfront_config,
    normalize_medialive_config,
    normalize_mediapackage_config,
    normalize_mediatailor_config,
)
from lambdas.shared.validators import (
    detect_cross_contamination,
    validate_config_enriquecida,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

def _get_bucket() -> str:
    return os.environ.get("KB_CONFIG_BUCKET", "")


def _get_prefix() -> str:
    return os.environ.get("KB_CONFIG_PREFIX", "kb-config/")


def handler(event: dict, context: Any) -> Dict[str, Any]:
    """Lambda handler triggered by EventBridge scheduled event.

    Extracts configurations from MediaLive, MediaPackage, MediaTailor
    and CloudFront, normalizes, validates and stores in S3.
    """
    logger.info("Pipeline de configurações iniciado")

    s3_client = boto3.client("s3")
    streaming_region = os.environ.get("STREAMING_REGION", "sa-east-1")
    medialive_client = boto3.client("medialive", region_name=streaming_region)
    mediapackage_client = boto3.client("mediapackage", region_name=streaming_region)
    mediatailor_client = boto3.client("mediatailor", region_name=streaming_region)
    cloudfront_client = boto3.client("cloudfront")

    results: Dict[str, Any] = {
        "stored": 0,
        "errors": [],
        "skipped_validation": 0,
        "skipped_contamination": 0,
    }

    # --- MediaLive ---
    _process_medialive(medialive_client, s3_client, results)

    # --- MediaPackage ---
    _process_mediapackage(mediapackage_client, s3_client, results)

    # --- MediaTailor ---
    _process_mediatailor(mediatailor_client, s3_client, results)

    # --- CloudFront ---
    _process_cloudfront(cloudfront_client, s3_client, results)

    logger.info(
        "Pipeline de configurações finalizado: %d armazenados, %d erros, "
        "%d rejeitados por validação, %d rejeitados por contaminação",
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


# ---------------------------------------------------------------------------
# Service processors
# ---------------------------------------------------------------------------


def _process_medialive(
    medialive_client: Any,
    s3_client: Any,
    results: Dict[str, Any],
) -> None:
    """List and describe all MediaLive channels, normalize and store."""
    try:
        channels = _paginate_medialive_channels(medialive_client)
    except Exception as exc:
        _record_error(results, "MediaLive", "list_channels", str(exc))
        return

    for summary in channels:
        channel_id = summary.get("Id", "unknown")
        try:
            detail = medialive_client.describe_channel(ChannelId=channel_id)
            # Remove ResponseMetadata injected by boto3
            detail.pop("ResponseMetadata", None)
            normalized = normalize_medialive_config(detail)
            _validate_and_store(s3_client, normalized, "MediaLive", channel_id, results)
        except Exception as exc:
            _record_error(results, "MediaLive", channel_id, str(exc))


def _process_mediapackage(
    mediapackage_client: Any,
    s3_client: Any,
    results: Dict[str, Any],
) -> None:
    """List MediaPackage channels and their origin endpoints, normalize and store."""
    try:
        channels = _paginate_mediapackage_channels(mediapackage_client)
    except Exception as exc:
        _record_error(results, "MediaPackage", "list_channels", str(exc))
        return

    for ch in channels:
        channel_id = ch.get("Id", "unknown")
        try:
            # Fetch origin endpoints for this channel
            endpoints = _paginate_mediapackage_endpoints(
                mediapackage_client, channel_id
            )
            # Build a combined config dict
            config = dict(ch)
            config["OriginEndpoints"] = endpoints
            normalized = normalize_mediapackage_config(config)
            _validate_and_store(s3_client, normalized, "MediaPackage", channel_id, results)
        except Exception as exc:
            _record_error(results, "MediaPackage", channel_id, str(exc))


def _process_mediatailor(
    mediatailor_client: Any,
    s3_client: Any,
    results: Dict[str, Any],
) -> None:
    """List and describe MediaTailor playback configurations, normalize and store."""
    try:
        configs = _paginate_mediatailor_configs(mediatailor_client)
    except Exception as exc:
        _record_error(results, "MediaTailor", "list_playback_configurations", str(exc))
        return

    for summary in configs:
        config_name = summary.get("Name", "unknown")
        try:
            detail = mediatailor_client.get_playback_configuration(Name=config_name)
            detail.pop("ResponseMetadata", None)
            normalized = normalize_mediatailor_config(detail)
            _validate_and_store(s3_client, normalized, "MediaTailor", config_name, results)
        except Exception as exc:
            _record_error(results, "MediaTailor", config_name, str(exc))


def _process_cloudfront(
    cloudfront_client: Any,
    s3_client: Any,
    results: Dict[str, Any],
) -> None:
    """List and describe CloudFront distributions, normalize and store."""
    try:
        distributions = _paginate_cloudfront_distributions(cloudfront_client)
    except Exception as exc:
        _record_error(results, "CloudFront", "list_distributions", str(exc))
        return

    for summary in distributions:
        dist_id = summary.get("Id", "unknown")
        try:
            detail = cloudfront_client.get_distribution(Id=dist_id)
            detail.pop("ResponseMetadata", None)
            normalized = normalize_cloudfront_config(detail)
            _validate_and_store(s3_client, normalized, "CloudFront", dist_id, results)
        except Exception as exc:
            _record_error(results, "CloudFront", dist_id, str(exc))


# ---------------------------------------------------------------------------
# Pagination helpers
# ---------------------------------------------------------------------------


def _paginate_medialive_channels(client: Any) -> List[dict]:
    """Paginate through all MediaLive channels."""
    channels: List[dict] = []
    params: Dict[str, Any] = {"MaxResults": 100}
    while True:
        resp = client.list_channels(**params)
        channels.extend(resp.get("Channels", []))
        next_token = resp.get("NextToken")
        if not next_token:
            break
        params["NextToken"] = next_token
    return channels


def _paginate_mediapackage_channels(client: Any) -> List[dict]:
    """Paginate through all MediaPackage channels."""
    channels: List[dict] = []
    params: Dict[str, Any] = {"MaxResults": 100}
    while True:
        resp = client.list_channels(**params)
        channels.extend(resp.get("Channels", []))
        next_token = resp.get("NextToken")
        if not next_token:
            break
        params["NextToken"] = next_token
    return channels


def _paginate_mediapackage_endpoints(
    client: Any, channel_id: str
) -> List[dict]:
    """Paginate through origin endpoints for a MediaPackage channel."""
    endpoints: List[dict] = []
    params: Dict[str, Any] = {"ChannelId": channel_id, "MaxResults": 100}
    while True:
        resp = client.list_origin_endpoints(**params)
        endpoints.extend(resp.get("OriginEndpoints", []))
        next_token = resp.get("NextToken")
        if not next_token:
            break
        params["NextToken"] = next_token
    return endpoints


def _paginate_mediatailor_configs(client: Any) -> List[dict]:
    """Paginate through all MediaTailor playback configurations."""
    configs: List[dict] = []
    params: Dict[str, Any] = {"MaxResults": 100}
    while True:
        resp = client.list_playback_configurations(**params)
        configs.extend(resp.get("Items", []))
        next_token = resp.get("NextToken")
        if not next_token:
            break
        params["NextToken"] = next_token
    return configs


def _paginate_cloudfront_distributions(client: Any) -> List[dict]:
    """Paginate through all CloudFront distributions."""
    distributions: List[dict] = []
    marker: str = ""
    while True:
        params: Dict[str, Any] = {"MaxItems": "100"}
        if marker:
            params["Marker"] = marker
        resp = client.list_distributions(**params)
        dist_list = resp.get("DistributionList", {})
        items = dist_list.get("Items", [])
        distributions.extend(items if items else [])
        if dist_list.get("IsTruncated") and dist_list.get("NextMarker"):
            marker = dist_list["NextMarker"]
        else:
            break
    return distributions


# ---------------------------------------------------------------------------
# Validation, contamination check and S3 storage
# ---------------------------------------------------------------------------


def _validate_and_store(
    s3_client: Any,
    normalized: Dict[str, Any],
    service: str,
    resource_id: str,
    results: Dict[str, Any],
) -> None:
    """Validate a normalized config, check contamination, and store in S3."""
    # Validate required fields
    validation = validate_config_enriquecida(normalized)
    if not validation.is_valid:
        logger.warning(
            "Validação falhou para %s/%s: %s",
            service,
            resource_id,
            validation.errors,
        )
        results["skipped_validation"] += 1
        return

    # Cross-contamination check
    contamination = detect_cross_contamination(normalized, "kb-config")
    if contamination.is_contaminated:
        logger.warning(
            "Contaminação cruzada detectada para %s/%s: %s",
            service,
            resource_id,
            contamination.alert_message,
        )
        results["skipped_contamination"] += 1
        return

    # Store in S3
    _store_config(s3_client, normalized, service, resource_id)
    results["stored"] += 1


def _store_config(
    s3_client: Any,
    config: Dict[str, Any],
    service: str,
    resource_id: str,
) -> None:
    """Store a validated Config_Enriquecida as JSON in S3."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_id = str(resource_id).replace("/", "_")
    bucket = _get_bucket()
    prefix = _get_prefix()
    key = f"{prefix}{service}/{safe_id}_{timestamp}.json"

    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(config, ensure_ascii=False, default=str),
        ContentType="application/json",
    )
    logger.info("Configuração armazenada: s3://%s/%s", bucket, key)


# ---------------------------------------------------------------------------
# Error recording
# ---------------------------------------------------------------------------


def _record_error(
    results: Dict[str, Any],
    service: str,
    resource_id: str,
    reason: str,
) -> None:
    """Log and record an extraction error for a specific resource."""
    logger.error(
        "Falha na extração de configuração - servico=%s, recurso=%s, motivo=%s",
        service,
        resource_id,
        reason,
    )
    results["errors"].append(
        {
            "service": service,
            "resource_id": resource_id,
            "reason": reason,
        }
    )
