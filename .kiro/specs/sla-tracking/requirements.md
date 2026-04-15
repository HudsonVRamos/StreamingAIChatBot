# Documento de Requisitos — SLA Tracking

## Introdução

Esta funcionalidade adiciona ao Streaming Chatbot a capacidade de calcular e consultar o uptime/disponibilidade de cada canal de streaming com base nos eventos já coletados pelo Pipeline_Metricas. O sistema reutiliza integralmente os dados existentes no DynamoDB StreamingLogs e no bucket S3 KB_LOGS — sem nova coleta de métricas — para calcular o percentual de uptime, identificar incidentes (períodos contíguos com severidade ERROR ou CRITICAL) e responder perguntas em linguagem natural como "Qual foi o uptime do canal WARNER no último mês?".

A resposta padrão segue o formato: "99.7% — 2h12min de degradação em 3 incidentes". O sistema também suporta consulta de múltiplos canais (ex: "SLA de todos os canais Globo"), geração de relatório exportável em CSV/JSON via Action_Group_Export e roteamento automático pelo Agente_Bedrock para palavras-chave como "uptime", "SLA", "disponibilidade", "tempo fora do ar" e "incidentes".

A implementação é adicionada à Lambda_Configuradora como nova ação `sla` no path `/gerenciarRecurso` existente, evitando a criação de um novo path e respeitando o limite de paths do Action_Group_Config.

## Glossário

- **Lambda_Configuradora**: Função Lambda existente que executa operações nos serviços de streaming AWS. Receberá a nova ação `sla` no path `/gerenciarRecurso`.
- **DynamoDB_StreamingLogs**: Tabela DynamoDB com TTL de 30 dias, PK=`{servico}#{canal}`, SK=`{timestamp}#{metrica_nome}`, GSI_Severidade. Fonte primária de dados para o cálculo de SLA.
- **KB_LOGS**: Bucket S3 com eventos normalizados no formato Evento_Estruturado. Fonte de fallback quando o DynamoDB estiver indisponível.
- **Evento_Estruturado**: Registro normalizado contendo: timestamp, canal, severidade, tipo_erro, descricao, causa_provavel, recomendacao_correcao, servico_origem, metrica_nome, metrica_valor.
- **Incidente_SLA**: Período contíguo de tempo em que um canal apresentou pelo menos um evento com severidade ERROR ou CRITICAL. Um incidente começa no timestamp do primeiro evento anômalo e termina quando não há mais eventos ERROR/CRITICAL por um intervalo superior à Janela_Consolidacao.
- **Janela_Consolidacao**: Intervalo de tempo (padrão: 60 minutos) usado para agrupar eventos consecutivos em um único incidente. Eventos separados por menos de 60 minutos são considerados parte do mesmo incidente.
- **Uptime_Percentual**: Percentual de tempo em que o canal operou sem incidentes no período consultado. Calculado como: `((periodo_total_minutos - tempo_total_degradacao_minutos) / periodo_total_minutos) * 100`, arredondado para duas casas decimais.
- **Relatorio_SLA**: Estrutura de dados contendo: `canal`, `servico`, `periodo_dias`, `uptime_percentual`, `total_incidentes`, `tempo_total_degradacao_minutos`, `lista_incidentes` (cada um com `inicio`, `fim`, `duracao_minutos`, `severidade_maxima`, `eventos_count`).
- **Calculador_SLA**: Módulo dentro da Lambda_Configuradora responsável por consultar o DynamoDB_StreamingLogs, agrupar eventos em incidentes e calcular o Uptime_Percentual.
- **Action_Group_Export**: Action Group do Bedrock Agent que invoca a Lambda_Exportadora para gerar arquivos CSV/JSON para download.
- **Lambda_Exportadora**: Função Lambda existente que gera arquivos filtrados a partir das bases de conhecimento e armazena no S3_Exports com URL pré-assinada.
- **Agente_Bedrock**: Agente Amazon Bedrock que classifica intenções do usuário e roteia para Action Groups ou Knowledge Bases.
- **Frontend_Chat**: Interface web (chat.html) com sidebar de sugestões e área de chat conversacional.
- **Periodo_Consulta**: Intervalo de tempo em dias para o cálculo de SLA. Padrão: 30 dias. Mínimo: 1 dia. Máximo: 30 dias (limitado pelo TTL do DynamoDB).

## Requisitos

### Requisito 1: Endpoint de Cálculo de SLA na Lambda_Configuradora

**User Story:** Como operador de NOC, eu quero consultar o SLA de um canal de streaming pelo chatbot, para que eu possa saber o uptime percentual e os incidentes ocorridos em um período sem precisar analisar logs manualmente.

#### Critérios de Aceitação

1. WHEN o Agente_Bedrock envia uma requisição para o path `/gerenciarRecurso` com `acao=sla` e `resource_id` preenchido, THE Lambda_Configuradora SHALL rotear a requisição para o Calculador_SLA.
2. WHEN o parâmetro `periodo_dias` é fornecido, THE Lambda_Configuradora SHALL usar o valor informado como janela de cálculo. WHEN não fornecido, THE Lambda_Configuradora SHALL usar o padrão de 30 dias.
3. WHEN o parâmetro `servico` é fornecido, THE Lambda_Configuradora SHALL filtrar os eventos apenas do serviço especificado (MediaLive, MediaPackage, MediaTailor ou CloudFront). WHEN não fornecido, THE Lambda_Configuradora SHALL consultar todos os serviços que possuam eventos para o canal informado.
4. WHEN o parâmetro `resource_id` contiver um nome parcial (ex: "WARNER"), THE Lambda_Configuradora SHALL aplicar busca case-insensitive por substring no campo `canal` dos eventos do DynamoDB_StreamingLogs.
5. IF o parâmetro `resource_id` estiver ausente, THEN THE Lambda_Configuradora SHALL retornar erro 400 com a mensagem "Para acao=sla, parâmetro obrigatório: resource_id".
6. IF o parâmetro `periodo_dias` exceder 30, THEN THE Lambda_Configuradora SHALL retornar erro 400 com a mensagem "periodo_dias máximo é 30 (limitado pelo TTL do DynamoDB)".

### Requisito 2: Consulta de Eventos no DynamoDB StreamingLogs

**User Story:** Como engenheiro de dados, eu quero que o cálculo de SLA use os dados já armazenados no DynamoDB StreamingLogs como fonte primária, para que não seja necessária nova coleta de métricas e o cálculo seja rápido.

#### Critérios de Aceitação

1. WHEN o Calculador_SLA é acionado, THE Calculador_SLA SHALL consultar o DynamoDB_StreamingLogs usando o GSI_Severidade para recuperar todos os eventos do canal no período solicitado, filtrando por SK entre `{timestamp_inicio}` e `{timestamp_fim}`.
2. THE Calculador_SLA SHALL construir a chave de partição no formato `{servico}#{canal}` para consultar o DynamoDB_StreamingLogs, iterando sobre os serviços relevantes quando `servico` não for especificado.
3. WHEN a consulta ao DynamoDB retornar mais de 1 MB de dados (limite de página), THE Calculador_SLA SHALL paginar automaticamente usando o `LastEvaluatedKey` até obter todos os eventos do período.
4. THE Calculador_SLA SHALL recuperar apenas os campos necessários para o cálculo: `timestamp`, `severidade`, `tipo_erro`, `metrica_nome`, `metrica_valor` — usando ProjectionExpression para reduzir o consumo de RCU.
5. IF a consulta ao DynamoDB_StreamingLogs falhar, THEN THE Calculador_SLA SHALL registrar o erro e acionar o fallback para o S3 KB_LOGS conforme o Requisito 3.

### Requisito 3: Fallback para S3 KB_LOGS

**User Story:** Como engenheiro de DevOps, eu quero que o cálculo de SLA tenha resiliência a falhas do DynamoDB, para que o operador ainda consiga consultar o SLA mesmo durante uma indisponibilidade do banco de dados.

#### Critérios de Aceitação

1. IF o DynamoDB_StreamingLogs estiver indisponível ou retornar erro, THEN THE Calculador_SLA SHALL consultar os eventos diretamente no bucket S3 KB_LOGS, listando objetos com prefixo `{servico}/{canal}` e filtrando por data de modificação dentro do período solicitado.
2. WHEN o fallback para S3 é ativado, THE Calculador_SLA SHALL incluir o campo `fonte_dados="s3_fallback"` na resposta do Relatorio_SLA para indicar que os dados vieram do S3.
3. WHEN o fallback para S3 é ativado, THE Calculador_SLA SHALL ler e desserializar cada arquivo JSON do KB_LOGS, extraindo os campos `timestamp`, `severidade`, `canal` e `tipo_erro` para o cálculo.
4. IF tanto o DynamoDB quanto o S3 KB_LOGS estiverem indisponíveis, THEN THE Lambda_Configuradora SHALL retornar erro 503 com a mensagem "Fontes de dados indisponíveis. Tente novamente em instantes."

### Requisito 4: Identificação e Agrupamento de Incidentes

**User Story:** Como operador de NOC, eu quero que o sistema identifique automaticamente os períodos de degradação como incidentes, para que eu possa ver quantas vezes o canal ficou fora do ar e por quanto tempo em cada ocorrência.

#### Critérios de Aceitação

1. THE Calculador_SLA SHALL considerar como Incidente_SLA qualquer período contíguo em que o canal apresente eventos com severidade ERROR ou CRITICAL.
2. WHEN dois eventos anômalos consecutivos estiverem separados por um intervalo menor que a Janela_Consolidacao (60 minutos), THE Calculador_SLA SHALL agrupá-los no mesmo Incidente_SLA.
3. WHEN dois eventos anômalos consecutivos estiverem separados por um intervalo igual ou superior à Janela_Consolidacao, THE Calculador_SLA SHALL tratá-los como incidentes distintos.
4. THE Calculador_SLA SHALL calcular a duração de cada Incidente_SLA como a diferença em minutos entre o timestamp do primeiro e do último evento do incidente, acrescida de um período de granularidade (5 minutos) para representar a duração mínima de um evento.
5. THE Calculador_SLA SHALL determinar a `severidade_maxima` de cada Incidente_SLA usando a hierarquia INFO < WARNING < ERROR < CRITICAL, retornando a severidade mais alta encontrada entre os eventos do incidente.
6. THE Calculador_SLA SHALL contar o número de eventos distintos (por `metrica_nome`) que compõem cada Incidente_SLA e armazenar no campo `eventos_count`.
7. WHEN nenhum evento ERROR ou CRITICAL for encontrado no período, THE Calculador_SLA SHALL retornar `total_incidentes=0`, `tempo_total_degradacao_minutos=0` e `uptime_percentual=100.00`.

### Requisito 5: Cálculo do Uptime Percentual

**User Story:** Como operador de NOC, eu quero receber o uptime percentual do canal de forma clara e precisa, para que eu possa reportar o SLA para stakeholders e comparar com metas estabelecidas.

#### Critérios de Aceitação

1. THE Calculador_SLA SHALL calcular o `uptime_percentual` usando a fórmula: `((periodo_total_minutos - tempo_total_degradacao_minutos) / periodo_total_minutos) * 100`, arredondado para duas casas decimais.
2. THE Calculador_SLA SHALL calcular o `periodo_total_minutos` como `periodo_dias * 24 * 60`.
3. THE Calculador_SLA SHALL calcular o `tempo_total_degradacao_minutos` como a soma das durações de todos os Incidente_SLA identificados no período.
4. WHEN o `tempo_total_degradacao_minutos` calculado exceder o `periodo_total_minutos`, THE Calculador_SLA SHALL limitar o `uptime_percentual` ao valor mínimo de 0.00 e registrar um aviso no log.
5. THE Calculador_SLA SHALL incluir no Relatorio_SLA o campo `tempo_total_degradacao_formatado` com a duração total de degradação em formato legível (ex: "2h12min" para 132 minutos, "45min" para 45 minutos, "3d 4h 10min" para valores acima de 24 horas).

### Requisito 6: Resposta Estruturada do Relatorio_SLA

**User Story:** Como operador de NOC, eu quero receber uma resposta estruturada com todos os detalhes do SLA, para que o chatbot possa formatar a informação de forma clara e eu possa exportar os dados se necessário.

#### Critérios de Aceitação

1. THE Lambda_Configuradora SHALL retornar o Relatorio_SLA contendo os campos: `canal` (nome do canal consultado), `servico` (serviço ou lista de serviços consultados), `periodo_dias` (período consultado), `data_inicio` (ISO 8601), `data_fim` (ISO 8601), `uptime_percentual` (número com 2 casas decimais), `total_incidentes` (inteiro), `tempo_total_degradacao_minutos` (inteiro), `tempo_total_degradacao_formatado` (string legível), `lista_incidentes` (array de objetos), `fonte_dados` (string: "dynamodb" ou "s3_fallback") e `mensagem_resumo` (string em português).
2. THE Lambda_Configuradora SHALL incluir em cada item de `lista_incidentes` os campos: `inicio` (ISO 8601), `fim` (ISO 8601), `duracao_minutos` (inteiro), `duracao_formatada` (string legível), `severidade_maxima` (string) e `eventos_count` (inteiro).
3. THE Lambda_Configuradora SHALL ordenar a `lista_incidentes` por `inicio` em ordem decrescente (incidente mais recente primeiro).
4. THE Lambda_Configuradora SHALL incluir o campo `mensagem_resumo` em português com texto descritivo no formato: "{uptime_percentual}% — {tempo_total_degradacao_formatado} de degradação em {total_incidentes} incidente(s)". Exemplo: "99.70% — 2h12min de degradação em 3 incidentes".
5. WHEN `total_incidentes` for 0, THE Lambda_Configuradora SHALL definir `mensagem_resumo` como "{uptime_percentual}% — Nenhum incidente registrado no período".

### Requisito 7: Consulta de SLA de Múltiplos Canais

**User Story:** Como operador de NOC, eu quero consultar o SLA de um grupo de canais de uma só vez (ex: "SLA de todos os canais Globo"), para que eu possa ter uma visão consolidada da disponibilidade de um grupo de canais sem precisar consultar um por um.

#### Critérios de Aceitação

1. WHEN o `resource_id` corresponder a múltiplos canais na busca por substring, THE Lambda_Configuradora SHALL calcular o Relatorio_SLA para cada canal encontrado e retornar uma lista de relatórios no campo `relatorios`.
2. THE Lambda_Configuradora SHALL incluir no campo `resumo_grupo` um objeto com: `total_canais` (inteiro), `uptime_medio` (média dos uptime_percentual, 2 casas decimais), `canal_pior_sla` (nome do canal com menor uptime_percentual) e `canal_melhor_sla` (nome do canal com maior uptime_percentual).
3. WHEN a consulta de múltiplos canais retornar mais de 20 canais, THE Lambda_Configuradora SHALL limitar o processamento aos 20 primeiros canais encontrados e incluir o campo `aviso` com a mensagem "Exibindo os 20 primeiros canais de {total_encontrado} encontrados. Use resource_id mais específico para refinar a busca."
4. THE Lambda_Configuradora SHALL processar os relatórios de múltiplos canais de forma sequencial, respeitando o timeout de 120 segundos da Lambda.
5. WHEN apenas um canal corresponder ao `resource_id`, THE Lambda_Configuradora SHALL retornar o Relatorio_SLA diretamente (sem o wrapper `relatorios`), mantendo compatibilidade com o formato de canal único.

### Requisito 8: Exportação de Relatório SLA

**User Story:** Como operador de NOC, eu quero exportar o relatório de SLA em CSV ou JSON, para que eu possa compartilhar os dados com stakeholders ou importar em ferramentas de análise externas.

#### Critérios de Aceitação

1. WHEN o usuário solicitar exportação do relatório SLA (ex: "exportar SLA do canal WARNER em CSV"), THE Agente_Bedrock SHALL acionar o Action_Group_Export com os parâmetros `tipo="sla"`, `resource_id`, `periodo_dias` e `formato` ("CSV" ou "JSON").
2. THE Lambda_Exportadora SHALL aceitar o parâmetro `tipo="sla"` e invocar internamente a lógica do Calculador_SLA para obter os dados antes de gerar o arquivo.
3. WHEN o formato solicitado for CSV, THE Lambda_Exportadora SHALL gerar um arquivo CSV com as colunas: `canal`, `servico`, `periodo_dias`, `data_inicio`, `data_fim`, `uptime_percentual`, `total_incidentes`, `tempo_total_degradacao_minutos`, `incidente_inicio`, `incidente_fim`, `incidente_duracao_minutos`, `incidente_severidade_maxima` — com uma linha por incidente (e uma linha com campos de incidente vazios quando `total_incidentes=0`).
4. WHEN o formato solicitado for JSON, THE Lambda_Exportadora SHALL gerar um arquivo JSON contendo o Relatorio_SLA completo conforme definido no Requisito 6.
5. THE Lambda_Exportadora SHALL armazenar o arquivo gerado no S3_Exports e retornar uma URL pré-assinada com validade de 1 hora para download.
6. THE Lambda_Exportadora SHALL nomear o arquivo no formato `sla-{resource_id}-{periodo_dias}d-{timestamp}.{extensao}` (ex: `sla-WARNER-30d-20240115T120000Z.csv`).

### Requisito 9: Roteamento do Agente Bedrock para SLA

**User Story:** Como operador de NOC, eu quero que o chatbot reconheça automaticamente perguntas sobre SLA e disponibilidade em linguagem natural, para que eu possa simplesmente perguntar "Qual foi o uptime do canal WARNER no último mês?" e obter a resposta.

#### Critérios de Aceitação

1. WHEN o usuário envia mensagem contendo palavras-chave de SLA ("uptime", "SLA", "disponibilidade", "tempo fora do ar", "incidentes", "degradação", "indisponibilidade"), THE Agente_Bedrock SHALL classificar a intenção como consulta de SLA e rotear para `/gerenciarRecurso` com `acao=sla`.
2. THE Agente_Bedrock SHALL extrair o nome do canal (`resource_id`) e o período (`periodo_dias`) da mensagem do usuário. WHEN o período não for mencionado, THE Agente_Bedrock SHALL usar o padrão de 30 dias.
3. WHEN o usuário mencionar "último mês" ou "mês passado", THE Agente_Bedrock SHALL mapear para `periodo_dias=30`. WHEN mencionar "última semana", SHALL mapear para `periodo_dias=7`. WHEN mencionar "hoje" ou "últimas 24 horas", SHALL mapear para `periodo_dias=1`.
4. WHEN o resultado do SLA é recebido, THE Agente_Bedrock SHALL formatar a resposta em português com: uptime percentual em destaque, tempo total de degradação formatado, número de incidentes e lista resumida dos incidentes (início, duração e severidade máxima de cada um).
5. WHEN o resultado contiver `relatorios` (múltiplos canais), THE Agente_Bedrock SHALL apresentar primeiro o `resumo_grupo` e depois listar os canais ordenados por `uptime_percentual` crescente (pior SLA primeiro).

### Requisito 10: Schema OpenAPI e Integração com Action_Group_Config

**User Story:** Como desenvolvedor, eu quero que a ação `sla` esteja definida no schema OpenAPI do Action_Group_Config, para que o Agente_Bedrock possa invocar o cálculo de SLA automaticamente com os parâmetros corretos.

#### Critérios de Aceitação

1. THE schema OpenAPI do Action_Group_Config SHALL adicionar `sla` ao enum do parâmetro `acao` do path `/gerenciarRecurso`, com descrição indicando que calcula o uptime e lista incidentes de um canal no período especificado.
2. THE schema OpenAPI SHALL documentar os parâmetros utilizados pela ação `sla`: `resource_id` (string, obrigatório — nome parcial do canal), `periodo_dias` (integer, opcional, padrão 30, máximo 30) e `servico` (string, opcional — filtra por serviço específico).
3. THE schema OpenAPI SHALL documentar o formato de resposta da ação `sla` incluindo os campos `uptime_percentual`, `total_incidentes`, `tempo_total_degradacao_minutos` e `lista_incidentes`.
4. WHILE o schema OpenAPI do Action_Group_Config possui paths existentes, THE schema SHALL manter o total de paths dentro do limite de 9 após a adição da ação `sla` (que reutiliza o path `/gerenciarRecurso` já existente, sem criar novo path).

### Requisito 11: Sugestões de SLA no Frontend

**User Story:** Como operador de NOC, eu quero ter sugestões de consulta de SLA na sidebar do chat, para que eu possa iniciar uma consulta de disponibilidade com um clique.

#### Critérios de Aceitação

1. THE Frontend_Chat SHALL incluir botões de sugestão na seção "🔍 Logs & Métricas" da sidebar para consultas de SLA: "Qual o uptime do canal WARNER no último mês?", "SLA de todos os canais Globo nos últimos 7 dias", "Exportar relatório SLA do canal ESPN em CSV".
2. WHEN o usuário clica em um botão de sugestão de SLA, THE Frontend_Chat SHALL inserir o texto da sugestão no campo de entrada do chat, seguindo o mesmo comportamento dos botões de sugestão existentes.

### Requisito 12: Prompt do Agente Bedrock para SLA

**User Story:** Como desenvolvedor, eu quero que o prompt do Agente_Bedrock inclua regras de roteamento para consultas de SLA, para que o agente saiba quando e como acionar a funcionalidade e como formatar a resposta.

#### Critérios de Aceitação

1. THE Agente_Bedrock SHALL incluir uma nova rota de prioridade no prompt, dedicada a consultas de SLA, posicionada entre as rotas HEALTH_CHECK_MASSA e LOGS_HISTÓRICOS.
2. THE rota de SLA SHALL conter palavras-chave: "uptime", "SLA", "disponibilidade", "tempo fora do ar", "incidentes", "degradação", "indisponibilidade", "ficou fora", "caiu por quanto tempo".
3. THE Agente_Bedrock SHALL diferenciar entre consulta de SLA (rota SLA com `acao=sla`) e consulta de logs históricos (rota LOGS_HISTÓRICOS via KB_LOGS) com base na presença de palavras como "uptime", "percentual", "disponibilidade" versus "por que caiu", "o que aconteceu".
4. WHEN o Agente_Bedrock apresenta o resultado do SLA, THE Agente_Bedrock SHALL exibir o `mensagem_resumo` em destaque, seguido da lista de incidentes formatada com data de início, duração e severidade máxima de cada incidente.

### Requisito 13: Resiliência e Tratamento de Erros

**User Story:** Como engenheiro de DevOps, eu quero que o cálculo de SLA seja resiliente a falhas e dados ausentes, para que o operador receba sempre uma resposta útil mesmo em condições adversas.

#### Critérios de Aceitação

1. IF nenhum evento for encontrado para o canal no período solicitado, THEN THE Lambda_Configuradora SHALL retornar um Relatorio_SLA com `uptime_percentual=100.00`, `total_incidentes=0` e `mensagem_resumo` indicando "Nenhum evento registrado para '{resource_id}' nos últimos {periodo_dias} dias. Uptime assumido: 100.00%".
2. IF o canal não for encontrado em nenhuma fonte de dados, THEN THE Lambda_Configuradora SHALL retornar erro 404 com a mensagem "Canal '{resource_id}' não encontrado no período especificado."
3. IF o cálculo de SLA exceder 100 segundos de execução (aproximando-se do timeout de 120s da Lambda), THEN THE Lambda_Configuradora SHALL interromper o processamento de canais pendentes e retornar os relatórios já calculados com o campo `aviso` indicando que o resultado é parcial.
4. THE Lambda_Configuradora SHALL incluir no Relatorio_SLA o campo `erros` (lista) contendo mensagens de erro de fontes de dados ou canais que falharam durante o cálculo.
5. WHEN o DynamoDB retornar `ProvisionedThroughputExceededException`, THE Calculador_SLA SHALL aplicar backoff exponencial com até 3 tentativas antes de acionar o fallback para S3.

### Requisito 14: Serialização e Round-Trip do Relatorio_SLA

**User Story:** Como engenheiro de dados, eu quero garantir que o Relatorio_SLA possa ser serializado para JSON e desserializado de volta sem perda de informação, para que a integridade dos dados seja preservada na comunicação entre Lambda e Agente_Bedrock e na geração de arquivos de exportação.

#### Critérios de Aceitação

1. FOR ALL Relatorio_SLA gerados pelo Calculador_SLA, serializar para JSON e desserializar de volta SHALL produzir um dicionário equivalente ao original (propriedade round-trip).
2. THE Lambda_Configuradora SHALL serializar todos os campos numéricos do Relatorio_SLA como números JSON (não strings), incluindo `uptime_percentual`, `total_incidentes`, `tempo_total_degradacao_minutos` e `duracao_minutos` de cada incidente.
3. THE Lambda_Configuradora SHALL serializar os campos de data/hora (`data_inicio`, `data_fim`, `inicio` e `fim` dos incidentes) como strings ISO 8601 com sufixo Z (UTC).
4. THE Lambda_Configuradora SHALL preservar caracteres Unicode em português na serialização (ensure_ascii=False), garantindo que campos como `mensagem_resumo` e `tempo_total_degradacao_formatado` sejam transmitidos corretamente.
