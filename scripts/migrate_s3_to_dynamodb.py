#!/usr/bin/env python3
"""One-time migration script: S3 → DynamoDB.

Reads existing JSON files from KB_CONFIG and KB_LOGS S3 buckets
and writes them to the corresponding DynamoDB tables using
batch_writer (handles 25-item batching automatically).

Usage:
    python scripts/migrate_s3_to_dynamodb.py
    python scripts/migrate_s3_to_dynamodb.py \
        --config-bucket my-kb-config \
        --logs-bucket my-kb-logs \
        --configs-table StreamingConfigs \
        --logs-table StreamingLogs

Environment variables (used as defaults):
    KB_CONFIG_BUCKET, KB_LOGS_BUCKET,
    CONFIGS_TABLE_NAME, LOGS_TABLE_NAME
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

CONFIG_PREFIX = "kb-config/"
LOGS_PREFIX = "kb-logs/"
TTL_DAYS = 30


# ── helpers ───────────────────────────────────────────────────────

def _list_s3_jsons(s3, bucket, prefix):
    """Yield all .json object keys under *prefix*."""
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".json"):
                yield key


def _is_within_last_n_days(key, days=30):
    """Return True if the filename contains a date within the last *days*.

    Log keys look like:
        kb-logs/MediaLive/CANAL_20240715T120000Z.json
    We extract the YYYYMMDD portion and compare.
    """
    match = re.search(r"(\d{4})(\d{2})(\d{2})T\d{6}Z\.json$", key)
    if not match:
        # If we can't parse a date, include the file to be safe
        return True
    file_date = datetime(
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        tzinfo=timezone.utc,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return file_date >= cutoff


def _read_s3_json(s3, bucket, key):
    """Read and parse a single JSON file from S3."""
    resp = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(resp["Body"].read().decode("utf-8"))


# ── transform helpers ─────────────────────────────────────────────

def transform_config(config, s3_key=""):
    """Transform a Config_Enriquecida dict into a DynamoDB item.

    Promotes ALL scalar fields to top-level DynamoDB attributes
    for better filtering. Uses S3 filename as SK for uniqueness.
    """
    servico = config.get("servico", "Unknown")
    tipo = config.get("tipo", "channel")
    if s3_key:
        fname = s3_key.rsplit("/", 1)[-1]
        sk = fname.replace(".json", "")
    else:
        sk = config.get("nome_canal", "") or str(
            config.get("channel_id", "")
        )
    item = {
        "PK": f"{servico}#{tipo}",
        "SK": sk,
        "data": json.dumps(
            config, ensure_ascii=False, default=str,
        ),
        "updated_at": datetime.now(
            timezone.utc,
        ).isoformat(),
    }
    # Promote all scalar fields
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
            item[k] = Decimal(str(v))
        elif isinstance(v, str):
            item[k] = v
    return item


def transform_log(evento):
    """Transform an Evento_Estruturado dict into a DynamoDB item.

    Mirrors _write_log_to_dynamodb in pipeline_logs/handler.py.
    """
    canal = evento.get("canal", "unknown")
    servico = evento.get("servico_origem", "Unknown")
    timestamp = evento.get("timestamp", "")
    metrica = evento.get("metrica_nome", "")
    metrica_valor = evento.get("metrica_valor", 0)
    ttl_epoch = int(
        (datetime.now(timezone.utc) + timedelta(days=TTL_DAYS)).timestamp()
    )
    return {
        "PK": f"{servico}#{canal}",
        "SK": f"{timestamp}#{metrica}",
        "severidade": evento.get("severidade", "INFO"),
        "tipo_erro": evento.get("tipo_erro", ""),
        "canal": canal,
        "servico_origem": servico,
        "metrica_nome": metrica,
        "metrica_valor": Decimal(str(metrica_valor)),
        "data": json.dumps(evento, ensure_ascii=False, default=str),
        "ttl": ttl_epoch,
    }


# ── migration logic ───────────────────────────────────────────────

def migrate_configs(s3, table, bucket):
    """Migrate all config JSONs from S3 to DynamoDB."""
    logger.info(
        "=== Migrating configs from s3://%s/%s ===",
        bucket, CONFIG_PREFIX,
    )
    count = 0
    errors = 0

    # Use overwrite_by_pkeys to handle duplicates gracefully
    with table.batch_writer(
        overwrite_by_pkeys=["PK", "SK"],
    ) as batch:
        for key in _list_s3_jsons(s3, bucket, CONFIG_PREFIX):
            try:
                config = _read_s3_json(s3, bucket, key)
                item = transform_config(config, key)
                batch.put_item(Item=item)
                count += 1
                if count % 100 == 0:
                    logger.info("  configs progress: %d items written", count)
            except Exception as exc:
                errors += 1
                logger.error("  Error migrating %s: %s", key, exc)

    logger.info(
        "=== Configs done: %d migrated, %d errors ===", count, errors,
    )
    return count, errors


def migrate_logs(s3, table, bucket):
    """Migrate log JSONs from the last 30 days from S3 to DynamoDB.

    Uses ThreadPoolExecutor for parallel S3 reads (20 threads).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    logger.info(
        "=== Migrating logs (last %d days) from s3://%s/%s ===",
        TTL_DAYS, bucket, LOGS_PREFIX,
    )

    # Collect keys first (with progress logging)
    keys = []
    skipped = 0
    total_listed = 0
    for key in _list_s3_jsons(s3, bucket, LOGS_PREFIX):
        total_listed += 1
        if total_listed % 5000 == 0:
            logger.info(
                "  listing keys: %d listed, %d kept, "
                "%d skipped so far...",
                total_listed, len(keys), skipped,
            )
        if not _is_within_last_n_days(key, TTL_DAYS):
            skipped += 1
            continue
        keys.append(key)

    logger.info(
        "  Found %d keys to migrate (%d skipped as old)",
        len(keys), skipped,
    )

    count = 0
    errors = 0

    def _read_and_transform(key):
        s3_local = boto3.client("s3")
        data = _read_s3_json(s3_local, bucket, key)
        return transform_log(data)

    with table.batch_writer() as batch:
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = {
                pool.submit(_read_and_transform, k): k
                for k in keys
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    item = future.result()
                    batch.put_item(Item=item)
                    count += 1
                    if count % 500 == 0:
                        logger.info(
                            "  logs progress: %d/%d",
                            count, len(keys),
                        )
                except Exception as exc:
                    errors += 1
                    logger.error(
                        "  Error migrating %s: %s",
                        key, exc,
                    )

    logger.info(
        "=== Logs done: %d migrated, %d skipped (old), "
        "%d errors ===",
        count, skipped, errors,
    )
    return count, errors


# ── CLI ───────────────────────────────────────────────────────────

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Migrate S3 JSON data to DynamoDB tables",
    )
    parser.add_argument(
        "--config-bucket",
        default=os.environ.get("KB_CONFIG_BUCKET", ""),
        help="S3 bucket for configs (default: $KB_CONFIG_BUCKET)",
    )
    parser.add_argument(
        "--logs-bucket",
        default=os.environ.get("KB_LOGS_BUCKET", ""),
        help="S3 bucket for logs (default: $KB_LOGS_BUCKET)",
    )
    parser.add_argument(
        "--configs-table",
        default=os.environ.get("CONFIGS_TABLE_NAME", "StreamingConfigs"),
        help="DynamoDB table for configs (default: StreamingConfigs)",
    )
    parser.add_argument(
        "--logs-table",
        default=os.environ.get("LOGS_TABLE_NAME", "StreamingLogs"),
        help="DynamoDB table for logs (default: StreamingLogs)",
    )
    parser.add_argument(
        "--skip-configs", action="store_true",
        help="Skip config migration",
    )
    parser.add_argument(
        "--skip-logs", action="store_true",
        help="Skip log migration",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if not args.config_bucket and not args.skip_configs:
        logger.error(
            "No config bucket. Use --config-bucket "
            "or set KB_CONFIG_BUCKET.",
        )
        sys.exit(1)
    if not args.logs_bucket and not args.skip_logs:
        logger.error(
            "No logs bucket. Use --logs-bucket "
            "or set KB_LOGS_BUCKET.",
        )
        sys.exit(1)

    s3 = boto3.client("s3")
    dynamodb = boto3.resource("dynamodb")

    total_migrated = 0
    total_errors = 0

    if not args.skip_configs:
        configs_table = dynamodb.Table(args.configs_table)
        migrated, errs = migrate_configs(s3, configs_table, args.config_bucket)
        total_migrated += migrated
        total_errors += errs

    if not args.skip_logs:
        logs_table = dynamodb.Table(args.logs_table)
        migrated, errs = migrate_logs(s3, logs_table, args.logs_bucket)
        total_migrated += migrated
        total_errors += errs

    logger.info(
        "Migration complete: %d total items, %d total errors",
        total_migrated, total_errors,
    )
    if total_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
