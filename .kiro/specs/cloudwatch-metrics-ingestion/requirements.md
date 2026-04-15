# Documento de Requisitos — Ingestão de Métricas CloudWatch

## Introdução

Este documento descreve os requisitos para substituir a abordagem atual de ingestão baseada em CloudWatch Logs pela ingestão baseada em CloudWatch Metrics no projeto Streaming Chatbot. A Lambda Pipeline_Logs existente tenta ler log groups do CloudWatch que não existem no ambiente do usuário. A nova abordagem consulta métricas do CloudWatch para os quatro serviços de streaming (MediaLive, MediaPackage V2, MediaTailor e CloudFront), gera eventos normalizados no formato Evento_Estruturado com classificação de severidade baseada em thresholds, e armazena no bucket KB_LOGS para consultas RAG pelo chatbot.

A arquitetura é cross-region: métricas de MediaLive e MediaPackage V2 são coletadas em **sa-east-1**, enquanto métricas de MediaTailor e CloudFront são coletadas em **us-east-1**. O pipeline executa a cada 1 hora via EventBridge, consultando métricas da última hora com granularidade de 5 minutos (período). Os recursos (canais, distribuições, configurações) são descobertos dinamicamente via APIs de listagem — nenhum ID é hardcoded.

## Glossário

- **Pipeline_Metricas**: Função AWS Lambda que substitui a Pipeline_Logs atual, coletando métricas do CloudWatch em vez de logs. Executada a cada 1 hora via EventBridge
- **CloudWatch_Metrics**: Serviço AWS CloudWatch utilizado para consultar métricas numéricas de performance e saúde dos serviços de streaming
- **Metrica_Bruta**: Dado numérico retornado pela API GetMetricData do CloudWatch, contendo namespace, metric name, dimensions, timestamps e values
- **Evento_Estruturado**: Registro normalizado contendo: timestamp, canal, severidade, tipo_erro, descricao, causa_provavel, recomendacao_correcao, servico_origem. Formato já existente no projeto, validado pelo módulo validators.py
- **Threshold_Severidade**: Regra que mapeia valores de métricas para níveis de severidade (INFO, WARNING, ERROR, CRITICAL). Exemplo: ActiveAlerts > 0 = ERROR, InputLossSeconds > 0 = WARNING
- **KB_LOGS**: Bucket S3 que armazena eventos normalizados para consulta RAG pelo Agente_Bedrock
- **Exportadora**: Lambda existente que consulta e filtra dados do KB_LOGS, gerando arquivos CSV/JSON para download
- **Periodo_Coleta**: Janela de tempo de 1 hora (última hora) com granularidade de 5 minutos para consulta de métricas
- **Descoberta_Dinamica**: Processo de listar todos os recursos ativos (canais, distribuições, configurações) via APIs AWS antes de consultar métricas, sem IDs hardcoded
- **MediaLive**: Serviço AWS Elemental MediaLive (namespace AWS/MediaLive, região sa-east-1). Dimensões: ChannelId, Pipeline
- **MediaPackage**: Serviço AWS Elemental MediaPackage V2 (namespace AWS/MediaPackage, região sa-east-1). Dimensões: ChannelGroup, Channel, OriginEndpoint, StatusCode
- **MediaTailor**: Serviço AWS Elemental MediaTailor (namespace AWS/MediaTailor, região us-east-1). Dimensões: ConfigurationName
- **CloudFront**: Serviço Amazon CloudFront (namespace AWS/CloudFront, região us-east-1, global). Dimensões: DistributionId, Region

## Requisitos

### Requisito 1: Descoberta Dinâmica de Recursos

**User Story:** Como operador de NOC, eu quero que o pipeline descubra automaticamente todos os canais e recursos de streaming ativos, para que novos canais sejam monitorados sem necessidade de configuração manual.

#### Critérios de Aceitação

1. WHEN o Pipeline_Metricas é executado, THE Pipeline_Metricas SHALL listar todos os canais MediaLive ativos na região sa-east-1 utilizando a API ListChannels
2. WHEN o Pipeline_Metricas é executado, THE Pipeline_Metricas SHALL listar todos os Channel Groups e Channels do MediaPackage V2 na região sa-east-1 utilizando as APIs ListChannelGroups e ListChannels
3. WHEN o Pipeline_Metricas é executado, THE Pipeline_Metricas SHALL listar todas as configurações de playback do MediaTailor na região us-east-1 utilizando a API ListPlaybackConfigurations
4. WHEN o Pipeline_Metricas é executado, THE Pipeline_Metricas SHALL listar todas as distribuições CloudFront utilizando a API ListDistributions na região us-east-1
5. THE Pipeline_Metricas SHALL utilizar clientes boto3 separados por região: sa-east-1 para MediaLive e MediaPackage, us-east-1 para MediaTailor e CloudFront
6. IF uma API de listagem retornar erro, THEN THE Pipeline_Metricas SHALL registrar o erro no log, continuar a coleta dos demais serviços e incluir o erro no resultado final

### Requisito 2: Coleta de Métricas MediaLive

**User Story:** Como operador de NOC, eu quero coletar métricas de saúde e performance de todos os canais MediaLive, para que eu possa detectar problemas como perda de sinal, frames perdidos e erros de output.

#### Critérios de Aceitação

1. WHEN o Pipeline_Metricas é executado, THE Pipeline_Metricas SHALL consultar as seguintes métricas do namespace AWS/MediaLive para cada canal descoberto: ActiveAlerts, InputLossSeconds, InputVideoFrameRate, DroppedFrames, FillMsec, NetworkIn, NetworkOut, Output4xxErrors, Output5xxErrors, PrimaryInputActive, ChannelInputErrorSeconds, RtpPacketsLost
2. THE Pipeline_Metricas SHALL consultar métricas MediaLive utilizando as dimensões ChannelId e Pipeline (valores 0 e 1)
3. THE Pipeline_Metricas SHALL consultar métricas da última hora com período de 300 segundos (5 minutos) e estatísticas Sum, Average e Maximum conforme apropriado para cada métrica
4. WHEN a métrica ActiveAlerts retornar valor maior que 0, THE Pipeline_Metricas SHALL classificar o evento como severidade ERROR com tipo_erro ALERTA_ATIVO
5. WHEN a métrica InputLossSeconds retornar valor maior que 0, THE Pipeline_Metricas SHALL classificar o evento como severidade WARNING com tipo_erro INPUT_LOSS
6. WHEN a métrica DroppedFrames retornar valor maior que 0, THE Pipeline_Metricas SHALL classificar o evento como severidade WARNING com tipo_erro FRAMES_PERDIDOS
7. WHEN as métricas Output4xxErrors ou Output5xxErrors retornarem valor maior que 0, THE Pipeline_Metricas SHALL classificar o evento como severidade ERROR com tipo_erro OUTPUT_ERROR
8. WHEN a métrica PrimaryInputActive retornar valor igual a 0, THE Pipeline_Metricas SHALL classificar o evento como severidade CRITICAL com tipo_erro FAILOVER_DETECTADO
9. WHEN todas as métricas de um canal estiverem dentro dos limites normais, THE Pipeline_Metricas SHALL gerar um evento de severidade INFO com tipo_erro METRICAS_NORMAIS


### Requisito 3: Coleta de Métricas MediaPackage V2

**User Story:** Como operador de NOC, eu quero coletar métricas de ingestão e distribuição do MediaPackage V2, para que eu possa monitorar a saúde do empacotamento e detectar erros de entrega.

#### Critérios de Aceitação

1. WHEN o Pipeline_Metricas é executado, THE Pipeline_Metricas SHALL consultar as seguintes métricas do namespace AWS/MediaPackage para cada canal descoberto: IngressBytes, IngressRequestCount, EgressBytes, EgressRequestCount, EgressResponseTime, IngressResponseTime
2. THE Pipeline_Metricas SHALL consultar métricas MediaPackage utilizando as dimensões ChannelGroup, Channel e OriginEndpoint
3. THE Pipeline_Metricas SHALL consultar EgressRequestCount filtrado por StatusCode (2xx, 4xx, 5xx) para identificar erros de distribuição
4. WHEN a métrica EgressRequestCount com StatusCode 5xx retornar valor maior que 0, THE Pipeline_Metricas SHALL classificar o evento como severidade ERROR com tipo_erro EGRESS_5XX
5. WHEN a métrica EgressRequestCount com StatusCode 4xx retornar valor maior que 0, THE Pipeline_Metricas SHALL classificar o evento como severidade WARNING com tipo_erro EGRESS_4XX
6. WHEN a métrica EgressResponseTime retornar valor médio acima de 1000 milissegundos, THE Pipeline_Metricas SHALL classificar o evento como severidade WARNING com tipo_erro LATENCIA_ALTA
7. WHEN a métrica IngressBytes retornar valor igual a 0 por mais de um período consecutivo, THE Pipeline_Metricas SHALL classificar o evento como severidade ERROR com tipo_erro INGESTAO_PARADA

### Requisito 4: Coleta de Métricas MediaTailor

**User Story:** Como operador de NOC, eu quero coletar métricas de inserção de anúncios do MediaTailor, para que eu possa monitorar a taxa de preenchimento de avails e detectar erros no ad server.

#### Critérios de Aceitação

1. WHEN o Pipeline_Metricas é executado, THE Pipeline_Metricas SHALL consultar as seguintes métricas do namespace AWS/MediaTailor para cada configuração descoberta: AdDecisionServer.Ads, AdDecisionServer.Duration, AdDecisionServer.Errors, AdDecisionServer.Timeouts, Avail.Duration, Avail.FilledDuration, Avail.FillRate
2. THE Pipeline_Metricas SHALL consultar métricas MediaTailor utilizando a dimensão ConfigurationName
3. WHEN a métrica AdDecisionServer.Errors retornar valor maior que 0, THE Pipeline_Metricas SHALL classificar o evento como severidade ERROR com tipo_erro AD_SERVER_ERROR
4. WHEN a métrica AdDecisionServer.Timeouts retornar valor maior que 0, THE Pipeline_Metricas SHALL classificar o evento como severidade WARNING com tipo_erro AD_SERVER_TIMEOUT
5. WHEN a métrica Avail.FillRate retornar valor abaixo de 80 por cento, THE Pipeline_Metricas SHALL classificar o evento como severidade WARNING com tipo_erro FILL_RATE_BAIXO
6. WHEN a métrica Avail.FillRate retornar valor abaixo de 50 por cento, THE Pipeline_Metricas SHALL classificar o evento como severidade ERROR com tipo_erro FILL_RATE_CRITICO

### Requisito 5: Coleta de Métricas CloudFront

**User Story:** Como operador de NOC, eu quero coletar métricas de distribuição CDN do CloudFront, para que eu possa monitorar taxas de erro e volume de tráfego das distribuições de streaming.

#### Critérios de Aceitação

1. WHEN o Pipeline_Metricas é executado, THE Pipeline_Metricas SHALL consultar as seguintes métricas do namespace AWS/CloudFront para cada distribuição descoberta: Requests, BytesDownloaded, BytesUploaded, 4xxErrorRate, 5xxErrorRate, TotalErrorRate
2. THE Pipeline_Metricas SHALL consultar métricas CloudFront utilizando as dimensões DistributionId e Region
3. WHEN a métrica 5xxErrorRate retornar valor acima de 5 por cento, THE Pipeline_Metricas SHALL classificar o evento como severidade ERROR com tipo_erro CDN_5XX_ALTO
4. WHEN a métrica 4xxErrorRate retornar valor acima de 10 por cento, THE Pipeline_Metricas SHALL classificar o evento como severidade WARNING com tipo_erro CDN_4XX_ALTO
5. WHEN a métrica TotalErrorRate retornar valor acima de 15 por cento, THE Pipeline_Metricas SHALL classificar o evento como severidade CRITICAL com tipo_erro CDN_ERROR_CRITICO
6. WHEN todas as métricas de uma distribuição estiverem dentro dos limites normais, THE Pipeline_Metricas SHALL gerar um evento de severidade INFO com tipo_erro METRICAS_NORMAIS


### Requisito 6: Normalização de Métricas em Evento_Estruturado

**User Story:** Como engenheiro de dados, eu quero que cada data point de métrica seja transformado em um Evento_Estruturado com campos padronizados, para que o chatbot e a Exportadora possam processar os dados de forma uniforme.

#### Critérios de Aceitação

1. THE Pipeline_Metricas SHALL gerar cada evento normalizado contendo os campos obrigatórios: timestamp (ISO 8601), canal (identificador do recurso), severidade (INFO, WARNING, ERROR ou CRITICAL), tipo_erro (classificação do evento), descricao (texto descritivo em português), causa_provavel (diagnóstico em português), recomendacao_correcao (ação sugerida em português)
2. THE Pipeline_Metricas SHALL incluir o campo servico_origem com o nome do serviço de origem (MediaLive, MediaPackage, MediaTailor ou CloudFront)
3. THE Pipeline_Metricas SHALL incluir campos adicionais de contexto: metrica_nome (nome da métrica CloudWatch), metrica_valor (valor numérico coletado), metrica_unidade (unidade da métrica), metrica_periodo (período em segundos), metrica_estatistica (Sum, Average ou Maximum)
4. THE Pipeline_Metricas SHALL gerar o campo descricao em português brasileiro com informações acionáveis. Exemplo: "Canal canal-01 apresentou 15 segundos de perda de input nos últimos 5 minutos"
5. THE Pipeline_Metricas SHALL gerar o campo causa_provavel em português brasileiro. Exemplo: "Perda de sinal de entrada detectada via métrica InputLossSeconds"
6. THE Pipeline_Metricas SHALL gerar o campo recomendacao_correcao em português brasileiro. Exemplo: "Verificar fonte de entrada e conectividade de rede do canal"
7. THE Pipeline_Metricas SHALL utilizar o timestamp do data point da métrica CloudWatch (não o timestamp de execução do pipeline) como timestamp do evento
8. FOR ALL eventos gerados, THE Pipeline_Metricas SHALL produzir eventos que passem na validação do validate_evento_estruturado existente no módulo validators.py

### Requisito 7: Classificação de Severidade por Thresholds

**User Story:** Como operador de NOC, eu quero que os eventos sejam classificados automaticamente por severidade com base em thresholds configuráveis, para que eu possa priorizar a resposta a incidentes.

#### Critérios de Aceitação

1. THE Pipeline_Metricas SHALL classificar eventos em quatro níveis de severidade: INFO (operação normal), WARNING (degradação detectada), ERROR (problema ativo) e CRITICAL (falha grave que requer ação imediata)
2. THE Pipeline_Metricas SHALL utilizar thresholds definidos em um dicionário Python no código-fonte, mapeando cada métrica para seus limites de severidade
3. WHEN múltiplas métricas de um mesmo recurso indicarem problemas simultâneos, THE Pipeline_Metricas SHALL gerar eventos separados para cada métrica anômala
4. THE Pipeline_Metricas SHALL utilizar a severidade mais alta aplicável quando uma métrica ultrapassar múltiplos thresholds (exemplo: FillRate abaixo de 50 por cento gera ERROR, não WARNING)
5. WHEN nenhuma métrica de um recurso ultrapassar thresholds, THE Pipeline_Metricas SHALL gerar um único evento INFO consolidado por recurso

### Requisito 8: Armazenamento no KB_LOGS

**User Story:** Como engenheiro de dados, eu quero que os eventos normalizados sejam armazenados no bucket KB_LOGS com organização por serviço e timestamp, para que o RAG e a Exportadora possam consultá-los eficientemente.

#### Critérios de Aceitação

1. THE Pipeline_Metricas SHALL armazenar cada evento normalizado como um arquivo JSON individual no bucket KB_LOGS
2. THE Pipeline_Metricas SHALL organizar os arquivos com a chave S3: {KB_LOGS_PREFIX}{servico}/{canal}_{timestamp_execucao}.json
3. THE Pipeline_Metricas SHALL definir o Content-Type como "application/json" para cada objeto armazenado
4. THE Pipeline_Metricas SHALL serializar o JSON com ensure_ascii=False para preservar caracteres em português
5. IF a gravação no S3 falhar, THEN THE Pipeline_Metricas SHALL registrar o erro no log e continuar processando os demais eventos
6. THE Pipeline_Metricas SHALL validar cada evento com validate_evento_estruturado antes de armazená-lo no S3
7. THE Pipeline_Metricas SHALL verificar contaminação cruzada com detect_cross_contamination antes de armazená-lo no S3

### Requisito 9: Compatibilidade com Exportadora

**User Story:** Como operador de NOC, eu quero que os eventos gerados pelo pipeline de métricas sejam consultáveis pela Exportadora existente, para que eu possa exportar relatórios filtrados por serviço, severidade, período e canal.

#### Critérios de Aceitação

1. THE Pipeline_Metricas SHALL gerar eventos com todos os campos esperados pela Exportadora: timestamp, canal, severidade, tipo_erro, descricao, causa_provavel, recomendacao_correcao, servico_origem
2. THE Pipeline_Metricas SHALL armazenar eventos no mesmo prefixo S3 utilizado pela Exportadora (KB_LOGS_PREFIX seguido do nome do serviço)
3. WHEN a Exportadora filtrar eventos por servico_origem, THE Pipeline_Metricas SHALL garantir que o campo servico_origem contenha valores válidos: MediaLive, MediaPackage, MediaTailor ou CloudFront
4. WHEN a Exportadora filtrar eventos por severidade, THE Pipeline_Metricas SHALL garantir que o campo severidade contenha valores válidos: INFO, WARNING, ERROR ou CRITICAL
5. WHEN a Exportadora filtrar eventos por periodo (inicio/fim), THE Pipeline_Metricas SHALL garantir que o campo timestamp esteja em formato ISO 8601 compatível com o parser da Exportadora

### Requisito 10: Infraestrutura e Permissões IAM

**User Story:** Como engenheiro de DevOps, eu quero que a Lambda Pipeline_Metricas tenha as permissões IAM necessárias para consultar métricas CloudWatch e listar recursos de streaming, para que o pipeline funcione sem erros de autorização.

#### Critérios de Aceitação

1. THE Pipeline_Metricas SHALL possuir permissão IAM para cloudwatch:GetMetricData e cloudwatch:ListMetrics em todas as regiões necessárias (sa-east-1 e us-east-1)
2. THE Pipeline_Metricas SHALL possuir permissão IAM para medialive:ListChannels e medialive:DescribeChannel na região sa-east-1
3. THE Pipeline_Metricas SHALL possuir permissão IAM para mediapackagev2:ListChannelGroups, mediapackagev2:ListChannels e mediapackagev2:ListOriginEndpoints na região sa-east-1
4. THE Pipeline_Metricas SHALL possuir permissão IAM para mediatailor:ListPlaybackConfigurations na região us-east-1
5. THE Pipeline_Metricas SHALL possuir permissão IAM para cloudfront:ListDistributions na região us-east-1
6. THE Pipeline_Metricas SHALL possuir permissão IAM para s3:PutObject no bucket KB_LOGS
7. THE MainStack do CDK SHALL atualizar a definição da Lambda Pipeline_Logs existente para incluir as novas permissões de CloudWatch Metrics e APIs de listagem de recursos, substituindo a permissão logs:* atual
8. THE MainStack do CDK SHALL adicionar variáveis de ambiente MEDIATAILOR_REGION=us-east-1 e CLOUDFRONT_REGION=us-east-1 à Lambda Pipeline_Metricas

### Requisito 11: Resiliência e Tratamento de Erros

**User Story:** Como engenheiro de DevOps, eu quero que o pipeline seja resiliente a falhas parciais, para que a indisponibilidade de um serviço não impeça a coleta de métricas dos demais.

#### Critérios de Aceitação

1. IF a coleta de métricas de um serviço falhar, THEN THE Pipeline_Metricas SHALL registrar o erro e continuar a coleta dos demais serviços
2. IF a coleta de métricas de um recurso individual falhar, THEN THE Pipeline_Metricas SHALL registrar o erro e continuar com o próximo recurso do mesmo serviço
3. IF a API GetMetricData retornar dados parciais, THEN THE Pipeline_Metricas SHALL processar os dados disponíveis e registrar um aviso sobre dados incompletos
4. THE Pipeline_Metricas SHALL retornar um resumo de execução contendo: total de eventos armazenados, total de erros, total de eventos rejeitados por validação e total de eventos rejeitados por contaminação cruzada
5. THE Pipeline_Metricas SHALL completar a execução dentro do timeout de 5 minutos configurado na Lambda
6. IF a API GetMetricData retornar throttling (TooManyRequestsException), THEN THE Pipeline_Metricas SHALL aplicar backoff exponencial com até 3 tentativas antes de registrar o erro

### Requisito 12: Serialização e Round-Trip de Evento_Estruturado

**User Story:** Como engenheiro de dados, eu quero garantir que os eventos gerados possam ser serializados para JSON e desserializados de volta sem perda de informação, para que a integridade dos dados seja preservada no ciclo completo de armazenamento e leitura.

#### Critérios de Aceitação

1. FOR ALL eventos gerados pelo Pipeline_Metricas, serializar para JSON e desserializar de volta SHALL produzir um dicionário equivalente ao original (propriedade round-trip)
2. THE Pipeline_Metricas SHALL serializar todos os campos numéricos como números JSON (não strings)
3. THE Pipeline_Metricas SHALL serializar timestamps como strings ISO 8601 com sufixo Z (UTC)
4. THE Pipeline_Metricas SHALL preservar caracteres Unicode em português na serialização (ensure_ascii=False)

### Requisito 13: Consulta de Métricas Sob Demanda via Chat

**User Story:** Como operador de NOC, eu quero consultar métricas CloudWatch em tempo real pelo chat com período e granularidade personalizados, para que eu possa investigar problemas específicos sem depender apenas dos dados coletados pelo pipeline automático.

#### Critérios de Aceitação

1. WHEN o usuário solicitar métricas de um canal específico (ex: "métricas do canal Warner na última hora"), THE Bedrock_Agent SHALL acionar um Action Group que consulta métricas CloudWatch em tempo real
2. THE Lambda_Configuradora SHALL expor um novo endpoint "/consultarMetricas" que aceita: servico, resource_id (nome parcial ou ID), periodo_minutos (padrão: 60), granularidade_segundos (padrão: 300) e metricas (lista opcional de métricas específicas)
3. WHEN o usuário especificar um período (ex: "últimos 15 minutos", "última hora", "últimas 24 horas"), THE Bedrock_Agent SHALL mapear para o parâmetro periodo_minutos correspondente
4. WHEN o usuário especificar granularidade (ex: "resolução de 1 minuto", "a cada 5 minutos"), THE Bedrock_Agent SHALL mapear para o parâmetro granularidade_segundos correspondente
5. THE endpoint "/consultarMetricas" SHALL retornar um resumo compacto das métricas com classificação de severidade, incluindo: valores atuais, valores médios do período, alertas ativos e anomalias detectadas
6. THE endpoint "/consultarMetricas" SHALL suportar busca fuzzy por nome de canal (reutilizando a lógica existente de resolução de nomes)
7. IF o recurso não for encontrado, THEN THE endpoint SHALL retornar erro 400 com mensagem descritiva
8. IF múltiplos recursos corresponderem ao nome parcial, THEN THE endpoint SHALL retornar a lista de candidatos para o usuário escolher (mesmo padrão do obterConfiguracao)
