# Feature: streaming-chatbot, Property 3: Rejeição de payloads inválidos
"""Property-based tests for Lambda_Orquestradora invalid payload rejection.

**Validates: Requirements 2.4**

For any JSON payload that does not contain the "pergunta" field as a
non-empty string (including missing field, null, empty string,
whitespace-only string, or incorrect type), the Lambda_Orquestradora
must return HTTP 400 with a descriptive error message containing "erro".
"""

import json
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import given, settings

# The handler module creates a boto3 client at import time.
# Patch it so the import succeeds even without real AWS credentials
# or a botocore version that knows about bedrock-agent-runtime.
with patch("boto3.client", return_value=MagicMock()):
    from lambdas.orquestradora.handler import handler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DUMMY_CONTEXT = None


def _api_gw_event(payload: dict) -> dict:
    """Wrap a payload dict into an API Gateway proxy integration event."""
    return {
        "httpMethod": "POST",
        "body": json.dumps(payload),
    }


def _assert_400_with_erro(response: dict) -> None:
    """Assert the response is HTTP 400 and body contains 'erro'."""
    assert response["statusCode"] == 400, (
        f"Expected 400, got {response['statusCode']}"
    )
    body = json.loads(response["body"])
    assert "erro" in body, f"Response body missing 'erro' field: {body}"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Strategy: random dicts that never contain the key "pergunta"
_dict_without_pergunta = st.dictionaries(
    keys=st.text().filter(lambda k: k != "pergunta"),
    values=st.text(),
    min_size=0,
    max_size=5,
)

# Strategy: non-string types for "pergunta"
_non_string_types = st.one_of(
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(keys=st.text(max_size=3), values=st.integers(), max_size=3),
)

# Strategy: whitespace-only strings (spaces, tabs, newlines)
_whitespace_only = st.from_regex(r"^[\s]+$", fullmatch=True)


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(payload=_dict_without_pergunta)
@patch("lambdas.orquestradora.handler.bedrock_agent_runtime")
def test_missing_pergunta_field_returns_400(mock_bedrock, payload):
    """Payloads without the 'pergunta' key must be rejected with HTTP 400."""
    event = _api_gw_event(payload)
    response = handler(event, DUMMY_CONTEXT)
    _assert_400_with_erro(response)


@settings(max_examples=100)
@given(data=st.just(None))
@patch("lambdas.orquestradora.handler.bedrock_agent_runtime")
def test_null_pergunta_returns_400(mock_bedrock, data):
    """A payload with 'pergunta' set to null must be rejected with HTTP 400."""
    event = _api_gw_event({"pergunta": data})
    response = handler(event, DUMMY_CONTEXT)
    _assert_400_with_erro(response)


@settings(max_examples=100)
@given(data=st.just(""))
@patch("lambdas.orquestradora.handler.bedrock_agent_runtime")
def test_empty_string_pergunta_returns_400(mock_bedrock, data):
    """A payload with 'pergunta' as empty string must be rejected with HTTP 400."""
    event = _api_gw_event({"pergunta": data})
    response = handler(event, DUMMY_CONTEXT)
    _assert_400_with_erro(response)


@settings(max_examples=100)
@given(ws=_whitespace_only)
@patch("lambdas.orquestradora.handler.bedrock_agent_runtime")
def test_whitespace_only_pergunta_returns_400(mock_bedrock, ws):
    """A payload with 'pergunta' as whitespace-only must be rejected with HTTP 400."""
    event = _api_gw_event({"pergunta": ws})
    response = handler(event, DUMMY_CONTEXT)
    _assert_400_with_erro(response)


@settings(max_examples=100)
@given(value=_non_string_types)
@patch("lambdas.orquestradora.handler.bedrock_agent_runtime")
def test_non_string_pergunta_returns_400(mock_bedrock, value):
    """A payload with 'pergunta' as a non-string type must be rejected with HTTP 400."""
    event = _api_gw_event({"pergunta": value})
    response = handler(event, DUMMY_CONTEXT)
    _assert_400_with_erro(response)
