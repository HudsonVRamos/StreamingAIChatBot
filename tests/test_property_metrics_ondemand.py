# Feature: cloudwatch-metrics-ingestion, Property 7: on-demand response
"""Property-based tests for on-demand metrics query response structure.

**Validates: Requirements 13.5**

Property 7: On-demand query response contains complete structure.

For any valid set of CloudWatch metric results, the function
``_consultar_metricas_query`` SHALL return a dict containing:
- severidade_geral ∈ {INFO, WARNING, ERROR, CRITICAL}
- alertas (a list)
- metricas (a dict where each entry has atual, max, media, unidade keys)
"""

from __future__ import annotations

import sys
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import hypothesis.strategies as st
from hypothesis import given, settings

from lambdas.configuradora.handler import (
    _consultar_metricas_query,
    _ONDEMAND_METRICS_CONFIG,
    _ONDEMAND_SEVERITY_THRESHOLDS,
    _classify_severity_ondemand,
    _SEVERITY_ORDER,
)

# -------------------------------------------------------------------
# Strategies
# -------------------------------------------------------------------

VALID_SEVERITIES = {"INFO", "WARNING", "ERROR", "CRITICAL"}
VALID_SERVICES = list(_ONDEMAND_METRICS_CONFIG.keys())

_metric_value = st.floats(
    min_value=0.0,
    max_value=10000.0,
    allow_nan=False,
    allow_infinity=False,
)


def _build_metric_values_strategy(servico: str):
    """Build a strategy that generates a dict of metric_name -> list[float]
    for a given service, simulating CloudWatch GetMetricData results."""
    config = _ONDEMAND_METRICS_CONFIG[servico]
    metric_names = [name for name, _ in config["metrics"]]
    return st.fixed_dictionaries({
        name: st.lists(_metric_value, min_size=1, max_size=12)
        for name in metric_names
    })


# Combined strategy: pick a service, then generate metric values for it
_service_and_values = st.sampled_from(VALID_SERVICES).flatmap(
    lambda svc: _build_metric_values_strategy(svc).map(
        lambda vals: (svc, vals)
    )
)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _mock_cw_response(servico, metric_values):
    """Build a mock CloudWatch GetMetricData response from metric_values dict."""
    results = []
    config = _ONDEMAND_METRICS_CONFIG[servico]

    for metric_name, values in metric_values.items():
        query_id = metric_name.replace(".", "_").lower()

        if servico == "MediaLive":
            # MediaLive has pipeline 0 and 1 — split values across both
            half = len(values) // 2 or 1
            for pipeline in ("0", "1"):
                pid = f"{query_id}_{pipeline}"
                pipe_values = values[:half] if pipeline == "0" else values[half:]
                results.append({
                    "Id": pid,
                    "Values": pipe_values,
                    "Timestamps": [
                        datetime.now(timezone.utc) - timedelta(minutes=5 * i)
                        for i in range(len(pipe_values))
                    ],
                    "StatusCode": "Complete",
                })
        elif servico == "MediaPackage" and metric_name == "EgressRequestCount":
            for sc in ("2xx", "4xx", "5xx"):
                results.append({
                    "Id": f"{query_id}_{sc}",
                    "Values": values,
                    "Timestamps": [
                        datetime.now(timezone.utc) - timedelta(minutes=5 * i)
                        for i in range(len(values))
                    ],
                    "StatusCode": "Complete",
                })
        else:
            results.append({
                "Id": query_id,
                "Values": values,
                "Timestamps": [
                    datetime.now(timezone.utc) - timedelta(minutes=5 * i)
                    for i in range(len(values))
                ],
                "StatusCode": "Complete",
            })

    return {"MetricDataResults": results}


def _make_resolved(servico):
    """Build a minimal resolved resource dict for a service."""
    if servico == "MediaLive":
        return {"_resolved_name": "TestChannel", "_channel_id": "12345"}
    elif servico == "MediaPackage":
        return {
            "_resolved_name": "TestChannel",
            "_channel_group": "TestGroup",
            "_channel_name": "TestChannel",
        }
    elif servico == "MediaTailor":
        return {"_resolved_name": "TestConfig"}
    elif servico == "CloudFront":
        return {"_resolved_name": "E1234ABCDEF", "_distribution_id": "E1234ABCDEF"}
    return {}


# -------------------------------------------------------------------
# Property test
# -------------------------------------------------------------------


@settings(max_examples=10)
@given(data=_service_and_values)
def test_ondemand_response_has_complete_structure(data):
    """For any valid metric results, _consultar_metricas_query returns
    a response with severidade_geral, alertas list, and metricas dict
    where each metric entry has atual, max, media, unidade keys.

    **Validates: Requirements 13.5**
    """
    servico, metric_values = data
    resolved = _make_resolved(servico)
    mock_response = _mock_cw_response(servico, metric_values)

    mock_cw = MagicMock()
    mock_cw.get_metric_data.return_value = mock_response

    # Also mock list_origin_endpoints for MediaPackage
    if servico == "MediaPackage":
        mock_mpv2 = MagicMock()
        mock_mpv2.list_origin_endpoints.return_value = {
            "Items": [{"OriginEndpointName": "TestEndpoint"}],
        }
        with patch("lambdas.configuradora.handler.boto3") as mock_boto3, \
             patch("lambdas.configuradora.handler.mediapackagev2_client", mock_mpv2):
            mock_boto3.client.return_value = mock_cw
            result = _consultar_metricas_query(
                servico, resolved, 60, 300, None,
            )
    else:
        with patch("lambdas.configuradora.handler.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_cw
            result = _consultar_metricas_query(
                servico, resolved, 60, 300, None,
            )

    # --- Structural assertions ---

    # 1. severidade_geral must be a valid severity
    assert "severidade_geral" in result, "Missing 'severidade_geral' key"
    assert result["severidade_geral"] in VALID_SEVERITIES, (
        f"Invalid severidade_geral: {result['severidade_geral']}"
    )

    # 2. alertas must be a list
    assert "alertas" in result, "Missing 'alertas' key"
    assert isinstance(result["alertas"], list), (
        f"alertas should be a list, got {type(result['alertas'])}"
    )

    # 3. Each alert must have required fields
    for alerta in result["alertas"]:
        assert "metrica" in alerta, "Alert missing 'metrica'"
        assert "valor" in alerta, "Alert missing 'valor'"
        assert "severidade" in alerta, "Alert missing 'severidade'"
        assert alerta["severidade"] in VALID_SEVERITIES
        assert "tipo_erro" in alerta, "Alert missing 'tipo_erro'"
        assert "descricao" in alerta, "Alert missing 'descricao'"

    # 4. metricas must be a dict
    assert "metricas" in result, "Missing 'metricas' key"
    assert isinstance(result["metricas"], dict), (
        f"metricas should be a dict, got {type(result['metricas'])}"
    )

    # 5. Each metric entry must have atual, max, media, unidade
    for metric_name, entry in result["metricas"].items():
        assert "atual" in entry, f"Metric '{metric_name}' missing 'atual'"
        assert "max" in entry, f"Metric '{metric_name}' missing 'max'"
        assert "media" in entry, f"Metric '{metric_name}' missing 'media'"
        assert "unidade" in entry, f"Metric '{metric_name}' missing 'unidade'"

    # 6. severidade_geral must be the highest severity among alertas
    if result["alertas"]:
        max_alert_sev = max(
            result["alertas"],
            key=lambda a: _SEVERITY_ORDER.get(a["severidade"], 0),
        )["severidade"]
        assert _SEVERITY_ORDER[result["severidade_geral"]] >= _SEVERITY_ORDER[max_alert_sev], (
            f"severidade_geral ({result['severidade_geral']}) should be >= "
            f"highest alert severity ({max_alert_sev})"
        )
    else:
        assert result["severidade_geral"] == "INFO", (
            f"No alerts but severidade_geral is {result['severidade_geral']}"
        )
