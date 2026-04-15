"""Pipeline de Ingestão de Métricas CloudWatch.

Coleta métricas do CloudWatch (MediaLive, MediaPackage, MediaTailor,
CloudFront), classifica severidade por thresholds, normaliza em
Evento_Estruturado, valida campos obrigatórios, verifica contaminação
cruzada e armazena no S3 (kb-logs/).

Triggered by EventBridge scheduled event (1h interval).

Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Tuple

import boto3

from shared.validators import (
    detect_cross_contamination,
    validate_evento_estruturado,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

KB_LOGS_BUCKET = os.environ.get("KB_LOGS_BUCKET", "")
KB_LOGS_PREFIX = os.environ.get("KB_LOGS_PREFIX", "kb-logs/")


# ===================================================================
# Metrics Configuration — namespace, region, metrics+statistics
# per service (Validates: Requirements 2.1, 2.2, 2.3, 3.1, 3.2,
# 4.1, 4.2, 5.1, 5.2)
# ===================================================================

METRICS_CONFIG = {
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

# ===================================================================
# Severity Thresholds — classification rules per service and metric
# Each entry: (condition_lambda, severity, error_type)
# Ordered most severe → least severe (CRITICAL > ERROR > WARNING)
# (Validates: Requirements 2.4–2.9, 3.4–3.7, 4.3–4.6, 5.3–5.6,
# 7.1, 7.2, 7.4)
# ===================================================================

SEVERITY_THRESHOLDS = {
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

# ===================================================================
# Enrichment Map — description templates, probable cause, and
# recommendation in Portuguese for each error type
# Templates use placeholders: {canal}, {valor}, {periodo}, {metrica}
# (Validates: Requirements 6.4, 6.5, 6.6)
# ===================================================================

ENRICHMENT_MAP = {
    "ALERTA_ATIVO": {
        "descricao_template": "Canal {canal} possui {valor} alerta(s) ativo(s)",
        "causa_provavel": "Alerta ativo detectado via métrica ActiveAlerts",
        "recomendacao": "Verificar alertas no console MediaLive e resolver a causa raiz",
    },
    "INPUT_LOSS": {
        "descricao_template": "Canal {canal} apresentou {valor}s de perda de input nos últimos {periodo} minutos",
        "causa_provavel": "Perda de sinal de entrada detectada via métrica InputLossSeconds",
        "recomendacao": "Verificar fonte de entrada e conectividade de rede do canal",
    },
    "FRAMES_PERDIDOS": {
        "descricao_template": "Canal {canal} perdeu {valor} frames nos últimos {periodo} minutos",
        "causa_provavel": "Frames descartados detectados via métrica DroppedFrames",
        "recomendacao": "Verificar capacidade de processamento e bitrate de entrada",
    },
    "OUTPUT_ERROR": {
        "descricao_template": "Canal {canal} apresentou {valor} erros de output ({metrica})",
        "causa_provavel": "Erros HTTP na saída do canal detectados via métricas Output4xx/5xxErrors",
        "recomendacao": "Verificar destino de output e configuração de empacotamento",
    },
    "FAILOVER_DETECTADO": {
        "descricao_template": "Canal {canal} está operando em pipeline secundário (failover ativo)",
        "causa_provavel": "Pipeline primário inativo detectado via métrica PrimaryInputActive=0",
        "recomendacao": "Investigar pipeline primário imediatamente — verificar input e encoder",
    },
    "EGRESS_5XX": {
        "descricao_template": "Canal {canal} apresentou {valor} erros 5xx no egress do MediaPackage",
        "causa_provavel": "Erros de servidor no empacotamento/distribuição",
        "recomendacao": "Verificar saúde do origin endpoint e logs do MediaPackage",
    },
    "EGRESS_4XX": {
        "descricao_template": "Canal {canal} apresentou {valor} erros 4xx no egress do MediaPackage",
        "causa_provavel": "Requisições inválidas no egress do MediaPackage",
        "recomendacao": "Verificar configuração de endpoints e permissões de acesso",
    },
    "LATENCIA_ALTA": {
        "descricao_template": "Canal {canal} com latência média de {valor}ms no egress",
        "causa_provavel": "Tempo de resposta elevado no MediaPackage",
        "recomendacao": "Verificar carga do endpoint e configuração de segmentos",
    },
    "INGESTAO_PARADA": {
        "descricao_template": "Canal {canal} sem bytes de ingestão por mais de um período consecutivo",
        "causa_provavel": "Nenhum dado sendo ingerido pelo MediaPackage",
        "recomendacao": "Verificar se o canal MediaLive está transmitindo para o endpoint de ingestão",
    },
    "AD_SERVER_ERROR": {
        "descricao_template": "Configuração {canal} apresentou {valor} erros no ad decision server",
        "causa_provavel": "Falhas na comunicação com o servidor de decisão de anúncios",
        "recomendacao": "Verificar URL e disponibilidade do ad decision server",
    },
    "AD_SERVER_TIMEOUT": {
        "descricao_template": "Configuração {canal} apresentou {valor} timeouts no ad decision server",
        "causa_provavel": "Servidor de anúncios não respondendo dentro do tempo limite",
        "recomendacao": "Verificar latência do ad server e considerar aumentar timeout",
    },
    "FILL_RATE_BAIXO": {
        "descricao_template": "Configuração {canal} com fill rate de {valor}% (abaixo de 80%)",
        "causa_provavel": "Taxa de preenchimento de avails abaixo do esperado",
        "recomendacao": "Verificar inventário de anúncios e configuração de avails",
    },
    "FILL_RATE_CRITICO": {
        "descricao_template": "Configuração {canal} com fill rate de {valor}% (abaixo de 50%)",
        "causa_provavel": "Taxa de preenchimento de avails criticamente baixa",
        "recomendacao": "Ação imediata: verificar ad server, inventário e configuração de slate",
    },
    "CDN_5XX_ALTO": {
        "descricao_template": "Distribuição {canal} com taxa de erros 5xx de {valor}% (acima de 5%)",
        "causa_provavel": "Taxa elevada de erros de servidor na CDN",
        "recomendacao": "Verificar saúde das origins e configuração do CloudFront",
    },
    "CDN_4XX_ALTO": {
        "descricao_template": "Distribuição {canal} com taxa de erros 4xx de {valor}% (acima de 10%)",
        "causa_provavel": "Taxa elevada de erros de cliente na CDN",
        "recomendacao": "Verificar URLs de acesso e configuração de cache behaviors",
    },
    "CDN_ERROR_CRITICO": {
        "descricao_template": "Distribuição {canal} com taxa total de erros de {valor}% (acima de 15%)",
        "causa_provavel": "Taxa total de erros da CDN em nível crítico",
        "recomendacao": "Ação imediata: verificar origins, cache e configuração de distribuição",
    },
    "METRICAS_NORMAIS": {
        "descricao_template": "Recurso {canal} operando normalmente — todas as métricas dentro dos limites",
        "causa_provavel": "Nenhuma anomalia detectada",
        "recomendacao": "Nenhuma ação necessária",
    },
}


# ===================================================================
# Dynamic Resource Discovery
# (Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6)
# ===================================================================

MEDIALIVE_REGION = "sa-east-1"
MEDIAPACKAGE_REGION = "sa-east-1"
MEDIATAILOR_REGION = os.environ.get(
    "MEDIATAILOR_REGION", "us-east-1",
)
CLOUDFRONT_REGION = os.environ.get(
    "CLOUDFRONT_REGION", "us-east-1",
)


def discover_resources() -> Dict[str, list]:
    """List active resources from each streaming service.

    Creates separate boto3 clients per region and queries
    each service's listing API. If one service fails, the
    others continue and the error is logged.

    Returns a dict with keys per service, each containing
    a list of resource dicts.
    """
    resources: Dict[str, list] = {
        "MediaLive": [],
        "MediaPackage": [],
        "MediaTailor": [],
        "CloudFront": [],
    }

    # --- MediaLive (sa-east-1) ---
    try:
        ml_client = boto3.client(
            "medialive", region_name=MEDIALIVE_REGION,
        )
        resources["MediaLive"] = _discover_medialive(
            ml_client,
        )
    except Exception as exc:
        logger.error(
            "Falha ao listar recursos MediaLive: %s", exc,
        )

    # --- MediaPackage V2 (sa-east-1) ---
    try:
        mp_client = boto3.client(
            "mediapackagev2",
            region_name=MEDIAPACKAGE_REGION,
        )
        resources["MediaPackage"] = (
            _discover_mediapackage(mp_client)
        )
    except Exception as exc:
        logger.error(
            "Falha ao listar recursos MediaPackage: %s",
            exc,
        )

    # --- MediaTailor (us-east-1) ---
    try:
        mt_client = boto3.client(
            "mediatailor",
            region_name=MEDIATAILOR_REGION,
        )
        resources["MediaTailor"] = (
            _discover_mediatailor(mt_client)
        )
    except Exception as exc:
        logger.error(
            "Falha ao listar recursos MediaTailor: %s",
            exc,
        )

    # --- CloudFront (us-east-1) ---
    try:
        cf_client = boto3.client(
            "cloudfront",
            region_name=CLOUDFRONT_REGION,
        )
        resources["CloudFront"] = (
            _discover_cloudfront(cf_client)
        )
    except Exception as exc:
        logger.error(
            "Falha ao listar recursos CloudFront: %s",
            exc,
        )

    return resources


def _discover_medialive(client: Any) -> List[dict]:
    """List all MediaLive channels via ListChannels."""
    channels: List[dict] = []
    params: Dict[str, Any] = {}
    while True:
        resp = client.list_channels(**params)
        for ch in resp.get("Channels", []):
            channels.append({
                "ChannelId": str(ch.get("Id", "")),
                "Name": ch.get("Name", ""),
            })
        token = resp.get("NextToken")
        if not token:
            break
        params["NextToken"] = token
    return channels


def _discover_mediapackage(
    client: Any,
) -> List[dict]:
    """List MediaPackage V2 resources.

    Walks ChannelGroups → Channels → OriginEndpoints.
    """
    resources: List[dict] = []
    grp_params: Dict[str, Any] = {}
    while True:
        grp_resp = client.list_channel_groups(
            **grp_params,
        )
        for grp in grp_resp.get("Items", []):
            grp_name = grp.get("ChannelGroupName", "")
            ch_params: Dict[str, Any] = {
                "ChannelGroupName": grp_name,
            }
            while True:
                ch_resp = client.list_channels(
                    **ch_params,
                )
                for ch in ch_resp.get("Items", []):
                    ch_name = ch.get(
                        "ChannelName", "",
                    )
                    ep_params: Dict[str, Any] = {
                        "ChannelGroupName": grp_name,
                        "ChannelName": ch_name,
                    }
                    endpoints: List[str] = []
                    while True:
                        ep_resp = (
                            client
                            .list_origin_endpoints(
                                **ep_params,
                            )
                        )
                        for ep in ep_resp.get(
                            "Items", [],
                        ):
                            endpoints.append(
                                ep.get(
                                    "OriginEndpointName",
                                    "",
                                ),
                            )
                        ep_tok = ep_resp.get(
                            "NextToken",
                        )
                        if not ep_tok:
                            break
                        ep_params["NextToken"] = (
                            ep_tok
                        )
                    resources.append({
                        "ChannelGroup": grp_name,
                        "ChannelName": ch_name,
                        "OriginEndpoints": endpoints,
                    })
                ch_tok = ch_resp.get("NextToken")
                if not ch_tok:
                    break
                ch_params["NextToken"] = ch_tok
        grp_tok = grp_resp.get("NextToken")
        if not grp_tok:
            break
        grp_params["NextToken"] = grp_tok
    return resources


def _discover_mediatailor(
    client: Any,
) -> List[dict]:
    """List MediaTailor playback configurations."""
    configs: List[dict] = []
    params: Dict[str, Any] = {}
    while True:
        resp = client.list_playback_configurations(
            **params,
        )
        for cfg in resp.get("Items", []):
            configs.append({
                "Name": cfg.get("Name", ""),
            })
        token = resp.get("NextToken")
        if not token:
            break
        params["NextToken"] = token
    return configs


def _discover_cloudfront(
    client: Any,
) -> List[dict]:
    """List CloudFront distributions."""
    distributions: List[dict] = []
    params: Dict[str, Any] = {}
    while True:
        resp = client.list_distributions(**params)
        dist_list = resp.get(
            "DistributionList", {},
        )
        for item in dist_list.get("Items", []):
            distributions.append({
                "DistributionId": item.get("Id", ""),
            })
        if dist_list.get("IsTruncated"):
            marker = dist_list.get("NextMarker")
            if marker:
                params["Marker"] = marker
            else:
                break
        else:
            break
    return distributions


# ===================================================================
# Metrics Collection
# (Validates: Requirements 2.1, 2.2, 2.3, 3.1, 3.2, 3.3,
# 4.1, 4.2, 5.1, 5.2, 11.3, 11.6)
# ===================================================================

_MAX_RETRIES = 3
_BASE_BACKOFF_SECONDS = 1


def _build_metric_queries(
    service: str,
    resource: dict,
) -> List[Dict[str, Any]]:
    """Build MetricDataQueries for a single resource.

    Returns a list of CloudWatch MetricDataQuery dicts ready
    for ``get_metric_data``.
    """
    config = METRICS_CONFIG[service]
    namespace = config["namespace"]
    queries: List[Dict[str, Any]] = []

    if service == "MediaLive":
        channel_id = resource.get("ChannelId", "")
        for metric_name, stat in config["metrics"]:
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
                                {
                                    "Name": "ChannelId",
                                    "Value": channel_id,
                                },
                                {
                                    "Name": "Pipeline",
                                    "Value": pipeline,
                                },
                            ],
                        },
                        "Period": 300,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                })

    elif service == "MediaPackage":
        grp = resource.get("ChannelGroup", "")
        ch = resource.get("ChannelName", "")
        endpoints = resource.get("OriginEndpoints", [])
        ep_name = endpoints[0] if endpoints else ""

        for metric_name, stat in config["metrics"]:
            if metric_name == "EgressRequestCount":
                # Query per StatusCode: 2xx, 4xx, 5xx
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
                                    {
                                        "Name": "ChannelGroup",
                                        "Value": grp,
                                    },
                                    {
                                        "Name": "Channel",
                                        "Value": ch,
                                    },
                                    {
                                        "Name": "OriginEndpoint",
                                        "Value": ep_name,
                                    },
                                    {
                                        "Name": "StatusCode",
                                        "Value": status_code,
                                    },
                                ],
                            },
                            "Period": 300,
                            "Stat": stat,
                        },
                        "ReturnData": True,
                    })
            else:
                query_id = (
                    metric_name.replace(".", "_").lower()
                )
                queries.append({
                    "Id": query_id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": namespace,
                            "MetricName": metric_name,
                            "Dimensions": [
                                {
                                    "Name": "ChannelGroup",
                                    "Value": grp,
                                },
                                {
                                    "Name": "Channel",
                                    "Value": ch,
                                },
                                {
                                    "Name": "OriginEndpoint",
                                    "Value": ep_name,
                                },
                            ],
                        },
                        "Period": 300,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                })

    elif service == "MediaTailor":
        cfg_name = resource.get("Name", "")
        for metric_name, stat in config["metrics"]:
            query_id = (
                metric_name.replace(".", "_").lower()
            )
            queries.append({
                "Id": query_id,
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": metric_name,
                        "Dimensions": [
                            {
                                "Name": "ConfigurationName",
                                "Value": cfg_name,
                            },
                        ],
                    },
                    "Period": 300,
                    "Stat": stat,
                },
                "ReturnData": True,
            })

    elif service == "CloudFront":
        dist_id = resource.get("DistributionId", "")
        for metric_name, stat in config["metrics"]:
            query_id = (
                metric_name.replace(".", "_").lower()
            )
            queries.append({
                "Id": query_id,
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": metric_name,
                        "Dimensions": [
                            {
                                "Name": "DistributionId",
                                "Value": dist_id,
                            },
                            {
                                "Name": "Region",
                                "Value": "Global",
                            },
                        ],
                    },
                    "Period": 300,
                    "Stat": stat,
                },
                "ReturnData": True,
            })

    return queries


def _get_resource_id(service: str, resource: dict) -> str:
    """Return a human-readable identifier for a resource."""
    if service == "MediaLive":
        return resource.get("Name") or resource.get(
            "ChannelId", "",
        )
    if service == "MediaPackage":
        return resource.get("ChannelName", "")
    if service == "MediaTailor":
        return resource.get("Name", "")
    if service == "CloudFront":
        return resource.get("DistributionId", "")
    return ""


def collect_metrics(
    service: str,
    resources: List[dict],
) -> List[dict]:
    """Query CloudWatch GetMetricData for *service* resources.

    Builds MetricDataQueries from ``METRICS_CONFIG``, uses a
    1-hour window with 300-second period, and applies exponential
    backoff (1 s, 2 s, 4 s) with up to 3 retries on throttling.

    Returns a list of dicts, one per resource, each containing:
    - ``resource``: the original resource dict
    - ``resource_id``: human-readable identifier
    - ``service``: service name
    - ``metrics``: dict mapping metric label → list of
      ``(timestamp, value)`` tuples
    - ``partial``: True when CloudWatch returned partial data
    - ``error``: error string if the call failed after retries
    """
    config = METRICS_CONFIG.get(service)
    if not config:
        return []

    region = config["region"]
    cw_client = boto3.client(
        "cloudwatch", region_name=region,
    )

    now = datetime.now(timezone.utc)
    start_time = now - timedelta(hours=1)

    results: List[dict] = []

    for resource in resources:
        resource_id = _get_resource_id(service, resource)
        queries = _build_metric_queries(service, resource)

        if not queries:
            continue

        entry: Dict[str, Any] = {
            "resource": resource,
            "resource_id": resource_id,
            "service": service,
            "metrics": {},
            "partial": False,
            "error": None,
        }

        attempt = 0
        while attempt < _MAX_RETRIES:
            try:
                resp = cw_client.get_metric_data(
                    MetricDataQueries=queries,
                    StartTime=start_time,
                    EndTime=now,
                )

                # Check for partial results
                if resp.get("Messages"):
                    entry["partial"] = True
                    logger.warning(
                        "Dados parciais para %s/%s: %s",
                        service,
                        resource_id,
                        resp["Messages"],
                    )

                for result_item in resp.get(
                    "MetricDataResults", [],
                ):
                    label = result_item.get("Id", "")
                    timestamps = result_item.get(
                        "Timestamps", [],
                    )
                    values = result_item.get(
                        "Values", [],
                    )
                    entry["metrics"][label] = list(
                        zip(timestamps, values),
                    )

                break

            except cw_client.exceptions.ClientError as exc:
                error_code = exc.response.get(
                    "Error", {},
                ).get("Code", "")
                if error_code in (
                    "TooManyRequestsException",
                    "Throttling",
                    "ThrottlingException",
                ):
                    attempt += 1
                    if attempt < _MAX_RETRIES:
                        wait = (
                            _BASE_BACKOFF_SECONDS
                            * (2 ** (attempt - 1))
                        )
                        logger.warning(
                            "Throttling para %s/%s, "
                            "tentativa %d/%d, "
                            "aguardando %ds",
                            service,
                            resource_id,
                            attempt,
                            _MAX_RETRIES,
                            wait,
                        )
                        time.sleep(wait)
                    else:
                        entry["error"] = str(exc)
                        logger.error(
                            "Falha após %d tentativas "
                            "para %s/%s: %s",
                            _MAX_RETRIES,
                            service,
                            resource_id,
                            exc,
                        )
                else:
                    entry["error"] = str(exc)
                    logger.error(
                        "Erro CloudWatch para %s/%s: %s",
                        service,
                        resource_id,
                        exc,
                    )
                    break

            except Exception as exc:
                entry["error"] = str(exc)
                logger.error(
                    "Erro inesperado para %s/%s: %s",
                    service,
                    resource_id,
                    exc,
                )
                break

        results.append(entry)

    return results


# ===================================================================
# Severity Classification
# (Validates: Requirements 7.1, 7.2, 7.4)
# ===================================================================


def classify_severity(
    metric_name: str,
    value: float,
    service: str,
) -> Tuple[str, str]:
    """Classify a metric value into a severity level.

    Looks up ``SEVERITY_THRESHOLDS[service][metric_name]`` and
    returns the first matching ``(severity, error_type)`` tuple.
    Rules are ordered most-severe-first so the highest applicable
    severity is always returned.

    Returns ``("INFO", "METRICAS_NORMAIS")`` when no threshold
    is exceeded.
    """
    service_thresholds = SEVERITY_THRESHOLDS.get(service, {})
    rules = service_thresholds.get(metric_name, [])

    for condition, severity, error_type in rules:
        if condition(value):
            return (severity, error_type)

    return ("INFO", "METRICAS_NORMAIS")


def build_evento_estruturado(
    metric_data: dict,
    resource_info: dict,
    severity_info: Tuple[str, str],
) -> dict:
    """Build a normalized Evento_Estruturado from metric data.

    Args:
        metric_data: dict with keys metric_name, value, timestamp,
            unit, period, statistic.
        resource_info: dict with keys resource_id, service.
        severity_info: tuple (severity, error_type) from
            classify_severity.

    Returns:
        A flat dict conforming to the Evento_Estruturado schema
        validated by ``validate_evento_estruturado``.

    Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8
    """
    severidade, tipo_erro = severity_info
    canal = resource_info.get("resource_id", "")
    service = resource_info.get("service", "")
    metric_name = metric_data.get("metric_name", "")
    value = metric_data.get("value", 0)
    period = metric_data.get("period", 300)

    # --- timestamp: use the CloudWatch data point timestamp ---
    ts_raw = metric_data.get("timestamp")
    if isinstance(ts_raw, datetime):
        timestamp = ts_raw.strftime("%Y-%m-%dT%H:%M:%SZ")
    elif isinstance(ts_raw, str) and ts_raw.strip():
        timestamp = ts_raw
    else:
        timestamp = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    # --- Enrichment from ENRICHMENT_MAP ---
    enrichment = ENRICHMENT_MAP.get(tipo_erro, {})
    descricao_template = enrichment.get(
        "descricao_template",
        "Métrica {metrica} com valor {valor} para {canal}",
    )
    causa_provavel = enrichment.get(
        "causa_provavel", "Causa não identificada",
    )
    recomendacao = enrichment.get(
        "recomendacao", "Investigar logs detalhados",
    )

    periodo_minutos = period // 60 if period else 5
    descricao = descricao_template.format(
        canal=canal,
        valor=value,
        periodo=periodo_minutos,
        metrica=metric_name,
    )

    return {
        "timestamp": timestamp,
        "canal": canal,
        "severidade": severidade,
        "tipo_erro": tipo_erro,
        "descricao": descricao,
        "causa_provavel": causa_provavel,
        "recomendacao_correcao": recomendacao,
        "servico_origem": service,
        "metrica_nome": metric_name,
        "metrica_valor": value,
        "metrica_unidade": metric_data.get("unit", ""),
        "metrica_periodo": period,
        "metrica_estatistica": metric_data.get(
            "statistic", "",
        ),
    }


def generate_s3_key(
    service: str,
    canal: str,
    timestamp: datetime,
) -> str:
    """Generate the S3 object key for an event.

    Format: ``{KB_LOGS_PREFIX}{servico}/{canal}_{YYYYMMDDTHHMMSSz}.json``

    The *timestamp* is the execution timestamp (not the data point
    timestamp) and is formatted as ``YYYYMMDDTHHMMSSz``.

    Validates: Requirements 8.2, 9.2
    """
    ts_str = timestamp.strftime("%Y%m%dT%H%M%SZ")
    return (
        f"{KB_LOGS_PREFIX}{service}/"
        f"{canal}_{ts_str}.json"
    )


# ===================================================================
# Handler — Metrics Pipeline Orchestrator
# (Validates: Requirements 7.3, 7.5, 8.1–8.7, 9.2, 11.1–11.5)
# ===================================================================


def handler(event: dict, context: Any) -> Dict[str, Any]:
    """Lambda handler triggered by EventBridge scheduled event.

    Orchestrates the full metrics pipeline:
    1. discover_resources() → list active resources per service
    2. collect_metrics(service, resources) per service
    3. classify_severity for each metric of each resource
    4. build_evento_estruturado for anomalous metrics
    5. build one consolidated INFO event for normal resources
    6. validate_evento_estruturado for each event
    7. detect_cross_contamination for each event
    8. Store valid events in S3 as individual JSON files

    Returns a summary dict with totals.
    """
    logger.info("Pipeline de métricas iniciado")

    s3_client = boto3.client("s3")
    timestamp_execucao = datetime.now(timezone.utc)

    summary: Dict[str, Any] = {
        "total_eventos_armazenados": 0,
        "total_erros": 0,
        "total_rejeitados_validacao": 0,
        "total_rejeitados_contaminacao": 0,
        "servicos_processados": [],
        "erros": [],
    }

    # Step 1: Discover resources
    try:
        all_resources = discover_resources()
    except Exception as exc:
        logger.error("Falha na descoberta de recursos: %s", exc)
        summary["total_erros"] += 1
        summary["erros"].append({
            "service": "discovery",
            "resource_id": "",
            "reason": str(exc),
        })
        return {"statusCode": 200, "body": summary}

    # Steps 2–8: Process each service
    for service, resources in all_resources.items():
        if not resources:
            continue

        try:
            _process_service(
                s3_client,
                service,
                resources,
                timestamp_execucao,
                summary,
            )
            if service not in summary["servicos_processados"]:
                summary["servicos_processados"].append(service)
        except Exception as exc:
            logger.error(
                "Falha ao processar serviço %s: %s",
                service, exc,
            )
            summary["total_erros"] += 1
            summary["erros"].append({
                "service": service,
                "resource_id": "",
                "reason": str(exc),
            })

    logger.info(
        "Pipeline de métricas finalizado: "
        "%d armazenados, %d erros, "
        "%d rejeitados validação, "
        "%d rejeitados contaminação",
        summary["total_eventos_armazenados"],
        summary["total_erros"],
        summary["total_rejeitados_validacao"],
        summary["total_rejeitados_contaminacao"],
    )

    return {"statusCode": 200, "body": summary}


def _process_service(
    s3_client: Any,
    service: str,
    resources: List[dict],
    timestamp_execucao: datetime,
    summary: Dict[str, Any],
) -> None:
    """Collect metrics and generate events for one service."""
    # Step 2: Collect metrics for all resources of this service
    metric_results = collect_metrics(service, resources)

    for entry in metric_results:
        resource_id = entry.get("resource_id", "")

        # If collect_metrics recorded an error for this resource
        if entry.get("error"):
            summary["total_erros"] += 1
            summary["erros"].append({
                "service": service,
                "resource_id": resource_id,
                "reason": entry["error"],
            })
            continue

        try:
            _process_resource(
                s3_client,
                service,
                entry,
                timestamp_execucao,
                summary,
            )
        except Exception as exc:
            logger.error(
                "Falha ao processar recurso %s/%s: %s",
                service, resource_id, exc,
            )
            summary["total_erros"] += 1
            summary["erros"].append({
                "service": service,
                "resource_id": resource_id,
                "reason": str(exc),
            })


def _process_resource(
    s3_client: Any,
    service: str,
    entry: dict,
    timestamp_execucao: datetime,
    summary: Dict[str, Any],
) -> None:
    """Classify, build events, validate and store for one resource."""
    resource_id = entry.get("resource_id", "")
    metrics = entry.get("metrics", {})
    resource_info = {
        "resource_id": resource_id,
        "service": service,
    }

    anomalous_events: List[dict] = []

    # Step 3: Classify severity for each metric
    config = METRICS_CONFIG.get(service, {})
    metric_defs = config.get("metrics", [])

    for metric_name, statistic in metric_defs:
        # Find matching query results — may have multiple
        # (e.g. per-pipeline for MediaLive, per-StatusCode
        # for MediaPackage EgressRequestCount)
        matching_keys = _find_matching_metric_keys(
            metric_name, metrics, service,
        )

        for query_key in matching_keys:
            datapoints = metrics.get(query_key, [])
            if not datapoints:
                continue

            # Use the latest data point
            latest_ts, latest_val = datapoints[0]
            for ts, val in datapoints:
                if ts > latest_ts:
                    latest_ts = ts
                    latest_val = val

            # Determine the effective metric name for
            # threshold lookup
            effective_name = _effective_metric_name(
                metric_name, query_key, service,
            )

            severity, error_type = classify_severity(
                effective_name, latest_val, service,
            )

            if severity != "INFO":
                # Step 4: Build event for anomalous metric
                metric_data = {
                    "metric_name": effective_name,
                    "value": latest_val,
                    "timestamp": latest_ts,
                    "unit": "",
                    "period": 300,
                    "statistic": statistic,
                }
                evento = build_evento_estruturado(
                    metric_data, resource_info,
                    (severity, error_type),
                )
                anomalous_events.append(evento)

    if anomalous_events:
        # Store each anomalous event individually
        for evento in anomalous_events:
            _validate_and_store_event(
                s3_client, evento, service,
                resource_id, timestamp_execucao,
                summary,
            )
    else:
        # Step 5: All metrics normal — build one INFO event
        info_evento = build_evento_estruturado(
            {
                "metric_name": "METRICAS_NORMAIS",
                "value": 0,
                "timestamp": timestamp_execucao,
                "unit": "",
                "period": 300,
                "statistic": "",
            },
            resource_info,
            ("INFO", "METRICAS_NORMAIS"),
        )
        _validate_and_store_event(
            s3_client, info_evento, service,
            resource_id, timestamp_execucao,
            summary,
        )


def _find_matching_metric_keys(
    metric_name: str,
    metrics: dict,
    service: str,
) -> List[str]:
    """Find query result keys that correspond to a metric name.

    For MediaLive, metrics are queried per-pipeline so keys
    look like ``activealerts_0``, ``activealerts_1``.
    For MediaPackage EgressRequestCount, keys include the
    status code: ``egressrequestcount_4xx``, etc.
    Other metrics use the metric name directly as the key.
    """
    base = metric_name.replace(".", "_").lower()
    matched = [
        k for k in metrics
        if k == base or k.startswith(base + "_")
    ]
    return matched if matched else []


def _effective_metric_name(
    metric_name: str,
    query_key: str,
    service: str,
) -> str:
    """Determine the threshold-lookup metric name.

    For MediaPackage EgressRequestCount queries split by
    StatusCode (e.g. ``egressrequestcount_4xx``), the
    threshold key is ``EgressRequestCount_4xx``.
    For all other cases, the original metric_name is used.
    """
    if (
        service == "MediaPackage"
        and metric_name == "EgressRequestCount"
    ):
        suffix = query_key.split("_")[-1]
        return f"EgressRequestCount_{suffix}"
    return metric_name


def _validate_and_store_event(
    s3_client: Any,
    evento: dict,
    service: str,
    resource_id: str,
    timestamp_execucao: datetime,
    summary: Dict[str, Any],
) -> None:
    """Validate, check contamination, and store one event."""
    # Step 6: Validate
    validation = validate_evento_estruturado(evento)
    if not validation.is_valid:
        logger.warning(
            "Validação falhou para %s/%s: %s",
            service, resource_id, validation.errors,
        )
        summary["total_rejeitados_validacao"] += 1
        return

    # Step 7: Cross-contamination check
    contamination = detect_cross_contamination(
        evento, "kb-logs",
    )
    if contamination.is_contaminated:
        logger.warning(
            "Contaminação cruzada para %s/%s: %s",
            service, resource_id,
            contamination.alert_message,
        )
        summary["total_rejeitados_contaminacao"] += 1
        return

    # Step 8: Store in S3
    canal = evento.get("canal", resource_id)
    key = generate_s3_key(
        service, canal, timestamp_execucao,
    )

    try:
        s3_client.put_object(
            Bucket=KB_LOGS_BUCKET,
            Key=key,
            Body=json.dumps(
                evento, ensure_ascii=False, default=str,
            ),
            ContentType="application/json",
        )
        summary["total_eventos_armazenados"] += 1
        logger.info(
            "Evento armazenado: s3://%s/%s",
            KB_LOGS_BUCKET, key,
        )
    except Exception as exc:
        logger.error(
            "Falha ao armazenar evento %s/%s: %s",
            service, resource_id, exc,
        )
        summary["total_erros"] += 1
        summary["erros"].append({
            "service": service,
            "resource_id": resource_id,
            "reason": str(exc),
        })
