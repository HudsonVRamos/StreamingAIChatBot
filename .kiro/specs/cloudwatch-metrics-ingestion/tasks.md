# Plano de Implementação: Ingestão de Métricas CloudWatch

## Visão Geral

Substituir a abordagem de CloudWatch Logs pela ingestão de CloudWatch Metrics na Lambda Pipeline_Logs. Reescrever completamente `lambdas/pipeline_logs/handler.py`, adicionar endpoint `/consultarMetricas` na Configuradora, atualizar permissões IAM e variáveis de ambiente no CDK, e criar testes unitários e de propriedade.

## Tarefas

- [x] 1. Definir constantes, thresholds e configuração de métricas
  - [x] 1.1 Criar dicionário `METRICS_CONFIG` com namespace, região e lista de métricas+estatísticas para cada serviço (MediaLive, MediaPackage, MediaTailor, CloudFront)
    - Definir no topo de `lambdas/pipeline_logs/handler.py`
    - MediaLive e MediaPackage: região sa-east-1; MediaTailor e CloudFront: região us-east-1
    - Incluir todas as métricas listadas nos requisitos 2.1, 3.1, 4.1, 5.1
    - _Requisitos: 2.1, 2.2, 2.3, 3.1, 3.2, 4.1, 4.2, 5.1, 5.2_

  - [x] 1.2 Criar dicionário `SEVERITY_THRESHOLDS` com regras de classificação por serviço e métrica
    - Cada entrada mapeia (serviço, métrica) → lista de (condição, severidade, tipo_erro)
    - Regras ordenadas da mais severa para a menos severa (CRITICAL > ERROR > WARNING)
    - Incluir todos os thresholds dos requisitos 2.4–2.9, 3.4–3.7, 4.3–4.6, 5.3–5.6
    - _Requisitos: 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 3.4, 3.5, 3.6, 3.7, 4.3, 4.4, 4.5, 4.6, 5.3, 5.4, 5.5, 5.6, 7.1, 7.2, 7.4_

  - [x] 1.3 Criar dicionário `ENRICHMENT_MAP` com templates de descrição, causa provável e recomendação em português para cada tipo_erro
    - Templates com placeholders {canal}, {valor}, {periodo}, {metrica}
    - Incluir entrada para METRICAS_NORMAIS (severidade INFO)
    - _Requisitos: 6.4, 6.5, 6.6_

- [x] 2. Implementar descoberta dinâmica de recursos
  - [x] 2.1 Implementar função `discover_resources()` que lista recursos ativos de cada serviço
    - Criar clientes boto3 separados por região: sa-east-1 (MediaLive, MediaPackage) e us-east-1 (MediaTailor, CloudFront)
    - MediaLive: `ListChannels` → extrair ChannelId e Name
    - MediaPackage V2: `ListChannelGroups` → `ListChannels` → `ListOriginEndpoints`
    - MediaTailor: `ListPlaybackConfigurations` → extrair Name
    - CloudFront: `ListDistributions` → extrair DistributionId
    - Tratar erros por serviço: se um falhar, continuar com os demais e registrar erro
    - _Requisitos: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [x] 2.2 Escrever testes unitários para `discover_resources()`
    - Mock das APIs de listagem com boto3 stubber
    - Testar cenário de falha parcial (um serviço falha, demais continuam)
    - Testar paginação de resultados
    - _Requisitos: 1.1, 1.2, 1.3, 1.4, 1.6_

- [x] 3. Implementar coleta de métricas e classificação de severidade
  - [x] 3.1 Implementar função `collect_metrics(service, resources)` que consulta CloudWatch GetMetricData
    - Construir MetricDataQueries a partir de `METRICS_CONFIG` para cada recurso
    - Usar período de 300 segundos e janela de 1 hora
    - Implementar backoff exponencial (1s, 2s, 4s) com até 3 tentativas para TooManyRequestsException
    - Processar dados parciais quando disponíveis
    - _Requisitos: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 4.1, 4.2, 5.1, 5.2, 11.3, 11.6_

  - [x] 3.2 Implementar função `classify_severity(metric_name, value, service)` que aplica thresholds
    - Retornar tupla (severidade, tipo_erro)
    - Quando valor ultrapassar múltiplos thresholds, retornar a severidade mais alta
    - Retornar ("INFO", "METRICAS_NORMAIS") quando nenhum threshold for ultrapassado
    - _Requisitos: 7.1, 7.2, 7.4_

  - [x] 3.3 Escrever teste de propriedade para classificação de severidade (Propriedade 1)
    - **Propriedade 1: Classificação de severidade é correta para qualquer valor de métrica**
    - Gerar combinações aleatórias de (serviço, métrica, valor) com `st.sampled_from` e `st.floats`
    - Verificar que severidade retornada ∈ {INFO, WARNING, ERROR, CRITICAL}
    - Verificar que a severidade mais alta aplicável é selecionada
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_metrics_severity.py`
    - **Valida: Requisitos 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 3.4, 3.5, 3.6, 3.7, 4.3, 4.4, 4.5, 4.6, 5.3, 5.4, 5.5, 5.6, 7.1, 7.4**

  - [x] 3.4 Escrever testes unitários para thresholds específicos (edge cases)
    - Testar valores exatos nos limites: FillRate=80, FillRate=79.9, FillRate=50, FillRate=49.9
    - Testar PrimaryInputActive=0 (CRITICAL) vs PrimaryInputActive=1 (normal)
    - Testar 5xxErrorRate=5.0 vs 5xxErrorRate=5.1
    - Testar IngressBytes=0 por períodos consecutivos
    - Arquivo: `tests/test_unit_metrics_thresholds.py`
    - _Requisitos: 2.4, 2.8, 3.7, 4.5, 4.6, 5.3, 5.5_

- [x] 4. Implementar normalização de métricas em Evento_Estruturado
  - [x] 4.1 Implementar função `build_evento_estruturado(metric_data, resource_info, severity_info)` que gera evento normalizado
    - Campos obrigatórios: timestamp (ISO 8601 do data point), canal, severidade, tipo_erro, descricao, causa_provavel, recomendacao_correcao, servico_origem
    - Campos de contexto: metrica_nome, metrica_valor (numérico), metrica_unidade, metrica_periodo, metrica_estatistica
    - Usar templates do `ENRICHMENT_MAP` para gerar textos em português
    - Usar timestamp do data point CloudWatch, não o timestamp de execução
    - _Requisitos: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8_

  - [x] 4.2 Escrever teste de propriedade para validação de eventos (Propriedade 2)
    - **Propriedade 2: Eventos normalizados passam na validação existente**
    - Gerar dados de métrica aleatórios e verificar que `build_evento_estruturado` produz eventos que passam em `validate_evento_estruturado`
    - Verificar presença de todos os campos obrigatórios
    - Verificar que servico_origem ∈ {MediaLive, MediaPackage, MediaTailor, CloudFront}
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_metrics_events.py`
    - **Valida: Requisitos 6.1, 6.2, 6.8, 9.1, 9.3, 9.4, 9.5**

  - [x] 4.3 Escrever teste de propriedade para contagem de eventos (Propriedade 3)
    - **Propriedade 3: Contagem de eventos por recurso é correta**
    - Gerar dicionários de métricas aleatórios e verificar que o número de eventos = número de métricas anômalas, ou 1 se todas normais
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_metrics_events.py`
    - **Valida: Requisitos 7.3, 7.5, 2.9, 5.6**

- [x] 5. Checkpoint — Verificar constantes, classificação e normalização
  - Garantir que todos os testes passam, perguntar ao usuário se houver dúvidas.

- [x] 6. Implementar handler principal e armazenamento S3
  - [x] 6.1 Reescrever completamente `lambdas/pipeline_logs/handler.py` com o novo handler `handler(event, context)`
    - Orquestrar: discover_resources → collect_metrics → classify_severity → build_evento_estruturado → validate → detect_cross_contamination → store S3
    - Gerar evento separado para cada métrica anômala; gerar um único evento INFO consolidado quando todas as métricas de um recurso estiverem normais
    - Chave S3: `{KB_LOGS_PREFIX}{servico}/{canal}_{timestamp_execucao}.json`
    - Content-Type: application/json, ensure_ascii=False
    - Tratar erros por serviço e por recurso individual (resiliência)
    - Retornar resumo: total_eventos_armazenados, total_erros, total_rejeitados_validacao, total_rejeitados_contaminacao
    - _Requisitos: 7.3, 7.5, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 9.2, 11.1, 11.2, 11.4, 11.5_

  - [x] 6.2 Escrever teste de propriedade para resiliência a falhas (Propriedade 4)
    - **Propriedade 4: Pipeline é resiliente a falhas parciais**
    - Gerar subconjuntos aleatórios de serviços que falham e verificar que os demais são processados
    - Verificar que o resumo final reflete corretamente totais
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_metrics_resilience.py`
    - **Valida: Requisitos 1.6, 11.1, 11.2, 11.4**

  - [x] 6.3 Escrever teste de propriedade para round-trip JSON (Propriedade 5)
    - **Propriedade 5: Round-trip de serialização JSON preserva dados**
    - Gerar eventos estruturados e verificar que json.loads(json.dumps(evento)) == evento
    - Verificar que campos numéricos são números JSON, timestamps são strings ISO 8601
    - Verificar preservação de caracteres Unicode em português
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_metrics_events.py`
    - **Valida: Requisitos 12.1, 12.2, 12.3, 12.4, 8.4**

  - [x] 6.4 Escrever teste de propriedade para formato de chave S3 (Propriedade 6)
    - **Propriedade 6: Formato da chave S3 é correto**
    - Gerar combinações de (serviço, canal, timestamp) e verificar padrão `{prefix}{servico}/{canal}_{ts}.json`
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_metrics_events.py`
    - **Valida: Requisitos 8.2, 9.2**

- [x] 7. Implementar endpoint `/consultarMetricas` na Configuradora
  - [x] 7.1 Adicionar bloco `/consultarMetricas` em `lambdas/configuradora/handler.py`
    - Seguir padrão existente dos endpoints `/obterConfiguracao` e `/criarCanalOrquestrado`
    - Parâmetros: servico (obrigatório), resource_id (obrigatório, fuzzy), periodo_minutos (default 60), granularidade_segundos (default 300), metricas (opcional)
    - Reutilizar `_resolve_medialive_channel`, `_resolve_mpv2_channel`, `_resolve_mediatailor_config` para fuzzy resolution
    - Consultar GetMetricData em tempo real com período e granularidade do usuário
    - Classificar severidade e retornar resumo compacto no formato `_bedrock_response`
    - Tratar: recurso não encontrado (400), múltiplos candidatos (200 com lista), serviço inválido (400), erro AWS (500)
    - _Requisitos: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 13.8_

  - [x] 7.2 Escrever teste de propriedade para resposta on-demand (Propriedade 7)
    - **Propriedade 7: Resposta de consulta on-demand contém estrutura completa**
    - Gerar resultados de métricas e verificar que a resposta contém severidade_geral, alertas e dicionário de métricas
    - `@settings(max_examples=100)`
    - Arquivo: `tests/test_property_metrics_ondemand.py`
    - **Valida: Requisitos 13.5**

  - [x] 7.3 Escrever testes unitários para endpoint `/consultarMetricas`
    - Testar fuzzy resolution com mock
    - Testar múltiplos candidatos
    - Testar parâmetros ausentes (erro 400)
    - Testar serviço inválido
    - Arquivo: `tests/test_unit_metrics_ondemand.py`
    - _Requisitos: 13.2, 13.6, 13.7, 13.8_

- [x] 8. Checkpoint — Verificar handler completo e endpoint on-demand
  - Garantir que todos os testes passam, perguntar ao usuário se houver dúvidas.

- [x] 9. Atualizar infraestrutura CDK
  - [x] 9.1 Atualizar permissões IAM da Lambda Pipeline_Logs em `stacks/main_stack.py`
    - Substituir `actions=["logs:*"]` por: `cloudwatch:GetMetricData`, `cloudwatch:ListMetrics`, `medialive:ListChannels`, `medialive:DescribeChannel`, `mediapackagev2:ListChannelGroups`, `mediapackagev2:ListChannels`, `mediapackagev2:ListOriginEndpoints`, `mediatailor:ListPlaybackConfigurations`, `cloudfront:ListDistributions`
    - _Requisitos: 10.1, 10.2, 10.3, 10.4, 10.5, 10.7_

  - [x] 9.2 Adicionar variáveis de ambiente à Lambda Pipeline_Logs
    - Adicionar `MEDIATAILOR_REGION=us-east-1` e `CLOUDFRONT_REGION=us-east-1`
    - _Requisitos: 10.8_

  - [x] 9.3 Adicionar permissões CloudWatch à Lambda Configuradora
    - Adicionar `cloudwatch:GetMetricData` e `cloudwatch:ListMetrics` ao policy da Configuradora
    - _Requisitos: 13.2_

  - [x] 9.4 Atualizar testes de stack CDK em `tests/test_pipeline_logs_stack.py`
    - Verificar que as novas permissões IAM estão presentes no template sintetizado
    - Verificar que as variáveis de ambiente MEDIATAILOR_REGION e CLOUDFRONT_REGION existem
    - Verificar que a permissão `logs:*` foi removida
    - _Requisitos: 10.1, 10.7, 10.8_

- [x] 10. Checkpoint final — Garantir que todos os testes passam
  - Executar suite completa de testes com `pytest`
  - Garantir que todos os testes passam, perguntar ao usuário se houver dúvidas.

## Notas

- Tarefas marcadas com `*` são opcionais e podem ser puladas para um MVP mais rápido
- Cada tarefa referencia requisitos específicos para rastreabilidade
- Checkpoints garantem validação incremental
- Testes de propriedade validam propriedades universais de corretude definidas no design
- Testes unitários validam exemplos específicos e edge cases
- O projeto já usa Hypothesis — manter `@settings(max_examples=100)` em cada teste de propriedade
