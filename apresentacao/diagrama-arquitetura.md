# Diagrama Arquitetural — Plataforma de Streaming Inteligente com IA

Use este texto como referência para criar o diagrama visual (draw.io, PowerPoint, Lucidchart, etc.)

---

## Diagrama em Texto (ASCII)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              USUÁRIO (Operador NOC)                             │
│                                    │                                            │
│                              Navegador Web                                      │
└────────────────────────────────────┬────────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        CAMADA DE APRESENTAÇÃO                                   │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐                   │
│  │  CloudFront   │◄──│  S3 Frontend  │    │  Cognito User    │                   │
│  │  (CDN)        │    │  (HTML/JS)    │    │  Pool (Auth)     │                   │
│  └──────┬───────┘    └──────────────┘    └──────────────────┘                   │
│         │  Chat UI: tema escuro, 45+ sugestões categorizadas                    │
└─────────┼───────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           CAMADA DE API                                         │
│  ┌──────────────────────┐    ┌──────────────────────────────┐                   │
│  │  API Gateway REST     │    │  Lambda Function URL          │                   │
│  │  (endpoints REST)     │    │  (streaming, timeout 5min)    │                   │
│  └──────────┬───────────┘    └──────────────┬───────────────┘                   │
└─────────────┼───────────────────────────────┼───────────────────────────────────┘
              │                               │
              ▼                               ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                     CAMADA DE ORQUESTRAÇÃO (us-east-1)                          │
│                                                                                 │
│  ┌─────────────────────────────────────────────────────────┐                    │
│  │              Lambda Orquestradora                        │                    │
│  │  - Ponto de entrada principal                            │                    │
│  │  - Invoca Bedrock Agent para consultas                   │                    │
│  │  - Bypass direto para exports e downloads                │                    │
│  └────────┬──────────────┬──────────────┬──────────────────┘                    │
│           │              │              │                                        │
│           ▼              ▼              ▼                                        │
│  ┌────────────┐  ┌──────────────┐  ┌──────────────┐                            │
│  │  Bedrock    │  │  Lambda       │  │  Lambda       │                            │
│  │  Agent      │  │  Exportadora  │  │  Configuradora│                            │
│  │  (Claude)   │  │              │  │              │                            │
│  │  PT-BR      │  │  - CSV/JSON  │  │  - Cria canais│                            │
│  └──────┬─────┘  │  - URLs pré-  │  │  - 4 etapas  │                            │
│         │        │    assinadas   │  │  - Rollback   │                            │
│         │        └──────┬────────┘  └──────┬────────┘                            │
│         │               │                  │                                     │
│         ▼               │                  │                                     │
│  ┌──────────────────┐   │                  │                                     │
│  │  Knowledge Bases  │   │                  │                                     │
│  │  (RAG)            │   │                  │                                     │
│  │                   │   │                  │                                     │
│  │  ┌─────────────┐ │   │                  │                                     │
│  │  │ KB_CONFIG    │ │   │                  │                                     │
│  │  │ (~220 canais)│ │   │                  │                                     │
│  │  └─────────────┘ │   │                  │                                     │
│  │  ┌─────────────┐ │   │                  │                                     │
│  │  │ KB_LOGS      │ │   │                  │                                     │
│  │  │ (métricas)   │ │   │                  │                                     │
│  │  └─────────────┘ │   │                  │                                     │
│  └──────────────────┘   │                  │                                     │
└─────────────────────────┼──────────────────┼─────────────────────────────────────┘
                          │                  │
                          ▼                  ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        CAMADA DE ARMAZENAMENTO (S3)                             │
│                                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
│  │ S3_KBConfig   │  │ S3_KBLogs    │  │ S3_Audit  │  │S3_Exports│  │S3_Frontend│ │
│  │ Configs JSON  │  │ Eventos      │  │ Trilha de │  │ Temp     │  │ HTML/JS   │ │
│  │ flat (~220)   │  │ estruturados │  │ auditoria │  │ 24h TTL  │  │ estáticos │ │
│  └──────┬───────┘  └──────┬───────┘  │ 365 dias  │  └──────────┘  └──────────┘ │
│         │                 │          └──────────┘                               │
└─────────┼─────────────────┼─────────────────────────────────────────────────────┘
          │                 │
          ▼                 ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                   CAMADA DE INGESTÃO (EventBridge + Lambdas)                    │
│                                                                                 │
│  ┌─────────────────────────────┐    ┌─────────────────────────────┐             │
│  │  EventBridge (a cada 6h)    │    │  EventBridge (a cada 1h)    │             │
│  │         │                   │    │         │                   │             │
│  │         ▼                   │    │         ▼                   │             │
│  │  Pipeline_Config Lambda     │    │  Pipeline_Logs Lambda       │             │
│  │  - Coleta configs paralela  │    │  - Coleta métricas CW       │             │
│  │  - Normaliza JSON flat      │    │  - Classifica severidade    │             │
│  │  - Valida campos            │    │  - Gera Evento_Estruturado  │             │
│  │  - Grava em S3_KBConfig     │    │  - Grava em S3_KBLogs       │             │
│  └─────────────────────────────┘    └─────────────────────────────┘             │
│                    │                              │                              │
└────────────────────┼──────────────────────────────┼──────────────────────────────┘
                     │                              │
                     ▼                              ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                  SERVIÇOS DE STREAMING AWS (sa-east-1)                           │
│                                                                                 │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐              │
│  │   AWS MediaLive   │  │ AWS MediaPackage  │  │  AWS MediaTailor  │              │
│  │                   │  │      V2           │  │                   │              │
│  │  - Canais live    │  │  - Channel Groups │  │  - Ad insertion   │              │
│  │  - Inputs RTMP/   │  │  - Endpoints      │  │  - Personalização │              │
│  │    RTP            │  │    HLS/DASH       │  │                   │              │
│  │  - Failover       │  │                   │  │                   │              │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘              │
│                                                                                 │
│  ┌──────────────────┐  ┌──────────────────┐                                    │
│  │ Amazon CloudFront │  │ Amazon CloudWatch │                                    │
│  │  (Distribuições)  │  │  (Métricas)       │                                    │
│  └──────────────────┘  └──────────────────┘                                    │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## Diagrama de Fluxo: Criação Orquestrada de Canal

```
┌──────────┐     ┌──────────────┐     ┌─────────────────────────────────────┐
│ Usuário   │────▶│ Bedrock Agent │────▶│ Lambda Configuradora                │
│ "Criar    │     │ (coleta       │     │                                     │
│  canal X" │     │  parâmetros)  │     │  Etapa 1: MediaPackage V2 Channel   │
└──────────┘     └──────────────┘     │           │                          │
                                       │           ▼                          │
                                       │  Etapa 2: Endpoints HLS/DASH        │
                                       │           │                          │
                                       │           ▼                          │
                                       │  Etapa 3: MediaLive Inputs          │
                                       │           (RTMP/RTP + failover)      │
                                       │           │                          │
                                       │           ▼                          │
                                       │  Etapa 4: MediaLive Channel         │
                                       │           │                          │
                                       │           ▼                          │
                                       │  ✅ Sucesso ──── ou ──── ❌ Falha   │
                                       │                          │           │
                                       │                    ┌─────▼─────┐     │
                                       │                    │ ROLLBACK   │     │
                                       │                    │ automático │     │
                                       │                    │ (desfaz    │     │
                                       │                    │  etapas    │     │
                                       │                    │  anteriores│     │
                                       │                    └───────────┘     │
                                       └─────────────────────────────────────┘
```

---

## Diagrama de Fluxo: Consulta via Chatbot

```
┌──────────┐     ┌───────────┐     ┌──────────────┐     ┌──────────────┐
│ Operador  │────▶│ CloudFront │────▶│ Lambda        │────▶│ Bedrock Agent │
│ NOC       │     │ + S3       │     │ Orquestradora │     │ (Claude)      │
└──────────┘     └───────────┘     └──────────────┘     └──────┬───────┘
                                                                │
                                                    ┌───────────┼───────────┐
                                                    ▼                       ▼
                                            ┌──────────────┐       ┌──────────────┐
                                            │  KB_CONFIG    │       │  KB_LOGS      │
                                            │  (S3 → RAG)  │       │  (S3 → RAG)  │
                                            │  ~220 configs │       │  métricas CW  │
                                            └──────────────┘       └──────────────┘
                                                    │                       │
                                                    └───────────┬───────────┘
                                                                │
                                                                ▼
                                                    ┌──────────────────────┐
                                                    │ Resposta contextual   │
                                                    │ em português          │
                                                    └──────────────────────┘
```

---

## Diagrama Multi-Região

```
┌─────────────────────────────────┐     ┌─────────────────────────────────┐
│         us-east-1               │     │         sa-east-1               │
│                                 │     │                                 │
│  ┌─────────────┐               │     │  ┌─────────────┐               │
│  │ Cognito      │               │     │  │ MediaLive    │               │
│  │ API Gateway  │               │     │  │ MediaPackage │               │
│  │ Bedrock Agent│               │     │  │ V2           │               │
│  │ Lambda Funcs │  ◄────────────┼─────┼──│ MediaTailor  │               │
│  │ S3 Buckets   │               │     │  │ CloudFront   │               │
│  │ EventBridge  │               │     │  │ (Distros)    │               │
│  └─────────────┘               │     │  └─────────────┘               │
└─────────────────────────────────┘     └─────────────────────────────────┘
```

---

## Legenda de Cores Sugeridas para o Diagrama Visual

| Camada | Cor Sugerida |
|---|---|
| Frontend / Apresentação | Azul claro (#4FC3F7) |
| API / Orquestração | Laranja (#FF9800) |
| IA / Bedrock | Roxo (#AB47BC) |
| Armazenamento S3 | Verde (#66BB6A) |
| Pipelines de Ingestão | Amarelo (#FDD835) |
| Serviços de Streaming | Vermelho (#EF5350) |
| Segurança / Cognito | Cinza (#78909C) |
