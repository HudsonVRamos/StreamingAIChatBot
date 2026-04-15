# Feature: cloudwatch-metrics-ingestion, Property 1: severity classification
"""Property-based tests for severity classification.

**Validates: Requirements 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 3.4, 3.5,
3.6, 3.7, 4.3, 4.4, 4.5, 4.6, 5.3, 5.4, 5.5, 5.6, 7.1, 7.4**

Property 1: Severity classification is correct for any metric value.

For any combination of (service, metric_name, numeric_value), the
function ``classify_severity`` SHALL return a (severity, error_type)
tuple where severity ∈ {INFO, WARNING, ERROR, CRITICAL}, and when a
value matches multiple thresholds the highest applicable severity is
selected.
"""

import sys
import os

# Mirror Lambda runtime layout so ``from shared.…`` resolves.
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
    classify_severity,
    SEVERITY_THRESHOLDS,
    METRICS_CONFIG,
)

# -------------------------------------------------------------------
# Strategies
# -------------------------------------------------------------------

VALID_SEVERITIES = {"INFO", "WARNING", "ERROR", "CRITICAL"}

# Severity ordering for comparison (higher index = more severe)
_SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 3}

# Build all valid (service, metric) pairs from SEVERITY_THRESHOLDS
_SERVICE_METRIC_PAIRS = []
for _svc, _metrics in SEVERITY_THRESHOLDS.items():
    for _metric in _metrics:
        _SERVICE_METRIC_PAIRS.append((_svc, _metric))

# Also include metrics that have NO thresholds (should return INFO)
_ALL_SERVICE_METRIC_PAIRS = list(_SERVICE_METRIC_PAIRS)
for _svc, _cfg in METRICS_CONFIG.items():
    for _metric_name, _stat in _cfg["metrics"]:
        pair = (_svc, _metric_name)
        if pair not in _ALL_SERVICE_METRIC_PAIRS:
            _ALL_SERVICE_METRIC_PAIRS.append(pair)


_service_metric_pair = st.sampled_from(_ALL_SERVICE_METRIC_PAIRS)

# Use floats that cover a wide range including boundary values
_metric_value = st.floats(
    min_value=0.0,
    max_value=10000.0,
    allow_nan=False,
    allow_infinity=False,
)


# -------------------------------------------------------------------
# Property test
# -------------------------------------------------------------------


@settings(max_examples=10)
@given(pair=_service_metric_pair, value=_metric_value)
def test_severity_classification_returns_valid_severity(pair, value):
    """For any (service, metric, value), classify_severity returns a
    valid severity in {INFO, WARNING, ERROR, CRITICAL}.

    **Validates: Requirements 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 3.4,
    3.5, 3.6, 3.7, 4.3, 4.4, 4.5, 4.6, 5.3, 5.4, 5.5, 5.6, 7.1, 7.4**
    """
    service, metric_name = pair
    severity, error_type = classify_severity(metric_name, value, service)

    # Severity must be one of the four valid values
    assert severity in VALID_SEVERITIES, (
        f"Invalid severity '{severity}' for "
        f"service={service}, metric={metric_name}, value={value}"
    )

    # error_type must be a non-empty string
    assert isinstance(error_type, str) and error_type, (
        f"Empty error_type for "
        f"service={service}, metric={metric_name}, value={value}"
    )


@settings(max_examples=10)
@given(pair=st.sampled_from(_SERVICE_METRIC_PAIRS), value=_metric_value)
def test_severity_highest_applicable_is_selected(pair, value):
    """When a value matches multiple thresholds, the most severe one
    is returned (rules are ordered most-severe-first).

    **Validates: Requirements 7.4**
    """
    service, metric_name = pair
    severity, error_type = classify_severity(metric_name, value, service)

    # Get the threshold rules for this (service, metric)
    rules = SEVERITY_THRESHOLDS.get(service, {}).get(metric_name, [])

    # Collect ALL matching rules
    matching = [
        (sev, etype)
        for cond, sev, etype in rules
        if cond(value)
    ]

    if not matching:
        # No threshold matched → must be INFO
        assert severity == "INFO", (
            f"Expected INFO when no threshold matches, got '{severity}' "
            f"for service={service}, metric={metric_name}, value={value}"
        )
        assert error_type == "METRICAS_NORMAIS"
    else:
        # The returned severity must be the highest among all matching
        highest_sev = max(matching, key=lambda x: _SEVERITY_ORDER[x[0]])
        assert severity == highest_sev[0], (
            f"Expected highest severity '{highest_sev[0]}' but got "
            f"'{severity}' for service={service}, metric={metric_name}, "
            f"value={value}. Matching rules: {matching}"
        )
        assert error_type == highest_sev[1], (
            f"Expected error_type '{highest_sev[1]}' but got "
            f"'{error_type}' for service={service}, metric={metric_name}, "
            f"value={value}"
        )
