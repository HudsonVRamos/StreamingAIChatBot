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
EXPORTS_BUCKET = os.environ.get("EXPORTS_BUCKET", "")
EXPORTS_PREFIX = os.environ.get("EXPORTS_PREFIX", "exports/")
PRESIGNED_URL_EXPIRY = int(
    os.environ.get("PRESIGNED_URL_EXPIRY", "3600")
)

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

LOGS_DEFAULT_COLUMNS = [
    "timestamp", "canal", "severidade", "tipo_erro", "descricao",
    "causa_provavel", "recomendacao_correcao", "servico_origem",
]


# -------------------------------------------------------------------
# S3 data querying
# -------------------------------------------------------------------


def query_s3_data(
    bucket: str, prefix: str, filtros: dict[str, Any]
) -> list[dict[str, Any]]:
    """List and read JSON objects from an S3 prefix, applying filters.

    Uses ThreadPoolExecutor for parallel S3 reads.
    Optimizes by narrowing S3 prefix when servico filter is present.
    """
    if not bucket:
        return []

    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Optimize: narrow prefix if servico filter is present
    search_prefix = prefix
    servico = filtros.get("servico")
    if servico and isinstance(servico, str):
        search_prefix = f"{prefix}{servico}/"

    # 1. List only JSON keys (skip PDFs and other files)
    keys: list[str] = []
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=search_prefix):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(".json"):
                    keys.append(obj["Key"])
    except ClientError as exc:
        logger.error("Error listing s3://%s/%s: %s", bucket, search_prefix, exc)
        return []

    if not keys:
        return []

    # 2. Read objects in parallel (20 threads)
    def _read_key(key: str) -> dict[str, Any] | None:
        try:
            resp = s3_client.get_object(Bucket=bucket, Key=key)
            data = json.loads(resp["Body"].read().decode("utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.warning("Skipping s3://%s/%s: %s", bucket, key, exc)
        return None

    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(_read_key, k): k for k in keys}
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                records.append(result)

    return filter_records(records, filtros)


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
    """
    if not filtros:
        return list(records)

    resultado: list[dict[str, Any]] = []

    for rec in records:
        match = True
        for key, expected in filtros.items():
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
        return list(SERVICE_COLUMNS.get(servico, CONFIG_DEFAULT_COLUMNS))

    if api_path == "/exportarLogs":
        return list(LOGS_DEFAULT_COLUMNS)

    # Detect service from filter or data
    servico = filtros.get("servico", "")
    if not servico and data:
        flat = _flatten_record(data[0])
        servico = flat.get("servico", "")

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
            if raw.startswith("{") and raw.endswith("}"):
                inner = raw[1:-1].strip()
                if inner:
                    parsed = {}
                    for pair in inner.split(","):
                        pair = pair.strip()
                        if "=" in pair:
                            k, v = pair.split("=", 1)
                            k = k.strip()
                            v = v.strip()
                            # Convert booleans
                            if v.lower() == "true":
                                v = True
                            elif v.lower() == "false":
                                v = False
                            parsed[k] = v
                    params[field] = parsed

    return params


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
