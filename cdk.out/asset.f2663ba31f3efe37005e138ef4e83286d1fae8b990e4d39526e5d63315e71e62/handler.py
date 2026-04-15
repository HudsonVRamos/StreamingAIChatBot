"""Pipeline de Ingestão de Configurações — parallelized.

Extracts configs from MediaLive, MediaPackage V2, MediaTailor, CloudFront.
Uses ThreadPoolExecutor for parallel API calls within each service.
"""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3
from botocore.config import Config as BotoConfig

from shared.normalizers import (
    normalize_cloudfront_config,
    normalize_medialive_config,
    normalize_mediapackage_config,
    normalize_mediatailor_config,
)
from shared.validators import (
    detect_cross_contamination,
    validate_config_enriquecida,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BOTO_CONFIG = BotoConfig(
    max_pool_connections=10,
    retries={"max_attempts": 5, "mode": "adaptive"},
)
WORKERS = 5

dynamodb_resource = boto3.resource("dynamodb", config=BOTO_CONFIG)


def _get_bucket():
    return os.environ.get("KB_CONFIG_BUCKET", "")


def _get_prefix():
    return os.environ.get("KB_CONFIG_PREFIX", "kb-config/")


def handler(event, context):
    logger.info("Pipeline de configurações iniciado")

    streaming_region = os.environ.get("STREAMING_REGION", "sa-east-1")
    mediatailor_region = os.environ.get("MEDIATAILOR_REGION", "us-east-1")

    s3 = boto3.client("s3")
    ml = boto3.client("medialive", region_name=streaming_region, config=BOTO_CONFIG)
    mpv2 = boto3.client("mediapackagev2", region_name=streaming_region, config=BOTO_CONFIG)
    mt = boto3.client("mediatailor", region_name=mediatailor_region, config=BOTO_CONFIG)
    cf = boto3.client("cloudfront", config=BOTO_CONFIG)

    results = {"stored": 0, "errors": [], "skipped_validation": 0, "skipped_contamination": 0}

    _process_medialive(ml, s3, results)
    _process_mediapackagev2(mpv2, s3, results)
    _process_mediatailor(mt, s3, results)
    _process_cloudfront(cf, s3, results)

    logger.info("Finalizado: %d stored, %d errors", results["stored"], len(results["errors"]))
    return {"statusCode": 200, "body": results}


# ===================================================================
# MediaLive — parallel describe_channel
# ===================================================================

def _process_medialive(ml, s3, results):
    try:
        channels = _paginate(ml.list_channels, "Channels", MaxResults=100)
    except Exception as e:
        _err(results, "MediaLive", "list", str(e)); return

    def _do(ch):
        cid = ch.get("Id", "?")
        try:
            detail = ml.describe_channel(ChannelId=cid)
            detail.pop("ResponseMetadata", None)
            return normalize_medialive_config(detail), cid
        except Exception as e:
            return e, cid

    _parallel_store(s3, results, "MediaLive", channels, _do)


# ===================================================================
# MediaPackage V2 — get channel + get each endpoint detail
# ===================================================================

def _process_mediapackagev2(mpv2, s3, results):
    try:
        groups = _paginate(mpv2.list_channel_groups, "Items", MaxResults=100)
    except Exception as e:
        _err(results, "MediaPackage", "list_groups", str(e)); return

    # Collect all (group, channel, endpoint) tasks
    tasks = []
    for g in groups:
        gn = g.get("ChannelGroupName", "?")
        try:
            channels = _paginate(mpv2.list_channels, "Items", ChannelGroupName=gn, MaxResults=100)
            for ch in channels:
                cn = ch.get("ChannelName", "?")
                # Save channel itself
                tasks.append(("channel", gn, cn, None))
                # List endpoints for this channel
                try:
                    eps = _paginate(
                        mpv2.list_origin_endpoints, "Items",
                        ChannelGroupName=gn, ChannelName=cn, MaxResults=100
                    )
                    for ep in eps:
                        tasks.append(("endpoint", gn, cn, ep.get("OriginEndpointName", "?")))
                except Exception as e:
                    _err(results, "MediaPackage", f"{gn}/{cn}/endpoints", str(e))
        except Exception as e:
            _err(results, "MediaPackage", gn, str(e))

    def _do(task):
        task_type, gn, cn, ep_name = task
        try:
            if task_type == "channel":
                detail = mpv2.get_channel(ChannelGroupName=gn, ChannelName=cn)
                detail.pop("ResponseMetadata", None)
                detail["ChannelGroupName"] = gn
                detail["OriginEndpoints"] = []  # channel-only, no endpoints
                normalized = normalize_mediapackage_config(detail)
                return normalized, cn
            else:
                # Get full endpoint detail
                ep_detail = mpv2.get_origin_endpoint(
                    ChannelGroupName=gn, ChannelName=cn, OriginEndpointName=ep_name
                )
                ep_detail.pop("ResponseMetadata", None)
                # Build a config with channel info + this endpoint
                config = {
                    "ChannelGroupName": gn,
                    "ChannelName": cn,
                    "OriginEndpoints": [ep_detail],
                }
                normalized = normalize_mediapackage_config(config)
                # Override nome_canal to include endpoint name
                if ep_name.startswith(cn):
                    normalized["nome_canal"] = ep_name
                else:
                    normalized["nome_canal"] = f"{cn}_{ep_name}"
                # S3 key: just the endpoint name
                return normalized, ep_name
        except Exception as e:
            rid = f"{gn}_{cn}_{ep_name}" if ep_name else f"{gn}_{cn}"
            return e, rid

    _parallel_store(s3, results, "MediaPackage", tasks, _do)


# ===================================================================
# MediaTailor — parallel get_playback_configuration
# ===================================================================

def _process_mediatailor(mt, s3, results):
    try:
        configs = _paginate(mt.list_playback_configurations, "Items", MaxResults=100)
    except Exception as e:
        _err(results, "MediaTailor", "list", str(e)); return

    def _do(cfg):
        name = cfg.get("Name", "?")
        try:
            detail = mt.get_playback_configuration(Name=name)
            detail.pop("ResponseMetadata", None)
            return normalize_mediatailor_config(detail), name
        except Exception as e:
            return e, name

    _parallel_store(s3, results, "MediaTailor", configs, _do)


# ===================================================================
# CloudFront — parallel get_distribution
# ===================================================================

def _process_cloudfront(cf, s3, results):
    try:
        dists = _paginate_cf(cf)
    except Exception as e:
        _err(results, "CloudFront", "list", str(e)); return

    def _do(d):
        did = d.get("Id", "?")
        try:
            detail = cf.get_distribution(Id=did)
            detail.pop("ResponseMetadata", None)
            return normalize_cloudfront_config(detail), did
        except Exception as e:
            return e, did

    _parallel_store(s3, results, "CloudFront", dists, _do)


# ===================================================================
# Shared helpers
# ===================================================================

def _write_config_to_dynamodb(config):
    """Write config record to DynamoDB StreamingConfigs. Fail-open."""
    table_name = os.environ.get("CONFIGS_TABLE_NAME", "")
    if not table_name:
        return
    try:
        table = dynamodb_resource.Table(table_name)
        servico = config.get("servico", "Unknown")
        tipo = config.get("tipo", "channel")
        sk = config.get("nome_canal", "") or str(
            config.get("channel_id", "")
        )
        # Promote all flat fields to top-level DynamoDB
        # attributes for better filtering
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
        # Add all scalar fields from the config
        for k, v in config.items():
            if k in ("PK", "SK", "data", "updated_at"):
                continue
            if v is None:
                continue
            # DynamoDB doesn't accept empty strings
            if isinstance(v, str) and not v:
                continue
            # Convert bools to DynamoDB-friendly format
            if isinstance(v, bool):
                item[k] = v
            elif isinstance(v, (int, float)):
                from decimal import Decimal
                item[k] = Decimal(str(v))
            elif isinstance(v, str):
                item[k] = v
            # Skip complex types (lists, dicts)
        table.put_item(Item=item)
    except Exception as exc:
        logger.error(
            "DynamoDB write failed (fail-open): %s", exc
        )


def _parallel_store(s3, results, service, items, fn):
    """Run fn in parallel for each item, validate and store results."""
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fn, item): item for item in items}
        for future in as_completed(futures):
            result, rid = future.result()
            if isinstance(result, Exception):
                _err(results, service, rid, str(result))
                continue
            # Validate
            v = validate_config_enriquecida(result)
            if not v.is_valid:
                logger.warning("Validation failed %s/%s: %s", service, rid, v.errors)
                results["skipped_validation"] += 1
                continue
            # Contamination check
            c = detect_cross_contamination(result, "kb-config")
            if c.is_contaminated:
                results["skipped_contamination"] += 1
                continue
            # Store
            _store(s3, result, service, rid)
            results["stored"] += 1
            # Dual-write to DynamoDB (fail-open)
            try:
                _write_config_to_dynamodb(result)
            except Exception as exc:
                logger.error(
                    "DynamoDB dual-write error %s/%s: %s",
                    service, rid, exc,
                )


def _store(s3, config, service, rid):
    safe = str(rid).replace("/", "_")
    bucket = _get_bucket()
    prefix = _get_prefix()
    key = f"{prefix}{service}/{safe}.json"
    s3.put_object(
        Bucket=bucket, Key=key,
        Body=json.dumps(config, ensure_ascii=False, default=str),
        ContentType="application/json",
    )


def _err(results, service, rid, reason):
    logger.error("Error %s/%s: %s", service, rid, reason)
    results["errors"].append({"service": service, "resource_id": rid, "reason": reason})


def _paginate(method, key, **kwargs):
    """Generic paginator for APIs that use NextToken."""
    items = []
    while True:
        resp = method(**kwargs)
        items.extend(resp.get(key, []))
        nt = resp.get("NextToken")
        if not nt:
            break
        kwargs["NextToken"] = nt
    return items


def _paginate_cf(cf):
    """CloudFront uses Marker/IsTruncated instead of NextToken."""
    items = []
    marker = ""
    while True:
        params = {"MaxItems": "100"}
        if marker:
            params["Marker"] = marker
        resp = cf.list_distributions(**params)
        dl = resp.get("DistributionList", {})
        items.extend(dl.get("Items") or [])
        if dl.get("IsTruncated") and dl.get("NextMarker"):
            marker = dl["NextMarker"]
        else:
            break
    return items
