# Plano de Implementação: Integração SpringServe Ad Server

## Visão Geral

Implementação incremental do Pipeline_Ads para ingestão de dados do SpringServe, normalização flat JSON, dual-write S3+DynamoDB, correlação MediaTailor↔SpringServe, atualização da Exportadora, prompt do agente Bedrock e frontend. Segue o padrão existente do `pipeline_config/handler.py`.

## Tarefas

- [x] 1. Criar estrutura do módulo pipeline_ads e autenticação SpringServe
  - [x] 1.1 Criar `lambdas/pipeline_ads/__init__.py`, `lambdas/pipeline_ads/shared/__init__.py` e estrutura de diretórios
    - _Requisitos: 11.2_
  - [x] 1.2 Implementar `lambdas/pipeline_ads/shared/auth.py` com classe `SpringServeAuth`
    - Implementar `__init__` com base_url, email, password, criação de `requests.Session`
    - Implementar `authenticate()`: POST /api/v1/auth com Content-Type application/x-www-form-urlencoded, extrai token, seta header Authorization na session
    - Implementar `request(method, path, **kwargs)`: faz request com re-autenticação automática em caso de HTTP 401 (uma única tentativa)
    - _Requisitos: 1.2, 1.3, 1.5, 1.6_
  - [x] 1.3 Escrever teste de propriedade para re-autenticação (Propriedade 5)
    - **Propriedade 5: Re-autenticação automática em caso de token expirado**
    - **Valida: Requisito 1.5**

- [x] 2. Implementar paginação genérica e normalizers
  - [x] 2.1 Implementar função `_paginate_springserve(auth, path, params)` em `lambdas/pipeline_ads/handler.py`
    - Paginação genérica: itera page=1,2,3... com per=MAX_PER_PAGE (1000), enquanto current_page < total_pages
    - Retorna lista consolidada de todos os resultados de todas as páginas
    - _Requisitos: 2.1, 3.1, 5.1, 5.2, 5.3_
  - [x] 2.2 Escrever teste de propriedade para paginação (Propriedade 1)
    - **Propriedade 1: Paginação coleta todos os itens**
    - **Valida: Requisitos 2.1, 3.1, 4.5**
  - [x] 2.3 Implementar `lambdas/pipeline_ads/shared/normalizers.py` com todas as funções de normalização
    - `normalize_supply_tag(raw, demand_priorities)` → flat JSON com channel_id, servico="SpringServe", tipo="supply_tag", supply_tag_id, nome, status, account_id, demand_tag_count, demand_tags, demand_tag_ids, created_at, updated_at
    - `normalize_demand_tag(raw)` → flat JSON com channel_id, servico="SpringServe", tipo="demand_tag", demand_tag_id, nome, status, demand_type, supply_tag_ids
    - `normalize_report(raw)` → flat JSON com channel_id, servico="SpringServe", tipo="report", supply_tag_id, supply_tag_name, fill_rate, total_impressions, total_revenue, total_cost, cpm, data_inicio, data_fim
    - `normalize_delivery_modifier(raw)` → flat JSON com channel_id, servico="SpringServe", tipo="delivery_modifier"
    - `normalize_creative(raw)` → flat JSON com channel_id, servico="SpringServe", tipo="creative"
    - `normalize_label(raw, label_type)` → flat JSON com channel_id, servico="SpringServe", tipo="supply_label"/"demand_label"
    - `normalize_scheduled_report(raw)` → flat JSON com channel_id, servico="SpringServe", tipo="scheduled_report"
    - `normalize_correlation(mt_config, supply_tag, report_data)` → flat JSON com channel_id, servico="Correlacao", tipo="canal_springserve"
    - Seguir padrão de `pipeline_config/shared/normalizers.py` (flat, sem nesting)
    - _Requisitos: 2.2, 3.2, 4.2, 5.1, 5.2, 5.3, 6.3, 9.1, 9.2, 9.3, 9.4_
  - [x] 2.4 Escrever teste de propriedade para normalização flat JSON (Propriedade 2)
    - **Propriedade 2: Normalização produz flat JSON com campos obrigatórios**
    - **Valida: Requisitos 2.2, 3.2, 4.2, 5.1, 5.2, 5.3, 6.3, 9.1, 9.2, 9.3, 9.4**
  - [x] 2.5 Escrever teste de propriedade para round-trip JSON (Propriedade 4)
    - **Propriedade 4: Round-trip de serialização JSON preserva dados**
    - **Valida: Requisito 9.5**

- [x] 3. Checkpoint — Verificar módulos base
  - Garantir que todos os testes passam, perguntar ao usuário se há dúvidas.

- [x] 4. Implementar handler principal do Pipeline_Ads
  - [x] 4.1 Criar `lambdas/pipeline_ads/handler.py` com constantes, variáveis de ambiente e função `handler(event, context)`
    - Definir constantes: BOTO_CONFIG, SPRINGSERVE_ENDPOINTS, MAX_PER_PAGE, WORKERS
    - Definir variáveis de ambiente: SPRINGSERVE_SECRET_NAME, SPRINGSERVE_BASE_URL, KB_ADS_BUCKET, KB_ADS_PREFIX, CONFIGS_TABLE_NAME, MEDIATAILOR_REGION, WORKERS
    - Implementar `handler()`: obtém credenciais do Secrets Manager, instancia SpringServeAuth, autentica, cria clientes boto3 (s3, dynamodb, mediatailor), inicializa dict de resultados
    - _Requisitos: 1.1, 1.2, 10.1, 11.2_
  - [x] 4.2 Implementar `_process_supply_tags(auth, s3, ddb, results)` no handler
    - Chama `_paginate_springserve` para supply_tags
    - Para cada supply tag, consulta GET /api/v1/supply_tags/{id}/demand_tag_priorities
    - Normaliza com `normalize_supply_tag`, faz dual-write S3 (kb-ads/SpringServe/supply_tag_{id}.json) e DynamoDB (PK=SpringServe#supply_tag, SK=nome)
    - Retorna lista de supply_tags para uso na correlação
    - _Requisitos: 2.1, 2.2, 2.3, 2.4, 2.5_
  - [x] 4.3 Implementar `_process_demand_tags(auth, s3, ddb, results)` no handler
    - Chama `_paginate_springserve` para demand_tags
    - Normaliza com `normalize_demand_tag`, faz dual-write S3 (kb-ads/SpringServe/demand_tag_{id}.json) e DynamoDB (PK=SpringServe#demand_tag, SK=nome)
    - _Requisitos: 3.1, 3.2, 3.3, 3.4_
  - [x] 4.4 Implementar `_process_reports(auth, s3, ddb, results)` e `_process_scheduled_reports(auth, s3, ddb, results)` no handler
    - Reports: POST /api/v1/reports com body JSON (async=false, date_range="yesterday", dimensions, metrics, interval, timezone)
    - Normaliza com `normalize_report`, faz dual-write S3 (kb-ads/SpringServe/report_supply_{id}_{data}.json) e DynamoDB (PK=SpringServe#report, SK=supply_tag_name#{data})
    - Scheduled reports: GET paginado, normaliza com `normalize_scheduled_report`, dual-write
    - _Requisitos: 4.1, 4.2, 4.3, 4.4, 4.5_
  - [x] 4.5 Implementar `_process_delivery_modifiers`, `_process_creatives` e `_process_labels` no handler
    - Cada função: GET paginado, normaliza com função correspondente, dual-write S3 e DynamoDB
    - Labels: GET /api/v1/supply_labels e GET /api/v1/demand_labels separados
    - Chaves S3: kb-ads/SpringServe/{tipo}_{id}.json; DynamoDB: PK=SpringServe#{tipo}, SK=nome
    - _Requisitos: 5.1, 5.2, 5.3, 5.4, 5.5_
  - [x] 4.6 Escrever teste de propriedade para formato de chave S3 (Propriedade 3)
    - **Propriedade 3: Formato da chave S3 segue o padrão correto**
    - **Valida: Requisitos 2.4, 3.3, 4.3, 5.4, 6.4**

- [x] 5. Implementar correlação MediaTailor ↔ SpringServe
  - [x] 5.1 Implementar `_process_correlations(auth, s3, ddb, mt_client, results, supply_tags)` no handler
    - Chama `mediatailor.list_playback_configurations()` com paginação
    - Para cada playback config, extrai `ad_decision_server_url` e tenta identificar supply_tag_id via parsing de URL
    - Quando correlação encontrada: busca métricas do report correspondente, normaliza com `normalize_correlation`, faz dual-write S3 (kb-ads/Correlacao/correlacao_{mediatailor_name}.json) e DynamoDB (PK=Correlacao#canal, SK=mediatailor_name)
    - Quando não encontrada: registra warning com nome da config e URL
    - _Requisitos: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_
  - [x] 5.2 Escrever teste de propriedade para correlação URL (Propriedade 7)
    - **Propriedade 7: Correlação URL identifica corretamente supply tags**
    - **Valida: Requisitos 6.2, 6.6**

- [x] 6. Implementar execução paralela, resiliência e cópia do DOC_SPRINGSERVER.yml
  - [x] 6.1 Integrar ThreadPoolExecutor no handler para execução paralela das funções de coleta
    - Usar `concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS)` para submeter as funções _process_* em paralelo
    - Cada função captura suas próprias exceções e incrementa contadores em `results` (stored, errors, skipped)
    - Falha em uma função não interrompe as demais
    - _Requisitos: 10.1, 10.2, 10.3_
  - [x] 6.2 Implementar resumo de execução e cópia do DOC_SPRINGSERVER.yml
    - Ao final do handler, copiar `DOC_SPRINGSERVER.yml` para S3 com chave kb-ads/Documentacao/DOC_SPRINGSERVER.yml
    - Registrar resumo: total_armazenados, total_erros, total_correlacoes, total_rejeitados
    - Verificar invariante: total_armazenados + total_erros + total_rejeitados == total_tentados
    - _Requisitos: 10.5, 14.1_
  - [x] 6.3 Escrever teste de propriedade para resiliência e resumo (Propriedade 6)
    - **Propriedade 6: Pipeline é resiliente a falhas parciais e o resumo é preciso**
    - **Valida: Requisitos 10.3, 10.5**

- [x] 7. Checkpoint — Verificar pipeline completo
  - Garantir que todos os testes passam, perguntar ao usuário se há dúvidas.

- [x] 8. Atualizar infraestrutura CDK em `stacks/main_stack.py`
  - [x] 8.1 Adicionar `aws_secretsmanager as secretsmanager` nos imports e criar secret `springserve/api-credentials`
    - `secretsmanager.Secret` com secret_name="springserve/api-credentials", template com campo email e geração de password
    - _Requisitos: 1.1, 11.5_
  - [x] 8.2 Criar Lambda `PipelineAdsFunction` no MainStack
    - Runtime Python 3.12, handler="handler.handler", code from_asset("lambdas/pipeline_ads"), timeout=Duration.minutes(5)
    - Variáveis de ambiente: KB_ADS_BUCKET (kb_config_bucket.bucket_name), KB_ADS_PREFIX ("kb-ads/"), SPRINGSERVE_SECRET_NAME, SPRINGSERVE_BASE_URL, CONFIGS_TABLE_NAME, MEDIATAILOR_REGION, WORKERS
    - _Requisitos: 11.2_
  - [x] 8.3 Conceder permissões IAM à Lambda Pipeline_Ads e criar regra EventBridge
    - `springserve_secret.grant_read(pipeline_ads_fn)`
    - `kb_config_bucket.grant_put(pipeline_ads_fn)`
    - `configs_table.grant_write_data(pipeline_ads_fn)`
    - PolicyStatement para mediatailor:ListPlaybackConfigurations e mediatailor:GetPlaybackConfiguration
    - EventBridge Rule com `events.Schedule.rate(Duration.hours(6))` apontando para pipeline_ads_fn
    - _Requisitos: 10.4, 11.1, 11.3, 11.4_

- [x] 9. Atualizar Lambda Exportadora para suportar dados KB_ADS
  - [x] 9.1 Adicionar variáveis de ambiente KB_ADS_BUCKET e KB_ADS_PREFIX à Exportadora no MainStack e no handler
    - Adicionar `KB_ADS_BUCKET` e `KB_ADS_PREFIX` nas env vars da `exportadora_fn` no CDK
    - Adicionar leitura dessas variáveis no topo de `lambdas/exportadora/handler.py`
    - Conceder `kb_config_bucket.grant_read(exportadora_fn)` para o prefixo kb-ads/ (já usa o mesmo bucket)
    - _Requisitos: 12.1_
  - [x] 9.2 Adicionar colunas padrão para entidades SpringServe e lógica de roteamento para KB_ADS em `lambdas/exportadora/handler.py`
    - Definir `SPRINGSERVE_COLUMNS` com campos comuns (channel_id, servico, tipo, nome, status) e colunas específicas por tipo
    - Adicionar mapeamento de servico "SpringServe" e "Correlacao" no `SERVICE_COLUMNS`
    - Atualizar `query_s3_data()` para detectar filtro `base_dados="KB_ADS"` ou `servico` in ("SpringServe", "Correlacao") e usar KB_ADS_BUCKET com prefixo kb-ads/
    - Suportar filtros específicos: tipo, supply_tag_name, fill_rate_min, fill_rate_max
    - _Requisitos: 12.1, 12.2, 12.3, 12.4_

- [x] 10. Atualizar prompt do Agente Bedrock e frontend
  - [x] 10.1 Atualizar `Help/agente-bedrock-prompt-v2.md` com nova rota KB_ADS e regras de roteamento para anúncios
    - Adicionar `<route priority="2.5" name="ANUNCIOS" knowledge_base="KB_ADS">` com palavras-chave: "anúncio", "ad", "SpringServe", "supply tag", "demand tag", "fill rate", "impressões", "receita", "delivery modifier", "creative", "correlação canal", "ad server"
    - Adicionar regra: para perguntas que envolvam canal + anúncios, consultar KB_ADS E KB_CONFIG
    - Adicionar rota de exportação de dados de anúncios via Action_Group_Export com filtro base_dados="KB_ADS"
    - _Requisitos: 8.1, 8.2, 8.3, 8.4_
  - [x] 10.2 Atualizar `frontend/chat.html` com nova categoria "📢 Anúncios / SpringServe" na sidebar
    - Adicionar `<span class="sidebar-title">📢 Anúncios / SpringServe</span>` após a seção MediaTailor
    - Adicionar botões de sugestão: supply tags, demand tags, fill rate por canal, correlação canal-SpringServe, delivery modifiers, creatives, exportações de anúncios, perguntas conceituais sobre SpringServe
    - _Requisitos: 13.1, 13.2, 13.3_

- [x] 11. Checkpoint final — Verificar todos os testes e integração
  - Garantir que todos os testes passam, perguntar ao usuário se há dúvidas.

## Notas

- Tarefas marcadas com `*` são opcionais e podem ser puladas para MVP mais rápido
- Cada tarefa referencia requisitos específicos para rastreabilidade
- Testes de propriedade usam Hypothesis (já presente no projeto em `.hypothesis/`)
- A KB_ADS deve ser criada manualmente no console Bedrock após o deploy do CDK (seguindo o padrão das KBs existentes)
- O secret `springserve/api-credentials` criado pelo CDK contém placeholder — atualizar com credenciais reais após o deploy
- O arquivo `DOC_SPRINGSERVER.yml` já existe na raiz do projeto e será copiado para S3 pelo pipeline
