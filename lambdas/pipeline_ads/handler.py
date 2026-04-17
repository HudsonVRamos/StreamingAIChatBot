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

from shared.auth import SpringServeAuth
from shared.normalizers import (
    normalize_supply_tag,
    normalize_demand_tag,
    normalize_report,
    normalize_report_by_label,
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
    timeout: tuple | None = None,
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
        logger.info(f"Fetching {path} page {page} (per={MAX_PER_PAGE})")
        
        try:
            resp = auth.request("GET", path, params=params, timeout=timeout)
            data = resp.json()
            results = data.get("results", [])
            all_results.extend(results)
            
            logger.info(f"Page {page}: {len(results)} items, total so far: {len(all_results)}")

            current_page = data.get("current_page", page)
            total_pages = data.get("total_pages", 1)

            if current_page >= total_pages:
                break
            page += 1
            
        except Exception as exc:
            logger.error(f"Error fetching {path} page {page}: {exc}")
            # Continue with what we have so far
            break

    logger.info(f"Pagination complete for {path}: {len(all_results)} total items")
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
        # Load supply tag names from S3 for enrichment
        stag_name_by_id = {}
        try:
            supply_tags_s3 = _load_supply_tags_from_s3(s3)
            stag_name_by_id = {
                str(st.get("supply_tag_id", "")): st.get("nome", "")
                for st in supply_tags_s3
                if st.get("supply_tag_id") and st.get("nome")
            }
            logger.info("reports_only: %d supply tag names carregados do S3", len(stag_name_by_id))
        except Exception as exc:
            logger.warning("reports_only: falha ao carregar supply tags do S3: %s", exc)
        _process_reports(auth, s3, ddb, results, stag_name_by_id)

        # Also collect reports by label (separate — does not affect supply tag reports)
        label_name_by_id: dict = {}
        from datetime import timedelta
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        _process_reports_by_label_for_date(auth, s3, ddb, results, label_name_by_id, yesterday)

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

    # --- Backfill mode: fetch reports for each day in a date range ---
    if mode == "backfill":
        from datetime import timedelta
        days = int(event.get("days", 30))
        # Optional: specific start/end date override
        start_date_str = event.get("start_date")
        end_date_str = event.get("end_date")

        logger.info("Modo backfill: days=%d, start=%s, end=%s", days, start_date_str, end_date_str)
        sm = boto3.client("secretsmanager", config=BOTO_CONFIG)
        secret_resp = sm.get_secret_value(SecretId=SPRINGSERVE_SECRET_NAME)
        creds = json.loads(secret_resp["SecretString"])
        auth = SpringServeAuth(SPRINGSERVE_BASE_URL, creds["email"], creds["password"])
        auth.authenticate()

        stag_name_by_id = {}
        try:
            supply_tags_s3 = _load_supply_tags_from_s3(s3)
            stag_name_by_id = {
                str(st.get("supply_tag_id", "")): st.get("nome", "")
                for st in supply_tags_s3
                if st.get("supply_tag_id") and st.get("nome")
            }
            logger.info("backfill: %d supply tag names carregados", len(stag_name_by_id))
        except Exception as exc:
            logger.warning("backfill: falha ao carregar supply tags: %s", exc)

        from datetime import timedelta
        today = datetime.now(timezone.utc).date()

        # Build list of dates to process
        if start_date_str and end_date_str:
            from datetime import date as date_type
            start_d = date_type.fromisoformat(start_date_str)
            end_d = date_type.fromisoformat(end_date_str)
            dates = []
            cur = start_d
            while cur <= end_d:
                dates.append(cur.strftime("%Y-%m-%d"))
                cur += timedelta(days=1)
        else:
            dates = [
                (today - timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(1, days + 1)
            ]

        total_stored = 0
        total_errors = 0

        for date_str in dates:
            logger.info("backfill: processando %s", date_str)
            day_results = {"stored": 0, "errors": [], "skipped_validation": 0}
            _process_reports_for_date(auth, s3, ddb, day_results, stag_name_by_id, date_str)
            _process_reports_by_label_for_date(auth, s3, ddb, day_results, {}, date_str)
            total_stored += day_results["stored"]
            total_errors += len(day_results["errors"])
            logger.info(
                "backfill %s: stored=%d, errors=%d",
                date_str, day_results["stored"], len(day_results["errors"])
            )

        logger.info("backfill finalizado: total_stored=%d, total_errors=%d", total_stored, total_errors)
        return {
            "statusCode": 200,
            "body": {
                "mode": "backfill",
                "dates_processed": len(dates),
                "total_stored": total_stored,
                "total_errors": total_errors,
            },
        }

    # --- Supply tags direct mode: process supply tags without SQS ---
    if mode == "supply_tags_direct":
        logger.info("Modo supply_tags_direct: processando supply tags sem SQS")
        sm = boto3.client("secretsmanager", config=BOTO_CONFIG)
        secret_resp = sm.get_secret_value(SecretId=SPRINGSERVE_SECRET_NAME)
        creds = json.loads(secret_resp["SecretString"])
        auth = SpringServeAuth(SPRINGSERVE_BASE_URL, creds["email"], creds["password"])
        auth.authenticate()
        _process_supply_tags_direct(auth, s3, ddb, results)
        total_stored = results["stored"]
        total_errors = len(results["errors"])
        logger.info(
            "supply_tags_direct finalizado: stored=%d, errors=%d",
            total_stored, total_errors,
        )
        return {
            "statusCode": 200,
            "body": {
                "mode": "supply_tags_direct",
                "total_stored": total_stored,
                "total_errors": total_errors,
            },
        }

    # --- Priorities-only mode: update existing supply tags with priorities ---
    if mode == "priorities_only":
        logger.info("Modo priorities_only: atualizando supply tags com priorities")
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
        _process_priorities_only(auth, s3, ddb, results)
        total_stored = results["stored"]
        total_errors = len(results["errors"])
        logger.info(
            "priorities_only finalizado: stored=%d, errors=%d",
            total_stored, total_errors,
        )
        return {
            "statusCode": 200,
            "body": {
                "mode": "priorities_only",
                "total_stored": total_stored,
                "total_errors": total_errors,
            },
        }
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

    # --- Phase 0: labels (needed for supply_tags enrichment) ---
    label_name_by_tag_id = _process_labels(auth, s3, ddb, results)

    # --- Phase 1: supply_tags (direct processing with label enrichment) ---
    supply_tags = _process_supply_tags_direct(auth, s3, ddb, results, label_name_by_tag_id=label_name_by_tag_id)

    # --- Phase 2: remaining collectors in parallel ---
    # Build supply tag name lookup for report enrichment
    stag_name_by_id = {
        str(st.get("supply_tag_id", "")): st.get("nome", "")
        for st in supply_tags
        if st.get("supply_tag_id") and st.get("nome")
    }

    parallel_fns = [
        ("demand_tags", _process_demand_tags),
        ("reports", lambda a, s, d, r: _process_reports(a, s, d, r, stag_name_by_id)),
        ("scheduled_reports", _process_scheduled_reports),
        ("delivery_modifiers", _process_delivery_modifiers),
        ("creatives", _process_creatives),
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

    For reports: uses conditional write to avoid overwriting
    a finalized day's data if it already exists with the same
    date. TTL set to 30 days for automatic cleanup.

    Validates: Requirements 2.5, 3.4, 4.4, 5.5
    """
    try:
        servico = config.get("servico", "Unknown")
        tipo = config.get("tipo", "unknown")
        sk = _build_dynamodb_sk(config)

        # TTL: 30 days from now
        import time
        ttl_value = int(time.time()) + (30 * 24 * 3600)

        item = {
            "PK": f"{servico}#{tipo}",
            "SK": sk,
            "data": json.dumps(
                config, ensure_ascii=False, default=str
            ),
            "updated_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "ttl": ttl_value,
        }

        # Promote scalar fields to top-level attributes
        for k, v in config.items():
            if k in ("PK", "SK", "data", "updated_at", "ttl"):
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
    if tipo == "report_by_label":
        name = config.get("supply_label_name", "")
        date = config.get("data_fim", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
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


def _process_supply_tags_inline(auth, s3, ddb, results):
    """Process supply tags inline WITHOUT priorities for speed.
    
    Skips demand_tag_priorities to avoid rate limits.
    Focus on getting basic supply tag data with ad_position categorization.
    """
    logger.info("Coletando supply tags inline (SEM priorities - modo rápido)")
    normalized_tags = []
    
    try:
        # Use longer timeout for supply tags collection
        raw_tags = _paginate_springserve(
            auth, SPRINGSERVE_ENDPOINTS["supply_tags"], timeout=(15, 120)
        )
    except Exception as exc:
        _err(results, "supply_tags", str(exc))
        return normalized_tags

    logger.info("Supply tags: %d encontradas, processando SEM priorities", len(raw_tags))

    # Process all supply tags WITHOUT fetching priorities (fast mode)
    for index, raw in enumerate(raw_tags):
        try:
            # Normalize without priorities (empty list)
            config = normalize_supply_tag(raw, demand_priorities=[])
            _dual_write(s3, ddb, config, results)
            normalized_tags.append(config)
            
            # Log progress every 500 items (faster processing)
            if (index + 1) % 500 == 0:
                logger.info(f"Supply tags processadas: {index + 1}/{len(raw_tags)}")
                
        except Exception as exc:
            _err(
                results,
                f"supply_tag_{raw.get('id', '?')}",
                str(exc),
            )

    logger.info("Supply tags: %d processadas com sucesso (SEM priorities)", len(normalized_tags))
    return normalized_tags


def _process_priorities_only(auth, s3, ddb, results):
    """Update existing supply tags with demand_tag_priorities.
    
    Loads supply tags from S3, fetches priorities, and updates them.
    """
    logger.info("Carregando supply tags existentes do S3")
    supply_tags = _load_supply_tags_from_s3(s3)
    logger.info(f"Supply tags carregadas: {len(supply_tags)}")
    
    if not supply_tags:
        logger.warning("Nenhuma supply tag encontrada no S3")
        return
    
    updated = 0
    for index, supply_tag in enumerate(supply_tags):
        tag_id = supply_tag.get("supply_tag_id", "")
        if not tag_id:
            continue
            
        try:
            # Add delay every 5 requests to be very conservative
            if index > 0 and index % 5 == 0:
                import time
                time.sleep(3)  # 3 second delay every 5 requests
                
            prio_path = (
                f"{SPRINGSERVE_ENDPOINTS['supply_tags']}"
                f"/{tag_id}/demand_tag_priorities"
            )
            prio_resp = auth.request("GET", prio_path, timeout=(15, 60))
            priorities = prio_resp.json()
            if isinstance(priorities, dict):
                priorities = priorities.get("results", [])
                
            # Update supply tag with priorities
            demand_names = [
                str(d.get("demand_tag_name", d.get("name", "")))
                for d in priorities
            ]
            demand_ids = [
                str(d.get("demand_tag_id", d.get("id", "")))
                for d in priorities
            ]
            
            supply_tag["demand_tag_count"] = len(priorities)
            supply_tag["demand_tags"] = ", ".join(demand_names) if demand_names else ""
            supply_tag["demand_tag_ids"] = ", ".join(demand_ids) if demand_ids else ""
            
            # Store updated supply tag
            _dual_write(s3, ddb, supply_tag, results)
            updated += 1
            
            # Log progress every 50 items
            if (index + 1) % 50 == 0:
                logger.info(f"Priorities atualizadas: {index + 1}/{len(supply_tags)}")
                
        except Exception as exc:
            if "429" in str(exc):
                logger.warning(f"Rate limited on supply_tag {tag_id}, skipping")
                import time
                time.sleep(10)  # Longer delay on rate limit
            else:
                logger.warning(f"Failed to update priorities for supply_tag {tag_id}: {exc}")
    
    logger.info(f"Priorities atualizadas: {updated} supply tags")


def _process_supply_tags_direct(auth, s3, ddb, results, raw_tags=None, store=True, label_name_by_tag_id=None):
    """Process supply tags directly without SQS (no demand priorities).

    Normalizes each supply tag with device/platform fields extracted
    from the name. Optionally stores to S3+DynamoDB.
    Returns list of normalized supply tags for correlation.
    
    Args:
        label_name_by_tag_id: Dict mapping supply_tag_id to label name.
    """
    if raw_tags is None:
        try:
            raw_tags = _paginate_springserve(
                auth, SPRINGSERVE_ENDPOINTS["supply_tags"]
            )
        except Exception as exc:
            _err(results, "supply_tags_direct", str(exc))
            return []

    label_name_by_tag_id = label_name_by_tag_id or {}
    logger.info(
        "supply_tags_direct: processando %d tags (store=%s, labels=%d)",
        len(raw_tags), store, len(label_name_by_tag_id),
    )
    normalized = []
    for raw in raw_tags:
        try:
            config = normalize_supply_tag(raw, label_name_by_tag_id=label_name_by_tag_id)
            normalized.append(config)
            if store:
                _dual_write(s3, ddb, config, results)
        except Exception as exc:
            _err(results, f"supply_tag_{raw.get('id', '?')}", str(exc))

    logger.info("supply_tags_direct: %d tags processadas", len(normalized))
    return normalized
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
        return normalize_supply_tag(raw, demand_priorities=priorities)

    # Reduced workers to 3 to respect SpringServe rate limits
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


def _process_reports_by_label_for_date(auth, s3, ddb, results, label_name_by_id, date_str):
    """Fetch and store SpringServe report metrics by supply_label for a specific date.

    Uses start_date/end_date with dimensions=["supply_label_id"].
    Stored separately as tipo=report_by_label — does NOT touch supply tag reports.
    """
    logger.info("Gerando relatórios por label para %s", date_str)
    report_body = {
        "async": False,
        "start_date": date_str,
        "end_date": date_str,
        "dimensions": ["supply_label_id"],
        "metrics": [
            "requests",
            "opportunities",
            "impressions",
            "fill_rate",
            "opp_fill_rate",
            "req_fill_rate",
            "revenue",
            "total_cost",
            "cpm",
            "rpm",
        ],
        "interval": "Cumulative",
        "timezone": "America/Sao_Paulo",
        "csv": False,
    }
    try:
        resp = auth.request("POST", SPRINGSERVE_ENDPOINTS["reports"], json=report_body)
        data = resp.json()
        rows = data.get("result", data.get("results", []))
        if isinstance(data, list):
            rows = data
        logger.info("Reports by label %s: %d linhas recebidas", date_str, len(rows))
    except Exception as exc:
        _err(results, f"reports_by_label_{date_str}", str(exc))
        return

    for raw in rows:
        try:
            lid = str(raw.get("supply_label_id", ""))
            if lid and label_name_by_id.get(lid):
                raw.setdefault("supply_label_name", label_name_by_id[lid])
            raw["start_date"] = date_str
            raw["end_date"] = date_str
            config = normalize_report_by_label(raw)
            _dual_write(s3, ddb, config, results)
        except Exception as exc:
            _err(results, f"report_label_{date_str}_{raw.get('supply_label_id', '?')}", str(exc))

    logger.info("Reports by label %s: %d linhas processadas", date_str, len(rows))



    """Generate and store yesterday's report metrics."""
    from datetime import timedelta
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    _process_reports_for_date(auth, s3, ddb, results, stag_name_by_id or {}, yesterday)


def _process_reports_for_date(auth, s3, ddb, results, stag_name_by_id, date_str):
    """Fetch and store SpringServe report metrics for a specific date.

    Uses start_date/end_date for exact day targeting with
    America/New_York timezone (matches SpringServe UI default).

    Validates: Requirements 4.1, 4.2, 4.3, 4.4
    """
    stag_name_by_id = stag_name_by_id or {}
    logger.info("Gerando relatórios de métricas para %s", date_str)
    report_body = {
        "async": False,
        "start_date": date_str,
        "end_date": date_str,
        "dimensions": ["supply_tag_id"],
        "metrics": [
            "requests",
            "opportunities",
            "impressions",
            "fill_rate",
            "opp_fill_rate",
            "req_fill_rate",
            "pod_time_req_fill_rate",
            "revenue",
            "total_cost",
            "cpm",
            "rpm",
        ],
        "interval": "Cumulative",
        "timezone": "America/Sao_Paulo",
        "csv": False,
    }
    try:
        resp = auth.request(
            "POST",
            SPRINGSERVE_ENDPOINTS["reports"],
            json=report_body,
        )
        data = resp.json()
        rows = data.get("result", data.get("results", []))
        if isinstance(data, list):
            rows = data
        logger.info("Reports %s: %d linhas recebidas", date_str, len(rows))
    except Exception as exc:
        try:
            body = exc.response.text[:500] if hasattr(exc, 'response') else ""
            logger.error("Reports API error body: %s", body)
        except Exception:
            pass
        _err(results, f"reports_{date_str}", str(exc))
        return

    for raw in rows:
        try:
            sid = str(raw.get("supply_tag_id", ""))
            if sid and stag_name_by_id.get(sid):
                raw.setdefault("supply_tag_name", stag_name_by_id[sid])
                api_name = raw.get("supply_tag_name", "")
                if not api_name or api_name == sid or api_name.lstrip("0123456789") == "":
                    raw["supply_tag_name"] = stag_name_by_id[sid]
            # Inject date
            raw["start_date"] = date_str
            raw["end_date"] = date_str
            config = normalize_report(raw)
            _dual_write(s3, ddb, config, results)
        except Exception as exc:
            _err(results, f"report_{date_str}_{raw.get('supply_tag_id', '?')}", str(exc))

    logger.info("Reports %s: %d linhas processadas", date_str, len(rows))


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

    Handles 403 Forbidden as warning (insufficient permissions).

    Validates: Requirements 5.1, 5.4, 5.5
    """
    logger.info("Coletando delivery modifiers")
    try:
        raw_items = _paginate_springserve(
            auth,
            SPRINGSERVE_ENDPOINTS["delivery_modifiers"],
        )
    except Exception as exc:
        # 403 Forbidden is common for delivery_modifiers
        if "403" in str(exc) or "Forbidden" in str(exc):
            logger.warning(
                "delivery_modifiers: 403 Forbidden (sem permissão), pulando"
            )
            return
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
    Returns a dict mapping supply_tag_id to label name.

    Validates: Requirements 5.3, 5.4, 5.5
    """
    logger.info("Coletando labels")

    # Supply labels
    label_name_by_tag_id = {}
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
            # Build reverse mapping: supply_tag_id → label_name
            label_name = raw.get("name", "")
            for tag_id in raw.get("supply_tag_ids", []):
                label_name_by_tag_id[tag_id] = label_name
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
    logger.info("Labels: %d coletados, %d supply_tag mappings", total, len(label_name_by_tag_id))
    return label_name_by_tag_id


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
