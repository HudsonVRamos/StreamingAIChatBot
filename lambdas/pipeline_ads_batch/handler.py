"""Pipeline de Processamento em Batch — Supply Tags SpringServe.

Processa batches de supply tags enviados via SQS, fazendo chamadas
individuais para demand_tag_priorities e armazenando via dual-write.
Cada Lambda processa ~100 supply tags, evitando timeout.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config as BotoConfig

try:
    from shared.auth import SpringServeAuth
    from shared.normalizers import normalize_supply_tag
except ImportError:
    # Fallback for local development - should not happen in Lambda
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from shared.auth import SpringServeAuth
    from shared.normalizers import normalize_supply_tag

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# -------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------

BOTO_CONFIG = BotoConfig(
    max_pool_connections=10,
    retries={"max_attempts": 5, "mode": "adaptive"},
)

SPRINGSERVE_ENDPOINTS = {
    "supply_tags": "/api/v1/supply_tags",
}

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

dynamodb_resource = boto3.resource("dynamodb", config=BOTO_CONFIG)


def handler(event, context):
    """Entry point for Pipeline_Ads_Batch Lambda.

    Processes SQS messages containing batches of supply tags.
    Each message contains ~100 supply tags to process.
    """
    logger.info("Pipeline_Ads_Batch iniciado")

    # --- Boto3 clients ---
    s3 = boto3.client("s3", config=BOTO_CONFIG)
    ddb = dynamodb_resource.Table(CONFIGS_TABLE_NAME)

    results = {
        "stored": 0,
        "errors": [],
        "skipped_validation": 0,
    }

    # --- Credentials from Secrets Manager ---
    sm = boto3.client("secretsmanager", config=BOTO_CONFIG)
    secret_resp = sm.get_secret_value(SecretId=SPRINGSERVE_SECRET_NAME)
    creds = json.loads(secret_resp["SecretString"])
    
    # --- Authenticate with SpringServe ---
    auth = SpringServeAuth(
        SPRINGSERVE_BASE_URL, creds["email"], creds["password"]
    )
    auth.authenticate()
    logger.info("SpringServe autenticado com sucesso")

    # --- Process SQS records ---
    processed_batches = 0
    for record in event.get("Records", []):
        try:
            message_body = json.loads(record["body"])
            batch_id = message_body.get("batch_id", "unknown")
            supply_tags = message_body.get("supply_tags", [])
            total_batches = message_body.get("total_batches", 1)
            
            logger.info(
                "Processando %s: %d supply tags (batch %s de %d)",
                batch_id, len(supply_tags), batch_id, total_batches
            )
            
            _process_supply_tags_batch(auth, s3, ddb, supply_tags, results)
            processed_batches += 1
            
        except Exception as exc:
            logger.error("Falha ao processar SQS record: %s", exc)
            results["errors"].append(f"sqs_record: {str(exc)}")

    # --- Execution summary ---
    total_stored = results["stored"]
    total_errors = len(results["errors"])
    total_skipped = results["skipped_validation"]

    logger.info(
        "Pipeline_Ads_Batch finalizado — resumo: "
        "batches=%d, stored=%d, errors=%d, skipped=%d",
        processed_batches, total_stored, total_errors, total_skipped,
    )
    
    return {
        "statusCode": 200,
        "body": {
            "processed_batches": processed_batches,
            "total_stored": total_stored,
            "total_errors": total_errors,
            "total_skipped": total_skipped,
            "errors": results["errors"],
        },
    }


def _process_supply_tags_batch(auth, s3, ddb, raw_tags, results):
    """Process a batch of supply tags.

    Only fetches demand_tag_priorities for preroll/midroll/postroll
    tags (top 5 only). Skips for unknown types to save API calls.
    """
    from shared.normalizers import _detect_ad_position

    def _fetch_and_normalize(raw):
        tag_id = raw.get("id", "")
        name = raw.get("name", "")
        ad_pos = _detect_ad_position(name)

        priorities = []
        if ad_pos in ("preroll", "midroll", "postroll"):
            try:
                prio_path = (
                    f"{SPRINGSERVE_ENDPOINTS['supply_tags']}"
                    f"/{tag_id}/demand_tag_priorities"
                )
                prio_resp = auth.request(
                    "GET", prio_path,
                    params={"per": 5, "page": 1},
                )
                priorities = prio_resp.json()
                if isinstance(priorities, dict):
                    priorities = priorities.get("results", [])
            except Exception as exc:
                logger.warning(
                    "Failed to get priorities for supply_tag %s: %s",
                    tag_id, exc,
                )
        return normalize_supply_tag(raw, demand_priorities=priorities)

    # Process with 3 workers to respect rate limits
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(_fetch_and_normalize, raw): raw
            for raw in raw_tags
        }
        for future in as_completed(futures):
            try:
                config = future.result()
                _dual_write(s3, ddb, config, results)
            except Exception as exc:
                raw = futures[future]
                results["errors"].append(
                    f"supply_tag_{raw.get('id', '?')}: {str(exc)}"
                )


def _dual_write(s3, ddb, config, results):
    """Store config in both S3 and DynamoDB."""
    try:
        # S3
        tipo = config.get("tipo", "unknown")
        entity_id = config.get("supply_tag_id", "unknown")
        key = f"{KB_ADS_PREFIX}SpringServe/{tipo}_{entity_id}.json"
        
        s3.put_object(
            Bucket=KB_ADS_BUCKET,
            Key=key,
            Body=json.dumps(config, ensure_ascii=False, default=str),
            ContentType="application/json",
        )
        
        # DynamoDB
        pk = f"SpringServe#{tipo}"
        sk = config.get("nome", f"{tipo}_{entity_id}")
        
        ddb.put_item(
            Item={
                "PK": pk,
                "SK": sk,
                **config,
            }
        )
        
        results["stored"] += 1
        
    except Exception as exc:
        logger.error("Dual-write failed for %s: %s", config.get("channel_id"), exc)
        results["errors"].append(f"dual_write_{config.get('channel_id')}: {str(exc)}")