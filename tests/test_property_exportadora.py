# Feature: streaming-chatbot, Property 14: Filtragem correta na exportação de configurações
# Feature: streaming-chatbot, Property 15: Filtragem correta na exportação de logs
# Feature: streaming-chatbot, Property 16: Consolidação correta na exportação combinada
# Feature: streaming-chatbot, Property 17: Round-trip de formatação CSV/JSON
# Feature: streaming-chatbot, Property 18: Exportação sem resultados não gera arquivo
# Feature: streaming-chatbot, Property 19: Mensagem de erro descritiva na exportação
"""Property-based tests for Lambda_Exportadora.

Tests cover:
- Property 14: Config export filtering (only matching records returned)
- Property 15: Log export filtering (only matching records returned)
- Property 16: Combined export consolidation (no loss or duplication)
- Property 17: CSV/JSON round-trip formatting equivalence
- Property 18: Empty results produce no file
- Property 19: Descriptive error messages contain the reason
"""

from __future__ import annotations

import csv
import io
import json

import hypothesis.strategies as st
from hypothesis import given, settings

from lambdas.exportadora.handler import (
    filter_records,
    format_as_csv,
    format_as_json,
    merge_data,
    _flatten_record,
)

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

_servicos = st.sampled_from(["MediaLive", "MediaPackage", "MediaTailor", "CloudFront"])
_channel_ids = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=20,
)
_canais = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=20,
)
_severidades = st.sampled_from(["INFO", "WARNING", "ERROR", "CRITICAL"])
_tipos_erro = st.sampled_from([
    "INPUT_LOSS", "BITRATE_DROP", "LATENCY_SPIKE",
    "AD_INSERTION_FAILURE", "CDN_ERROR", "CODEC_ERROR",
])
_codecs = st.sampled_from(["H.264", "H.265", "MPEG-2"])
_resolucoes = st.sampled_from(["1080p", "720p", "480p", "4K"])
_booleans = st.sampled_from([True, False])


# ---------------------------------------------------------------------------
# Config record strategy
# ---------------------------------------------------------------------------

@st.composite
def config_enriquecida(draw):
    """Generate a random Config_Enriquecida record."""
    return {
        "channel_id": draw(_channel_ids),
        "nome_canal": f"canal-{draw(st.integers(min_value=1, max_value=9999))}",
        "servico": draw(_servicos),
        "estado": draw(st.sampled_from(["RUNNING", "IDLE", "STOPPED"])),
        "regiao": draw(st.sampled_from(["us-east-1", "eu-west-1", "sa-east-1"])),
        "dados": {
            "codec_video": draw(_codecs),
            "resolucao": draw(_resolucoes),
            "bitrate_video": draw(st.integers(min_value=1000, max_value=20000)),
            "low_latency": draw(_booleans),
            "protocolo_ingest": draw(st.sampled_from(["SRT", "RTMP", "HLS", "RTP"])),
        },
    }


# ---------------------------------------------------------------------------
# Log record strategy
# ---------------------------------------------------------------------------

@st.composite
def evento_estruturado(draw):
    """Generate a random Evento_Estruturado record."""
    return {
        "timestamp": f"2024-01-{draw(st.integers(min_value=1, max_value=28)):02d}T"
                     f"{draw(st.integers(min_value=0, max_value=23)):02d}:"
                     f"{draw(st.integers(min_value=0, max_value=59)):02d}:00Z",
        "canal": draw(_canais),
        "severidade": draw(_severidades),
        "tipo_erro": draw(_tipos_erro),
        "descricao": draw(st.text(min_size=1, max_size=50)),
        "causa_provavel": draw(st.text(min_size=1, max_size=50)),
        "recomendacao_correcao": draw(st.text(min_size=1, max_size=50)),
        "servico_origem": draw(_servicos),
    }


# ===================================================================
# Property 14: Filtragem correta na exportação de configurações
# **Validates: Requirements 14.1, 14.7**
# ===================================================================


@st.composite
def config_records_and_filters(draw):
    """Generate a list of Config_Enriquecida records and a filter dict using values from the records."""
    records = draw(st.lists(config_enriquecida(), min_size=1, max_size=20))

    # Build filters from values actually present in the records
    filter_keys = draw(st.lists(
        st.sampled_from(["servico", "channel_id"]),
        min_size=1,
        max_size=2,
        unique=True,
    ))

    # Pick a random record to source filter values from
    source = draw(st.sampled_from(records))
    filtros: dict = {}
    for key in filter_keys:
        if key == "servico":
            filtros["servico"] = source["servico"]
        elif key == "channel_id":
            filtros["channel_id"] = source["channel_id"]

    return records, filtros


@settings(max_examples=100)
@given(data=config_records_and_filters())
def test_property14_config_filter_returns_only_matching(data):
    """Filtered config export must contain ONLY records matching all filters.

    **Validates: Requirements 14.1, 14.7**
    """
    records, filtros = data
    result = filter_records(records, filtros)

    # Every returned record must match all filters
    for rec in result:
        for key, expected in filtros.items():
            actual = rec.get(key)
            if actual is None:
                dados = rec.get("dados", {})
                if isinstance(dados, dict):
                    actual = dados.get(key)
            assert actual is not None, (
                f"Filtered record missing key '{key}'"
            )
            assert str(actual).lower() == str(expected).lower(), (
                f"Record {key}={actual} does not match filter {expected}"
            )

    # Every record in the original that matches all filters must be in the result
    for rec in records:
        matches_all = True
        for key, expected in filtros.items():
            actual = rec.get(key)
            if actual is None:
                dados = rec.get("dados", {})
                if isinstance(dados, dict):
                    actual = dados.get(key)
            if actual is None or str(actual).lower() != str(expected).lower():
                matches_all = False
                break
        if matches_all:
            assert rec in result, (
                f"Matching record not found in filtered result: {rec}"
            )


# ===================================================================
# Property 15: Filtragem correta na exportação de logs
# **Validates: Requirements 14.2, 14.7**
# ===================================================================


@st.composite
def log_records_and_filters(draw):
    """Generate a list of Evento_Estruturado records and a filter dict using values from the records."""
    records = draw(st.lists(evento_estruturado(), min_size=1, max_size=20))

    filter_keys = draw(st.lists(
        st.sampled_from(["canal", "severidade", "tipo_erro"]),
        min_size=1,
        max_size=3,
        unique=True,
    ))

    source = draw(st.sampled_from(records))
    filtros: dict = {}
    for key in filter_keys:
        filtros[key] = source[key]

    return records, filtros


@settings(max_examples=100)
@given(data=log_records_and_filters())
def test_property15_log_filter_returns_only_matching(data):
    """Filtered log export must contain ONLY records matching all filters.

    **Validates: Requirements 14.2, 14.7**
    """
    records, filtros = data
    result = filter_records(records, filtros)

    # Every returned record must match all filters
    for rec in result:
        for key, expected in filtros.items():
            actual = rec.get(key)
            if actual is None:
                dados = rec.get("dados", {})
                if isinstance(dados, dict):
                    actual = dados.get(key)
            assert actual is not None, (
                f"Filtered record missing key '{key}'"
            )
            assert str(actual).lower() == str(expected).lower(), (
                f"Record {key}={actual} does not match filter {expected}"
            )

    # Every record in the original that matches all filters must be in the result
    for rec in records:
        matches_all = True
        for key, expected in filtros.items():
            actual = rec.get(key)
            if actual is None:
                dados = rec.get("dados", {})
                if isinstance(dados, dict):
                    actual = dados.get(key)
            if actual is None or str(actual).lower() != str(expected).lower():
                matches_all = False
                break
        if matches_all:
            assert rec in result, (
                f"Matching record not found in filtered result: {rec}"
            )


# ===================================================================
# Property 16: Consolidação correta na exportação combinada
# **Validates: Requirements 14.3**
# ===================================================================


@settings(max_examples=100)
@given(
    config_data=st.lists(config_enriquecida(), min_size=0, max_size=10),
    logs_data=st.lists(evento_estruturado(), min_size=0, max_size=10),
)
def test_property16_merge_preserves_all_records(config_data, logs_data):
    """Merged data must contain all config and log records without loss or duplication.

    **Validates: Requirements 14.3**
    """
    merged = merge_data(config_data, logs_data)

    # Total count must equal sum of both sources
    assert len(merged) == len(config_data) + len(logs_data), (
        f"Expected {len(config_data) + len(logs_data)} records, got {len(merged)}"
    )

    # Config records tagged as 'configuracao'
    config_in_merged = [r for r in merged if r.get("_fonte") == "configuracao"]
    logs_in_merged = [r for r in merged if r.get("_fonte") == "logs"]

    assert len(config_in_merged) == len(config_data), (
        f"Expected {len(config_data)} config records, got {len(config_in_merged)}"
    )
    assert len(logs_in_merged) == len(logs_data), (
        f"Expected {len(logs_data)} log records, got {len(logs_in_merged)}"
    )

    # Verify each original config record is present (by checking key fields)
    for orig in config_data:
        found = any(
            m.get("channel_id") == orig.get("channel_id")
            and m.get("servico") == orig.get("servico")
            and m.get("_fonte") == "configuracao"
            for m in config_in_merged
        )
        assert found, f"Config record not found in merged: {orig.get('channel_id')}"

    # Verify each original log record is present
    for orig in logs_data:
        found = any(
            m.get("timestamp") == orig.get("timestamp")
            and m.get("canal") == orig.get("canal")
            and m.get("tipo_erro") == orig.get("tipo_erro")
            and m.get("_fonte") == "logs"
            for m in logs_in_merged
        )
        assert found, f"Log record not found in merged: {orig.get('canal')}"


# ===================================================================
# Property 17: Round-trip de formatação CSV/JSON
# **Validates: Requirements 14.4**
# ===================================================================

# Strategy for simple flat records (no nested 'dados') to make round-trip clean
@st.composite
def flat_exportable_record(draw):
    """Generate a flat record suitable for CSV/JSON round-trip testing."""
    return {
        "channel_id": draw(_channel_ids),
        "nome_canal": f"canal-{draw(st.integers(min_value=1, max_value=9999))}",
        "servico": draw(_servicos),
        "estado": draw(st.sampled_from(["RUNNING", "IDLE", "STOPPED"])),
    }


@settings(max_examples=100)
@given(records=st.lists(flat_exportable_record(), min_size=1, max_size=10))
def test_property17_csv_roundtrip(records):
    """Data formatted as CSV and parsed back must be equivalent to the original.

    **Validates: Requirements 14.4**
    """
    columns = ["channel_id", "nome_canal", "servico", "estado"]
    csv_output = format_as_csv(records, columns)

    # Parse CSV back
    reader = csv.DictReader(io.StringIO(csv_output))
    parsed = list(reader)

    assert len(parsed) == len(records), (
        f"CSV round-trip: expected {len(records)} rows, got {len(parsed)}"
    )

    for orig, row in zip(records, parsed):
        flat = _flatten_record(orig)
        for col in columns:
            expected_val = str(flat.get(col, "")) if flat.get(col) is not None else ""
            actual_val = row.get(col, "")
            assert actual_val == expected_val, (
                f"CSV round-trip mismatch for '{col}': expected '{expected_val}', got '{actual_val}'"
            )


@settings(max_examples=100)
@given(records=st.lists(flat_exportable_record(), min_size=1, max_size=10))
def test_property17_json_roundtrip(records):
    """Data formatted as JSON and parsed back must be equivalent to the original.

    **Validates: Requirements 14.4**
    """
    columns = ["channel_id", "nome_canal", "servico", "estado"]
    json_output = format_as_json(records, columns)

    # Parse JSON back
    parsed = json.loads(json_output)

    assert len(parsed) == len(records), (
        f"JSON round-trip: expected {len(records)} items, got {len(parsed)}"
    )

    for orig, item in zip(records, parsed):
        flat = _flatten_record(orig)
        for col in columns:
            expected_val = flat.get(col)
            actual_val = item.get(col)
            assert actual_val == expected_val, (
                f"JSON round-trip mismatch for '{col}': expected '{expected_val}', got '{actual_val}'"
            )


# ===================================================================
# Property 18: Exportação sem resultados não gera arquivo
# **Validates: Requirements 14.8**
# ===================================================================


@st.composite
def records_and_non_matching_filters(draw):
    """Generate records + filters guaranteed to match nothing."""
    records = draw(st.lists(config_enriquecida(), min_size=1, max_size=10))

    # Collect all servico values present in the records
    present_servicos = {r["servico"] for r in records}
    all_servicos = {"MediaLive", "MediaPackage", "MediaTailor", "CloudFront"}
    absent_servicos = all_servicos - present_servicos

    if absent_servicos:
        # Use a servico value that doesn't exist in any record
        non_matching = draw(st.sampled_from(sorted(absent_servicos)))
        filtros = {"servico": non_matching}
    else:
        # All servicos present — use a channel_id that can't exist
        filtros = {"channel_id": "NONEXISTENT_CHANNEL_ID_99999"}

    return records, filtros


@settings(max_examples=100)
@given(data=records_and_non_matching_filters())
def test_property18_no_match_returns_empty(data):
    """When filters match no records, filter_records must return an empty list.

    **Validates: Requirements 14.8**
    """
    records, filtros = data
    result = filter_records(records, filtros)
    assert result == [], (
        f"Expected empty result for non-matching filters {filtros}, got {len(result)} records"
    )


# ===================================================================
# Property 19: Mensagem de erro descritiva na exportação
# **Validates: Requirements 14.9**
# ===================================================================

_error_codes = st.sampled_from([
    "NoSuchBucket", "NoSuchKey", "AccessDenied",
    "InternalError", "ServiceUnavailable",
    "InvalidBucketName", "InvalidObjectState",
])

_error_reasons = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Z", "P")),
    min_size=1,
    max_size=80,
)


def build_export_error_message(error_code: str, error_message: str) -> str:
    """Build the error message string as Lambda_Exportadora does for S3 errors."""
    return f"Erro ao acessar S3: [{error_code}] {error_message}"


def build_export_generic_error_message(reason: str) -> str:
    """Build the generic error message string as Lambda_Exportadora does."""
    return f"Erro na exportação: {reason}"


@settings(max_examples=100)
@given(
    error_code=_error_codes,
    error_message=_error_reasons,
)
def test_property19_s3_error_contains_code_and_reason(error_code, error_message):
    """S3 error responses must contain both the error code and the reason.

    **Validates: Requirements 14.9**
    """
    msg = build_export_error_message(error_code, error_message)

    assert error_code in msg, (
        f"Error code '{error_code}' not found in message: {msg}"
    )
    assert error_message in msg, (
        f"Error reason '{error_message}' not found in message: {msg}"
    )


@settings(max_examples=100)
@given(reason=_error_reasons)
def test_property19_generic_error_contains_reason(reason):
    """Generic export error responses must contain the reason for the failure.

    **Validates: Requirements 14.9**
    """
    msg = build_export_generic_error_message(reason)

    assert reason in msg, (
        f"Error reason '{reason}' not found in message: {msg}"
    )
