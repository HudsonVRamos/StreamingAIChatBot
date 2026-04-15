from __future__ import annotations

import json
import os
import uuid
import logging

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ReadTimeoutError, ClientError, EventStreamError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT_ID = os.environ.get("AGENT_ID", "")
AGENT_ALIAS_ID = os.environ.get("AGENT_ALIAS_ID", "")

bedrock_agent_runtime = boto3.client(
    "bedrock-agent-runtime",
    config=BotoConfig(read_timeout=300, connect_timeout=10),
)

CORS_HEADERS = {
    "Content-Type": "application/json",
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


def _invoke_agent(pergunta: str, session_id: str = "") -> str:
    """Invoke the Bedrock Agent and collect the streaming response."""
    if not session_id:
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


def _handle_export(body):
    """Direct export — calls Exportadora Lambda and returns CSV/JSON content."""
    try:
        lambda_client = boto3.client("lambda")
        export_fn = os.environ.get("EXPORT_FUNCTION_NAME", "")

        if not export_fn:
            return _response(500, {"erro": "EXPORT_FUNCTION_NAME not configured"})

        # Build the Action Group event format
        filtros = body.get("filtros", {})
        formato = body.get("formato", "CSV")
        api_path = body.get("api_path", "/exportarConfiguracoes")

        payload = {
            "actionGroup": "direct_export",
            "apiPath": api_path,
            "httpMethod": "POST",
            "requestBody": {
                "content": {
                    "application/json": {
                        "properties": [
                            {"name": "filtros", "value": json.dumps(filtros)},
                            {"name": "formato", "value": formato},
                        ]
                    }
                }
            },
        }

        resp = lambda_client.invoke(
            FunctionName=export_fn,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )

        result = json.loads(resp["Payload"].read())
        resp_body = result.get("response", {}).get("responseBody", {})
        inner = resp_body.get("application/json", {}).get("body", "{}")
        data = json.loads(inner) if isinstance(inner, str) else inner

        # Return the exported content directly
        return _response(200, {
            "resposta": data.get("mensagem", "Exportação concluída"),
            "dados_exportados": data.get("dados_exportados"),
            "formato": data.get("formato_arquivo", formato.lower()),
            "resumo": data.get("resumo"),
        })

    except Exception as e:
        logger.error("Export error: %s", e)
        return _response(500, {"erro": f"Erro na exportação: {e}"})


def _handle_export_download(body):
    """Download an export file by invoking the exportadora Lambda."""
    try:
        lambda_client = boto3.client("lambda")
        export_fn = os.environ.get("EXPORT_FUNCTION_NAME", "")

        if not export_fn:
            return _response(500, {"erro": "EXPORT_FUNCTION_NAME not configured"})

        filename = body.get("filename", "")
        if not filename:
            return _response(400, {"erro": "filename é obrigatório"})

        logger.info("Export download requested: filename=%s", filename)

        # Call exportadora with a special download action
        payload = {
            "actionGroup": "direct_export_download",
            "apiPath": "/downloadExport",
            "httpMethod": "POST",
            "requestBody": {
                "content": {
                    "application/json": {
                        "properties": [
                            {"name": "filename", "value": filename},
                        ]
                    }
                }
            },
        }

        resp = lambda_client.invoke(
            FunctionName=export_fn,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )

        result = json.loads(resp["Payload"].read())
        logger.info("Export download result keys: %s", list(result.keys()) if isinstance(result, dict) else type(result))
        resp_body = (
            result.get("response", {})
            .get("responseBody", {})
        )
        inner = (
            resp_body
            .get("application/json", {})
            .get("body", "{}")
        )
        data = json.loads(inner) if isinstance(inner, str) else inner
        logger.info("Export download data keys: %s, has dados_exportados: %s",
                     list(data.keys()) if isinstance(data, dict) else type(data),
                     "dados_exportados" in data if isinstance(data, dict) else False)

        if data.get("erro"):
            logger.error("Export download error from exportadora: %s", data["erro"])
            return _response(500, {"erro": data["erro"]})

        content = data.get("dados_exportados")
        if not content:
            return _response(500, {"erro": "Arquivo vazio ou não encontrado"})

        return _response(200, {
            "dados_exportados": content,
            "formato": data.get("formato", "csv"),
            "arquivo": filename,
        })

    except Exception as e:
        logger.error("Export download error: %s", e)
        return _response(500, {"erro": f"Erro ao baixar exportação: {e}"})


def _handle_config_download(body):
    """Direct config download — calls Configuradora to get full JSON."""
    try:
        lambda_client = boto3.client("lambda")
        config_fn = os.environ.get("CONFIG_FUNCTION_NAME", "")

        if not config_fn:
            return _response(500, {
                "erro": "CONFIG_FUNCTION_NAME not configured",
            })

        servico = body.get("servico", "MediaLive")
        tipo_recurso = body.get("tipo_recurso", "channel")
        resource_id = body.get("resource_id", "")

        payload = {
            "actionGroup": "direct_config",
            "apiPath": "/obterConfiguracao",
            "httpMethod": "POST",
            "requestBody": {
                "content": {
                    "application/json": {
                        "properties": [
                            {"name": "servico", "value": servico},
                            {"name": "tipo_recurso", "value": tipo_recurso},
                            {"name": "resource_id", "value": resource_id},
                        ]
                    }
                }
            },
        }

        resp = lambda_client.invoke(
            FunctionName=config_fn,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )

        result = json.loads(resp["Payload"].read())
        resp_body = (
            result.get("response", {})
            .get("responseBody", {})
        )
        inner = (
            resp_body
            .get("application/json", {})
            .get("body", "{}")
        )
        data = json.loads(inner) if isinstance(inner, str) else inner

        return _response(200, {
            "resposta": data.get("mensagem", ""),
            "dados_exportados": data.get("dados_exportados"),
            "formato": data.get("formato_arquivo", "json"),
            "resumo": data.get("resumo"),
        })

    except Exception as e:
        logger.error("Config download error: %s", e)
        return _response(500, {
            "erro": f"Erro ao obter configuração: {e}",
        })


def _handle_healthcheck(body):
    """Direct health check — calls Configuradora's gerenciarRecurso with acao=healthcheck."""
    try:
        lambda_client = boto3.client(
            "lambda",
            config=BotoConfig(read_timeout=310, connect_timeout=10),
        )
        config_fn = os.environ.get("CONFIG_FUNCTION_NAME", "")

        if not config_fn:
            return _response(500, {"erro": "CONFIG_FUNCTION_NAME not configured"})

        servico = body.get("servico", "")
        periodo = body.get("periodo_minutos", 15)

        properties = [
            {"name": "acao", "value": "healthcheck"},
            {"name": "periodo_minutos", "value": str(periodo)},
        ]
        if servico:
            properties.append({"name": "servico", "value": servico})

        payload = {
            "actionGroup": "direct_healthcheck",
            "apiPath": "/gerenciarRecurso",
            "httpMethod": "POST",
            "requestBody": {
                "content": {
                    "application/json": {
                        "properties": properties,
                    }
                }
            },
        }

        resp = lambda_client.invoke(
            FunctionName=config_fn,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )

        result = json.loads(resp["Payload"].read())
        status = result.get("response", {}).get("httpStatusCode", 200)
        resp_body = (
            result.get("response", {})
            .get("responseBody", {})
        )
        inner = (
            resp_body
            .get("application/json", {})
            .get("body", "{}")
        )
        data = (
            json.loads(inner)
            if isinstance(inner, str)
            else inner
        )

        return _response(status, {
            "healthcheck": True,
            "dashboard": data,
        })

    except Exception as e:
        logger.error("Healthcheck error: %s", e)
        return _response(500, {"erro": f"Erro no health check: {e}"})


def _handle_metrics_query(body):
    """Direct metrics query — calls Configuradora's consultarMetricas."""
    try:
        lambda_client = boto3.client("lambda")
        config_fn = os.environ.get("CONFIG_FUNCTION_NAME", "")

        if not config_fn:
            return _response(500, {
                "erro": "CONFIG_FUNCTION_NAME not configured",
            })

        servico = body.get("servico", "MediaLive")
        resource_id = body.get("resource_id", "")
        periodo = body.get("periodo_minutos", 60)
        granularidade = body.get("granularidade_segundos", 300)
        metricas = body.get("metricas")

        properties = [
            {"name": "servico", "value": servico},
            {"name": "resource_id", "value": resource_id},
            {"name": "periodo_minutos", "value": str(periodo)},
            {
                "name": "granularidade_segundos",
                "value": str(granularidade),
            },
        ]
        if metricas:
            properties.append({
                "name": "metricas",
                "value": json.dumps(metricas),
            })

        payload = {
            "actionGroup": "direct_metrics",
            "apiPath": "/consultarMetricas",
            "httpMethod": "POST",
            "requestBody": {
                "content": {
                    "application/json": {
                        "properties": properties,
                    }
                }
            },
        }

        resp = lambda_client.invoke(
            FunctionName=config_fn,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )

        result = json.loads(resp["Payload"].read())
        status = result.get("response", {}).get(
            "httpStatusCode", 200,
        )
        resp_body = (
            result.get("response", {})
            .get("responseBody", {})
        )
        inner = (
            resp_body
            .get("application/json", {})
            .get("body", "{}")
        )
        data = (
            json.loads(inner)
            if isinstance(inner, str)
            else inner
        )

        return _response(status, {
            "resposta": data.get("mensagem", ""),
            "metrics_chart_data": data.get("resumo"),
            "recurso": data.get("recurso"),
            "servico": data.get("servico"),
            "periodo": data.get("periodo"),
            "multiplos_resultados": data.get(
                "multiplos_resultados",
            ),
            "candidatos": data.get("candidatos"),
        })

    except Exception as e:
        logger.error("Metrics query error: %s", e)
        return _response(500, {
            "erro": f"Erro ao consultar métricas: {e}",
        })


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
    if not http_method:
        http_method = (event.get("requestContext", {})
                       .get("http", {}).get("method", ""))
    if http_method.upper() == "OPTIONS":
        return _response(200, {})

    # Parse body
    raw_body = event.get("body")
    try:
        body = json.loads(raw_body) if isinstance(raw_body, str) else (raw_body or {})
    except (json.JSONDecodeError, TypeError):
        body = {}

    # Direct export route — bypasses Bedrock agent
    if body.get("download"):
        return _handle_export(body)

    # Direct config download — bypasses Bedrock agent
    if body.get("download_config"):
        return _handle_config_download(body)

    # Direct export file download — reads from S3 exports bucket
    if body.get("download_export"):
        return _handle_export_download(body)

    # Direct metrics query — bypasses Bedrock agent
    if body.get("consultar_metricas"):
        return _handle_metrics_query(body)

    # Direct health check — bypasses Bedrock agent
    if body.get("healthcheck"):
        return _handle_healthcheck(body)

    # Validate input
    try:
        pergunta = _validate_pergunta(event)
    except ValueError as e:
        logger.warning("Invalid request: %s", e)
        return _response(400, {"erro": str(e)})

    # Invoke Bedrock Agent
    try:
        session_id = body.get("session_id", "")
        resposta = _invoke_agent(pergunta, session_id)

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
