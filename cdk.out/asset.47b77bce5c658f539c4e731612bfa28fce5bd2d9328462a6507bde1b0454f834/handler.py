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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
# Proactive Alerts — Severity Filtering
# (Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5)
# ===================================================================

SEVERITY_ORDER = {
    "INFO": 0,
    "WARNING": 1,
    "ERROR": 2,
    "CRITICAL": 3,
}

_VALID_THRESHOLDS = {"WARNING", "ERROR", "CRITICAL"}


def get_alert_threshold() -> str:
    """Read ALERT_SEVERITY_THRESHOLD from env.

    Returns 'ERROR' if absent or invalid.
    """
    raw = os.environ.get("ALERT_SEVERITY_THRESHOLD", "")
    val = raw.strip().upper()
    if val in _VALID_THRESHOLDS:
        return val
    if raw:
        logger.warning(
            "ALERT_SEVERITY_THRESHOLD inválido: '%s'. "
            "Usando ERROR como padrão.",
            raw,
        )
    return "ERROR"


def filter_events_by_threshold(
    eventos: List[dict],
    threshold: str,
) -> List[dict]:
    """Return events with severity >= threshold."""
    min_level = SEVERITY_ORDER.get(threshold, 2)
    return [
        e for e in eventos
        if SEVERITY_ORDER.get(
            e.get("severidade", "INFO"), 0,
        ) >= min_level
    ]


# ===================================================================
# Proactive Alerts — Suppression Manager
# (Validates: Requirements 3.1–3.8)
# ===================================================================


def build_suppression_key(
    canal: str, metrica_nome: str,
) -> str:
    """Return 'canal::metrica_nome'."""
    return f"{canal}::{metrica_nome}"


class SuppressionManager:
    """Manages alert suppression state stored in S3."""

    def __init__(
        self,
        s3_client: Any,
        bucket: str,
        prefix: str,
        window_minutes: int,
    ):
        self._s3 = s3_client
        self._bucket = bucket
        self._key = (
            f"{prefix}alertas/suppression_state.json"
        )
        self._window = timedelta(minutes=window_minutes)
        self._state: Dict[str, str] = {}

    def load_state(self) -> None:
        """Read suppression_state.json from S3.

        Treats any failure as empty state (fail-open).
        """
        try:
            resp = self._s3.get_object(
                Bucket=self._bucket,
                Key=self._key,
            )
            raw = resp["Body"].read().decode("utf-8")
            self._state = json.loads(raw)
        except Exception as exc:
            logger.warning(
                "Falha ao ler estado de supressão "
                "(tratando como vazio): %s",
                exc,
            )
            self._state = {}

    def cleanup_expired(self) -> None:
        """Remove entries older than the window."""
        now = datetime.now(timezone.utc)
        cutoff = now - self._window
        self._state = {
            k: v for k, v in self._state.items()
            if _parse_iso(v) >= cutoff
        }

    def is_suppressed(self, key: str) -> bool:
        """Check if key was alerted within the window."""
        ts_str = self._state.get(key)
        if not ts_str:
            return False
        now = datetime.now(timezone.utc)
        ts = _parse_iso(ts_str)
        return (now - ts) < self._window

    def record_alert(self, key: str) -> None:
        """Record current timestamp for key."""
        now = datetime.now(timezone.utc)
        self._state[key] = now.strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def save_state(self) -> None:
        """Write updated state to S3."""
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=self._key,
                Body=json.dumps(
                    self._state,
                    ensure_ascii=False,
                ),
                ContentType="application/json",
            )
        except Exception as exc:
            logger.error(
                "Falha ao gravar estado de supressão: %s",
                exc,
            )


def _parse_iso(ts_str: str) -> datetime:
    """Parse ISO 8601Z timestamp string."""
    try:
        return datetime.strptime(
            ts_str, "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


# ===================================================================
# Proactive Alerts — Notification Formatting
# (Validates: Requirements 2.2–2.5, 6.1–6.5, 7.1–7.4)
# ===================================================================

_MAX_SNS_BYTES = 262144  # 256 KB


def get_severity_emoji(severidade: str) -> str:
    """Return emoji for severity level."""
    return {
        "CRITICAL": "\U0001f534",
        "ERROR": "\U0001f7e0",
        "WARNING": "\U0001f7e1",
    }.get(severidade, "")


def format_alert_subject(
    severidade: str, canal: str, servico: str,
) -> str:
    """Build SNS Subject line."""
    emoji = get_severity_emoji(severidade)
    return (
        f"[{emoji} {severidade}] "
        f"Alerta Streaming - {canal} - {servico}"
    )


def format_alert_message(
    eventos: List[dict],
    canal: str,
    servico: str,
) -> Tuple[str, str]:
    """Return (subject, body) formatted for SNS.

    Truncates body to 256 KB if necessary.
    """
    if not eventos:
        return ("", "")

    max_sev = max(
        eventos,
        key=lambda e: SEVERITY_ORDER.get(
            e.get("severidade", "INFO"), 0,
        ),
    )
    severidade = max_sev.get("severidade", "ERROR")
    emoji = get_severity_emoji(severidade)
    subject = format_alert_subject(
        severidade, canal, servico,
    )

    sep = "\u2550" * 40
    thin_sep = "\u2500" * 40
    header = (
        f"{sep}\n"
        f"{emoji} ALERTA {severidade} \u2014 {servico}\n"
        f"Canal: {canal}\n"
        f"{sep}\n"
    )

    items: List[str] = []
    for ev in eventos:
        item = (
            f"\u25b8 {ev.get('tipo_erro', '')}\n"
            f"  Métrica: {ev.get('metrica_nome', '')} "
            f"= {ev.get('metrica_valor', '')}\n"
            f"  Descrição: {ev.get('descricao', '')}\n"
            f"  Causa provável: "
            f"{ev.get('causa_provavel', '')}\n"
            f"  Recomendação: "
            f"{ev.get('recomendacao_correcao', '')}\n"
            f"  Timestamp: {ev.get('timestamp', '')}\n"
        )
        items.append(item)

    now_str = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    footer = (
        f"\n{thin_sep}\n"
        f"Gerado por Pipeline_Metricas em {now_str}\n"
    )

    body = header + "\n".join(items) + footer

    # Truncate to 256 KB
    encoded = body.encode("utf-8")
    if len(encoded) > _MAX_SNS_BYTES:
        # Binary search for how many items fit
        kept = 0
        for i, item in enumerate(items):
            test_body = (
                header
                + "\n".join(items[: i + 1])
                + footer
            )
            if len(test_body.encode("utf-8")) <= (
                _MAX_SNS_BYTES - 100
            ):
                kept = i + 1
            else:
                break
        omitted = len(items) - kept
        body = (
            header
            + "\n".join(items[:kept])
            + f"\n... e {omitted} eventos adicionais "
            f"omitidos\n"
            + footer
        )

    return (subject, body)


def serialize_alert_payload(
    eventos: List[dict],
) -> str:
    """Serialize payload JSON.

    ensure_ascii=False, timestamps ISO 8601Z,
    numbers as JSON numbers.
    """
    return json.dumps(
        eventos, ensure_ascii=False, default=str,
    )


# ===================================================================
# Proactive Alerts — SNS Publishing with Retry
# (Validates: Requirements 2.1, 5.1, 5.5)
# ===================================================================

_SNS_MAX_RETRIES = 3
_SNS_BASE_BACKOFF = 1  # seconds


def publish_alert(
    sns_client: Any,
    topic_arn: str,
    subject: str,
    message: str,
) -> bool:
    """Publish to SNS with exponential backoff.

    Up to 3 attempts with delays 1s, 2s, 4s on throttling.
    Returns True on success, False on final failure.
    """
    for attempt in range(_SNS_MAX_RETRIES):
        try:
            sns_client.publish(
                TopicArn=topic_arn,
                Subject=subject[:100],
                Message=message,
            )
            return True
        except Exception as exc:
            err_code = ""
            if hasattr(exc, "response"):
                err_code = (
                    exc.response.get("Error", {})
                    .get("Code", "")
                )
            is_throttle = err_code in (
                "Throttling",
                "ThrottlingException",
                "TooManyRequestsException",
            )
            if is_throttle and (
                attempt < _SNS_MAX_RETRIES - 1
            ):
                wait = _SNS_BASE_BACKOFF * (
                    2 ** attempt
                )
                logger.warning(
                    "SNS throttling, tentativa %d/%d, "
                    "aguardando %ds: %s",
                    attempt + 1,
                    _SNS_MAX_RETRIES,
                    wait,
                    exc,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "Falha ao publicar no SNS: %s",
                    exc,
                )
                return False
    return False


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
    each service's listing API in parallel. If one service
    fails, the others continue and the error is logged.

    Returns a dict with keys per service, each containing
    a list of resource dicts.
    """
    resources: Dict[str, list] = {
        "MediaLive": [],
        "MediaPackage": [],
        "MediaTailor": [],
        "CloudFront": [],
    }

    def _discover_ml():
        client = boto3.client(
            "medialive", region_name=MEDIALIVE_REGION,
        )
        return "MediaLive", _discover_medialive(client)

    def _discover_mp():
        client = boto3.client(
            "mediapackagev2",
            region_name=MEDIAPACKAGE_REGION,
        )
        return "MediaPackage", _discover_mediapackage(client)

    def _discover_mt():
        client = boto3.client(
            "mediatailor",
            region_name=MEDIATAILOR_REGION,
        )
        return "MediaTailor", _discover_mediatailor(client)

    def _discover_cf():
        client = boto3.client(
            "cloudfront",
            region_name=CLOUDFRONT_REGION,
        )
        return "CloudFront", _discover_cloudfront(client)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_discover_ml),
            executor.submit(_discover_mp),
            executor.submit(_discover_mt),
            executor.submit(_discover_cf),
        ]
        for future in as_completed(futures):
            try:
                service_name, result = future.result()
                resources[service_name] = result
            except Exception as exc:
                logger.error(
                    "Falha ao listar recursos: %s", exc,
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
    lookback_hours: int = 1,
) -> List[dict]:
    """Query CloudWatch GetMetricData for *service* resources.

    Builds MetricDataQueries from ``METRICS_CONFIG``, uses a
    configurable lookback window (default 1 hour) with 300-second
    period, and applies exponential backoff on throttling.
    """
    config = METRICS_CONFIG.get(service)
    if not config:
        return []

    region = config["region"]
    cw_client = boto3.client(
        "cloudwatch", region_name=region,
    )

    now = datetime.now(timezone.utc)
    start_time = now - timedelta(hours=lookback_hours)

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
# Proactive Alerts — Orchestration Step
# (Validates: Requirements 1.5, 2.6, 5.1–5.4)
# ===================================================================


def proactive_alerts_step(
    eventos: List[dict],
    summary: Dict[str, Any],
) -> None:
    """Orchestrate filtering, suppression, formatting,
    and SNS publishing for proactive alerts.
    """
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
    if not topic_arn:
        logger.info(
            "SNS_TOPIC_ARN não definido — "
            "alertas proativos desativados."
        )
        return

    # 1. Filter by threshold
    threshold = get_alert_threshold()
    above = filter_events_by_threshold(
        eventos, threshold,
    )
    if not above:
        logger.info(
            "Nenhum evento acima do threshold %s.",
            threshold,
        )
        return

    # 2. Group by canal
    groups: Dict[str, List[dict]] = {}
    for ev in above:
        canal = ev.get("canal", "desconhecido")
        groups.setdefault(canal, []).append(ev)

    # 3. Suppression
    supp_minutes = 60
    raw_min = os.environ.get(
        "ALERT_SUPPRESSION_MINUTES", "",
    )
    if raw_min.strip().isdigit():
        supp_minutes = int(raw_min.strip())

    s3_client = boto3.client("s3")
    mgr = SuppressionManager(
        s3_client, KB_LOGS_BUCKET,
        KB_LOGS_PREFIX, supp_minutes,
    )
    mgr.load_state()
    mgr.cleanup_expired()

    sns_client = boto3.client("sns")

    # 4. For each group: check suppression, format, publish
    for canal, evts in groups.items():
        servico = evts[0].get(
            "servico_origem", "Streaming",
        )

        # Build suppression key per canal (group-level)
        sup_key = build_suppression_key(
            canal,
            evts[0].get("metrica_nome", ""),
        )
        if mgr.is_suppressed(sup_key):
            logger.info(
                "Alerta suprimido para %s", sup_key,
            )
            summary["total_alertas_suprimidos"] += 1
            continue

        subject, body = format_alert_message(
            evts, canal, servico,
        )
        ok = publish_alert(
            sns_client, topic_arn, subject, body,
        )
        if ok:
            mgr.record_alert(sup_key)
            summary["total_alertas_enviados"] += 1
            logger.info(
                "Alerta publicado para %s", canal,
            )
        else:
            summary["total_alertas_falha"] += 1

    # 5. Save suppression state
    mgr.save_state()


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

    lookback_hours = event.get("lookback_hours", 1)
    logger.info("Lookback: %d horas", lookback_hours)
    timestamp_execucao = datetime.now(timezone.utc)

    summary: Dict[str, Any] = {
        "total_eventos_armazenados": 0,
        "total_erros": 0,
        "total_rejeitados_validacao": 0,
        "total_rejeitados_contaminacao": 0,
        "total_alertas_enviados": 0,
        "total_alertas_suprimidos": 0,
        "total_alertas_falha": 0,
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

    # Steps 2–8: Process each service in parallel
    services_to_process = [
        (svc, res) for svc, res in all_resources.items()
        if res
    ]

    def _run_service(svc_resources):
        svc, res = svc_resources
        local_summary = {
            "total_eventos_armazenados": 0,
            "total_erros": 0,
            "total_rejeitados_validacao": 0,
            "total_rejeitados_contaminacao": 0,
            "erros": [],
            "eventos": [],
        }
        local_s3 = boto3.client("s3")
        try:
            _process_service(
                local_s3, svc, res,
                timestamp_execucao, local_summary,
                lookback_hours,
            )
        except Exception as exc:
            logger.error(
                "Falha ao processar serviço %s: %s",
                svc, exc,
            )
            local_summary["total_erros"] += 1
            local_summary["erros"].append({
                "service": svc,
                "resource_id": "",
                "reason": str(exc),
            })
        return svc, local_summary

    all_stored_events: List[dict] = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_run_service, item)
            for item in services_to_process
        ]
        for future in as_completed(futures):
            try:
                svc, local = future.result()
                summary["total_eventos_armazenados"] += (
                    local["total_eventos_armazenados"]
                )
                summary["total_erros"] += local["total_erros"]
                summary["total_rejeitados_validacao"] += (
                    local["total_rejeitados_validacao"]
                )
                summary["total_rejeitados_contaminacao"] += (
                    local["total_rejeitados_contaminacao"]
                )
                summary["erros"].extend(local["erros"])
                all_stored_events.extend(
                    local.get("eventos", []),
                )
                if local["total_erros"] == 0 or local[
                    "total_eventos_armazenados"
                ] > 0:
                    summary["servicos_processados"].append(svc)
            except Exception as exc:
                logger.error(
                    "Falha em thread de serviço: %s", exc,
                )

    # Proactive Alerts — fail-open
    try:
        proactive_alerts_step(
            all_stored_events, summary,
        )
    except Exception as exc:
        logger.error(
            "Falha na etapa de alertas proativos "
            "(pipeline continua): %s",
            exc,
        )

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
    lookback_hours: int = 1,
) -> None:
    """Collect metrics and generate events for one service."""
    metric_results = collect_metrics(
        service, resources, lookback_hours,
    )

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
    """Classify, build events, validate and store for one resource.

    Generates one event per metric per data point with the real
    numeric value. This enables graphing and trend analysis over time.
    """
    resource_id = entry.get("resource_id", "")
    metrics = entry.get("metrics", {})
    resource_info = {
        "resource_id": resource_id,
        "service": service,
    }

    all_events: List[dict] = []

    config = METRICS_CONFIG.get(service, {})
    metric_defs = config.get("metrics", [])

    for metric_name, statistic in metric_defs:
        matching_keys = _find_matching_metric_keys(
            metric_name, metrics, service,
        )

        for query_key in matching_keys:
            datapoints = metrics.get(query_key, [])
            if not datapoints:
                continue

            effective_name = _effective_metric_name(
                metric_name, query_key, service,
            )

            # Emit one event per data point
            for dp_ts, dp_val in datapoints:
                severity, error_type = classify_severity(
                    effective_name, dp_val, service,
                )

                metric_data = {
                    "metric_name": effective_name,
                    "value": dp_val,
                    "timestamp": dp_ts,
                    "unit": "",
                    "period": 300,
                    "statistic": statistic,
                }
                evento = build_evento_estruturado(
                    metric_data, resource_info,
                    (severity, error_type),
                )
                all_events.append(evento)

    if not all_events:
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
        all_events.append(info_evento)

    for evento in all_events:
        _validate_and_store_event(
            s3_client, evento, service,
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

    # Step 8: Store in S3 — use data point timestamp + metric
    # name in the key to avoid collisions
    canal = evento.get("canal", resource_id)
    metric_name = evento.get("metrica_nome", "unknown")
    dp_ts_str = evento.get("timestamp", "").replace(
        ":", "",
    ).replace("-", "")
    safe_metric = metric_name.replace(".", "_")
    key = (
        f"{KB_LOGS_PREFIX}{service}/"
        f"{canal}_{safe_metric}_{dp_ts_str}.json"
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
        if "eventos" in summary:
            summary["eventos"].append(evento)
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
