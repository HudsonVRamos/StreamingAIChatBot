"""Pipeline de Ingestão de Dados de Anúncios — SpringServe.

Collects data from the SpringServe REST API (supply tags, demand tags,
reports, delivery modifiers, creatives, labels), correlates with
MediaTailor playback configurations, normalises to flat JSON and
stores via dual-write (S3 + DynamoDB).

Validates: Requirements 1.1, 1.2, 2.1, 2.2, 2.3, 2.4, 2.5,
           3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, 4.4, 4.5,
           5.1, 5.2, 5.3, 5.4, 5.5, 10.1, 11.2
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import boto3
from botocore.config import Config as BotoConfig

try:
    from shared.auth import SpringServeAuth
    from shared.normalizers import (
        normalize_supply_tag,
        normalize_demand_tag,
        normalize_report,
        normalize_delivery_modifier,
        normalize_creative,
        normalize_label,
        normalize_scheduled_report,
        normalize_correlation,
    )
except ImportError:
    from lambdas.pipeline_ads.shared.auth import SpringServeAuth
    from lambdas.pipeline_ads.shared.normalizers import (
        normalize_supply_tag,
        normalize_demand_tag,
        normalize_report,
        normalize_delivery_modifier,
        normalize_creative,
        normalize_label,
        normalize_scheduled_report,
        normalize_correlation,
    )

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# -------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------

BOTO_CONFIG = BotoConfig(
    max_pool_connections=10,
    retries={"max_attempts": 5, "mode": "adaptive"},
)

MAX_PER_PAGE = 1000

SPRINGSERVE_ENDPOINTS = {
    "supply_tags": "/api/v1/supply_tags",
    "demand_tags": "/api/v1/demand_tags",
    "delivery_modifiers": "/api/v1/delivery_modifiers",
    "creatives": "/api/v1/creatives",
    "supply_labels": "/api/v1/supply_labels",
    "demand_labels": "/api/v1/demand_labels",
    "scheduled_reports": "/api/v1/scheduled_reports",
    "reports": "/api/v1/reports",
    "auth": "/api/v1/auth",
}

WORKERS = int(os.environ.get("WORKERS", "5"))

# -------------------------------------------------------------------
# Environment variables
# -------------------------------------------------------------------

SPRINGSERVE_SECRET_NAME = os.environ.get(
    "SPRINGSERVE_SECRET_NAME", "springserve/api-credentials"
)
SPRINGSERVE_BASE_URL = os.environ.get(
    "SPRINGSERVE_BASE_URL", "https://video.springserve.com"
)
KB_ADS_BUCKET = os.environ.get("KB_ADS_BUCKET", "")
KB_ADS_PREFIX = os.environ.get("KB_ADS_PREFIX", "kb-ads/")
CONFIGS_TABLE_NAME = os.environ.get("CONFIGS_TABLE_NAME", "")
MEDIATAILOR_REGION = os.environ.get(
    "MEDIATAILOR_REGION", "us-east-1"
)

dynamodb_resource = boto3.resource(
    "dynamodb", config=BOTO_CONFIG
)


# -------------------------------------------------------------------
# Pagination helper
# -------------------------------------------------------------------


def _paginate_springserve(
    auth: SpringServeAuth,
    path: str,
    params: dict | None = None,
) -> list:
    """Generic paginator for SpringServe API endpoints.

    Iterates page=1,2,3… with per=MAX_PER_PAGE (1000),
    while current_page < total_pages.
    Returns a consolidated list of all results from every page.

    Validates: Requirements 2.1, 3.1, 5.1, 5.2, 5.3
    """
    all_results: list = []
    page = 1
    params = dict(params) if params else {}
    params["per"] = MAX_PER_PAGE

    while True:
        params["page"] = page
        resp = auth.request("GET", path, params=params)
        data = resp.json()
        all_results.extend(data.get("results", []))

        current_page = data.get("current_page", page)
        total_pages = data.get("total_pages", 1)

        if current_page >= total_pages:
            break
        page += 1

    return all_results


# -------------------------------------------------------------------
# Handler
# -------------------------------------------------------------------


def handler(event, context):
    """Entry point for Pipeline_Ads Lambda.

    Obtains SpringServe credentials from Secrets Manager,
    authenticates, runs supply_tags first (needed by correlations),
    then runs remaining collectors in parallel via ThreadPoolExecutor,
    and finally runs correlations and copies DOC_SPRINGSERVER.yml.

    Validates: Requirements 1.1, 1.2, 10.1, 10.2, 10.3, 10.5,
               11.2, 14.1
    """
    logger.info("Pipeline_Ads iniciado")

    mode = event.get("mode", "full") if isinstance(event, dict) else "full"
    logger.info("Modo de execução: %s", mode)

    # --- Boto3 clients ---
    s3 = boto3.client("s3", config=BOTO_CONFIG)
    ddb = dynamodb_resource.Table(CONFIGS_TABLE_NAME)

    results = {
        "stored": 0,
        "errors": [],
        "skipped_validation": 0,
    }

    # --- Reports-only mode: just POST /reports, fast ---
    if mode == "reports_only":
        logger.info("Modo reports_only: coletando apenas métricas")
        sm = boto3.client("secretsmanager", config=BOTO_CONFIG)
        secret_resp = sm.get_secret_value(
            SecretId=SPRINGSERVE_SECRET_NAME
        )
        creds = json.loads(secret_resp["SecretString"])
        auth = SpringServeAuth(
            SPRINGSERVE_BASE_URL,
            creds["email"],
            creds["password"],
        )
        auth.authenticate()
        _process_reports(auth, s3, ddb, results)
        total_stored = results["stored"]
        total_errors = len(results["errors"])
        logger.info(
            "reports_only finalizado: stored=%d, errors=%d",
            total_stored, total_errors,
        )
        return {
            "statusCode": 200,
            "body": {
                "mode": "reports_only",
                "total_stored": total_stored,
                "total_errors": total_errors,
            },
        }

    # --- Correlations-only mode ---
    if mode == "correlations_only":
        logger.info("Modo correlations_only: lendo supply tags do S3")
        supply_tags = _load_supply_tags_from_s3(s3)
        logger.info("Supply tags carregadas do S3: %d", len(supply_tags))

        mt_client = boto3.client(
            "mediatailor",
            region_name=MEDIATAILOR_REGION,
            config=BOTO_CONFIG,
        )
        _process_correlations(
            None, s3, ddb, mt_client, results, supply_tags
        )

        total_stored = results["stored"]
        total_errors = len(results["errors"])
        logger.info(
            "Correlations-only finalizado: stored=%d, errors=%d",
            total_stored, total_errors,
        )
        return {
            "statusCode": 200,
            "body": {
                "mode": "correlations_only",
                "total_stored": total_stored,
                "total_errors": total_errors,
                "supply_tags_loaded": len(supply_tags),
            },
        }

    # --- Credentials from Secrets Manager ---
    sm = boto3.client("secretsmanager", config=BOTO_CONFIG)
    secret_resp = sm.get_secret_value(
        SecretId=SPRINGSERVE_SECRET_NAME
    )
    creds = json.loads(secret_resp["SecretString"])
    email = creds["email"]
    password = creds["password"]

    # --- Authenticate with SpringServe ---
    auth = SpringServeAuth(
        SPRINGSERVE_BASE_URL, email, password
    )
    auth.authenticate()
    logger.info("SpringServe autenticado com sucesso")

    # --- Boto3 clients ---
    s3 = boto3.client("s3", config=BOTO_CONFIG)
    ddb = dynamodb_resource.Table(CONFIGS_TABLE_NAME)

    results = {
        "stored": 0,
        "errors": [],
        "skipped_validation": 0,
    }

    # --- Phase 1: supply_tags MUST complete first ---
    # (correlations depend on supply_tags data)
    supply_tags = _process_supply_tags(auth, s3, ddb, results)

    # --- Phase 2: remaining collectors in parallel ---
    parallel_fns = [
        ("demand_tags", _process_demand_tags),
        ("reports", _process_reports),
        ("scheduled_reports", _process_scheduled_reports),
        ("delivery_modifiers", _process_delivery_modifiers),
        ("creatives", _process_creatives),
        ("labels", _process_labels),
    ]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {}
        for name, fn in parallel_fns:
            future = pool.submit(fn, auth, s3, ddb, results)
            futures[future] = name

        for future in as_completed(futures):
            fn_name = futures[future]
            try:
                future.result()
            except Exception as exc:
                logger.error(
                    "Parallel task '%s' failed: %s",
                    fn_name,
                    exc,
                )
                _err(results, fn_name, str(exc))

    # --- Phase 3: correlations (after supply_tags) ---
    # If supply_tags in memory is empty/incomplete (e.g. due to
    # 429 rate limits during priorities fetch), fall back to S3
    if not supply_tags:
        logger.info(
            "supply_tags vazio em memória, carregando do S3"
        )
        supply_tags = _load_supply_tags_from_s3(s3)
        logger.info(
            "supply_tags carregadas do S3: %d", len(supply_tags)
        )

    mt_client = boto3.client(
        "mediatailor",
        region_name=MEDIATAILOR_REGION,
        config=BOTO_CONFIG,
    )
    _process_correlations(
        auth, s3, ddb, mt_client, results, supply_tags
    )

    # --- Phase 4: copy DOC_SPRINGSERVER.yml to S3 ---
    try:
        import os as _os
        doc_path = _os.path.join(
            _os.path.dirname(__file__),
            "DOC_SPRINGSERVER.yml",
        )
        with open(doc_path, "r") as f:
            doc_content = f.read()
        s3.put_object(
            Bucket=KB_ADS_BUCKET,
            Key=f"{KB_ADS_PREFIX}Documentacao/DOC_SPRINGSERVER.yml",
            Body=doc_content,
            ContentType="application/x-yaml",
        )
        logger.info("DOC_SPRINGSERVER.yml copiado para S3")
    except Exception as exc:
        logger.error(
            "Falha ao copiar DOC_SPRINGSERVER.yml: %s", exc
        )
        _err(results, "DOC_SPRINGSERVER.yml", str(exc))

    # --- Execution summary ---
    total_stored = results["stored"]
    total_errors = len(results["errors"])
    total_skipped = results["skipped_validation"]
    total_attempted = total_stored + total_errors + total_skipped

    # Invariant check
    if total_stored + total_errors + total_skipped != total_attempted:
        logger.error(
            "Invariant violation: stored(%d) + errors(%d) "
            "+ skipped(%d) != attempted(%d)",
            total_stored,
            total_errors,
            total_skipped,
            total_attempted,
        )

    logger.info(
        "Pipeline_Ads finalizado — resumo: "
        "stored=%d, errors=%d, correlations=%d, skipped=%d, "
        "total_attempted=%d",
        total_stored,
        total_errors,
        len([
            st for st in supply_tags
            if st.get("tipo") == "supply_tag"
        ]),
        total_skipped,
        total_attempted,
    )
    return {
        "statusCode": 200,
        "body": {
            "total_stored": total_stored,
            "total_errors": total_errors,
            "total_skipped": total_skipped,
            "total_attempted": total_attempted,
            "total_correlations": len(supply_tags),
            "errors": results["errors"],
        },
        "supply_tags": supply_tags,
    }


# -------------------------------------------------------------------
# Dual-write helpers (S3 + DynamoDB)
# -------------------------------------------------------------------


def _store_ad(s3, config):
    """Store a normalized ad config as JSON in S3.

    Builds the S3 key from the config's ``tipo`` and entity ID.
    Pattern: ``{KB_ADS_PREFIX}{category}/{tipo}_{id}.json``

    Validates: Requirements 2.4, 3.3, 4.3, 5.4
    """
    tipo = config.get("tipo", "unknown")
    entity_id = _extract_entity_id(config)
    category = _category_for_tipo(tipo)
    key = build_s3_key(tipo, entity_id, category)

    s3.put_object(
        Bucket=KB_ADS_BUCKET,
        Key=key,
        Body=json.dumps(
            config, ensure_ascii=False, default=str
        ),
        ContentType="application/json",
    )


def build_s3_key(tipo, entity_id, category=None):
    """Build the S3 object key for a given entity.

    Returns ``{KB_ADS_PREFIX}{category}/{tipo}_{id}.json``.

    Validates: Requirements 2.4, 3.3, 4.3, 5.4, 6.4
    """
    if category is None:
        category = _category_for_tipo(tipo)
    return f"{KB_ADS_PREFIX}{category}/{tipo}_{entity_id}.json"


def _category_for_tipo(tipo):
    """Return the S3 sub-folder category for a given tipo."""
    if tipo == "canal_springserve":
        return "Correlacao"
    return "SpringServe"


def _extract_entity_id(config):
    """Extract the numeric/string entity ID from a config."""
    tipo = config.get("tipo", "")
    if tipo == "supply_tag":
        return config.get("supply_tag_id", "")
    if tipo == "demand_tag":
        return config.get("demand_tag_id", "")
    if tipo == "report":
        sid = config.get("supply_tag_id", "")
        date = config.get(
            "data_fim",
            config.get(
                "data_inicio",
                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            ),
        )
        return f"{sid}_{date}"
    if tipo == "delivery_modifier":
        return config.get("modifier_id", "")
    if tipo == "creative":
        return config.get("creative_id", "")
    if tipo in ("supply_label", "demand_label"):
        return config.get("label_id", "")
    if tipo == "scheduled_report":
        return config.get("report_id", "")
    if tipo == "canal_springserve":
        return config.get("mediatailor_name", "")
    return config.get("channel_id", "")


def _write_ad_to_dynamodb(ddb, config):
    """Write a normalized ad config to DynamoDB. Fail-open.

    Uses PK = ``{servico}#{tipo}`` and SK derived from the
    entity name, following the same pattern as
    ``_write_config_to_dynamodb`` in pipeline_config.

    Validates: Requirements 2.5, 3.4, 4.4, 5.5
    """
    try:
        servico = config.get("servico", "Unknown")
        tipo = config.get("tipo", "unknown")
        sk = _build_dynamodb_sk(config)

        item = {
            "PK": f"{servico}#{tipo}",
            "SK": sk,
            "data": json.dumps(
                config, ensure_ascii=False, default=str
            ),
            "updated_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }

        # Promote scalar fields to top-level attributes
        for k, v in config.items():
            if k in ("PK", "SK", "data", "updated_at"):
                continue
            if v is None:
                continue
            if isinstance(v, str) and not v:
                continue
            if isinstance(v, bool):
                item[k] = v
            elif isinstance(v, (int, float)):
                from decimal import Decimal
                item[k] = Decimal(str(v))
            elif isinstance(v, str):
                item[k] = v

        ddb.put_item(Item=item)
    except Exception as exc:
        logger.error(
            "DynamoDB write failed (fail-open): %s", exc
        )


def _build_dynamodb_sk(config):
    """Build the DynamoDB sort key for a config."""
    tipo = config.get("tipo", "")
    if tipo == "report":
        name = config.get("supply_tag_name", "")
        date = config.get(
            "data_fim",
            config.get(
                "data_inicio",
                datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d"
                ),
            ),
        )
        return f"{name}#{date}"
    if tipo == "canal_springserve":
        # SK = mediatailor_name so Configuradora can look up by MT name
        return config.get(
            "mediatailor_name",
            str(config.get("channel_id", "")),
        )
    return config.get("nome", str(config.get("channel_id", "")))


def _dual_write(s3, ddb, config, results):
    """Store config in S3 and DynamoDB, updating results."""
    try:
        _store_ad(s3, config)
        results["stored"] += 1
    except Exception as exc:
        _err(results, config.get("tipo", "?"), str(exc))
        return
    try:
        _write_ad_to_dynamodb(ddb, config)
    except Exception as exc:
        logger.error(
            "DynamoDB dual-write error %s: %s",
            config.get("tipo", "?"),
            exc,
        )


def _err(results, resource, reason):
    """Log and record an error."""
    logger.error("Error %s: %s", resource, reason)
    results["errors"].append(
        {"resource": resource, "reason": reason}
    )


# -------------------------------------------------------------------
# Process functions
# -------------------------------------------------------------------


def _load_supply_tags_from_s3(s3):
    """Load previously stored supply tag JSONs from S3.

    Used by correlations_only mode to avoid re-fetching
    from SpringServe API.
    """
    supply_tags = []
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=KB_ADS_BUCKET,
            Prefix=f"{KB_ADS_PREFIX}SpringServe/supply_tag_",
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".json"):
                    continue
                try:
                    resp = s3.get_object(
                        Bucket=KB_ADS_BUCKET, Key=key,
                    )
                    data = json.loads(
                        resp["Body"].read().decode("utf-8")
                    )
                    supply_tags.append(data)
                except Exception as exc:
                    logger.warning(
                        "Failed to read %s: %s", key, exc
                    )
    except Exception as exc:
        logger.error(
            "Failed to list supply tags from S3: %s", exc
        )
    return supply_tags


def _process_supply_tags(auth, s3, ddb, results):
    """Collect, normalize and store all supply tags.

    For each supply tag, also fetches demand_tag_priorities
    in parallel using ThreadPoolExecutor for speed.
    Returns the list of normalized supply tags for correlation.

    Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5
    """
    logger.info("Coletando supply tags")
    normalized_tags = []
    try:
        raw_tags = _paginate_springserve(
            auth, SPRINGSERVE_ENDPOINTS["supply_tags"]
        )
    except Exception as exc:
        _err(results, "supply_tags", str(exc))
        return normalized_tags

    logger.info("Supply tags: %d encontradas, buscando priorities em paralelo", len(raw_tags))

    def _fetch_and_normalize(raw):
        tag_id = raw.get("id", "")
        try:
            prio_path = (
                f"{SPRINGSERVE_ENDPOINTS['supply_tags']}"
                f"/{tag_id}/demand_tag_priorities"
            )
            prio_resp = auth.request("GET", prio_path)
            priorities = prio_resp.json()
            if isinstance(priorities, dict):
                priorities = priorities.get("results", [])
        except Exception as exc:
            logger.warning(
                "Failed to get priorities for supply_tag "
                "%s: %s", tag_id, exc,
            )
            priorities = []
        return normalize_supply_tag(raw, priorities)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(_fetch_and_normalize, raw): raw
            for raw in raw_tags
        }
        for future in as_completed(futures):
            try:
                config = future.result()
                _dual_write(s3, ddb, config, results)
                normalized_tags.append(config)
            except Exception as exc:
                raw = futures[future]
                _err(
                    results,
                    f"supply_tag_{raw.get('id', '?')}",
                    str(exc),
                )

    logger.info(
        "Supply tags: %d coletadas", len(normalized_tags)
    )
    return normalized_tags


def _process_demand_tags(auth, s3, ddb, results):
    """Collect, normalize and store all demand tags.

    Validates: Requirements 3.1, 3.2, 3.3, 3.4
    """
    logger.info("Coletando demand tags")
    try:
        raw_tags = _paginate_springserve(
            auth, SPRINGSERVE_ENDPOINTS["demand_tags"]
        )
    except Exception as exc:
        _err(results, "demand_tags", str(exc))
        return

    for raw in raw_tags:
        try:
            config = normalize_demand_tag(raw)
            _dual_write(s3, ddb, config, results)
        except Exception as exc:
            _err(
                results,
                f"demand_tag_{raw.get('id', '?')}",
                str(exc),
            )

    logger.info("Demand tags: %d coletadas", len(raw_tags))


def _process_reports(auth, s3, ddb, results):
    """Generate and store yesterday's report metrics.

    Uses POST /api/v1/reports with synchronous execution.

    Validates: Requirements 4.1, 4.2, 4.3, 4.4
    """
    logger.info("Gerando relatórios de métricas")
    report_body = {
        "async": False,
        "date_range": "today",
        "dimensions": ["supply_tag_id"],
        "metrics": [
            "impressions",
            "revenue",
            "total_cost",
            "cpm",
            "fill_rate",
        ],
        "interval": "Cumulative",
        "timezone": "Etc/UTC",
        "csv": False,
    }
    try:
        resp = auth.request(
            "POST",
            SPRINGSERVE_ENDPOINTS["reports"],
            json=report_body,
        )
        data = resp.json()
        logger.info(
            "Reports API response keys: %s, type: %s",
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
            type(data).__name__,
        )
        # API returns "result" (without 's') or "results"
        rows = data.get("result", data.get("results", []))
        if isinstance(data, list):
            rows = data
    except Exception as exc:
        # Log response body if available
        try:
            body = exc.response.text[:500] if hasattr(exc, 'response') else ""
            logger.error("Reports API error body: %s", body)
        except Exception:
            pass
        _err(results, "reports", str(exc))
        return

    for raw in rows:
        try:
            config = normalize_report(raw)
            _dual_write(s3, ddb, config, results)
        except Exception as exc:
            _err(
                results,
                f"report_{raw.get('supply_tag_id', '?')}",
                str(exc),
            )

    logger.info("Reports: %d linhas processadas", len(rows))


def _process_scheduled_reports(auth, s3, ddb, results):
    """Collect, normalize and store scheduled reports.

    Validates: Requirements 4.5
    """
    logger.info("Coletando scheduled reports")
    try:
        raw_reports = _paginate_springserve(
            auth,
            SPRINGSERVE_ENDPOINTS["scheduled_reports"],
        )
    except Exception as exc:
        _err(results, "scheduled_reports", str(exc))
        return

    for raw in raw_reports:
        try:
            config = normalize_scheduled_report(raw)
            _dual_write(s3, ddb, config, results)
        except Exception as exc:
            _err(
                results,
                f"scheduled_report_{raw.get('id', '?')}",
                str(exc),
            )

    logger.info(
        "Scheduled reports: %d coletados", len(raw_reports)
    )


def _process_delivery_modifiers(auth, s3, ddb, results):
    """Collect, normalize and store delivery modifiers.

    Validates: Requirements 5.1, 5.4, 5.5
    """
    logger.info("Coletando delivery modifiers")
    try:
        raw_items = _paginate_springserve(
            auth,
            SPRINGSERVE_ENDPOINTS["delivery_modifiers"],
        )
    except Exception as exc:
        _err(results, "delivery_modifiers", str(exc))
        return

    for raw in raw_items:
        try:
            config = normalize_delivery_modifier(raw)
            _dual_write(s3, ddb, config, results)
        except Exception as exc:
            _err(
                results,
                f"delivery_modifier_{raw.get('id', '?')}",
                str(exc),
            )

    logger.info(
        "Delivery modifiers: %d coletados", len(raw_items)
    )


def _process_creatives(auth, s3, ddb, results):
    """Collect, normalize and store creatives.

    Validates: Requirements 5.2, 5.4, 5.5
    """
    logger.info("Coletando creatives")
    try:
        raw_items = _paginate_springserve(
            auth, SPRINGSERVE_ENDPOINTS["creatives"]
        )
    except Exception as exc:
        _err(results, "creatives", str(exc))
        return

    for raw in raw_items:
        try:
            config = normalize_creative(raw)
            _dual_write(s3, ddb, config, results)
        except Exception as exc:
            _err(
                results,
                f"creative_{raw.get('id', '?')}",
                str(exc),
            )

    logger.info("Creatives: %d coletados", len(raw_items))


def _process_labels(auth, s3, ddb, results):
    """Collect, normalize and store supply and demand labels.

    Fetches supply_labels and demand_labels separately.

    Validates: Requirements 5.3, 5.4, 5.5
    """
    logger.info("Coletando labels")

    # Supply labels
    try:
        raw_supply = _paginate_springserve(
            auth, SPRINGSERVE_ENDPOINTS["supply_labels"]
        )
    except Exception as exc:
        _err(results, "supply_labels", str(exc))
        raw_supply = []

    for raw in raw_supply:
        try:
            config = normalize_label(raw, "supply")
            _dual_write(s3, ddb, config, results)
        except Exception as exc:
            _err(
                results,
                f"supply_label_{raw.get('id', '?')}",
                str(exc),
            )

    # Demand labels
    try:
        raw_demand = _paginate_springserve(
            auth, SPRINGSERVE_ENDPOINTS["demand_labels"]
        )
    except Exception as exc:
        _err(results, "demand_labels", str(exc))
        raw_demand = []

    for raw in raw_demand:
        try:
            config = normalize_label(raw, "demand")
            _dual_write(s3, ddb, config, results)
        except Exception as exc:
            _err(
                results,
                f"demand_label_{raw.get('id', '?')}",
                str(exc),
            )

    total = len(raw_supply) + len(raw_demand)
    logger.info("Labels: %d coletados", total)


# -------------------------------------------------------------------
# URL parsing helper
# -------------------------------------------------------------------


def _extract_supply_tag_id_from_url(url: str) -> str | None:
    """Extract supply_tag_id from an ad decision server URL.

    Handles patterns like:
    - https://video.springserve.com/vast/{id}
    - https://tv-iad.springserve.com/rt/{id}?params
    - URLs with supply_tag_id as a query parameter

    Returns the supply_tag_id as a string, or None if not found.

    Validates: Requirements 6.2
    """
    if not url:
        return None

    parsed = urlparse(url)

    # Check query params for supply_tag_id
    qs = parse_qs(parsed.query)
    if "supply_tag_id" in qs:
        val = qs["supply_tag_id"][0]
        if val:
            return val

    # Check URL path for /vast/{id} or /rt/{id} pattern
    path = parsed.path.rstrip("/")
    match = re.search(r"/(?:vast|rt)/(\d+)", path)
    if match:
        return match.group(1)

    return None


# -------------------------------------------------------------------
# Correlation processing
# -------------------------------------------------------------------


def _process_correlations(
    auth, s3, ddb, mt_client, results, supply_tags
):
    """Correlate MediaTailor playback configs with SpringServe
    supply tags using multiple strategies:
    1. URL parsing (/vast/{id} or /rt/{id})
    2. Channel ID matching (extract numeric ID from MT name,
       find supply tag whose name contains that ID)
    3. Name substring matching

    Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6
    """
    logger.info("Processando correlações MediaTailor ↔ SpringServe")

    # Build lookups
    stag_by_id = {}
    stag_list = []
    for stag in supply_tags:
        sid = str(stag.get("supply_tag_id", ""))
        if sid:
            stag_by_id[sid] = stag
        stag_list.append(stag)

    # Paginate MediaTailor playback configurations
    mt_configs = []
    try:
        params = {"MaxResults": 100}
        while True:
            resp = mt_client.list_playback_configurations(
                **params,
            )
            mt_configs.extend(resp.get("Items", []))
            next_token = resp.get("NextToken")
            if not next_token:
                break
            params["NextToken"] = next_token
    except Exception as exc:
        _err(results, "mediatailor_correlations", str(exc))
        return

    correlated = 0
    skipped_non_springserve = 0
    for cfg in mt_configs:
        ad_url = cfg.get("AdDecisionServerUrl", "")
        mt_name = cfg.get("Name", "")

        # Skip non-SpringServe URLs (FreeWheel, etc.)
        if ad_url and "springserve" not in ad_url.lower():
            skipped_non_springserve += 1
            continue

        matched_tag = None

        # Strategy 1: URL parsing (/vast/{id} — direct supply_tag_id)
        tag_id = _extract_supply_tag_id_from_url(ad_url)
        if tag_id and tag_id in stag_by_id:
            matched_tag = stag_by_id[tag_id]

        # Strategy 2: Extract channel_name from URL query params
        # e.g. channel_name=ESPN%20ARGENTINA → find supply tag
        # containing "espn" AND "argentina" in its name
        if not matched_tag and ad_url:
            ch_name = _extract_channel_name_from_url(ad_url)
            if ch_name:
                matched_tag = _fuzzy_match_supply_tag(
                    ch_name, stag_list
                )

        # Strategy 3: MT config name substring match
        if not matched_tag and mt_name:
            # Remove "live_" prefix and numeric suffix for cleaner match
            clean_mt = re.sub(
                r"^live_\d+_?", "", mt_name, flags=re.IGNORECASE
            ).strip("_")
            if clean_mt:
                clean_lower = clean_mt.lower()
                for stag in stag_list:
                    sname = (stag.get("nome", "") or "").lower()
                    if clean_lower in sname:
                        matched_tag = stag
                        break

        if matched_tag:
            corr = normalize_correlation(
                cfg, matched_tag, None
            )
            _dual_write(s3, ddb, corr, results)
            correlated += 1
        else:
            logger.warning(
                "Correlação não encontrada para config "
                "'%s' com URL: %s",
                mt_name,
                ad_url,
            )

    logger.info(
        "Correlações: %d estabelecidas de %d configs "
        "(%d ignoradas — não-SpringServe)",
        correlated,
        len(mt_configs),
        skipped_non_springserve,
    )


def _extract_channel_id(mt_name):
    """Extract numeric channel ID from MediaTailor config name.

    Examples:
        "live_1010" → "1010"
        "live_1010_backup" → "1010"
        "channel_abc" → None
    """
    if not mt_name:
        return None
    match = re.search(r"(\d{3,})", mt_name)
    if match:
        return match.group(1)
    return None


def _extract_channel_name_from_url(url):
    """Extract channel_name from ad decision server URL query params.

    Handles URL-encoded values like channel_name=A%26E → "A&E".
    Returns None if not found.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "channel_name" in qs:
            return qs["channel_name"][0]
    except Exception:
        pass
    return None


def _fuzzy_match_supply_tag(channel_name, stag_list):
    """Find the best matching supply tag for a channel name.

    Splits channel_name into words and scores each supply tag
    by how many words match. Returns the best match or None.

    Example: "ESPN ARGENTINA" matches "ESPN Argentina - SSLA - Preroll"
    because both "espn" and "argentina" appear in the supply tag name.
    """
    if not channel_name:
        return None

    # Split into meaningful words (3+ chars)
    words = [
        w.lower() for w in re.split(r"[\s\-_&]+", channel_name)
        if len(w) >= 2
    ]
    if not words:
        return None

    best_match = None
    best_score = 0

    for stag in stag_list:
        sname = (stag.get("nome", "") or "").lower()
        if not sname:
            continue

        # Count how many channel_name words appear in supply tag name
        score = sum(1 for w in words if w in sname)

        if score > best_score:
            best_score = score
            best_match = stag

    # Require at least half the words to match
    min_required = max(1, len(words) // 2)
    if best_score >= min_required:
        return best_match

    return None
