"""Normalization module for raw AWS API responses into Config_Enriquecida format.

Each normalizer extracts relevant fields from the raw AWS API response
and produces a Config_Enriquecida dict with channel_id, servico,
tipo="configuracao", and dados containing the structured configuration.

Prioritizes structured JSON data over raw text (Req 11.2).

Validates: Requirements 5.2, 11.1, 11.2, 11.3
"""

from typing import Any, Dict, Optional


def _safe_get(d: dict, *keys: str, default: Any = None) -> Any:
    """Safely traverse nested dicts."""
    current = d
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key, default)
        else:
            return default
    return current


def normalize_medialive_config(raw_config: dict) -> Dict[str, Any]:
    """Normalize a raw MediaLive DescribeChannel response into Config_Enriquecida.

    Extracts channel metadata, encoder settings, input attachments,
    and output groups from the raw API response.
    """
    channel_id = str(
        raw_config.get("Id", raw_config.get("ChannelId", raw_config.get("Name", "")))
    )

    # Extract encoder settings from the first output group's first output
    encoder = _safe_get(raw_config, "EncoderSettings") or {}
    video_descriptions = encoder.get("VideoDescriptions", [])
    audio_descriptions = encoder.get("AudioDescriptions", [])

    video_desc = video_descriptions[0] if video_descriptions else {}
    audio_desc = audio_descriptions[0] if audio_descriptions else {}

    codec_settings = video_desc.get("CodecSettings", {})
    video_codec = _extract_video_codec(codec_settings)
    audio_codec = _extract_audio_codec(audio_desc.get("CodecSettings", {}))

    # Resolution
    width = video_desc.get("Width")
    height = video_desc.get("Height")
    resolucao = f"{width}x{height}" if width and height else None

    # GOP settings from H264/H265 codec settings
    gop_size, gop_unit = _extract_gop_settings(codec_settings)

    # Framerate
    framerate = _extract_framerate(codec_settings)

    # Bitrate
    bitrate_video = _extract_video_bitrate(codec_settings)

    # Input attachments
    input_attachments = raw_config.get("InputAttachments", [])
    input_type = None
    protocolo_ingest = None
    if input_attachments:
        first_input = input_attachments[0]
        input_settings = first_input.get("InputSettings", {})
        input_type = input_settings.get("SourceEndpointBehavior")
        # Protocol is typically derived from the input class/type
        network_input = input_settings.get("NetworkInputSettings", {})
        if network_input:
            protocolo_ingest = "SRT" if "srt" in str(network_input).lower() else None

    # Output groups
    output_groups = encoder.get("OutputGroups", [])
    outputs = _extract_outputs(output_groups)

    dados = {
        "nome_canal": raw_config.get("Name"),
        "estado": raw_config.get("State"),
        "regiao": raw_config.get("Arn", "").split(":")[3] if raw_config.get("Arn") else None,
        "input_type": input_type,
        "codec_video": video_codec,
        "codec_audio": audio_codec,
        "resolucao": resolucao,
        "bitrate_video": bitrate_video,
        "gop_size": gop_size,
        "gop_unit": gop_unit,
        "framerate": framerate,
        "low_latency": raw_config.get("ChannelClass") == "SINGLE_PIPELINE"
        if raw_config.get("ChannelClass")
        else None,
        "protocolo_ingest": protocolo_ingest,
        "outputs": outputs,
        "configuracao_completa": raw_config,
    }

    return {
        "channel_id": channel_id,
        "servico": "MediaLive",
        "tipo": "configuracao",
        "dados": dados,
    }


def normalize_mediapackage_config(raw_config: dict) -> Dict[str, Any]:
    """Normalize a raw MediaPackage DescribeChannel/ListOriginEndpoints response.

    Extracts channel metadata and origin endpoint details.
    """
    channel_id = str(
        raw_config.get("Id", raw_config.get("ChannelId", raw_config.get("Arn", "")))
    )

    # Origin endpoints may be nested or provided separately
    origin_endpoints = raw_config.get("OriginEndpoints") or []
    outputs = []
    for ep in origin_endpoints:
        output_entry: Dict[str, Any] = {
            "tipo": None,
            "destino": ep.get("Url"),
            "segment_length": None,
        }
        if ep.get("HlsPackage"):
            output_entry["tipo"] = "HLS"
            output_entry["segment_length"] = ep["HlsPackage"].get("SegmentDurationSeconds")
        elif ep.get("DashPackage"):
            output_entry["tipo"] = "DASH"
            output_entry["segment_length"] = ep["DashPackage"].get("SegmentDurationSeconds")
        elif ep.get("CmafPackage"):
            output_entry["tipo"] = "CMAF"
            output_entry["segment_length"] = _safe_get(
                ep, "CmafPackage", "SegmentDurationSeconds"
            )
        outputs.append(output_entry)

    hls_ingest = raw_config.get("HlsIngest") or {}
    ingest_endpoints = hls_ingest.get("IngestEndpoints") or []

    dados = {
        "nome_canal": raw_config.get("Description") or raw_config.get("Id"),
        "estado": None,  # MediaPackage channels don't have a State field like MediaLive
        "regiao": raw_config.get("Arn", "").split(":")[3] if raw_config.get("Arn") else None,
        "input_type": "HLS" if ingest_endpoints else None,
        "outputs": outputs,
        "ingest_endpoints": [
            {"id": ep.get("Id"), "url": ep.get("Url")} for ep in ingest_endpoints
        ],
        "configuracao_completa": raw_config,
    }

    return {
        "channel_id": channel_id,
        "servico": "MediaPackage",
        "tipo": "configuracao",
        "dados": dados,
    }


def normalize_mediatailor_config(raw_config: dict) -> Dict[str, Any]:
    """Normalize a raw MediaTailor GetPlaybackConfiguration response.

    Extracts ad insertion settings and playback configuration details.
    """
    channel_id = str(
        raw_config.get("Name", raw_config.get("PlaybackConfigurationArn", ""))
    )

    dados = {
        "nome_canal": raw_config.get("Name"),
        "estado": None,
        "regiao": (
            raw_config.get("PlaybackConfigurationArn", "").split(":")[3]
            if raw_config.get("PlaybackConfigurationArn")
            else None
        ),
        "ad_insertion": {
            "playback_configuration_name": raw_config.get("Name"),
            "ad_decision_server_url": raw_config.get("AdDecisionServerUrl"),
            "cdn_content_segment_url_prefix": (
                raw_config.get("CdnConfiguration") or {}
            ).get("ContentSegmentUrlPrefix"),
            "slate_ad_url": raw_config.get("SlateAdUrl"),
            "personalization_threshold_seconds": raw_config.get(
                "PersonalizationThresholdSeconds"
            ),
            "avail_suppression_mode": _safe_get(
                raw_config, "AvailSuppression", "Mode"
            ),
        },
        "session_initialization_endpoint_prefix": raw_config.get(
            "SessionInitializationEndpointPrefix"
        ),
        "video_content_source_url": raw_config.get("VideoContentSourceUrl"),
        "configuracao_completa": raw_config,
    }

    return {
        "channel_id": channel_id,
        "servico": "MediaTailor",
        "tipo": "configuracao",
        "dados": dados,
    }


def normalize_cloudfront_config(raw_config: dict) -> Dict[str, Any]:
    """Normalize a raw CloudFront GetDistribution response.

    Extracts distribution metadata, origins, cache behaviors, and settings.
    """
    # Handle both GetDistribution (nested under Distribution) and direct config
    distribution = raw_config.get("Distribution", raw_config)
    dist_config = distribution.get("DistributionConfig", distribution)

    channel_id = str(distribution.get("Id", raw_config.get("Id", "")))

    # Origins
    origins_data = dist_config.get("Origins", {})
    origin_items = origins_data.get("Items", []) if isinstance(origins_data, dict) else []
    origins = [
        {
            "id": o.get("Id"),
            "domain_name": o.get("DomainName"),
            "origin_path": o.get("OriginPath", ""),
        }
        for o in origin_items
    ]

    # Default cache behavior
    default_cb = dist_config.get("DefaultCacheBehavior") or {}
    default_cache_behavior = {
        "viewer_protocol_policy": default_cb.get("ViewerProtocolPolicy"),
        "allowed_methods": _safe_get(default_cb, "AllowedMethods", "Items", default=[]),
        "cache_policy_id": default_cb.get("CachePolicyId"),
    }

    dados = {
        "nome_canal": dist_config.get("Comment") or channel_id,
        "estado": "Deployed" if distribution.get("Status") == "Deployed" else distribution.get("Status"),
        "regiao": "global",  # CloudFront is a global service
        "cdn_distribution": {
            "distribution_id": channel_id,
            "domain_name": distribution.get("DomainName"),
            "origins": origins,
            "default_cache_behavior": default_cache_behavior,
            "price_class": dist_config.get("PriceClass"),
            "enabled": dist_config.get("Enabled"),
        },
        "configuracao_completa": raw_config,
    }

    return {
        "channel_id": channel_id,
        "servico": "CloudFront",
        "tipo": "configuracao",
        "dados": dados,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_video_codec(codec_settings: dict) -> Optional[str]:
    """Extract video codec name from MediaLive CodecSettings."""
    if codec_settings.get("H264Settings"):
        return "H.264"
    if codec_settings.get("H265Settings"):
        return "H.265"
    if codec_settings.get("Mpeg2Settings"):
        return "MPEG-2"
    return None


def _extract_audio_codec(codec_settings: dict) -> Optional[str]:
    """Extract audio codec name from MediaLive AudioCodecSettings."""
    if codec_settings.get("AacSettings"):
        return "AAC"
    if codec_settings.get("Ac3Settings"):
        return "AC3"
    if codec_settings.get("Eac3Settings"):
        return "EAC3"
    if codec_settings.get("Mp2Settings"):
        return "MP2"
    return None


def _extract_gop_settings(codec_settings: dict) -> tuple:
    """Extract GOP size and unit from codec settings."""
    for key in ("H264Settings", "H265Settings"):
        settings = codec_settings.get(key, {})
        if settings:
            gop_size = settings.get("GopSize")
            gop_unit = settings.get("GopSizeUnits")
            return gop_size, gop_unit
    return None, None


def _extract_framerate(codec_settings: dict) -> Optional[float]:
    """Extract framerate from codec settings."""
    for key in ("H264Settings", "H265Settings"):
        settings = codec_settings.get(key, {})
        if settings:
            num = settings.get("FramerateNumerator")
            den = settings.get("FramerateDenominator")
            if num and den and den != 0:
                return round(num / den, 2)
    return None


def _extract_video_bitrate(codec_settings: dict) -> Optional[int]:
    """Extract video bitrate from codec settings."""
    for key in ("H264Settings", "H265Settings"):
        settings = codec_settings.get(key, {})
        if settings:
            return settings.get("Bitrate")
    return None


def _extract_outputs(output_groups: list) -> list:
    """Extract output information from MediaLive output groups."""
    outputs = []
    for og in output_groups:
        og_settings = og.get("OutputGroupSettings", {})
        output_type = None
        if og_settings.get("HlsGroupSettings"):
            output_type = "HLS"
        elif og_settings.get("DashIsoGroupSettings"):
            output_type = "DASH"
        elif og_settings.get("CmafIngestGroupSettings"):
            output_type = "CMAF"

        destination = _safe_get(og_settings, "HlsGroupSettings", "Destination", "DestinationRefId")
        if not destination:
            destination = _safe_get(og_settings, "DashIsoGroupSettings", "Destination", "DestinationRefId")

        segment_length = _safe_get(og_settings, "HlsGroupSettings", "SegmentLength")
        if segment_length is None:
            segment_length = _safe_get(og_settings, "DashIsoGroupSettings", "SegmentLength")

        outputs.append({
            "tipo": output_type,
            "destino": destination,
            "segment_length": segment_length,
        })
    return outputs


# -------------------------------------------------------------------
# Evento_Estruturado normalization & enrichment
# Validates: Requirements 7.2, 7.3, 11.4
# -------------------------------------------------------------------

import re
from datetime import datetime, timezone


# Mapping of known error keywords to tipo_erro classification
_ERROR_PATTERNS: list = [
    (r"input.?loss", "INPUT_LOSS"),
    (r"bitrate.?drop", "BITRATE_DROP"),
    (r"latency.?spike", "LATENCY_SPIKE"),
    (r"output.?fail", "OUTPUT_FAILURE"),
    (r"encoder.?error", "ENCODER_ERROR"),
    (r"ad.?insertion.?fail", "AD_INSERTION_FAILURE"),
    (r"cdn.?distribution.?error", "CDN_DISTRIBUTION_ERROR"),
    (r"cdn.?origin.?error", "CDN_ORIGIN_ERROR"),
    (r"cdn.?cache.?error", "CDN_CACHE_ERROR"),
]

# Severity keywords found in log messages
_SEVERITY_PATTERNS: list = [
    (r"\bCRITICAL\b", "CRITICAL"),
    (r"\bERROR\b", "ERROR"),
    (r"\bWARN(?:ING)?\b", "WARNING"),
    (r"\bINFO\b", "INFO"),
]

# Enrichment mapping: tipo_erro -> (causa, impacto, recomendacao)
_ENRICHMENT_MAP: dict = {
    "INPUT_LOSS": (
        "Perda de sinal de entrada",
        "Canal fora do ar",
        "Verificar fonte de entrada e conectividade",
    ),
    "BITRATE_DROP": (
        "Degradação na qualidade do sinal",
        "Qualidade de vídeo reduzida",
        "Verificar largura de banda e encoder",
    ),
    "LATENCY_SPIKE": (
        "Aumento na latência de processamento",
        "Atraso na entrega do conteúdo",
        "Verificar carga do pipeline e rede",
    ),
    "OUTPUT_FAILURE": (
        "Falha na entrega do output",
        "Interrupção na distribuição",
        "Verificar destino de output e permissões",
    ),
    "ENCODER_ERROR": (
        "Erro no processo de encoding",
        "Possível interrupção do canal",
        "Verificar configurações de codec e input",
    ),
    "AD_INSERTION_FAILURE": (
        "Falha na inserção de anúncio",
        "Anúncios não exibidos",
        (
            "Verificar configuração do MediaTailor"
            " e ad server"
        ),
    ),
    "CDN_DISTRIBUTION_ERROR": (
        "Erro na distribuição CDN",
        "Conteúdo indisponível para viewers",
        (
            "Verificar configuração do CloudFront"
            " e origins"
        ),
    ),
    "CDN_ORIGIN_ERROR": (
        "Erro na origin do CDN",
        "Falha na busca de conteúdo",
        "Verificar origin server e conectividade",
    ),
    "CDN_CACHE_ERROR": (
        "Erro no cache CDN",
        "Performance degradada",
        "Verificar cache policy e invalidações",
    ),
}

_DEFAULT_ENRICHMENT = (
    "Causa não identificada",
    "Impacto a ser avaliado",
    "Investigar logs detalhados",
)


def _epoch_ms_to_iso(epoch_ms) -> str:
    """Convert epoch milliseconds to ISO 8601 UTC string."""
    try:
        ts = int(epoch_ms)
        dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError):
        return str(epoch_ms)


def _classify_tipo_erro(message: str) -> str:
    """Extract tipo_erro from a log message string."""
    lower = message.lower()
    for pattern, tipo in _ERROR_PATTERNS:
        if re.search(pattern, lower):
            return tipo
    return "OTHER"


def _classify_severidade(message: str) -> str:
    """Extract severity from a log message string."""
    for pattern, sev in _SEVERITY_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE):
            return sev
    return "INFO"


def _extract_canal(raw_log: dict) -> str:
    """Best-effort extraction of channel id from raw log."""
    # Try explicit channel_id field first
    for key in ("channel_id", "channelId", "canal"):
        val = raw_log.get(key)
        if val and str(val).strip():
            return str(val)

    # Try to extract from logStreamName (often contains id)
    stream = raw_log.get("logStreamName", "")
    parts = stream.split("/")
    if len(parts) > 1 and parts[-1].strip():
        return parts[-1]

    # Try logGroupName
    group = raw_log.get("logGroupName", "")
    parts = group.split("/")
    if len(parts) > 1 and parts[-1].strip():
        return parts[-1]

    return "unknown"


def normalize_cloudwatch_log(
    raw_log: dict,
    servico_origem: str,
) -> dict:
    """Normalize a raw CloudWatch log entry into Evento_Estruturado.

    Parameters
    ----------
    raw_log : dict
        Raw log entry with fields like timestamp (epoch ms),
        message (str), logStreamName, logGroupName.
    servico_origem : str
        Service the log came from (MediaLive, MediaPackage,
        MediaTailor, CloudFront, CloudWatch).

    Returns
    -------
    dict  Evento_Estruturado format.
    """
    message = raw_log.get("message", "")
    timestamp_raw = raw_log.get("timestamp", "")

    # Convert epoch ms to ISO 8601 if numeric
    if isinstance(timestamp_raw, (int, float)):
        timestamp = _epoch_ms_to_iso(timestamp_raw)
    elif isinstance(timestamp_raw, str) and timestamp_raw.isdigit():
        timestamp = _epoch_ms_to_iso(int(timestamp_raw))
    else:
        timestamp = str(timestamp_raw) if timestamp_raw else ""

    canal = _extract_canal(raw_log)
    severidade = _classify_severidade(message)
    tipo_erro = _classify_tipo_erro(message)
    descricao = message if message else "Sem descrição"

    return {
        "timestamp": timestamp,
        "canal": canal,
        "severidade": severidade,
        "tipo_erro": tipo_erro,
        "descricao": descricao,
        "servico_origem": servico_origem,
        "log_group": raw_log.get("logGroupName", ""),
        "log_stream": raw_log.get("logStreamName", ""),
    }


def enrich_evento(evento: dict) -> dict:
    """Enrich an Evento_Estruturado with root-cause analysis.

    Adds causa_provavel, impacto_estimado and
    recomendacao_correcao based on tipo_erro.

    Returns a new dict with the enrichment fields added.
    """
    tipo = evento.get("tipo_erro", "")
    causa, impacto, rec = _ENRICHMENT_MAP.get(
        tipo, _DEFAULT_ENRICHMENT,
    )
    enriched = dict(evento)
    enriched["causa_provavel"] = causa
    enriched["impacto_estimado"] = impacto
    enriched["recomendacao_correcao"] = rec
    return enriched
