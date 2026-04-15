# Unit tests for Lambda_Orquestradora
"""Unit tests for Lambda_Orquestradora handler.

Validates: Requirements 2.2, 2.3, 2.5

Tests cover:
- Valid question returns HTTP 200 with "resposta" field
- Bedrock Agent timeout (ReadTimeoutError) returns HTTP 504
- EventStreamError with timeout message returns HTTP 504
- Empty agent response returns informative message
- ClientError returns HTTP 500
- CORS preflight (OPTIONS) returns HTTP 200
"""

import json
from unittest.mock import MagicMock, patch

from botocore.exceptions import (
    ClientError,
    EventStreamError,
    ReadTimeoutError,
)

# Patch boto3.client at import time (same pattern as property tests)
with patch("boto3.client", return_value=MagicMock()):
    from lambdas.orquestradora.handler import handler

DUMMY_CONTEXT = None


def _api_gw_event(body: dict, method: str = "POST") -> dict:
    """Build an API Gateway proxy integration event."""
    return {
        "httpMethod": method,
        "body": json.dumps(body),
    }


# ------------------------------------------------------------------
# 1. Valid pergunta returns HTTP 200
# ------------------------------------------------------------------
@patch("lambdas.orquestradora.handler.bedrock_agent_runtime")
def test_valid_pergunta_returns_200(mock_bedrock):
    """A valid question should invoke the agent and return HTTP 200
    with a JSON body containing the 'resposta' field."""
    mock_bedrock.invoke_agent.return_value = {
        "completion": [
            {"chunk": {"bytes": "Olá, ".encode("utf-8")}},
            {"chunk": {"bytes": "mundo!".encode("utf-8")}},
        ]
    }

    event = _api_gw_event({"pergunta": "Qual a configuração do canal 1057?"})
    response = handler(event, DUMMY_CONTEXT)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert "resposta" in body
    assert body["resposta"] == "Olá, mundo!"
    mock_bedrock.invoke_agent.assert_called_once()


# ------------------------------------------------------------------
# 2. ReadTimeoutError returns HTTP 504
# ------------------------------------------------------------------
@patch("lambdas.orquestradora.handler.bedrock_agent_runtime")
def test_timeout_returns_504(mock_bedrock):
    """When invoke_agent raises ReadTimeoutError the handler
    should return HTTP 504 (Gateway Timeout)."""
    mock_bedrock.invoke_agent.side_effect = ReadTimeoutError(
        endpoint_url="https://bedrock.us-east-1.amazonaws.com"
    )

    event = _api_gw_event({"pergunta": "Quais canais estão com low latency?"})
    response = handler(event, DUMMY_CONTEXT)

    assert response["statusCode"] == 504
    body = json.loads(response["body"])
    assert "erro" in body


# ------------------------------------------------------------------
# 3. EventStreamError with "timeout" returns HTTP 504
# ------------------------------------------------------------------
@patch("lambdas.orquestradora.handler.bedrock_agent_runtime")
def test_event_stream_timeout_returns_504(mock_bedrock):
    """When invoke_agent raises EventStreamError whose message
    contains 'timeout', the handler should return HTTP 504."""
    mock_bedrock.invoke_agent.side_effect = EventStreamError(
        error_response={
            "Error": {
                "Code": "RequestTimeout",
                "Message": "The request timeout limit was exceeded",
            }
        },
        operation_name="InvokeAgent",
    )

    event = _api_gw_event({"pergunta": "Relatório do canal 1060"})
    response = handler(event, DUMMY_CONTEXT)

    assert response["statusCode"] == 504
    body = json.loads(response["body"])
    assert "erro" in body


# ------------------------------------------------------------------
# 4. Empty agent response returns informative message
# ------------------------------------------------------------------
@patch("lambdas.orquestradora.handler.bedrock_agent_runtime")
def test_empty_agent_response_returns_informative_message(mock_bedrock):
    """When the agent returns an empty completion the handler should
    return HTTP 200 with an informative fallback message."""
    mock_bedrock.invoke_agent.return_value = {"completion": []}

    event = _api_gw_event({"pergunta": "Algo sem resposta"})
    response = handler(event, DUMMY_CONTEXT)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert "resposta" in body
    # The fallback message should be non-empty and informative
    assert len(body["resposta"]) > 0
    assert body["resposta"] != ""


# ------------------------------------------------------------------
# 5. ClientError returns HTTP 500
# ------------------------------------------------------------------
@patch("lambdas.orquestradora.handler.bedrock_agent_runtime")
def test_client_error_returns_500(mock_bedrock):
    """When invoke_agent raises a ClientError the handler should
    return HTTP 500."""
    mock_bedrock.invoke_agent.side_effect = ClientError(
        error_response={
            "Error": {
                "Code": "AccessDeniedException",
                "Message": "Access denied",
            }
        },
        operation_name="InvokeAgent",
    )

    event = _api_gw_event({"pergunta": "Quais erros aconteceram hoje?"})
    response = handler(event, DUMMY_CONTEXT)

    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert "erro" in body


# ------------------------------------------------------------------
# 6. OPTIONS (CORS preflight) returns HTTP 200
# ------------------------------------------------------------------
@patch("lambdas.orquestradora.handler.bedrock_agent_runtime")
def test_options_returns_200(mock_bedrock):
    """An OPTIONS request (CORS preflight) should return HTTP 200
    with CORS headers and no agent invocation."""
    event = {"httpMethod": "OPTIONS"}
    response = handler(event, DUMMY_CONTEXT)

    assert response["statusCode"] == 200
    assert "Access-Control-Allow-Origin" in response["headers"]
    assert "Access-Control-Allow-Methods" in response["headers"]
    mock_bedrock.invoke_agent.assert_not_called()
