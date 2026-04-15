# Tarefas de Implementação — Alertas Proativos via SNS

## Tarefa 1: Infraestrutura CDK — Tópico SNS e Permissões

- [x] 1.1 Criar tópico SNS "StreamingAlertsNotifications" no MainStack (stacks/main_stack.py)
  - Adicionar `aws_sns as sns` nos imports do CDK
  - Criar `sns.Topic(self, "AlertsTopic", topic_name="StreamingAlertsNotifications")`
  - Conceder `sns:Publish` à role da Lambda `pipeline_logs_fn` via `topic.grant_publish(pipeline_logs_fn)`
  - Adicionar variáveis de ambiente à Lambda: `SNS_TOPIC_ARN` (topic.topic_arn), `ALERT_SEVERITY_THRESHOLD` ("ERROR"), `ALERT_SUPPRESSION_MINUTES` ("60")
  - Conceder `s3:GetObject` e `s3:PutObject` no prefixo `kb-logs/alertas/` à Lambda (para estado de supressão) — nota: `grant_put` já existe, adicionar `grant_read` no `kb_logs_bucket`
  - Adicionar `CfnOutput(self, "AlertsTopicArn", value=topic.topic_arn)` como output do CloudFormation
  - **Valida: Requisitos 4.1, 4.2, 4.3, 4.4, 4.5, 4.6**

## Tarefa 2: Módulo de Filtragem por Threshold

- [x] 2.1 Implementar constante SEVERITY_ORDER e funções de filtragem em `lambdas/pipeline_logs/handler.py`
  - Adicionar constante `SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 3}`
  - Implementar `get_alert_threshold() -> str`: lê `ALERT_SEVERITY_THRESHOLD` do env, valida contra valores permitidos (WARNING, ERROR, CRITICAL), retorna "ERROR" se ausente ou inválido, loga aviso se inválido
  - Implementar `filter_events_by_threshold(eventos: List[dict], threshold: str) -> List[dict]`: filtra eventos com `SEVERITY_ORDER[evento["severidade"]] >= SEVERITY_ORDER[threshold]`
  - **Valida: Requisitos 1.1, 1.2, 1.3, 1.4, 1.5**

## Tarefa 3: Módulo de Supressão de Alertas

- [x] 3.1 Implementar `build_suppression_key()` e classe `SuppressionManager` em `lambdas/pipeline_logs/handler.py`
  - Implementar `build_suppression_key(canal: str, metrica_nome: str) -> str`: retorna `f"{canal}::{metrica_nome}"`
  - Implementar classe `SuppressionManager` com:
    - `__init__(self, s3_client, bucket, prefix, window_minutes)`: inicializa com parâmetros e estado vazio
    - `load_state()`: lê `{prefix}alertas/suppression_state.json` do S3, trata falha como estado vazio (fail-open), loga erro
    - `cleanup_expired()`: remove entradas com timestamp mais antigo que `now - window_minutes`
    - `is_suppressed(key: str) -> bool`: retorna True se key existe no estado e timestamp está dentro da janela
    - `record_alert(key: str)`: registra timestamp atual para key
    - `save_state()`: grava estado atualizado no S3 como JSON, trata falha com log
  - Ler `ALERT_SUPPRESSION_MINUTES` do env (padrão 60)
  - **Valida: Requisitos 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**

## Tarefa 4: Módulo de Formatação de Notificações

- [x] 4.1 Implementar funções de formatação em `lambdas/pipeline_logs/handler.py`
  - Implementar `get_severity_emoji(severidade: str) -> str`: retorna "🔴" para CRITICAL, "🟠" para ERROR, "🟡" para WARNING
  - Implementar `format_alert_subject(severidade: str, canal: str, servico: str) -> str`: retorna `f"[{emoji} {severidade}] Alerta Streaming - {canal} - {servico}"`
  - Implementar `format_alert_message(eventos: List[dict], canal: str, servico: str) -> Tuple[str, str]`: retorna (subject, body) com cabeçalho, separadores visuais, cada evento como item com tipo_erro/métrica/descrição/causa/recomendação/timestamp, rodapé com timestamp de geração
  - Implementar truncamento a 256KB: se body excede limite, truncar eventos e adicionar nota "... e N eventos adicionais omitidos"
  - Implementar `serialize_alert_payload(eventos: List[dict]) -> str`: serializa com `json.dumps(ensure_ascii=False)`, timestamps como ISO 8601Z, números como JSON numbers
  - **Valida: Requisitos 2.2, 2.3, 2.4, 2.5, 6.1, 6.2, 6.3, 6.4, 6.5, 7.1, 7.2, 7.3, 7.4**

## Tarefa 5: Módulo de Publicação SNS com Retry

- [x] 5.1 Implementar `publish_alert()` em `lambdas/pipeline_logs/handler.py`
  - Implementar `publish_alert(sns_client, topic_arn: str, subject: str, message: str) -> bool`: publica no SNS, retorna True se sucesso
  - Implementar backoff exponencial: até 3 tentativas com delays de 1s, 2s, 4s para erros de throttling
  - Capturar exceções e retornar False em caso de falha final, logando o erro
  - **Valida: Requisitos 2.1, 5.1, 5.5**

## Tarefa 6: Orquestração — Integrar Alertas no Handler

- [x] 6.1 Implementar `proactive_alerts_step()` e integrar no `handler()` existente em `lambdas/pipeline_logs/handler.py`
  - Implementar `proactive_alerts_step(eventos: List[dict], summary: Dict[str, Any]) -> None` que orquestra:
    1. Verificar se `SNS_TOPIC_ARN` está definido (senão, logar e retornar)
    2. Chamar `get_alert_threshold()` e `filter_events_by_threshold()`
    3. Se nenhum evento acima do threshold, logar e retornar
    4. Agrupar eventos por canal (dict de canal → lista de eventos)
    5. Instanciar `SuppressionManager`, chamar `load_state()` e `cleanup_expired()`
    6. Para cada grupo: verificar supressão, formatar, publicar, registrar
    7. Chamar `save_state()`
    8. Atualizar contadores no summary: `total_alertas_enviados`, `total_alertas_suprimidos`, `total_alertas_falha`
  - Modificar `handler()`: coletar todos os eventos armazenados durante processamento, chamar `proactive_alerts_step()` após o loop de armazenamento S3, envolver em try/except para garantir que falhas não afetam o pipeline
  - Inicializar contadores de alertas no summary
  - **Valida: Requisitos 1.5, 2.6, 5.1, 5.2, 5.3, 5.4**

## Tarefa 7: Testes de Propriedade (Hypothesis)

- [ ] 7.1 Criar arquivo de testes `tests/test_proactive_alerts_properties.py` com testes de propriedade Hypothesis
  - [ ] 7.1.1 P1 — Filtragem correta por threshold: gerar listas de eventos com severidades aleatórias e thresholds aleatórios, verificar que todos os retornados têm severidade >= threshold e nenhum abaixo é incluído
  - [ ] 7.1.2 P2 — Threshold inválido retorna ERROR: gerar strings aleatórias que não são WARNING/ERROR/CRITICAL, verificar retorno "ERROR"
  - [ ] 7.1.3 P3 — Mensagem formatada contém campos obrigatórios: gerar eventos estruturados aleatórios, verificar presença de canal, servico_origem, severidade, metrica_nome, metrica_valor, descricao, recomendacao_correcao, timestamp, emoji, separadores
  - [ ] 7.1.4 P4 — Subject no formato correto: gerar combinações aleatórias, verificar formato com regex
  - [ ] 7.1.5 P5 — Agrupamento por recurso: gerar listas com canais repetidos, verificar número de grupos = canais distintos e todos os eventos presentes
  - [ ] 7.1.6 P6 — Supressão correta dentro da janela: gerar estados com timestamps variados e janelas aleatórias, verificar decisão correta
  - [ ] 7.1.7 P7 — Limpeza de registros expirados: gerar estados com idades variadas, verificar que apenas entradas dentro da janela permanecem
  - [ ] 7.1.8 P8 — Contadores de resumo consistentes: gerar cenários com mix de envios/supressões/falhas, verificar soma = total de grupos
  - [ ] 7.1.9 P9 — Mensagem limitada a 256KB: gerar listas grandes de eventos, verificar tamanho <= 256KB e nota de truncamento
  - [ ] 7.1.10 P10 — Round-trip de serialização JSON: gerar payloads com caracteres pt-BR e números, verificar json.loads(json.dumps(x)) == x
  - [ ] 7.1.11 P11 — Timestamps ISO 8601Z: gerar timestamps aleatórios, verificar formato regex

## Tarefa 8: Testes Unitários

- [ ] 8.1 Criar testes unitários em `tests/test_proactive_alerts_unit.py`
  - Testar `get_alert_threshold()` com variável ausente (retorna "ERROR")
  - Testar `get_alert_threshold()` com variável "WARNING" (retorna "WARNING")
  - Testar `filter_events_by_threshold()` com lista vazia (retorna [])
  - Testar `filter_events_by_threshold()` com todos INFO e threshold ERROR (retorna [])
  - Testar `format_alert_message()` com exemplo concreto de evento ERROR MediaLive
  - Testar `publish_alert()` com mock SNS sucesso
  - Testar `publish_alert()` com mock SNS throttling → 3 retries → falha
  - Testar `SuppressionManager.load_state()` com S3 que lança exceção → estado vazio
  - Testar `proactive_alerts_step()` com SNS_TOPIC_ARN vazio → nenhuma publicação
  - Testar `proactive_alerts_step()` com falha SNS → pipeline continua, contadores corretos
  - **Valida: Requisitos 1.3, 1.5, 2.6, 3.6, 3.8, 5.1, 5.2, 5.5**
