# AWS Resources — Nomes Reais (us-east-1)

Account: 761018874615

---

## S3 Buckets

| Nome | Descrição |
|---|---|
| streamingchatbotstack-kbconfigbucketfcbd41b2-tddvqk3i9s4f | Configurações dos canais (kb-config/) |
| streamingchatbotstack-kblogsbucket3e8a8f80-eftv7ne3zk4u | Logs e métricas (kb-logs/) |
| streamingchatbotstack-auditbucketb01e0ae8-xiaicda5vgfy | Auditoria de operações |
| streamingchatbotstack-exportsbucket5a12738b-ipvwkzat4dbg | Exportações temporárias (24h) |
| streamingchatbotstack-frontendbucketefe2e19c-cm6ovgye8prh | Frontend estático |

---

## DynamoDB Tables

| Nome | Descrição |
|---|---|
| StreamingChatbotStack-StreamingConfigs49C3BEE3-1L2E79YK6U5QU | Configurações dos canais (GSI: GSI_NomeCanal) |
| StreamingChatbotStack-StreamingLogsBE97608D-128FHQRIR52Y5 | Logs e métricas (GSI: GSI_Severidade, TTL ativo) |

---

## Lambda Functions

| Nome | Descrição |
|---|---|
| StreamingChatbotStack-OrquestradoraFunctionC93F4B4-9i6FWg7EVPqV | Orquestradora — invoca Bedrock Agent |
| StreamingChatbotStack-ConfiguradoraFunction8C3D631-iH4bsa38s3jZ | Configuradora — CRUD MediaLive/MediaPackage/MediaTailor/CloudFront |
| StreamingChatbotStack-ExportadoraFunctionF7DCB910-tR185Y8NQSVn | Exportadora — gera CSV/JSON com presigned URL |
| StreamingChatbotStack-PipelineConfigFunction079AFC-k9oe8dUaRswq | Pipeline Config — coleta configs a cada 6h |
| StreamingChatbotStack-PipelineLogsFunctionE340BB88-5SkoNySBybw4 | Pipeline Logs — coleta métricas a cada 1h |
| StreamingChatbotPipelineA-PipelineAdsFunctionB6C11-hAyAMXXJRxcy | Pipeline Ads — orquestra supply tags SpringServe a cada 6h |

---

## API Gateway

| ID | Nome |
|---|---|
| czy293objk | StreamingChatbotApi (POST /chat) |

---

## CloudFront

| ID | Descrição |
|---|---|
| E3HP1WD4UAEZYF | Frontend Distribution — origem S3 via OAC |

---

## Cognito

| Recurso | ID / Nome |
|---|---|
| User Pool | us-east-1_mvuyZ5ERc |
| App Client | 5r2hg6aag4iqbe84ldj8e0k9g |

---

## SNS

| ARN |
|---|
| arn:aws:sns:us-east-1:761018874615:StreamingAlertsNotifications |

---

## Secrets Manager

| ARN |
|---|
| arn:aws:secretsmanager:us-east-1:761018874615:secret:springserve/api-credentials-B1tjHF |

---

## Bedrock (criado manualmente)

| Recurso | ID |
|---|---|
| Agent ID | AM81CBRWNE |
| Agent Alias | TSTALIASID |
| Knowledge Base Config | KB_CONFIG |
| Knowledge Base Logs | KB_LOGS |
