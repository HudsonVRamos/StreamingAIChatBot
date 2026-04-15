# Documento de Requisitos — Agendamento de Operações (scheduled-operations)

## Introdução

Esta funcionalidade permite que operadores de NOC agendem operações de streaming para execução futura diretamente pelo chatbot, usando linguagem natural. Em vez de precisar estar disponível no horário exato de uma manutenção, o operador pode dizer "Pare o canal TESTE_KIRO amanhã às 02:00" e o sistema garante a execução automática no horário correto.

A arquitetura utiliza o **EventBridge Scheduler** (não EventBridge Rules) para disparar cada agendamento individualmente no horário exato. Uma nova Lambda_Scheduler recebe o evento, executa a operação via Lambda_Configuradora e atualiza o status na tabela DynamoDB ScheduledOperations. O resultado é registrado no S3_Audit e uma notificação SNS é enviada ao operador.

O Agente_Bedrock interpreta horários em BRT (UTC-3), converte para UTC antes de criar o schedule e roteia mensagens com palavras-chave de agendamento para o novo endpoint `/agendarOperacao` da Lambda_Configuradora.

## Glossário

- **ScheduledOperations**: Tabela DynamoDB dedicada ao armazenamento de agendamentos. PK=`schedule_id` (UUID v4), SK=`status`. Contém GSI por canal e por data de execução.
- **Schedule_ID**: Identificador único (UUID v4) gerado para cada agendamento. Usado como nome do EventBridge Scheduler schedule e como chave primária no DynamoDB.
- **Status_Agendamento**: Estado do ciclo de vida de um agendamento. Valores válidos: `PENDING` (aguardando execução), `EXECUTED` (executado com sucesso), `CANCELLED` (cancelado pelo operador), `FAILED` (executado com falha).
- **Lambda_Scheduler**: Nova função AWS Lambda Python 3.12 invocada pelo EventBridge Scheduler no horário agendado. Responsável por executar a operação via Lambda_Configuradora, atualizar o status no DynamoDB e publicar notificação SNS.
- **Lambda_Configuradora**: Função Lambda existente que executa operações nos serviços AWS de streaming (MediaLive, MediaPackage V2, MediaTailor, CloudFront). Receberá o novo endpoint `/agendarOperacao`.
- **EventBridge_Scheduler**: Serviço AWS EventBridge Scheduler (distinto de EventBridge Rules) utilizado para criar schedules one-time com horário exato de execução. Cada agendamento gera um schedule independente nomeado com o Schedule_ID.
- **Agente_Bedrock**: Agente Amazon Bedrock que interpreta linguagem natural do operador, extrai parâmetros de agendamento (canal, operação, horário, fuso horário) e roteia para o Action_Group_Config.
- **Action_Group_Config**: Grupo de ações do Agente_Bedrock que invoca a Lambda_Configuradora via OpenAPI. Receberá os novos paths `/agendarOperacao` e as novas ações `listarAgendamentos` e `cancelarAgendamento` no path `/gerenciarRecurso`.
- **Topico_SNS_Alertas**: Tópico Amazon SNS existente (criado pelo spec proactive-alerts, nome "StreamingAlertsNotifications") reutilizado para publicar notificações de execução de agendamentos.
- **S3_Audit**: Bucket S3 existente que armazena logs de auditoria com prefixo `audit/YYYY/MM/DD/`. Os registros de agendamento incluirão os campos adicionais `agendado_por` e `schedule_id`.
- **BRT**: Horário de Brasília (UTC-3). Fuso horário padrão para interpretação de horários fornecidos pelo operador via chat.
- **Operacao_Agendavel**: Operação de streaming que pode ser agendada. Valores suportados: `start`, `stop`, `deletar`. Futuramente: `criar`.
- **Operacao_Destrutiva**: Operação irreversível que requer confirmação explícita antes do agendamento. Atualmente: `deletar`.
- **GSI_Canal**: Global Secondary Index da tabela ScheduledOperations com PK=`canal_id` e SK=`scheduled_time`, permitindo listar agendamentos por canal.
- **GSI_Data**: Global Secondary Index da tabela ScheduledOperations com PK=`data_execucao` (formato `YYYY-MM-DD`) e SK=`scheduled_time`, permitindo listar agendamentos por data.
- **MainStack**: Stack CDK principal (stacks/main_stack.py) que define toda a infraestrutura do projeto.
- **Frontend_Chat**: Interface web de chat (chat.html) com sidebar de sugestões categorizadas.
- **DynamoDB_StreamingConfigs**: Tabela DynamoDB existente com configurações dos canais, consultada para validar se o canal informado existe antes de criar o agendamento.

## Requisitos

### Requisito 1: Criação de Agendamento via Chat

**User Story:** Como operador de NOC, eu quero agendar operações de streaming para execução futura usando linguagem natural no chat, para que eu não precise estar disponível no horário exato da manutenção.

#### Critérios de Aceitação

1. WHEN o usuário envia uma mensagem contendo palavras-chave de agendamento ("agendar", "programar", "às HH:MM", "amanhã", "na sexta", "no dia DD/MM"), THE Agente_Bedrock SHALL classificar a intenção como criação de agendamento e rotear para o endpoint `/agendarOperacao` da Lambda_Configuradora.
2. WHEN o Agente_Bedrock interpreta um horário fornecido pelo usuário sem especificação de fuso horário, THE Agente_Bedrock SHALL assumir BRT (UTC-3) e converter para UTC antes de enviar o parâmetro `scheduled_time` ao endpoint `/agendarOperacao`.
3. WHEN o Agente_Bedrock interpreta referências relativas de data ("amanhã", "na sexta", "segunda-feira", "no dia DD/MM"), THE Agente_Bedrock SHALL resolver a data absoluta com base na data e hora atual em BRT antes de enviar ao endpoint.
4. WHEN o usuário solicita o agendamento de uma Operacao_Destrutiva (`deletar`), THE Agente_Bedrock SHALL exibir um aviso de que a operação é irreversível e solicitar confirmação explícita antes de invocar o endpoint `/agendarOperacao`.
5. WHEN o endpoint `/agendarOperacao` retorna sucesso, THE Agente_Bedrock SHALL confirmar ao usuário o agendamento criado informando: Schedule_ID, canal, operação, data e hora em BRT e status `PENDING`.
6. IF o endpoint `/agendarOperacao` retornar erro de validação (ex: horário no passado), THEN THE Agente_Bedrock SHALL apresentar a mensagem de erro ao usuário e sugerir um novo horário.

### Requisito 2: Endpoint /agendarOperacao na Lambda_Configuradora

**User Story:** Como desenvolvedor, eu quero um endpoint dedicado na Lambda_Configuradora para criação de agendamentos, para que a lógica de validação, persistência no DynamoDB e criação do EventBridge Scheduler schedule fique encapsulada.

#### Critérios de Aceitação

1. THE Lambda_Configuradora SHALL expor o endpoint `/agendarOperacao` que aceita os parâmetros: `canal_id` (string, obrigatório), `servico` (string, obrigatório), `tipo_recurso` (string, obrigatório), `operacao` (enum: `start`, `stop`, `deletar`, obrigatório), `scheduled_time` (string ISO 8601 UTC, obrigatório), `usuario_id` (string, obrigatório) e `parametros_adicionais` (objeto JSON, opcional).
2. WHEN o endpoint `/agendarOperacao` é invocado, THE Lambda_Configuradora SHALL validar que o `scheduled_time` é estritamente posterior ao momento atual (UTC). IF o `scheduled_time` for igual ou anterior ao momento atual, THEN THE Lambda_Configuradora SHALL retornar erro 400 com a mensagem "Não é possível agendar operações no passado. Horário informado: {scheduled_time_brt} BRT".
3. WHEN todos os parâmetros são válidos, THE Lambda_Configuradora SHALL gerar um Schedule_ID único (UUID v4) e persistir o agendamento na tabela ScheduledOperations com status `PENDING`.
4. WHEN o registro é persistido no DynamoDB, THE Lambda_Configuradora SHALL criar um EventBridge Scheduler schedule one-time com nome igual ao Schedule_ID, target apontando para a Lambda_Scheduler e horário igual ao `scheduled_time`.
5. IF a criação do EventBridge Scheduler schedule falhar após a persistência no DynamoDB, THEN THE Lambda_Configuradora SHALL atualizar o status do agendamento para `FAILED` no DynamoDB, registrar o erro no log e retornar erro 500 com detalhes da falha.
6. WHEN o agendamento é criado com sucesso, THE Lambda_Configuradora SHALL retornar HTTP 201 com o corpo contendo: `schedule_id`, `status` (`PENDING`), `scheduled_time_utc` e `scheduled_time_brt` (convertido para BRT para exibição).
7. IF qualquer parâmetro obrigatório estiver ausente, THEN THE Lambda_Configuradora SHALL retornar erro 400 com a lista de parâmetros faltantes.

### Requisito 3: Tabela DynamoDB ScheduledOperations

**User Story:** Como engenheiro de dados, eu quero uma tabela DynamoDB dedicada para armazenar agendamentos com índices eficientes por canal e por data, para que as consultas de listagem sejam rápidas sem varredura completa da tabela.

#### Critérios de Aceitação

1. THE MainStack SHALL criar a tabela DynamoDB `ScheduledOperations` com PK=`schedule_id` (String) e SK=`status` (String).
2. THE MainStack SHALL criar o GSI_Canal na tabela ScheduledOperations com PK=`canal_id` (String) e SK=`scheduled_time` (String ISO 8601), projetando todos os atributos.
3. THE MainStack SHALL criar o GSI_Data na tabela ScheduledOperations com PK=`data_execucao` (String, formato `YYYY-MM-DD`) e SK=`scheduled_time` (String ISO 8601), projetando todos os atributos.
4. THE tabela ScheduledOperations SHALL armazenar os seguintes atributos por item: `schedule_id`, `status`, `canal_id`, `servico`, `tipo_recurso`, `operacao`, `scheduled_time` (ISO 8601 UTC), `data_execucao` (YYYY-MM-DD, derivado de `scheduled_time`), `usuario_id`, `criado_em` (ISO 8601 UTC), `executado_em` (ISO 8601 UTC, preenchido após execução), `resultado` (objeto com detalhes da execução), `parametros_adicionais` (objeto JSON opcional) e `eventbridge_schedule_name` (nome do schedule criado no EventBridge Scheduler).
5. THE MainStack SHALL configurar TTL na tabela ScheduledOperations usando o atributo `ttl_expiracao`, com valor padrão de 90 dias após a data de execução agendada.
6. THE MainStack SHALL configurar billing mode PAY_PER_REQUEST na tabela ScheduledOperations.

### Requisito 4: Lambda_Scheduler — Execução do Agendamento

**User Story:** Como operador de NOC, eu quero que a operação agendada seja executada automaticamente no horário correto sem intervenção manual, para que a manutenção ocorra conforme planejado mesmo fora do horário de trabalho.

#### Critérios de Aceitação

1. THE Lambda_Scheduler SHALL ser implementada em Python 3.12 com timeout de 5 minutos (300 segundos) e memória mínima de 256 MB.
2. WHEN o EventBridge Scheduler aciona a Lambda_Scheduler no horário agendado, THE Lambda_Scheduler SHALL ler o `schedule_id` do evento recebido e consultar o agendamento correspondente na tabela ScheduledOperations.
3. WHEN o agendamento é encontrado com status `PENDING`, THE Lambda_Scheduler SHALL invocar a Lambda_Configuradora com os parâmetros da operação (`canal_id`, `servico`, `tipo_recurso`, `operacao`, `parametros_adicionais`).
4. WHEN a Lambda_Configuradora retorna sucesso, THE Lambda_Scheduler SHALL atualizar o status do agendamento para `EXECUTED` no DynamoDB, preenchendo os campos `executado_em` e `resultado`.
5. IF a Lambda_Configuradora retornar erro, THEN THE Lambda_Scheduler SHALL atualizar o status do agendamento para `FAILED` no DynamoDB, preenchendo `executado_em` e `resultado` com os detalhes do erro.
6. WHEN o agendamento é encontrado com status diferente de `PENDING` (ex: `CANCELLED`), THE Lambda_Scheduler SHALL registrar no log que o agendamento foi ignorado por não estar pendente e encerrar sem executar a operação.
7. IF o agendamento não for encontrado no DynamoDB, THEN THE Lambda_Scheduler SHALL registrar o erro no log e encerrar sem executar nenhuma operação.
8. WHEN a execução é concluída (sucesso ou falha), THE Lambda_Scheduler SHALL publicar uma notificação no Topico_SNS_Alertas com o resultado da operação.
9. WHEN a execução é concluída, THE Lambda_Scheduler SHALL registrar a operação no S3_Audit com os campos padrão de auditoria acrescidos de `agendado_por` (valor do campo `usuario_id` do agendamento) e `schedule_id`.

### Requisito 5: Notificação SNS ao Executar Agendamento

**User Story:** Como operador de NOC, eu quero receber uma notificação automática quando uma operação agendada for executada, para que eu saiba imediatamente se a manutenção ocorreu com sucesso ou falhou.

#### Critérios de Aceitação

1. WHEN a Lambda_Scheduler conclui a execução de um agendamento (sucesso ou falha), THE Lambda_Scheduler SHALL publicar uma mensagem no Topico_SNS_Alertas identificado pela variável de ambiente SNS_TOPIC_ARN.
2. THE Lambda_Scheduler SHALL formatar o Subject da mensagem SNS como: `[AGENDAMENTO] Canal {canal_id} - {operacao} executada com {sucesso|falha}`.
3. THE Lambda_Scheduler SHALL formatar o corpo da mensagem SNS contendo: Schedule_ID, canal, serviço, operação, horário agendado (BRT), horário de execução (BRT), resultado (sucesso ou falha), detalhes do erro (quando falha) e usuário que criou o agendamento.
4. IF a publicação no Topico_SNS_Alertas falhar, THEN THE Lambda_Scheduler SHALL registrar o erro no log e continuar a execução sem interromper o fluxo principal (a falha de notificação não deve reverter a atualização de status no DynamoDB).
5. WHEN a variável de ambiente SNS_TOPIC_ARN não estiver definida, THE Lambda_Scheduler SHALL ignorar a etapa de publicação SNS e registrar um aviso no log.

### Requisito 6: Listagem de Agendamentos via Chat

**User Story:** Como operador de NOC, eu quero listar os agendamentos pendentes pelo chatbot com filtros por canal, serviço ou status, para que eu tenha visibilidade das operações programadas sem precisar acessar o DynamoDB diretamente.

#### Critérios de Aceitação

1. WHEN o usuário solicita a listagem de agendamentos ("quais agendamentos existem", "listar agendamentos", "o que está agendado"), THE Agente_Bedrock SHALL rotear para `/gerenciarRecurso` com `acao=listarAgendamentos`.
2. THE Lambda_Configuradora SHALL aceitar os seguintes filtros opcionais para `acao=listarAgendamentos`: `canal_id` (string), `servico` (string), `status` (enum: `PENDING`, `EXECUTED`, `CANCELLED`, `FAILED`) e `data_execucao` (string, formato `YYYY-MM-DD`).
3. WHEN nenhum filtro é fornecido, THE Lambda_Configuradora SHALL retornar todos os agendamentos com status `PENDING` ordenados por `scheduled_time` crescente.
4. WHEN o filtro `canal_id` é fornecido, THE Lambda_Configuradora SHALL utilizar o GSI_Canal para consultar agendamentos do canal especificado, sem realizar varredura completa da tabela.
5. WHEN o filtro `data_execucao` é fornecido, THE Lambda_Configuradora SHALL utilizar o GSI_Data para consultar agendamentos da data especificada.
6. THE Lambda_Configuradora SHALL retornar a lista de agendamentos com os campos: `schedule_id`, `canal_id`, `servico`, `operacao`, `scheduled_time_brt` (convertido para BRT), `status`, `usuario_id` e `criado_em_brt`.
7. THE Lambda_Configuradora SHALL limitar a resposta a no máximo 50 agendamentos por consulta, incluindo o campo `total_encontrado` quando houver mais resultados.

### Requisito 7: Cancelamento de Agendamento via Chat

**User Story:** Como operador de NOC, eu quero cancelar um agendamento pendente pelo chatbot informando o Schedule_ID ou o nome do canal, para que eu possa desistir de uma operação programada antes que ela seja executada.

#### Critérios de Aceitação

1. WHEN o usuário solicita o cancelamento de um agendamento ("cancelar agendamento", "desagendar", "remover agendamento"), THE Agente_Bedrock SHALL rotear para `/gerenciarRecurso` com `acao=cancelarAgendamento`.
2. THE Lambda_Configuradora SHALL aceitar o parâmetro `schedule_id` (obrigatório) para `acao=cancelarAgendamento`.
3. WHEN o `schedule_id` é recebido, THE Lambda_Configuradora SHALL verificar que o agendamento existe e está com status `PENDING`. IF o agendamento não existir, THEN THE Lambda_Configuradora SHALL retornar erro 404 com a mensagem "Agendamento {schedule_id} não encontrado".
4. IF o agendamento existir com status diferente de `PENDING`, THEN THE Lambda_Configuradora SHALL retornar erro 409 com a mensagem "Agendamento {schedule_id} não pode ser cancelado pois está com status {status}".
5. WHEN o agendamento está com status `PENDING`, THE Lambda_Configuradora SHALL deletar o EventBridge Scheduler schedule correspondente (usando o campo `eventbridge_schedule_name` armazenado no DynamoDB).
6. WHEN o EventBridge Scheduler schedule é deletado com sucesso, THE Lambda_Configuradora SHALL atualizar o status do agendamento para `CANCELLED` no DynamoDB.
7. IF a deleção do EventBridge Scheduler schedule falhar, THEN THE Lambda_Configuradora SHALL registrar o erro no log, NÃO atualizar o status no DynamoDB e retornar erro 500 com detalhes da falha.
8. WHEN o cancelamento é concluído com sucesso, THE Lambda_Configuradora SHALL retornar HTTP 200 com confirmação contendo `schedule_id`, `status` (`CANCELLED`) e `cancelado_em` (ISO 8601 UTC).

### Requisito 8: Registro de Auditoria com Campos de Agendamento

**User Story:** Como auditor de operações, eu quero que as operações executadas via agendamento sejam registradas no S3_Audit com campos adicionais identificando o agendamento e o usuário que o criou, para que seja possível rastrear a origem de cada operação automatizada.

#### Critérios de Aceitação

1. WHEN a Lambda_Scheduler executa uma operação agendada, THE Lambda_Scheduler SHALL registrar a operação no S3_Audit com a estrutura padrão de auditoria acrescida dos campos: `agendado_por` (valor do `usuario_id` do agendamento), `schedule_id` (UUID do agendamento) e `tipo_execucao` com valor `"agendado"`.
2. THE Lambda_Scheduler SHALL usar a chave S3 no formato `audit/YYYY/MM/DD/{YYYYMMDDTHHMMSSz}-{schedule_id}.json` para o registro de auditoria, garantindo unicidade pelo Schedule_ID.
3. WHEN a operação agendada falha, THE Lambda_Scheduler SHALL registrar no S3_Audit o campo `resultado` como `"falha"` e o campo `erro` com o código e mensagem de erro retornados pela Lambda_Configuradora.
4. WHEN a operação agendada é bem-sucedida, THE Lambda_Scheduler SHALL registrar no S3_Audit o campo `resultado` como `"sucesso"` e o campo `resposta_configuradora` com o corpo da resposta da Lambda_Configuradora.
5. IF o registro no S3_Audit falhar, THEN THE Lambda_Scheduler SHALL registrar o erro no CloudWatch Logs e continuar a execução sem interromper a atualização de status no DynamoDB (a falha de auditoria não deve reverter o resultado da operação).

### Requisito 9: Validações de Negócio no Agendamento

**User Story:** Como operador de NOC, eu quero que o sistema valide as regras de negócio antes de confirmar um agendamento, para que erros óbvios sejam detectados antes da execução e não durante a madrugada.

#### Critérios de Aceitação

1. WHEN o endpoint `/agendarOperacao` recebe uma requisição, THE Lambda_Configuradora SHALL validar que o `scheduled_time` é estritamente posterior ao momento atual em UTC. IF não for, THEN THE Lambda_Configuradora SHALL retornar erro 400 com a mensagem "Não é possível agendar operações no passado. Horário informado: {scheduled_time_brt} BRT".
2. WHEN o endpoint `/agendarOperacao` recebe uma requisição com `operacao=deletar`, THE Lambda_Configuradora SHALL verificar que o campo `confirmacao_destrutiva` está presente e com valor `true` no corpo da requisição. IF ausente ou `false`, THEN THE Lambda_Configuradora SHALL retornar erro 400 com a mensagem "Operação 'deletar' requer confirmação explícita. Inclua confirmacao_destrutiva=true na requisição".
3. WHEN o endpoint `/agendarOperacao` recebe uma requisição, THE Lambda_Configuradora SHALL validar que o valor de `operacao` pertence ao conjunto de Operacoes_Agendaveis (`start`, `stop`, `deletar`). IF não pertencer, THEN THE Lambda_Configuradora SHALL retornar erro 400 com a mensagem "Operação '{operacao}' não é suportada para agendamento. Operações válidas: start, stop, deletar".
4. WHEN o endpoint `/agendarOperacao` recebe uma requisição, THE Lambda_Configuradora SHALL validar que o `scheduled_time` está no futuro por pelo menos 1 minuto a partir do momento atual, para evitar agendamentos que expirariam antes de serem processados pelo EventBridge Scheduler.
5. WHEN o Agente_Bedrock solicita confirmação para Operacao_Destrutiva, THE Agente_Bedrock SHALL exibir o nome do canal, o tipo de recurso e o horário agendado em BRT antes de solicitar confirmação, e incluir `confirmacao_destrutiva=true` na requisição somente após confirmação explícita do usuário.

### Requisito 10: Integração com OpenAPI e Action_Group_Config

**User Story:** Como desenvolvedor, eu quero que os novos endpoints de agendamento estejam definidos no schema OpenAPI do Action_Group_Config, para que o Agente_Bedrock possa invocar as operações de agendamento com os parâmetros corretos.

#### Critérios de Aceitação

1. THE schema OpenAPI do Action_Group_Config SHALL adicionar o path `/agendarOperacao` com método POST, documentando os parâmetros: `canal_id`, `servico`, `tipo_recurso`, `operacao` (enum: `start`, `stop`, `deletar`), `scheduled_time` (string ISO 8601 UTC), `usuario_id`, `parametros_adicionais` (opcional) e `confirmacao_destrutiva` (boolean, opcional).
2. THE schema OpenAPI do Action_Group_Config SHALL adicionar `listarAgendamentos` ao enum do parâmetro `acao` do path `/gerenciarRecurso`, documentando os filtros opcionais: `canal_id`, `servico`, `status` e `data_execucao`.
3. THE schema OpenAPI do Action_Group_Config SHALL adicionar `cancelarAgendamento` ao enum do parâmetro `acao` do path `/gerenciarRecurso`, documentando o parâmetro obrigatório `schedule_id`.
4. WHILE o schema OpenAPI do Action_Group_Config possui paths existentes, THE schema SHALL manter o total de paths dentro do limite de 9 após a adição do path `/agendarOperacao` (que adiciona 1 novo path, totalizando no máximo 9).
5. THE schema OpenAPI SHALL documentar o formato de resposta do path `/agendarOperacao` incluindo os campos `schedule_id`, `status`, `scheduled_time_utc` e `scheduled_time_brt`.

### Requisito 11: Roteamento do Agente_Bedrock para Agendamentos

**User Story:** Como operador de NOC, eu quero que o chatbot reconheça automaticamente intenções de agendamento em linguagem natural, para que eu possa agendar operações sem precisar conhecer a API diretamente.

#### Critérios de Aceitação

1. THE Agente_Bedrock SHALL incluir uma nova rota no prompt dedicada a criação de agendamentos, com palavras-chave: "agendar", "programar", "às HH:MM", "amanhã", "na sexta", "no dia DD/MM", "para segunda", "para amanhã", "às {hora}".
2. WHEN o Agente_Bedrock identifica uma intenção de agendamento, THE Agente_Bedrock SHALL extrair os seguintes parâmetros da mensagem do usuário: canal (nome ou ID parcial), operação (start/stop/deletar), data e hora em BRT.
3. WHEN o Agente_Bedrock não consegue extrair o canal ou a operação da mensagem, THE Agente_Bedrock SHALL solicitar ao usuário as informações faltantes antes de invocar o endpoint `/agendarOperacao`.
4. WHEN o Agente_Bedrock não consegue extrair o horário da mensagem, THE Agente_Bedrock SHALL solicitar ao usuário o horário desejado em BRT antes de invocar o endpoint `/agendarOperacao`.
5. THE Agente_Bedrock SHALL incluir uma rota para listagem de agendamentos com palavras-chave: "listar agendamentos", "o que está agendado", "quais agendamentos", "agendamentos pendentes", "próximas operações agendadas".
6. THE Agente_Bedrock SHALL incluir uma rota para cancelamento de agendamentos com palavras-chave: "cancelar agendamento", "desagendar", "remover agendamento", "cancelar schedule".
7. WHEN o Agente_Bedrock apresenta a confirmação de um agendamento criado, THE Agente_Bedrock SHALL exibir o horário em BRT (não UTC) para facilitar a leitura pelo operador.

### Requisito 12: Infraestrutura CDK para Agendamento de Operações

**User Story:** Como engenheiro de DevOps, eu quero que toda a infraestrutura do Agendamento de Operações seja provisionada pelo CDK de forma reprodutível, para que o deploy seja automatizado e rastreável.

#### Critérios de Aceitação

1. THE MainStack SHALL criar a tabela DynamoDB ScheduledOperations conforme especificado no Requisito 3, com os GSIs GSI_Canal e GSI_Data.
2. THE MainStack SHALL criar a Lambda_Scheduler com runtime Python 3.12, timeout de 300 segundos, memória de 256 MB e handler `lambdas/scheduler/handler.lambda_handler`.
3. THE MainStack SHALL conceder à role IAM da Lambda_Scheduler as seguintes permissões mínimas: `dynamodb:GetItem`, `dynamodb:UpdateItem` na tabela ScheduledOperations; `lambda:InvokeFunction` na Lambda_Configuradora; `sns:Publish` no Topico_SNS_Alertas; `s3:PutObject` no S3_Audit com prefixo `audit/*`.
4. THE MainStack SHALL conceder à role IAM da Lambda_Configuradora as seguintes permissões adicionais: `dynamodb:PutItem`, `dynamodb:GetItem`, `dynamodb:UpdateItem`, `dynamodb:Query` na tabela ScheduledOperations; `scheduler:CreateSchedule`, `scheduler:DeleteSchedule`, `scheduler:GetSchedule` no EventBridge Scheduler; `iam:PassRole` para a role de execução do EventBridge Scheduler target.
5. THE MainStack SHALL criar uma role IAM dedicada para o EventBridge Scheduler invocar a Lambda_Scheduler, com permissão `lambda:InvokeFunction` na Lambda_Scheduler e trust policy para o serviço `scheduler.amazonaws.com`.
6. THE MainStack SHALL adicionar as seguintes variáveis de ambiente à Lambda_Scheduler: `SCHEDULED_OPERATIONS_TABLE` (nome da tabela DynamoDB), `SNS_TOPIC_ARN` (ARN do Topico_SNS_Alertas), `S3_AUDIT_BUCKET` (nome do bucket S3_Audit) e `CONFIGURADORA_FUNCTION_NAME` (nome da Lambda_Configuradora).
7. THE MainStack SHALL adicionar as seguintes variáveis de ambiente à Lambda_Configuradora: `SCHEDULED_OPERATIONS_TABLE` (nome da tabela DynamoDB), `SCHEDULER_ROLE_ARN` (ARN da role IAM do EventBridge Scheduler) e `SCHEDULER_TARGET_ARN` (ARN da Lambda_Scheduler).
8. THE MainStack SHALL exportar o ARN da Lambda_Scheduler como output do CloudFormation com o nome "SchedulerLambdaArn".

### Requisito 13: Sugestões de Agendamento no Frontend_Chat

**User Story:** Como operador de NOC, eu quero ter sugestões de agendamento na sidebar do chat, para que eu possa iniciar um agendamento com um clique sem precisar lembrar a sintaxe correta.

#### Critérios de Aceitação

1. THE Frontend_Chat SHALL incluir uma nova seção "⏰ Agendamentos" na sidebar com os seguintes botões de sugestão: "Agendar parada de canal para amanhã", "Agendar início de canal para sexta", "Listar agendamentos pendentes" e "Cancelar agendamento".
2. WHEN o usuário clica em um botão de sugestão de agendamento, THE Frontend_Chat SHALL inserir o texto da sugestão no campo de entrada do chat, seguindo o mesmo comportamento dos botões de sugestão existentes.
3. THE Frontend_Chat SHALL posicionar a seção "⏰ Agendamentos" na sidebar após a seção de operações de start/stop e antes das seções de criação/modificação.

### Requisito 14: Resiliência da Lambda_Scheduler

**User Story:** Como engenheiro de DevOps, eu quero que a Lambda_Scheduler seja resiliente a falhas transitórias, para que uma falha temporária na Lambda_Configuradora não resulte em operação não executada sem registro.

#### Critérios de Aceitação

1. WHEN a Lambda_Scheduler falha ao invocar a Lambda_Configuradora por erro transitório (timeout, throttling), THE Lambda_Scheduler SHALL aplicar backoff exponencial com até 3 tentativas (intervalos de 2s, 4s, 8s) antes de registrar a falha definitiva.
2. IF após 3 tentativas a Lambda_Configuradora ainda não responder, THEN THE Lambda_Scheduler SHALL atualizar o status do agendamento para `FAILED` no DynamoDB com o campo `erro` descrevendo o motivo, publicar notificação SNS de falha e registrar no S3_Audit.
3. WHEN a Lambda_Scheduler recebe um evento do EventBridge Scheduler, THE Lambda_Scheduler SHALL registrar no CloudWatch Logs o início da execução com o `schedule_id` para facilitar o diagnóstico.
4. THE Lambda_Scheduler SHALL ser configurada com `MaximumRetryAttempts=0` no EventBridge Scheduler para evitar reexecuções automáticas que poderiam duplicar operações destrutivas (a lógica de retry é gerenciada internamente pela Lambda).
5. IF a atualização de status no DynamoDB falhar após a execução bem-sucedida da operação, THEN THE Lambda_Scheduler SHALL registrar o erro no CloudWatch Logs com severidade CRITICAL, incluindo o `schedule_id` e o resultado da operação para reconciliação manual.

### Requisito 15: Serialização e Round-Trip dos Dados de Agendamento

**User Story:** Como engenheiro de dados, eu quero garantir que os dados de agendamento possam ser serializados para JSON e desserializados de volta sem perda de informação, para que a integridade dos dados seja preservada entre o DynamoDB, o EventBridge Scheduler e o S3_Audit.

#### Critérios de Aceitação

1. FOR ALL registros de agendamento persistidos na tabela ScheduledOperations, serializar o item para JSON e desserializar de volta SHALL produzir um dicionário equivalente ao original (propriedade round-trip).
2. THE Lambda_Configuradora SHALL serializar o campo `scheduled_time` como string ISO 8601 com sufixo Z (UTC) ao persistir no DynamoDB e ao criar o EventBridge Scheduler schedule.
3. THE Lambda_Configuradora SHALL serializar o campo `parametros_adicionais` como string JSON ao persistir no DynamoDB e desserializar de volta para objeto ao ler o registro para execução.
4. THE Lambda_Scheduler SHALL serializar o campo `resultado` como objeto JSON ao persistir no DynamoDB, preservando todos os campos retornados pela Lambda_Configuradora sem truncamento.
5. THE Lambda_Configuradora SHALL serializar campos booleanos (`confirmacao_destrutiva`) como valores booleanos JSON nativos (true/false), não como strings, ao persistir no DynamoDB.
6. FOR ALL payloads de notificação SNS gerados pela Lambda_Scheduler, serializar para string e desserializar de volta SHALL preservar todos os campos sem perda de informação, incluindo caracteres Unicode em português (`ensure_ascii=False`).
