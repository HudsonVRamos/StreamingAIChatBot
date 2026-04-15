# Roteiro para Apresentação em PowerPoint
# Plataforma de Streaming Inteligente com IA (Chatbot NOC)

---

## SLIDE 1 — Capa

**Título**: Plataforma de Gerenciamento Inteligente de Canais de Streaming com IA
**Subtítulo**: Chatbot para operadores NOC com Amazon Bedrock, MediaLive, MediaPackage V2, MediaTailor e CloudFront
**Elementos visuais**: Logo AWS, ícone de chat/IA, ícone de streaming

---

## SLIDE 2 — Problema / Contexto

**Título**: O Desafio

**Conteúdo**:
- Operadores NOC precisam gerenciar ~220 canais de streaming ao vivo
- Consultar configurações, logs e métricas exige navegar múltiplos consoles AWS
- Criação de canais envolve 4+ serviços com configurações interdependentes
- Diagnóstico de problemas requer correlação manual de dados de diferentes fontes

**Nota para o slide**: Use ícones representando complexidade (múltiplas telas, relógios, alertas)

---

## SLIDE 3 — Solução Proposta

**Título**: A Solução — Chatbot Inteligente para NOC

**Conteúdo**:
- Interface conversacional em linguagem natural (português)
- Consulta inteligente via RAG (Retrieval-Augmented Generation) sobre configurações e métricas
- Criação automatizada de canais completos com orquestração e rollback
- Exportação de dados em CSV/JSON com links pré-assinados
- Ingestão automática de configurações (6h) e métricas CloudWatch (1h)

**Nota para o slide**: Coloque um screenshot da interface do chat (tema escuro, sidebar com sugestões)

---

## SLIDE 4 — Arquitetura Geral (Visão Alto Nível)

**Título**: Arquitetura da Solução

**Conteúdo**: Usar o diagrama arquitetural (ver arquivo `diagrama-arquitetura.md`)

**Elementos principais a mostrar**:
- Frontend (S3 + CloudFront + Cognito)
- API Gateway / Lambda Function URL
- Lambda Orquestradora → Bedrock Agent
- 2 Knowledge Bases (Config + Logs/Métricas)
- Lambdas especializadas (Configuradora, Exportadora)
- Pipelines de ingestão (EventBridge → Lambdas → S3)
- Serviços de streaming (MediaLive, MediaPackage V2, MediaTailor, CloudFront)

**Nota para o slide**: Diagrama visual com setas mostrando fluxo. Use cores diferentes para cada camada.

---

## SLIDE 5 — Stack Tecnológica

**Título**: Tecnologias Utilizadas

**Conteúdo em colunas**:

| Camada | Tecnologia |
|---|---|
| Frontend | HTML/JS, S3, CloudFront |
| Autenticação | Amazon Cognito (User Pool) |
| API | API Gateway REST + Lambda Function URL |
| IA/ML | Amazon Bedrock (Claude), RAG com Knowledge Bases |
| Compute | AWS Lambda (Python) |
| Armazenamento | Amazon S3 (5 buckets) + DynamoDB (consultas rápidas) |
| Streaming | MediaLive, MediaPackage V2, MediaTailor |
| CDN | Amazon CloudFront |
| Orquestração | EventBridge Scheduler |
| Notificações | Amazon SNS (alertas proativos) |
| IaC | AWS CDK (Python) — 10 stacks modulares |
| Monitoramento | CloudWatch Metrics |

---

## SLIDE 6 — Funcionalidade 1: Chatbot Conversacional

**Título**: Chatbot com RAG — Consultas Inteligentes

**Conteúdo**:
- Operador faz perguntas em linguagem natural
- Bedrock Agent consulta 2 Knowledge Bases:
  - KB_CONFIG: ~220 configurações de canais (JSON normalizado)
  - KB_LOGS: Eventos estruturados de métricas CloudWatch
- 45+ sugestões categorizadas na sidebar (MediaLive, MediaPackage, MediaTailor, CloudFront, Exportações, Criação, Conceitos)
- Respostas contextualizadas com dados reais dos canais

**Exemplos de perguntas**:
- "Quais canais estão com resolução 1080p?"
- "Mostre os erros críticos das últimas 24h"
- "Qual a configuração do canal X?"

**Nota para o slide**: Screenshot da interface com exemplo de conversa

---

## SLIDE 7 — Funcionalidade 2: Criação Orquestrada de Canais

**Título**: Criação Automatizada de Canais (4 Etapas)

**Conteúdo — Fluxo em 4 passos**:
1. Criar Canal MediaPackage V2 (Channel Group + Channel)
2. Criar Endpoints HLS/DASH no MediaPackage V2
3. Criar Inputs no MediaLive (RTMP/RTP com failover)
4. Criar Canal MediaLive (vinculado aos inputs e endpoints)

**Destaques**:
- Coleta conversacional de parâmetros (codec, resolução, bitrate, etc.)
- Rollback automático em caso de falha em qualquer etapa
- Registro de auditoria de todas as operações
- Suporte a failover automático entre inputs

**Nota para o slide**: Diagrama de fluxo vertical com as 4 etapas e seta de rollback

---

## SLIDE 8 — Funcionalidade 3: Ingestão de Métricas CloudWatch

**Título**: Pipeline de Ingestão de Métricas e Configurações

**Conteúdo**:

**Pipeline de Configurações (a cada 6h)**:
- Coleta paralela de configs de MediaLive, MediaPackage V2, MediaTailor, CloudFront
- Normalização para JSON flat (sem aninhamento)
- Validação de campos obrigatórios
- Armazenamento em S3_KBConfig

**Pipeline de Métricas (a cada 1h)**:
- Coleta de métricas CloudWatch dos 4 serviços
- Classificação de severidade: INFO / WARNING / ERROR / CRITICAL
- Geração de Evento_Estruturado com: timestamp, canal, severidade, tipo_erro, descrição, causa provável, recomendação
- Armazenamento em S3_KBLogs

**Nota para o slide**: Diagrama com EventBridge → Lambda → APIs AWS → S3

---

## SLIDE 9 — Modelo de Dados

**Título**: Estrutura de Dados

**Conteúdo em 3 blocos**:

**Config_Enriquecida (JSON flat)**:
```
channel_id, servico, tipo, nome_canal, codec_video,
resolucoes, bitrates, audio_pids, caption_pids,
failover_settings, estado, regiao...
```

**Evento_Estruturado (Métricas)**:
```
timestamp, canal, severidade, tipo_erro,
descricao, causa_provavel, recomendacao_correcao,
servico_origem
```

**DynamoDB Tables**:
```
StreamingConfigs: PK={servico}#{tipo}, SK={resource_id}
  GSI_NomeCanal: PK=servico, SK=nome_canal
  ~285 items, point-in-time recovery

StreamingLogs: PK={servico}#{canal}, SK={timestamp}#{metrica}
  GSI_Severidade: PK=severidade, SK=SK
  TTL 30 dias, ~1.74M items/mês
```

**Nota para o slide**: Mostrar exemplo visual de cada JSON e schema DynamoDB

---

## SLIDE 10 — Fluxos de Dados

**Título**: Fluxos Principais

**3 fluxos para ilustrar**:

1. **Fluxo de Consulta**: Usuário → Frontend → Lambda Function URL (5min timeout) → Bedrock Agent → KB → Resposta
2. **Fluxo de Exportação**: Usuário pede export → Bypass direto → Lambda Exportadora → Leitura paralela S3 → CSV/JSON → URL pré-assinada
3. **Fluxo de Criação**: Usuário pede canal → Bedrock coleta parâmetros → Lambda Configuradora → 4 etapas → Rollback se falha

**Nota para o slide**: 3 diagramas de sequência simplificados lado a lado

---

## SLIDE 11 — Infraestrutura como Código (CDK)

**Título**: Infraestrutura com AWS CDK (Python)

**Conteúdo**:
- 9 stacks CDK modulares:
  - main_stack (orquestração geral)
  - api_stack (API Gateway + Lambda)
  - bedrock_agent_stack (Agente + instruções em PT-BR)
  - bedrock_kb_stack (Knowledge Bases)
  - s3_stack (5 buckets com lifecycle policies)
  - configuradora_stack, exportadora_stack
  - pipeline_config_stack, pipeline_logs_stack
  - frontend_stack (S3 + CloudFront)

- Deploy multi-região: us-east-1 (API/Bedrock/Cognito) + sa-east-1 (streaming)

**Nota para o slide**: Diagrama de dependência entre stacks

---

## SLIDE 12 — Segurança e Autenticação

**Título**: Segurança

**Conteúdo**:
- Amazon Cognito User Pool (self-signup desabilitado, criação admin-only)
- Autenticação via token JWT no frontend
- IAM roles com least privilege para cada Lambda
- S3 buckets com lifecycle policies (exports: 24h, audit: 365 dias)
- CloudFront com OAI para acesso ao S3
- Auditoria completa de operações no S3_Audit

---

## SLIDE 13 — Custos Estimados

**Título**: Estimativa de Custos Mensais

| Serviço | Custo Estimado |
|---|---|
| Amazon Bedrock (Claude) | $30 - $100 |
| Amazon S3 (5 buckets) | $1 - $5 |
| Amazon DynamoDB (2 tabelas + GSIs) | ~$3 |
| AWS Lambda | $0 - $5 |
| Amazon CloudFront | $1 - $5 |
| SNS + Cognito + API GW + EventBridge | ~$2 |
| **Total** | **$38 - $125/mês** |

**Nota**: Custos dos serviços de streaming (MediaLive, etc.) são separados e dependem do uso.

---

## SLIDE 14 — Próximos Passos / Roadmap

**Título**: Evolução Planejada

**Conteúdo**:
- ✅ Migração DynamoDB como camada de consulta rápida (dual-write S3 + DynamoDB)
- ✅ Ingestão de métricas CloudWatch com classificação de severidade
- ✅ Alertas proativos via SNS para eventos ERROR/CRITICAL
- ✅ Comparação de canais, histórico de auditoria, health check em massa
- Dashboard visual de saúde dos canais em tempo real
- Análise preditiva de falhas usando histórico de métricas

---

## SLIDE 15 — Encerramento

**Título**: Obrigado!

**Conteúdo**:
- Resumo: Chatbot IA para NOC que simplifica gestão de ~220 canais de streaming
- Reduz tempo de diagnóstico e criação de canais
- Arquitetura serverless, escalável e de baixo custo
- Perguntas?
