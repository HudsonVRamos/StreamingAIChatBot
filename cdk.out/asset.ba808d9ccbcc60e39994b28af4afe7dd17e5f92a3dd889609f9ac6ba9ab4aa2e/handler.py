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
                # SRT Caller: default values
                create_params["Sources"] = [{
                    "Url": "srt://1.1.1.1:5000",
                    "Decryption": {
                        "Algorithm": "AES256",
                        "PassphraseSecretArn": os.environ.get(
                            "SRT_PASSPHRASE_ARN",
                            "SRTPassword",
                        ),
                    },
                }]

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
        # ---- Step 1: Create MPV2 Channel -----------------------------------
        mpv2_config = {
            "ChannelGroupName": params.channel_group,
            "ChannelName": params.nome_canal,
            "InputType": "HLS",
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

        # ---- Step 2: Create HLS and DASH endpoints -------------------------
        for ep_type in ("HLS", "DASH"):
            ep_config = _build_endpoint_config(params, ep_type)
            ep_result = create_resource(
                "MediaPackage", "origin_endpoint_v2", ep_config,
            )
            rollback_stack.append(RollbackEntry(
                servico="MediaPackage",
                tipo_recurso="origin_endpoint_v2",
                resource_id=ep_result["resource_id"],
                channel_group=params.channel_group,
                channel_name=params.nome_canal,
                endpoint_name=f"{params.nome_canal}_{ep_type}",
            ))

        # ---- Step 3: Create MediaLive Inputs -------------------------------
        template_config = get_full_config(
            "MediaLive", "channel", params.template_resource_id,
        )

        if template_config.get("multiplos_resultados"):
            raise ValueError(
                f"Template ambíguo: {template_config.get('mensagem', '')}. "
                f"Candidatos: {template_config.get('candidatos', [])}"
            )

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
        destinations = [{
            "Id": destinations_id,
            "Settings": [{"Url": ingest_url}],
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
        encoder = channel_config.get("EncoderSettings", {})
        for og in encoder.get("OutputGroups", []):
            og_settings = og.get("OutputGroupSettings", {})
            for key in ("HlsGroupSettings", "DashIsoGroupSettings", "CmafIngestGroupSettings"):
                group = og_settings.get(key, {})
                dest = group.get("Destination", {})
                if dest.get("DestinationRefId"):
                    dest["DestinationRefId"] = destinations_id

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
                "endpoint_hls": f"{params.nome_canal}_HLS",
                "endpoint_dash": f"{params.nome_canal}_DASH",
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
        return _strip_keys(resp, _ML_STRIP_FIELDS)

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
            for o in og.get("Outputs", []):
                name = o.get("OutputName", "")
                if name:
                    outputs.append(name)
        return {
            "nome": cfg.get("Name"),
            "channel_class": cfg.get("ChannelClass"),
            "resolucoes": [
                f"{v.get('Width')}x{v.get('Height')}"
                for v in videos
                if v.get("Width") and v.get("Height")
            ],
            "total_outputs": len(outputs),
            "outputs": outputs,
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
        result["_channel_id"] = result.get("Id", resource_id)
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

    queries = _consultar_metricas_build_queries(
        servico, resolved, granularidade_segundos, metricas_filtro,
    )

    if not queries:
        return {
            "severidade_geral": "INFO",
            "alertas": [],
            "metricas": {},
        }

    resp = cw_client.get_metric_data(
        MetricDataQueries=queries,
        StartTime=start_time,
        EndTime=now,
    )

    # --- Aggregate results by metric name ---
    # For MediaLive, merge pipeline 0 and 1 data per metric
    metric_data: dict[str, list[float]] = {}
    for result_item in resp.get("MetricDataResults", []):
        query_id = result_item.get("Id", "")
        values = result_item.get("Values", [])

        # Derive the metric name from the query id
        metric_name = _query_id_to_metric_name(query_id, servico)
        if metric_name not in metric_data:
            metric_data[metric_name] = []
        metric_data[metric_name].extend(values)

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
        metricas_resumo[metric_name] = {
            "atual": atual,
            "max": maximo,
            "media": round(media, 2),
            "unidade": unidade,
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
            return _bedrock_response(event, 200, {
                "mensagem": (
                    f"Configuração de '{nome}' extraída. "
                    f"Inclua na resposta o marcador "
                    f"[DOWNLOAD_CONFIG:{resource_id}:"
                    f"{servico}:{tipo_recurso}] para "
                    f"o frontend gerar o botão de download."
                ),
                "servico": servico,
                "tipo_recurso": tipo_recurso,
                "resource_id": resource_id,
                "nome_recurso": nome,
                "resumo": resumo,
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

    # --- Validate config JSON (for create/modify only) ---
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
