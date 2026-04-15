# Documento de Requisitos — Daily Digest

## Introdução

Esta funcionalidade adiciona ao Streaming Chatbot um relatório diário automático gerado toda manhã com um resumo executivo da plataforma de streaming do dia anterior. O relatório é produzido por uma nova Lambda (Lambda_DailyDigest), acionada via EventBridge Schedule (padrão: 08:00 BRT / 11:00 UTC), e publicado no Topico_SNS_Alertas já existente (criado pelo spec proactive-alerts), podendo ser recebido por email ou Slack.

O relatório consolida dados do DynamoDB StreamingLogs e reutiliza o Calculador_SLA (criado pelo spec sla-tracking) para calcular uptime por canal. O resultado é armazenado no S3 com chave `digest/YYYY/MM/DD/digest.json` para consulta histórica via chatbot. O operador pode perguntar "Qual foi o resumo de ontem?" ou "Digest de 10/01" e o Agente_Bedrock recupera o digest armazenado.

## Glossário

- **Lambda_DailyDigest**: Nova função AWS Lambda Python 3.12 responsável por coletar dados do DynamoDB StreamingLogs, calcular métricas do dia anterior, formatar o relatório e publicar no SNS e S3. Timeout de 5 minutos.
- **Digest_Diario**: Estrutura de dados JSON contendo o resumo executivo da plataforma de streaming para um dia específico, incluindo score de saúde, totais por serviço, incidentes, top canais com pior SLA, métricas de ad delivery e comparação com o dia anterior.
- **Score_Saude**: Percentual de canais monitorados que não registraram nenhum incidente (evento ERROR ou CRITICAL) no dia. Calculado como `(canais_sem_incidente / total_canais_monitorados) * 100`, arredondado para uma casa decimal.
- **Calculador_SLA**: Módulo existente (spec sla-tracking) dentro da Lambda_Configuradora que calcula uptime percentual e lista incidentes por canal a partir do DynamoDB StreamingLogs.
- **DynamoDB_StreamingLogs**: Tabela DynamoDB com TTL de 30 dias, PK=`{servico}#{canal}`, SK=`{timestamp}#{metrica_nome}`, GSI_Severidade. Fonte primária de dados para o Daily Digest.
- **Topico_SNS_Alertas**: Tópico Amazon SNS existente (criado pelo spec proactive-alerts, nome "StreamingAlertsNotifications") utilizado para publicar o Daily Digest.
- **S3_Digests**: Prefixo `digest/` no bucket S3 KB_LOGS onde os arquivos `digest.json` são armazenados com chave `digest/YYYY/MM/DD/digest.json`.
- **EventBridge_Schedule_Digest**: Regra EventBridge do tipo Schedule que aciona a Lambda_DailyDigest diariamente no horário configurado.
- **Periodo_Digest**: Janela de tempo do dia anterior, de 00:00:00 UTC a 23:59:59 UTC do dia anterior à execução.
- **Canal_Offline**: Canal que registrou eventos com `tipo_erro` indicando ausência de sinal (ex: InputLossSeconds > 0 ou ActiveAlerts com tipo OFFLINE) por duração acumulada superior ao threshold configurável (padrão: 30 minutos) no Periodo_Digest.
- **Top3_Pior_SLA**: Os três canais com menor `uptime_percentual` calculado pelo Calculador_SLA no Periodo_Digest, excluindo canais sem nenhum evento registrado.
- **Fill_Rate_Medio**: Média aritmética dos valores da métrica `Avail.FillRate` do MediaTailor coletados no Periodo_Digest, expressa como percentual (0–100).
- **Digest_Anterior**: Arquivo `digest.json` do dia imediatamente anterior ao Periodo_Digest, lido do S3_Digests para comparação de tendência.
- **MainStack**: Stack CDK principal (stacks/main_stack.py) que define toda a infraestrutura do projeto.
- **DIGEST_SCHEDULE_CRON**: Variável de ambiente da Lambda_DailyDigest contendo a expressão cron do EventBridge Schedule. Padrão: `cron(0 11 * * ? *)` (11:00 UTC / 08:00 BRT).
- **DIGEST_OFFLINE_THRESHOLD_MINUTES**: Variável de ambiente da Lambda_DailyDigest com o tempo mínimo offline (em minutos) para classificar um canal como Canal_Offline. Padrão: 30.
- **Frontend_Chat**: Interface web (chat.html) com sidebar de sugestões e área de chat conversacional.
- **Agente_Bedrock**: Agente Amazon Bedrock que classifica intenções do usuário e roteia para Action Groups ou Knowledge Bases.
- **Lambda_Configuradora**: Função Lambda existente que executa operações nos serviços de streaming AWS. Receberá a nova ação `digest` no path `/gerenciarRecurso`.

## Requisitos

### Requisito 1: Acionamento Diário via EventBridge Schedule

**User Story:** Como operador de NOC, eu quero que o relatório diário seja gerado automaticamente toda manhã, para que eu receba o resumo da plataforma sem precisar solicitar manualmente.

#### Critérios de Aceitação

1. THE MainStack SHALL criar uma regra EventBridge_Schedule_Digest do tipo Schedule com expressão cron configurável via variável DIGEST_SCHEDULE_CRON, com valor padrão `cron(0 11 * * ? *)` (11:00 UTC / 08:00 BRT).
2. WHEN o EventBridge_Schedule_Digest é acionado, THE EventBridge_Schedule_Digest SHALL invocar a Lambda_DailyDigest passando o evento de trigger padrão do EventBridge.
3. THE Lambda_DailyDigest SHALL ser implementada em Python 3.12 com timeout de 5 minutos (300 segundos) e memória mínima de 256 MB.
4. WHEN a Lambda_DailyDigest é invocada, THE Lambda_DailyDigest SHALL calcular o Periodo_Digest como o intervalo de 00:00:00 UTC a 23:59:59 UTC do dia anterior à data de execução.
5. IF a variável de ambiente DIGEST_SCHEDULE_CRON não estiver definida, THEN THE MainStack SHALL utilizar `cron(0 11 * * ? *)` como valor padrão na regra EventBridge.

---

### Requisito 2: Coleta de Dados do DynamoDB StreamingLogs

**User Story:** Como engenheiro de dados, eu quero que o Daily Digest colete dados diretamente do DynamoDB StreamingLogs para o dia anterior, para que o relatório reflita com precisão o que ocorreu na plataforma.

#### Critérios de Aceitação

1. WHEN a Lambda_DailyDigest é executada, THE Lambda_DailyDigest SHALL consultar o DynamoDB_StreamingLogs para todos os serviços (MediaLive, MediaPackage, MediaTailor, CloudFront) filtrando eventos com SK entre `{timestamp_inicio_dia_anterior}` e `{timestamp_fim_dia_anterior}`.
2. THE Lambda_DailyDigest SHALL paginar automaticamente usando `LastEvaluatedKey` quando a consulta ao DynamoDB retornar mais de 1 MB de dados, até obter todos os eventos do Periodo_Digest.
3. THE Lambda_DailyDigest SHALL contar o total de canais distintos monitorados por serviço, agrupando por PK no formato `{servico}#{canal}`.
4. THE Lambda_DailyDigest SHALL identificar os Canais_Offline consultando eventos com `metrica_nome` igual a `InputLossSeconds` (MediaLive) com valor acumulado superior ao DIGEST_OFFLINE_THRESHOLD_MINUTES convertido em segundos, ou eventos com `tipo_erro` contendo "OFFLINE" com duração acumulada superior ao threshold.
5. IF a consulta ao DynamoDB_StreamingLogs falhar, THEN THE Lambda_DailyDigest SHALL registrar o erro, incluir o campo `erro_coleta` no Digest_Diario e publicar o digest parcial com os dados disponíveis.
6. THE Lambda_DailyDigest SHALL utilizar ProjectionExpression para recuperar apenas os campos necessários (`timestamp`, `canal`, `severidade`, `tipo_erro`, `metrica_nome`, `metrica_valor`, `servico_origem`), reduzindo o consumo de RCU.

---

### Requisito 3: Cálculo do Score de Saúde da Plataforma

**User Story:** Como operador de NOC, eu quero ver um score de saúde geral da plataforma no relatório diário, para que eu possa avaliar rapidamente se o dia anterior foi bom ou ruim para a operação.

#### Critérios de Aceitação

1. THE Lambda_DailyDigest SHALL calcular o Score_Saude como `(canais_sem_incidente / total_canais_monitorados) * 100`, arredondado para uma casa decimal.
2. THE Lambda_DailyDigest SHALL considerar como canal com incidente qualquer canal que tenha registrado pelo menos um evento com severidade ERROR ou CRITICAL no Periodo_Digest.
3. WHEN o total_canais_monitorados for zero, THE Lambda_DailyDigest SHALL definir Score_Saude como null e incluir o campo `aviso_sem_dados` com a mensagem "Nenhum canal monitorado no período".
4. THE Lambda_DailyDigest SHALL incluir no Digest_Diario os campos `score_saude` (número com 1 casa decimal), `total_canais_monitorados` (inteiro), `canais_sem_incidente` (inteiro) e `canais_com_incidente` (inteiro).
5. THE Lambda_DailyDigest SHALL calcular o Score_Saude de forma independente por serviço, incluindo `score_saude_por_servico` como objeto com chaves MediaLive, MediaPackage, MediaTailor e CloudFront.

---

### Requisito 4: Reutilização do Calculador_SLA para Top 3 Canais com Pior SLA

**User Story:** Como operador de NOC, eu quero ver os três canais com pior SLA no dia anterior, para que eu possa priorizar investigações e ações corretivas.

#### Critérios de Aceitação

1. WHEN a Lambda_DailyDigest calcula o Top3_Pior_SLA, THE Lambda_DailyDigest SHALL invocar o Calculador_SLA para cada canal que registrou pelo menos um evento ERROR ou CRITICAL no Periodo_Digest, passando `periodo_dias=1`.
2. THE Lambda_DailyDigest SHALL ordenar os canais por `uptime_percentual` crescente e selecionar os três primeiros como Top3_Pior_SLA.
3. THE Lambda_DailyDigest SHALL incluir no Digest_Diario o campo `top3_pior_sla` como array de objetos, cada um contendo: `canal` (string), `servico` (string), `uptime_percentual` (número), `total_incidentes` (inteiro) e `tempo_total_degradacao_formatado` (string legível).
4. WHEN menos de três canais registrarem incidentes no Periodo_Digest, THE Lambda_DailyDigest SHALL incluir apenas os canais disponíveis no `top3_pior_sla`, sem preencher posições vazias.
5. WHEN nenhum canal registrar incidente no Periodo_Digest, THE Lambda_DailyDigest SHALL definir `top3_pior_sla` como array vazio e incluir a mensagem "Nenhum incidente registrado no período" no campo `mensagem_top3`.

---

### Requisito 5: Resumo de Incidentes do Dia Anterior

**User Story:** Como operador de NOC, eu quero ver um resumo consolidado dos incidentes do dia anterior no relatório, para que eu tenha visibilidade rápida sobre quantos problemas ocorreram e quais canais foram afetados.

#### Critérios de Aceitação

1. THE Lambda_DailyDigest SHALL incluir no Digest_Diario o campo `resumo_incidentes` contendo: `total_incidentes` (inteiro — soma de todos os incidentes de todos os canais), `canais_afetados` (lista de nomes de canais distintos que tiveram incidente), `duracao_total_minutos` (inteiro — soma das durações de todos os incidentes) e `duracao_total_formatada` (string legível, ex: "4h32min").
2. THE Lambda_DailyDigest SHALL listar em `canais_afetados` apenas canais com pelo menos um incidente de severidade ERROR ou CRITICAL, ordenados por duração total de degradação decrescente.
3. WHEN o `total_incidentes` for zero, THE Lambda_DailyDigest SHALL definir `canais_afetados` como array vazio e `duracao_total_minutos` como zero.
4. THE Lambda_DailyDigest SHALL incluir o campo `incidentes_por_severidade` com contagem separada de incidentes por severidade: `{"ERROR": N, "CRITICAL": N}`.

---

### Requisito 6: Métricas de Ad Delivery do MediaTailor

**User Story:** Como operador de NOC, eu quero ver o fill rate médio do MediaTailor no relatório diário, para que eu possa monitorar a performance de entrega de anúncios sem precisar consultar o CloudWatch manualmente.

#### Critérios de Aceitação

1. WHEN eventos com `servico_origem="MediaTailor"` e `metrica_nome="Avail.FillRate"` existirem no Periodo_Digest, THE Lambda_DailyDigest SHALL calcular o Fill_Rate_Medio como a média aritmética dos valores `metrica_valor` desses eventos, arredondada para duas casas decimais.
2. THE Lambda_DailyDigest SHALL incluir no Digest_Diario o campo `ad_delivery` contendo: `fill_rate_medio` (número ou null), `disponivel` (booleano indicando se dados de MediaTailor existem no período) e `total_configuracoes_tailor` (inteiro — número de configurações MediaTailor distintas monitoradas).
3. WHEN nenhum evento de MediaTailor com `metrica_nome="Avail.FillRate"` for encontrado no Periodo_Digest, THE Lambda_DailyDigest SHALL definir `fill_rate_medio` como null e `disponivel` como false.
4. THE Lambda_DailyDigest SHALL incluir no campo `ad_delivery` o campo `variacao_fill_rate` com a diferença entre o Fill_Rate_Medio do dia atual e o do Digest_Anterior (positivo = melhora, negativo = piora), ou null se o Digest_Anterior não existir.

---

### Requisito 7: Comparação com o Dia Anterior

**User Story:** Como operador de NOC, eu quero que o relatório diário compare os indicadores com o dia anterior, para que eu possa identificar tendências de melhora ou piora na plataforma.

#### Critérios de Aceitação

1. WHEN a Lambda_DailyDigest é executada, THE Lambda_DailyDigest SHALL tentar ler o Digest_Anterior do S3_Digests usando a chave `digest/{YYYY}/{MM}/{DD}/digest.json` do dia imediatamente anterior ao Periodo_Digest.
2. WHEN o Digest_Anterior for encontrado, THE Lambda_DailyDigest SHALL calcular e incluir no Digest_Diario o campo `comparacao_dia_anterior` contendo: `score_saude_delta` (diferença do Score_Saude atual menos o anterior), `total_incidentes_delta` (diferença do total de incidentes), `canais_afetados_delta` (diferença do número de canais afetados) e `tendencia` (string: "melhora", "piora" ou "estavel").
3. THE Lambda_DailyDigest SHALL definir `tendencia` como "melhora" quando `score_saude_delta` for positivo, "piora" quando negativo, e "estavel" quando zero.
4. WHEN o Digest_Anterior não for encontrado no S3, THE Lambda_DailyDigest SHALL definir `comparacao_dia_anterior` como null e incluir o campo `aviso_sem_historico` com a mensagem "Sem dados do dia anterior para comparação".
5. IF a leitura do Digest_Anterior do S3 falhar por erro de I/O, THEN THE Lambda_DailyDigest SHALL registrar o erro no log, definir `comparacao_dia_anterior` como null e continuar a execução normalmente.

---

### Requisito 8: Identificação de Canais Offline

**User Story:** Como operador de NOC, eu quero saber quais canais ficaram offline por mais de 30 minutos no dia anterior, para que eu possa investigar causas e prevenir recorrências.

#### Critérios de Aceitação

1. THE Lambda_DailyDigest SHALL identificar Canais_Offline consultando eventos do DynamoDB_StreamingLogs com `metrica_nome="InputLossSeconds"` e somando os valores `metrica_valor` por canal no Periodo_Digest.
2. WHEN a soma de `InputLossSeconds` de um canal exceder `DIGEST_OFFLINE_THRESHOLD_MINUTES * 60` segundos, THE Lambda_DailyDigest SHALL classificar o canal como Canal_Offline.
3. THE Lambda_DailyDigest SHALL incluir no Digest_Diario o campo `canais_offline` como array de objetos, cada um contendo: `canal` (string), `servico` (string), `tempo_offline_minutos` (inteiro) e `tempo_offline_formatado` (string legível, ex: "1h45min").
4. THE Lambda_DailyDigest SHALL ordenar `canais_offline` por `tempo_offline_minutos` decrescente (canal com mais tempo offline primeiro).
5. WHEN nenhum canal atingir o threshold de offline, THE Lambda_DailyDigest SHALL definir `canais_offline` como array vazio.
6. IF a variável de ambiente DIGEST_OFFLINE_THRESHOLD_MINUTES não estiver definida, THEN THE Lambda_DailyDigest SHALL utilizar 30 minutos como valor padrão.

---

### Requisito 9: Formatação do Relatório para Email e Slack

**User Story:** Como operador de NOC, eu quero receber o relatório diário em formato legível com emojis e seções bem definidas, para que eu possa ler rapidamente no email ou no Slack sem precisar interpretar dados brutos.

#### Critérios de Aceitação

1. THE Lambda_DailyDigest SHALL formatar o relatório como texto com seções separadas por linhas, emojis indicadores e valores destacados, compatível com email e Slack.
2. THE Lambda_DailyDigest SHALL incluir no relatório formatado as seguintes seções em ordem: cabeçalho com data e score de saúde, totais por serviço, resumo de incidentes, top 3 canais com pior SLA, canais offline, métricas de ad delivery (se disponível) e comparação com dia anterior.
3. THE Lambda_DailyDigest SHALL utilizar os seguintes emojis por seção: "📊" para score de saúde, "📺" para totais por serviço, "🚨" para incidentes, "📉" para top 3 pior SLA, "🔴" para canais offline, "📢" para ad delivery e "📈" ou "📉" para comparação (conforme tendência).
4. THE Lambda_DailyDigest SHALL formatar o Score_Saude com indicador visual: "🟢" quando >= 95%, "🟡" quando >= 80% e < 95%, e "🔴" quando < 80%.
5. THE Lambda_DailyDigest SHALL limitar o tamanho do texto formatado a 256 KB (limite do SNS), truncando seções menos prioritárias (canais offline e ad delivery) quando necessário e incluindo nota de truncamento.
6. THE Lambda_DailyDigest SHALL incluir no rodapé do relatório o timestamp de geração (ISO 8601 UTC) e a versão do digest.

---

### Requisito 10: Publicação no Topico_SNS_Alertas

**User Story:** Como operador de NOC, eu quero receber o Daily Digest no mesmo canal de notificações dos alertas proativos, para que eu não precise assinar um novo tópico SNS.

#### Critérios de Aceitação

1. WHEN o relatório formatado estiver pronto, THE Lambda_DailyDigest SHALL publicar a mensagem no Topico_SNS_Alertas identificado pela variável de ambiente SNS_TOPIC_ARN.
2. THE Lambda_DailyDigest SHALL definir o atributo Subject da mensagem SNS no formato: `[DAILY DIGEST] Streaming Platform - {DD/MM/YYYY}`, onde a data corresponde ao Periodo_Digest.
3. WHEN a publicação no SNS falhar, THE Lambda_DailyDigest SHALL aplicar backoff exponencial com até 3 tentativas (intervalos de 1s, 2s, 4s) antes de registrar a falha.
4. IF após 3 tentativas a publicação no SNS ainda falhar, THEN THE Lambda_DailyDigest SHALL registrar o erro no CloudWatch Logs e continuar para a etapa de armazenamento no S3, garantindo que o digest seja salvo mesmo sem publicação SNS.
5. IF a variável de ambiente SNS_TOPIC_ARN não estiver definida ou estiver vazia, THEN THE Lambda_DailyDigest SHALL ignorar a etapa de publicação SNS, registrar um aviso no log e continuar para o armazenamento no S3.

---

### Requisito 11: Armazenamento do Digest no S3 para Histórico

**User Story:** Como operador de NOC, eu quero que cada Daily Digest seja armazenado no S3 com uma chave organizada por data, para que eu possa consultar relatórios históricos via chatbot.

#### Critérios de Aceitação

1. WHEN o Digest_Diario é gerado, THE Lambda_DailyDigest SHALL armazenar o arquivo `digest.json` no bucket S3 KB_LOGS com a chave `digest/{YYYY}/{MM}/{DD}/digest.json`, onde a data corresponde ao Periodo_Digest.
2. THE Lambda_DailyDigest SHALL serializar o Digest_Diario como JSON com `ensure_ascii=False` e indentação de 2 espaços antes de armazenar no S3.
3. THE Lambda_DailyDigest SHALL definir o Content-Type do objeto S3 como `application/json`.
4. WHEN um digest para a mesma data já existir no S3, THE Lambda_DailyDigest SHALL sobrescrever o arquivo existente (comportamento de upsert).
5. IF o armazenamento no S3 falhar, THEN THE Lambda_DailyDigest SHALL registrar o erro no CloudWatch Logs com o ARN do bucket e a chave tentada, e retornar status de erro na resposta da Lambda.
6. THE Lambda_DailyDigest SHALL incluir no Digest_Diario o campo `s3_key` com a chave completa do arquivo armazenado, para facilitar a recuperação posterior.

---

### Requisito 12: Consulta de Digest Histórico via Chatbot

**User Story:** Como operador de NOC, eu quero perguntar ao chatbot "Qual foi o resumo de ontem?" ou "Digest de 10/01" e receber o relatório correspondente, para que eu possa consultar histórico sem precisar acessar o S3 diretamente.

#### Critérios de Aceitação

1. WHEN o usuário envia mensagem contendo palavras-chave de digest ("digest", "resumo diário", "relatório diário", "resumo de ontem", "resumo do dia"), THE Agente_Bedrock SHALL classificar a intenção como consulta de digest e rotear para `/gerenciarRecurso` com `acao=digest`.
2. WHEN o Agente_Bedrock envia requisição para `/gerenciarRecurso` com `acao=digest`, THE Lambda_Configuradora SHALL ler o arquivo `digest/{YYYY}/{MM}/{DD}/digest.json` do S3 KB_LOGS e retornar o conteúdo do Digest_Diario.
3. WHEN o parâmetro `data` não for fornecido na requisição, THE Lambda_Configuradora SHALL usar a data do dia anterior à execução como data padrão para busca do digest.
4. WHEN o parâmetro `data` for fornecido no formato `DD/MM` ou `DD/MM/YYYY`, THE Lambda_Configuradora SHALL converter para a chave S3 correspondente e buscar o digest da data especificada.
5. IF o digest para a data solicitada não existir no S3, THEN THE Lambda_Configuradora SHALL retornar erro 404 com a mensagem "Digest não encontrado para a data {data}. O relatório é gerado diariamente às 08:00 BRT."
6. WHEN o digest é encontrado, THE Lambda_Configuradora SHALL retornar o campo `texto_formatado` do Digest_Diario para que o Agente_Bedrock apresente o relatório ao usuário.
7. THE Agente_Bedrock SHALL extrair a data da mensagem do usuário quando mencionada (ex: "10/01", "ontem", "segunda-feira") e mapear para o parâmetro `data` da requisição.

---

### Requisito 13: Infraestrutura CDK para Daily Digest

**User Story:** Como engenheiro de DevOps, eu quero que toda a infraestrutura do Daily Digest seja provisionada pelo CDK de forma reprodutível, para que o deploy seja automatizado e rastreável.

#### Critérios de Aceitação

1. THE MainStack SHALL criar a Lambda_DailyDigest com runtime Python 3.12, timeout de 300 segundos, memória de 256 MB e handler `lambdas/daily_digest/handler.lambda_handler`.
2. THE MainStack SHALL criar a regra EventBridge_Schedule_Digest com a expressão cron definida em DIGEST_SCHEDULE_CRON e target apontando para a Lambda_DailyDigest.
3. THE MainStack SHALL conceder permissão de invocação (lambda:InvokeFunction) ao EventBridge para acionar a Lambda_DailyDigest.
4. THE MainStack SHALL conceder à role IAM da Lambda_DailyDigest as seguintes permissões mínimas: `dynamodb:Query` e `dynamodb:Scan` na tabela StreamingLogs, `sns:Publish` no Topico_SNS_Alertas, `s3:GetObject` e `s3:PutObject` no bucket KB_LOGS com prefixo `digest/*`.
5. THE MainStack SHALL adicionar as seguintes variáveis de ambiente à Lambda_DailyDigest: `SNS_TOPIC_ARN` (ARN do Topico_SNS_Alertas), `DYNAMODB_TABLE_NAME` (nome da tabela StreamingLogs), `S3_BUCKET_LOGS` (nome do bucket KB_LOGS), `DIGEST_OFFLINE_THRESHOLD_MINUTES` (padrão "30") e `DIGEST_SCHEDULE_CRON` (padrão `cron(0 11 * * ? *)`).
6. THE MainStack SHALL exportar o ARN da Lambda_DailyDigest como output do CloudFormation com o nome "DailyDigestLambdaArn".

---

### Requisito 14: Schema OpenAPI e Integração com Action_Group_Config

**User Story:** Como desenvolvedor, eu quero que a ação `digest` esteja definida no schema OpenAPI do Action_Group_Config, para que o Agente_Bedrock possa invocar a consulta de digest histórico com os parâmetros corretos.

#### Critérios de Aceitação

1. THE schema OpenAPI do Action_Group_Config SHALL adicionar `digest` ao enum do parâmetro `acao` do path `/gerenciarRecurso`, com descrição indicando que recupera o Daily Digest de uma data específica do S3.
2. THE schema OpenAPI SHALL documentar o parâmetro `data` (string, opcional, formato `DD/MM` ou `DD/MM/YYYY`) utilizado pela ação `digest` para especificar a data do relatório desejado.
3. THE schema OpenAPI SHALL documentar o formato de resposta da ação `digest` incluindo os campos `score_saude`, `resumo_incidentes`, `top3_pior_sla`, `canais_offline`, `ad_delivery`, `comparacao_dia_anterior` e `texto_formatado`.
4. WHILE o schema OpenAPI do Action_Group_Config possui paths existentes, THE schema SHALL manter o total de paths dentro do limite de 9 após a adição da ação `digest` (que reutiliza o path `/gerenciarRecurso` já existente, sem criar novo path).

---

### Requisito 15: Sugestões de Daily Digest no Frontend

**User Story:** Como operador de NOC, eu quero ter sugestões de consulta de Daily Digest na sidebar do chat, para que eu possa acessar o relatório diário com um clique.

#### Critérios de Aceitação

1. THE Frontend_Chat SHALL incluir uma nova seção "📊 Daily Digest" na sidebar com os seguintes botões de sugestão: "Qual foi o resumo de ontem?", "Digest de hoje", "Digest de [data específica]" e "Comparar digest de ontem com anteontem".
2. WHEN o usuário clica em um botão de sugestão de digest, THE Frontend_Chat SHALL inserir o texto da sugestão no campo de entrada do chat, seguindo o mesmo comportamento dos botões de sugestão existentes.
3. THE Frontend_Chat SHALL posicionar a seção "📊 Daily Digest" na sidebar após a seção "🔍 Logs & Métricas" e antes das seções de operações de escrita.

---

### Requisito 16: Prompt do Agente Bedrock para Daily Digest

**User Story:** Como desenvolvedor, eu quero que o prompt do Agente_Bedrock inclua regras de roteamento para consultas de Daily Digest, para que o agente saiba quando e como acionar a funcionalidade e como formatar a resposta.

#### Critérios de Aceitação

1. THE Agente_Bedrock SHALL incluir uma nova rota no prompt dedicada a consultas de Daily Digest, com palavras-chave: "digest", "resumo diário", "relatório diário", "resumo de ontem", "resumo do dia", "como foi o dia", "relatório matinal".
2. THE rota de Daily Digest SHALL usar `gerenciarRecurso` com `acao=digest` e extrair o parâmetro `data` da mensagem do usuário quando mencionado.
3. THE Agente_Bedrock SHALL diferenciar entre consulta de digest (rota DAILY_DIGEST com `acao=digest`) e consulta de métricas em tempo real (rota MÉTRICAS_TEMPO_REAL com `acao=consultarMetricas`) com base na presença de palavras como "ontem", "resumo", "digest" versus "agora", "atual", "status".
4. WHEN o Agente_Bedrock apresenta o resultado do digest, THE Agente_Bedrock SHALL exibir o campo `texto_formatado` diretamente, preservando emojis e formatação de seções.

---

### Requisito 17: Resiliência e Tratamento de Erros

**User Story:** Como engenheiro de DevOps, eu quero que falhas parciais na geração do digest não impeçam a publicação do relatório, para que o operador receba sempre algum relatório mesmo em condições adversas.

#### Critérios de Aceitação

1. IF a invocação do Calculador_SLA para um canal específico falhar, THEN THE Lambda_DailyDigest SHALL registrar o erro, excluir o canal do Top3_Pior_SLA e continuar o processamento dos demais canais.
2. IF a coleta de dados do DynamoDB para um serviço específico falhar, THEN THE Lambda_DailyDigest SHALL registrar o erro, incluir o serviço no campo `servicos_com_erro` do Digest_Diario e continuar com os serviços disponíveis.
3. THE Lambda_DailyDigest SHALL incluir no Digest_Diario o campo `status_geracao` com valor "completo", "parcial" ou "erro", indicando se o digest foi gerado com todos os dados, com dados parciais ou com falha crítica.
4. WHEN o tempo de execução da Lambda_DailyDigest atingir 240 segundos (80% do timeout de 300s), THE Lambda_DailyDigest SHALL interromper coletas pendentes, marcar `status_geracao` como "parcial" e prosseguir para publicação e armazenamento com os dados já coletados.
5. THE Lambda_DailyDigest SHALL incluir no Digest_Diario o campo `tempo_geracao_segundos` com o tempo total de execução da geração do relatório.

---

### Requisito 18: Serialização e Round-Trip do Digest_Diario

**User Story:** Como engenheiro de dados, eu quero garantir que o Digest_Diario possa ser serializado para JSON e desserializado de volta sem perda de informação, para que a integridade dos dados seja preservada no armazenamento S3 e na comunicação com o Agente_Bedrock.

#### Critérios de Aceitação

1. FOR ALL Digest_Diario gerados pela Lambda_DailyDigest, serializar para JSON e desserializar de volta SHALL produzir um dicionário equivalente ao original (propriedade round-trip).
2. THE Lambda_DailyDigest SHALL serializar todos os campos numéricos do Digest_Diario como números JSON (não strings), incluindo `score_saude`, `total_canais_monitorados`, `fill_rate_medio`, `uptime_percentual` e `tempo_offline_minutos`.
3. THE Lambda_DailyDigest SHALL serializar os campos de data/hora (`periodo_inicio`, `periodo_fim`, `gerado_em`) como strings ISO 8601 com sufixo Z (UTC).
4. THE Lambda_DailyDigest SHALL preservar caracteres Unicode em português na serialização (`ensure_ascii=False`), garantindo que campos como `texto_formatado` e `mensagem_top3` sejam transmitidos corretamente.
5. THE Lambda_DailyDigest SHALL serializar campos booleanos (`disponivel`, `truncado`) como valores booleanos JSON nativos (true/false), não como strings.
