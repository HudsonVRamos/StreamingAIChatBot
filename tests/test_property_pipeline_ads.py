# Feature: springserve-ad-integration, Property 5: re-auth on 401
"""Property-based tests for Pipeline_Ads — SpringServeAuth.

**Validates: Requirements 1.5**

Property 5: Re-autenticação automática em caso de token expirado.

For any API call that returns HTTP 401, the `request` method of
`SpringServeAuth` SHALL re-authenticate automatically via POST
/api/v1/auth exactly once and retry the original call. If
re-authentication fails, SHALL propagate the error without infinite
loop.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
import requests
from hypothesis import given, settings

from lambdas.pipeline_ads.shared.auth import SpringServeAuth

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_http_methods = st.sampled_from(["GET", "POST", "PUT", "DELETE", "PATCH"])

_api_paths = st.one_of(
    st.just("/api/v1/supply_tags"),
    st.just("/api/v1/demand_tags"),
    st.just("/api/v1/delivery_modifiers"),
    st.just("/api/v1/creatives"),
    st.just("/api/v1/reports"),
    st.just("/api/v1/supply_labels"),
    st.just("/api/v1/demand_labels"),
    st.just("/api/v1/scheduled_reports"),
)

_tokens = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=8,
    max_size=64,
)

_new_tokens = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=8,
    max_size=64,
)

_success_status_codes = st.sampled_from([200, 201, 204])


def _make_response(status_code: int, json_body: dict | None = None):
    """Build a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    if json_body is not None:
        resp.json.return_value = json_body
    if status_code >= 400:
        http_err = requests.HTTPError(
            response=resp,
        )
        resp.raise_for_status.side_effect = http_err
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Property 5: Re-autenticação automática em caso de token expirado
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    method=_http_methods,
    path=_api_paths,
    initial_token=_tokens,
    new_token=_new_tokens,
    success_code=_success_status_codes,
)
def test_reauth_on_401_retries_exactly_once_and_succeeds(
    method, path, initial_token, new_token, success_code
):
    """When a request returns 401 and a token exists, re-auth happens
    exactly once and the original call is retried successfully.

    **Validates: Requirements 1.5**
    """
    base_url = "https://video.springserve.com"
    auth = SpringServeAuth(base_url, "user@test.com", "secret")
    auth.token = initial_token
    auth.session.headers["Authorization"] = initial_token

    auth_response = _make_response(200, {"token": new_token})
    first_response = _make_response(401)
    retry_response = _make_response(success_code)

    session_request_calls = []

    def fake_session_request(m, url, **kwargs):
        session_request_calls.append((m, url))
        if len(session_request_calls) == 1:
            return first_response
        return retry_response

    with patch.object(auth.session, "post", return_value=auth_response) as mock_post, \
         patch.object(auth.session, "request", side_effect=fake_session_request):

        result = auth.request(method, path)

    # The original request was made twice (first 401, then retry)
    assert len(session_request_calls) == 2, (
        f"Expected 2 session.request calls, got {len(session_request_calls)}"
    )

    # Both calls used the same method and URL
    expected_url = f"{base_url}{path}"
    assert session_request_calls[0] == (method, expected_url)
    assert session_request_calls[1] == (method, expected_url)

    # Re-authentication was called exactly once
    mock_post.assert_called_once()
    post_call_url = mock_post.call_args[0][0]
    assert post_call_url == f"{base_url}/api/v1/auth"

    # Token was updated to the new one
    assert auth.token == new_token
    assert auth.session.headers["Authorization"] == new_token

    # The successful response is returned
    assert result is retry_response


@settings(max_examples=100)
@given(
    method=_http_methods,
    path=_api_paths,
    initial_token=_tokens,
)
def test_reauth_failure_propagates_error_without_infinite_loop(
    method, path, initial_token
):
    """When re-authentication itself fails, the error is propagated
    and no infinite loop occurs.

    **Validates: Requirements 1.5**
    """
    base_url = "https://video.springserve.com"
    auth = SpringServeAuth(base_url, "user@test.com", "secret")
    auth.token = initial_token
    auth.session.headers["Authorization"] = initial_token

    first_response = _make_response(401)
    failed_auth_response = _make_response(401)

    session_request_calls = []

    def fake_session_request(m, url, **kwargs):
        session_request_calls.append((m, url))
        return first_response

    with patch.object(
        auth.session, "post", return_value=failed_auth_response
    ) as mock_post, \
         patch.object(auth.session, "request", side_effect=fake_session_request):

        try:
            auth.request(method, path)
            raised = False
        except requests.HTTPError:
            raised = True

    # Error must be propagated
    assert raised, "Expected HTTPError to be raised when re-auth fails"

    # session.request was called exactly once (the initial call only)
    assert len(session_request_calls) == 1, (
        f"Expected 1 session.request call before re-auth, "
        f"got {len(session_request_calls)}"
    )

    # Re-authentication was attempted exactly once (no infinite loop)
    mock_post.assert_called_once()


@settings(max_examples=100)
@given(
    method=_http_methods,
    path=_api_paths,
    success_code=_success_status_codes,
)
def test_no_reauth_when_no_token_set(method, path, success_code):
    """When no token is set and a 401 is returned, re-auth is NOT
    attempted (token is None means we haven't authenticated yet).

    **Validates: Requirements 1.5**
    """
    base_url = "https://video.springserve.com"
    auth = SpringServeAuth(base_url, "user@test.com", "secret")
    # token is None — no prior authentication

    response_401 = _make_response(401)

    session_request_calls = []

    def fake_session_request(m, url, **kwargs):
        session_request_calls.append((m, url))
        return response_401

    with patch.object(auth.session, "post") as mock_post, \
         patch.object(auth.session, "request", side_effect=fake_session_request):

        try:
            auth.request(method, path)
            raised = False
        except requests.HTTPError:
            raised = True

    # Error must be propagated
    assert raised, "Expected HTTPError when 401 with no token"

    # session.request was called exactly once (no retry)
    assert len(session_request_calls) == 1

    # Re-authentication was NOT attempted
    mock_post.assert_not_called()


@settings(max_examples=100)
@given(
    method=_http_methods,
    path=_api_paths,
    initial_token=_tokens,
    success_code=_success_status_codes,
)
def test_no_reauth_on_non_401_errors(method, path, initial_token, success_code):
    """Non-401 errors (e.g. 500, 403) do NOT trigger re-authentication.

    **Validates: Requirements 1.5**
    """
    base_url = "https://video.springserve.com"
    auth = SpringServeAuth(base_url, "user@test.com", "secret")
    auth.token = initial_token

    error_codes = [400, 403, 404, 429, 500, 503]
    for error_code in error_codes:
        error_response = _make_response(error_code)
        session_request_calls = []

        def fake_session_request(m, url, **kwargs):
            session_request_calls.append((m, url))
            return error_response

        with patch.object(auth.session, "post") as mock_post, \
             patch.object(auth.session, "request", side_effect=fake_session_request):

            try:
                auth.request(method, path)
                raised = False
            except requests.HTTPError:
                raised = True

        assert raised, f"Expected HTTPError for status {error_code}"
        assert len(session_request_calls) == 1, (
            f"Expected 1 call for status {error_code}, "
            f"got {len(session_request_calls)}"
        )
        mock_post.assert_not_called()


# ===================================================================
# Feature: springserve-ad-integration,
# Property 1: pagination collects all items
# ===================================================================
"""
Property 1: Paginação coleta todos os itens.

For any paginated SpringServe API response with N pages and M items
per page, `_paginate_springserve` SHALL collect exactly the sum of
all items from all pages.

**Validates: Requirements 2.1, 3.1, 4.5**
"""

from lambdas.pipeline_ads.handler import (  # noqa: E402
    _paginate_springserve,
)


def _build_paginated_responses(total_pages, items_per_page):
    """Build a list of mock API responses for pagination."""
    pages = []
    for p in range(1, total_pages + 1):
        items = [
            {"id": (p - 1) * items_per_page + i}
            for i in range(items_per_page)
        ]
        pages.append({
            "results": items,
            "current_page": p,
            "total_pages": total_pages,
        })
    return pages


@settings(max_examples=100)
@given(
    total_pages=st.integers(min_value=1, max_value=20),
    items_per_page=st.integers(min_value=1, max_value=100),
)
def test_pagination_collects_all_items(
    total_pages, items_per_page
):
    """Pagination collects exactly the sum of all items from
    all pages.

    **Validates: Requirements 2.1, 3.1, 4.5**
    """
    pages = _build_paginated_responses(
        total_pages, items_per_page
    )
    call_count = [0]

    base_url = "https://video.springserve.com"
    auth = SpringServeAuth(base_url, "u@t.com", "pw")
    auth.token = "fake"

    def fake_request(method, url, **kwargs):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = pages[call_count[0]]
        call_count[0] += 1
        return resp

    with patch.object(
        auth.session, "request", side_effect=fake_request
    ):
        result = _paginate_springserve(
            auth, "/api/v1/supply_tags"
        )

    expected_total = total_pages * items_per_page
    assert len(result) == expected_total, (
        f"Expected {expected_total} items, got {len(result)}"
    )
    assert call_count[0] == total_pages


@settings(max_examples=100)
@given(
    total_pages=st.integers(min_value=1, max_value=20),
    items_per_page=st.integers(min_value=1, max_value=100),
)
def test_pagination_preserves_item_identity(
    total_pages, items_per_page
):
    """All individual items from every page appear in the result.

    **Validates: Requirements 2.1, 3.1, 4.5**
    """
    pages = _build_paginated_responses(
        total_pages, items_per_page
    )
    call_count = [0]

    base_url = "https://video.springserve.com"
    auth = SpringServeAuth(base_url, "u@t.com", "pw")
    auth.token = "fake"

    def fake_request(method, url, **kwargs):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = pages[call_count[0]]
        call_count[0] += 1
        return resp

    with patch.object(
        auth.session, "request", side_effect=fake_request
    ):
        result = _paginate_springserve(
            auth, "/api/v1/supply_tags"
        )

    expected_ids = set()
    for page in pages:
        for item in page["results"]:
            expected_ids.add(item["id"])

    result_ids = {item["id"] for item in result}
    assert result_ids == expected_ids


# ===================================================================
# Feature: springserve-ad-integration,
# Property 2: normalization produces flat JSON
# ===================================================================
"""
Property 2: Normalização produz flat JSON com campos obrigatórios.

For any raw SpringServe entity, the normalization function SHALL
produce a flat dict (no nested dicts) with required fields:
channel_id (non-empty), servico (correct value), tipo (correct value).

**Validates: Requirements 2.2, 3.2, 4.2, 5.1, 5.2, 5.3, 6.3,
             9.1, 9.2, 9.3, 9.4**
"""

import json  # noqa: E402

from lambdas.pipeline_ads.shared.normalizers import (  # noqa: E402
    normalize_supply_tag,
    normalize_demand_tag,
    normalize_report,
    normalize_delivery_modifier,
    normalize_creative,
    normalize_label,
    normalize_scheduled_report,
    normalize_correlation,
)

# --- Strategies for raw SpringServe entities ---

_pos_int = st.integers(min_value=1, max_value=99999)
_text = st.text(min_size=1, max_size=60).filter(
    lambda s: s.strip() != ""
)
_opt_text = st.one_of(st.none(), _text)
_bool = st.booleans()


def _raw_supply_tag():
    return st.fixed_dictionaries({
        "id": _pos_int,
        "name": _text,
        "is_active": _bool,
        "account_id": _pos_int,
        "created_at": st.just("2024-01-01T00:00:00Z"),
        "updated_at": st.just("2024-06-01T00:00:00Z"),
    })


def _demand_priorities():
    return st.lists(
        st.fixed_dictionaries({
            "demand_tag_id": _pos_int,
            "demand_tag_name": _text,
        }),
        min_size=0,
        max_size=5,
    )


def _raw_demand_tag():
    return st.fixed_dictionaries({
        "id": _pos_int,
        "name": _text,
        "is_active": _bool,
        "type": st.sampled_from(["vast_url", "rtb", "direct"]),
        "supply_tag_ids": st.lists(
            _pos_int, min_size=0, max_size=5
        ),
    })


def _raw_report():
    return st.fixed_dictionaries({
        "supply_tag_id": _pos_int,
        "supply_tag_name": _text,
        "fill_rate": st.floats(
            min_value=0.0, max_value=1.0,
            allow_nan=False, allow_infinity=False,
        ),
        "impressions": st.integers(
            min_value=0, max_value=1000000
        ),
        "revenue": st.floats(
            min_value=0.0, max_value=100000.0,
            allow_nan=False, allow_infinity=False,
        ),
        "total_cost": st.floats(
            min_value=0.0, max_value=100000.0,
            allow_nan=False, allow_infinity=False,
        ),
        "cpm": st.floats(
            min_value=0.0, max_value=1000.0,
            allow_nan=False, allow_infinity=False,
        ),
        "start_date": st.just("2024-06-19"),
        "end_date": st.just("2024-06-20"),
    })


def _raw_delivery_modifier():
    return st.fixed_dictionaries({
        "id": _pos_int,
        "name": _text,
        "description": _opt_text,
        "active": _bool,
        "demand_tag_ids": st.lists(
            _pos_int, min_size=0, max_size=5
        ),
        "multiplier_interaction": st.sampled_from(
            ["multiply", "add", ""]
        ),
    })


def _raw_creative():
    return st.fixed_dictionaries({
        "id": _pos_int,
        "name": _text,
        "is_active": _bool,
        "creative_type": st.sampled_from(
            ["video", "audio", "display"]
        ),
        "demand_tag_id": _pos_int,
        "format": st.sampled_from(["VAST", "VPAID", ""]),
        "duration": st.one_of(
            st.none(),
            st.integers(min_value=1, max_value=120),
        ),
    })


def _raw_label():
    return st.fixed_dictionaries({
        "id": _pos_int,
        "name": _text,
    })


def _raw_scheduled_report():
    return st.fixed_dictionaries({
        "id": _pos_int,
        "name": _text,
        "is_active": _bool,
        "frequency": st.sampled_from(
            ["daily", "weekly", "monthly"]
        ),
        "dimensions": st.lists(
            st.sampled_from(["supply_tag_id", "demand_tag_id"]),
            min_size=0, max_size=3,
        ),
        "metrics": st.lists(
            st.sampled_from([
                "impressions", "revenue", "fill_rate"
            ]),
            min_size=0, max_size=3,
        ),
    })


def _mt_config():
    return st.fixed_dictionaries({
        "Name": _text,
        "AdDecisionServerUrl": st.just(
            "https://video.springserve.com/vast/123"
        ),
    })


def _assert_flat_and_required(result, servico, tipo):
    """Assert result is flat and has required fields."""
    # channel_id must be non-empty string
    assert isinstance(result["channel_id"], str)
    assert result["channel_id"].strip() != ""

    # servico and tipo must match
    assert result["servico"] == servico
    assert result["tipo"] == tipo

    # No nested dicts or lists of dicts
    for key, val in result.items():
        assert not isinstance(val, dict), (
            f"Field '{key}' is a dict — not flat"
        )
        if isinstance(val, list):
            for item in val:
                assert not isinstance(item, dict), (
                    f"Field '{key}' contains a dict — "
                    "not flat"
                )


@settings(max_examples=100)
@given(raw=_raw_supply_tag(), prios=_demand_priorities())
def test_normalize_supply_tag_flat(raw, prios):
    """Supply tag normalization produces flat JSON.

    **Validates: Requirements 2.2, 9.1, 9.2, 9.3**
    """
    result = normalize_supply_tag(raw, prios)
    _assert_flat_and_required(result, "SpringServe", "supply_tag")


@settings(max_examples=100)
@given(raw=_raw_demand_tag())
def test_normalize_demand_tag_flat(raw):
    """Demand tag normalization produces flat JSON.

    **Validates: Requirements 3.2, 9.1, 9.2, 9.3**
    """
    result = normalize_demand_tag(raw)
    _assert_flat_and_required(
        result, "SpringServe", "demand_tag"
    )


@settings(max_examples=100)
@given(raw=_raw_report())
def test_normalize_report_flat(raw):
    """Report normalization produces flat JSON.

    **Validates: Requirements 4.2, 9.1, 9.2, 9.3**
    """
    result = normalize_report(raw)
    _assert_flat_and_required(result, "SpringServe", "report")


@settings(max_examples=100)
@given(raw=_raw_delivery_modifier())
def test_normalize_delivery_modifier_flat(raw):
    """Delivery modifier normalization produces flat JSON.

    **Validates: Requirements 5.1, 9.1, 9.2, 9.3**
    """
    result = normalize_delivery_modifier(raw)
    _assert_flat_and_required(
        result, "SpringServe", "delivery_modifier"
    )


@settings(max_examples=100)
@given(raw=_raw_creative())
def test_normalize_creative_flat(raw):
    """Creative normalization produces flat JSON.

    **Validates: Requirements 5.2, 9.1, 9.2, 9.3**
    """
    result = normalize_creative(raw)
    _assert_flat_and_required(result, "SpringServe", "creative")


@settings(max_examples=100)
@given(
    raw=_raw_label(),
    ltype=st.sampled_from(["supply", "demand"]),
)
def test_normalize_label_flat(raw, ltype):
    """Label normalization produces flat JSON.

    **Validates: Requirements 5.3, 9.1, 9.2, 9.3**
    """
    result = normalize_label(raw, ltype)
    _assert_flat_and_required(
        result, "SpringServe", f"{ltype}_label"
    )


@settings(max_examples=100)
@given(raw=_raw_scheduled_report())
def test_normalize_scheduled_report_flat(raw):
    """Scheduled report normalization produces flat JSON.

    **Validates: Requirements 5.3, 9.1, 9.2, 9.3**
    """
    result = normalize_scheduled_report(raw)
    _assert_flat_and_required(
        result, "SpringServe", "scheduled_report"
    )


@settings(max_examples=100)
@given(
    mt=_mt_config(),
    stag=_raw_supply_tag(),
    prios=_demand_priorities(),
    report=_raw_report(),
)
def test_normalize_correlation_flat(mt, stag, prios, report):
    """Correlation normalization produces flat JSON.

    **Validates: Requirements 6.3, 9.1, 9.4**
    """
    norm_stag = normalize_supply_tag(stag, prios)
    norm_report = normalize_report(report)
    result = normalize_correlation(mt, norm_stag, norm_report)
    _assert_flat_and_required(
        result, "Correlacao", "canal_springserve"
    )


# ===================================================================
# Feature: springserve-ad-integration,
# Property 4: JSON round-trip
# ===================================================================
"""
Property 4: Round-trip de serialização JSON preserva dados.

For any normalized Config_Ad, json.dumps + json.loads SHALL produce
an equivalent dict. Numeric fields preserved as numbers, Unicode
preserved.

**Validates: Requirement 9.5**
"""


def _any_normalized_config():
    """Strategy that produces a random normalized Config_Ad."""
    return st.one_of(
        st.builds(
            normalize_supply_tag,
            _raw_supply_tag(),
            _demand_priorities(),
        ),
        st.builds(normalize_demand_tag, _raw_demand_tag()),
        st.builds(normalize_report, _raw_report()),
        st.builds(
            normalize_delivery_modifier,
            _raw_delivery_modifier(),
        ),
        st.builds(normalize_creative, _raw_creative()),
        st.builds(
            normalize_label,
            _raw_label(),
            st.sampled_from(["supply", "demand"]),
        ),
        st.builds(
            normalize_scheduled_report,
            _raw_scheduled_report(),
        ),
    )


@settings(max_examples=100)
@given(config=_any_normalized_config())
def test_json_round_trip_preserves_data(config):
    """json.dumps + json.loads produces an equivalent dict.

    **Validates: Requirement 9.5**
    """
    serialized = json.dumps(config, ensure_ascii=False)
    deserialized = json.loads(serialized)

    assert deserialized == config, (
        f"Round-trip mismatch:\n"
        f"  original:     {config}\n"
        f"  deserialized: {deserialized}"
    )

    # Numeric fields stay numeric
    for key, val in config.items():
        if isinstance(val, (int, float)):
            assert isinstance(deserialized[key], (int, float)), (
                f"Field '{key}' lost numeric type"
            )


# ===================================================================
# Feature: springserve-ad-integration,
# Property 3: S3 key format follows the correct pattern
# ===================================================================
"""
Property 3: Formato da chave S3 segue o padrão correto.

For any (entity_type, entity_id), the S3 key SHALL follow
``kb-ads/{category}/{type}_{id}.json``, where category is
"SpringServe" for SpringServe entities and "Correlacao" for
correlations.

**Validates: Requirements 2.4, 3.3, 4.3, 5.4, 6.4**
"""

import re  # noqa: E402

from lambdas.pipeline_ads.handler import (  # noqa: E402
    build_s3_key,
    _extract_entity_id,
    _category_for_tipo,
    KB_ADS_PREFIX,
)

# Entity types and their expected categories
_springserve_tipos = st.sampled_from([
    "supply_tag",
    "demand_tag",
    "report",
    "delivery_modifier",
    "creative",
    "supply_label",
    "demand_label",
    "scheduled_report",
])

_correlacao_tipos = st.just("canal_springserve")

_entity_ids = st.one_of(
    st.integers(min_value=1, max_value=99999).map(str),
    st.text(
        alphabet=st.characters(
            whitelist_categories=("L", "N"),
            whitelist_characters="-_",
        ),
        min_size=1,
        max_size=30,
    ).filter(lambda s: s.strip() != ""),
)


@settings(max_examples=100)
@given(
    tipo=_springserve_tipos,
    entity_id=_entity_ids,
)
def test_s3_key_format_springserve_entities(
    tipo, entity_id
):
    """S3 key for SpringServe entities follows the pattern
    ``kb-ads/SpringServe/{tipo}_{id}.json``.

    **Validates: Requirements 2.4, 3.3, 4.3, 5.4**
    """
    key = build_s3_key(tipo, entity_id)

    # Must start with the configured prefix
    assert key.startswith(KB_ADS_PREFIX), (
        f"Key '{key}' does not start with '{KB_ADS_PREFIX}'"
    )

    # Category must be SpringServe
    after_prefix = key[len(KB_ADS_PREFIX):]
    parts = after_prefix.split("/", 1)
    assert len(parts) == 2, (
        f"Key '{key}' missing category/filename structure"
    )
    category, filename = parts
    assert category == "SpringServe", (
        f"Expected category 'SpringServe', got '{category}'"
    )

    # Filename must be {tipo}_{id}.json
    assert filename.endswith(".json"), (
        f"Filename '{filename}' does not end with .json"
    )
    expected_filename = f"{tipo}_{entity_id}.json"
    assert filename == expected_filename, (
        f"Expected '{expected_filename}', got '{filename}'"
    )

    # Full pattern check
    pattern = re.compile(
        r"^kb-ads/SpringServe/[a-z_]+_.+\.json$"
    )
    assert pattern.match(key), (
        f"Key '{key}' does not match expected pattern"
    )


@settings(max_examples=100)
@given(
    entity_id=_entity_ids,
)
def test_s3_key_format_correlacao_entities(entity_id):
    """S3 key for Correlacao entities follows the pattern
    ``kb-ads/Correlacao/canal_springserve_{id}.json``.

    **Validates: Requirements 6.4**
    """
    tipo = "canal_springserve"
    key = build_s3_key(tipo, entity_id)

    assert key.startswith(KB_ADS_PREFIX)

    after_prefix = key[len(KB_ADS_PREFIX):]
    parts = after_prefix.split("/", 1)
    assert len(parts) == 2
    category, filename = parts
    assert category == "Correlacao", (
        f"Expected 'Correlacao', got '{category}'"
    )

    expected = f"canal_springserve_{entity_id}.json"
    assert filename == expected, (
        f"Expected '{expected}', got '{filename}'"
    )


@settings(max_examples=100)
@given(config=_any_normalized_config())
def test_s3_key_from_normalized_config(config):
    """S3 key built from a normalized config has correct
    category and tipo prefix.

    **Validates: Requirements 2.4, 3.3, 4.3, 5.4**
    """
    tipo = config["tipo"]
    entity_id = _extract_entity_id(config)
    category = _category_for_tipo(tipo)
    key = build_s3_key(tipo, entity_id, category)

    assert key.startswith(KB_ADS_PREFIX)
    assert key.endswith(".json")

    after_prefix = key[len(KB_ADS_PREFIX):]
    parts = after_prefix.split("/", 1)
    assert len(parts) == 2
    cat, fname = parts
    assert cat == category
    assert fname.startswith(f"{tipo}_")


# ===================================================================
# Feature: springserve-ad-integration,
# Property 7: URL correlation identifies supply tags correctly
# ===================================================================
"""
Property 7: Correlação URL identifica corretamente supply tags.

For any ad_decision_server_url that contains a reference to an
existing supply_tag_id, the correlation function SHALL correctly
identify the supply_tag_id. URLs without supply tag references
SHALL result in empty correlation.

**Validates: Requirements 6.2, 6.6**
"""

from lambdas.pipeline_ads.handler import (  # noqa: E402
    _extract_supply_tag_id_from_url,
)

_supply_tag_ids = st.integers(min_value=1, max_value=99999)

_query_params = st.lists(
    st.tuples(
        st.sampled_from(["cb", "ts", "ref", "format"]),
        st.text(
            alphabet=st.characters(
                whitelist_categories=("L", "N"),
            ),
            min_size=1,
            max_size=10,
        ),
    ),
    min_size=0,
    max_size=3,
)


def _build_vast_url(tag_id, extra_params):
    """Build a /vast/{id} URL with optional query params."""
    base = (
        f"https://video.springserve.com/vast/{tag_id}"
    )
    if extra_params:
        qs = "&".join(
            f"{k}={v}" for k, v in extra_params
        )
        return f"{base}?{qs}"
    return base


def _build_query_param_url(tag_id, extra_params):
    """Build a URL with supply_tag_id as query parameter."""
    parts = [f"supply_tag_id={tag_id}"]
    for k, v in extra_params:
        parts.append(f"{k}={v}")
    qs = "&".join(parts)
    return (
        f"https://video.springserve.com/vast?{qs}"
    )


@settings(max_examples=100)
@given(
    tag_id=_supply_tag_ids,
    extra_params=_query_params,
)
def test_extract_supply_tag_from_vast_path(
    tag_id, extra_params
):
    """URLs with /vast/{id} pattern are correctly parsed.

    **Validates: Requirements 6.2, 6.6**
    """
    url = _build_vast_url(tag_id, extra_params)
    result = _extract_supply_tag_id_from_url(url)
    assert result == str(tag_id), (
        f"Expected '{tag_id}', got '{result}' "
        f"for URL: {url}"
    )


@settings(max_examples=100)
@given(
    tag_id=_supply_tag_ids,
    extra_params=_query_params,
)
def test_extract_supply_tag_from_query_param(
    tag_id, extra_params
):
    """URLs with supply_tag_id query param are correctly parsed.

    **Validates: Requirements 6.2, 6.6**
    """
    url = _build_query_param_url(tag_id, extra_params)
    result = _extract_supply_tag_id_from_url(url)
    assert result == str(tag_id), (
        f"Expected '{tag_id}', got '{result}' "
        f"for URL: {url}"
    )


_no_tag_urls = st.sampled_from([
    "https://example.com/ads/serve",
    "https://adserver.example.com/video?format=vast",
    "https://video.springserve.com/api/v1/supply_tags",
    "https://other.com/vast/",
    "",
    "https://example.com/path/to/resource",
    "https://adserver.example.com/?cb=12345",
])


@settings(max_examples=100)
@given(url=_no_tag_urls)
def test_no_supply_tag_returns_none(url):
    """URLs without supply tag references return None.

    **Validates: Requirements 6.2, 6.6**
    """
    result = _extract_supply_tag_id_from_url(url)
    assert result is None, (
        f"Expected None for URL '{url}', got '{result}'"
    )


# ===================================================================
# Feature: springserve-ad-integration,
# Property 6: resilience and summary accuracy
# ===================================================================
"""
Property 6: Pipeline é resiliente a falhas parciais e o resumo é preciso.

For any subset of resources that fail during collection, the pipeline
SHALL continue processing remaining resources, and the final summary
SHALL correctly reflect: total_stored + total_errors + total_skipped
== total_attempted.

**Validates: Requirements 10.3, 10.5**
"""

from lambdas.pipeline_ads.handler import _dual_write  # noqa: E402


def _make_s3_client(fail_indices):
    """Build a mock S3 client that fails on specific call indices.

    ``fail_indices`` is a set of 0-based call indices where
    put_object should raise an exception.
    """
    call_count = [0]

    def put_object(**kwargs):
        idx = call_count[0]
        call_count[0] += 1
        if idx in fail_indices:
            raise Exception(f"S3 put_object failed at index {idx}")

    client = MagicMock()
    client.put_object.side_effect = put_object
    return client


def _make_ddb_table(fail_indices):
    """Build a mock DynamoDB table that fails on specific call indices.

    ``fail_indices`` is a set of 0-based call indices where
    put_item should raise an exception.
    """
    call_count = [0]

    def put_item(**kwargs):
        idx = call_count[0]
        call_count[0] += 1
        if idx in fail_indices:
            raise Exception(f"DynamoDB put_item failed at index {idx}")

    table = MagicMock()
    table.put_item.side_effect = put_item
    return table


def _make_config(idx):
    """Build a minimal normalized config for testing dual_write."""
    return {
        "channel_id": f"supply_tag_{idx}",
        "servico": "SpringServe",
        "tipo": "supply_tag",
        "supply_tag_id": idx,
        "nome": f"tag_{idx}",
        "status": "active",
        "account_id": 1,
        "demand_tag_count": 0,
        "demand_tags": "",
        "demand_tag_ids": "",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-06-01T00:00:00Z",
    }


@settings(max_examples=100)
@given(
    num_items=st.integers(min_value=1, max_value=20),
    s3_fail_mask=st.lists(
        st.booleans(), min_size=1, max_size=20
    ),
)
def test_dual_write_resilience_and_summary_accuracy(
    num_items, s3_fail_mask
):
    """For any mix of successful and failing S3 operations,
    _dual_write continues processing all items and the summary
    satisfies: stored + errors + skipped == attempted.

    **Validates: Requirements 10.3, 10.5**
    """
    # Align mask length to num_items
    mask = s3_fail_mask[:num_items]
    while len(mask) < num_items:
        mask.append(False)

    fail_indices = {i for i, should_fail in enumerate(mask) if should_fail}

    s3_client = _make_s3_client(fail_indices)
    ddb_table = _make_ddb_table(set())  # DynamoDB always succeeds

    results = {
        "stored": 0,
        "errors": [],
        "skipped_validation": 0,
    }

    configs = [_make_config(i) for i in range(num_items)]

    # Process all items — failures must NOT stop processing
    for config in configs:
        _dual_write(s3_client, ddb_table, config, results)

    total_stored = results["stored"]
    total_errors = len(results["errors"])
    total_skipped = results["skipped_validation"]
    total_attempted = total_stored + total_errors + total_skipped

    # Invariant: stored + errors + skipped == attempted
    assert total_stored + total_errors + total_skipped == total_attempted, (
        f"Invariant violated: {total_stored} + {total_errors} + "
        f"{total_skipped} != {total_attempted}"
    )

    # All items were attempted (none skipped due to earlier failure)
    assert total_stored + total_errors == num_items, (
        f"Expected {num_items} total attempts, got "
        f"stored={total_stored} + errors={total_errors}"
    )

    # Stored count matches items that did NOT fail
    expected_stored = num_items - len(fail_indices)
    assert total_stored == expected_stored, (
        f"Expected {expected_stored} stored, got {total_stored}"
    )

    # Error count matches items that DID fail
    expected_errors = len(fail_indices)
    assert total_errors == expected_errors, (
        f"Expected {expected_errors} errors, got {total_errors}"
    )


@settings(max_examples=100)
@given(
    num_items=st.integers(min_value=1, max_value=20),
    ddb_fail_mask=st.lists(
        st.booleans(), min_size=1, max_size=20
    ),
)
def test_dual_write_ddb_failure_does_not_affect_s3_count(
    num_items, ddb_fail_mask
):
    """DynamoDB failures are fail-open: they do NOT reduce the
    stored count or add to errors (S3 write already succeeded).

    **Validates: Requirements 10.3, 10.5**
    """
    mask = ddb_fail_mask[:num_items]
    while len(mask) < num_items:
        mask.append(False)

    ddb_fail_indices = {
        i for i, should_fail in enumerate(mask) if should_fail
    }

    s3_client = _make_s3_client(set())  # S3 always succeeds
    ddb_table = _make_ddb_table(ddb_fail_indices)

    results = {
        "stored": 0,
        "errors": [],
        "skipped_validation": 0,
    }

    configs = [_make_config(i) for i in range(num_items)]

    for config in configs:
        _dual_write(s3_client, ddb_table, config, results)

    total_stored = results["stored"]
    total_errors = len(results["errors"])
    total_skipped = results["skipped_validation"]

    # All items stored successfully (DynamoDB is fail-open)
    assert total_stored == num_items, (
        f"Expected {num_items} stored, got {total_stored}"
    )

    # No errors recorded from DynamoDB failures
    assert total_errors == 0, (
        f"Expected 0 errors, got {total_errors}"
    )

    # Invariant holds
    total_attempted = total_stored + total_errors + total_skipped
    assert total_stored + total_errors + total_skipped == total_attempted


@settings(max_examples=100)
@given(
    num_items=st.integers(min_value=1, max_value=15),
    s3_fail_mask=st.lists(
        st.booleans(), min_size=1, max_size=15
    ),
    ddb_fail_mask=st.lists(
        st.booleans(), min_size=1, max_size=15
    ),
)
def test_dual_write_mixed_failures_summary_invariant(
    num_items, s3_fail_mask, ddb_fail_mask
):
    """With both S3 and DynamoDB failures, the summary invariant
    still holds and all items are attempted.

    **Validates: Requirements 10.3, 10.5**
    """
    s3_mask = s3_fail_mask[:num_items]
    while len(s3_mask) < num_items:
        s3_mask.append(False)

    ddb_mask = ddb_fail_mask[:num_items]
    while len(ddb_mask) < num_items:
        ddb_mask.append(False)

    s3_fail_indices = {
        i for i, fail in enumerate(s3_mask) if fail
    }
    ddb_fail_indices = {
        i for i, fail in enumerate(ddb_mask) if fail
    }

    s3_client = _make_s3_client(s3_fail_indices)
    ddb_table = _make_ddb_table(ddb_fail_indices)

    results = {
        "stored": 0,
        "errors": [],
        "skipped_validation": 0,
    }

    configs = [_make_config(i) for i in range(num_items)]

    for config in configs:
        _dual_write(s3_client, ddb_table, config, results)

    total_stored = results["stored"]
    total_errors = len(results["errors"])
    total_skipped = results["skipped_validation"]
    total_attempted = total_stored + total_errors + total_skipped

    # Core invariant
    assert total_stored + total_errors + total_skipped == total_attempted, (
        f"Invariant violated: {total_stored} + {total_errors} + "
        f"{total_skipped} != {total_attempted}"
    )

    # All items were attempted
    assert total_stored + total_errors == num_items, (
        f"Not all items attempted: stored={total_stored}, "
        f"errors={total_errors}, expected={num_items}"
    )

    # Stored = items where S3 succeeded
    expected_stored = num_items - len(s3_fail_indices)
    assert total_stored == expected_stored, (
        f"Expected {expected_stored} stored, got {total_stored}"
    )
