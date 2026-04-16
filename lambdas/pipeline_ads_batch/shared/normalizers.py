"""Normalization module for SpringServe ad data — flat JSON output.

Each normalizer produces a flat dict (no nesting) with all relevant
fields extracted from the raw SpringServe API response.
Follows the same pattern as pipeline_config/shared/normalizers.py.

Validates: Requirements 2.2, 3.2, 4.2, 5.1, 5.2, 5.3, 6.3,
           9.1, 9.2, 9.3, 9.4
"""

from __future__ import annotations

from typing import Any, Dict


def _detect_ad_position(name: str) -> str:
    """Detect ad position (preroll/midroll/postroll) from supply tag name.

    Matches case-insensitively against the tag name suffix or any word.
    Returns 'preroll', 'midroll', 'postroll', or 'unknown'.
    """
    lower = name.lower()
    if "preroll" in lower or "pre-roll" in lower or "pre_roll" in lower:
        return "preroll"
    if "midroll" in lower or "mid-roll" in lower or "mid_roll" in lower:
        return "midroll"
    if "postroll" in lower or "post-roll" in lower or "post_roll" in lower:
        return "postroll"
    return "unknown"


_KNOWN_DEVICES = {
    "android_tv", "android_mobile", "android_tablet",
    "samsung_tv", "fire_tv", "apple_tv", "roku", "lg_tv",
    "ios", "iphone", "ipad", "chromecast", "xbox", "playstation",
    "smart_tv", "desktop", "mobile", "tablet",
}
_KNOWN_PLATFORMS = {"ctv", "app", "web", "stb", "ott"}


def _parse_supply_tag_name(name: str) -> dict:
    """Parse canal_nome, platform, device from supply tag name.

    Pattern: "Canal - Platform - Device - AdPosition"
    """
    parts = [p.strip() for p in name.split(" - ")]
    canal_nome = parts[0] if parts else ""
    platform = ""
    device = ""

    for part in parts[1:]:
        lower = part.lower().replace(" ", "_")
        if lower in _KNOWN_PLATFORMS:
            platform = lower
        elif lower in _KNOWN_DEVICES:
            device = lower
        else:
            for kd in _KNOWN_DEVICES:
                if kd in lower:
                    device = kd
                    break

    return {"canal_nome": canal_nome, "platform": platform, "device": device}


def normalize_supply_tag(
    raw: dict,
    demand_priorities: list | None = None,
) -> Dict[str, Any]:
    """Normalize a raw SpringServe supply tag to flat JSON.

    Args:
        raw: Raw supply tag dict from the API.
        demand_priorities: List of demand_tag_priority dicts
            from GET /supply_tags/{id}/demand_tag_priorities.
    """
    supply_id = raw.get("id", "")
    demand_priorities = demand_priorities or []
    name = raw.get("name", "")

    demand_names = [
        str(d.get("demand_tag_name", d.get("name", "")))
        for d in demand_priorities
    ]
    demand_ids = [
        str(d.get("demand_tag_id", d.get("id", "")))
        for d in demand_priorities
    ]

    parsed = _parse_supply_tag_name(name)

    return {
        "channel_id": f"supply_tag_{supply_id}",
        "servico": "SpringServe",
        "tipo": "supply_tag",
        "supply_tag_id": supply_id,
        "nome": name,
        "canal_nome": parsed["canal_nome"],
        "platform": parsed["platform"],
        "device": parsed["device"],
        "ad_position": _detect_ad_position(name),
        "status": (
            "active" if raw.get("is_active") else "inactive"
        ),
        "account_id": raw.get("account_id"),
        "demand_tag_count": len(demand_priorities),
        "demand_tags": ", ".join(demand_names) if demand_names else "",
        "demand_tag_ids": ", ".join(demand_ids) if demand_ids else "",
        "created_at": raw.get("created_at", ""),
        "updated_at": raw.get("updated_at", ""),
    }


def normalize_demand_tag(raw: dict) -> Dict[str, Any]:
    """Normalize a raw SpringServe demand tag to flat JSON."""
    demand_id = raw.get("id", "")
    supply_ids = raw.get("supply_tag_ids", []) or []

    return {
        "channel_id": f"demand_tag_{demand_id}",
        "servico": "SpringServe",
        "tipo": "demand_tag",
        "demand_tag_id": demand_id,
        "nome": raw.get("name", ""),
        "status": (
            "active" if raw.get("is_active") else "inactive"
        ),
        "demand_type": raw.get("type", raw.get("demand_type", "")),
        "supply_tag_ids": ", ".join(str(s) for s in supply_ids),
    }


def normalize_report(raw: dict) -> Dict[str, Any]:
    """Normalize a raw SpringServe report row to flat JSON.

    Captures all metrics available in the SpringServe UI:
    Requests, Opps, Imps, Opp Fill %, Req Fill %,
    Pod Time Req Fill %, RPM, Rev.
    Also derives ad_position from the supply tag name.
    """
    supply_id = raw.get("supply_tag_id", "")
    supply_name = raw.get("supply_tag_name", "")

    return {
        "channel_id": f"report_supply_{supply_id}",
        "servico": "SpringServe",
        "tipo": "report",
        "supply_tag_id": supply_id,
        "supply_tag_name": supply_name,
        "ad_position": _detect_ad_position(supply_name),
        # Core delivery metrics
        "requests": raw.get("requests"),
        "opportunities": raw.get("opportunities", raw.get("opps")),
        "impressions": raw.get("impressions", raw.get("total_impressions")),
        # Fill rates
        "fill_rate": raw.get("fill_rate"),
        "opp_fill_rate": raw.get("opp_fill_rate", raw.get("opp_fill_pct")),
        "req_fill_rate": raw.get("req_fill_rate", raw.get("req_fill_pct")),
        "pod_time_req_fill_rate": raw.get(
            "pod_time_req_fill_rate",
            raw.get("pod_time_req_fill_pct"),
        ),
        # Revenue metrics
        "revenue": raw.get("revenue", raw.get("total_revenue")),
        "total_cost": raw.get("total_cost"),
        "cpm": raw.get("cpm"),
        "rpm": raw.get("rpm"),
        # Backwards-compat aliases
        "total_impressions": raw.get(
            "impressions", raw.get("total_impressions")
        ),
        "total_revenue": raw.get("revenue", raw.get("total_revenue")),
        "data_inicio": raw.get("data_inicio", raw.get("start_date", "")),
        "data_fim": raw.get("data_fim", raw.get("end_date", "")),
    }


def normalize_delivery_modifier(raw: dict) -> Dict[str, Any]:
    """Normalize a raw SpringServe delivery modifier to flat JSON."""
    modifier_id = raw.get("id", "")
    dtag_ids = raw.get("demand_tag_ids", []) or []

    return {
        "channel_id": f"delivery_modifier_{modifier_id}",
        "servico": "SpringServe",
        "tipo": "delivery_modifier",
        "modifier_id": modifier_id,
        "nome": raw.get("name", ""),
        "descricao": raw.get("description", ""),
        "ativo": bool(raw.get("active", raw.get("is_active", False))),
        "demand_tag_ids": ", ".join(str(d) for d in dtag_ids),
        "multiplier_interaction": raw.get(
            "multiplier_interaction", ""
        ),
    }


def normalize_creative(raw: dict) -> Dict[str, Any]:
    """Normalize a raw SpringServe creative to flat JSON."""
    creative_id = raw.get("id", "")

    return {
        "channel_id": f"creative_{creative_id}",
        "servico": "SpringServe",
        "tipo": "creative",
        "creative_id": creative_id,
        "nome": raw.get("name", ""),
        "creative_type": raw.get("creative_type", raw.get("type", "")),
        "status": (
            "active" if raw.get("is_active") else "inactive"
        ),
        "demand_tag_id": raw.get("demand_tag_id"),
        "format": raw.get("format", ""),
        "duration": raw.get("duration"),
    }


def normalize_label(
    raw: dict,
    label_type: str = "supply",
) -> Dict[str, Any]:
    """Normalize a raw SpringServe label to flat JSON.

    Args:
        raw: Raw label dict from the API.
        label_type: "supply" or "demand".
    """
    label_id = raw.get("id", "")
    tipo = f"{label_type}_label"

    return {
        "channel_id": f"{tipo}_{label_id}",
        "servico": "SpringServe",
        "tipo": tipo,
        "label_id": label_id,
        "nome": raw.get("name", ""),
    }


def normalize_scheduled_report(raw: dict) -> Dict[str, Any]:
    """Normalize a raw SpringServe scheduled report to flat JSON."""
    report_id = raw.get("id", "")
    dims = raw.get("dimensions", []) or []
    metrics = raw.get("metrics", []) or []

    return {
        "channel_id": f"scheduled_report_{report_id}",
        "servico": "SpringServe",
        "tipo": "scheduled_report",
        "report_id": report_id,
        "nome": raw.get("name", ""),
        "frequency": raw.get("frequency", ""),
        "status": (
            "active" if raw.get("is_active", raw.get("active")) else "inactive"
        ),
        "dimensions": (
            ", ".join(str(d) for d in dims)
            if isinstance(dims, list) else str(dims)
        ),
        "metrics": (
            ", ".join(str(m) for m in metrics)
            if isinstance(metrics, list) else str(metrics)
        ),
    }


def normalize_correlation(
    mt_config: dict,
    supply_tag: dict,
    report_data: dict | None = None,
) -> Dict[str, Any]:
    """Normalize a MediaTailor↔SpringServe correlation to flat JSON.

    Args:
        mt_config: MediaTailor playback configuration dict.
        supply_tag: Normalized supply tag dict.
        report_data: Optional normalized report dict with metrics.
    """
    mt_name = mt_config.get("Name", mt_config.get("nome_canal", ""))
    report_data = report_data or {}

    return {
        "channel_id": f"correlacao_{mt_name}",
        "servico": "Correlacao",
        "tipo": "canal_springserve",
        "mediatailor_name": mt_name,
        "mediatailor_ad_server_url": mt_config.get(
            "AdDecisionServerUrl",
            mt_config.get("ad_server_url", ""),
        ),
        "supply_tag_id": supply_tag.get("supply_tag_id", ""),
        "supply_tag_name": supply_tag.get("nome", ""),
        "ad_position": supply_tag.get("ad_position", "unknown"),
        "demand_tags_associadas": supply_tag.get("demand_tags", ""),
        # Metrics from report
        "requests": report_data.get("requests"),
        "opportunities": report_data.get("opportunities"),
        "fill_rate_atual": report_data.get("fill_rate"),
        "opp_fill_rate": report_data.get("opp_fill_rate"),
        "req_fill_rate": report_data.get("req_fill_rate"),
        "pod_time_req_fill_rate": report_data.get(
            "pod_time_req_fill_rate"
        ),
        "total_impressions_24h": report_data.get(
            "impressions", report_data.get("total_impressions")
        ),
        "revenue": report_data.get(
            "revenue", report_data.get("total_revenue")
        ),
        "rpm": report_data.get("rpm"),
        "cpm": report_data.get("cpm"),
    }
