"""Lambda Configuradora — creates and modifies streaming resources.

Invoked by the Bedrock Action_Group_Config to execute AWS API calls for
MediaLive, MediaPackage, MediaTailor and CloudFront resources.  Every
operation (success or failure) is recorded as an audit log entry in S3.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
import logging
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError
from botocore.config import Config as BotoConfig

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AUDIT_BUCKET = os.environ.get("AUDIT_BUCKET", "")
AUDIT_PREFIX = os.environ.get("AUDIT_PREFIX", "audit/")
EXPORTS_BUCKET = os.environ.get("EXPORTS_BUCKET", "")
EXPORTS_PREFIX = os.environ.get("EXPORTS_PREFIX", "exports/")
PRESIGNED_URL_EXPIRY = int(
    os.environ.get("PRESIGNED_URL_EXPIRY", "3600"),
)
SPEKE_ROLE_ARN = os.environ.get("SPEKE_ROLE_ARN", "")
SPEKE_URL = os.environ.get("SPEKE_URL", "")
CDN_SECRET_ROLE_ARN = os.environ.get(
    "CDN_SECRET_ROLE_ARN", "",
)
CDN_SECRET_ARN = os.environ.get("CDN_SECRET_ARN", "")

s3_client = boto3.client("s3")
s3_presign = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
    config=BotoConfig(signature_version="s3v4"),
)
_STREAMING_REGION = os.environ.get("STREAMING_REGION", "sa-east-1")
_MEDIATAILOR_REGION = os.environ.get("MEDIATAILOR_REGION", "us-east-1")
medialive_client = boto3.client("medialive", region_name=_STREAMING_REGION)
mediapackage_client = boto3.client("mediapackage", region_name=_STREAMING_REGION)
mediapackagev2_client = boto3.client(
    "mediapackagev2", region_name=_STREAMING_REGION,
)
mediatailor_client = boto3.client(
    "mediatailor", region_name=_MEDIATAILOR_REGION,
)
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


@dataclass
class RollbackEntry:
    servico: str              # "MediaPackage" ou "MediaLive"
    tipo_recurso: str         # "channel_v2", "origin_endpoint_v2", "input", "channel"
    resource_id: str          # ID do recurso criado
    channel_group: str = ""   # Necessário para MPV2 (channel e endpoint)
    channel_name: str = ""    # Necessário para MPV2 endpoint
    endpoint_name: str = ""   # Necessário para MPV2 endpoint


@dataclass
class OrchestrationParams:
    nome_canal: str
    channel_group: str
    template_resource_id: str
    segment_duration: int = 6
    drm_resource_id: str = ""       # Default: "Live_{nome_canal}"
    manifest_window_seconds: int = 7200
    startover_window_hls_seconds: int = 900
    startover_window_dash_seconds: int = 14460
    ts_include_dvb_subtitles: bool = True
    min_buffer_time_seconds: int = 2
    suggested_presentation_delay_seconds: int = 12


@dataclass
class OrchestrationResult:
    success: bool
    recursos_criados: dict[str, Any]  # tipo -> identificador
    ingest_url: str = ""
    canal_medialive_id: str = ""
    erro: str = ""
    rollback_executado: bool = False
    recursos_removidos: list[str] = field(default_factory=list)
    recursos_falha_remocao: list[str] = field(default_factory=list)


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
        elif tipo_recurso == "channel_v2":
            resp = mediapackagev2_client.create_channel(**config_json)
            return {"resource_id": resp.get("ChannelName", ""), "details": resp}
        elif tipo_recurso == "origin_endpoint_v2":
            resp = mediapackagev2_client.create_origin_endpoint(**config_json)
            return {"resource_id": resp.get("OriginEndpointName", ""), "details": resp}

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
# AWS API helpers — delete (used by rollback)
# ---------------------------------------------------------------------------


def delete_resource(entry: RollbackEntry) -> dict[str, Any]:
    """Delete an AWS resource described by a RollbackEntry.

    Supports MediaPackage V2 (channel_v2, origin_endpoint_v2) and
    MediaLive (input, channel).  Returns a dict with the deletion
    status and resource identifier.
    """
    if entry.servico == "MediaPackage":
        if entry.tipo_recurso == "channel_v2":
            mediapackagev2_client.delete_channel(
                ChannelGroupName=entry.channel_group,
                ChannelName=entry.channel_name or entry.resource_id,
            )
            return {
                "status": "deleted",
                "resource_id": entry.resource_id,
            }
        elif entry.tipo_recurso == "origin_endpoint_v2":
            mediapackagev2_client.delete_origin_endpoint(
                ChannelGroupName=entry.channel_group,
                ChannelName=entry.channel_name,
                OriginEndpointName=entry.endpoint_name or entry.resource_id,
            )
            return {
                "status": "deleted",
                "resource_id": entry.resource_id,
            }

    elif entry.servico == "MediaLive":
        if entry.tipo_recurso == "input":
            medialive_client.delete_input(InputId=entry.resource_id)
            return {
                "status": "deleted",
                "resource_id": entry.resource_id,
            }
        elif entry.tipo_recurso == "channel":
            medialive_client.delete_channel(ChannelId=entry.resource_id)
            return {
                "status": "deleted",
                "resource_id": entry.resource_id,
            }

    raise ValueError(
        f"Operação de exclusão não suportada: "
        f"{entry.servico}/{entry.tipo_recurso}"
    )


# ---------------------------------------------------------------------------
# AWS API helpers — create from template
# ---------------------------------------------------------------------------


def _create_inputs_for_channel(
    channel_name: str,
    channel_class: str,
    template_attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create MediaLive inputs for a new channel and return InputAttachments.

    Naming convention:
    - SINGLE_PIPELINE: {name}_INPUT_1, {name}_INPUT_2 (with failover)
    - STANDARD: {name}_INPUT (single input)

    Detects input type from the template's existing inputs.
    Creates inputs via MediaLive API and builds InputAttachments
    with the same InputSettings (audio/caption selectors, etc.)
    from the template.
    """
    # Detect input type from template
    input_type = "UDP_PUSH"  # default
    if template_attachments:
        first_id = template_attachments[0].get("InputId", "")
        if first_id:
            try:
                desc = medialive_client.describe_input(
                    InputId=first_id,
                )
                input_type = desc.get("Type", "UDP_PUSH")
            except ClientError:
                pass

    # Copy InputSettings from template (selectors, filters)
    template_settings = {}
    if template_attachments:
        template_settings = dict(
            template_attachments[0].get("InputSettings", {}),
        )

    is_single = channel_class == "SINGLE_PIPELINE"
    input_count = 2 if is_single else 1

    created_inputs = []
    for i in range(1, input_count + 1):
        if is_single:
            inp_name = f"{channel_name}_INPUT_{i}"
        else:
            inp_name = f"{channel_name}_INPUT"

        try:
            create_params = {
                "Name": inp_name,
                "Type": input_type,
            }

            if input_type == "RTP_PUSH":
                # RTP: use fixed VPC config
                create_params["Vpc"] = {
                    "SecurityGroupIds": [
                        os.environ.get(
                            "RTP_SECURITY_GROUP",
                            "sg-079b02876e4a0003d",
                        ),
                    ],
                    "SubnetIds": [
                        os.environ.get(
                            "RTP_SUBNET_1",
                            "subnet-0fe1897129a56fecd",
                        ),
                        os.environ.get(
                            "RTP_SUBNET_2",
                            "subnet-0fa22e4baf39978a1",
                        ),
                    ],
                }
                # Use first available MediaLive role
                try:
                    roles = medialive_client.list_input_security_groups()
                    # RoleArn from first existing input
                    if template_attachments:
                        first_id = template_attachments[0].get(
                            "InputId", "",
                        )
                        if first_id:
                            d = medialive_client.describe_input(
                                InputId=first_id,
                            )
                            role = d.get("RoleArn", "")
                            if role:
                                create_params["RoleArn"] = role
                except ClientError:
                    pass

            elif input_type == "SRT_CALLER":
                # SRT Caller: needs SrtCallerSources
                create_params["SrtSettings"] = {
                    "SrtCallerSources": [{
                        "SrtListenerAddress": "1.1.1.1",
                        "SrtListenerPort": "5000",
                        "StreamId": channel_name,
                    }],
                }

            else:
                # Other types (UDP_PUSH, etc): use input security group
                isg = []
                try:
                    sg_resp = (
                        medialive_client
                        .list_input_security_groups()
                    )
                    sgs = sg_resp.get(
                        "InputSecurityGroups", [],
                    )
                    if sgs:
                        isg = [sgs[0].get("Id", "")]
                except ClientError:
                    pass
                if isg:
                    create_params["InputSecurityGroups"] = isg

            resp = medialive_client.create_input(
                **create_params,
            )
            inp = resp.get("Input", {})
            created_inputs.append({
                "id": inp.get("Id", ""),
                "name": inp_name,
            })
            logger.info(
                "Created input %s (ID: %s)",
                inp_name, inp.get("Id"),
            )
        except ClientError as exc:
            logger.error(
                "Failed to create input %s: %s",
                inp_name, exc,
            )
            raise

    # Build InputAttachments
    attachments = []
    for idx, inp in enumerate(created_inputs):
        att = {
            "InputAttachmentName": inp["name"],
            "InputId": inp["id"],
            "InputSettings": dict(template_settings),
        }
        # First input in SINGLE_PIPELINE gets failover
        if is_single and idx == 0 and len(created_inputs) > 1:
            att["AutomaticInputFailoverSettings"] = {
                "ErrorClearTimeMsec": 30000,
                "FailoverConditions": [{
                    "FailoverConditionSettings": {
                        "InputLossSettings": {
                            "InputLossThresholdMsec": 10000,
                        },
                    },
                }],
                "InputPreference": "PRIMARY_INPUT_PREFERRED",
                "SecondaryInputId": created_inputs[1]["id"],
            }
        attachments.append(att)

    return attachments


def _build_endpoint_config(params: OrchestrationParams, endpoint_type: str) -> dict[str, Any]:
    """Build the JSON payload for an HLS or DASH origin endpoint on MPV2.

    Parameters
    ----------
    params:
        Validated orchestration parameters.
    endpoint_type:
        ``"HLS"`` or ``"DASH"``.

    Returns
    -------
    dict
        Complete payload ready to be passed to
        ``mediapackagev2_client.create_origin_endpoint(**payload)``.
    """
    drm_resource_id = params.drm_resource_id or f"Live_{params.nome_canal}"

    # --- Encryption / SpekeKeyProvider (shared by both types) ---------------
    speke_key_provider: dict[str, Any] = {
        "EncryptionContractConfiguration": {
            "PresetSpeke20Audio": "SHARED",
            "PresetSpeke20Video": "SHARED",
        },
        "ResourceId": drm_resource_id,
        "DrmSystems": ["FAIRPLAY"] if endpoint_type == "HLS" else ["PLAYREADY", "WIDEVINE"],
        "RoleArn": SPEKE_ROLE_ARN,
        "Url": SPEKE_URL,
    }

    encryption_method = (
        "CBCS" if endpoint_type == "HLS" else "CENC"
    )

    encryption: dict[str, Any] = {
        "EncryptionMethod": {"CmafEncryptionMethod": encryption_method},
        "SpekeKeyProvider": speke_key_provider,
    }

    # --- Segment ------------------------------------------------------------
    segment: dict[str, Any] = {
        "SegmentDurationSeconds": params.segment_duration,
        "SegmentName": "segment",
        "TsUseAudioRenditionGroup": True,
        "IncludeIframeOnlyStreams": False,
        "TsIncludeDvbSubtitles": params.ts_include_dvb_subtitles,
        "Encryption": encryption,
    }

    # --- Top-level config ---------------------------------------------------
    config: dict[str, Any] = {
        "ChannelGroupName": params.channel_group,
        "ChannelName": params.nome_canal,
        "OriginEndpointName": f"{params.nome_canal}_{endpoint_type}",
        "ContainerType": "CMAF",
        "Segment": segment,
    }

    if endpoint_type == "HLS":
        config["StartoverWindowSeconds"] = params.startover_window_hls_seconds
        config["HlsManifests"] = [
            {
                "ManifestName": "master",
                "ManifestWindowSeconds": params.manifest_window_seconds,
            },
        ]
    else:  # DASH
        config["StartoverWindowSeconds"] = params.startover_window_dash_seconds
        config["DashManifests"] = [
            {
                "ManifestName": "manifest",
                "ManifestWindowSeconds": params.manifest_window_seconds,
                "MinUpdatePeriodSeconds": params.segment_duration,
                "MinBufferTimeSeconds": params.min_buffer_time_seconds,
                "SuggestedPresentationDelaySeconds": params.suggested_presentation_delay_seconds,
                "SegmentTemplateFormat": "NUMBER_WITH_TIMELINE",
                "PeriodTriggers": [
                    "AVAILS",
                    "DRM_KEY_ROTATION",
                    "SOURCE_CHANGES",
                    "SOURCE_DISRUPTIONS",
                ],
                "DrmSignaling": "INDIVIDUAL",
                "UtcTiming": {"TimingMode": "UTC_DIRECT"},
            },
        ]

    return config


def _extract_ingest_url(create_channel_response: dict[str, Any]) -> str:
    """Extract the ingest URL from a MediaPackage V2 CreateChannel response.

    Parameters
    ----------
    create_channel_response:
        Raw response dict from ``mediapackagev2_client.create_channel()``.

    Returns
    -------
    str
        The first non-empty ``IngestEndpointUrl`` found.

    Raises
    ------
    ValueError
        If no ingest URL is present in the response.
    """
    for endpoint in create_channel_response.get("IngestEndpoints", []):
        url = endpoint.get("Url", "") or endpoint.get("IngestEndpointUrl", "")
        if url:
            return url
    raise ValueError("Nenhuma URL de ingestão encontrada na resposta do Canal MPV2")


def _execute_rollback(
    rollback_stack: list[RollbackEntry],
) -> tuple[list[str], list[str]]:
    """Delete previously created resources in reverse order.

    Parameters
    ----------
    rollback_stack:
        Resources to remove, ordered by creation time (earliest first).

    Returns
    -------
    tuple[list[str], list[str]]
        ``(recursos_removidos, recursos_falha_remocao)`` — resource IDs
        that were successfully deleted and those that failed.
    """
    recursos_removidos: list[str] = []
    recursos_falha_remocao: list[str] = []

    for entry in reversed(rollback_stack):
        try:
            delete_resource(entry)
            recursos_removidos.append(entry.resource_id)
            store_audit_log(
                build_audit_log(
                    operacao="rollback",
                    servico=entry.servico,
                    tipo_recurso=entry.tipo_recurso,
                    resource_id=entry.resource_id,
                    resultado="sucesso",
                ),
            )
        except Exception as exc:
            logger.error(
                "Rollback falhou para %s/%s (%s): %s",
                entry.servico,
                entry.tipo_recurso,
                entry.resource_id,
                exc,
            )
            recursos_falha_remocao.append(entry.resource_id)
            store_audit_log(
                build_audit_log(
                    operacao="rollback",
                    servico=entry.servico,
                    tipo_recurso=entry.tipo_recurso,
                    resource_id=entry.resource_id,
                    resultado="falha",
                    erro={"mensagem": str(exc)},
                ),
            )

    return recursos_removidos, recursos_falha_remocao


# ---------------------------------------------------------------------------
# MPV2 endpoint cloning — pure helpers
# ---------------------------------------------------------------------------

_MPV2_INGEST_URL_PATTERN = re.compile(
    r"https://.+\.mediapackagev2\..+\.amazonaws\.com"
    r"/in/v1/([^/]+)/\d+/([^/]+)/.*"
)


def _detect_template_mpv2_channel(
    template_destinations: list[dict[str, Any]],
) -> tuple[str, str] | None:
    """Detect the MPV2 channel associated with a MediaLive template.

    Inspects Destinations to find either:
    1. MediaPackageSettings with ChannelGroup + ChannelName
    2. Settings with CMAF ingest URL matching MPV2 pattern

    Returns:
        (channel_group, channel_name) or None if not detected.
    """
    for dest in template_destinations:
        # Try MediaPackageSettings first
        mps = dest.get("MediaPackageSettings")
        if isinstance(mps, list) and mps:
            first = mps[0]
            cg = first.get("ChannelGroup", "")
            cn = first.get("ChannelName", "")
            if cg and cn:
                return (cg, cn)

    # Fall through to CMAF URL parsing
    for dest in template_destinations:
        settings = dest.get("Settings")
        if isinstance(settings, list):
            for setting in settings:
                url = setting.get("Url", "")
                if url:
                    m = _MPV2_INGEST_URL_PATTERN.match(url)
                    if m:
                        return (m.group(1), m.group(2))

    return None


def _generate_cloned_endpoint_name(
    template_endpoint_config: dict[str, Any],
    template_channel_name: str,
    new_channel_name: str,
) -> str:
    """Generate the cloned endpoint name.

    Replaces the template channel name prefix in the original
    endpoint name with the new channel name. If the original
    name doesn't start with the template channel name, prepends
    the new channel name.

    Examples:
        ("LLHLS_INTERSTICIALS", "HLS_INTERSTICIALS_TEST", "NOVO") → "NOVO_LLHLS_INTERSTICIALS"
        ("0001_WARNER_CHANNEL_HLS", "0001_WARNER_CHANNEL", "NOVO") → "NOVO_HLS"
        ("0008_BAND_NEWS_LL_CBCS", "0008_BAND_NEWS_LL", "NOVO_LL") → "NOVO_LL_CBCS"
    """
    original_name = template_endpoint_config.get(
        "OriginEndpointName", "",
    )

    # Try to replace the template channel name prefix
    if original_name.startswith(template_channel_name):
        suffix = original_name[len(template_channel_name):]
        if suffix.startswith("_"):
            return f"{new_channel_name}{suffix}"
        elif suffix == "":
            return new_channel_name
        else:
            return f"{new_channel_name}_{suffix}"

    # Fallback: prepend new channel name
    return f"{new_channel_name}_{original_name}"


_ENDPOINT_READONLY_FIELDS = {
    "Arn", "CreatedAt", "ModifiedAt", "ETag", "Tags",
    "ResponseMetadata", "ResetAt",
}

# Fields accepted by create_origin_endpoint
_ENDPOINT_ALLOWED_FIELDS = {
    "ChannelGroupName", "ChannelName", "OriginEndpointName",
    "ContainerType", "Segment", "ClientToken", "Description",
    "StartoverWindowSeconds", "HlsManifests",
    "LowLatencyHlsManifests", "DashManifests", "MssManifests",
    "ForceEndpointErrorConfiguration", "Tags",
}


def _clone_endpoint_config(
    template_config: dict[str, Any],
    template_channel_name: str,
    new_channel_name: str,
    new_channel_group: str,
    drm_resource_id: str,
) -> dict[str, Any]:
    """Clone an endpoint configuration for a new channel.

    Performs deep copy, removes read-only fields, and substitutes
    channel-specific fields (names, DRM credentials, SPEKE config).

    Returns:
        New endpoint configuration ready for create_origin_endpoint().
    """
    config = deepcopy(template_config)

    # Keep only fields accepted by create_origin_endpoint
    keys_to_remove = [
        k for k in config if k not in _ENDPOINT_ALLOWED_FIELDS
    ]
    for k in keys_to_remove:
        del config[k]

    # Remove Url from each manifest entry
    for manifest_key in ("HlsManifests", "LowLatencyHlsManifests", "DashManifests"):
        for entry in config.get(manifest_key, []):
            entry.pop("Url", None)

    # Substitute identification fields
    config["ChannelGroupName"] = new_channel_group
    config["ChannelName"] = new_channel_name
    config["OriginEndpointName"] = _generate_cloned_endpoint_name(
        template_config, template_channel_name, new_channel_name,
    )

    # Substitute SpekeKeyProvider credentials
    speke = (
        config
        .get("Segment", {})
        .get("Encryption", {})
        .get("SpekeKeyProvider")
    )
    if speke:
        speke["ResourceId"] = drm_resource_id
        speke["RoleArn"] = SPEKE_ROLE_ARN
        speke["Url"] = SPEKE_URL

    return config


def _fetch_template_endpoints(
    channel_group: str,
    channel_name: str,
) -> list[dict[str, Any]]:
    """Fetch full configuration of all endpoints from a template MPV2 channel.

    Returns:
        List of endpoint configurations (from get_origin_endpoint).
        Empty list if no endpoints found or API error.
    """
    try:
        resp = mediapackagev2_client.list_origin_endpoints(
            ChannelGroupName=channel_group,
            ChannelName=channel_name,
        )
        items = resp.get("Items", [])
        if not items:
            logger.info(
                "No endpoints found for template %s/%s",
                channel_group, channel_name,
            )
            return []

        endpoints: list[dict[str, Any]] = []
        for item in items:
            ep_name = item.get("OriginEndpointName", "")
            if not ep_name:
                continue
            try:
                ep = mediapackagev2_client.get_origin_endpoint(
                    ChannelGroupName=channel_group,
                    ChannelName=channel_name,
                    OriginEndpointName=ep_name,
                )
                endpoints.append(ep)
            except ClientError as exc:
                logger.error(
                    "Failed to get endpoint %s/%s/%s: %s",
                    channel_group, channel_name, ep_name, exc,
                )
                return []
        return endpoints
    except ClientError as exc:
        logger.error(
            "Failed to list endpoints for %s/%s: %s",
            channel_group, channel_name, exc,
        )
        return []


def _apply_cdn_auth_policy(
    params: OrchestrationParams,
    endpoint_name: str,
) -> None:
    """Apply CDN auth policy to an endpoint. Logs warning on failure."""
    if not CDN_SECRET_ARN or not CDN_SECRET_ROLE_ARN:
        return

    try:
        policy = json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "AllowCloudFrontAccessWithCdnHeader",
                "Effect": "Allow",
                "Principal": "*",
                "Action": [
                    "mediapackagev2:GetObject",
                    "mediapackagev2:GetHeadObject",
                ],
                "Resource": (
                    f"arn:aws:mediapackagev2:sa-east-1:"
                    f"761018874615:channelGroup/"
                    f"{params.channel_group}/channel/"
                    f"{params.nome_canal}/originEndpoint/"
                    f"{endpoint_name}"
                ),
                "Condition": {
                    "Bool": {
                        "mediapackagev2:RequestHasMatchingCdnAuthHeader": "true",
                    },
                },
            }],
        })
        mediapackagev2_client.put_origin_endpoint_policy(
            ChannelGroupName=params.channel_group,
            ChannelName=params.nome_canal,
            OriginEndpointName=endpoint_name,
            Policy=policy,
            CdnAuthConfiguration={
                "SecretsRoleArn": CDN_SECRET_ROLE_ARN,
                "CdnIdentifierSecretArns": [CDN_SECRET_ARN],
            },
        )
        logger.info("CDN auth + policy set for %s", endpoint_name)
    except Exception as pol_exc:
        logger.warning(
            "Failed to set CDN auth for %s: %s",
            endpoint_name, pol_exc,
        )


# ---------------------------------------------------------------------------
# Orchestrated channel creation
# ---------------------------------------------------------------------------


def _execute_orchestrated_creation(params: OrchestrationParams) -> OrchestrationResult:
    """Execute the 4-step orchestrated channel creation with automatic rollback.

    Steps:
        1. Create MediaPackage V2 channel and extract ingest URL.
        2. Create HLS and DASH origin endpoints.
        3. Create MediaLive inputs (reuses ``_create_inputs_for_channel``).
        4. Create MediaLive channel from template with destinations and inputs.

    On failure at any step, all previously created resources are rolled back
    in reverse order via ``_execute_rollback``.
    """
    rollback_stack: list[RollbackEntry] = []
    try:
        # Detect template format to determine MPV2 InputType
        # We need to peek at the template first
        template_peek = get_full_config(
            "MediaLive", "channel", params.template_resource_id,
        )
        if template_peek.get("multiplos_resultados"):
            raise ValueError(
                f"Template ambíguo: {template_peek.get('mensagem', '')}. "
                f"Candidatos: {template_peek.get('candidatos', [])}"
            )

        template_destinations = template_peek.get("Destinations", [])
        uses_mediapackage_settings = any(
            d.get("MediaPackageSettings")
            for d in template_destinations
        )
        mpv2_input_type = "HLS" if uses_mediapackage_settings else "CMAF"

        # ---- Step 1: Create MPV2 Channel -----------------------------------
        mpv2_config = {
            "ChannelGroupName": params.channel_group,
            "ChannelName": params.nome_canal,
            "InputType": mpv2_input_type,
        }
        mpv2_result = create_resource("MediaPackage", "channel_v2", mpv2_config)
        rollback_stack.append(RollbackEntry(
            servico="MediaPackage",
            tipo_recurso="channel_v2",
            resource_id=mpv2_result["resource_id"],
            channel_group=params.channel_group,
            channel_name=params.nome_canal,
        ))
        ingest_url = _extract_ingest_url(mpv2_result["details"])

        # ---- Step 2: Clone or create endpoints -------------------------
        template_mpv2 = _detect_template_mpv2_channel(template_destinations)

        template_endpoints: list[dict[str, Any]] = []
        template_cn = ""
        if template_mpv2:
            template_cg, template_cn = template_mpv2
            template_endpoints = _fetch_template_endpoints(
                template_cg, template_cn,
            )

        created_endpoint_names: list[str] = []

        if template_endpoints:
            # Clone each endpoint from template
            drm_resource_id = (
                params.drm_resource_id or f"Live_{params.nome_canal}"
            )
            for ep_config in template_endpoints:
                cloned = _clone_endpoint_config(
                    ep_config, template_cn, params.nome_canal,
                    params.channel_group, drm_resource_id,
                )
                ep_result = create_resource(
                    "MediaPackage", "origin_endpoint_v2", cloned,
                )
                ep_name = cloned["OriginEndpointName"]
                created_endpoint_names.append(ep_name)
                rollback_stack.append(RollbackEntry(
                    servico="MediaPackage",
                    tipo_recurso="origin_endpoint_v2",
                    resource_id=ep_result["resource_id"],
                    channel_group=params.channel_group,
                    channel_name=params.nome_canal,
                    endpoint_name=ep_name,
                ))
                _apply_cdn_auth_policy(params, ep_name)
        else:
            # Fallback: use _build_endpoint_config (current behavior)
            if not template_mpv2:
                logger.warning(
                    "Clonagem não disponível: não foi possível detectar "
                    "canal MPV2 do template. Usando _build_endpoint_config.",
                )
            else:
                logger.warning(
                    "Clonagem não disponível: nenhum endpoint encontrado "
                    "no template %s/%s. Usando _build_endpoint_config.",
                    template_mpv2[0], template_mpv2[1],
                )
            for ep_type in ("HLS", "DASH"):
                ep_config = _build_endpoint_config(params, ep_type)
                ep_result = create_resource(
                    "MediaPackage", "origin_endpoint_v2", ep_config,
                )
                ep_name = f"{params.nome_canal}_{ep_type}"
                created_endpoint_names.append(ep_name)
                rollback_stack.append(RollbackEntry(
                    servico="MediaPackage",
                    tipo_recurso="origin_endpoint_v2",
                    resource_id=ep_result["resource_id"],
                    channel_group=params.channel_group,
                    channel_name=params.nome_canal,
                    endpoint_name=ep_name,
                ))
                _apply_cdn_auth_policy(params, ep_name)

        # ---- Step 3: Create MediaLive Inputs -------------------------------
        template_config = template_peek  # Already fetched above

        channel_class = template_config.get("ChannelClass", "SINGLE_PIPELINE")
        template_attachments = template_config.get("InputAttachments", [])

        input_attachments = _create_inputs_for_channel(
            params.nome_canal, channel_class, template_attachments,
        )

        for att in input_attachments:
            rollback_stack.append(RollbackEntry(
                servico="MediaLive",
                tipo_recurso="input",
                resource_id=att["InputId"],
            ))

        # ---- Step 4: Create MediaLive Channel from template ----------------
        destinations_id = params.nome_canal.replace("_", "-")

        if uses_mediapackage_settings:
            # Direct MediaPackage V2 integration
            destinations = [{
                "Id": destinations_id,
                "MediaPackageSettings": [{
                    "ChannelGroup": params.channel_group,
                    "ChannelName": params.nome_canal,
                }],
            }]
        else:
            # CMAF ingest via URL
            # HlsGroupSettings requires a filename, not just a folder
            # Check if template uses HlsGroupSettings
            has_hls_group = False
            for og in template_peek.get(
                "EncoderSettings", {},
            ).get("OutputGroups", []):
                if "HlsGroupSettings" in og.get(
                    "OutputGroupSettings", {},
                ):
                    has_hls_group = True
                    break

            dest_url = ingest_url
            if has_hls_group and not dest_url.endswith(
                ".m3u8",
            ):
                dest_url = dest_url.rstrip("/") + "/master"

            destinations = [{
                "Id": destinations_id,
                "Settings": [{"Url": dest_url}],
            }]

        keys_to_strip = {
            "Arn", "Id", "EgressEndpoints", "PipelinesRunningCount",
            "State", "Tags", "ChannelId",
            "PipelineDetails", "Vpc", "Maintenance",
        }
        channel_config = {
            k: deepcopy(v)
            for k, v in template_config.items()
            if k not in keys_to_strip
        }

        channel_config["Name"] = params.nome_canal
        channel_config["Destinations"] = destinations
        channel_config["InputAttachments"] = input_attachments
        channel_config["ChannelClass"] = channel_class

        # Update DestinationRefId in OutputGroups to match new Destinations.Id
        # Also clean read-only fields from template
        encoder = channel_config.get("EncoderSettings", {})
        valid_dest_ids = {destinations_id}
        all_group_keys = (
            "HlsGroupSettings", "DashIsoGroupSettings",
            "CmafIngestGroupSettings", "MediaPackageGroupSettings",
            "UdpGroupSettings", "RtmpGroupSettings",
            "FrameCaptureGroupSettings", "MultiplexGroupSettings",
        )
        for og in encoder.get("OutputGroups", []):
            og_settings = og.get("OutputGroupSettings", {})
            for key in all_group_keys:
                group = og_settings.get(key, {})
                dest = group.get("Destination", {})
                if dest.get("DestinationRefId"):
                    dest["DestinationRefId"] = destinations_id
                # Remove read-only Uri field from Destination
                dest.pop("Uri", None)

        role_arn = template_config.get("RoleArn", "")
        if role_arn:
            channel_config["RoleArn"] = role_arn

        ml_result = create_resource("MediaLive", "channel", channel_config)
        canal_medialive_id = ml_result["resource_id"]
        rollback_stack.append(RollbackEntry(
            servico="MediaLive",
            tipo_recurso="channel",
            resource_id=canal_medialive_id,
        ))

        # Upload channel JSON for download
        upload_config_json(ml_result["details"], params.nome_canal)

        input_names = [att["InputAttachmentName"] for att in input_attachments]

        return OrchestrationResult(
            success=True,
            recursos_criados={
                "canal_mpv2": params.nome_canal,
                "endpoints": created_endpoint_names,
                "inputs": input_names,
                "canal_medialive": canal_medialive_id,
            },
            ingest_url=ingest_url,
            canal_medialive_id=canal_medialive_id,
        )

    except Exception as exc:
        logger.error("Orchestrated creation failed: %s", exc)
        recursos_removidos, recursos_falha_remocao = _execute_rollback(
            rollback_stack,
        )
        return OrchestrationResult(
            success=False,
            recursos_criados={},
            erro=str(exc),
            rollback_executado=True,
            recursos_removidos=recursos_removidos,
            recursos_falha_remocao=recursos_falha_remocao,
        )


def create_from_template(
    servico: str,
    tipo_recurso: str,
    template_resource_id: str,
    modificacoes: dict[str, Any],
) -> dict[str, Any]:
    """Fetch an existing resource config, apply modifications, and create.

    For MediaLive channels, automatically creates new inputs
    following the naming convention and configures failover
    for SINGLE_PIPELINE channels.
    """
    # 1. Fetch template (supports fuzzy search)
    template = get_full_config(
        servico, tipo_recurso, template_resource_id,
    )

    if template.get("multiplos_resultados"):
        return template

    # 2. Apply name
    new_name = modificacoes.get("Name", "")
    if new_name:
        template["Name"] = new_name

    # 3. Update destinations
    dest_url = modificacoes.get("destination_url")
    dest_id = modificacoes.get(
        "destination_id",
        new_name.replace("_", "-") if new_name else "",
    )
    if dest_id and "Destinations" in template:
        for dest in template["Destinations"]:
            dest["Id"] = dest_id
            if dest_url:
                for s in dest.get("Settings", []):
                    s["Url"] = dest_url

    # 4. Create inputs automatically for MediaLive channels
    if (
        servico == "MediaLive"
        and tipo_recurso == "channel"
        and new_name
    ):
        channel_class = template.get(
            "ChannelClass", "SINGLE_PIPELINE",
        )
        old_attachments = template.get(
            "InputAttachments", [],
        )
        new_attachments = _create_inputs_for_channel(
            new_name, channel_class, old_attachments,
        )
        template["InputAttachments"] = new_attachments

    # 5. Apply any other top-level overrides
    skip = {
        "Name", "destination_url",
        "destination_id", "input_ids",
    }
    for k, v in modificacoes.items():
        if k not in skip:
            template[k] = v

    # 6. Upload generated JSON for user download
    upload_name = new_name or template_resource_id
    upload_info = upload_config_json(template, upload_name)

    # 7. Create the channel
    result = create_resource(servico, tipo_recurso, template)
    result["dados_exportados"] = upload_info["dados_exportados"]
    result["formato_arquivo"] = "json"

    return result


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
# AWS API helpers — get full config (for template-based creation)
# ---------------------------------------------------------------------------

# Fields to strip from a DescribeChannel response so the result can be
# used directly as a CreateChannel payload.
_ML_STRIP_FIELDS = {
    "Arn", "ChannelId", "Id", "EgressEndpoints",
    "PipelinesRunningCount", "State", "Tags",
    "ResponseMetadata", "Maintenance",
}

_CF_STRIP_FIELDS = {
    "ResponseMetadata", "ETag",
}


def _strip_keys(d: dict, keys: set) -> dict:
    """Return a shallow copy of *d* without the given top-level keys."""
    return {k: v for k, v in d.items() if k not in keys}


# ---------------------------------------------------------------------------
# Fuzzy name resolution helpers
# ---------------------------------------------------------------------------


def _resolve_medialive_channel(
    search_term: str,
) -> dict[str, Any]:
    """Resolve a fuzzy search term to a MediaLive channel.

    Lists all channels and filters by case-insensitive substring
    match on the channel Name.

    Returns:
        - Single match: full describe_channel response (cleaned)
        - Multiple matches: dict with "candidatos" list
        - No match: raises ValueError
    """
    search_lower = search_term.lower()
    matches = []
    paginator = medialive_client.get_paginator(
        "list_channels",
    )
    for page in paginator.paginate():
        for ch in page.get("Channels", []):
            name = ch.get("Name", "")
            if search_lower in name.lower():
                matches.append({
                    "channel_id": ch.get("Id", ""),
                    "nome": name,
                    "estado": ch.get("State", ""),
                })

    if len(matches) == 0:
        raise ValueError(
            f"Nenhum canal MediaLive encontrado com "
            f"'{search_term}' no nome"
        )

    if len(matches) == 1:
        resp = medialive_client.describe_channel(
            ChannelId=matches[0]["channel_id"],
        )
        channel_id = resp.get("Id", matches[0]["channel_id"])
        cleaned = _strip_keys(resp, _ML_STRIP_FIELDS)
        cleaned["Id"] = channel_id
        return cleaned

    # Multiple matches — return candidates for user to pick
    return {
        "multiplos_resultados": True,
        "mensagem": (
            f"Encontrei {len(matches)} canais com "
            f"'{search_term}' no nome. "
            f"Qual deles você quer usar como template?"
        ),
        "candidatos": matches,
    }


def _resolve_medialive_input(
    search_term: str,
) -> dict[str, Any]:
    """Resolve a fuzzy search term to a MediaLive input."""
    search_lower = search_term.lower()
    matches = []
    paginator = medialive_client.get_paginator(
        "list_inputs",
    )
    for page in paginator.paginate():
        for inp in page.get("Inputs", []):
            name = inp.get("Name", "")
            if search_lower in name.lower():
                matches.append({
                    "input_id": inp.get("Id", ""),
                    "nome": name,
                    "tipo": inp.get("Type", ""),
                })

    if len(matches) == 0:
        raise ValueError(
            f"Nenhum input MediaLive encontrado com "
            f"'{search_term}' no nome"
        )

    if len(matches) == 1:
        resp = medialive_client.describe_input(
            InputId=matches[0]["input_id"],
        )
        return _strip_keys(resp, _ML_STRIP_FIELDS)

    return {
        "multiplos_resultados": True,
        "mensagem": (
            f"Encontrei {len(matches)} inputs com "
            f"'{search_term}' no nome. Qual deles?"
        ),
        "candidatos": matches,
    }


def _resolve_mediatailor_config(
    search_term: str,
) -> dict[str, Any]:
    """Resolve a fuzzy search term to a MediaTailor config."""
    search_lower = search_term.lower()
    matches = []
    resp = mediatailor_client.list_playback_configurations(
        MaxResults=100,
    )
    for cfg in resp.get("Items", []):
        name = cfg.get("Name", "")
        if search_lower in name.lower():
            matches.append({
                "nome": name,
            })

    if len(matches) == 0:
        raise ValueError(
            f"Nenhuma configuração MediaTailor encontrada "
            f"com '{search_term}' no nome"
        )

    if len(matches) == 1:
        detail = mediatailor_client.get_playback_configuration(
            Name=matches[0]["nome"],
        )
        detail.pop("ResponseMetadata", None)
        return detail

    return {
        "multiplos_resultados": True,
        "mensagem": (
            f"Encontrei {len(matches)} configurações com "
            f"'{search_term}' no nome. Qual delas?"
        ),
        "candidatos": matches,
    }


def _resolve_mpv2_channel(
    search_term: str,
) -> dict[str, Any]:
    """Resolve a fuzzy search term to a MediaPackage V2 channel.

    Iterates all channel groups and channels looking for a
    case-insensitive substring match on ChannelName.
    """
    search_lower = search_term.lower()
    matches = []

    groups_resp = mediapackagev2_client.list_channel_groups(
        MaxResults=100,
    )
    for grp in groups_resp.get("Items", []):
        grp_name = grp.get("ChannelGroupName", "")
        ch_resp = mediapackagev2_client.list_channels(
            ChannelGroupName=grp_name, MaxResults=100,
        )
        for ch in ch_resp.get("Items", []):
            ch_name = ch.get("ChannelName", "")
            if search_lower in ch_name.lower():
                matches.append({
                    "channel_group": grp_name,
                    "channel_name": ch_name,
                })

    if len(matches) == 0:
        raise ValueError(
            f"Nenhum canal MediaPackage V2 encontrado "
            f"com '{search_term}' no nome"
        )

    if len(matches) == 1:
        m = matches[0]
        resp = mediapackagev2_client.get_channel(
            ChannelGroupName=m["channel_group"],
            ChannelName=m["channel_name"],
        )
        resp.pop("ResponseMetadata", None)
        return resp

    return {
        "multiplos_resultados": True,
        "mensagem": (
            f"Encontrei {len(matches)} canais MediaPackage "
            f"com '{search_term}' no nome. Qual deles?"
        ),
        "candidatos": matches,
    }


def _is_numeric_or_exact_id(value: str) -> bool:
    """Check if value looks like a numeric ID or exact resource ID."""
    return value.isdigit() or "/" in value


def get_full_config(
    servico: str,
    tipo_recurso: str,
    resource_id: str,
) -> dict[str, Any]:
    """Retrieve the full API-native JSON of a resource.

    If *resource_id* is a numeric ID or contains '/', it is used
    directly.  Otherwise it is treated as a fuzzy search term and
    resolved by listing resources and matching by name substring
    (case-insensitive).

    When multiple resources match, returns a dict with
    ``multiplos_resultados=True`` and a ``candidatos`` list so the
    agent can ask the user to pick one.
    """
    # --- MediaLive ---
    if servico == "MediaLive":
        if tipo_recurso == "channel":
            if _is_numeric_or_exact_id(resource_id):
                resp = medialive_client.describe_channel(
                    ChannelId=resource_id,
                )
                return _strip_keys(resp, _ML_STRIP_FIELDS)
            return _resolve_medialive_channel(resource_id)

        if tipo_recurso == "input":
            if _is_numeric_or_exact_id(resource_id):
                resp = medialive_client.describe_input(
                    InputId=resource_id,
                )
                return _strip_keys(resp, _ML_STRIP_FIELDS)
            return _resolve_medialive_input(resource_id)

    # --- MediaPackage V2 ---
    elif servico == "MediaPackage":
        if tipo_recurso == "channel":
            parts = resource_id.split("/", 1)
            if len(parts) == 2:
                resp = mediapackagev2_client.get_channel(
                    ChannelGroupName=parts[0],
                    ChannelName=parts[1],
                )
                resp.pop("ResponseMetadata", None)
                return resp
            return _resolve_mpv2_channel(resource_id)

        if tipo_recurso == "origin_endpoint":
            parts = resource_id.split("/", 2)
            if len(parts) == 3:
                resp = mediapackagev2_client.get_origin_endpoint(
                    ChannelGroupName=parts[0],
                    ChannelName=parts[1],
                    OriginEndpointName=parts[2],
                )
                resp.pop("ResponseMetadata", None)
                return resp
            # Fallback V1
            resp = mediapackage_client.describe_origin_endpoint(
                Id=resource_id,
            )
            resp.pop("ResponseMetadata", None)
            return resp

    # --- MediaTailor ---
    elif servico == "MediaTailor":
        if tipo_recurso == "playback_configuration":
            if _is_numeric_or_exact_id(resource_id):
                resp = (
                    mediatailor_client
                    .get_playback_configuration(
                        Name=resource_id,
                    )
                )
                resp.pop("ResponseMetadata", None)
                return resp
            return _resolve_mediatailor_config(resource_id)

    # --- CloudFront ---
    elif servico == "CloudFront":
        if tipo_recurso == "distribution":
            resp = cloudfront_client.get_distribution(
                Id=resource_id,
            )
            return _strip_keys(resp, _CF_STRIP_FIELDS)

    raise ValueError(
        f"obterConfiguracao não suportado: {servico}/{tipo_recurso}"
    )


# ---------------------------------------------------------------------------
# AWS API helpers — update
# ---------------------------------------------------------------------------


_NAME_MERGEABLE_KEYS = {"VideoDescriptions", "AudioDescriptions", "CaptionDescriptions"}

# Arrays where items should be merged by "OutputName" instead of "Name"
_OUTPUT_NAME_MERGEABLE_KEYS = {"Outputs"}

# Arrays where items should be merged by "Name" (OutputGroups have Name)
_OG_NAME_MERGEABLE_KEYS = {"OutputGroups"}


def _remove_nested_field(obj: dict, path: str, field: str) -> None:
    """Remove a field from a nested dict following a dotted path with array indices.

    Example: _remove_nested_field(d, "EncoderSettings.OutputGroups[0].OutputGroupSettings.UdpGroupSettings", "TimedMetadataBehavior")
    """
    parts = re.split(r'\.', path)
    current = obj
    for part in parts:
        # Handle array index like "OutputGroups[0]"
        match = re.match(r'(.+)\[(\d+)\]', part)
        if match:
            key, idx = match.group(1), int(match.group(2))
            if isinstance(current, dict) and key in current:
                current = current[key]
                if isinstance(current, list) and idx < len(current):
                    current = current[idx]
                else:
                    return
            else:
                return
        else:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return
    # Now current is the parent dict, remove the field
    if isinstance(current, dict) and field in current:
        del current[field]


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge *patch* into *base*.

    - Dicts are merged recursively.
    - Lists under specific keys are merged by matching on a key field:
      - VideoDescriptions, AudioDescriptions, CaptionDescriptions → match by "Name"
      - Outputs → match by "OutputName"
      - OutputGroups → match by "Name"
    - All other lists in *patch* REPLACE lists in *base* entirely.
    - Scalar values in *patch* override *base*.
    """
    result = dict(base)
    for key, value in patch.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        elif (
            key in result
            and isinstance(result[key], list)
            and isinstance(value, list)
            and value
            and isinstance(value[0], dict)
        ):
            # Determine the match key for this array
            match_key = None
            if key in _NAME_MERGEABLE_KEYS and "Name" in value[0]:
                match_key = "Name"
            elif key in _OUTPUT_NAME_MERGEABLE_KEYS and "OutputName" in value[0]:
                match_key = "OutputName"
            elif key in _OG_NAME_MERGEABLE_KEYS and "Name" in value[0]:
                match_key = "Name"

            if match_key:
                # Merge lists of dicts by match key
                base_list = list(result[key])
                for patch_item in value:
                    patch_val = patch_item.get(match_key)
                    matched = False
                    for i, base_item in enumerate(base_list):
                        if isinstance(base_item, dict) and base_item.get(match_key) == patch_val:
                            base_list[i] = _deep_merge(base_item, patch_item)
                            matched = True
                            break
                    if not matched:
                        base_list.append(patch_item)
                result[key] = base_list
            else:
                result[key] = value
        else:
            result[key] = value
    return result


def _strip_none_values(obj: Any) -> Any:
    """Recursively clean None values from nested structures.

    - Fields ending with 'Settings' or 'Configuration' that are None
      get replaced with {} (boto3 expects dict, not None).
    - All other None values are removed entirely.
    """
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            if v is None:
                if k.endswith("Settings") or k.endswith("Configuration"):
                    cleaned[k] = {}
                # else: skip (remove the None key)
            else:
                cleaned[k] = _strip_none_values(v)
        return cleaned
    if isinstance(obj, list):
        return [_strip_none_values(item) for item in obj]
    return obj


# Fields that UpdateChannel does NOT accept (read-only from Describe)
_ML_UPDATE_STRIP = {
    "Arn", "ChannelId", "Id", "EgressEndpoints",
    "PipelinesRunningCount", "State", "Tags",
    "ResponseMetadata", "Maintenance", "ChannelClass",
    "InputSpecification", "LogLevel", "PipelineDetails",
    "Vpc",
}


def patch_and_update_medialive_channel(
    resource_id: str, patch_json: dict[str, Any],
) -> dict[str, Any]:
    """Fetch current MediaLive channel config, apply patch, and update.

    This is the 'smart patch' approach:
    1. DescribeChannel to get full current config
    2. Deep-merge the patch into the current config
    3. Strip read-only fields
    4. Call UpdateChannel with the merged config

    This allows partial updates like changing a single bitrate,
    adding an audio description, or adding an output group —
    without needing to send the entire EncoderSettings.
    """
    import time

    logger.info("patch_and_update received patch_json: %s", json.dumps(patch_json, default=str)[:2000])

    # 1. Get current config
    current = medialive_client.describe_channel(ChannelId=resource_id)
    current.pop("ResponseMetadata", None)

    channel_name = current.get("Name", resource_id)
    was_running = current.get("State") in ("RUNNING", "STARTING")

    # 1b. If channel is running, stop it first
    if was_running:
        logger.info("Canal %s esta RUNNING, parando para aplicar update...", channel_name)
        medialive_client.stop_channel(ChannelId=resource_id)
        # Wait up to 120s for IDLE
        for _ in range(24):
            time.sleep(5)
            desc = medialive_client.describe_channel(ChannelId=resource_id)
            if desc.get("State") == "IDLE":
                break
        else:
            raise RuntimeError(
                f"Canal {channel_name} nao ficou IDLE apos 120s. "
                f"Estado atual: {desc.get('State')}"
            )
        # Refresh current config after stop
        current = medialive_client.describe_channel(ChannelId=resource_id)
        current.pop("ResponseMetadata", None)

    # 2. Pre-process: detect output renames
    # If the patch has an OutputGroup with a Name that matches an existing OG,
    # and the Outputs have OutputNames that DON'T match any existing output,
    # check if it's a rename by looking at VideoDescriptionName match.
    # Also handle case where patch OG Name doesn't match any existing OG
    # but has outputs referencing existing VideoDescriptions — likely a
    # rename where the agent used the wrong OG name.
    patch_ogs = (
        patch_json
        .get("EncoderSettings", {})
        .get("OutputGroups", [])
    )
    current_ogs = (
        current
        .get("EncoderSettings", {})
        .get("OutputGroups", [])
    )
    if patch_ogs and current_ogs:
        for patch_og in patch_ogs:
            patch_outputs = patch_og.get("Outputs", [])
            for patch_out in patch_outputs:
                new_name = patch_out.get("OutputName", "")
                video_desc = patch_out.get("VideoDescriptionName", "")
                if not new_name:
                    continue
                # Search all current OGs for an output with matching
                # VideoDescriptionName or similar OutputName pattern
                for cog in current_ogs:
                    for cout in cog.get("Outputs", []):
                        old_name = cout.get("OutputName", "")
                        old_video = cout.get("VideoDescriptionName", "")
                        # Match by VideoDescriptionName or by similar name pattern
                        is_rename = (
                            (video_desc and old_video == video_desc and old_name != new_name)
                            or (not video_desc and old_name != new_name
                                and old_name.replace(old_name, "") == "")
                        )
                        if is_rename and video_desc and old_video == video_desc:
                            logger.info(
                                "Detected output rename: %s -> %s (video: %s)",
                                old_name, new_name, video_desc,
                            )
                            cout["OutputName"] = new_name
        # After applying renames directly, remove OutputGroups from patch
        # so the merge doesn't add phantom OGs
        if "EncoderSettings" in patch_json:
            patch_json = dict(patch_json)
            patch_encoder = dict(patch_json.get("EncoderSettings", {}))
            patch_encoder.pop("OutputGroups", None)
            if patch_encoder:
                patch_json["EncoderSettings"] = patch_encoder
            else:
                patch_json.pop("EncoderSettings", None)

    # 2b. Deep merge patch into current
    merged = _deep_merge(current, patch_json)

    # 3. Strip read-only fields
    for field in _ML_UPDATE_STRIP:
        merged.pop(field, None)

    # 3b. Clean None values — for *Settings/*Configuration keys that
    # are the ONLY key in their parent dict (meaning they're the active
    # type), keep as {}. Otherwise remove None entirely.
    def _clean_nones(obj):
        if isinstance(obj, dict):
            cleaned = {}
            for k, v in obj.items():
                if v is None:
                    # Just remove all None values — the active settings
                    # already come as {} from describe_channel, not None.
                    # None means "not used" and should be removed.
                    pass
                else:
                    cleaned[k] = _clean_nones(v)
            return cleaned
        if isinstance(obj, list):
            return [_clean_nones(i) for i in obj]
        return obj
    merged = _clean_nones(merged)

    logger.info(
        "Updating channel %s (was_running=%s), merged keys: %s",
        channel_name, was_running, list(merged.keys()),
    )
    # Log OutputGroups structure for debugging
    ogs = merged.get("EncoderSettings", {}).get("OutputGroups", [])
    for i, og in enumerate(ogs):
        og_name = og.get("Name", "?")
        has_ogs = "OutputGroupSettings" in og
        outputs = og.get("Outputs", [])
        output_names = [o.get("OutputName", "?") for o in outputs]
        has_os = all("OutputSettings" in o for o in outputs)
        logger.info(
            "  OG[%d] name=%s hasOutputGroupSettings=%s outputs=%s allHaveOutputSettings=%s",
            i, og_name, has_ogs, output_names, has_os,
        )

    try:
        resp = medialive_client.update_channel(
            ChannelId=resource_id, **merged,
        )
    except Exception:
        if was_running:
            logger.info("Update falhou, reiniciando canal %s...", channel_name)
            time.sleep(3)
            medialive_client.start_channel(ChannelId=resource_id)
        raise

    # 5. Restart if it was running before
    if was_running:
        logger.info("Reiniciando canal %s...", channel_name)
        # Wait briefly for update to settle
        time.sleep(5)
        medialive_client.start_channel(ChannelId=resource_id)

    return {
        "resource_id": resource_id,
        "nome": channel_name,
        "details": resp.get("Channel", {}),
        "canal_reiniciado": was_running,
    }


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
        elif tipo_recurso == "origin_endpoint_v2":
            # resource_id = "ChannelGroup/ChannelName/EndpointName"
            parts = resource_id.split("/", 2)
            if len(parts) != 3:
                raise ValueError(
                    "Para origin_endpoint_v2, resource_id deve ser "
                    "'ChannelGroup/ChannelName/EndpointName'"
                )
            config_json["ChannelGroupName"] = parts[0]
            config_json["ChannelName"] = parts[1]
            config_json["OriginEndpointName"] = parts[2]
            resp = mediapackagev2_client.update_origin_endpoint(**config_json)
            resp.pop("ResponseMetadata", None)
            return {"resource_id": resource_id, "details": resp}
        elif tipo_recurso == "channel_v2":
            parts = resource_id.split("/", 1)
            if len(parts) != 2:
                raise ValueError(
                    "Para channel_v2, resource_id deve ser "
                    "'ChannelGroup/ChannelName'"
                )
            config_json["ChannelGroupName"] = parts[0]
            config_json["ChannelName"] = parts[1]
            resp = mediapackagev2_client.update_channel(**config_json)
            resp.pop("ResponseMetadata", None)
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
# AWS API helpers — delete individual resource
# ---------------------------------------------------------------------------


def delete_resource_individual(
    servico: str, tipo_recurso: str, resource_id: str,
) -> dict[str, Any]:
    """Delete a single AWS resource by service/type/id.

    Unlike ``delete_resource`` (used by rollback with RollbackEntry),
    this function accepts flat parameters for the ``/deletarRecurso``
    endpoint.
    """
    if servico == "MediaLive":
        if tipo_recurso == "channel":
            # Must stop channel before deleting
            try:
                desc = medialive_client.describe_channel(ChannelId=resource_id)
                if desc.get("State") in ("RUNNING", "STARTING"):
                    medialive_client.stop_channel(ChannelId=resource_id)
                    import time
                    # Wait up to 60s for channel to stop
                    for _ in range(12):
                        time.sleep(5)
                        desc = medialive_client.describe_channel(ChannelId=resource_id)
                        if desc.get("State") in ("IDLE", "STOPPING"):
                            break
            except ClientError:
                pass  # best-effort stop
            medialive_client.delete_channel(ChannelId=resource_id)
            return {"status": "deleted", "resource_id": resource_id}
        elif tipo_recurso == "input":
            medialive_client.delete_input(InputId=resource_id)
            return {"status": "deleted", "resource_id": resource_id}

    elif servico == "MediaPackage":
        if tipo_recurso == "channel":
            mediapackage_client.delete_channel(Id=resource_id)
            return {"status": "deleted", "resource_id": resource_id}
        elif tipo_recurso == "origin_endpoint":
            mediapackage_client.delete_origin_endpoint(Id=resource_id)
            return {"status": "deleted", "resource_id": resource_id}
        elif tipo_recurso == "channel_v2":
            parts = resource_id.split("/", 1)
            if len(parts) != 2:
                raise ValueError(
                    "Para channel_v2, resource_id deve ser "
                    "'ChannelGroup/ChannelName'"
                )
            mediapackagev2_client.delete_channel(
                ChannelGroupName=parts[0], ChannelName=parts[1],
            )
            return {"status": "deleted", "resource_id": resource_id}
        elif tipo_recurso == "origin_endpoint_v2":
            parts = resource_id.split("/", 2)
            if len(parts) != 3:
                raise ValueError(
                    "Para origin_endpoint_v2, resource_id deve ser "
                    "'ChannelGroup/ChannelName/EndpointName'"
                )
            mediapackagev2_client.delete_origin_endpoint(
                ChannelGroupName=parts[0],
                ChannelName=parts[1],
                OriginEndpointName=parts[2],
            )
            return {"status": "deleted", "resource_id": resource_id}

    elif servico == "MediaTailor":
        if tipo_recurso == "playback_configuration":
            mediatailor_client.delete_playback_configuration(Name=resource_id)
            return {"status": "deleted", "resource_id": resource_id}

    elif servico == "CloudFront":
        if tipo_recurso == "distribution":
            # Must disable distribution first, then delete with ETag
            current = cloudfront_client.get_distribution_config(Id=resource_id)
            etag = current.get("ETag", "")
            dist_config = current.get("DistributionConfig", {})
            if dist_config.get("Enabled"):
                dist_config["Enabled"] = False
                cloudfront_client.update_distribution(
                    Id=resource_id, IfMatch=etag,
                    DistributionConfig=dist_config,
                )
                # Re-fetch ETag after update
                current = cloudfront_client.get_distribution_config(Id=resource_id)
                etag = current.get("ETag", "")
            cloudfront_client.delete_distribution(Id=resource_id, IfMatch=etag)
            return {"status": "deleted", "resource_id": resource_id}

    raise ValueError(
        f"Operação de exclusão não suportada: {servico}/{tipo_recurso}"
    )


# ---------------------------------------------------------------------------
# AWS API helpers — start/stop MediaLive channel
# ---------------------------------------------------------------------------


def start_stop_channel(
    resource_id: str, acao: str,
) -> dict[str, Any]:
    """Start or stop a MediaLive channel."""
    if acao not in ("start", "stop"):
        raise ValueError(f"Ação inválida: '{acao}'. Use 'start' ou 'stop'.")

    # Resolve fuzzy name if needed
    if not _is_numeric_or_exact_id(resource_id):
        resolved = _resolve_medialive_channel(resource_id)
        if resolved.get("multiplos_resultados"):
            return resolved
        resource_id = str(resolved.get("Id", resource_id))

    desc = medialive_client.describe_channel(ChannelId=resource_id)
    current_state = desc.get("State", "UNKNOWN")
    channel_name = desc.get("Name", resource_id)

    if acao == "start":
        if current_state == "RUNNING":
            return {
                "resource_id": resource_id,
                "nome": channel_name,
                "estado_anterior": current_state,
                "mensagem": f"Canal {channel_name} já está RUNNING.",
            }
        medialive_client.start_channel(ChannelId=resource_id)
        return {
            "resource_id": resource_id,
            "nome": channel_name,
            "estado_anterior": current_state,
            "estado_novo": "STARTING",
            "mensagem": f"Canal {channel_name} iniciando (STARTING).",
        }
    else:  # stop
        if current_state == "IDLE":
            return {
                "resource_id": resource_id,
                "nome": channel_name,
                "estado_anterior": current_state,
                "mensagem": f"Canal {channel_name} já está IDLE (parado).",
            }
        medialive_client.stop_channel(ChannelId=resource_id)
        return {
            "resource_id": resource_id,
            "nome": channel_name,
            "estado_anterior": current_state,
            "estado_novo": "STOPPING",
            "mensagem": f"Canal {channel_name} parando (STOPPING).",
        }


# ---------------------------------------------------------------------------
# AWS API helpers — list resources
# ---------------------------------------------------------------------------


def list_resources(
    servico: str, tipo_recurso: str,
) -> dict[str, Any]:
    """List resources for a given service/type. Returns summary list."""
    items: list[dict[str, Any]] = []

    if servico == "MediaLive":
        if tipo_recurso == "channel":
            paginator = medialive_client.get_paginator("list_channels")
            for page in paginator.paginate():
                for ch in page.get("Channels", []):
                    items.append({
                        "id": ch.get("Id", ""),
                        "nome": ch.get("Name", ""),
                        "estado": ch.get("State", ""),
                        "classe": ch.get("ChannelClass", ""),
                    })
        elif tipo_recurso == "input":
            paginator = medialive_client.get_paginator("list_inputs")
            for page in paginator.paginate():
                for inp in page.get("Inputs", []):
                    items.append({
                        "id": inp.get("Id", ""),
                        "nome": inp.get("Name", ""),
                        "tipo": inp.get("Type", ""),
                        "estado": inp.get("State", ""),
                    })

    elif servico == "MediaPackage":
        if tipo_recurso == "channel_v2":
            # List all channel groups, then channels in each
            groups_resp = mediapackagev2_client.list_channel_groups()
            for grp in groups_resp.get("Items", []):
                grp_name = grp.get("ChannelGroupName", "")
                ch_paginator = mediapackagev2_client.get_paginator("list_channels")
                for page in ch_paginator.paginate(ChannelGroupName=grp_name):
                    for ch in page.get("Items", []):
                        items.append({
                            "channel_group": grp_name,
                            "nome": ch.get("ChannelName", ""),
                            "input_type": ch.get("InputType", ""),
                        })
        elif tipo_recurso == "origin_endpoint_v2":
            groups_resp = mediapackagev2_client.list_channel_groups()
            for grp in groups_resp.get("Items", []):
                grp_name = grp.get("ChannelGroupName", "")
                try:
                    ch_paginator = mediapackagev2_client.get_paginator("list_channels")
                    for ch_page in ch_paginator.paginate(ChannelGroupName=grp_name):
                        for ch in ch_page.get("Items", []):
                            ch_name = ch.get("ChannelName", "")
                            ep_paginator = mediapackagev2_client.get_paginator(
                                "list_origin_endpoints",
                            )
                            for ep_page in ep_paginator.paginate(
                                ChannelGroupName=grp_name,
                                ChannelName=ch_name,
                            ):
                                for ep in ep_page.get("Items", []):
                                    items.append({
                                        "channel_group": grp_name,
                                        "canal": ch_name,
                                        "endpoint": ep.get("OriginEndpointName", ""),
                                        "container": ep.get("ContainerType", ""),
                                    })
                except ClientError:
                    continue

    elif servico == "MediaTailor":
        if tipo_recurso == "playback_configuration":
            resp = mediatailor_client.list_playback_configurations()
            for cfg in resp.get("Items", []):
                items.append({
                    "nome": cfg.get("Name", ""),
                    "video_source": cfg.get("VideoContentSourceUrl", ""),
                    "ad_server": cfg.get("AdDecisionServerUrl", "")[:80] + "..." if len(cfg.get("AdDecisionServerUrl", "")) > 80 else cfg.get("AdDecisionServerUrl", ""),
                })

    elif servico == "CloudFront":
        if tipo_recurso == "distribution":
            paginator = cloudfront_client.get_paginator("list_distributions")
            for page in paginator.paginate():
                dist_list = page.get("DistributionList", {})
                for dist in dist_list.get("Items", []):
                    items.append({
                        "id": dist.get("Id", ""),
                        "domain": dist.get("DomainName", ""),
                        "status": dist.get("Status", ""),
                        "enabled": dist.get("Enabled", False),
                        "comment": dist.get("Comment", "")[:60],
                    })

    else:
        raise ValueError(
            f"Listagem não suportada: {servico}/{tipo_recurso}"
        )

    return {
        "servico": servico,
        "tipo_recurso": tipo_recurso,
        "total": len(items),
        "recursos": items,
    }


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Consultor_Historico — audit history query functions
# ---------------------------------------------------------------------------


def listar_audit_keys(periodo_dias: int) -> list[str]:
    """List all S3 keys under audit/ for the last *periodo_dias* days.

    Generates date prefixes (``audit/YYYY/MM/DD/``) for each day in the
    period and uses ``list_objects_v2`` with pagination to collect every
    object key.
    """
    keys: list[str] = []
    hoje = datetime.now(timezone.utc).date()
    for i in range(periodo_dias):
        dia = hoje - timedelta(days=i)
        prefix = f"{AUDIT_PREFIX}{dia.strftime('%Y/%m/%d')}/"
        continuation_token = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": AUDIT_BUCKET,
                "Prefix": prefix,
            }
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            resp = s3_client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                keys.append(obj["Key"])
            if resp.get("IsTruncated"):
                continuation_token = resp.get(
                    "NextContinuationToken",
                )
            else:
                break
    return keys


def carregar_entradas_auditoria(keys: list[str]) -> list[dict]:
    """Download and parse audit log entries from S3.

    Corrupted or unreadable entries are silently skipped with a
    warning log.
    """
    entradas: list[dict] = []
    for key in keys:
        try:
            resp = s3_client.get_object(
                Bucket=AUDIT_BUCKET, Key=key,
            )
            body = resp["Body"].read().decode("utf-8")
            entrada = json.loads(body)
            entradas.append(entrada)
        except (ClientError, json.JSONDecodeError, Exception) as exc:
            logger.warning(
                "Skipping corrupted audit entry %s: %s",
                key, exc,
            )
    return entradas


def filtrar_por_recurso(
    entradas: list[dict], resource_id: str,
) -> list[dict]:
    """Filter audit entries by *resource_id* (partial, case-insensitive).

    Matches against the entry's ``resource_id`` field and also against
    ``Name``, ``Id``, ``ChannelName`` and ``nome_canal`` inside
    ``configuracao_json_aplicada``.
    """
    filtro = resource_id.lower()
    resultado: list[dict] = []
    for entrada in entradas:
        rid = str(entrada.get("resource_id", "")).lower()
        if filtro in rid:
            resultado.append(entrada)
            continue
        config = entrada.get("configuracao_json_aplicada")
        if isinstance(config, dict):
            for campo in ("Name", "Id", "ChannelName", "nome_canal"):
                valor = str(config.get(campo, "")).lower()
                if valor and filtro in valor:
                    resultado.append(entrada)
                    break
    return resultado


def filtrar_por_tipo_operacao(
    entradas: list[dict], tipo_operacao: str,
) -> list[dict]:
    """Filter entries by operation type (case-insensitive).

    When *tipo_operacao* is empty, all entries are returned unchanged.
    """
    if not tipo_operacao:
        return entradas
    filtro = tipo_operacao.lower()
    return [
        e for e in entradas
        if str(e.get("tipo_operacao", "")).lower() == filtro
    ]


def formatar_entrada_timeline(entrada: dict) -> dict:
    """Format an audit entry into a timeline-friendly dict.

    Always includes: data_hora, operacao, servico, recurso, resultado,
    usuario, detalhes.  Conditionally includes ``erro`` (when resultado
    is ``falha``) and ``rollback`` (when rollback_info is present).
    """
    ts_raw = entrada.get("timestamp", "")
    try:
        dt = datetime.fromisoformat(ts_raw)
        data_hora = dt.strftime("%d/%m/%Y %H:%M:%S UTC")
    except (ValueError, TypeError):
        data_hora = ts_raw

    config = entrada.get("configuracao_json_aplicada", {})
    if isinstance(config, dict) and config:
        chaves = ", ".join(list(config.keys())[:5])
        detalhes = (
            f"Alteração em {entrada.get('tipo_recurso', 'recurso')}: "
            f"{chaves}"
        )
    else:
        detalhes = "Sem detalhes de configuração"

    item: dict[str, Any] = {
        "data_hora": data_hora,
        "operacao": entrada.get("tipo_operacao", ""),
        "servico": entrada.get("servico_aws", ""),
        "recurso": entrada.get("resource_id", ""),
        "resultado": entrada.get("resultado", ""),
        "usuario": entrada.get("usuario_id", ""),
        "detalhes": detalhes,
    }

    if entrada.get("resultado") == "falha" and entrada.get("erro"):
        item["erro"] = entrada["erro"]

    if entrada.get("rollback_info"):
        item["rollback"] = entrada["rollback_info"]

    return item


def consultar_historico(
    resource_id: str,
    periodo_dias: int,
    tipo_operacao: str = "",
) -> dict:
    """Orchestrate a full audit history query.

    1. List S3 keys for the period
    2. Load entries
    3. Filter by resource_id (partial, case-insensitive)
    4. Filter by tipo_operacao (if provided)
    5. Sort by timestamp DESC
    6. Limit to 50 entries
    7. Format timeline
    """
    keys = listar_audit_keys(periodo_dias)
    entradas = carregar_entradas_auditoria(keys)
    entradas = filtrar_por_recurso(entradas, resource_id)
    entradas = filtrar_por_tipo_operacao(entradas, tipo_operacao)

    entradas.sort(
        key=lambda e: e.get("timestamp", ""), reverse=True,
    )

    total = len(entradas)
    limitadas = entradas[:50]
    timeline = [
        formatar_entrada_timeline(e) for e in limitadas
    ]

    periodo_texto = f"últimos {periodo_dias} dias"

    if total == 0:
        mensagem = (
            f"Nenhuma alteração encontrada para "
            f"'{resource_id}' nos {periodo_texto}."
        )
    elif total > 50:
        mensagem = (
            f"Exibindo as 50 alterações mais recentes de "
            f"{total} encontradas para '{resource_id}' "
            f"nos {periodo_texto}."
        )
    else:
        mensagem = (
            f"Encontradas {total} alterações para "
            f"'{resource_id}' nos {periodo_texto}."
        )

    return {
        "mensagem": mensagem,
        "recurso": resource_id,
        "periodo": periodo_texto,
        "total_encontrado": total,
        "timeline": timeline,
    }


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


def upload_config_json(
    config: dict[str, Any],
    resource_name: str,
) -> dict[str, str]:
    """Save a config JSON to S3_Exports and return inline content.

    Returns dict with 's3_key' and 'dados_exportados' (inline
    JSON string for the frontend to create a download blob).
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = uuid.uuid4().hex[:8]
    safe_name = resource_name.replace("/", "_")
    filename = f"config-{safe_name}-{ts}-{uid}.json"
    s3_key = f"{EXPORTS_PREFIX}{filename}"

    body = json.dumps(
        config, ensure_ascii=False, indent=2, default=str,
    )
    s3_client.put_object(
        Bucket=EXPORTS_BUCKET,
        Key=s3_key,
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )

    logger.info(
        "Config JSON uploaded: s3://%s/%s",
        EXPORTS_BUCKET, s3_key,
    )
    return {
        "s3_key": s3_key,
        "dados_exportados": body,
        "formato_arquivo": "json",
    }


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


def _build_config_summary(
    servico: str,
    tipo_recurso: str,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Build a short summary of a resource config.

    Returns a compact dict the agent can use to describe the
    template to the user without exceeding token limits.
    """
    if servico == "MediaLive" and tipo_recurso == "channel":
        encoder = cfg.get("EncoderSettings", {})
        videos = encoder.get("VideoDescriptions", [])
        audios = encoder.get("AudioDescriptions", [])
        captions = encoder.get("CaptionDescriptions", [])
        outputs = []
        for og in encoder.get("OutputGroups", []):
            og_name = og.get("Name", og.get("OutputGroupSettings", {}).get("HlsGroupSettings", {}).get("Destination", {}).get("DestinationRefId", ""))
            for o in og.get("Outputs", []):
                name = o.get("OutputName", "")
                if name:
                    outputs.append({
                        "nome": name,
                        "video": o.get("VideoDescriptionName", ""),
                        "audio": o.get("AudioDescriptionName", ""),
                        "caption": o.get("CaptionDescriptionNames", []),
                        "output_group": og_name,
                    })

        # Build pre-formatted text listings for the agent to include directly
        outputs_text = "\n".join(
            f"  {i+1}. {o['nome']} (vídeo: {o['video']}, áudio: {o['audio']})"
            for i, o in enumerate(outputs)
        ) if outputs else "  Nenhum output encontrado."

        videos_text = "\n".join(
            f"  {i+1}. {v.get('Name', '?')} — {v.get('Width', '?')}x{v.get('Height', '?')}"
            for i, v in enumerate(videos)
        ) if videos else "  Nenhuma video description encontrada."

        audios_text = "\n".join(
            f"  {i+1}. {a.get('Name', '?')} (idioma: {a.get('LanguageCode', 'N/A')})"
            for i, a in enumerate(audios)
        ) if audios else "  Nenhum áudio encontrado."

        return {
            "nome": cfg.get("Name"),
            "channel_class": cfg.get("ChannelClass"),
            "video_descriptions": [
                {
                    "nome": v.get("Name"),
                    "resolucao": f"{v.get('Width')}x{v.get('Height')}" if v.get("Width") else None,
                    "codec": list(v.get("CodecSettings", {}).keys())[0] if v.get("CodecSettings") else None,
                }
                for v in videos
            ],
            "total_outputs": len(outputs),
            "outputs": outputs,
            "outputs_formatado": f"Outputs do canal {cfg.get('Name', '')}:\n{outputs_text}",
            "videos_formatado": f"Video Descriptions do canal {cfg.get('Name', '')}:\n{videos_text}",
            "audios_formatado": f"Áudios do canal {cfg.get('Name', '')}:\n{audios_text}",
            "audios": [
                {
                    "nome": a.get("Name"),
                    "idioma": a.get("LanguageCode"),
                }
                for a in audios
            ],
            "legendas": [
                c.get("Name") for c in captions
            ],
            "inputs": [
                ia.get("InputAttachmentName")
                for ia in cfg.get("InputAttachments", [])
            ],
        }

    # Generic summary for other services
    return {
        "nome": (
            cfg.get("Name")
            or cfg.get("ChannelName")
            or cfg.get("Id", "")
        ),
        "campos_principais": list(cfg.keys())[:15],
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
# /consultarMetricas helpers
# ---------------------------------------------------------------------------

# Metrics config for on-demand queries (same as pipeline_logs)
_ONDEMAND_METRICS_CONFIG = {
    "MediaLive": {
        "namespace": "AWS/MediaLive",
        "region": "sa-east-1",
        "metrics": [
            ("ActiveAlerts", "Maximum"),
            ("InputLossSeconds", "Sum"),
            ("InputVideoFrameRate", "Average"),
            ("DroppedFrames", "Sum"),
            ("FillMsec", "Sum"),
            ("NetworkIn", "Sum"),
            ("NetworkOut", "Sum"),
            ("Output4xxErrors", "Sum"),
            ("Output5xxErrors", "Sum"),
            ("PrimaryInputActive", "Minimum"),
            ("ChannelInputErrorSeconds", "Sum"),
            ("RtpPacketsLost", "Sum"),
        ],
    },
    "MediaPackage": {
        "namespace": "AWS/MediaPackage",
        "region": "sa-east-1",
        "metrics": [
            ("IngressBytes", "Sum"),
            ("IngressRequestCount", "Sum"),
            ("EgressBytes", "Sum"),
            ("EgressRequestCount", "Sum"),
            ("EgressResponseTime", "Average"),
            ("IngressResponseTime", "Average"),
        ],
    },
    "MediaTailor": {
        "namespace": "AWS/MediaTailor",
        "region": "us-east-1",
        "metrics": [
            ("AdDecisionServer.Ads", "Sum"),
            ("AdDecisionServer.Duration", "Sum"),
            ("AdDecisionServer.Errors", "Sum"),
            ("AdDecisionServer.Timeouts", "Sum"),
            ("Avail.Duration", "Sum"),
            ("Avail.FilledDuration", "Sum"),
            ("Avail.FillRate", "Average"),
        ],
    },
    "CloudFront": {
        "namespace": "AWS/CloudFront",
        "region": "us-east-1",
        "metrics": [
            ("Requests", "Sum"),
            ("BytesDownloaded", "Sum"),
            ("BytesUploaded", "Sum"),
            ("4xxErrorRate", "Average"),
            ("5xxErrorRate", "Average"),
            ("TotalErrorRate", "Average"),
        ],
    },
}

# Severity thresholds for on-demand classification (same as pipeline_logs)
_ONDEMAND_SEVERITY_THRESHOLDS: dict[str, dict[str, list]] = {
    "MediaLive": {
        "PrimaryInputActive": [
            (lambda v: v == 0, "CRITICAL", "FAILOVER_DETECTADO"),
        ],
        "ActiveAlerts": [
            (lambda v: v > 0, "ERROR", "ALERTA_ATIVO"),
        ],
        "Output4xxErrors": [
            (lambda v: v > 0, "ERROR", "OUTPUT_ERROR"),
        ],
        "Output5xxErrors": [
            (lambda v: v > 0, "ERROR", "OUTPUT_ERROR"),
        ],
        "InputLossSeconds": [
            (lambda v: v > 0, "WARNING", "INPUT_LOSS"),
        ],
        "DroppedFrames": [
            (lambda v: v > 0, "WARNING", "FRAMES_PERDIDOS"),
        ],
    },
    "MediaPackage": {
        "EgressRequestCount_5xx": [
            (lambda v: v > 0, "ERROR", "EGRESS_5XX"),
        ],
        "IngressBytes": [
            (lambda v: v == 0, "ERROR", "INGESTAO_PARADA"),
        ],
        "EgressRequestCount_4xx": [
            (lambda v: v > 0, "WARNING", "EGRESS_4XX"),
        ],
        "EgressResponseTime": [
            (lambda v: v > 1000, "WARNING", "LATENCIA_ALTA"),
        ],
    },
    "MediaTailor": {
        "AdDecisionServer.Errors": [
            (lambda v: v > 0, "ERROR", "AD_SERVER_ERROR"),
        ],
        "Avail.FillRate": [
            (lambda v: v < 50, "ERROR", "FILL_RATE_CRITICO"),
            (lambda v: v < 80, "WARNING", "FILL_RATE_BAIXO"),
        ],
        "AdDecisionServer.Timeouts": [
            (lambda v: v > 0, "WARNING", "AD_SERVER_TIMEOUT"),
        ],
    },
    "CloudFront": {
        "TotalErrorRate": [
            (lambda v: v > 15, "CRITICAL", "CDN_ERROR_CRITICO"),
        ],
        "5xxErrorRate": [
            (lambda v: v > 5, "ERROR", "CDN_5XX_ALTO"),
        ],
        "4xxErrorRate": [
            (lambda v: v > 10, "WARNING", "CDN_4XX_ALTO"),
        ],
    },
}

_SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 3}

# ---------------------------------------------------------------------------
# Health-check em massa — métricas-chave (subconjunto reduzido)
# ---------------------------------------------------------------------------
_HEALTHCHECK_METRICS = {
    "MediaLive": {
        "namespace": "AWS/MediaLive",
        "region": "sa-east-1",
        "dimension_key": "ChannelId",
        "has_pipeline": True,
        "metrics": [
            ("ActiveAlerts", "Maximum"),
            ("InputLossSeconds", "Sum"),
            ("DroppedFrames", "Sum"),
            ("Output4xxErrors", "Sum"),
            ("Output5xxErrors", "Sum"),
        ],
    },
    "MediaPackage": {
        "namespace": "AWS/MediaPackage",
        "region": "sa-east-1",
        "dimension_key": "Channel",
        "has_pipeline": False,
        "metrics": [
            ("EgressResponseTime", "Average"),
            ("IngressBytes", "Sum"),
        ],
    },
    "MediaTailor": {
        "namespace": "AWS/MediaTailor",
        "region": "us-east-1",
        "dimension_key": "ConfigurationName",
        "has_pipeline": False,
        "metrics": [
            ("AdDecisionServer.Errors", "Sum"),
            ("Avail.FillRate", "Average"),
        ],
    },
    "CloudFront": {
        "namespace": "AWS/CloudFront",
        "region": "us-east-1",
        "dimension_key": "DistributionId",
        "has_pipeline": False,
        "metrics": [
            ("5xxErrorRate", "Average"),
            ("TotalErrorRate", "Average"),
        ],
    },
}


def _classify_severity_ondemand(
    metric_name: str, value: float, service: str,
) -> tuple[str, str]:
    """Classify a metric value into severity for on-demand queries."""
    service_thresholds = _ONDEMAND_SEVERITY_THRESHOLDS.get(service, {})
    rules = service_thresholds.get(metric_name, [])
    for condition, severity, error_type in rules:
        if condition(value):
            return (severity, error_type)
    return ("INFO", "METRICAS_NORMAIS")


# ---------------------------------------------------------------------------
# Health-check em massa — funções auxiliares
# ---------------------------------------------------------------------------

_HC_SERVICE_MAP = {
    "MediaLive": ("MediaLive", "channel"),
    "MediaPackage": ("MediaPackage", "channel_v2"),
    "MediaTailor": ("MediaTailor", "playback_configuration"),
    "CloudFront": ("CloudFront", "distribution"),
}


def _healthcheck_discover_resources(
    servicos: list[str],
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Discover resources for each requested service.

    Returns (recursos_por_servico, erros).
    """
    recursos: dict[str, list[dict]] = {}
    erros: list[dict] = []

    for svc in servicos:
        mapping = _HC_SERVICE_MAP.get(svc)
        if not mapping:
            continue
        svc_name, tipo = mapping
        try:
            result = list_resources(svc_name, tipo)
            recursos[svc] = result.get("recursos", [])
        except Exception as exc:
            logger.error("Healthcheck discover %s: %s", svc, exc)
            erros.append({"servico": svc, "mensagem": str(exc)})
            recursos[svc] = []

    return recursos, erros


def _healthcheck_build_queries(
    servico: str,
    recursos: list[dict],
) -> list[dict[str, Any]]:
    """Build MetricDataQueries for all resources of a service."""
    config = _HEALTHCHECK_METRICS.get(servico)
    if not config:
        return []

    namespace = config["namespace"]
    dim_key = config["dimension_key"]
    has_pipeline = config["has_pipeline"]
    metrics_list = config["metrics"]
    queries: list[dict[str, Any]] = []

    for rec in recursos:
        # Determine resource identifier for dimensions
        if servico == "MediaLive":
            res_id = rec.get("id", "")
        elif servico == "MediaPackage":
            res_id = rec.get("nome", "")
        elif servico == "MediaTailor":
            res_id = rec.get("nome", "")
        elif servico == "CloudFront":
            res_id = rec.get("id", "")
        else:
            continue

        if not res_id:
            continue

        safe_id = re.sub(r"[^a-zA-Z0-9]", "", res_id)[:40]

        for metric_name, stat in metrics_list:
            safe_metric = metric_name.replace(".", "_").lower()
            if has_pipeline:
                for pipeline in ("0", "1"):
                    qid = f"hc_{safe_id}_{safe_metric}_p{pipeline}"
                    dims = [
                        {"Name": dim_key, "Value": res_id},
                        {"Name": "Pipeline", "Value": pipeline},
                    ]
                    queries.append({
                        "Id": qid,
                        "MetricStat": {
                            "Metric": {
                                "Namespace": namespace,
                                "MetricName": metric_name,
                                "Dimensions": dims,
                            },
                            "Period": 300,
                            "Stat": stat,
                        },
                        "ReturnData": True,
                        "_hc_resource_id": res_id,
                        "_hc_metric": metric_name,
                    })
            else:
                qid = f"hc_{safe_id}_{safe_metric}"
                dims = [{"Name": dim_key, "Value": res_id}]
                if servico == "CloudFront":
                    dims.append(
                        {"Name": "Region", "Value": "Global"},
                    )
                queries.append({
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": namespace,
                            "MetricName": metric_name,
                            "Dimensions": dims,
                        },
                        "Period": 300,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                    "_hc_resource_id": res_id,
                    "_hc_metric": metric_name,
                })

    return queries


def _healthcheck_batch_get_metrics(
    queries: list[dict],
    region: str,
    start_time: datetime,
    end_time: datetime,
) -> tuple[list[dict], list[dict]]:
    """Execute GetMetricData in batches of ≤500 queries.

    Returns (metric_results, erros).
    """
    BATCH_SIZE = 500
    MAX_RETRIES = 3
    cw = boto3.client("cloudwatch", region_name=region)
    all_results: list[dict] = []
    erros: list[dict] = []

    for i in range(0, len(queries), BATCH_SIZE):
        chunk = queries[i:i + BATCH_SIZE]
        # Strip internal metadata before sending to CloudWatch
        cw_queries = []
        for q in chunk:
            cq = {
                "Id": q["Id"],
                "MetricStat": q["MetricStat"],
                "ReturnData": q["ReturnData"],
            }
            cw_queries.append(cq)

        attempt = 0
        while attempt < MAX_RETRIES:
            try:
                next_token = None
                while True:
                    kwargs: dict[str, Any] = {
                        "MetricDataQueries": cw_queries,
                        "StartTime": start_time,
                        "EndTime": end_time,
                    }
                    if next_token:
                        kwargs["NextToken"] = next_token
                    resp = cw.get_metric_data(**kwargs)
                    for mr in resp.get("MetricDataResults", []):
                        all_results.append(mr)
                    next_token = resp.get("NextToken")
                    if not next_token:
                        break
                break  # success
            except ClientError as exc:
                code = exc.response["Error"].get("Code", "")
                if code in (
                    "Throttling",
                    "TooManyRequestsException",
                    "ThrottlingException",
                ):
                    attempt += 1
                    if attempt < MAX_RETRIES:
                        wait = 2 ** (attempt - 1)
                        logger.warning(
                            "CW throttle batch %d, retry %d in %ds",
                            i // BATCH_SIZE, attempt, wait,
                        )
                        time.sleep(wait)
                    else:
                        logger.error(
                            "CW throttle batch %d after %d retries",
                            i // BATCH_SIZE, MAX_RETRIES,
                        )
                        erros.append({
                            "servico": "CloudWatch",
                            "mensagem": (
                                f"ThrottlingException após "
                                f"{MAX_RETRIES} tentativas"
                            ),
                        })
                        break
                else:
                    logger.error("CW error batch %d: %s", i // BATCH_SIZE, exc)
                    erros.append({
                        "servico": "CloudWatch",
                        "mensagem": str(exc),
                    })
                    break

    return all_results, erros


def _healthcheck_classify_resources(
    servico: str,
    recursos: list[dict],
    metric_results: list[dict],
    queries: list[dict],
) -> list[dict]:
    """Classify each resource with a semaphore color.

    Returns list of classified resources.
    """
    # Build lookup: query_id → (resource_id, metric_name)
    q_map: dict[str, tuple[str, str]] = {}
    for q in queries:
        q_map[q["Id"]] = (q["_hc_resource_id"], q["_hc_metric"])

    # Build lookup: result_id → latest value
    result_map: dict[str, float | None] = {}
    for mr in metric_results:
        rid = mr.get("Id", "")
        values = mr.get("Values", [])
        result_map[rid] = values[0] if values else None

    # Group values by resource
    resource_metrics: dict[str, list[tuple[str, float | None]]] = {}
    for qid, (res_id, metric_name) in q_map.items():
        val = result_map.get(qid)
        resource_metrics.setdefault(res_id, []).append(
            (metric_name, val),
        )

    # Name lookup
    name_map: dict[str, str] = {}
    for rec in recursos:
        rid = rec.get("id", "") or rec.get("nome", "")
        nome = rec.get("nome", "") or rec.get("domain", rid)
        name_map[rid] = nome

    classified: list[dict] = []
    for res_id, metric_vals in resource_metrics.items():
        worst_sev = "INFO"
        worst_tipo = "METRICAS_NORMAIS"
        alertas: list[dict] = []
        has_data = False
        nota = None

        for metric_name, val in metric_vals:
            if val is None:
                continue
            has_data = True
            sev, tipo = _classify_severity_ondemand(
                metric_name, val, servico,
            )
            if _SEVERITY_ORDER.get(sev, 0) > _SEVERITY_ORDER.get(worst_sev, 0):
                worst_sev = sev
                worst_tipo = tipo
            if sev != "INFO":
                alertas.append({
                    "metrica": metric_name,
                    "valor": val,
                    "severidade": sev,
                    "tipo_erro": tipo,
                })

        if not has_data:
            worst_sev = "INFO"
            nota = "sem dados no período"

        if worst_sev in ("ERROR", "CRITICAL"):
            cor = "vermelho"
        elif worst_sev == "WARNING":
            cor = "amarelo"
        else:
            cor = "verde"

        classified.append({
            "nome": name_map.get(res_id, res_id),
            "servico": servico,
            "resource_id": res_id,
            "cor": cor,
            "severidade": worst_sev,
            "alertas": alertas,
            "nota": nota,
        })

    # Include resources that had no queries at all
    seen = {c["resource_id"] for c in classified}
    for rec in recursos:
        rid = rec.get("id", "") or rec.get("nome", "")
        if rid and rid not in seen:
            classified.append({
                "nome": name_map.get(rid, rid),
                "servico": servico,
                "resource_id": rid,
                "cor": "verde",
                "severidade": "INFO",
                "alertas": [],
                "nota": "sem dados no período",
            })

    return classified


def _healthcheck_build_dashboard(
    recursos_classificados: list[dict],
    servicos_consultados: list[str],
    periodo_minutos: int,
    erros: list[dict],
    parcial: bool = False,
) -> dict[str, Any]:
    """Build the Dashboard_Saude response."""
    verde = sum(1 for r in recursos_classificados if r["cor"] == "verde")
    amarelo = sum(1 for r in recursos_classificados if r["cor"] == "amarelo")
    vermelho = sum(1 for r in recursos_classificados if r["cor"] == "vermelho")
    total = verde + amarelo + vermelho

    if total > 0:
        score = round(verde / total * 100, 1)
    else:
        score = 100.0

    # Build red list sorted by severity desc (CRITICAL before ERROR)
    reds = [r for r in recursos_classificados if r["cor"] == "vermelho"]
    reds.sort(
        key=lambda r: -_SEVERITY_ORDER.get(r["severidade"], 0),
    )
    recursos_vermelho = [
        {
            "nome": r["nome"],
            "servico": r["servico"],
            "severidade": r["severidade"],
            "alertas": r["alertas"],
        }
        for r in reds
    ]

    # Build yellow list sorted by name asc
    yellows = [r for r in recursos_classificados if r["cor"] == "amarelo"]
    yellows.sort(key=lambda r: r["nome"])
    recursos_amarelo = [
        {
            "nome": r["nome"],
            "servico": r["servico"],
            "alertas": r["alertas"],
        }
        for r in yellows
    ]

    msg = (
        f"Dashboard de saúde: {verde} verdes, "
        f"{amarelo} amarelos, {vermelho} vermelhos "
        f"de {total} recursos. Score: {score}%"
    )
    if parcial:
        msg += " (resultado parcial — timeout)"

    return {
        "timestamp": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        ),
        "periodo": periodo_minutos,
        "servicos_consultados": servicos_consultados,
        "total_recursos": total,
        "totais": {
            "verde": verde,
            "amarelo": amarelo,
            "vermelho": vermelho,
        },
        "score_saude": score,
        "recursos_vermelho": recursos_vermelho,
        "recursos_amarelo": recursos_amarelo,
        "erros": erros,
        "parcial": parcial,
        "mensagem_resumo": msg,
    }


def _execute_healthcheck(
    servico_filtro: str | None,
    periodo_minutos: int = 15,
) -> dict[str, Any]:
    """Execute mass health check and return Dashboard_Saude."""
    t0 = time.time()
    TIMEOUT_LIMIT = 280  # seconds — cut before Lambda 300s timeout

    # Determine services to check
    if servico_filtro:
        if servico_filtro not in _HEALTHCHECK_METRICS:
            raise ValueError(
                f"Serviço inválido: '{servico_filtro}'. "
                f"Válidos: {sorted(_HEALTHCHECK_METRICS.keys())}"
            )
        servicos = [servico_filtro]
    else:
        servicos = list(_HEALTHCHECK_METRICS.keys())

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=periodo_minutos)

    # Discover resources
    recursos_por_servico, erros = _healthcheck_discover_resources(servicos)

    all_classified: list[dict] = []
    parcial = False

    # Group services by region
    region_services: dict[str, list[str]] = {}
    for svc in servicos:
        cfg = _HEALTHCHECK_METRICS.get(svc, {})
        rgn = cfg.get("region", "us-east-1")
        region_services.setdefault(rgn, []).append(svc)

    for region, svcs in region_services.items():
        if time.time() - t0 > TIMEOUT_LIMIT:
            parcial = True
            erros.append({
                "servico": "healthcheck",
                "mensagem": "Timeout safety — resultado parcial",
            })
            break

        for svc in svcs:
            if time.time() - t0 > TIMEOUT_LIMIT:
                parcial = True
                erros.append({
                    "servico": "healthcheck",
                    "mensagem": "Timeout safety — resultado parcial",
                })
                break

            recs = recursos_por_servico.get(svc, [])
            if not recs:
                continue

            queries = _healthcheck_build_queries(svc, recs)
            if not queries:
                continue

            results, batch_erros = _healthcheck_batch_get_metrics(
                queries, region, start_time, end_time,
            )
            erros.extend(batch_erros)

            classified = _healthcheck_classify_resources(
                svc, recs, results, queries,
            )
            all_classified.extend(classified)

        if parcial:
            break

    return _healthcheck_build_dashboard(
        all_classified, servicos, periodo_minutos, erros, parcial,
    )


def _consultar_metricas_resolve(
    servico: str, resource_id: str,
) -> dict[str, Any]:
    """Resolve a resource_id to a concrete resource for metrics query.

    Uses the existing fuzzy resolution functions. Returns a dict with
    resource details and a ``_resolved_name`` key for display.
    """
    if servico == "MediaLive":
        if _is_numeric_or_exact_id(resource_id):
            resp = medialive_client.describe_channel(
                ChannelId=resource_id,
            )
            cleaned = _strip_keys(resp, _ML_STRIP_FIELDS)
            cleaned["_resolved_name"] = resp.get("Name", resource_id)
            cleaned["_channel_id"] = resp.get("Id", resource_id)
            return cleaned
        result = _resolve_medialive_channel(resource_id)
        if result.get("multiplos_resultados"):
            return result
        result["_resolved_name"] = result.get("Name", resource_id)
        # Id was stripped by _strip_keys — re-fetch it
        # Use describe_channel to get the numeric Id
        try:
            name = result.get("Name", resource_id)
            paginator = medialive_client.get_paginator("list_channels")
            for page in paginator.paginate():
                for ch in page.get("Channels", []):
                    if ch.get("Name", "") == name:
                        result["_channel_id"] = str(ch.get("Id", ""))
                        break
                if "_channel_id" in result:
                    break
        except Exception:
            pass
        if "_channel_id" not in result:
            result["_channel_id"] = resource_id
        return result

    elif servico == "MediaPackage":
        parts = resource_id.split("/", 1)
        if len(parts) == 2:
            resp = mediapackagev2_client.get_channel(
                ChannelGroupName=parts[0],
                ChannelName=parts[1],
            )
            resp.pop("ResponseMetadata", None)
            resp["_resolved_name"] = resp.get("ChannelName", resource_id)
            resp["_channel_group"] = parts[0]
            resp["_channel_name"] = parts[1]
            return resp
        result = _resolve_mpv2_channel(resource_id)
        if result.get("multiplos_resultados"):
            return result
        result["_resolved_name"] = result.get("ChannelName", resource_id)
        result["_channel_group"] = result.get("ChannelGroupName", "")
        result["_channel_name"] = result.get("ChannelName", "")
        return result

    elif servico == "MediaTailor":
        if _is_numeric_or_exact_id(resource_id):
            resp = mediatailor_client.get_playback_configuration(
                Name=resource_id,
            )
            resp.pop("ResponseMetadata", None)
            resp["_resolved_name"] = resp.get("Name", resource_id)
            return resp
        result = _resolve_mediatailor_config(resource_id)
        if result.get("multiplos_resultados"):
            return result
        result["_resolved_name"] = result.get("Name", resource_id)
        return result

    elif servico == "CloudFront":
        # CloudFront uses DistributionId directly (no fuzzy)
        resp = cloudfront_client.get_distribution(Id=resource_id)
        cleaned = _strip_keys(resp, _CF_STRIP_FIELDS)
        cleaned["_resolved_name"] = resource_id
        cleaned["_distribution_id"] = resource_id
        return cleaned

    raise ValueError(
        f"Serviço inválido: '{servico}'. "
        f"Válidos: {sorted(VALID_SERVICOS)}"
    )


def _consultar_metricas_build_queries(
    servico: str,
    resolved: dict[str, Any],
    granularidade_segundos: int,
    metricas_filtro: list[str] | None,
) -> list[dict[str, Any]]:
    """Build MetricDataQueries for the on-demand metrics endpoint."""
    config = _ONDEMAND_METRICS_CONFIG.get(servico)
    if not config:
        return []

    namespace = config["namespace"]
    metrics_list = config["metrics"]

    # Filter metrics if user specified a subset
    if metricas_filtro:
        filtro_lower = [m.lower() for m in metricas_filtro]
        metrics_list = [
            (name, stat) for name, stat in metrics_list
            if name.lower() in filtro_lower
        ]

    queries: list[dict[str, Any]] = []

    if servico == "MediaLive":
        channel_id = resolved.get("_channel_id", "")
        for metric_name, stat in metrics_list:
            for pipeline in ("0", "1"):
                query_id = (
                    f"{metric_name}_{pipeline}"
                    .replace(".", "_")
                    .lower()
                )
                queries.append({
                    "Id": query_id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": namespace,
                            "MetricName": metric_name,
                            "Dimensions": [
                                {"Name": "ChannelId", "Value": channel_id},
                                {"Name": "Pipeline", "Value": pipeline},
                            ],
                        },
                        "Period": granularidade_segundos,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                })

    elif servico == "MediaPackage":
        grp = resolved.get("_channel_group", "")
        ch = resolved.get("_channel_name", "")
        # Get first origin endpoint
        ep_name = ""
        try:
            ep_resp = mediapackagev2_client.list_origin_endpoints(
                ChannelGroupName=grp, ChannelName=ch, MaxResults=1,
            )
            items = ep_resp.get("Items", [])
            if items:
                ep_name = items[0].get("OriginEndpointName", "")
        except ClientError:
            pass

        for metric_name, stat in metrics_list:
            if metric_name == "EgressRequestCount":
                for status_code in ("2xx", "4xx", "5xx"):
                    query_id = (
                        f"{metric_name}_{status_code}"
                        .replace(".", "_")
                        .lower()
                    )
                    queries.append({
                        "Id": query_id,
                        "MetricStat": {
                            "Metric": {
                                "Namespace": namespace,
                                "MetricName": metric_name,
                                "Dimensions": [
                                    {"Name": "ChannelGroup", "Value": grp},
                                    {"Name": "Channel", "Value": ch},
                                    {"Name": "OriginEndpoint", "Value": ep_name},
                                    {"Name": "StatusCode", "Value": status_code},
                                ],
                            },
                            "Period": granularidade_segundos,
                            "Stat": stat,
                        },
                        "ReturnData": True,
                    })
            else:
                query_id = metric_name.replace(".", "_").lower()
                queries.append({
                    "Id": query_id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": namespace,
                            "MetricName": metric_name,
                            "Dimensions": [
                                {"Name": "ChannelGroup", "Value": grp},
                                {"Name": "Channel", "Value": ch},
                                {"Name": "OriginEndpoint", "Value": ep_name},
                            ],
                        },
                        "Period": granularidade_segundos,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                })

    elif servico == "MediaTailor":
        cfg_name = resolved.get("_resolved_name", "")
        for metric_name, stat in metrics_list:
            query_id = metric_name.replace(".", "_").lower()
            queries.append({
                "Id": query_id,
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": metric_name,
                        "Dimensions": [
                            {"Name": "ConfigurationName", "Value": cfg_name},
                        ],
                    },
                    "Period": granularidade_segundos,
                    "Stat": stat,
                },
                "ReturnData": True,
            })

    elif servico == "CloudFront":
        dist_id = resolved.get("_distribution_id", "")
        for metric_name, stat in metrics_list:
            query_id = metric_name.replace(".", "_").lower()
            queries.append({
                "Id": query_id,
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": metric_name,
                        "Dimensions": [
                            {"Name": "DistributionId", "Value": dist_id},
                            {"Name": "Region", "Value": "Global"},
                        ],
                    },
                    "Period": granularidade_segundos,
                    "Stat": stat,
                },
                "ReturnData": True,
            })

    return queries


def _consultar_metricas_query(
    servico: str,
    resolved: dict[str, Any],
    periodo_minutos: int,
    granularidade_segundos: int,
    metricas_filtro: list[str] | None,
) -> dict[str, Any]:
    """Execute CloudWatch GetMetricData and build the summary response.

    Returns a dict with severidade_geral, alertas, and metricas.
    """
    config = _ONDEMAND_METRICS_CONFIG.get(servico, {})
    region = config.get("region", "us-east-1")
    cw_client = boto3.client("cloudwatch", region_name=region)

    now = datetime.now(timezone.utc)
    start_time = now - timedelta(minutes=periodo_minutos)

    # Auto-adjust granularity for large periods to avoid
    # too many data points (CloudWatch limit + response size)
    if periodo_minutos > 1440 and granularidade_segundos < 3600:
        granularidade_segundos = 3600  # 1 hour for >24h
    elif periodo_minutos > 360 and granularidade_segundos < 900:
        granularidade_segundos = 900   # 15 min for >6h

    queries = _consultar_metricas_build_queries(
        servico, resolved, granularidade_segundos, metricas_filtro,
    )

    if not queries:
        return {
            "severidade_geral": "INFO",
            "alertas": [],
            "metricas": {},
        }

    import logging as _log
    _log.getLogger().info(
        "consultarMetricas: servico=%s region=%s "
        "periodo=%d granularidade=%d queries=%d "
        "resolved_keys=%s channel_id=%s",
        servico, region, periodo_minutos,
        granularidade_segundos, len(queries),
        list(resolved.keys()),
        resolved.get("_channel_id", "N/A"),
    )

    resp = cw_client.get_metric_data(
        MetricDataQueries=queries,
        StartTime=start_time,
        EndTime=now,
    )

    # --- Aggregate results by metric name ---
    _log.getLogger().info(
        "consultarMetricas: got %d MetricDataResults",
        len(resp.get("MetricDataResults", [])),
    )
    for _ri in resp.get("MetricDataResults", [])[:3]:
        _log.getLogger().info(
            "  %s: %d values, %d timestamps",
            _ri.get("Id", "?"),
            len(_ri.get("Values", [])),
            len(_ri.get("Timestamps", [])),
        )
    # For MediaLive, merge pipeline 0 and 1 data per metric
    # For PrimaryInputActive, use MAX instead of merging
    # (avoids false CRITICAL on SINGLE_PIPELINE channels)
    # Collect both values and timestamps for time series
    metric_data: dict[str, list[float]] = {}
    metric_timeseries: dict[str, list[dict[str, Any]]] = {}
    _primary_input_by_ts: dict[str, float] = {}
    for result_item in resp.get("MetricDataResults", []):
        query_id = result_item.get("Id", "")
        values = result_item.get("Values", [])
        timestamps = result_item.get("Timestamps", [])

        metric_name = _query_id_to_metric_name(query_id, servico)

        # Special handling for PrimaryInputActive — take max
        # across pipelines per timestamp to avoid false CRITICAL
        if metric_name == "PrimaryInputActive" and servico == "MediaLive":
            for ts_val, val in zip(timestamps, values):
                ts_key = (
                    ts_val.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if hasattr(ts_val, "strftime")
                    else str(ts_val)
                )
                if ts_key not in _primary_input_by_ts or val > _primary_input_by_ts[ts_key]:
                    _primary_input_by_ts[ts_key] = val
            continue

        if metric_name not in metric_data:
            metric_data[metric_name] = []
            metric_timeseries[metric_name] = []
        metric_data[metric_name].extend(values)
        for ts_val, val in zip(timestamps, values):
            ts_str = (
                ts_val.strftime("%Y-%m-%dT%H:%M:%SZ")
                if hasattr(ts_val, "strftime")
                else str(ts_val)
            )
            metric_timeseries[metric_name].append({
                "timestamp": ts_str,
                "value": val,
            })

    # Merge PrimaryInputActive after max-per-timestamp
    if _primary_input_by_ts:
        metric_data["PrimaryInputActive"] = list(
            _primary_input_by_ts.values(),
        )
        metric_timeseries["PrimaryInputActive"] = [
            {"timestamp": ts, "value": val}
            for ts, val in sorted(_primary_input_by_ts.items())
        ]

    # --- Build summary ---
    metricas_resumo: dict[str, dict[str, Any]] = {}
    alertas: list[dict[str, Any]] = []
    severidade_geral = "INFO"

    # Determine the unit for each metric (simple heuristic)
    metric_units = _get_metric_units(servico)

    for metric_name, values in metric_data.items():
        if values:
            atual = values[0]  # latest data point (CW returns newest first)
            maximo = max(values)
            media = sum(values) / len(values)
        else:
            atual = 0
            maximo = 0
            media = 0.0

        unidade = metric_units.get(metric_name, "None")
        # Sort time series by timestamp ascending
        series = sorted(
            metric_timeseries.get(metric_name, []),
            key=lambda x: x["timestamp"],
        )
        metricas_resumo[metric_name] = {
            "atual": atual,
            "max": maximo,
            "media": round(media, 2),
            "unidade": unidade,
            "serie_temporal": series,
        }

        # Classify severity using the latest value
        sev, tipo_erro = _classify_severity_ondemand(
            metric_name, atual, servico,
        )
        if sev != "INFO":
            alertas.append({
                "metrica": metric_name,
                "valor": atual,
                "severidade": sev,
                "tipo_erro": tipo_erro,
                "descricao": f"{metric_name} = {atual} ({sev})",
            })
            if _SEVERITY_ORDER.get(sev, 0) > _SEVERITY_ORDER.get(severidade_geral, 0):
                severidade_geral = sev

    return {
        "severidade_geral": severidade_geral,
        "alertas": alertas,
        "metricas": metricas_resumo,
    }


def _query_id_to_metric_name(query_id: str, servico: str) -> str:
    """Convert a CloudWatch query ID back to a metric name.

    Handles pipeline suffixes for MediaLive and StatusCode suffixes
    for MediaPackage EgressRequestCount.
    """
    if servico == "MediaLive":
        # Remove pipeline suffix (_0 or _1)
        for suffix in ("_0", "_1"):
            if query_id.endswith(suffix):
                base = query_id[: -len(suffix)]
                return _underscore_to_metric(base)
        return _underscore_to_metric(query_id)

    if servico == "MediaPackage":
        # EgressRequestCount with StatusCode suffix
        for sc in ("_2xx", "_4xx", "_5xx"):
            if query_id.endswith(sc):
                return f"EgressRequestCount{sc}"
        return _underscore_to_metric(query_id)

    return _underscore_to_metric(query_id)


def _underscore_to_metric(query_id: str) -> str:
    """Best-effort conversion of underscore query_id to original metric name.

    Looks up the metric config to find the original name.
    """
    for svc_config in _ONDEMAND_METRICS_CONFIG.values():
        for metric_name, _ in svc_config["metrics"]:
            if metric_name.replace(".", "_").lower() == query_id:
                return metric_name
    # Fallback: return as-is
    return query_id


def _get_metric_units(servico: str) -> dict[str, str]:
    """Return a mapping of metric name → unit for a service."""
    units: dict[str, str] = {}
    if servico == "MediaLive":
        units = {
            "ActiveAlerts": "Count",
            "InputLossSeconds": "Seconds",
            "InputVideoFrameRate": "Count/Second",
            "DroppedFrames": "Count",
            "FillMsec": "Milliseconds",
            "NetworkIn": "Bytes",
            "NetworkOut": "Bytes",
            "Output4xxErrors": "Count",
            "Output5xxErrors": "Count",
            "PrimaryInputActive": "Count",
            "ChannelInputErrorSeconds": "Seconds",
            "RtpPacketsLost": "Count",
        }
    elif servico == "MediaPackage":
        units = {
            "IngressBytes": "Bytes",
            "IngressRequestCount": "Count",
            "EgressBytes": "Bytes",
            "EgressRequestCount": "Count",
            "EgressRequestCount_2xx": "Count",
            "EgressRequestCount_4xx": "Count",
            "EgressRequestCount_5xx": "Count",
            "EgressResponseTime": "Milliseconds",
            "IngressResponseTime": "Milliseconds",
        }
    elif servico == "MediaTailor":
        units = {
            "AdDecisionServer.Ads": "Count",
            "AdDecisionServer.Duration": "Seconds",
            "AdDecisionServer.Errors": "Count",
            "AdDecisionServer.Timeouts": "Count",
            "Avail.Duration": "Seconds",
            "Avail.FilledDuration": "Seconds",
            "Avail.FillRate": "Percent",
        }
    elif servico == "CloudFront":
        units = {
            "Requests": "Count",
            "BytesDownloaded": "Bytes",
            "BytesUploaded": "Bytes",
            "4xxErrorRate": "Percent",
            "5xxErrorRate": "Percent",
            "TotalErrorRate": "Percent",
        }
    return units


# ---------------------------------------------------------------------------
# Channel comparison — pure functions
# ---------------------------------------------------------------------------

_COMPARISON_IGNORE_FIELDS: set[str] = {
    "Arn", "Id", "ChannelId", "State", "Tags",
    "ResponseMetadata", "PipelinesRunningCount",
    "EgressEndpoints", "Maintenance", "ETag",
    "CreatedAt", "ModifiedAt",
}


def _strip_comparison_fields(config: Any) -> Any:
    """Recursively remove read-only metadata fields from *config*."""
    if isinstance(config, dict):
        return {
            k: _strip_comparison_fields(v)
            for k, v in config.items()
            if k not in _COMPARISON_IGNORE_FIELDS
        }
    if isinstance(config, list):
        return [_strip_comparison_fields(item) for item in config]
    return config


def compare_configs(
    config_a: dict[str, Any],
    config_b: dict[str, Any],
    path: str = "",
) -> dict[str, Any]:
    """Pure recursive comparison of two configuration dicts.

    Returns::

        {
            "campos_iguais": ["path.field", ...],
            "campos_diferentes": [
                {"campo": "path.field",
                 "valor_recurso_1": ..., "valor_recurso_2": ...},
            ],
            "campos_exclusivos": [
                {"campo": "path.field", "presente_em": "recurso_1"},
            ],
        }
    """
    result: dict[str, list[Any]] = {
        "campos_iguais": [],
        "campos_diferentes": [],
        "campos_exclusivos": [],
    }

    all_keys = set(config_a.keys()) | set(config_b.keys())

    for key in sorted(all_keys):
        full_path = f"{path}.{key}" if path else key
        in_a = key in config_a
        in_b = key in config_b

        if in_a and not in_b:
            result["campos_exclusivos"].append(
                {"campo": full_path, "presente_em": "recurso_1"},
            )
            continue
        if in_b and not in_a:
            result["campos_exclusivos"].append(
                {"campo": full_path, "presente_em": "recurso_2"},
            )
            continue

        val_a = config_a[key]
        val_b = config_b[key]

        if isinstance(val_a, dict) and isinstance(val_b, dict):
            sub = compare_configs(val_a, val_b, full_path)
            result["campos_iguais"].extend(sub["campos_iguais"])
            result["campos_diferentes"].extend(sub["campos_diferentes"])
            result["campos_exclusivos"].extend(sub["campos_exclusivos"])
        elif isinstance(val_a, list) and isinstance(val_b, list):
            _compare_lists(val_a, val_b, full_path, result)
        else:
            if val_a == val_b:
                result["campos_iguais"].append(full_path)
            else:
                result["campos_diferentes"].append({
                    "campo": full_path,
                    "valor_recurso_1": val_a,
                    "valor_recurso_2": val_b,
                })

    return result


def _compare_lists(
    list_a: list[Any],
    list_b: list[Any],
    path: str,
    result: dict[str, list[Any]],
) -> None:
    """Compare two lists element-by-element by index."""
    min_len = min(len(list_a), len(list_b))
    for idx in range(min_len):
        elem_path = f"{path}[{idx}]"
        va, vb = list_a[idx], list_b[idx]
        if isinstance(va, dict) and isinstance(vb, dict):
            sub = compare_configs(va, vb, elem_path)
            result["campos_iguais"].extend(sub["campos_iguais"])
            result["campos_diferentes"].extend(sub["campos_diferentes"])
            result["campos_exclusivos"].extend(sub["campos_exclusivos"])
        elif va == vb:
            result["campos_iguais"].append(elem_path)
        else:
            result["campos_diferentes"].append({
                "campo": elem_path,
                "valor_recurso_1": va,
                "valor_recurso_2": vb,
            })

    # Extra elements
    for idx in range(min_len, len(list_a)):
        result["campos_exclusivos"].append(
            {"campo": f"{path}[{idx}]", "presente_em": "recurso_1"},
        )
    for idx in range(min_len, len(list_b)):
        result["campos_exclusivos"].append(
            {"campo": f"{path}[{idx}]", "presente_em": "recurso_2"},
        )


CATEGORY_PREFIXES: dict[str, list[str]] = {
    "vídeo": [
        "EncoderSettings.VideoDescriptions",
        "Width", "Height", "Bitrate", "Codec",
    ],
    "áudio": [
        "EncoderSettings.AudioDescriptions",
        "AudioSelector",
    ],
    "legendas": [
        "CaptionDescriptions", "CaptionSelector", "DvbSub",
    ],
    "outputs": [
        "EncoderSettings.OutputGroups",
        "OutputGroup", "Destination",
    ],
    "inputs": [
        "InputAttachments", "InputSpecification", "InputSettings",
    ],
    "drm": [
        "Encryption", "SpekeKeyProvider", "DrmSystems",
    ],
    "failover": [
        "AutomaticInputFailoverSettings", "Failover",
    ],
    "rede": [
        "Vpc", "SecurityGroup", "Subnet",
    ],
}


def _build_comparison_summary(
    result: dict[str, Any],
    name_1: str,
    name_2: str,
) -> str:
    """Generate a Portuguese textual summary grouped by category."""
    diffs = result.get("campos_diferentes", [])
    exclusivos = result.get("campos_exclusivos", [])
    if not diffs and not exclusivos:
        return (
            f"As configurações de {name_1} e {name_2} "
            f"são idênticas."
        )

    categorized: dict[str, list[str]] = {}
    uncategorized: list[str] = []

    for d in diffs:
        campo = d["campo"]
        matched = False
        for cat, prefixes in CATEGORY_PREFIXES.items():
            if any(p in campo for p in prefixes):
                categorized.setdefault(cat, []).append(
                    f"{campo}: {d['valor_recurso_1']} → {d['valor_recurso_2']}"
                )
                matched = True
                break
        if not matched:
            uncategorized.append(
                f"{campo}: {d['valor_recurso_1']} → {d['valor_recurso_2']}"
            )

    for e in exclusivos:
        campo = e["campo"]
        label = (
            f"{campo}: presente apenas em "
            f"{'recurso 1' if e['presente_em'] == 'recurso_1' else 'recurso 2'}"
        )
        matched = False
        for cat, prefixes in CATEGORY_PREFIXES.items():
            if any(p in campo for p in prefixes):
                categorized.setdefault(cat, []).append(label)
                matched = True
                break
        if not matched:
            uncategorized.append(label)

    lines = [
        f"Diferenças encontradas entre {name_1} e {name_2}:",
    ]
    for cat in (
        "vídeo", "áudio", "legendas", "outputs",
        "inputs", "drm", "failover", "rede",
    ):
        items = categorized.get(cat)
        if items:
            lines.append(f"\n**{cat.title()}**: " + ", ".join(items))

    if uncategorized:
        lines.append("\n**Outros**: " + ", ".join(uncategorized))

    return "\n".join(lines)


_MAX_DIFF_FIELDS = 50


def _apply_truncation(result: dict[str, Any]) -> dict[str, Any]:
    """Add counters and truncate campos_diferentes to 50 max."""
    total_iguais = len(result["campos_iguais"])
    total_diferentes = len(result["campos_diferentes"])
    total_exclusivos = len(result["campos_exclusivos"])

    if total_diferentes > _MAX_DIFF_FIELDS:
        result["campos_diferentes"] = (
            result["campos_diferentes"][:_MAX_DIFF_FIELDS]
        )

    result["total_campos_iguais"] = total_iguais
    result["total_campos_diferentes"] = total_diferentes
    result["total_campos_exclusivos"] = total_exclusivos
    return result


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

    # --- /obterConfiguracao — read-only, no config_json needed ---
    if api_path == "/obterConfiguracao":
        resource_id = parameters.get("resource_id", "")
        if not servico or not tipo_recurso or not resource_id:
            return _bedrock_response(event, 400, {
                "erro": (
                    "Parâmetros obrigatórios: servico, "
                    "tipo_recurso, resource_id"
                ),
            })
        try:
            full_cfg = get_full_config(
                servico, tipo_recurso, resource_id,
            )

            # Fuzzy search returned multiple candidates
            if full_cfg.get("multiplos_resultados"):
                return _bedrock_response(event, 200, {
                    "multiplos_resultados": True,
                    "mensagem": full_cfg["mensagem"],
                    "candidatos": full_cfg["candidatos"],
                })

            # Single result — upload JSON to S3, return
            # pre-signed URL + summary for the agent
            nome = (
                full_cfg.get("Name")
                or full_cfg.get("ChannelName")
                or resource_id
            )
            upload_info = upload_config_json(
                full_cfg, nome,
            )

            # Build a short summary so the agent can
            # describe the template without dumping 500 lines
            resumo = _build_config_summary(
                servico, tipo_recurso, full_cfg,
            )

            # Check if called from direct bypass (frontend)
            # or from Bedrock agent
            is_direct = event.get("actionGroup") == "direct_config"

            if is_direct:
                # Return inline JSON for frontend download
                body_json = json.dumps(
                    full_cfg, ensure_ascii=False,
                    indent=2, default=str,
                )
                return _bedrock_response(event, 200, {
                    "mensagem": (
                        f"Configuração de '{nome}' extraída."
                    ),
                    "resumo": resumo,
                    "dados_exportados": body_json,
                    "formato_arquivo": "json",
                })

            # Agent path — return summary + marker only
            # Include pre-formatted listings directly in mensagem
            # so the agent reproduces them in its response
            listagens = ""
            if servico == "MediaLive" and tipo_recurso == "channel":
                for key in ("outputs_formatado", "videos_formatado", "audios_formatado"):
                    if key in resumo:
                        listagens += "\n" + resumo[key]

            mensagem_completa = (
                f"Configuração de '{nome}' extraída.\n"
                f"{listagens}\n\n"
                f"INSTRUÇÃO: Você DEVE copiar e colar TODA a listagem acima "
                f"na sua resposta ao usuário. NÃO resuma, NÃO omita itens. "
                f"Mostre cada linha numerada exatamente como está.\n\n"
                f"Marcador de download: "
                f"[DOWNLOAD_CONFIG:{resource_id}:"
                f"{servico}:{tipo_recurso}]"
            )

            return _bedrock_response(event, 200, {
                "mensagem": mensagem_completa,
                "servico": servico,
                "tipo_recurso": tipo_recurso,
                "resource_id": resource_id,
                "nome_recurso": nome,
                "marcador_download": (
                    f"[DOWNLOAD_CONFIG:{resource_id}"
                    f":{servico}:{tipo_recurso}]"
                ),
            })
        except ClientError as exc:
            code = exc.response["Error"].get("Code", "")
            msg = exc.response["Error"].get("Message", "")
            logger.error(
                "obterConfiguracao error [%s]: %s", code, msg,
            )
            return _bedrock_response(event, 500, {
                "erro": f"Erro AWS: [{code}] {msg}",
            })
        except ValueError as exc:
            return _bedrock_response(event, 400, {
                "erro": str(exc),
            })

    # --- /criarRecursoBaseadoEmTemplate ---
    if api_path == "/criarRecursoBaseadoEmTemplate":
        template_id = parameters.get("template_resource_id", "")
        modificacoes = parameters.get("modificacoes", {})
        if isinstance(modificacoes, str):
            try:
                modificacoes = json.loads(modificacoes)
            except (json.JSONDecodeError, TypeError):
                modificacoes = {}

        if not servico or not tipo_recurso or not template_id:
            return _bedrock_response(event, 400, {
                "erro": (
                    "Parâmetros obrigatórios: servico, "
                    "tipo_recurso, template_resource_id"
                ),
            })
        if not modificacoes.get("Name"):
            return _bedrock_response(event, 400, {
                "erro": "modificacoes.Name é obrigatório",
            })

        try:
            result = create_from_template(
                servico, tipo_recurso,
                template_id, modificacoes,
            )

            # Fuzzy returned multiple candidates
            if result.get("multiplos_resultados"):
                return _bedrock_response(event, 200, {
                    "multiplos_resultados": True,
                    "mensagem": result["mensagem"],
                    "candidatos": result["candidatos"],
                })

            new_id = result.get("resource_id", "")
            audit_entry = build_audit_log(
                operacao="criacao_template",
                servico=servico,
                tipo_recurso=tipo_recurso,
                resource_id=new_id,
                config_aplicada=modificacoes,
                resultado="sucesso",
                rollback_info={
                    "resource_id": new_id,
                    "template_origin": template_id,
                    "acao_reversao": "delete",
                },
            )
            store_audit_log(audit_entry)

            return _bedrock_response(event, 200, {
                "mensagem": (
                    f"Canal criado com sucesso: {new_id}"
                ),
                "resource_id": new_id,
                "template_usado": template_id,
                "dados_exportados": result.get(
                    "dados_exportados", ""
                ),
                "formato_arquivo": "json",
            })

        except ClientError as exc:
            code = exc.response["Error"].get("Code", "")
            msg = exc.response["Error"].get("Message", "")
            audit_entry = build_audit_log(
                operacao="criacao_template",
                servico=servico,
                tipo_recurso=tipo_recurso,
                config_aplicada=modificacoes,
                resultado="falha",
                erro={
                    "codigo": code,
                    "mensagem": msg,
                },
            )
            store_audit_log(audit_entry)
            return _bedrock_response(event, 500, {
                "erro": f"Erro AWS: [{code}] {msg}",
            })
        except ValueError as exc:
            return _bedrock_response(event, 400, {
                "erro": str(exc),
            })

    # --- /criarCanalOrquestrado ---
    if api_path == "/criarCanalOrquestrado":
        # Validate required parameters
        required = ["nome_canal", "channel_group", "template_resource_id"]
        missing = [p for p in required if not parameters.get(p)]
        if missing:
            return _bedrock_response(event, 400, {
                "erro": f"Parâmetros obrigatórios ausentes: {', '.join(missing)}",
                "parametros_faltantes": missing,
            })

        # Build OrchestrationParams with defaults
        nome_canal = parameters["nome_canal"]
        orch_params = OrchestrationParams(
            nome_canal=nome_canal,
            channel_group=parameters["channel_group"],
            template_resource_id=parameters["template_resource_id"],
            segment_duration=int(parameters.get("segment_duration", 6)),
            drm_resource_id=parameters.get("drm_resource_id", ""),
            manifest_window_seconds=int(parameters.get("manifest_window_seconds", 7200)),
            startover_window_hls_seconds=int(parameters.get("startover_window_hls_seconds", 900)),
            startover_window_dash_seconds=int(parameters.get("startover_window_dash_seconds", 14460)),
            ts_include_dvb_subtitles=str(parameters.get("ts_include_dvb_subtitles", "true")).lower() == "true",
            min_buffer_time_seconds=int(parameters.get("min_buffer_time_seconds", 2)),
            suggested_presentation_delay_seconds=int(parameters.get("suggested_presentation_delay_seconds", 12)),
        )

        result = _execute_orchestrated_creation(orch_params)

        if result.success:
            audit_entry = build_audit_log(
                operacao="criacao_orquestrada",
                servico="MultiService",
                tipo_recurso="canal_completo",
                resource_id=result.canal_medialive_id,
                config_aplicada={"params": vars(orch_params)},
                resultado="sucesso",
                rollback_info={"recursos_criados": result.recursos_criados},
            )
            store_audit_log(audit_entry)

            return _bedrock_response(event, 200, {
                "mensagem": f"Canal {nome_canal} criado com sucesso!",
                "recursos_criados": result.recursos_criados,
                "ingest_url": result.ingest_url,
                "marcador_download": (
                    f"[DOWNLOAD_CONFIG:{result.canal_medialive_id}"
                    f":MediaLive:channel]"
                ),
            })
        else:
            audit_entry = build_audit_log(
                operacao="criacao_orquestrada",
                servico="MultiService",
                tipo_recurso="canal_completo",
                config_aplicada={"params": vars(orch_params)},
                resultado="falha",
                erro={"mensagem": result.erro},
                rollback_info={
                    "recursos_removidos": result.recursos_removidos,
                    "recursos_falha_remocao": result.recursos_falha_remocao,
                },
            )
            store_audit_log(audit_entry)

            return _bedrock_response(event, 500, {
                "erro": result.erro,
                "rollback": {
                    "recursos_removidos": result.recursos_removidos,
                    "recursos_falha_remocao": result.recursos_falha_remocao,
                },
            })

    # --- /consultarMetricas — on-demand CloudWatch metrics query ---
    if api_path == "/consultarMetricas":
        resource_id = parameters.get("resource_id", "")
        if not servico or not resource_id:
            return _bedrock_response(event, 400, {
                "erro": "Parâmetros obrigatórios: servico, resource_id",
            })

        if servico not in VALID_SERVICOS:
            return _bedrock_response(event, 400, {
                "erro": (
                    f"Serviço inválido: '{servico}'. "
                    f"Válidos: {sorted(VALID_SERVICOS)}"
                ),
            })

        periodo_minutos = int(parameters.get("periodo_minutos", 60))
        granularidade_segundos = int(parameters.get("granularidade_segundos", 300))
        metricas_filtro = parameters.get("metricas")
        if isinstance(metricas_filtro, str):
            try:
                metricas_filtro = json.loads(metricas_filtro)
            except (json.JSONDecodeError, TypeError):
                metricas_filtro = None

        # --- Resolve resource via fuzzy search ---
        try:
            resolved = _consultar_metricas_resolve(servico, resource_id)
        except ValueError as exc:
            return _bedrock_response(event, 400, {"erro": str(exc)})
        except ClientError as exc:
            code = exc.response["Error"].get("Code", "")
            msg = exc.response["Error"].get("Message", "")
            return _bedrock_response(event, 500, {
                "erro": f"Erro AWS: [{code}] {msg}",
            })

        if resolved.get("multiplos_resultados"):
            return _bedrock_response(event, 200, {
                "multiplos_resultados": True,
                "mensagem": resolved["mensagem"],
                "candidatos": resolved["candidatos"],
            })

        # --- Query CloudWatch GetMetricData ---
        try:
            resumo = _consultar_metricas_query(
                servico,
                resolved,
                periodo_minutos,
                granularidade_segundos,
                metricas_filtro,
            )
        except ClientError as exc:
            code = exc.response["Error"].get("Code", "")
            msg = exc.response["Error"].get("Message", "")
            return _bedrock_response(event, 500, {
                "erro": f"Erro AWS: [{code}] {msg}",
            })

        recurso_nome = resolved.get("_resolved_name", resource_id)
        return _bedrock_response(event, 200, {
            "mensagem": f"Métricas do canal {recurso_nome} coletadas com sucesso",
            "recurso": recurso_nome,
            "servico": servico,
            "periodo": f"últimos {periodo_minutos} minutos",
            "resumo": resumo,
        })

    # --- /deletarRecurso | /startStopCanal | /listarRecursos → consolidado em /gerenciarRecurso ---
    if api_path in ("/gerenciarRecurso", "/deletarRecurso", "/startStopCanal", "/listarRecursos"):
        acao = parameters.get("acao", "")
        resource_id = parameters.get("resource_id", "")

        if not acao:
            return _bedrock_response(event, 400, {
                "erro": "Parâmetro obrigatório: acao (deletar, start, stop, listar)",
            })

        # --- LISTAR ---
        if acao == "listar":
            if not servico or not tipo_recurso:
                return _bedrock_response(event, 400, {
                    "erro": "Para acao=listar, parâmetros obrigatórios: servico, tipo_recurso",
                })
            try:
                result = list_resources(servico, tipo_recurso)
                return _bedrock_response(event, 200, {
                    "mensagem": f"Encontrados {result['total']} recursos {tipo_recurso} em {servico}.",
                    "total": result["total"],
                    "recursos": result["recursos"],
                })
            except ClientError as exc:
                code = exc.response["Error"].get("Code", "")
                msg = exc.response["Error"].get("Message", "")
                return _bedrock_response(event, 500, {
                    "erro": f"Erro AWS: [{code}] {msg}",
                })
            except ValueError as exc:
                return _bedrock_response(event, 400, {
                    "erro": str(exc),
                })

        # --- START / STOP ---
        if acao in ("start", "stop"):
            if not resource_id:
                return _bedrock_response(event, 400, {
                    "erro": "Para acao=start/stop, parâmetro obrigatório: resource_id",
                })
            try:
                result = start_stop_channel(resource_id, acao)
                if result.get("multiplos_resultados"):
                    return _bedrock_response(event, 200, {
                        "multiplos_resultados": True,
                        "mensagem": result["mensagem"],
                        "candidatos": result["candidatos"],
                    })
                audit_entry = build_audit_log(
                    operacao=f"canal_{acao}",
                    servico="MediaLive",
                    tipo_recurso="channel",
                    resource_id=result.get("resource_id", resource_id),
                    resultado="sucesso",
                    config_aplicada={"acao": acao},
                )
                store_audit_log(audit_entry)
                return _bedrock_response(event, 200, result)
            except ClientError as exc:
                code = exc.response["Error"].get("Code", "")
                msg = exc.response["Error"].get("Message", "")
                audit_entry = build_audit_log(
                    operacao=f"canal_{acao}",
                    servico="MediaLive",
                    tipo_recurso="channel",
                    resource_id=resource_id,
                    resultado="falha",
                    erro={"codigo": code, "mensagem": msg},
                )
                store_audit_log(audit_entry)
                return _bedrock_response(event, 500, {
                    "erro": f"Erro AWS: [{code}] {msg}",
                })
            except ValueError as exc:
                return _bedrock_response(event, 400, {
                    "erro": str(exc),
                })

        # --- DELETAR ---
        if acao == "deletar":
            if not servico or not tipo_recurso or not resource_id:
                return _bedrock_response(event, 400, {
                    "erro": "Para acao=deletar, parâmetros obrigatórios: servico, tipo_recurso, resource_id",
                })
            try:
                result = delete_resource_individual(servico, tipo_recurso, resource_id)
                audit_entry = build_audit_log(
                    operacao="exclusao",
                    servico=servico,
                    tipo_recurso=tipo_recurso,
                    resource_id=resource_id,
                    resultado="sucesso",
                )
                store_audit_log(audit_entry)
                return _bedrock_response(event, 200, {
                    "mensagem": f"Recurso {resource_id} excluído com sucesso.",
                    "resource_id": resource_id,
                })
            except ClientError as exc:
                code = exc.response["Error"].get("Code", "")
                msg = exc.response["Error"].get("Message", "")
                audit_entry = build_audit_log(
                    operacao="exclusao",
                    servico=servico,
                    tipo_recurso=tipo_recurso,
                    resource_id=resource_id,
                    resultado="falha",
                    erro={"codigo": code, "mensagem": msg},
                )
                store_audit_log(audit_entry)
                return _bedrock_response(event, 500, {
                    "erro": f"Erro AWS: [{code}] {msg}",
                })
            except ValueError as exc:
                return _bedrock_response(event, 400, {
                    "erro": str(exc),
                })

        # --- HEALTHCHECK ---
        if acao == "healthcheck":
            periodo_minutos = 15
            raw_periodo = parameters.get("periodo_minutos")
            if raw_periodo is not None:
                try:
                    periodo_minutos = int(raw_periodo)
                except (ValueError, TypeError):
                    return _bedrock_response(event, 400, {
                        "erro": "periodo_minutos deve ser um inteiro positivo",
                    })
            if periodo_minutos <= 0:
                return _bedrock_response(event, 400, {
                    "erro": "periodo_minutos deve ser um inteiro positivo",
                })
            try:
                dashboard = _execute_healthcheck(
                    servico_filtro=servico or None,
                    periodo_minutos=periodo_minutos,
                )
                return _bedrock_response(event, 200, dashboard)
            except ValueError as exc:
                return _bedrock_response(event, 400, {
                    "erro": str(exc),
                })
            except Exception as exc:
                logger.error("Healthcheck error: %s", exc)
                return _bedrock_response(event, 500, {
                    "erro": f"Erro no health check: {exc}",
                })

        # --- HISTORICO ---
        if acao == "historico":
            if not resource_id:
                return _bedrock_response(event, 400, {
                    "erro": (
                        "Para acao=historico, parâmetro "
                        "obrigatório: resource_id"
                    ),
                })
            raw_periodo = parameters.get("periodo_dias")
            periodo_dias = 7
            if raw_periodo is not None:
                try:
                    periodo_dias = int(raw_periodo)
                except (ValueError, TypeError):
                    return _bedrock_response(event, 400, {
                        "erro": (
                            "periodo_dias deve ser um "
                            "inteiro positivo"
                        ),
                    })
            if periodo_dias <= 0:
                return _bedrock_response(event, 400, {
                    "erro": (
                        "periodo_dias deve ser um "
                        "inteiro positivo"
                    ),
                })
            tipo_operacao = parameters.get("tipo_operacao", "")
            try:
                result = consultar_historico(
                    resource_id, periodo_dias, tipo_operacao,
                )
                return _bedrock_response(event, 200, result)
            except ClientError as exc:
                code = exc.response["Error"].get("Code", "")
                msg = exc.response["Error"].get("Message", "")
                return _bedrock_response(event, 500, {
                    "erro": (
                        "Erro ao acessar logs de auditoria: "
                        f"[{code}] {msg}"
                    ),
                })

        # --- COMPARAR ---
        if acao == "comparar":
            resource_id_1 = resource_id or parameters.get("resource_id_1", "")
            resource_id_2 = parameters.get("resource_id_2", "")
            comp_servico = servico or "MediaLive"
            comp_tipo = tipo_recurso or "channel"

            if not resource_id_1 or not resource_id_2:
                return _bedrock_response(event, 400, {
                    "erro": (
                        "Para acao=comparar, parâmetros obrigatórios: "
                        "resource_id (ou resource_id_1), resource_id_2"
                    ),
                })

            if comp_servico not in VALID_SERVICOS:
                return _bedrock_response(event, 400, {
                    "erro": (
                        f"Serviço inválido: '{comp_servico}'. "
                        f"Válidos: {sorted(VALID_SERVICOS)}"
                    ),
                })

            valid_tipos = VALID_TIPOS_RECURSO.get(comp_servico, set())
            if comp_tipo not in valid_tipos:
                return _bedrock_response(event, 400, {
                    "erro": (
                        f"Tipo de recurso inválido '{comp_tipo}' "
                        f"para {comp_servico}. "
                        f"Válidos: {sorted(valid_tipos)}"
                    ),
                })

            try:
                cfg_1 = get_full_config(comp_servico, comp_tipo, resource_id_1)
            except (ClientError, ValueError) as exc:
                return _bedrock_response(event, 400 if isinstance(exc, ValueError) else 500, {
                    "erro": f"Erro ao buscar recurso 1 ({resource_id_1}): {exc}",
                })

            if cfg_1.get("multiplos_resultados"):
                return _bedrock_response(event, 200, {
                    "multiplos_resultados": True,
                    "recurso_ambiguo": "recurso_1",
                    "mensagem": (
                        f"Encontrei múltiplos candidatos para "
                        f"'{resource_id_1}'. Qual usar na comparação?"
                    ),
                    "candidatos": cfg_1["candidatos"],
                })

            try:
                cfg_2 = get_full_config(comp_servico, comp_tipo, resource_id_2)
            except (ClientError, ValueError) as exc:
                return _bedrock_response(event, 400 if isinstance(exc, ValueError) else 500, {
                    "erro": f"Erro ao buscar recurso 2 ({resource_id_2}): {exc}",
                })

            if cfg_2.get("multiplos_resultados"):
                return _bedrock_response(event, 200, {
                    "multiplos_resultados": True,
                    "recurso_ambiguo": "recurso_2",
                    "mensagem": (
                        f"Encontrei múltiplos candidatos para "
                        f"'{resource_id_2}'. Qual usar na comparação?"
                    ),
                    "candidatos": cfg_2["candidatos"],
                })

            name_1 = cfg_1.get("Name") or cfg_1.get("ChannelName") or resource_id_1
            name_2 = cfg_2.get("Name") or cfg_2.get("ChannelName") or resource_id_2

            clean_1 = _strip_comparison_fields(cfg_1)
            clean_2 = _strip_comparison_fields(cfg_2)

            comp_result = compare_configs(clean_1, clean_2)
            resumo = _build_comparison_summary(comp_result, name_1, name_2)
            comp_result = _apply_truncation(comp_result)

            audit_entry = build_audit_log(
                operacao="comparacao",
                servico=comp_servico,
                tipo_recurso=comp_tipo,
                resource_id=f"{resource_id_1} vs {resource_id_2}",
                resultado="sucesso",
            )
            store_audit_log(audit_entry)

            return _bedrock_response(event, 200, {
                "recurso_1": name_1,
                "recurso_2": name_2,
                "servico": comp_servico,
                "tipo_recurso": comp_tipo,
                "total_campos_iguais": comp_result["total_campos_iguais"],
                "total_campos_diferentes": comp_result["total_campos_diferentes"],
                "total_campos_exclusivos": comp_result["total_campos_exclusivos"],
                "campos_iguais": comp_result["campos_iguais"],
                "campos_diferentes": comp_result["campos_diferentes"],
                "campos_exclusivos": comp_result["campos_exclusivos"],
                "resumo_textual": resumo,
            })

        return _bedrock_response(event, 400, {
            "erro": f"Ação inválida: '{acao}'. Use: deletar, start, stop, listar, healthcheck, historico, comparar",
        })

    # --- /compararRecursos — compare two resource configs ---
    if api_path == "/compararRecursos":
        resource_id_1 = parameters.get("resource_id_1", "")
        resource_id_2 = parameters.get("resource_id_2", "")
        comp_servico = parameters.get("servico", "") or "MediaLive"
        comp_tipo = parameters.get("tipo_recurso", "") or "channel"

        if not resource_id_1 or not resource_id_2:
            return _bedrock_response(event, 400, {
                "erro": (
                    "Parâmetros obrigatórios: "
                    "resource_id_1, resource_id_2"
                ),
            })

        if comp_servico not in VALID_SERVICOS:
            return _bedrock_response(event, 400, {
                "erro": (
                    f"Serviço inválido: '{comp_servico}'. "
                    f"Válidos: {sorted(VALID_SERVICOS)}"
                ),
            })

        valid_tipos = VALID_TIPOS_RECURSO.get(comp_servico, set())
        if comp_tipo not in valid_tipos:
            return _bedrock_response(event, 400, {
                "erro": (
                    f"Tipo de recurso inválido '{comp_tipo}' "
                    f"para {comp_servico}. "
                    f"Válidos: {sorted(valid_tipos)}"
                ),
            })

        try:
            cfg_1 = get_full_config(
                comp_servico, comp_tipo, resource_id_1,
            )
        except (ClientError, ValueError) as exc:
            return _bedrock_response(event, 400 if isinstance(exc, ValueError) else 500, {
                "erro": f"Erro ao buscar recurso 1 ({resource_id_1}): {exc}",
            })

        if cfg_1.get("multiplos_resultados"):
            return _bedrock_response(event, 200, {
                "multiplos_resultados": True,
                "recurso_ambiguo": "recurso_1",
                "mensagem": (
                    f"Encontrei múltiplos candidatos para "
                    f"'{resource_id_1}'. Qual usar na comparação?"
                ),
                "candidatos": cfg_1["candidatos"],
            })

        try:
            cfg_2 = get_full_config(
                comp_servico, comp_tipo, resource_id_2,
            )
        except (ClientError, ValueError) as exc:
            return _bedrock_response(event, 400 if isinstance(exc, ValueError) else 500, {
                "erro": f"Erro ao buscar recurso 2 ({resource_id_2}): {exc}",
            })

        if cfg_2.get("multiplos_resultados"):
            return _bedrock_response(event, 200, {
                "multiplos_resultados": True,
                "recurso_ambiguo": "recurso_2",
                "mensagem": (
                    f"Encontrei múltiplos candidatos para "
                    f"'{resource_id_2}'. Qual usar na comparação?"
                ),
                "candidatos": cfg_2["candidatos"],
            })

        name_1 = (
            cfg_1.get("Name")
            or cfg_1.get("ChannelName")
            or resource_id_1
        )
        name_2 = (
            cfg_2.get("Name")
            or cfg_2.get("ChannelName")
            or resource_id_2
        )

        clean_1 = _strip_comparison_fields(cfg_1)
        clean_2 = _strip_comparison_fields(cfg_2)

        comp_result = compare_configs(clean_1, clean_2)
        resumo = _build_comparison_summary(
            comp_result, name_1, name_2,
        )
        comp_result = _apply_truncation(comp_result)

        audit_entry = build_audit_log(
            operacao="comparacao",
            servico=comp_servico,
            tipo_recurso=comp_tipo,
            resource_id=f"{resource_id_1} vs {resource_id_2}",
            resultado="sucesso",
        )
        store_audit_log(audit_entry)

        return _bedrock_response(event, 200, {
            "recurso_1": name_1,
            "recurso_2": name_2,
            "servico": comp_servico,
            "tipo_recurso": comp_tipo,
            "total_campos_iguais": comp_result["total_campos_iguais"],
            "total_campos_diferentes": comp_result["total_campos_diferentes"],
            "total_campos_exclusivos": comp_result["total_campos_exclusivos"],
            "campos_iguais": comp_result["campos_iguais"],
            "campos_diferentes": comp_result["campos_diferentes"],
            "campos_exclusivos": comp_result["campos_exclusivos"],
            "resumo_textual": resumo,
        })

    # --- Validate config JSON (for create/modify only) ---
    if not isinstance(config_json, dict):
        return _bedrock_response(event, 400, {
            "erro": "configuracao_json deve ser um objeto JSON válido"
        })

    is_modification = api_path == "/modificarRecurso" or (
        api_path == "/mutarRecurso" and parameters.get("operacao") == "modificar"
    )
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
        if api_path == "/criarRecurso" or (
            api_path == "/mutarRecurso" and parameters.get("operacao") == "criar"
        ):
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

        elif api_path == "/modificarRecurso" or (
            api_path == "/mutarRecurso" and parameters.get("operacao") == "modificar"
        ):
            resource_id = parameters.get("resource_id", "")
            if not resource_id:
                return _bedrock_response(event, 400, {
                    "erro": "resource_id é obrigatório para modificação"
                })

            # Resolve fuzzy name for MediaLive channels
            if servico == "MediaLive" and tipo_recurso == "channel":
                if not _is_numeric_or_exact_id(resource_id):
                    resolved = _resolve_medialive_channel(resource_id)
                    if resolved.get("multiplos_resultados"):
                        return _bedrock_response(event, 200, {
                            "multiplos_resultados": True,
                            "mensagem": resolved["mensagem"],
                            "candidatos": resolved["candidatos"],
                        })
                    # Id was stripped by _strip_keys — re-fetch it
                    name = resolved.get("Name", resource_id)
                    numeric_id = resource_id
                    try:
                        paginator = medialive_client.get_paginator("list_channels")
                        for page in paginator.paginate():
                            for ch in page.get("Channels", []):
                                if ch.get("Name", "") == name:
                                    numeric_id = str(ch.get("Id", ""))
                                    break
                            if numeric_id != resource_id:
                                break
                    except Exception:
                        pass
                    resource_id = numeric_id

            # Get current config for rollback
            config_anterior = get_current_config(servico, tipo_recurso, resource_id)

            # Smart patch for MediaLive channels: deep-merge into current config
            if servico == "MediaLive" and tipo_recurso == "channel":
                result = patch_and_update_medialive_channel(resource_id, config_json)
            else:
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
