# Plano de Implementação: Health Check em Massa

## Visão Geral

Implementar health check em massa na Lambda_Configuradora, permitindo consultar métricas CloudWatch de TODOS os recursos de streaming em batch via `acao=healthcheck` no path `/gerenciarRecurso`. Inclui constante `_HEALTHCHECK_METRICS`, funções de descoberta/batch/classificação/dashboard, alterações no schema OpenAPI e botões no frontend.

## Tarefas

- [x] 1. Definir constante `_HEALTHCHECK_METRICS` e métricas-chave
  - [x] 1.1 Criar dicionário `_HEALTHCHECK_METRICS` em `lambdas/configuradora/handler.py`
    - Definir subconjunto reduzido de métricas por serviço separado de `_ONDEMAND_METRICS_CONFIG`
    - MediaLive: ActiveAlerts (Maximum), InputLossSeconds (Sum), DroppedFrames (Sum), Output4xxErrors (Sum), Output5xxErrors (Sum) — região sa-east-1, dimension_key=ChannelId, has_pipeline=True
    - MediaPackage: EgressResponseTime (Average), IngressBytes (Sum) — região sa-east-1, dimension_key=Channel, has_pipeline=False
    - MediaTailor: AdDecisionServer.Errors (Sum), Avail.FillRate (Average) — região us-east-1, dimension_key=ConfigurationName, has_pipeline=False
    - CloudFront: 5xxErrorRate (Average), TotalErrorRate (Average) — região us-east-1, dimension_key=DistributionId, has_pipeline=False
    - _Requisitos: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [ ]* 1.2 Escrever testes unitários para `_HEALTHCHECK_METRICS`
    - Verificar que cada serviço tem as métricas corretas
    - Verificar que `_HEALTHCHECK_METRICS` é separado de `_ONDEMAND_METRICS_CONFIG`
    - Verificar mapeamento de região por serviço (sa-east-1 vs us-east-1)
    - Arquivo: `tests/test_healthcheck.py`
    - _Requisitos: 6.1, 6.2, 6.3, 6.4, 6.5, 3.4_

- [x] 2. Implementar descoberta de recursos para health check
  - [x] 2.1 Criar função `_healthcheck_discover_resources(servicos)` em `lambdas/configuradora/handler.py`
    - Reutilizar `list_resources()` existente com mapeamento: MediaLive→channel, MediaPackage→channel_v2, MediaTailor→playback_configuration, CloudFront→distribution
    - Retornar tupla `(recursos_por_servico, erros)` — dict de serviço→lista de recursos e lista de erros
    - Se listagem de um serviço falhar, registrar erro e continuar com demais serviços
    - _Requisitos: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [ ]* 2.2 Escrever testes unitários para `_healthcheck_discover_resources()`
    - Mock de `list_resources()` para cada serviço
    - Testar cenário de falha parcial (um serviço falha, demais continuam)
    - Testar lista vazia de recursos
    - Arquivo: `tests/test_healthcheck.py`
    - _Requisitos: 2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 3. Implementar construção de queries e batch GetMetricData
  - [x] 3.1 Criar função `_healthcheck_build_queries(servico, recursos)` em `lambdas/configuradora/handler.py`
    - Construir `MetricDataQueries` usando `_HEALTHCHECK_METRICS` para cada recurso
    - Para MediaLive (has_pipeline=True), gerar queries para pipeline "0" e "1"
    - Gerar IDs únicos por query no formato `hc_{servico}_{resource_id}_{metrica}_{pipeline}`
    - _Requisitos: 3.1, 6.1, 6.2, 6.3, 6.4_

  - [x] 3.2 Criar função `_healthcheck_batch_get_metrics(queries, region, start_time, end_time)` em `lambdas/configuradora/handler.py`
    - Dividir queries em chunks de ≤500 por chamada `GetMetricData`
    - Usar clientes CloudWatch separados por região (sa-east-1 e us-east-1)
    - Implementar paginação com NextToken
    - Implementar backoff exponencial (1s, 2s, 4s) com até 3 tentativas para throttling
    - Retornar tupla `(metric_results, erros)`
    - _Requisitos: 3.2, 3.3, 3.4, 3.5, 3.6_

  - [ ]* 3.3 Escrever teste de propriedade: batch ≤500 (Propriedade 3)
    - **Propriedade 3: Batches de queries nunca excedem 500**
    - Gerar número variável de queries com `st.integers(min_value=1, max_value=2000)`
    - Verificar que cada batch tem no máximo 500 queries
    - Verificar que a soma de todos os batches é igual ao total de queries
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_healthcheck.py`
    - **Valida: Requisitos 3.2, 3.3**

  - [ ]* 3.4 Escrever testes unitários para batch e retry
    - Mock de `GetMetricData` com NextToken para testar paginação
    - Mock de throttling para testar backoff exponencial com 3 retries
    - Testar divisão correta em chunks de 500
    - Arquivo: `tests/test_healthcheck.py`
    - _Requisitos: 3.2, 3.3, 3.6, 10.2_

- [ ] 4. Checkpoint — Verificar testes até aqui
  - Garantir que todos os testes passam, perguntar ao usuário se houver dúvidas.

- [x] 5. Implementar classificação semáforo e montagem do dashboard
  - [x] 5.1 Criar função `_healthcheck_classify_resources(servico, recursos, metric_results)` em `lambdas/configuradora/handler.py`
    - Reutilizar `_classify_severity_ondemand()` existente para classificar cada métrica
    - Determinar cor semáforo por recurso usando pior severidade via `_SEVERITY_ORDER`
    - INFO → verde, WARNING → amarelo, ERROR/CRITICAL → vermelho
    - Recursos sem data points → verde com nota "sem dados no período"
    - Retornar lista de recursos classificados com campos: nome, servico, resource_id, cor, severidade, alertas, nota
    - _Requisitos: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [ ]* 5.2 Escrever teste de propriedade: cor semáforo = pior severidade (Propriedade 1)
    - **Propriedade 1: Cor semáforo é determinada pela pior severidade**
    - Gerar listas de severidades com `st.lists(st.sampled_from(["INFO","WARNING","ERROR","CRITICAL"]))`
    - Verificar que INFO→verde, WARNING→amarelo, ERROR/CRITICAL→vermelho
    - Verificar que recursos sem métricas são verdes
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_healthcheck.py`
    - **Valida: Requisitos 4.2, 4.3, 4.4, 4.5, 4.6**

  - [x] 5.3 Criar função `_healthcheck_build_dashboard(recursos_classificados, servicos_consultados, periodo_minutos, erros, parcial)` em `lambdas/configuradora/handler.py`
    - Calcular totais (verde, amarelo, vermelho) e score_saude = round(verde/total*100, 1)
    - Se total_recursos=0, score_saude=100.0
    - Ordenar `recursos_vermelho` por severidade decrescente (CRITICAL antes de ERROR)
    - Ordenar `recursos_amarelo` por nome do recurso em ordem alfabética
    - Incluir todos os campos obrigatórios: timestamp, periodo, servicos_consultados, total_recursos, totais, score_saude, recursos_vermelho, recursos_amarelo, erros, parcial, mensagem_resumo
    - Gerar mensagem_resumo em português
    - _Requisitos: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [ ]* 5.4 Escrever teste de propriedade: score de saúde (Propriedade 4)
    - **Propriedade 4: Score de saúde é calculado corretamente**
    - Gerar contagens verde/amarelo/vermelho com `st.integers(0, 500)`
    - Verificar score = round(verde/total*100, 1) quando total > 0
    - Verificar score = 100.0 quando total = 0
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_healthcheck.py`
    - **Valida: Requisitos 5.2, 10.5**

  - [ ]* 5.5 Escrever teste de propriedade: ordenação do dashboard (Propriedade 5)
    - **Propriedade 5: Ordenação do dashboard é correta**
    - Gerar listas de recursos classificados com severidades e nomes aleatórios
    - Verificar que `recursos_vermelho` está ordenado por severidade decrescente
    - Verificar que `recursos_amarelo` está ordenado por nome alfabético
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_healthcheck.py`
    - **Valida: Requisitos 5.5**

  - [ ]* 5.6 Escrever teste de propriedade: estrutura do dashboard (Propriedade 6)
    - **Propriedade 6: Dashboard contém todos os campos obrigatórios**
    - Gerar recursos classificados e erros aleatórios
    - Verificar presença de todos os campos obrigatórios no Dashboard_Saude
    - Verificar tipos dos campos (timestamp=str ISO 8601, periodo=int, totais=dict, etc.)
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_healthcheck.py`
    - **Valida: Requisitos 5.1, 5.3, 5.4, 5.6, 10.4**

- [x] 6. Implementar orquestrador `_execute_healthcheck()` com timeout safety
  - [x] 6.1 Criar função `_execute_healthcheck(servico_filtro, periodo_minutos)` em `lambdas/configuradora/handler.py`
    - Orquestrar fluxo completo: descoberta → build queries → batch get metrics → classificação → dashboard
    - Validar parâmetros: servico_filtro deve ser válido ou None, periodo_minutos deve ser inteiro positivo
    - Agrupar queries por região (sa-east-1 para MediaLive/MediaPackage, us-east-1 para MediaTailor/CloudFront)
    - Implementar timeout safety: monitorar tempo de execução, cortar em ~100s e retornar dashboard parcial com `parcial=True`
    - _Requisitos: 1.1, 1.2, 1.3, 1.4, 1.5, 10.1, 10.3_

  - [ ]* 6.2 Escrever teste de propriedade: filtro de serviço (Propriedade 2)
    - **Propriedade 2: Filtro de serviço restringe resultados ao serviço solicitado**
    - Gerar serviço aleatório com `st.sampled_from(["MediaLive","MediaPackage","MediaTailor","CloudFront"])`
    - Verificar que Dashboard_Saude contém apenas recursos do serviço filtrado
    - Verificar que `servicos_consultados` contém apenas o serviço filtrado
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_healthcheck.py`
    - **Valida: Requisitos 1.2**

  - [ ]* 6.3 Escrever testes unitários para `_execute_healthcheck()`
    - Mock de time para simular execução lenta → verificar dashboard parcial com `parcial=True`
    - Testar com servico_filtro=None (todos os serviços)
    - Testar com servico_filtro="MediaLive" (apenas um serviço)
    - Testar com nenhum recurso encontrado → dashboard vazio com score 100.0
    - Testar validação de parâmetros inválidos (servico inválido, periodo negativo)
    - Arquivo: `tests/test_healthcheck.py`
    - _Requisitos: 1.1, 1.2, 1.3, 1.4, 1.5, 10.3, 10.5_

- [ ] 7. Checkpoint — Verificar testes de lógica core
  - Garantir que todos os testes passam, perguntar ao usuário se houver dúvidas.

- [x] 8. Integrar no handler e atualizar schema OpenAPI
  - [x] 8.1 Adicionar tratamento de `acao=healthcheck` no handler existente em `lambdas/configuradora/handler.py`
    - No bloco `if api_path in ("/gerenciarRecurso", ...)`, adicionar branch para `acao == "healthcheck"`
    - Extrair `periodo_minutos` dos parâmetros (padrão 15)
    - Chamar `_execute_healthcheck()` e retornar via `_bedrock_response()`
    - Tratar erros com HTTP 400 para parâmetros inválidos e HTTP 500 para erros inesperados
    - _Requisitos: 1.1, 1.4_

  - [x] 8.2 Atualizar schema OpenAPI em `Help/openapi-config-v2.json`
    - Adicionar `"healthcheck"` ao enum de `acao` no path `/gerenciarRecurso`
    - Adicionar parâmetro `periodo_minutos` (integer, opcional, padrão 15) ao schema
    - Atualizar description do path para incluir health check em massa
    - _Requisitos: 7.1, 7.2_

  - [ ]* 8.3 Escrever teste unitário para roteamento `acao=healthcheck`
    - Mock event com `acao=healthcheck`, verificar invocação de `_execute_healthcheck()`
    - Testar resposta HTTP 400 para serviço inválido
    - Testar resposta HTTP 500 para erro inesperado
    - Arquivo: `tests/test_healthcheck.py`
    - _Requisitos: 1.1, 7.1_

- [x] 9. Atualizar frontend e prompt do agente
  - [x] 9.1 Adicionar botões de sugestão de health check no `frontend/chat.html`
    - Adicionar na seção "🔍 Logs & Métricas" da sidebar: "Qual a saúde de todos os canais?", "Health check de todos os canais MediaLive", "Dashboard de saúde geral"
    - Seguir o mesmo padrão dos botões de sugestão existentes
    - _Requisitos: 9.1, 9.2_

  - [x] 9.2 Atualizar prompt do Agente Bedrock em `Help/agente-bedrock-prompt-v2.md`
    - Adicionar rota de health check em massa entre MÉTRICAS_TEMPO_REAL e LOGS_HISTÓRICOS
    - Incluir palavras-chave: "saúde de todos", "health check de todos", "health check geral", "dashboard de saúde", "status geral", "como estão todos os canais"
    - Incluir regra de diferenciação entre consulta individual e health check em massa
    - Incluir regra de formatação: score geral primeiro, depois vermelhos, depois amarelos, omitir verdes
    - _Requisitos: 7.3, 7.4, 7.5, 8.1, 8.2, 8.3, 8.4_

- [ ] 10. Testes de serialização e round-trip JSON
  - [ ]* 10.1 Escrever teste de propriedade: round-trip JSON (Propriedade 7)
    - **Propriedade 7: Round-trip JSON preserva dados do Dashboard**
    - Gerar Dashboard_Saude completo com texto em português usando strategies compostas
    - Serializar com `json.dumps(ensure_ascii=False)` e desserializar com `json.loads()`
    - Verificar que o dicionário resultante é equivalente ao original
    - Verificar que campos numéricos são números JSON e caracteres Unicode são preservados
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_healthcheck.py`
    - **Valida: Requisitos 11.1, 11.2, 11.3, 11.4**

- [ ] 11. Checkpoint final — Verificar todos os testes
  - Garantir que todos os testes passam, perguntar ao usuário se houver dúvidas.

## Notas

- Tarefas marcadas com `*` são opcionais e podem ser puladas para um MVP mais rápido
- Cada tarefa referencia requisitos específicos para rastreabilidade
- Checkpoints garantem validação incremental
- Testes de propriedade validam propriedades universais de corretude definidas no design
- Testes unitários validam exemplos específicos e edge cases
- A linguagem de implementação é Python, conforme o design e o código existente
