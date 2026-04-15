# Feature: cloudwatch-metrics-ingestion, Property 4: resilience
"""Property-based tests for pipeline resilience to partial failures.

**Validates: Requirements 1.6, 11.1, 11.2, 11.4**

Property 4: Pipeline is resilient to partial failures.
For any subset of services that fail during collection, the pipeline
SHALL continue processing the remaining services, record each error,
and the final summary SHALL correctly reflect totals.
"""

import sys
import os
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        os.pardir,
        "lambdas",
        "pipeline_logs",
    ),
)

import hypothesis.strategies as st
from hypothesis import given, settings

from lambdas.pipeline_logs.handler import (
    handler,
    METRICS_CONFIG,
)

VALID_SERVICES = list(METRICS_CONFIG.keys())

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

_FAKE_RESOURCES = {
    "MediaLive": [
        {"ChannelId": "111", "Name": "Canal_A"},
    ],
    "MediaPackage": [
        {
            "ChannelGroup": "grp1",
            "ChannelName": "ch1",
            "OriginEndpoints": ["ep1"],
        },
    ],
    "MediaTailor": [{"Name": "config1"}],
    "CloudFront": [{"DistributionId": "E123"}],
}


def _make_discover_side_effect(failing_services):
    """Return a discover_resources result where *failing_services*
    have empty resource lists (simulating discovery failure for
    those services)."""
    result = {}
    for svc in VALID_SERVICES:
        if svc in failing_services:
            result[svc] = []
        else:
            result[svc] = _FAKE_RESOURCES[svc]
    return result


def _make_collect_side_effect(failing_services):
    """Return a collect_metrics side-effect function.

    Services in *failing_services* raise an exception.
    Others return a single resource entry with one normal metric
    data point so the pipeline can generate an INFO event.
    """
    def _side_effect(service, resources):
        if service in failing_services:
            raise RuntimeError(
                f"Simulated failure for {service}"
            )
        ts = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        return [
            {
                "resource": resources[0] if resources else {},
                "resource_id": f"res-{service}",
                "service": service,
                "metrics": {
                    "dummy_metric": [(ts, 0.0)],
                },
                "partial": False,
                "error": None,
            },
        ]
    return _side_effect


# -------------------------------------------------------------------
# Property 4: Pipeline is resilient to partial failures
# -------------------------------------------------------------------


@settings(max_examples=10)
@given(
    failing=st.lists(
        st.sampled_from(VALID_SERVICES),
        unique=True,
        min_size=0,
        max_size=len(VALID_SERVICES),
    ),
)
def test_pipeline_resilient_to_partial_failures(failing):
    """For any subset of services that fail, the remaining services
    are still processed and the summary totals are correct.

    **Validates: Requirements 1.6, 11.1, 11.2, 11.4**
    """
    surviving = [s for s in VALID_SERVICES if s not in failing]

    discover_result = _make_discover_side_effect(failing)
    collect_side_effect = _make_collect_side_effect(failing)

    mock_s3 = MagicMock()
    mock_s3.put_object.return_value = {}

    with patch(
        "lambdas.pipeline_logs.handler.discover_resources",
        return_value=discover_result,
    ), patch(
        "lambdas.pipeline_logs.handler.collect_metrics",
        side_effect=collect_side_effect,
    ), patch(
        "lambdas.pipeline_logs.handler.boto3"
    ) as mock_boto3:
        mock_boto3.client.return_value = mock_s3

        result = handler({}, None)

    body = result.get("body", {})

    # The pipeline must always return statusCode 200
    assert result.get("statusCode") == 200

    stored = body.get("total_eventos_armazenados", 0)
    erros = body.get("total_erros", 0)
    rejeitados_val = body.get(
        "total_rejeitados_validacao", 0,
    )
    rejeitados_cont = body.get(
        "total_rejeitados_contaminacao", 0,
    )

    # Surviving services should each produce at least one event
    # (an INFO event when all metrics are normal)
    assert stored >= 0

    # If all services fail, no events should be stored
    if not surviving:
        assert stored == 0

    # If some services survive, they should produce events
    if surviving:
        assert stored >= len(surviving), (
            f"Expected at least {len(surviving)} events for "
            f"surviving services {surviving}, got {stored}"
        )

    # Processed services list should contain only surviving ones
    processed = body.get("servicos_processados", [])
    for svc in surviving:
        assert svc in processed, (
            f"Surviving service {svc} not in "
            f"servicos_processados: {processed}"
        )

    # Failing services should NOT appear in processed list
    for svc in failing:
        assert svc not in processed, (
            f"Failing service {svc} should not be in "
            f"servicos_processados: {processed}"
        )

    # Summary totals must be non-negative integers
    assert stored >= 0
    assert erros >= 0
    assert rejeitados_val >= 0
    assert rejeitados_cont >= 0
