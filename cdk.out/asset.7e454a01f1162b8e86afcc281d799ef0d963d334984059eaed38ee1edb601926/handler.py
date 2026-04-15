from __future__ import annotations

import json
import os
import uuid
import logging

import boto3
from botocore.exceptions import ReadTimeoutError, ClientError, EventStreamError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT_ID = os.environ.get("AGENT_ID", "")
AGENT_ALIAS_ID = os.environ.get("AGENT_ALIAS_ID", "")

bedrock_agent_runtime = boto3.client("bedrock-agent-runtime")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST,GET,OPTIONS",
}


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _validate_pergunta(event: dict) -> str | None:
    """Extract and validate the 'pergunta' field from the event body.

    Returns the trimmed pergunta string, or None if invalid.
    Raises ValueError with a descriptive message on validation failure.
    """
    raw_body = event.get("body")
    if raw_body is None:
        raise ValueError("Request body is missing")

    try:
        body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
    except (json.JSONDecodeError, TypeError):
        raise ValueError("Request body is not valid JSON")

    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")

    if "pergunta" not in body:
        raise ValueError("Campo 'pergunta' é obrigatório")

    pergunta = body["pergunta"]

    if pergunta is None:
        raise ValueError("Campo 'pergunta' não pode ser null")

    if not isinstance(pergunta, str):
        raise ValueError("Campo 'pergunta' deve ser uma string")

    pergunta = pergunta.strip()
    if not pergunta:
        raise ValueError("Campo 'pergunta' não pode ser vazio")

    return pergunta


def _invoke_agent(pergunta: str) -> str:
    """Invoke the Bedrock Agent and collect the streaming response."""
    session_id = str(uuid.uuid4())

    response = bedrock_agent_runtime.invoke_agent(
        agentId=AGENT_ID,
        agentAliasId=AGENT_ALIAS_ID,
        sessionId=session_id,
        inputText=pergunta,
    )

    completion = response.get("completion", [])
    chunks = []
    for event in completion:
        if "chunk" in event:
            chunk_bytes = event["chunk"].get("bytes", b"")
            if chunk_bytes:
                chunks.append(chunk_bytes.decode("utf-8"))

    return "".join(chunks)


def _is_timeout_error(exc: Exception) -> bool:
    """Check if an exception represents a timeout condition."""
    if isinstance(exc, ReadTimeoutError):
        return True
    if isinstance(exc, EventStreamError):
        msg = str(exc).lower()
        if "timeout" in msg:
            return True
    return False


def handler(event, context):
    """Lambda handler for API Gateway and Function URL."""
    # Handle CORS preflight
    http_method = event.get("httpMethod", "")
    # Function URL uses requestContext.http.method
    if not http_method:
        http_method = (event.get("requestContext", {})
                       .get("http", {}).get("method", ""))
    if http_method.upper() == "OPTIONS":
        return _response(200, {})

    # Validate input
    try:
        pergunta = _validate_pergunta(event)
    except ValueError as e:
        logger.warning("Invalid request: %s", e)
        return _response(400, {"erro": str(e)})

    # Invoke Bedrock Agent
    try:
        resposta = _invoke_agent(pergunta)

        if not resposta:
            resposta = "Não foi possível obter uma resposta do agente."

        return _response(200, {"resposta": resposta})

    except (ReadTimeoutError, EventStreamError) as e:
        if _is_timeout_error(e):
            logger.error("Timeout invoking Bedrock Agent: %s", e)
            return _response(504, {"erro": "Tempo limite excedido ao processar a pergunta"})
        logger.error("EventStream error invoking Bedrock Agent: %s", e)
        return _response(500, {"erro": "Erro interno ao processar a pergunta"})

    except ClientError as e:
        logger.error("AWS ClientError invoking Bedrock Agent: %s", e)
        return _response(500, {"erro": "Erro interno ao processar a pergunta"})

    except Exception as e:
        logger.error("Unexpected error: %s", e)
        return _response(500, {"erro": "Erro interno ao processar a pergunta"})
