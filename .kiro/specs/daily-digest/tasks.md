# Tarefas de Implementação — Daily Digest

## Tarefa 1: Infraestrutura CDK — Lambda, EventBridge e IAM

- [ ] 1.1 Criar a Lambda_DailyDigest no MainStack (`stacks/main_stack.py`)
  - Adicionar `_lambda.Function(self, "DailyDigestFunction", runtime=PYTHON_3_12, handler="handler.lambda_handler", code=Code.from_asset("lambdas/daily_digest"), timeout=Duration.seconds(300), memory_size=256)`
  - Adicionar variáveis de ambiente: `SNS_TOPIC_ARN` (alerts_topic.topic_arn), `DYNAMODB_TABLE_NAME` (logs_table.table_name), `S3_BUCKET_LOGS` (kb_logs_bucket.bucket_name), `DIGEST_OFFLINE_THRESHOLD_MINUTES` ("30"), `DIGEST_SCHEDULE_CRON` ("cron(0 11 * * ? *)"), `CONFIGURADORA_FUNCTION_NAME` (configuradora_fn.function_name)
  - **Valida: Requisitos 13.1, 13.5**

- [ ] 1.2 Criar a regra EventBridge_Schedule_Digest no MainStack
  - Adicionar `events.Rule(self, "DailyDigestSchedule", schedule=events.Schedule.expression("cron(0 11 * * ? *)"))` com target `targets.LambdaFunction(daily_digest_fn)`
  - Conceder permissão de invocação ao EventBridge: `daily_digest_fn.add_permission("EventBridgeInvoke", principal=iam.ServicePrincipal("events.amazonaws.com"))`
  - **Valida: Requisitos 1.1, 1.2, 13.2, 13.3**

- [ ] 1.3 Configurar permissões IAM mínimas para a Lambda_DailyDigest
  - Conceder `dynamodb:Query` e `dynamodb:Scan` na tabela StreamingLogs: `logs_table.grant(daily_digest_fn, "dynamodb:Query", "dynamodb:Scan")`
  - Conceder `sns:Publish` no Topico_SNS_Alertas: `alerts_topic.grant_publish(daily_digest_fn)`
  - Conceder `s3:GetObject` e `s3:PutObject` no bucket KB_LOGS com prefixo `digest/*`: `kb_logs_bucket.grant_read_write(daily_digest_fn, "digest/*")`
  - Conceder `lambda:InvokeFunction` na Lambda_Configuradora: `configuradora_fn.grant_invoke(daily_digest_fn)`
  - **Valida: Requisito 13.4**

- [ ] 1.4 Adicionar output CloudFormation e criar diretório da Lambda
  - Adicionar `CfnOutput(self, "DailyDigestLambdaArn", value=daily_digest_fn.function_arn)`
  - Criar diretório `lambdas/daily_digest/` com arquivo `__init__.py` vazio
  - **Valida: Requisito 13.6**

## Tarefa 2: Estrutura Base da Lambda_DailyDigest

- [ ] 2.1 Criar `lambdas/daily_digest/handler.py` com imports, constantes e variáveis de ambiente
  - Imports: `boto3`, `json`, `os`, `logging`, `time`, `datetime`, `timezone`, `timedelta`, `typing`
  - Constantes: `VERSAO_DIGEST = "1.0"`, `SNS_MAX_BYTES = 262144`, `TIMEOUT_GUARD_SECONDS = 240`
  - Variáveis de ambiente: `SNS_TOPIC_ARN`, `DYNAMODB_TABLE_NAME`, `S3_BUCKET_LOGS`, `DIGEST_OFFLINE_THRESHOLD_MINUTES`, `CONFIGURADORA_FUNCTION_NAME`
  - Clientes boto3: `dynamodb_resource`, `s3_client`, `sns_client`, `lambda_client`
  - **Valida: Requisitos 1.3, 13.1**

- [ ] 2.2 Implementar `lambda_handler(event, context)` — orquestrador principal
  - Calcular `periodo_inicio` e `periodo_fim` (dia anterior 00:00:00Z a 23:59:59Z)
  - Registrar `t_inicio = time.time()` para controle de timeout
  - Chamar `_coletar_dados_dynamodb(periodo_inicio, periodo_fim)` com guard de timeout a 240s
  - Chamar `_calcular_score_saude(eventos_por_canal)`
  - Chamar `_identificar_canais_offline(todos_eventos, threshold)`
  - Chamar `_calcular_top3_pior_sla(canais_com_incidente)` com guard de timeout
  - Calcular Fill_Rate_Medio a partir dos eventos MediaTailor
  - Chamar `_comparar_com_anterior(digest_atual, s3_client, bucket, periodo_inicio)`
  - Montar `digest_data` completo com todos os campos do modelo Digest_Diario
  - Chamar `_formatar_relatorio(digest_data)` e adicionar `texto_formatado` ao digest
  - Chamar `_publicar_sns(texto, subject, sns_client, topic_arn)` se SNS_TOPIC_ARN definido
  - Chamar `_salvar_s3(digest_data, s3_client, bucket, periodo_inicio)`
  - Retornar `{"statusCode": 200, "body": json.dumps({"status": status_geracao, "s3_key": s3_key})}`
  - **Valida: Requisitos 1.4, 10.5, 17.3, 17.4, 17.5**

## Tarefa 3: Coleta de Dados do DynamoDB

- [ ] 3.1 Implementar `_coletar_dados_dynamodb(periodo_inicio, periodo_fim)`
  - Iterar sobre os 4 serviços: MediaLive, MediaPackage, MediaTailor, CloudFront
  - Para cada serviço, fazer Query com `KeyConditionExpression: PK begins_with "{servico}#"` e `SK BETWEEN periodo_inicio AND periodo_fim`
  - Usar `ProjectionExpression="timestamp, canal, severidade, tipo_erro, metrica_nome, metrica_valor, servico_origem"` para reduzir RCU
  - Paginar automaticamente via `LastEvaluatedKey` até obter todos os eventos
  - Em caso de falha por serviço, registrar em `servicos_com_erro` e continuar
  - Retornar `{"eventos_por_canal": dict, "todos_eventos": list, "servicos_com_erro": list}`
  - **Valida: Requisitos 2.1, 2.2, 2.3, 2.5, 2.6**

- [ ] 3.2 Implementar agrupamento de eventos por canal e por serviço
  - Agrupar `todos_eventos` por chave `"{servico_origem}#{canal}"` para `eventos_por_canal`
  - Contar canais distintos por serviço para `total_canais_por_servico`
  - **Valida: Requisito 2.3**

## Tarefa 4: Cálculo do Score de Saúde

- [ ] 4.1 Implementar `_calcular_score_saude(eventos_por_canal)`
  - Para cada canal em `eventos_por_canal`, verificar se há pelo menos um evento com `severidade in ("ERROR", "CRITICAL")`
  - Calcular `canais_sem_incidente` e `canais_com_incidente`
  - Calcular `score_saude = round((canais_sem_incidente / total) * 100, 1)` ou `null` se total = 0
  - Calcular `score_saude_por_servico` separadamente para cada serviço
  - Incluir `aviso_sem_dados` quando total = 0
  - Retornar dict com todos os campos do modelo (score_saude, total_canais_monitorados, canais_sem_incidente, canais_com_incidente, score_saude_por_servico, aviso_sem_dados)
  - **Valida: Requisitos 3.1, 3.2, 3.3, 3.4, 3.5**

## Tarefa 5: Identificação de Canais Offline

- [ ] 5.1 Implementar `_identificar_canais_offline(eventos, threshold_minutos)`
  - Filtrar eventos com `metrica_nome == "InputLossSeconds"`
  - Agrupar por canal e somar `metrica_valor` por canal
  - Classificar como offline se soma > `threshold_minutos * 60`
  - Calcular `tempo_offline_minutos = int(soma_segundos / 60)`
  - Formatar `tempo_offline_formatado` (ex: "1h45min", "45min")
  - Ordenar resultado por `tempo_offline_minutos` decrescente
  - Retornar lista de dicts com campos: canal, servico, tempo_offline_minutos, tempo_offline_formatado
  - **Valida: Requisitos 8.1, 8.2, 8.3, 8.4, 8.5, 8.6**

## Tarefa 6: Cálculo do Top 3 Pior SLA

- [ ] 6.1 Implementar `_calcular_top3_pior_sla(canais_com_incidente, lambda_client, configuradora_fn_name)`
  - Para cada canal em `canais_com_incidente`, invocar Lambda_Configuradora com payload `{"apiPath": "/gerenciarRecurso", "parameters": {"acao": "sla", "resource_id": canal, "periodo_dias": 1}}`
  - Capturar falhas individuais: registrar erro, excluir canal do resultado, continuar
  - Ordenar canais por `uptime_percentual` crescente
  - Selecionar os 3 primeiros (ou menos se disponíveis)
  - Retornar lista com campos: canal, servico, uptime_percentual, total_incidentes, tempo_total_degradacao_formatado
  - **Valida: Requisitos 4.1, 4.2, 4.3, 4.4, 4.5, 17.1**

## Tarefa 7: Cálculo do Fill Rate Médio

- [ ] 7.1 Implementar cálculo do Fill_Rate_Medio e campo `ad_delivery`
  - Filtrar `todos_eventos` com `servico_origem == "MediaTailor"` e `metrica_nome == "Avail.FillRate"`
  - Calcular `fill_rate_medio = round(mean(valores), 2)` ou `null` se lista vazia
  - Contar `total_configuracoes_tailor` (canais MediaTailor distintos)
  - Calcular `variacao_fill_rate` comparando com `digest_anterior["ad_delivery"]["fill_rate_medio"]` (ou null)
  - Retornar dict com campos: fill_rate_medio, disponivel, total_configuracoes_tailor, variacao_fill_rate
  - **Valida: Requisitos 6.1, 6.2, 6.3, 6.4**

## Tarefa 8: Comparação com Digest Anterior

- [ ] 8.1 Implementar `_comparar_com_anterior(digest_atual, s3_client, bucket, periodo_inicio)`
  - Calcular data do dia anterior ao `periodo_inicio` para montar chave S3
  - Tentar ler `digest/{YYYY}/{MM}/{DD}/digest.json` do S3
  - Se 404 (NoSuchKey): retornar `None` e definir `aviso_sem_historico`
  - Se erro de I/O: registrar erro no log, retornar `None`
  - Se encontrado: calcular `score_saude_delta`, `total_incidentes_delta`, `canais_afetados_delta`
  - Definir `tendencia`: "melhora" se delta > 0, "piora" se delta < 0, "estavel" se delta == 0
  - Retornar dict com campos: score_saude_delta, total_incidentes_delta, canais_afetados_delta, tendencia, fill_rate_delta
  - **Valida: Requisitos 7.1, 7.2, 7.3, 7.4, 7.5**

## Tarefa 9: Formatação do Relatório

- [ ] 9.1 Implementar `_formatar_relatorio(digest_data)`
  - Implementar `_get_score_emoji(score)`: retorna "🟢" se >= 95, "🟡" se >= 80, "🔴" se < 80
  - Montar cabeçalho com data do período e score de saúde com emoji
  - Montar seção 📺 totais por serviço
  - Montar seção 🚨 resumo de incidentes (total, canais afetados, duração, por severidade)
  - Montar seção 📉 top 3 pior SLA (ou mensagem "Nenhum incidente registrado")
  - Montar seção 🔴 canais offline (ou omitir se lista vazia)
  - Montar seção 📢 ad delivery (apenas se `disponivel == True`)
  - Montar seção 📈/📉 comparação com dia anterior (apenas se `comparacao_dia_anterior` não for null)
  - Montar rodapé com `gerado_em` (ISO 8601Z) e versão
  - **Valida: Requisitos 9.1, 9.2, 9.3, 9.4, 9.6**

- [ ] 9.2 Implementar truncamento a 256KB em `_formatar_relatorio`
  - Verificar tamanho do texto em bytes UTF-8
  - Se exceder 256KB, truncar seções menos prioritárias (canais_offline, ad_delivery) e adicionar nota de truncamento
  - Definir `digest_data["truncado"] = True` quando truncamento ocorrer
  - **Valida: Requisito 9.5**

## Tarefa 10: Publicação no SNS

- [ ] 10.1 Implementar `_publicar_sns(texto, subject, sns_client, topic_arn)`
  - Verificar se `topic_arn` está definido; se não, logar aviso e retornar False
  - Implementar backoff exponencial: até 3 tentativas com delays de 1s, 2s, 4s
  - Capturar `ClientError` de throttling e aplicar retry
  - Retornar True se sucesso, False se falha após 3 tentativas (logando o erro)
  - Subject: `f"[DAILY DIGEST] Streaming Platform - {data_formatada}"` onde data_formatada = DD/MM/YYYY do Periodo_Digest
  - **Valida: Requisitos 10.1, 10.2, 10.3, 10.4, 10.5**

## Tarefa 11: Armazenamento no S3

- [ ] 11.1 Implementar `_salvar_s3(digest_data, s3_client, bucket, periodo_inicio)`
  - Calcular chave S3: `f"digest/{YYYY}/{MM}/{DD}/digest.json"` a partir de `periodo_inicio`
  - Adicionar `s3_key` ao `digest_data` antes de serializar
  - Serializar com `json.dumps(digest_data, ensure_ascii=False, indent=2, default=str)`
  - Chamar `s3_client.put_object(Bucket=bucket, Key=chave, Body=conteudo, ContentType="application/json")`
  - Em caso de falha: registrar erro com ARN do bucket e chave tentada, propagar exceção
  - Retornar a chave S3 completa
  - **Valida: Requisitos 11.1, 11.2, 11.3, 11.4, 11.5, 11.6**

## Tarefa 12: Ação `digest` na Lambda_Configuradora

- [ ] 12.1 Adicionar tratamento de `acao == "digest"` no handler da Lambda_Configuradora (`lambdas/configuradora/handler.py`)
  - Dentro do bloco de roteamento de `acao` em `/gerenciarRecurso`, adicionar case para `"digest"`
  - Ler parâmetro `data` (opcional) dos `parameters`
  - Se `data` fornecido no formato "DD/MM" ou "DD/MM/YYYY": converter para chave S3 `digest/{YYYY}/{MM}/{DD}/digest.json`
  - Se `data` não fornecido: usar dia anterior à execução como padrão
  - Ler objeto do S3 KB_LOGS com a chave calculada
  - Se `NoSuchKey`: retornar `_bedrock_response(event, 404, {"erro": "Digest não encontrado para a data {data}. O relatório é gerado diariamente às 08:00 BRT."})`
  - Se encontrado: desserializar JSON e retornar `_bedrock_response(event, 200, digest_data)`
  - Adicionar variável de ambiente `S3_BUCKET_LOGS` à Lambda_Configuradora no CDK
  - **Valida: Requisitos 12.2, 12.3, 12.4, 12.5, 12.6**

## Tarefa 13: Schema OpenAPI — Adição da Ação `digest`

- [ ] 13.1 Atualizar o schema OpenAPI do Action_Group_Config para incluir a ação `digest`
  - Localizar o arquivo de schema OpenAPI (Help/openapi-config-v2.json ou equivalente no projeto)
  - Adicionar `"digest"` ao enum do parâmetro `acao` do path `/gerenciarRecurso`
  - Adicionar documentação do parâmetro `data` (string, opcional, formato "DD/MM" ou "DD/MM/YYYY", descrição: "Data do digest desejado. Padrão: dia anterior.")
  - Adicionar documentação do formato de resposta incluindo campos: score_saude, resumo_incidentes, top3_pior_sla, canais_offline, ad_delivery, comparacao_dia_anterior, texto_formatado
  - Verificar que o total de paths permanece dentro do limite de 9
  - **Valida: Requisitos 14.1, 14.2, 14.3, 14.4**

## Tarefa 14: Prompt do Agente Bedrock — Rota DAILY_DIGEST

- [ ] 14.1 Atualizar o prompt do Agente_Bedrock (`Help/agente-bedrock-prompt-v2.md`) com a rota DAILY_DIGEST
  - Adicionar nova `<route priority="3.5" name="DAILY_DIGEST">` posicionada entre MÉTRICAS_TEMPO_REAL e HEALTH_CHECK_MASSA
  - Palavras-chave: "digest", "resumo diário", "relatório diário", "resumo de ontem", "resumo do dia", "como foi o dia", "relatório matinal"
  - Ação: `gerenciarRecurso` com `acao=digest` e parâmetro `data` extraído da mensagem quando mencionado
  - Regra de diferenciação: "ontem", "resumo", "digest" → DAILY_DIGEST; "agora", "atual", "status" → MÉTRICAS_TEMPO_REAL
  - Instrução de apresentação: exibir campo `texto_formatado` diretamente, preservando emojis e formatação
  - Exemplos de mapeamento: "resumo de ontem" → sem data (usa padrão), "digest de 10/01" → data="10/01", "como foi o dia 15/01/2024" → data="15/01/2024"
  - **Valida: Requisitos 16.1, 16.2, 16.3, 16.4**

## Tarefa 15: Frontend — Seção Daily Digest na Sidebar

- [ ] 15.1 Atualizar `frontend/chat.html` com a seção "📊 Daily Digest" na sidebar
  - Adicionar nova seção `<div class="sidebar-section">` com título "📊 Daily Digest"
  - Posicionar após a seção "🔍 Logs & Métricas" e antes das seções de operações de escrita
  - Adicionar 4 botões de sugestão com os textos: "Qual foi o resumo de ontem?", "Digest de hoje", "Digest de [data específica]", "Comparar digest de ontem com anteontem"
  - Garantir que os botões usam o mesmo handler JavaScript dos botões de sugestão existentes (inserção no campo de entrada do chat)
  - **Valida: Requisitos 15.1, 15.2, 15.3**

## Tarefa 16: Testes de Propriedade (Hypothesis)

- [ ] 16.1 Criar arquivo `tests/test_daily_digest_properties.py` com testes de propriedade Hypothesis
  - [ ] 16.1.1 P1 — Cálculo correto do Periodo_Digest: gerar datas de execução aleatórias, verificar que periodo_inicio = dia_anterior 00:00:00Z e periodo_fim = dia_anterior 23:59:59Z
  - [ ] 16.1.2 P2 — Score de Saúde segue fórmula e hierarquia: gerar conjuntos de eventos com severidades aleatórias, verificar fórmula e indicador visual (🟢/🟡/🔴)
  - [ ] 16.1.3 P3 — Identificação e ordenação de canais offline: gerar eventos InputLossSeconds com valores e thresholds aleatórios, verificar classificação e ordenação decrescente
  - [ ] 16.1.4 P4 — Top 3 contém os canais com menor uptime: gerar listas de canais com uptime_percentual aleatórios, verificar que top3 contém os 3 menores e está ordenado crescentemente
  - [ ] 16.1.5 P5 — Cálculo correto do Fill Rate Médio: gerar listas de valores de fill rate aleatórios, verificar que fill_rate_medio = round(mean, 2) e comportamento com lista vazia
  - [ ] 16.1.6 P6 — Comparação com dia anterior segue regra de tendência: gerar pares de digests aleatórios, verificar deltas e tendência
  - [ ] 16.1.7 P7 — Relatório formatado contém seções obrigatórias e respeita 256KB: gerar Digest_Diario aleatórios, verificar presença de seções e tamanho <= 256KB
  - [ ] 16.1.8 P8 — Subject SNS no formato correto: gerar datas aleatórias, verificar formato com regex `^\[DAILY DIGEST\] Streaming Platform - \d{2}/\d{2}/\d{4}$`
  - [ ] 16.1.9 P9 — Chave S3 gerada corretamente: gerar datas aleatórias, verificar formato `digest/\d{4}/\d{2}/\d{2}/digest\.json`
  - [ ] 16.1.10 P10 — Conversão de data para chave S3: gerar datas nos formatos DD/MM e DD/MM/YYYY, verificar chave S3 correta
  - [ ] 16.1.11 P11 — Paginação completa do DynamoDB: gerar N páginas de resultados mockados, verificar que todos os itens são coletados
  - [ ] 16.1.12 P12 — Round-trip de serialização do Digest_Diario: gerar Digest_Diario aleatórios com todos os campos, verificar json.loads(json.dumps(x)) == x preservando tipos

## Tarefa 17: Testes Unitários

- [ ] 17.1 Criar arquivo `tests/test_daily_digest_unit.py` com testes unitários
  - Testar `_calcular_score_saude` com zero canais (retorna null, aviso_sem_dados preenchido)
  - Testar `_calcular_score_saude` com todos os canais saudáveis (retorna 100.0)
  - Testar `_identificar_canais_offline` com threshold exato (borda: soma == threshold * 60 não classifica como offline)
  - Testar `_calcular_top3_pior_sla` com menos de 3 canais com incidente
  - Testar `_calcular_top3_pior_sla` com falha do Calculador_SLA para um canal (continua com demais)
  - Testar `_comparar_com_anterior` com S3 retornando 404 (retorna None, aviso_sem_historico preenchido)
  - Testar `_comparar_com_anterior` com S3 lançando exceção (retorna None, execução continua)
  - Testar `_publicar_sns` com SNS_TOPIC_ARN vazio (retorna False sem chamar SNS)
  - Testar `_publicar_sns` com mock SNS throttling → 3 retries → falha (retorna False)
  - Testar `_publicar_sns` com mock SNS sucesso na 2ª tentativa (retorna True)
  - Testar `_salvar_s3` com mock S3 lançando exceção (propaga exceção, loga erro)
  - Testar `lambda_handler` com DynamoDB falhando para todos os serviços (publica digest com status_geracao="erro")
  - Testar `lambda_handler` com SNS_TOPIC_ARN ausente (ignora SNS, salva no S3)
  - Testar ação `digest` na Lambda_Configuradora com data "DD/MM" (converte corretamente)
  - Testar ação `digest` na Lambda_Configuradora com S3 retornando 404 (retorna erro 404 com mensagem específica)
  - **Valida: Requisitos 2.5, 3.3, 4.4, 7.4, 7.5, 8.5, 8.6, 10.3, 10.4, 10.5, 11.5, 12.5, 17.1, 17.2**
