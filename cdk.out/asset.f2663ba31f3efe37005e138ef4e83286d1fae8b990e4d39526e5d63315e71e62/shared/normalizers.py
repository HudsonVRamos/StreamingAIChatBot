"""Normalization module — flat JSON output for RAG and Export.

Each normalizer produces a flat dict (no nesting) with all relevant
fields extracted from the raw AWS API response.

Validates: Requirements 5.2, 11.1, 11.2, 11.3
"""

from typing import Any, Dict, List, Optional


def _safe_get(d: dict, *keys: str, default: Any = None) -> Any:
    """Safely traverse nested dicts."""
    current = d
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key, default)
        else:
            return default
    return current


# ===================================================================
# MediaLive
# ===================================================================


def normalize_medialive_config(raw_config: dict) -> Dict[str, Any]:
    """Normalize a raw MediaLive DescribeChannel response — flat output."""
    channel_id = str(
        raw_config.get("Id", raw_config.get("ChannelId", raw_config.get("Name", "")))
    )

    encoder = _safe_get(raw_config, "EncoderSettings") or {}
    video_descs = encoder.get("VideoDescriptions", [])
    audio_descs = encoder.get("AudioDescriptions", [])
    caption_descs = encoder.get("CaptionDescriptions", [])
    output_groups = encoder.get("OutputGroups", [])

    # --- Video: all resolutions, codecs, bitrates ---
    resolucoes = []
    bitrates = []
    codec_video = None
    gop_size = None
    gop_unit = None
    framerate = None

    for vd in video_descs:
        w = vd.get("Width")
        h = vd.get("Height")
        if w and h:
            resolucoes.append(f"{w}x{h}")

        cs = vd.get("CodecSettings", {})
        for codec_key, codec_name in [("H264Settings", "H.264"), ("H265Settings", "H.265"), ("Mpeg2Settings", "MPEG-2")]:
            settings = cs.get(codec_key, {})
            if settings:
                codec_video = codec_name
                br = settings.get("MaxBitrate") or settings.get("Bitrate")
                if br:
                    bitrates.append(br)
                if gop_size is None:
                    gop_size = settings.get("GopSize")
                    gop_unit = settings.get("GopSizeUnits")
                if framerate is None:
                    num = settings.get("FramerateNumerator")
                    den = settings.get("FramerateDenominator")
                    if num and den and den != 0:
                        framerate = round(num / den, 2)

    # --- Audio ---
    result = {
        "channel_id": channel_id,
        "servico": "MediaLive",
        "tipo": "configuracao",
        "nome_canal": raw_config.get("Name"),
        "estado": raw_config.get("State"),
        "regiao": raw_config.get("Arn", "").split(":")[3] if raw_config.get("Arn") else None,
        "channel_class": raw_config.get("ChannelClass"),
        "codec_video": codec_video,
        "gop_size": gop_size,
        "gop_unit": gop_unit,
        "framerate": framerate,
        "resolucoes": ", ".join(resolucoes) if resolucoes else None,
        "resolucao_count": len(resolucoes),
        "resolucao_max": resolucoes[-1] if resolucoes else None,
        "bitrates": ", ".join(str(b) for b in bitrates) if bitrates else None,
        "bitrate_max": max(bitrates) if bitrates else None,
        "audio_count": len(audio_descs),
    }

    # Audio details (up to 4)
    for i, ad in enumerate(audio_descs[:4], 1):
        result[f"audio_{i}_name"] = ad.get("Name")
        result[f"audio_{i}_language"] = ad.get("LanguageCode")
        aac = _safe_get(ad, "CodecSettings", "AacSettings")
        if aac:
            result[f"audio_{i}_codec"] = "AAC"
            result[f"audio_{i}_bitrate"] = aac.get("Bitrate")
        else:
            result[f"audio_{i}_codec"] = None
            result[f"audio_{i}_bitrate"] = None

    # --- Captions ---
    result["caption_count"] = len(caption_descs)
    for i, cd in enumerate(caption_descs[:4], 1):
        result[f"caption_{i}_name"] = cd.get("Name")
        result[f"caption_{i}_language"] = cd.get("LanguageCode")
        dest = cd.get("DestinationSettings", {})
        if dest.get("WebvttDestinationSettings"):
            result[f"caption_{i}_type"] = "WEBVTT"
        elif dest.get("DvbSubDestinationSettings"):
            result[f"caption_{i}_type"] = "DVB_SUB"
        else:
            result[f"caption_{i}_type"] = cd.get("CaptionSelectorName")

    # --- Inputs ---
    inputs = raw_config.get("InputAttachments", [])
    result["input_count"] = len(inputs)

    for i, inp in enumerate(inputs[:2], 1):
        result[f"input_{i}_name"] = inp.get("InputAttachmentName")
        result[f"input_{i}_id"] = inp.get("InputId")

        inp_settings = inp.get("InputSettings", {})

        # Audio PIDs
        audio_sels = inp_settings.get("AudioSelectors", [])
        for j, asel in enumerate(audio_sels[:4], 1):
            pid = _safe_get(asel, "SelectorSettings", "AudioPidSelection", "Pid")
            result[f"input_{i}_audio_{j}_pid"] = pid
            result[f"input_{i}_audio_{j}_name"] = asel.get("Name")

        # Caption selectors
        cap_sels = inp_settings.get("CaptionSelectors", [])
        for j, csel in enumerate(cap_sels[:4], 1):
            ss = csel.get("SelectorSettings", {})
            dvb = ss.get("DvbSubSourceSettings", {})
            if dvb:
                result[f"input_{i}_caption_{j}_type"] = "DVB_SUB"
                result[f"input_{i}_caption_{j}_pid"] = dvb.get("Pid")
                result[f"input_{i}_caption_{j}_language"] = dvb.get("OcrLanguage")
            else:
                result[f"input_{i}_caption_{j}_type"] = csel.get("Name")

        # Video PID / Program ID
        vs = inp_settings.get("VideoSelector", {})
        prog_id = _safe_get(vs, "SelectorSettings", "VideoSelectorProgramId", "ProgramId")
        result[f"input_{i}_program_id"] = prog_id

    # Failover
    failover = _safe_get(inputs[0], "AutomaticInputFailoverSettings") if inputs else None
    result["failover_enabled"] = failover is not None
    if failover:
        result["failover_threshold_ms"] = _safe_get(
            failover, "FailoverConditions", default=[{}]
        )[0].get("FailoverConditionSettings", {}).get("InputLossSettings", {}).get("InputLossThresholdMsec") if failover.get("FailoverConditions") else None

    # --- Input Specification ---
    ispec = raw_config.get("InputSpecification", {})
    result["input_codec"] = ispec.get("Codec")
    result["input_max_bitrate"] = ispec.get("MaximumBitrate")
    result["input_resolution"] = ispec.get("Resolution")

    # --- Output ---
    if output_groups:
        og = output_groups[0]
        og_settings = og.get("OutputGroupSettings", {})
        hls = og_settings.get("HlsGroupSettings", {})
        dash = og_settings.get("DashIsoGroupSettings", {})
        if hls:
            result["output_type"] = "HLS"
            result["segment_length"] = hls.get("SegmentLength")
            result["destination_id"] = _safe_get(hls, "Destination", "DestinationRefId")
        elif dash:
            result["output_type"] = "DASH"
            result["segment_length"] = dash.get("SegmentLength")
            result["destination_id"] = _safe_get(dash, "Destination", "DestinationRefId")
        else:
            result["output_type"] = None
            result["segment_length"] = None
            result["destination_id"] = None

        # M3u8 PIDs from first output
        outputs = og.get("Outputs", [])
        if outputs:
            m3u8 = _safe_get(outputs[0], "OutputSettings", "HlsOutputSettings", "HlsSettings", "StandardHlsSettings", "M3u8Settings") or {}
            result["video_pid"] = m3u8.get("VideoPid")
            result["audio_pids"] = m3u8.get("AudioPids")
            result["pmt_pid"] = m3u8.get("PmtPid")
            result["program_num"] = m3u8.get("ProgramNum")
    else:
        result["output_type"] = None
        result["segment_length"] = None

    # Destination URL
    dests = raw_config.get("Destinations", [])
    if dests and dests[0].get("Settings"):
        result["destination_url"] = dests[0]["Settings"][0].get("Url")
    else:
        result["destination_url"] = None

    return result


# ===================================================================
# MediaPackage V2
# ===================================================================


def normalize_mediapackage_config(raw_config: dict) -> Dict[str, Any]:
    """Normalize a MediaPackage V2 channel + endpoints — flat output."""
    channel_id = str(
        raw_config.get("ChannelName",
            raw_config.get("Id",
                raw_config.get("ChannelId",
                    raw_config.get("Arn", ""))))
    )

    result = {
        "channel_id": channel_id,
        "servico": "MediaPackage",
        "tipo": "configuracao",
        "nome_canal": raw_config.get("ChannelName") or raw_config.get("Description") or raw_config.get("Id"),
        "regiao": raw_config.get("Arn", "").split(":")[3] if raw_config.get("Arn") else None,
        "channel_group": raw_config.get("ChannelGroupName"),
        "input_type": raw_config.get("InputType", "HLS"),
    }

    # Origin endpoints
    endpoints = raw_config.get("OriginEndpoints") or []
    result["endpoint_count"] = len(endpoints)

    hls_ep = None
    dash_ep = None

    for ep in endpoints:
        name = ep.get("OriginEndpointName", "")
        segment = ep.get("Segment", {})
        encryption = segment.get("Encryption", {})
        speke = encryption.get("SpekeKeyProvider", {})
        drm_systems = speke.get("DrmSystems", [])

        if ep.get("HlsManifests") or "HLS" in name.upper():
            hls_ep = ep
            enc_method = _safe_get(encryption, "EncryptionMethod", "CmafEncryptionMethod")
            result["endpoint_hls_name"] = name
            result["endpoint_hls_container"] = ep.get("ContainerType")
            result["endpoint_hls_segment_duration"] = segment.get("SegmentDurationSeconds")
            result["endpoint_hls_startover_seconds"] = ep.get("StartoverWindowSeconds")
            result["endpoint_hls_drm"] = ", ".join(drm_systems) if drm_systems else None
            result["endpoint_hls_encryption"] = enc_method
            result["endpoint_hls_dvb_subtitles"] = segment.get("TsIncludeDvbSubtitles")
            manifests = ep.get("HlsManifests", [])
            if manifests:
                result["endpoint_hls_manifest_window"] = manifests[0].get("ManifestWindowSeconds")

        if ep.get("DashManifests") or "DASH" in name.upper():
            dash_ep = ep
            enc_method = _safe_get(encryption, "EncryptionMethod", "CmafEncryptionMethod")
            result["endpoint_dash_name"] = name
            result["endpoint_dash_container"] = ep.get("ContainerType")
            result["endpoint_dash_segment_duration"] = segment.get("SegmentDurationSeconds")
            result["endpoint_dash_startover_seconds"] = ep.get("StartoverWindowSeconds")
            result["endpoint_dash_drm"] = ", ".join(drm_systems) if drm_systems else None
            result["endpoint_dash_encryption"] = enc_method
            manifests = ep.get("DashManifests", [])
            if manifests:
                result["endpoint_dash_manifest_window"] = manifests[0].get("ManifestWindowSeconds")

    # DRM resource ID (from any endpoint)
    for ep in endpoints:
        rid = _safe_get(ep, "Segment", "Encryption", "SpekeKeyProvider", "ResourceId")
        if rid:
            result["drm_resource_id"] = rid
            break

    return result


# ===================================================================
# MediaTailor
# ===================================================================


def normalize_mediatailor_config(raw_config: dict) -> Dict[str, Any]:
    """Normalize a MediaTailor GetPlaybackConfiguration — flat output."""
    channel_id = str(
        raw_config.get("Name", raw_config.get("PlaybackConfigurationArn", ""))
    )

    return {
        "channel_id": channel_id,
        "servico": "MediaTailor",
        "tipo": "configuracao",
        "nome_canal": raw_config.get("Name"),
        "regiao": (
            raw_config.get("PlaybackConfigurationArn", "").split(":")[3]
            if raw_config.get("PlaybackConfigurationArn")
            else None
        ),
        "ad_server_url": raw_config.get("AdDecisionServerUrl"),
        "video_source_url": raw_config.get("VideoContentSourceUrl"),
        "cdn_segment_prefix": _safe_get(raw_config, "CdnConfiguration", "ContentSegmentUrlPrefix"),
        "dash_mpd_location": _safe_get(raw_config, "DashConfiguration", "MpdLocation"),
        "dash_origin_manifest": _safe_get(raw_config, "DashConfiguration", "OriginManifestType"),
        "avail_suppression_mode": _safe_get(raw_config, "AvailSuppression", "Mode"),
        "avail_fill_policy": _safe_get(raw_config, "AvailSuppression", "FillPolicy"),
        "preroll_enabled": raw_config.get("LivePreRollConfiguration") is not None,
        "ad_marker_passthrough": _safe_get(raw_config, "ManifestProcessingRules", "AdMarkerPassthrough", "Enabled"),
        "stream_conditioning": _safe_get(raw_config, "AdConditioningConfiguration", "StreamingMediaFileConditioning"),
    }


# ===================================================================
# CloudFront
# ===================================================================


def normalize_cloudfront_config(raw_config: dict) -> Dict[str, Any]:
    """Normalize a CloudFront GetDistribution response — flat output."""
    distribution = raw_config.get("Distribution", raw_config)
    dist_config = distribution.get("DistributionConfig", distribution)

    channel_id = str(distribution.get("Id", raw_config.get("Id", "")))

    # Origins
    origins_data = dist_config.get("Origins", {})
    origin_items = origins_data.get("Items", []) if isinstance(origins_data, dict) else []

    # Aliases
    aliases = dist_config.get("Aliases", {})
    alias_items = aliases.get("Items", []) if isinstance(aliases, dict) else []

    # Cache behaviors
    cache_behaviors = dist_config.get("CacheBehaviors", {})
    cb_items = cache_behaviors.get("Items", []) if isinstance(cache_behaviors, dict) else []

    # Default cache behavior
    default_cb = dist_config.get("DefaultCacheBehavior") or {}

    # CloudFront Functions
    cf_functions = set()
    # From default behavior
    for fa in (default_cb.get("FunctionAssociations", {}).get("Items", []) or []):
        arn = fa.get("FunctionARN", "")
        name = arn.split("/")[-1] if "/" in arn else arn
        if name:
            cf_functions.add(name)
    # From cache behaviors
    for cb in cb_items:
        for fa in (cb.get("FunctionAssociations", {}).get("Items", []) or []):
            arn = fa.get("FunctionARN", "")
            name = arn.split("/")[-1] if "/" in arn else arn
            if name:
                cf_functions.add(name)

    # Detect origin types
    has_mediatailor = any("mediatailor" in o.get("DomainName", "").lower() for o in origin_items)
    has_mediapackage = any("mediapackage" in o.get("DomainName", "").lower() for o in origin_items)

    result = {
        "channel_id": channel_id,
        "servico": "CloudFront",
        "tipo": "configuracao",
        "nome_canal": dist_config.get("Comment") or channel_id,
        "estado": distribution.get("Status"),
        "domain_name": distribution.get("DomainName"),
        "alias": alias_items[0] if alias_items else None,
        "alias_count": len(alias_items),
        "enabled": dist_config.get("Enabled"),
        "http_version": dist_config.get("HttpVersion"),
        "ipv6_enabled": dist_config.get("IsIPV6Enabled"),
        "price_class": dist_config.get("PriceClass"),
        "ssl_protocol": _safe_get(dist_config, "ViewerCertificate", "MinimumProtocolVersion"),
        "origin_count": len(origin_items),
        "has_mediatailor_origin": has_mediatailor,
        "has_mediapackage_origin": has_mediapackage,
        "default_behavior_origin": default_cb.get("TargetOriginId"),
        "default_behavior_protocol": default_cb.get("ViewerProtocolPolicy"),
        "cache_behavior_count": len(cb_items),
        "cf_function_count": len(cf_functions),
        "cf_functions": ", ".join(sorted(cf_functions)) if cf_functions else None,
        "logging_enabled": _safe_get(dist_config, "Logging", "Enabled"),
        "logging_bucket": _safe_get(dist_config, "Logging", "Bucket"),
        "geo_restriction": _safe_get(dist_config, "Restrictions", "GeoRestriction", "RestrictionType"),
    }

    # Origin details (up to 4)
    for i, o in enumerate(origin_items[:4], 1):
        result[f"origin_{i}_id"] = o.get("Id")
        result[f"origin_{i}_domain"] = o.get("DomainName")
        result[f"origin_{i}_path"] = o.get("OriginPath") or None
        result[f"origin_{i}_shield"] = _safe_get(o, "OriginShield", "Enabled")
        result[f"origin_{i}_shield_region"] = _safe_get(o, "OriginShield", "OriginShieldRegion")

    return result


# ===================================================================
# Evento_Estruturado normalization & enrichment
# ===================================================================

import re
from datetime import datetime, timezone

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

_SEVERITY_PATTERNS: list = [
    (r"\bCRITICAL\b", "CRITICAL"),
    (r"\bERROR\b", "ERROR"),
    (r"\bWARN(?:ING)?\b", "WARNING"),
    (r"\bINFO\b", "INFO"),
]

_ENRICHMENT_MAP: dict = {
    "INPUT_LOSS": ("Perda de sinal de entrada", "Canal fora do ar", "Verificar fonte de entrada e conectividade"),
    "BITRATE_DROP": ("Degradação na qualidade do sinal", "Qualidade de vídeo reduzida", "Verificar largura de banda e encoder"),
    "LATENCY_SPIKE": ("Aumento na latência de processamento", "Atraso na entrega do conteúdo", "Verificar carga do pipeline e rede"),
    "OUTPUT_FAILURE": ("Falha na entrega do output", "Interrupção na distribuição", "Verificar destino de output e permissões"),
    "ENCODER_ERROR": ("Erro no processo de encoding", "Possível interrupção do canal", "Verificar configurações de codec e input"),
    "AD_INSERTION_FAILURE": ("Falha na inserção de anúncio", "Anúncios não exibidos", "Verificar configuração do MediaTailor e ad server"),
    "CDN_DISTRIBUTION_ERROR": ("Erro na distribuição CDN", "Conteúdo indisponível para viewers", "Verificar configuração do CloudFront e origins"),
    "CDN_ORIGIN_ERROR": ("Erro na origin do CDN", "Falha na busca de conteúdo", "Verificar origin server e conectividade"),
    "CDN_CACHE_ERROR": ("Erro no cache CDN", "Performance degradada", "Verificar cache policy e invalidações"),
}

_DEFAULT_ENRICHMENT = ("Causa não identificada", "Impacto a ser avaliado", "Investigar logs detalhados")


def _epoch_ms_to_iso(epoch_ms) -> str:
    try:
        ts = int(epoch_ms)
        dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError):
        return str(epoch_ms)


def _classify_tipo_erro(message: str) -> str:
    lower = message.lower()
    for pattern, tipo in _ERROR_PATTERNS:
        if re.search(pattern, lower):
            return tipo
    return "OTHER"


def _classify_severidade(message: str) -> str:
    for pattern, sev in _SEVERITY_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE):
            return sev
    return "INFO"


def _extract_canal(raw_log: dict) -> str:
    for key in ("channel_id", "channelId", "canal"):
        val = raw_log.get(key)
        if val and str(val).strip():
            return str(val)
    stream = raw_log.get("logStreamName", "")
    parts = stream.split("/")
    if len(parts) > 1 and parts[-1].strip():
        return parts[-1]
    group = raw_log.get("logGroupName", "")
    parts = group.split("/")
    if len(parts) > 1 and parts[-1].strip():
        return parts[-1]
    return "unknown"


def normalize_cloudwatch_log(raw_log: dict, servico_origem: str) -> dict:
    """Normalize a raw CloudWatch log entry into Evento_Estruturado."""
    message = raw_log.get("message", "")
    timestamp_raw = raw_log.get("timestamp", "")

    if isinstance(timestamp_raw, (int, float)):
        timestamp = _epoch_ms_to_iso(timestamp_raw)
    elif isinstance(timestamp_raw, str) and timestamp_raw.isdigit():
        timestamp = _epoch_ms_to_iso(int(timestamp_raw))
    else:
        timestamp = str(timestamp_raw) if timestamp_raw else ""

    return {
        "timestamp": timestamp,
        "canal": _extract_canal(raw_log),
        "severidade": _classify_severidade(message),
        "tipo_erro": _classify_tipo_erro(message),
        "descricao": message if message else "Sem descrição",
        "servico_origem": servico_origem,
        "log_group": raw_log.get("logGroupName", ""),
        "log_stream": raw_log.get("logStreamName", ""),
    }


def enrich_evento(evento: dict) -> dict:
    """Enrich an Evento_Estruturado with root-cause analysis."""
    tipo = evento.get("tipo_erro", "")
    causa, impacto, rec = _ENRICHMENT_MAP.get(tipo, _DEFAULT_ENRICHMENT)
    enriched = dict(evento)
    enriched["causa_provavel"] = causa
    enriched["impacto_estimado"] = impacto
    enriched["recomendacao_correcao"] = rec
    return enriched
