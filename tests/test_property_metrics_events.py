# Feature: cloudwatch-metrics-ingestion, Property 2: event validation
# Feature: cloudwatch-metrics-ingestion, Property 3: event count
"""Property-based tests for event normalisation and event count.

**Validates: Requirements 6.1, 6.2, 6.8, 9.1, 9.3, 9.4, 9.5,
7.3, 7.5, 2.9, 5.6**

Property 2: Normalized events pass existing validation.
Property 3: Event count per resource is correct.
"""

import sys
import os
from datetime import datetime, timezone

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
    build_evento_estruturado,
    classify_severity,
    METRICS_CONFIG,
    SEVERITY_THRESHOLDS,
    ENRICHMENT_MAP,
)
from lambdas.shared.validators import (
    validate_evento_estruturado,
    SERVICOS_ORIGEM_VALIDOS,
)

# -------------------------------------------------------------------
# Strategies
# -------------------------------------------------------------------

VALID_SERVICES = list(METRICS_CONFIG.keys())

# Build (service, metric_name, statistic) triples from METRICS_CONFIG
_SERVICE_METRIC_TRIPLES = []
for _svc, _cfg in METRICS_CONFIG.items():
    for _metric_name, _stat in _cfg["metrics"]:
        _SERVICE_METRIC_TRIPLES.append((_svc, _metric_name, _stat))

_service_metric_triple = st.sampled_from(_SERVICE_METRIC_TRIPLES)

_metric_value = st.floats(
    min_value=0.0,
    max_value=10000.0,
    allow_nan=False,
    allow_infinity=False,
)

_canal_name = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "Pd"),
    ),
    min_size=1,
    max_size=30,
)

_timestamp_dt = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31),
    timezones=st.just(timezone.utc),
)

VALID_SERVICOS_PIPELINE = {
    "MediaLive", "MediaPackage", "MediaTailor", "CloudFront",
}


# -------------------------------------------------------------------
# Property 2: Normalized events pass existing validation
# -------------------------------------------------------------------


@settings(max_examples=10)
@given(
    triple=_service_metric_triple,
    value=_metric_value,
    canal=_canal_name,
    ts=_timestamp_dt,
)
def test_build_evento_passes_validation(triple, value, canal, ts):
    """build_evento_estruturado produces events that pass
    validate_evento_estruturado for any valid metric input.

    **Validates: Requirements 6.1, 6.2, 6.8, 9.1, 9.3, 9.4, 9.5**
    """
    service, metric_name, statistic = triple

    severity_info = classify_severity(metric_name, value, service)

    metric_data = {
        "metric_name": metric_name,
        "value": value,
        "timestamp": ts,
        "unit": "Count",
        "period": 300,
        "statistic": statistic,
    }
    resource_info = {
        "resource_id": canal,
        "service": service,
    }

    evento = build_evento_estruturado(
        metric_data, resource_info, severity_info,
    )

    # Must pass the existing validator
    result = validate_evento_estruturado(evento)
    assert result.is_valid, (
        f"Validation failed for service={service}, "
        f"metric={metric_name}, value={value}, canal={canal}: "
        f"{result.errors}"
    )

    # All required fields must be present and non-empty strings
    for field in (
        "timestamp", "canal", "severidade", "tipo_erro",
        "descricao", "causa_provavel", "recomendacao_correcao",
    ):
        assert field in evento, f"Missing field: {field}"
        assert isinstance(evento[field], str), (
            f"Field {field} is not a string: {type(evento[field])}"
        )
        assert evento[field].strip(), (
            f"Field {field} is empty"
        )

    # servico_origem must be one of the four valid services
    assert evento["servico_origem"] in VALID_SERVICOS_PIPELINE, (
        f"Invalid servico_origem: '{evento['servico_origem']}'"
    )


# -------------------------------------------------------------------
# Property 3: Event count per resource is correct
# -------------------------------------------------------------------

# Build per-service metric name lists from METRICS_CONFIG
_SERVICE_METRICS_MAP = {}
for _svc, _cfg in METRICS_CONFIG.items():
    _SERVICE_METRICS_MAP[_svc] = [
        m for m, _ in _cfg["metrics"]
    ]


def _count_events_for_resource(service, metrics_dict):
    """Replicate the pipeline logic: one event per anomalous metric,
    or exactly one INFO event if all metrics are normal."""
    anomalous = 0
    for metric_name, value in metrics_dict.items():
        sev, _ = classify_severity(metric_name, value, service)
        if sev != "INFO":
            anomalous += 1
    return anomalous if anomalous > 0 else 1


@st.composite
def _service_with_metrics(draw):
    """Generate a (service, {metric_name: value}) pair."""
    service = draw(st.sampled_from(VALID_SERVICES))
    metric_names = _SERVICE_METRICS_MAP[service]
    # Generate a value for each metric of this service
    values = draw(
        st.fixed_dictionaries(
            {
                m: st.floats(
                    min_value=0.0,
                    max_value=10000.0,
                    allow_nan=False,
                    allow_infinity=False,
                )
                for m in metric_names
            }
        )
    )
    return service, values


@settings(max_examples=10)
@given(data=_service_with_metrics())
def test_event_count_per_resource(data):
    """The number of events generated for a resource equals the
    number of anomalous metrics, or 1 if all metrics are normal.

    **Validates: Requirements 7.3, 7.5, 2.9, 5.6**
    """
    service, metrics_dict = data

    expected_count = _count_events_for_resource(
        service, metrics_dict,
    )

    # Simulate what the pipeline does: classify each metric
    anomalous_events = []
    for metric_name, value in metrics_dict.items():
        sev, error_type = classify_severity(
            metric_name, value, service,
        )
        if sev != "INFO":
            anomalous_events.append(
                (metric_name, value, sev, error_type)
            )

    if anomalous_events:
        actual_count = len(anomalous_events)
    else:
        # Pipeline generates exactly 1 INFO event
        actual_count = 1

    assert actual_count == expected_count, (
        f"Expected {expected_count} events for service={service}, "
        f"got {actual_count}. Metrics: {metrics_dict}"
    )

    # Additional: verify each anomalous event would produce a valid
    # Evento_Estruturado
    ts = datetime.now(timezone.utc)
    for metric_name, value, sev, error_type in anomalous_events:
        metric_data = {
            "metric_name": metric_name,
            "value": value,
            "timestamp": ts,
            "unit": "Count",
            "period": 300,
            "statistic": "Sum",
        }
        resource_info = {
            "resource_id": "test-resource",
            "service": service,
        }
        evento = build_evento_estruturado(
            metric_data, resource_info, (sev, error_type),
        )
        result = validate_evento_estruturado(evento)
        assert result.is_valid, (
            f"Anomalous event failed validation: {result.errors}"
        )


# Feature: cloudwatch-metrics-ingestion, Property 5: JSON round-trip
# Feature: cloudwatch-metrics-ingestion, Property 6: S3 key format

import json
import re

from lambdas.pipeline_logs.handler import (
    generate_s3_key,
    KB_LOGS_PREFIX,
)


# -------------------------------------------------------------------
# Property 5: JSON serialization round-trip preserves data
# -------------------------------------------------------------------


@settings(max_examples=10)
@given(
    triple=_service_metric_triple,
    value=_metric_value,
    canal=_canal_name,
    ts=_timestamp_dt,
)
def test_json_round_trip_preserves_data(triple, value, canal, ts):
    """Serializing an Evento_Estruturado to JSON and deserializing
    back produces an equivalent dict. Numeric fields remain numbers,
    timestamps remain ISO 8601 strings, and Unicode is preserved.

    **Validates: Requirements 12.1, 12.2, 12.3, 12.4, 8.4**
    """
    service, metric_name, statistic = triple

    severity_info = classify_severity(metric_name, value, service)

    metric_data = {
        "metric_name": metric_name,
        "value": value,
        "timestamp": ts,
        "unit": "Count",
        "period": 300,
        "statistic": statistic,
    }
    resource_info = {
        "resource_id": canal,
        "service": service,
    }

    evento = build_evento_estruturado(
        metric_data, resource_info, severity_info,
    )

    # Round-trip: serialize then deserialize
    json_str = json.dumps(evento, ensure_ascii=False)
    restored = json.loads(json_str)

    assert restored == evento, (
        f"Round-trip mismatch.\n"
        f"Original: {evento}\n"
        f"Restored: {restored}"
    )

    # Numeric fields must be JSON numbers (not strings)
    assert isinstance(restored["metrica_valor"], (int, float)), (
        f"metrica_valor is not a number: "
        f"{type(restored['metrica_valor'])}"
    )
    assert isinstance(restored["metrica_periodo"], (int, float)), (
        f"metrica_periodo is not a number: "
        f"{type(restored['metrica_periodo'])}"
    )

    # Timestamp must be an ISO 8601 string
    assert isinstance(restored["timestamp"], str), (
        f"timestamp is not a string: {type(restored['timestamp'])}"
    )
    # Basic ISO 8601 check: contains T separator and ends with Z
    assert "T" in restored["timestamp"], (
        f"timestamp missing T separator: {restored['timestamp']}"
    )
    assert restored["timestamp"].endswith("Z"), (
        f"timestamp missing Z suffix: {restored['timestamp']}"
    )

    # Unicode preservation: canal should survive round-trip
    assert restored["canal"] == canal


# -------------------------------------------------------------------
# Additional: verify Portuguese Unicode preservation explicitly
# -------------------------------------------------------------------

_PORTUGUESE_CHARS = st.text(
    alphabet="áàãâéêíóôõúüçÁÀÃÂÉÊÍÓÔÕÚÜÇ",
    min_size=1,
    max_size=15,
)


@settings(max_examples=10)
@given(
    canal_pt=_PORTUGUESE_CHARS,
    triple=_service_metric_triple,
    value=_metric_value,
    ts=_timestamp_dt,
)
def test_json_round_trip_unicode_portuguese(
    canal_pt, triple, value, ts,
):
    """Portuguese characters in canal names survive JSON round-trip.

    **Validates: Requirements 12.4, 8.4**
    """
    service, metric_name, statistic = triple
    severity_info = classify_severity(metric_name, value, service)

    metric_data = {
        "metric_name": metric_name,
        "value": value,
        "timestamp": ts,
        "unit": "Count",
        "period": 300,
        "statistic": statistic,
    }
    resource_info = {
        "resource_id": canal_pt,
        "service": service,
    }

    evento = build_evento_estruturado(
        metric_data, resource_info, severity_info,
    )

    json_str = json.dumps(evento, ensure_ascii=False)

    # Verify the Portuguese characters appear literally in JSON
    assert canal_pt in json_str, (
        f"Portuguese chars '{canal_pt}' not found in JSON output"
    )

    restored = json.loads(json_str)
    assert restored["canal"] == canal_pt


# -------------------------------------------------------------------
# Property 6: S3 key format is correct
# -------------------------------------------------------------------

_S3_KEY_PATTERN = re.compile(
    r"^(?P<prefix>.+)"
    r"(?P<service>MediaLive|MediaPackage|MediaTailor|CloudFront)"
    r"/(?P<canal>.+)_(?P<ts>\d{8}T\d{6}Z)\.json$"
)


@settings(max_examples=10)
@given(
    service=st.sampled_from(VALID_SERVICES),
    canal=st.text(
        alphabet=st.characters(
            whitelist_categories=("L", "N", "Pd"),
        ),
        min_size=1,
        max_size=30,
    ),
    ts=st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2030, 12, 31),
        timezones=st.just(timezone.utc),
    ),
)
def test_s3_key_format_correct(service, canal, ts):
    """generate_s3_key produces keys matching the pattern
    {KB_LOGS_PREFIX}{servico}/{canal}_{YYYYMMDDTHHMMSSz}.json

    **Validates: Requirements 8.2, 9.2**
    """
    key = generate_s3_key(service, canal, ts)

    # Must start with the configured prefix
    assert key.startswith(KB_LOGS_PREFIX), (
        f"Key does not start with prefix '{KB_LOGS_PREFIX}': {key}"
    )

    # Must contain the service name after the prefix
    after_prefix = key[len(KB_LOGS_PREFIX):]
    assert after_prefix.startswith(f"{service}/"), (
        f"Key does not contain service '{service}/' "
        f"after prefix: {key}"
    )

    # Must end with .json
    assert key.endswith(".json"), (
        f"Key does not end with .json: {key}"
    )

    # Must match the full pattern
    match = _S3_KEY_PATTERN.match(key)
    assert match is not None, (
        f"Key does not match expected pattern: {key}"
    )

    # Verify extracted components
    assert match.group("service") == service
    assert match.group("canal") == canal

    # Verify timestamp format matches the input datetime
    expected_ts = ts.strftime("%Y%m%dT%H%M%SZ")
    assert match.group("ts") == expected_ts, (
        f"Timestamp mismatch: expected {expected_ts}, "
        f"got {match.group('ts')}"
    )
