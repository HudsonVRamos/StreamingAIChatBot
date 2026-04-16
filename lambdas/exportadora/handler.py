"""Lambda Exportadora — exports filtered data from KB buckets.

Invoked by the Bedrock Action_Group_Export to query S3 data from
KB_CONFIG and KB_LOGS, apply filters, format as CSV/JSON, upload
to S3_Exports and generate pre-signed URLs for download.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError
from botocore.config import Config as BotoConfig

logger = logging.getLogger()
logger.setLevel(logging.INFO)

KB_CONFIG_BUCKET = os.environ.get("KB_CONFIG_BUCKET", "")
KB_CONFIG_PREFIX = os.environ.get("KB_CONFIG_PREFIX", "kb-config/")
KB_LOGS_BUCKET = os.environ.get("KB_LOGS_BUCKET", "")
KB_LOGS_PREFIX = os.environ.get("KB_LOGS_PREFIX", "kb-logs/")
KB_ADS_BUCKET = os.environ.get("KB_ADS_BUCKET", "")
KB_ADS_PREFIX = os.environ.get("KB_ADS_PREFIX", "kb-ads/")
EXPORTS_BUCKET = os.environ.get("EXPORTS_BUCKET", "")
EXPORTS_PREFIX = os.environ.get("EXPORTS_PREFIX", "exports/")
PRESIGNED_URL_EXPIRY = int(
    os.environ.get("PRESIGNED_URL_EXPIRY", "3600")
)

# DynamoDB table names (empty = disabled / fallback to S3)
CONFIGS_TABLE_NAME = os.environ.get("CONFIGS_TABLE_NAME", "")
LOGS_TABLE_NAME = os.environ.get("LOGS_TABLE_NAME", "")

s3_client = boto3.client(
    "s3",
    config=BotoConfig(max_pool_connections=25),
)

# Separate client for pre-signed URLs with SigV4
s3_presign = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
    config=BotoConfig(signature_version="s3v4"),
)

# DynamoDB resource for query operations
dynamodb_resource = boto3.resource("dynamodb")

# -------------------------------------------------------------------
# Default columns per export type
# -------------------------------------------------------------------

CONFIG_DEFAULT_COLUMNS = [
    "channel_id", "nome_canal", "servico", "tipo", "estado", "regiao",
]

MEDIALIVE_COLUMNS = CONFIG_DEFAULT_COLUMNS + [
    "channel_class", "codec_video", "gop_size", "gop_unit", "framerate",
    "resolucoes", "resolucao_count", "resolucao_max",
    "bitrates", "bitrate_max",
    "audio_count", "audio_1_name", "audio_1_language", "audio_1_codec", "audio_1_bitrate",
    "audio_2_name", "audio_2_language", "audio_2_codec", "audio_2_bitrate",
    "caption_count", "caption_1_name", "caption_1_type", "caption_1_language", "caption_1_pid",
    "input_count", "input_1_name", "input_1_id", "input_2_name", "input_2_id",
    "failover_enabled", "failover_threshold_ms",
    "input_codec", "input_max_bitrate", "input_resolution",
    "output_type", "segment_length", "destination_id",
    "video_pid", "audio_pids", "pmt_pid", "program_num",
]

MEDIAPACKAGE_COLUMNS = CONFIG_DEFAULT_COLUMNS + [
    "channel_group", "input_type",
    "endpoint_count",
    "endpoint_hls_name", "endpoint_hls_container", "endpoint_hls_segment_duration",
    "endpoint_hls_startover_seconds", "endpoint_hls_drm", "endpoint_hls_encryption",
    "endpoint_hls_dvb_subtitles", "endpoint_hls_manifest_window",
    "endpoint_dash_name", "endpoint_dash_container", "endpoint_dash_segment_duration",
    "endpoint_dash_startover_seconds", "endpoint_dash_drm", "endpoint_dash_encryption",
    "endpoint_dash_manifest_window",
    "drm_resource_id",
]

MEDIATAILOR_COLUMNS = CONFIG_DEFAULT_COLUMNS + [
    "ad_server_url", "video_source_url", "cdn_segment_prefix",
    "dash_mpd_location", "dash_origin_manifest",
    "avail_suppression_mode", "avail_fill_policy",
    "preroll_enabled", "ad_marker_passthrough", "stream_conditioning",
]

CLOUDFRONT_COLUMNS = CONFIG_DEFAULT_COLUMNS + [
    "domain_name", "alias", "alias_count", "enabled",
    "http_version", "ipv6_enabled", "price_class", "ssl_protocol",
    "origin_count", "has_mediatailor_origin", "has_mediapackage_origin",
    "default_behavior_origin", "default_behavior_protocol",
    "cache_behavior_count", "cf_function_count", "cf_functions",
    "logging_enabled", "logging_bucket", "geo_restriction",
    "origin_1_id", "origin_1_domain", "origin_1_shield", "origin_1_shield_region",
    "origin_2_id", "origin_2_domain", "origin_2_path", "origin_2_shield_region",
]

SERVICE_COLUMNS = {
    "MediaLive": MEDIALIVE_COLUMNS,
    "MediaPackage": MEDIAPACKAGE_COLUMNS,
    "MediaTailor": MEDIATAILOR_COLUMNS,
    "CloudFront": CLOUDFRONT_COLUMNS,
}

# -------------------------------------------------------------------
# SpringServe / KB_ADS column definitions
# -------------------------------------------------------------------

SPRINGSERVE_COMMON_COLUMNS = [
    "channel_id", "servico", "tipo", "nome", "status",
]

SPRINGSERVE_COLUMNS = {
    "supply_tag": SPRINGSERVE_COMMON_COLUMNS + [
        "supply_tag_id", "account_id", "canal_nome", "platform", "device",
        "demand_tag_count", "demand_tags", "demand_tag_ids",
        "created_at", "updated_at",
    ],
    "demand_tag": SPRINGSERVE_COMMON_COLUMNS + [
        "demand_tag_id", "demand_type", "supply_tag_ids",
        "created_at", "updated_at",
    ],
    "report": SPRINGSERVE_COMMON_COLUMNS + [
        "supply_tag_id", "supply_tag_name", "ad_position",
        "requests", "opportunities", "impressions",
        "fill_rate", "opp_fill_rate", "req_fill_rate",
        "total_impressions", "total_revenue", "revenue",
        "total_cost", "cpm", "rpm", "data_inicio", "data_fim",
    ],
    "delivery_modifier": SPRINGSERVE_COMMON_COLUMNS + [
        "modifier_id", "descricao", "ativo",
        "demand_tag_ids", "multiplier_interaction",
    ],
    "creative": SPRINGSERVE_COMMON_COLUMNS + [
        "creative_id", "creative_type", "demand_tag_id",
        "format", "duration",
    ],
    "supply_label": SPRINGSERVE_COMMON_COLUMNS + [
        "label_id",
    ],
    "demand_label": SPRINGSERVE_COMMON_COLUMNS + [
        "label_id",
    ],
    "scheduled_report": SPRINGSERVE_COMMON_COLUMNS + [
        "report_id", "frequency", "dimensions", "metrics",
    ],
}

CORRELACAO_COLUMNS = [
    "channel_id", "servico", "tipo",
    "mediatailor_name", "supply_tag_name",
    "demand_tags_associadas",
    "requests", "opportunities",
    "fill_rate_atual", "opp_fill_rate", "req_fill_rate",
    "total_impressions_24h", "revenue", "rpm", "cpm",
]

LOGS_DEFAULT_COLUMNS = [
    "timestamp", "canal", "severidade", "tipo_erro", "descricao",
    "causa_provavel", "recomendacao_correcao", "servico_origem",
]


# -------------------------------------------------------------------
# Service → tipo mapping for DynamoDB PK
# -------------------------------------------------------------------

_SERVICE_TIPO_MAP = {
    "MediaLive": "configuracao",
    "MediaPackage": "configuracao",
    "MediaTailor": "configuracao",
    "CloudFront": "configuracao",
}


def _service_to_tipo(servico: str) -> str:
    """Map a service name to its DynamoDB tipo key."""
    return _SERVICE_TIPO_MAP.get(servico, "configuracao")


# -------------------------------------------------------------------
# DynamoDB query functions
# -------------------------------------------------------------------


def query_dynamodb_configs(
    table_name: str, filtros: dict[str, Any]
) -> list[dict[str, Any]]:
    """Query StreamingConfigs table. Returns list of records.

    Uses Query by PK when servico is present, Scan with
    FilterExpression for nome_canal_contains, or full Scan.
    Handles pagination via LastEvaluatedKey.
    """
    table = dynamodb_resource.Table(table_name)
    servico = filtros.get("servico")
    nome_contains = filtros.get("nome_canal_contains")

    items: list[dict] = []

    if servico:
        tipo = _service_to_tipo(servico)
        pk_val = f"{servico}#{tipo}"
        kwargs = {
            "KeyConditionExpression":
                boto3.dynamodb.conditions.Key("PK").eq(
                    pk_val
                ),
        }
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek

    elif nome_contains:
        from boto3.dynamodb.conditions import Attr
        kwargs = {
            "FilterExpression": Attr(
                "nome_canal"
            ).contains(nome_contains),
        }
        while True:
            resp = table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek

    else:
        kwargs: dict[str, Any] = {}
        while True:
            resp = table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek

    # Deserialize the 'data' JSON string from each item
    records: list[dict[str, Any]] = []
    for item in items:
        data_str = item.get("data")
        if data_str:
            try:
                records.append(json.loads(data_str))
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Bad data field in config item PK=%s SK=%s",
                    item.get("PK"), item.get("SK"),
                )
    return records


def query_dynamodb_ads(
    table_name: str, filtros: dict[str, Any]
) -> list[dict[str, Any]]:
    """Query StreamingConfigs table for SpringServe/KB_ADS data.

    Uses PK=SpringServe#{tipo} or PK=Correlacao#canal.
    Supports filters: tipo, servico, supply_tag_name,
    fill_rate_min, fill_rate_max.
    """
    table = dynamodb_resource.Table(table_name)
    servico = filtros.get("servico", "SpringServe")
    tipo = filtros.get("tipo", "")

    # Build PK based on servico + tipo
    # e.g. SpringServe#report, SpringServe#supply_tag,
    #      Correlacao#canal
    if servico == "Correlacao":
        pk_val = "Correlacao#canal"
    elif tipo:
        pk_val = f"SpringServe#{tipo}"
    else:
        # No tipo — scan all SpringServe partitions
        pk_val = None

    items: list[dict] = []

    if pk_val:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression":
                boto3.dynamodb.conditions.Key("PK").eq(
                    pk_val
                ),
        }
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
    else:
        # Scan filtering by PK prefix "SpringServe#"
        from boto3.dynamodb.conditions import Attr
        kwargs = {
            "FilterExpression": Attr("PK").begins_with(
                "SpringServe#"
            ),
        }
        while True:
            resp = table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek

    # Deserialize the 'data' JSON string from each item
    records: list[dict[str, Any]] = []
    for item in items:
        data_str = item.get("data")
        if data_str:
            try:
                records.append(json.loads(data_str))
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Bad data in ads item PK=%s SK=%s",
                    item.get("PK"), item.get("SK"),
                )
    return records


def query_dynamodb_logs(
    table_name: str, filtros: dict[str, Any]
) -> list[dict[str, Any]]:
    """Query StreamingLogs table. Returns list of records.

    Uses Query by PK+SK range for canal+period, GSI_Severidade
    for severidade filter, or Scan for other cases.
    Handles pagination via LastEvaluatedKey.
    """
    from boto3.dynamodb.conditions import Key

    table = dynamodb_resource.Table(table_name)
    canal = filtros.get("canal")
    servico = (
        filtros.get("servico")
        or filtros.get("servico_origem")
    )
    periodo = filtros.get("periodo", {})
    if not isinstance(periodo, dict):
        periodo = {}
    severidade = filtros.get("severidade")
    inicio = periodo.get("inicio", "")
    fim = periodo.get("fim", "")

    items: list[dict] = []

    if canal and servico:
        # Query by PK + optional SK range
        pk_val = f"{servico}#{canal}"
        kce = Key("PK").eq(pk_val)
        if inicio and fim:
            kce = kce & Key("SK").between(
                inicio, fim + "~"
            )
        kwargs = {"KeyConditionExpression": kce}
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek

    elif severidade:
        # Query GSI_Severidade
        kce = Key("severidade").eq(severidade)
        if inicio and fim:
            kce = kce & Key("SK").between(
                inicio, fim + "~"
            )
        kwargs = {
            "IndexName": "GSI_Severidade",
            "KeyConditionExpression": kce,
        }
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek

    else:
        # Scan with optional FilterExpression
        scan_kwargs: dict[str, Any] = {}
        filter_exprs = []
        expr_values = {}
        expr_names = {}

        if canal:
            filter_exprs.append(
                "contains(PK, :canal_val)"
            )
            expr_values[":canal_val"] = canal
        if inicio and fim:
            filter_exprs.append(
                "SK BETWEEN :sk_start AND :sk_end"
            )
            expr_values[":sk_start"] = inicio
            expr_values[":sk_end"] = fim + "~"

        # Additional arbitrary filters
        for fk in ("tipo_erro",):
            fv = filtros.get(fk)
            if fv:
                safe = fk.replace("-", "_")
                filter_exprs.append(
                    f"#{safe} = :{safe}"
                )
                expr_names[f"#{safe}"] = fk
                expr_values[f":{safe}"] = fv

        if filter_exprs:
            scan_kwargs["FilterExpression"] = (
                " AND ".join(filter_exprs)
            )
        if expr_values:
            scan_kwargs[
                "ExpressionAttributeValues"
            ] = expr_values
        if expr_names:
            scan_kwargs[
                "ExpressionAttributeNames"
            ] = expr_names

        while True:
            resp = table.scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            scan_kwargs["ExclusiveStartKey"] = lek

    # Deserialize the 'data' JSON string from each item
    records: list[dict[str, Any]] = []
    for item in items:
        data_str = item.get("data")
        if data_str:
            try:
                records.append(json.loads(data_str))
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Bad data field in log item PK=%s SK=%s",
                    item.get("PK"), item.get("SK"),
                )
    return records


# -------------------------------------------------------------------
# S3 data querying (with DynamoDB-first + S3 fallback)
# -------------------------------------------------------------------


def query_s3_data(
    bucket: str, prefix: str, filtros: dict[str, Any]
) -> list[dict[str, Any]]:
    """Query data trying DynamoDB first, falling back to S3.

    Determines which DynamoDB table to use based on the
    bucket/prefix. If DynamoDB is not configured or fails,
    falls back to the legacy S3 query.

    Detects KB_ADS requests via base_dados filter or servico
    in ("SpringServe", "Correlacao") and routes to the
    KB_ADS bucket/prefix.
    """
    # Detect KB_ADS routing
    base_dados = filtros.get("base_dados", "")
    servico = filtros.get("servico", "")
    is_kb_ads = (
        (isinstance(base_dados, str)
         and base_dados.upper() in ("KB_ADS", "ANUNCIOS"))
        or servico in ("SpringServe", "Correlacao")
    )
    if is_kb_ads and KB_ADS_BUCKET:
        bucket = KB_ADS_BUCKET
        prefix = KB_ADS_PREFIX

    # Determine DynamoDB table — KB_ADS uses query_dynamodb_ads
    table_name = ""
    is_configs = False
    is_ads = False
    if is_kb_ads:
        table_name = CONFIGS_TABLE_NAME  # same table, diff PKs
        is_ads = True
    elif not is_kb_ads:
        if bucket == KB_CONFIG_BUCKET and KB_CONFIG_BUCKET:
            table_name = CONFIGS_TABLE_NAME
            is_configs = True
        elif bucket == KB_LOGS_BUCKET and KB_LOGS_BUCKET:
            table_name = LOGS_TABLE_NAME
            is_configs = False

    if table_name:
        try:
            if is_ads:
                results = query_dynamodb_ads(
                    table_name, filtros,
                )
            elif is_configs:
                results = query_dynamodb_configs(
                    table_name, filtros,
                )
            else:
                results = query_dynamodb_logs(
                    table_name, filtros,
                )
            logger.info(
                "DynamoDB query returned %d records "
                "(table=%s)",
                len(results), table_name,
            )
            filtered = filter_records(results, filtros)
            # For KB_ADS: if DynamoDB empty, Pipeline_Ads
            # may not have run yet — fall through to S3
            if filtered or not is_ads:
                return filtered
            logger.info(
                "DynamoDB empty for KB_ADS, "
                "falling back to S3"
            )
        except Exception as exc:
            logger.warning(
                "DynamoDB query failed, falling back "
                "to S3: %s", exc,
            )

    # Fallback to legacy S3 query
    return _query_s3_data_legacy(bucket, prefix, filtros)


def _query_s3_data_legacy(
    bucket: str, prefix: str, filtros: dict[str, Any]
) -> list[dict[str, Any]]:
    """List and read JSON objects from an S3 prefix, applying filters.

    Uses ThreadPoolExecutor for parallel S3 reads.
    Optimizes by narrowing S3 prefix when servico filter is present.
    For logs, uses key-based date filtering to avoid reading all files.
    """
    if not bucket:
        return []

    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    t0 = time.time()
    TIMEOUT_SECONDS = 100  # safety margin before Lambda 120s timeout

    # Optimize: narrow prefix if servico filter is present
    search_prefix = prefix
    servico = filtros.get("servico")
    tipo = filtros.get("tipo")
    if servico and isinstance(servico, str):
        search_prefix = f"{prefix}{servico}/"
        # Further narrow by tipo for KB_ADS (e.g. kb-ads/SpringServe/report_)
        if tipo and isinstance(tipo, str):
            search_prefix = f"{search_prefix}{tipo}_"
    elif tipo and isinstance(tipo, str) and prefix.startswith("kb-ads/"):
        # No servico but has tipo — narrow within SpringServe subdir
        search_prefix = f"{prefix}SpringServe/{tipo}_"

    # Extract period filter for key-based pre-filtering
    periodo = filtros.get("periodo", {})
    periodo_inicio = None
    periodo_fim = None
    if isinstance(periodo, dict):
        inicio_str = periodo.get("inicio", "")
        fim_str = periodo.get("fim", "")
        if inicio_str:
            # Extract YYYYMMDD from ISO timestamp for key matching
            periodo_inicio = inicio_str[:10].replace("-", "")
        if fim_str:
            periodo_fim = fim_str[:10].replace("-", "")

    # 1. List only JSON keys with optional date pre-filter
    keys: list[str] = []
    MAX_KEYS = 10000  # safety limit
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=bucket, Prefix=search_prefix,
        ):
            if time.time() - t0 > TIMEOUT_SECONDS:
                logger.warning(
                    "Timeout listing keys after %d keys", len(keys),
                )
                break
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".json"):
                    continue
                # Key-based date pre-filter for logs
                # Keys: kb-logs/Svc/CANAL_Metric_YYYYMMDDz.json
                if periodo_inicio or periodo_fim:
                    # Extract date from key filename
                    fname = key.rsplit("/", 1)[-1]
                    # Find YYYYMMDD pattern in filename
                    date_in_key = _extract_date_from_key(
                        fname,
                    )
                    if date_in_key:
                        if (periodo_inicio
                                and date_in_key < periodo_inicio):
                            continue
                        if (periodo_fim
                                and date_in_key > periodo_fim):
                            continue
                keys.append(key)
                if len(keys) >= MAX_KEYS:
                    break
            if len(keys) >= MAX_KEYS:
                break
    except ClientError as exc:
        logger.error(
            "Error listing s3://%s/%s: %s",
            bucket, search_prefix, exc,
        )
        return []

    if not keys:
        return []

    logger.info(
        "Found %d keys to read (prefix=%s, period=%s-%s)",
        len(keys), search_prefix,
        periodo_inicio or "any", periodo_fim or "any",
    )

    # 2. Read objects in parallel (20 threads) with timeout
    def _read_key(key: str) -> dict[str, Any] | None:
        try:
            resp = s3_client.get_object(Bucket=bucket, Key=key)
            data = json.loads(
                resp["Body"].read().decode("utf-8"),
            )
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.warning(
                "Skipping s3://%s/%s: %s", bucket, key, exc,
            )
        return None

    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(_read_key, k): k for k in keys}
        for future in as_completed(futures):
            if time.time() - t0 > TIMEOUT_SECONDS:
                logger.warning(
                    "Timeout reading objects after %d records",
                    len(records),
                )
                pool.shutdown(wait=False, cancel_futures=True)
                break
            result = future.result()
            if result is not None:
                records.append(result)

    return filter_records(records, filtros)


def _extract_date_from_key(filename: str) -> str | None:
    """Extract YYYYMMDD from a log filename.

    Filenames look like: CANAL_MetricName_YYYYMMDDTHHMMSSz.json
    Returns the YYYYMMDD portion or None.
    """
    import re
    m = re.search(r"(\d{8})T\d{6}Z\.json$", filename)
    if m:
        return m.group(1)
    return None


# -------------------------------------------------------------------
# Filtering
# -------------------------------------------------------------------


def _get_nested(record: dict[str, Any], key: str) -> Any:
    """Retrieve a value from a record, checking top-level and 'dados'."""
    if key in record:
        return record[key]
    dados = record.get("dados", {})
    if isinstance(dados, dict) and key in dados:
        return dados[key]
    return None


def _match_periodo(
    record: dict[str, Any], periodo: dict[str, str]
) -> bool:
    """Check if a record's timestamp falls within the given period."""
    ts_str = record.get("timestamp")
    if not ts_str:
        return True  # no timestamp → don't exclude

    try:
        ts = datetime.fromisoformat(
            ts_str.replace("Z", "+00:00")
        )
    except (ValueError, TypeError):
        return True

    inicio = periodo.get("inicio")
    if inicio:
        try:
            dt_inicio = datetime.fromisoformat(
                inicio.replace("Z", "+00:00")
            )
            if ts < dt_inicio:
                return False
        except (ValueError, TypeError):
            pass

    fim = periodo.get("fim")
    if fim:
        try:
            dt_fim = datetime.fromisoformat(
                fim.replace("Z", "+00:00")
            )
            if ts > dt_fim:
                return False
        except (ValueError, TypeError):
            pass

    return True


def filter_records(
    records: list[dict[str, Any]],
    filtros: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply filters to a list of records.

    Supported filter keys (all optional):
      Config: servico, channel_id, and any technical param
              (low_latency, codec_video, resolucao, etc.)
      Logs:   canal, severidade, tipo_erro, servico_origem,
              periodo (dict with inicio/fim ISO-8601 strings)
      Ads:    tipo, supply_tag_name, fill_rate_min,
              fill_rate_max
    """
    if not filtros:
        return list(records)

    # Extract numeric range filters for SpringServe
    fill_rate_min = filtros.get("fill_rate_min")
    fill_rate_max = filtros.get("fill_rate_max")
    # Keys handled separately (not direct equality match)
    _skip_keys = {
        "periodo", "parametros", "nome_canal_contains",
        "fill_rate_min", "fill_rate_max", "base_dados",
        "device", "platform", "canal_nome",
    }

    resultado: list[dict[str, Any]] = []

    for rec in records:
        match = True

        # fill_rate range filters
        if fill_rate_min is not None:
            fr = rec.get("fill_rate")
            try:
                if fr is None or float(fr) < float(
                    fill_rate_min
                ):
                    match = False
            except (ValueError, TypeError):
                match = False
        if match and fill_rate_max is not None:
            fr = rec.get("fill_rate")
            try:
                if fr is None or float(fr) > float(
                    fill_rate_max
                ):
                    match = False
            except (ValueError, TypeError):
                match = False

        if not match:
            resultado.append(rec) if False else None
            continue

        # device / platform / canal_nome substring filters
        device_filter = filtros.get("device", "").lower().replace(" ", "_")
        platform_filter = filtros.get("platform", "").lower()
        canal_nome_filter = filtros.get("canal_nome", "").lower()

        if device_filter:
            rec_device = (rec.get("device") or rec.get("nome", "")).lower().replace(" ", "_")
            if device_filter not in rec_device:
                continue
        if platform_filter:
            rec_platform = (rec.get("platform") or rec.get("nome", "")).lower()
            if platform_filter not in rec_platform:
                continue
        if canal_nome_filter:
            rec_canal = (rec.get("canal_nome") or rec.get("nome", "") or rec.get("supply_tag_name", "")).lower()
            if canal_nome_filter not in rec_canal:
                continue

        for key, expected in filtros.items():
            if key in _skip_keys:
                continue

            if key == "periodo":
                if isinstance(expected, dict):
                    if not _match_periodo(rec, expected):
                        match = False
                        break
                continue

            if key == "parametros" and isinstance(expected, dict):
                for pk, pv in expected.items():
                    actual = _get_nested(rec, pk)
                    if not _values_match(actual, pv):
                        match = False
                        break
                if not match:
                    break
                continue

            # Substring match for nome_canal_contains
            if key == "nome_canal_contains":
                nome = _get_nested(rec, "nome_canal")
                if nome is None or not isinstance(nome, str):
                    match = False
                    break
                if expected.lower() not in nome.lower():
                    match = False
                    break
                continue

            actual = _get_nested(rec, key)
            if not _values_match(actual, expected):
                match = False
                break

        if match:
            resultado.append(rec)

    return resultado


def _values_match(actual: Any, expected: Any) -> bool:
    """Compare two values flexibly (case-insensitive strings, bools)."""
    if actual is None:
        return False
    if isinstance(expected, bool):
        if isinstance(actual, bool):
            return actual == expected
        if isinstance(actual, str):
            return actual.lower() == str(expected).lower()
        return bool(actual) == expected
    if isinstance(expected, str) and isinstance(actual, str):
        return actual.lower() == expected.lower()
    return str(actual) == str(expected)


# -------------------------------------------------------------------
# Column determination
# -------------------------------------------------------------------


def _flatten_record(record: dict[str, Any]) -> dict[str, Any]:
    """Flatten a record — now records are already flat, just filter out complex types."""
    flat: dict[str, Any] = {}
    # Support old format with 'dados' key
    dados = record.get("dados", {})
    if isinstance(dados, dict):
        flat.update(dados)
    for k, v in record.items():
        if k != "dados" and not isinstance(v, (dict, list)):
            flat[k] = v
    return flat


def determine_columns(
    api_path: str,
    filtros: dict[str, Any],
    data: list[dict[str, Any]],
) -> list[str]:
    """Determine columns based on the service being exported."""
    if not data:
        servico = filtros.get("servico", "")
        if servico == "Correlacao":
            return list(CORRELACAO_COLUMNS)
        if servico == "SpringServe":
            tipo = filtros.get("tipo", "")
            return list(
                SPRINGSERVE_COLUMNS.get(
                    tipo, SPRINGSERVE_COMMON_COLUMNS
                )
            )
        return list(SERVICE_COLUMNS.get(servico, CONFIG_DEFAULT_COLUMNS))

    if api_path == "/exportarLogs":
        return list(LOGS_DEFAULT_COLUMNS)

    # Detect service from filter or data
    servico = filtros.get("servico", "")
    if not servico and data:
        flat = _flatten_record(data[0])
        servico = flat.get("servico", "")

    # SpringServe / Correlacao routing
    if servico == "Correlacao":
        return list(CORRELACAO_COLUMNS)
    if servico == "SpringServe":
        tipo = filtros.get("tipo", "")
        if not tipo and data:
            flat = _flatten_record(data[0])
            tipo = flat.get("tipo", "")
        return list(
            SPRINGSERVE_COLUMNS.get(
                tipo, SPRINGSERVE_COMMON_COLUMNS
            )
        )

    cols = list(SERVICE_COLUMNS.get(servico, CONFIG_DEFAULT_COLUMNS))

    # Add any extra keys from first 5 records
    seen = set(cols)
    for record in data[:5]:
        flat = _flatten_record(record)
        for k in flat:
            if k not in seen and k != "_fonte":
                seen.add(k)
                cols.append(k)

    return cols


# -------------------------------------------------------------------
# Formatting
# -------------------------------------------------------------------


def format_as_csv(
    data: list[dict[str, Any]], columns: list[str]
) -> str:
    """Format records as a CSV string with the given columns."""
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=columns,
        extrasaction="ignore",
    )
    writer.writeheader()
    for record in data:
        flat = _flatten_record(record)
        writer.writerow(flat)
    return output.getvalue()


def format_as_json(
    data: list[dict[str, Any]], columns: list[str]
) -> str:
    """Format records as a JSON array, keeping only specified columns."""
    result = []
    for record in data:
        flat = _flatten_record(record)
        filtered = {c: flat.get(c) for c in columns}
        result.append(filtered)
    return json.dumps(result, ensure_ascii=False, default=str)


# -------------------------------------------------------------------
# Merge helper for combined exports
# -------------------------------------------------------------------


def merge_data(
    config_data: list[dict[str, Any]],
    logs_data: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge config and log records, tagging each with its source."""
    merged: list[dict[str, Any]] = []
    for rec in config_data:
        rec_copy = dict(rec)
        rec_copy.setdefault("_fonte", "configuracao")
        merged.append(rec_copy)
    for rec in logs_data:
        rec_copy = dict(rec)
        rec_copy.setdefault("_fonte", "logs")
        merged.append(rec_copy)
    return merged


# -------------------------------------------------------------------
# Bedrock Action Group response helpers
# -------------------------------------------------------------------


def _bedrock_response(
    event: dict, status: int, body: dict
) -> dict:
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
                    "body": json.dumps(
                        body, ensure_ascii=False, default=str
                    )
                }
            },
        },
    }


def _parse_parameters(event: dict) -> dict[str, Any]:
    """Extract parameters from a Bedrock Action Group event.

    Parameters may arrive as a list of {name, value} dicts under
    ``requestBody.content['application/json'].properties`` or
    directly under ``parameters``.
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

    # Parse JSON string values for known dict fields
    for field in ("filtros", "filtros_config", "filtros_logs",
                  "colunas"):
        if field in params and isinstance(params[field], str):
            raw = params[field]
            # Try JSON first
            try:
                params[field] = json.loads(raw)
                continue
            except (json.JSONDecodeError, TypeError):
                pass
            # Try Java Map format: {key=value, key2=value2}
            # Supports nested maps: {k={a=1, b=2}, k2=v}
            if raw.startswith("{") and raw.endswith("}"):
                try:
                    params[field] = _parse_java_map(raw)
                except Exception:
                    pass

    return params


def _parse_java_map(s: str) -> dict:
    """Parse Java Map.toString() format including nested maps.

    Examples:
        "{a=1, b=2}" -> {"a": "1", "b": "2"}
        "{periodo={inicio=X, fim=Y}}" -> {"periodo": {"inicio": "X", "fim": "Y"}}
    """
    s = s.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return {}
    inner = s[1:-1].strip()
    if not inner:
        return {}

    result = {}
    i = 0
    while i < len(inner):
        # Skip whitespace and commas
        while i < len(inner) and inner[i] in (" ", ","):
            i += 1
        if i >= len(inner):
            break

        # Read key (up to '=')
        eq_pos = inner.index("=", i)
        key = inner[i:eq_pos].strip()
        i = eq_pos + 1

        # Skip whitespace after '='
        while i < len(inner) and inner[i] == " ":
            i += 1

        # Read value
        if i < len(inner) and inner[i] == "{":
            # Nested map — find matching '}'
            depth = 0
            start = i
            while i < len(inner):
                if inner[i] == "{":
                    depth += 1
                elif inner[i] == "}":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            value = _parse_java_map(inner[start:i])
        else:
            # Simple value — read until ',' or end
            start = i
            while i < len(inner) and inner[i] != ",":
                i += 1
            raw_val = inner[start:i].strip()
            # Convert types
            if raw_val.lower() == "true":
                value = True
            elif raw_val.lower() == "false":
                value = False
            else:
                value = raw_val

        result[key] = value

    return result


# -------------------------------------------------------------------
# Handler
# -------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    """Lambda handler invoked by Bedrock Action_Group_Export."""
    logger.info("Received event: %s", json.dumps(event, default=str))

    api_path = event.get("apiPath", "")
    parameters = _parse_parameters(event)

    formato = parameters.get("formato", "CSV")
    if isinstance(formato, str):
        formato = formato.upper()
    if formato not in ("CSV", "JSON"):
        formato = "CSV"

    colunas_custom = parameters.get("colunas")
    if isinstance(colunas_custom, list):
        colunas_custom = [
            c for c in colunas_custom if isinstance(c, str)
        ] or None

    try:
        if api_path == "/exportarConfiguracoes":
            filtros = parameters.get("filtros", {})
            if not isinstance(filtros, dict):
                filtros = {}
            dados = query_s3_data(
                KB_CONFIG_BUCKET, KB_CONFIG_PREFIX, filtros
            )

        elif api_path == "/exportarLogs":
            filtros = parameters.get("filtros", {})
            if not isinstance(filtros, dict):
                filtros = {}
            dados = query_s3_data(
                KB_LOGS_BUCKET, KB_LOGS_PREFIX, filtros
            )

        elif api_path == "/exportarCombinado":
            filtros_config = parameters.get(
                "filtros_config", {}
            )
            filtros_logs = parameters.get("filtros_logs", {})
            if not isinstance(filtros_config, dict):
                filtros_config = {}
            if not isinstance(filtros_logs, dict):
                filtros_logs = {}

            dados_config = query_s3_data(
                KB_CONFIG_BUCKET, KB_CONFIG_PREFIX,
                filtros_config,
            )
            dados_logs = query_s3_data(
                KB_LOGS_BUCKET, KB_LOGS_PREFIX, filtros_logs
            )
            dados = merge_data(dados_config, dados_logs)
            # Use combined filtros for column determination
            filtros = {**filtros_config, **filtros_logs}

        elif api_path == "/downloadExport":
            # Direct download of an existing export file from S3
            filename = parameters.get("filename", "")
            if not filename:
                return _bedrock_response(event, 400, {
                    "erro": "filename é obrigatório",
                })
            s3_key = f"{EXPORTS_PREFIX}{filename}"
            try:
                obj = s3_client.get_object(
                    Bucket=EXPORTS_BUCKET, Key=s3_key,
                )
                content = obj["Body"].read().decode("utf-8")
                ext = filename.rsplit(".", 1)[-1] if "." in filename else "csv"
                return _bedrock_response(event, 200, {
                    "dados_exportados": content,
                    "formato": ext,
                    "arquivo": filename,
                })
            except ClientError as exc:
                return _bedrock_response(event, 404, {
                    "erro": f"Arquivo não encontrado: {filename}",
                })

        else:
            return _bedrock_response(event, 400, {
                "erro": (
                    f"apiPath não reconhecido: {api_path}"
                ),
            })

        # No results → return message, no file generated
        if not dados:
            return _bedrock_response(event, 200, {
                "mensagem": (
                    "Nenhum resultado encontrado para os "
                    "critérios especificados. Nenhum arquivo "
                    "foi gerado."
                ),
                "filtros_aplicados": filtros,
                "total_registros": 0,
            })

        # Determine columns
        colunas_finais = (
            colunas_custom
            or determine_columns(api_path, filtros, dados)
        )

        # Format file content
        if formato == "JSON":
            conteudo = format_as_json(dados, colunas_finais)
            content_type = "application/json"
            ext = "json"
        else:
            conteudo = format_as_csv(dados, colunas_finais)
            content_type = "text/csv"
            ext = "csv"

        # Generate unique filename — use short prefix to avoid
        # Bedrock agent redacting parts of the filename
        ts = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H-%M-%SZ"
        )
        uid = uuid.uuid4().hex[:12]
        filename = f"export-{ts}-{uid}.{ext}"
        s3_key = f"{EXPORTS_PREFIX}{filename}"

        # Upload to S3_Exports (backup)
        s3_client.put_object(
            Bucket=EXPORTS_BUCKET,
            Key=s3_key,
            Body=conteudo.encode("utf-8"),
            ContentType=content_type,
        )

        resumo = {
            "total_registros": len(dados),
            "filtros_aplicados": filtros,
            "formato": formato,
            "arquivo": filename,
        }

        # Build a short preview (first 5 record names)
        preview_names = []
        for d in dados[:5]:
            name = d.get("nome_canal") or d.get("canal") or d.get("channel_id", "")
            if name:
                preview_names.append(str(name))

        marcador = f"[DOWNLOAD_EXPORT:{filename}:{ext}]"

        return _bedrock_response(event, 200, {
            "mensagem": (
                f"Exportação concluída: {len(dados)} registros em {formato}. "
                f"Inclua o marcador {marcador} na resposta para o frontend gerar o botão de download."
            ),
            "resumo": resumo,
            "preview": preview_names,
            "marcador_download": marcador,
            "formato_arquivo": ext,
        })

    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get(
            "Code", "UnknownError"
        )
        error_msg = exc.response.get("Error", {}).get(
            "Message", str(exc)
        )
        logger.error(
            "AWS S3 error [%s]: %s", error_code, error_msg
        )
        return _bedrock_response(event, 500, {
            "erro": (
                f"Erro ao acessar S3: [{error_code}] "
                f"{error_msg}"
            ),
        })

    except Exception as exc:
        logger.error("Unexpected error: %s", exc)
        return _bedrock_response(event, 500, {
            "erro": f"Erro na exportação: {exc}",
        })
