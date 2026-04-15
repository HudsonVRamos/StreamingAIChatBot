# Plano de Implementação: SLA Tracking

## Visão Geral

Implementar o módulo Calculador_SLA dentro da Lambda_Configuradora, adicionando a ação `sla` ao path `/gerenciarRecurso` existente. Inclui as funções `_calcular_sla()`, `_agrupar_incidentes()` e `_formatar_duracao()`, consulta ao DynamoDB StreamingLogs com fallback para S3 KB_LOGS, suporte a múltiplos canais por substring, exportação via Lambda_Exportadora, atualização do schema OpenAPI, prompt do Agente_Bedrock e botões no frontend.

## Tarefas

- [x] 1. Implementar funções utilitárias do Calculador_SLA
  - [x] 1.1 Criar função `_formatar_duracao(minutos: int) -> str` em `lambdas/configuradora/handler.py`
    - Converter minutos em string legível: 45 → "45min", 132 → "2h12min", 4570 → "3d 4h 10min", 0 → "0min"
    - Regras: < 60min → apenas "Xmin"; 60-1439min → "Xh" ou "XhYmin"; ≥ 1440min → inclui "Xd"
    - _Requisitos: 5.5, 6.2_

  - [x] 1.2 Escrever teste de propriedade: formatação de duração (Propriedade 5)
    - **Propriedade 5: Formatação de duração é legível e correta**
    - Gerar minutos aleatórios com `st.integers(0, 100000)`
    - Verificar que a string retornada é não-vazia e representa o mesmo número de minutos
    - Verificar regras de formato por faixa (< 60, 60-1439, ≥ 1440)
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_sla.py`
    - **Valida: Requisito 5.5**

  - [x] 1.3 Criar função `_agrupar_incidentes(eventos: list[dict], janela_consolidacao_minutos: int = 60) -> list[dict]` em `lambdas/configuradora/handler.py`
    - Filtrar apenas eventos com severidade ERROR ou CRITICAL
    - Ordenar por timestamp ASC
    - Agrupar eventos consecutivos com gap < janela_consolidacao_minutos no mesmo incidente
    - Calcular duração de cada incidente: `(fim - inicio).total_seconds() / 60 + 5` (granularidade mínima 5min)
    - Determinar `severidade_maxima` usando `_SEVERITY_ORDER` (INFO=0, WARNING=1, ERROR=2, CRITICAL=3)
    - Contar `eventos_count` como número de `metrica_nome` distintos no incidente
    - Retornar lista de incidentes com campos: inicio, fim, duracao_minutos, duracao_formatada, severidade_maxima, eventos_count
    - _Requisitos: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [x] 1.4 Escrever teste de propriedade: agrupamento por janela (Propriedade 1)
    - **Propriedade 1: Agrupamento de incidentes pela Janela_Consolidacao**
    - Gerar listas de eventos ERROR/CRITICAL com timestamps e gaps variados
    - Verificar que eventos com gap < 60min estão no mesmo incidente
    - Verificar que eventos com gap >= 60min estão em incidentes distintos
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_sla.py`
    - **Valida: Requisitos 4.2, 4.3**

  - [x] 1.5 Escrever teste de propriedade: apenas ERROR/CRITICAL formam incidentes (Propriedade 2)
    - **Propriedade 2: Apenas eventos ERROR/CRITICAL formam incidentes**
    - Gerar listas de eventos com severidades mistas (INFO, WARNING, ERROR, CRITICAL)
    - Verificar que nenhum incidente contém eventos INFO ou WARNING
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_sla.py`
    - **Valida: Requisito 4.1**

  - [x] 1.6 Escrever teste de propriedade: severidade máxima (Propriedade 3)
    - **Propriedade 3: Severidade máxima segue a hierarquia**
    - Gerar listas de severidades com `st.lists(st.sampled_from(["INFO","WARNING","ERROR","CRITICAL"]), min_size=1)`
    - Verificar que `severidade_maxima` é a mais alta segundo a hierarquia
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_sla.py`
    - **Valida: Requisito 4.5**

- [x] 2. Checkpoint — Verificar testes das funções utilitárias
  - Garantir que todos os testes passam antes de prosseguir.

- [x] 3. Implementar consulta ao DynamoDB e fallback S3
  - [x] 3.1 Criar função `_consultar_ddb_sla(canal, servico, timestamp_inicio, timestamp_fim, ddb_table) -> list[dict]` em `lambdas/configuradora/handler.py`
    - Construir PK no formato `{servico}#{canal}`
    - Consultar com SK range entre timestamp_inicio e timestamp_fim
    - Usar ProjectionExpression com apenas os campos necessários: timestamp, severidade, tipo_erro, metrica_nome, metrica_valor
    - Paginar automaticamente com LastEvaluatedKey até obter todos os eventos
    - Aplicar backoff exponencial (1s, 2s, 4s) com até 3 tentativas para ProvisionedThroughputExceededException
    - Lançar exceção em caso de falha para acionar fallback
    - _Requisitos: 2.1, 2.2, 2.3, 2.4, 2.5, 13.5_

  - [x] 3.2 Criar função `_consultar_s3_sla_fallback(canal, servico, timestamp_inicio, timestamp_fim) -> list[dict]` em `lambdas/configuradora/handler.py`
    - Listar objetos S3 com prefixo `{servico}/{canal}` (ou apenas `{canal}` se servico=None)
    - Filtrar por data de modificação dentro do período solicitado
    - Desserializar cada JSON e extrair campos: timestamp, severidade, canal, tipo_erro, metrica_nome
    - Retornar lista de eventos no mesmo formato do DynamoDB
    - _Requisitos: 3.1, 3.2, 3.3_

  - [x] 3.3 Escrever testes unitários para consulta DynamoDB e fallback S3
    - Mock DynamoDB com LastEvaluatedKey para testar paginação
    - Mock throttling para testar backoff exponencial com 3 retries
    - Mock DynamoDB falhando → verificar chamada ao S3 com prefixo correto
    - Verificar que ProjectionExpression está presente na query
    - Arquivo: `tests/test_sla.py`
    - _Requisitos: 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 13.5_

- [ ] 4. Implementar orquestrador `_calcular_sla()`
  - [-] 4.1 Criar função `_calcular_sla(resource_id, periodo_dias, servico) -> dict` em `lambdas/configuradora/handler.py`
    - Calcular `timestamp_inicio` e `timestamp_fim` com base em `periodo_dias`
    - Iterar sobre serviços relevantes (todos ou apenas o especificado)
    - Para cada serviço, buscar canais que contenham `resource_id` como substring (case-insensitive)
    - Consultar DynamoDB via `_consultar_ddb_sla()` com fallback para `_consultar_s3_sla_fallback()`
    - Chamar `_agrupar_incidentes()` para identificar incidentes
    - Calcular `uptime_percentual` com fórmula: `round(((periodo_total - degradacao) / periodo_total) * 100, 2)`, limitado a [0.00, 100.00]
    - Calcular `tempo_total_degradacao_minutos` como soma das durações dos incidentes
    - Montar Relatorio_SLA com todos os campos obrigatórios (incluindo `fonte_dados`, `erros`)
    - Ordenar `lista_incidentes` por `inicio` DESC
    - Gerar `mensagem_resumo` no formato especificado
    - Para múltiplos canais: retornar dict com `relatorios` e `resumo_grupo`; para canal único: retornar Relatorio_SLA diretamente
    - Implementar timeout safety: monitorar tempo de execução, cortar em ~100s com campo `aviso`
    - Limitar a 20 canais quando busca por substring retornar mais de 20
    - _Requisitos: 1.1, 1.2, 1.3, 1.4, 5.1, 5.2, 5.3, 5.4, 6.1, 6.2, 6.3, 6.4, 6.5, 7.1, 7.2, 7.3, 7.4, 7.5, 13.1, 13.2, 13.3, 13.4_

  - [~] 4.2 Escrever teste de propriedade: cálculo de uptime (Propriedade 4)
    - **Propriedade 4: Cálculo de uptime percentual**
    - Gerar `periodo_dias` (1-30) e `tempo_total_degradacao_minutos` (0 até periodo_total) aleatórios
    - Verificar fórmula: `round(((periodo_dias * 1440 - degradacao) / (periodo_dias * 1440)) * 100, 2)`
    - Verificar que resultado está sempre em [0.00, 100.00]
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_sla.py`
    - **Valida: Requisitos 5.1, 5.2, 5.4**

  - [~] 4.3 Escrever teste de propriedade: estrutura do Relatorio_SLA (Propriedade 6)
    - **Propriedade 6: Relatorio_SLA contém todos os campos obrigatórios**
    - Gerar eventos e parâmetros variados, chamar `_calcular_sla()` com mocks
    - Verificar presença de todos os campos obrigatórios no Relatorio_SLA
    - Verificar campos de cada item em `lista_incidentes`
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_sla.py`
    - **Valida: Requisitos 6.1, 6.2**

  - [~] 4.4 Escrever teste de propriedade: ordenação lista_incidentes (Propriedade 7)
    - **Propriedade 7: Lista de incidentes ordenada por inicio DESC**
    - Gerar incidentes com timestamps aleatórios
    - Verificar que para todo par consecutivo (i, i+1), `inicio[i] >= inicio[i+1]`
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_sla.py`
    - **Valida: Requisito 6.3**

  - [~] 4.5 Escrever teste de propriedade: resumo de grupo (Propriedade 8)
    - **Propriedade 8: Resumo de grupo contém campos corretos**
    - Gerar lista de Relatorio_SLA com uptime_percentual variados (2-20 relatórios)
    - Verificar `total_canais`, `uptime_medio`, `canal_pior_sla`, `canal_melhor_sla`
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_sla.py`
    - **Valida: Requisito 7.2**

  - [~] 4.6 Escrever testes unitários para `_calcular_sla()`
    - Nenhum evento encontrado → uptime=100.00 com mensagem específica
    - Canal não encontrado → HTTP 404
    - Timeout safety → resultado parcial com campo aviso
    - Mais de 20 canais → limite e campo aviso
    - Canal único → sem wrapper relatorios
    - Múltiplos canais → com wrapper relatorios e resumo_grupo
    - Arquivo: `tests/test_sla.py`
    - _Requisitos: 7.3, 7.5, 13.1, 13.2, 13.3_

- [~] 5. Checkpoint — Verificar testes do Calculador_SLA
  - Garantir que todos os testes passam antes de prosseguir.

- [ ] 6. Integrar no handler e atualizar schema OpenAPI
  - [~] 6.1 Adicionar tratamento de `acao=sla` no handler existente em `lambdas/configuradora/handler.py`
    - No bloco `if api_path in ("/gerenciarRecurso", ...)`, adicionar branch para `acao == "sla"` antes do fallback de ação inválida
    - Validar `resource_id` obrigatório → HTTP 400 com mensagem correta
    - Validar `periodo_dias` ≤ 30 → HTTP 400 com mensagem correta
    - Validar `periodo_dias` > 0 → HTTP 400 para valores inválidos
    - Extrair `servico` opcional dos parâmetros
    - Chamar `_calcular_sla()` e retornar via `_bedrock_response()`
    - Tratar dupla falha DynamoDB+S3 → HTTP 503
    - Tratar canal não encontrado → HTTP 404
    - Atualizar mensagem de erro do fallback de ação inválida para incluir "sla" na lista
    - _Requisitos: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 3.4_

  - [~] 6.2 Atualizar schema OpenAPI em `Help/openapi-config-v2.json`
    - Adicionar `"sla"` ao enum de `acao` no path `/gerenciarRecurso`
    - Atualizar `description` do path para incluir a ação sla
    - Atualizar `summary` do path para incluir "sla"
    - Verificar que `periodo_dias` já está documentado (default 7 → atualizar para mencionar que para sla o padrão é 30 e máximo é 30)
    - _Requisitos: 10.1, 10.2, 10.3, 10.4_

  - [~] 6.3 Escrever testes unitários para roteamento `acao=sla`
    - Mock event com `acao=sla` e `resource_id`, verificar invocação de `_calcular_sla()`
    - Testar HTTP 400 para `resource_id` ausente
    - Testar HTTP 400 para `periodo_dias > 30`
    - Testar HTTP 503 para dupla falha de fontes de dados
    - Arquivo: `tests/test_sla.py`
    - _Requisitos: 1.1, 1.5, 1.6, 3.4_

- [ ] 7. Implementar exportação SLA na Lambda_Exportadora
  - [~] 7.1 Adicionar suporte a `tipo="sla"` em `lambdas/exportadora/handler.py`
    - Adicionar branch para `tipo == "sla"` no handler de exportação
    - Invocar internamente a lógica do Calculador_SLA (importar ou replicar `_calcular_sla()`)
    - Gerar CSV com colunas: canal, servico, periodo_dias, data_inicio, data_fim, uptime_percentual, total_incidentes, tempo_total_degradacao_minutos, incidente_inicio, incidente_fim, incidente_duracao_minutos, incidente_severidade_maxima (uma linha por incidente; linha com campos de incidente vazios quando total_incidentes=0)
    - Gerar JSON com Relatorio_SLA completo quando formato="JSON"
    - Nomear arquivo no formato `sla-{resource_id}-{periodo_dias}d-{timestamp}.{extensao}`
    - Armazenar no S3_Exports e retornar URL pré-assinada com validade de 1 hora
    - _Requisitos: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

  - [~] 7.2 Escrever testes unitários para exportação SLA
    - Mock de `_calcular_sla()`, verificar geração de CSV com colunas corretas
    - Verificar geração de JSON com estrutura correta
    - Verificar nome do arquivo no formato correto
    - Verificar URL pré-assinada retornada
    - Arquivo: `tests/test_exportadora_sla.py`
    - _Requisitos: 8.3, 8.4, 8.5, 8.6_

- [ ] 8. Testes de serialização e round-trip JSON
  - [~] 8.1 Escrever teste de propriedade: round-trip JSON (Propriedade 9)
    - **Propriedade 9: Round-trip JSON preserva o Relatorio_SLA**
    - Gerar Relatorio_SLA completo com texto em português usando strategies compostas
    - Serializar com `json.dumps(ensure_ascii=False)` e desserializar com `json.loads()`
    - Verificar que o dicionário resultante é equivalente ao original
    - Verificar que campos numéricos são números JSON (não strings)
    - Verificar que campos de data são strings ISO 8601 com sufixo Z
    - Verificar que caracteres Unicode em português são preservados
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_sla.py`
    - **Valida: Requisitos 14.1, 14.2, 14.3, 14.4**

- [ ] 9. Atualizar frontend e prompt do agente
  - [~] 9.1 Adicionar botões de sugestão de SLA no `frontend/chat.html`
    - Adicionar na seção "🔍 Logs & Métricas" da sidebar os botões: "Qual o uptime do canal WARNER no último mês?", "SLA de todos os canais Globo nos últimos 7 dias", "Exportar relatório SLA do canal ESPN em CSV"
    - Seguir o mesmo padrão dos botões de sugestão existentes (onclick inserindo texto no campo de entrada)
    - _Requisitos: 11.1, 11.2_

  - [~] 9.2 Atualizar prompt do Agente Bedrock em `Help/agente-bedrock-prompt-v2.md`
    - Adicionar rota SLA com priority="4.5" entre HEALTH_CHECK_MASSA (priority 4) e LOGS_HISTÓRICOS (priority 5)
    - Incluir palavras-chave: "uptime", "SLA", "disponibilidade", "tempo fora do ar", "incidentes", "degradação", "indisponibilidade", "ficou fora", "caiu por quanto tempo"
    - Incluir regra de diferenciação: "uptime"/"percentual"/"disponibilidade" → rota SLA; "por que caiu"/"o que aconteceu" → rota LOGS_HISTÓRICOS
    - Incluir mapeamento de período: "último mês" → periodo_dias=30, "última semana" → periodo_dias=7, "hoje"/"últimas 24h" → periodo_dias=1
    - Incluir regra de formatação: exibir mensagem_resumo em destaque, depois lista de incidentes com data de início, duração e severidade máxima
    - Incluir regra para múltiplos canais: apresentar resumo_grupo primeiro, depois canais ordenados por uptime_percentual crescente (pior SLA primeiro)
    - _Requisitos: 9.1, 9.2, 9.3, 9.4, 9.5, 12.1, 12.2, 12.3, 12.4_

- [~] 10. Checkpoint final — Verificar todos os testes
  - Garantir que todos os testes passam, perguntar ao usuário se houver dúvidas.

## Notas

- Tarefas marcadas com `*` são opcionais e podem ser puladas para um MVP mais rápido
- Cada tarefa referencia requisitos específicos para rastreabilidade
- Checkpoints garantem validação incremental
- Testes de propriedade validam propriedades universais de corretude definidas no design
- Testes unitários validam exemplos específicos e edge cases
- A linguagem de implementação é Python, conforme o design e o código existente
- O módulo Calculador_SLA reutiliza `_SEVERITY_ORDER` já existente no handler
- A variável de ambiente `LOGS_TABLE_NAME` já está disponível na Lambda_Configuradora
