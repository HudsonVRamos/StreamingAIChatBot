# Documento de Requisitos — Health Check em Massa

## Introdução

Esta funcionalidade adiciona ao Streaming Chatbot a capacidade de executar um health check em massa de todos os canais/recursos de streaming de uma só vez. Quando o usuário pergunta "Qual a saúde de todos os canais?" ou "Health check de todos os canais", o Agente_Bedrock aciona a Lambda_Configuradora que consulta métricas CloudWatch para TODOS os recursos de um ou mais serviços em batch, classifica cada recurso com indicadores de semáforo (verde/amarelo/vermelho) e retorna um dashboard resumido com totais por cor, lista de recursos com problemas e um score geral de saúde.

A implementação reutiliza a infraestrutura existente: `_ONDEMAND_METRICS_CONFIG` para definições de métricas, `_ONDEMAND_SEVERITY_THRESHOLDS` para classificação de severidade, `_classify_severity_ondemand()` para classificar valores, `list_resources()` para descoberta de recursos e `_consultar_metricas_build_queries()` para construção de queries CloudWatch. A diferença fundamental é que o endpoint existente `/consultarMetricas` consulta métricas de UM recurso por vez, enquanto o health check em massa consulta TODOS os recursos em batch usando chamadas `GetMetricData` otimizadas (até 500 queries por chamada).

A arquitetura é cross-region: métricas de MediaLive e MediaPackage são coletadas em sa-east-1, enquanto métricas de MediaTailor e CloudFront são coletadas em us-east-1. O Lambda timeout é 120 segundos, então a implementação precisa ser eficiente para lidar com ~220 canais.

## Glossário

- **Lambda_Configuradora**: Função Lambda existente (timeout 120s) que executa operações nos serviços de streaming AWS. Já possui `/consultarMetricas` para consulta individual e `list_resources()` para listagem
- **Health_Check_Massa**: Operação que consulta métricas CloudWatch de TODOS os recursos de um ou mais serviços em batch e retorna um dashboard com classificação semáforo
- **Indicador_Semaforo**: Classificação visual de saúde de cada recurso: verde (INFO — operação normal), amarelo (WARNING — degradação detectada), vermelho (ERROR ou CRITICAL — problema ativo)
- **Dashboard_Saude**: Resposta estruturada contendo: totais por cor, lista de recursos vermelhos e amarelos com detalhes, score geral de saúde e timestamp da consulta
- **Score_Saude**: Percentual de recursos verdes sobre o total de recursos consultados. Exemplo: 200 verdes de 220 total = 90.9%
- **Batch_GetMetricData**: Chamada CloudWatch GetMetricData com múltiplas MetricDataQueries (até 500 por chamada) para consultar métricas de vários recursos em uma única requisição
- **Agente_Bedrock**: Agente Amazon Bedrock que classifica intenções do usuário e roteia para Action Groups ou Knowledge Bases
- **Action_Group_Config**: Action Group do Bedrock Agent que invoca a Lambda_Configuradora. Schema OpenAPI com 7 paths após channel-comparison (limite de 9)
- **Metricas_Chave**: Subconjunto reduzido de métricas por serviço usado no health check em massa para manter eficiência. Menos métricas que a consulta individual, focando nas mais críticas para classificação de saúde
- **Frontend_Chat**: Interface web (chat.html) com sidebar de sugestões e área de chat conversacional

## Requisitos

### Requisito 1: Endpoint de Health Check em Massa na Lambda_Configuradora

**User Story:** Como operador de NOC, eu quero consultar a saúde de todos os canais de streaming de uma só vez, para que eu possa ter uma visão geral rápida do estado da plataforma sem precisar consultar canal por canal.

#### Critérios de Aceitação

1. WHEN o Agente_Bedrock envia uma requisição para o path `/gerenciarRecurso` com `acao=healthcheck`, THE Lambda_Configuradora SHALL executar um health check em massa de todos os recursos dos serviços solicitados.
2. WHEN o parâmetro `servico` é fornecido (ex: "MediaLive"), THE Lambda_Configuradora SHALL consultar métricas apenas dos recursos daquele serviço.
3. WHEN o parâmetro `servico` não é fornecido, THE Lambda_Configuradora SHALL consultar métricas de todos os quatro serviços: MediaLive, MediaPackage, MediaTailor e CloudFront.
4. WHEN o parâmetro `periodo_minutos` é fornecido, THE Lambda_Configuradora SHALL usar o período especificado para a consulta de métricas. WHEN não fornecido, THE Lambda_Configuradora SHALL usar o padrão de 15 minutos.
5. THE Lambda_Configuradora SHALL retornar a resposta dentro do timeout de 120 segundos da Lambda, mesmo para ~220 canais MediaLive.

### Requisito 2: Descoberta de Recursos para Health Check

**User Story:** Como operador de NOC, eu quero que o health check descubra automaticamente todos os recursos ativos, para que novos canais sejam incluídos sem configuração manual.

#### Critérios de Aceitação

1. WHEN o health check é executado para MediaLive, THE Lambda_Configuradora SHALL listar todos os canais MediaLive usando a função `list_resources()` existente e extrair o ID e nome de cada canal.
2. WHEN o health check é executado para MediaPackage, THE Lambda_Configuradora SHALL listar todos os canais MediaPackage V2 usando `list_resources()` e extrair channel_group e nome de cada canal.
3. WHEN o health check é executado para MediaTailor, THE Lambda_Configuradora SHALL listar todas as configurações de playback usando `list_resources()` e extrair o nome de cada configuração.
4. WHEN o health check é executado para CloudFront, THE Lambda_Configuradora SHALL listar todas as distribuições usando `list_resources()` e extrair o ID e domain de cada distribuição.
5. IF a listagem de recursos de um serviço falhar, THEN THE Lambda_Configuradora SHALL registrar o erro, continuar com os demais serviços e incluir o serviço com erro no campo `erros` da resposta.

### Requisito 3: Consulta de Métricas em Batch Otimizada

**User Story:** Como engenheiro de DevOps, eu quero que o health check use consultas CloudWatch em batch, para que a operação seja eficiente e caiba no timeout de 120 segundos mesmo com centenas de canais.

#### Critérios de Aceitação

1. THE Lambda_Configuradora SHALL usar um subconjunto de Metricas_Chave por serviço para o health check em massa, reduzindo o número de queries por recurso em comparação com a consulta individual `/consultarMetricas`.
2. THE Lambda_Configuradora SHALL agrupar as MetricDataQueries em chamadas batch de até 500 queries por chamada `GetMetricData`, conforme o limite da API CloudWatch.
3. WHEN o total de queries exceder 500, THE Lambda_Configuradora SHALL dividir em múltiplas chamadas `GetMetricData` sequenciais.
4. THE Lambda_Configuradora SHALL usar clientes CloudWatch separados por região: sa-east-1 para MediaLive e MediaPackage, us-east-1 para MediaTailor e CloudFront.
5. THE Lambda_Configuradora SHALL usar granularidade de 300 segundos (5 minutos) e estatística conforme definido em `_ONDEMAND_METRICS_CONFIG` para cada métrica.
6. IF uma chamada `GetMetricData` retornar throttling (TooManyRequestsException), THEN THE Lambda_Configuradora SHALL aplicar backoff exponencial com até 3 tentativas antes de registrar o erro e continuar.

### Requisito 4: Classificação Semáforo por Recurso

**User Story:** Como operador de NOC, eu quero que cada recurso seja classificado com um indicador de semáforo (verde/amarelo/vermelho), para que eu possa identificar rapidamente quais canais precisam de atenção.

#### Critérios de Aceitação

1. THE Lambda_Configuradora SHALL classificar cada métrica de cada recurso usando a função `_classify_severity_ondemand()` existente com os thresholds de `_ONDEMAND_SEVERITY_THRESHOLDS`.
2. WHEN todas as métricas de um recurso retornam severidade INFO, THE Lambda_Configuradora SHALL classificar o recurso como verde.
3. WHEN a severidade mais alta de qualquer métrica de um recurso é WARNING, THE Lambda_Configuradora SHALL classificar o recurso como amarelo.
4. WHEN a severidade mais alta de qualquer métrica de um recurso é ERROR ou CRITICAL, THE Lambda_Configuradora SHALL classificar o recurso como vermelho.
5. THE Lambda_Configuradora SHALL usar a hierarquia de severidade existente `_SEVERITY_ORDER` (INFO < WARNING < ERROR < CRITICAL) para determinar a severidade mais alta de cada recurso.
6. WHEN um recurso não retorna dados de métricas (sem data points no período), THE Lambda_Configuradora SHALL classificar o recurso como verde com uma nota indicando "sem dados no período".

### Requisito 5: Resposta Dashboard de Saúde

**User Story:** Como operador de NOC, eu quero receber um resumo tipo dashboard com totais por cor, lista de problemas e score geral, para que eu tenha uma visão executiva da saúde da plataforma.

#### Critérios de Aceitação

1. THE Lambda_Configuradora SHALL retornar um Dashboard_Saude contendo: `timestamp` (ISO 8601 da consulta), `periodo` (período consultado em minutos), `servicos_consultados` (lista de serviços), `total_recursos` (total de recursos consultados), `totais` (contadores verde, amarelo, vermelho), `score_saude` (percentual de recursos verdes), `recursos_vermelho` (lista de recursos vermelhos com detalhes) e `recursos_amarelo` (lista de recursos amarelos com detalhes).
2. THE Lambda_Configuradora SHALL calcular o Score_Saude como: (total de recursos verdes / total de recursos consultados) multiplicado por 100, arredondado para uma casa decimal.
3. WHEN um recurso é classificado como vermelho, THE Lambda_Configuradora SHALL incluir na lista `recursos_vermelho`: nome do recurso, serviço, severidade mais alta, lista de alertas (métrica, valor, severidade, tipo_erro).
4. WHEN um recurso é classificado como amarelo, THE Lambda_Configuradora SHALL incluir na lista `recursos_amarelo`: nome do recurso, serviço, lista de alertas (métrica, valor, severidade, tipo_erro).
5. THE Lambda_Configuradora SHALL ordenar a lista `recursos_vermelho` por severidade decrescente (CRITICAL antes de ERROR) e a lista `recursos_amarelo` por nome do recurso.
6. THE Lambda_Configuradora SHALL incluir um campo `mensagem_resumo` em português com texto descritivo. Exemplo: "Dashboard de saúde: 195 verdes, 15 amarelos, 10 vermelhos de 220 recursos. Score: 88.6%"

### Requisito 6: Seleção de Métricas-Chave para Eficiência

**User Story:** Como engenheiro de DevOps, eu quero que o health check use apenas as métricas mais críticas por serviço, para que a consulta seja rápida e caiba no limite de 500 queries por chamada GetMetricData.

#### Critérios de Aceitação

1. THE Lambda_Configuradora SHALL usar as seguintes Metricas_Chave para MediaLive: ActiveAlerts (Maximum), InputLossSeconds (Sum), DroppedFrames (Sum), Output4xxErrors (Sum), Output5xxErrors (Sum).
2. THE Lambda_Configuradora SHALL usar as seguintes Metricas_Chave para MediaPackage: EgressResponseTime (Average), IngressBytes (Sum).
3. THE Lambda_Configuradora SHALL usar as seguintes Metricas_Chave para MediaTailor: AdDecisionServer.Errors (Sum), Avail.FillRate (Average).
4. THE Lambda_Configuradora SHALL usar as seguintes Metricas_Chave para CloudFront: 5xxErrorRate (Average), TotalErrorRate (Average).
5. THE Lambda_Configuradora SHALL definir as Metricas_Chave em um dicionário Python `_HEALTHCHECK_METRICS` separado do `_ONDEMAND_METRICS_CONFIG`, permitindo ajuste independente.

### Requisito 7: Schema OpenAPI e Roteamento do Agente

**User Story:** Como operador de NOC, eu quero que o chatbot reconheça automaticamente pedidos de health check em massa em linguagem natural, para que eu possa simplesmente perguntar "Qual a saúde de todos os canais?" e obter o dashboard.

#### Critérios de Aceitação

1. THE schema OpenAPI do Action_Group_Config SHALL adicionar `healthcheck` ao enum do parâmetro `acao` do path `/gerenciarRecurso`, com descrição indicando que executa health check em massa de todos os recursos.
2. THE schema OpenAPI SHALL adicionar o parâmetro `periodo_minutos` (integer, opcional, padrão 15) ao path `/gerenciarRecurso` para uso com `acao=healthcheck`.
3. WHEN o usuário envia mensagem contendo palavras-chave de health check em massa ("saúde de todos", "health check de todos", "health check geral", "dashboard de saúde", "status de todos os canais", "como estão todos os canais"), THE Agente_Bedrock SHALL classificar a intenção como health check em massa e rotear para `gerenciarRecurso` com `acao=healthcheck`.
4. WHEN o usuário especifica um serviço (ex: "saúde de todos os canais MediaLive"), THE Agente_Bedrock SHALL incluir o parâmetro `servico` na chamada.
5. WHEN o resultado do health check é recebido, THE Agente_Bedrock SHALL formatar a resposta como um dashboard legível em português com indicadores de semáforo (🟢 🟡 🔴), totais, score e lista de problemas.

### Requisito 8: Prompt do Agente Bedrock para Health Check em Massa

**User Story:** Como desenvolvedor, eu quero que o prompt do Agente_Bedrock inclua regras de roteamento para health check em massa, para que o agente saiba quando e como acionar a funcionalidade.

#### Critérios de Aceitação

1. THE Agente_Bedrock SHALL incluir uma nova rota de prioridade entre MÉTRICAS_TEMPO_REAL e LOGS_HISTÓRICOS no prompt, dedicada ao health check em massa.
2. THE rota de health check em massa SHALL conter palavras-chave: "saúde de todos", "health check de todos", "health check geral", "dashboard de saúde", "status geral", "como estão todos os canais", "visão geral de saúde".
3. THE Agente_Bedrock SHALL diferenciar entre consulta de métricas individual (rota MÉTRICAS_TEMPO_REAL com `/consultarMetricas`) e health check em massa (rota nova com `/gerenciarRecurso` acao=healthcheck) baseado na presença de palavras como "todos", "geral", "dashboard".
4. WHEN o Agente_Bedrock apresenta o resultado do health check, THE Agente_Bedrock SHALL mostrar primeiro o score geral e totais, depois listar recursos vermelhos com detalhes, depois recursos amarelos, e omitir recursos verdes (apenas informar o total).

### Requisito 9: Sugestões de Health Check no Frontend

**User Story:** Como operador de NOC, eu quero ter sugestões de health check em massa na sidebar do chat, para que eu possa iniciar o health check com um clique.

#### Critérios de Aceitação

1. THE Frontend_Chat SHALL incluir botões de sugestão na seção "🔍 Logs & Métricas" da sidebar para health check em massa: "Qual a saúde de todos os canais?", "Health check de todos os canais MediaLive", "Dashboard de saúde geral".
2. WHEN o usuário clica em um botão de sugestão de health check, THE Frontend_Chat SHALL inserir o texto da sugestão no campo de entrada do chat, seguindo o mesmo comportamento dos botões de sugestão existentes.

### Requisito 10: Resiliência e Tratamento de Erros

**User Story:** Como engenheiro de DevOps, eu quero que o health check seja resiliente a falhas parciais, para que a indisponibilidade de um serviço ou recurso não impeça o dashboard dos demais.

#### Critérios de Aceitação

1. IF a consulta de métricas de um serviço inteiro falhar, THEN THE Lambda_Configuradora SHALL registrar o erro, continuar com os demais serviços e incluir o serviço com erro no campo `erros` da resposta do Dashboard_Saude.
2. IF a chamada GetMetricData retornar dados parciais (NextToken), THEN THE Lambda_Configuradora SHALL paginar automaticamente para obter todos os resultados.
3. IF o tempo de execução se aproximar do timeout de 120 segundos (ex: 100 segundos decorridos), THEN THE Lambda_Configuradora SHALL interromper consultas pendentes e retornar o Dashboard_Saude parcial com os dados já coletados, incluindo uma nota indicando que o resultado é parcial.
4. THE Lambda_Configuradora SHALL incluir no Dashboard_Saude um campo `erros` (lista) contendo mensagens de erro de serviços ou recursos que falharam durante a coleta.
5. IF nenhum recurso for encontrado para os serviços solicitados, THEN THE Lambda_Configuradora SHALL retornar um Dashboard_Saude vazio com `total_recursos=0` e `score_saude=100.0` e mensagem indicando que nenhum recurso foi encontrado.

### Requisito 11: Serialização e Round-Trip do Dashboard de Saúde

**User Story:** Como engenheiro de dados, eu quero garantir que o Dashboard_Saude possa ser serializado para JSON e desserializado de volta sem perda de informação, para que a integridade dos dados seja preservada na comunicação entre Lambda e Agente.

#### Critérios de Aceitação

1. FOR ALL Dashboard_Saude gerados pelo health check, serializar para JSON e desserializar de volta SHALL produzir um dicionário equivalente ao original (propriedade round-trip).
2. THE Lambda_Configuradora SHALL serializar todos os campos numéricos do Dashboard_Saude como números JSON (não strings).
3. THE Lambda_Configuradora SHALL serializar o campo `timestamp` como string ISO 8601 com sufixo Z (UTC).
4. THE Lambda_Configuradora SHALL preservar caracteres Unicode em português na serialização (ensure_ascii=False).
