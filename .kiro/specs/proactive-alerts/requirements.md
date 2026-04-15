# Documento de Requisitos — Alertas Proativos via SNS

## Introdução

Este documento descreve os requisitos para adicionar notificações proativas ao pipeline de métricas existente (Pipeline_Logs Lambda). Atualmente, o pipeline coleta métricas CloudWatch a cada 1 hora, classifica eventos por severidade (INFO, WARNING, ERROR, CRITICAL) e armazena Evento_Estruturado no bucket KB_LOGS para consulta RAG. Porém, o operador de NOC só descobre problemas quando consulta o chatbot manualmente.

A funcionalidade de alertas proativos faz com que, após a coleta e classificação de métricas, o pipeline verifique se existem eventos com severidade igual ou superior a um threshold configurável (padrão: ERROR). Quando encontrados, o pipeline publica uma notificação formatada em um tópico SNS. O tópico SNS pode ser assinado por email, Slack (via webhook ou Lambda intermediária), PagerDuty ou qualquer outro endpoint compatível.

Para evitar fadiga de alertas, o sistema implementa uma janela de supressão: se um alerta para a mesma combinação canal+métrica já foi enviado dentro de X minutos, o alerta é suprimido. A infraestrutura CDK cria o tópico SNS, concede permissão de publicação à Lambda e configura variáveis de ambiente.

## Glossário

- **Pipeline_Metricas**: Função AWS Lambda existente (lambdas/pipeline_logs/handler.py) que coleta métricas CloudWatch, classifica severidade e armazena Evento_Estruturado no S3. Executada a cada 1 hora via EventBridge
- **Evento_Estruturado**: Registro normalizado contendo: timestamp, canal, severidade, tipo_erro, descricao, causa_provavel, recomendacao_correcao, servico_origem, metrica_nome, metrica_valor, metrica_unidade, metrica_periodo, metrica_estatistica
- **Topico_SNS_Alertas**: Tópico Amazon SNS criado pelo CDK para receber notificações de alertas proativos do pipeline de métricas
- **Threshold_Alerta**: Nível mínimo de severidade que dispara uma notificação SNS. Valores válidos: WARNING, ERROR, CRITICAL. Padrão: ERROR
- **Janela_Supressao**: Período em minutos durante o qual alertas duplicados (mesma combinação canal+metrica_nome) são suprimidos para evitar fadiga de alertas. Padrão: 60 minutos
- **Chave_Supressao**: Identificador único de um alerta composto por canal e metrica_nome, usado para verificar duplicatas na janela de supressão
- **Notificacao_Alerta**: Mensagem formatada publicada no Topico_SNS_Alertas contendo informações acionáveis sobre o evento detectado
- **MainStack**: Stack CDK principal (stacks/main_stack.py) que define toda a infraestrutura do projeto, incluindo a Lambda Pipeline_Metricas
- **SNS_TOPIC_ARN**: Variável de ambiente da Lambda Pipeline_Metricas contendo o ARN do Topico_SNS_Alertas
- **ALERT_SEVERITY_THRESHOLD**: Variável de ambiente da Lambda Pipeline_Metricas contendo o nível mínimo de severidade para disparo de alertas (WARNING, ERROR ou CRITICAL)
- **ALERT_SUPPRESSION_MINUTES**: Variável de ambiente da Lambda Pipeline_Metricas contendo a duração da janela de supressão em minutos

## Requisitos

### Requisito 1: Filtragem de Eventos por Threshold de Severidade

**User Story:** Como operador de NOC, eu quero que o pipeline filtre automaticamente eventos com severidade igual ou superior ao threshold configurado, para que apenas problemas relevantes gerem notificações.

#### Critérios de Aceitação

1. WHEN o Pipeline_Metricas finaliza a classificação de severidade de todos os eventos coletados, THE Pipeline_Metricas SHALL identificar todos os eventos com severidade igual ou superior ao valor definido na variável de ambiente ALERT_SEVERITY_THRESHOLD
2. THE Pipeline_Metricas SHALL utilizar a seguinte hierarquia de severidade para comparação: INFO menor que WARNING menor que ERROR menor que CRITICAL
3. WHEN a variável de ambiente ALERT_SEVERITY_THRESHOLD não estiver definida, THE Pipeline_Metricas SHALL utilizar ERROR como valor padrão
4. WHEN a variável de ambiente ALERT_SEVERITY_THRESHOLD contiver um valor inválido (diferente de WARNING, ERROR ou CRITICAL), THE Pipeline_Metricas SHALL registrar um aviso no log e utilizar ERROR como valor padrão
5. WHEN nenhum evento atingir o threshold de severidade configurado, THE Pipeline_Metricas SHALL registrar no log que nenhum alerta foi necessário e continuar a execução normalmente

### Requisito 2: Publicação de Notificações no SNS

**User Story:** Como operador de NOC, eu quero receber notificações formatadas via SNS quando eventos críticos forem detectados, para que eu possa agir rapidamente sem precisar consultar o chatbot.

#### Critérios de Aceitação

1. WHEN um evento atinge o threshold de severidade configurado, THE Pipeline_Metricas SHALL publicar uma mensagem no Topico_SNS_Alertas identificado pela variável de ambiente SNS_TOPIC_ARN
2. THE Pipeline_Metricas SHALL formatar a mensagem SNS contendo os seguintes campos do Evento_Estruturado: canal (nome do canal ou recurso), servico_origem (MediaLive, MediaPackage, MediaTailor ou CloudFront), severidade (ERROR ou CRITICAL), metrica_nome (nome da métrica CloudWatch), metrica_valor (valor numérico coletado), descricao (texto descritivo em português), recomendacao_correcao (ação sugerida em português)
3. THE Pipeline_Metricas SHALL definir o atributo Subject da mensagem SNS com o formato: "[SEVERIDADE] Alerta Streaming - canal - servico_origem"
4. THE Pipeline_Metricas SHALL formatar o corpo da mensagem SNS como texto legível com quebras de linha, incluindo separadores visuais entre seções para facilitar leitura em email e Slack
5. THE Pipeline_Metricas SHALL incluir o timestamp do evento (ISO 8601) na mensagem SNS
6. WHEN a variável de ambiente SNS_TOPIC_ARN não estiver definida ou estiver vazia, THE Pipeline_Metricas SHALL ignorar a etapa de publicação SNS e registrar um aviso no log
7. WHEN múltiplos eventos do mesmo recurso atingirem o threshold na mesma execução, THE Pipeline_Metricas SHALL agrupar os eventos em uma única mensagem SNS por recurso para evitar excesso de notificações

### Requisito 3: Janela de Supressão de Alertas

**User Story:** Como operador de NOC, eu quero que alertas repetidos para o mesmo canal e métrica sejam suprimidos dentro de uma janela de tempo configurável, para que eu não receba notificações duplicadas sobre o mesmo problema.

#### Critérios de Aceitação

1. THE Pipeline_Metricas SHALL gerar uma Chave_Supressao para cada alerta combinando os campos canal e metrica_nome no formato "canal::metrica_nome"
2. WHEN um alerta é candidato a publicação, THE Pipeline_Metricas SHALL verificar se já existe um registro de alerta com a mesma Chave_Supressao enviado dentro da Janela_Supressao
3. WHEN um alerta com a mesma Chave_Supressao foi enviado dentro da Janela_Supressao, THE Pipeline_Metricas SHALL suprimir o alerta e registrar no log que o alerta foi suprimido por duplicidade
4. THE Pipeline_Metricas SHALL armazenar o registro de alertas enviados em um objeto JSON no bucket KB_LOGS com a chave "{KB_LOGS_PREFIX}alertas/suppression_state.json"
5. THE Pipeline_Metricas SHALL utilizar o valor da variável de ambiente ALERT_SUPPRESSION_MINUTES como duração da Janela_Supressao em minutos
6. WHEN a variável de ambiente ALERT_SUPPRESSION_MINUTES não estiver definida, THE Pipeline_Metricas SHALL utilizar 60 minutos como valor padrão
7. THE Pipeline_Metricas SHALL remover registros expirados (mais antigos que a Janela_Supressao) do estado de supressão antes de verificar duplicatas
8. IF a leitura do estado de supressão do S3 falhar, THEN THE Pipeline_Metricas SHALL tratar como estado vazio (sem supressões ativas) e registrar o erro no log

### Requisito 4: Infraestrutura CDK para SNS

**User Story:** Como engenheiro de DevOps, eu quero que o CDK crie automaticamente o tópico SNS e configure as permissões necessárias, para que a infraestrutura de alertas seja provisionada de forma reprodutível.

#### Critérios de Aceitação

1. THE MainStack SHALL criar um tópico SNS com o nome "StreamingAlertsNotifications" para receber notificações de alertas proativos
2. THE MainStack SHALL conceder permissão de publicação (sns:Publish) no Topico_SNS_Alertas à role IAM da Lambda Pipeline_Metricas
3. THE MainStack SHALL adicionar a variável de ambiente SNS_TOPIC_ARN à Lambda Pipeline_Metricas com o ARN do Topico_SNS_Alertas criado
4. THE MainStack SHALL adicionar a variável de ambiente ALERT_SEVERITY_THRESHOLD à Lambda Pipeline_Metricas com valor padrão "ERROR"
5. THE MainStack SHALL adicionar a variável de ambiente ALERT_SUPPRESSION_MINUTES à Lambda Pipeline_Metricas com valor padrão "60"
6. THE MainStack SHALL exportar o ARN do Topico_SNS_Alertas como output do CloudFormation com o nome "AlertsTopicArn"

### Requisito 5: Resiliência do Sistema de Alertas

**User Story:** Como engenheiro de DevOps, eu quero que falhas no sistema de alertas não afetem o pipeline principal de coleta de métricas, para que a funcionalidade existente continue operando mesmo se o SNS estiver indisponível.

#### Critérios de Aceitação

1. IF a publicação de uma mensagem no Topico_SNS_Alertas falhar, THEN THE Pipeline_Metricas SHALL registrar o erro no log e continuar a execução do pipeline sem interromper o processamento dos demais eventos
2. IF a leitura ou gravação do estado de supressão no S3 falhar, THEN THE Pipeline_Metricas SHALL registrar o erro no log e publicar o alerta normalmente (fail-open)
3. THE Pipeline_Metricas SHALL incluir contadores de alertas no resumo de execução: total_alertas_enviados, total_alertas_suprimidos e total_alertas_falha
4. THE Pipeline_Metricas SHALL executar a etapa de alertas proativos após a etapa de armazenamento de eventos no S3, garantindo que a coleta e armazenamento de métricas não sejam afetados por falhas no sistema de alertas
5. WHEN o Topico_SNS_Alertas retornar throttling, THE Pipeline_Metricas SHALL aplicar backoff exponencial com até 3 tentativas antes de registrar a falha

### Requisito 6: Formatação da Notificação de Alerta

**User Story:** Como operador de NOC, eu quero que as notificações de alerta sejam claras e acionáveis, para que eu possa entender o problema e tomar ação imediata sem precisar consultar outros sistemas.

#### Critérios de Aceitação

1. THE Pipeline_Metricas SHALL formatar a Notificacao_Alerta com as seguintes seções separadas por linhas: cabeçalho com severidade e serviço, identificação do canal ou recurso, detalhes da métrica (nome, valor, unidade), descrição do problema em português, causa provável em português, recomendação de correção em português, e timestamp do evento
2. WHEN múltiplos eventos de um mesmo recurso são agrupados em uma única notificação, THE Pipeline_Metricas SHALL listar cada evento como um item separado dentro da mesma mensagem, precedido pelo tipo de erro
3. THE Pipeline_Metricas SHALL incluir no cabeçalho da mensagem um emoji indicador de severidade: "🔴" para CRITICAL, "🟠" para ERROR, "🟡" para WARNING
4. THE Pipeline_Metricas SHALL limitar o tamanho da mensagem SNS a 256 KB (limite do SNS) truncando eventos excedentes e incluindo uma nota informando quantos eventos adicionais foram omitidos
5. FOR ALL notificações geradas, serializar para string e desserializar de volta SHALL preservar todos os campos sem perda de informação (propriedade round-trip)

### Requisito 7: Serialização e Round-Trip da Notificação

**User Story:** Como engenheiro de dados, eu quero garantir que as notificações geradas possam ser serializadas e desserializadas sem perda de informação, para que integrações downstream (Slack webhooks, Lambda de processamento) recebam dados íntegros.

#### Critérios de Aceitação

1. FOR ALL notificações geradas pelo Pipeline_Metricas, serializar o payload JSON para string e desserializar de volta SHALL produzir um dicionário equivalente ao original (propriedade round-trip)
2. THE Pipeline_Metricas SHALL serializar o payload JSON da notificação com ensure_ascii=False para preservar caracteres em português
3. THE Pipeline_Metricas SHALL serializar timestamps como strings ISO 8601 com sufixo Z (UTC)
4. THE Pipeline_Metricas SHALL serializar todos os campos numéricos (metrica_valor) como números JSON, não como strings
