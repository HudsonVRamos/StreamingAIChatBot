# Documento de Requisitos — Criação Orquestrada de Canal

## Introdução

Este documento especifica os requisitos para o fluxo de criação orquestrada de canal no Streaming Chatbot. A funcionalidade permite que o usuário crie, via conversa natural, toda a cadeia de recursos necessários para um canal de streaming: canal MediaPackage V2, endpoints HLS e DASH, inputs MediaLive e canal MediaLive — tudo na ordem correta e com vinculação automática entre os recursos. Em caso de falha em qualquer etapa, os recursos já criados são removidos automaticamente (rollback).

## Glossário

- **Lambda_Configuradora**: Lambda responsável por executar chamadas às APIs AWS (MediaLive, MediaPackage V2, MediaTailor, CloudFront) para criar, modificar e consultar recursos de streaming.
- **Lambda_Orquestradora**: Lambda que recebe perguntas do frontend, invoca o Bedrock Agent e retorna respostas ao usuário.
- **Bedrock_Agent**: Agente de IA (Amazon Bedrock) que interpreta linguagem natural e aciona Action Groups para executar operações.
- **Orquestrador_de_Criacao**: Módulo ou fluxo dentro da Lambda_Configuradora responsável por coordenar a criação sequencial de múltiplos recursos e executar rollback em caso de falha.
- **Canal_MPV2**: Canal do serviço MediaPackage V2, que recebe o stream HLS do MediaLive e o distribui via endpoints.
- **Channel_Group**: Agrupamento lógico de canais no MediaPackage V2 (ex: "VRIO_CHANNELS").
- **Endpoint_HLS**: Origin endpoint do MediaPackage V2 que serve conteúdo no formato HLS com encriptação CBCS/FAIRPLAY.
- **Endpoint_DASH**: Origin endpoint do MediaPackage V2 que serve conteúdo no formato DASH com encriptação CENC/PLAYREADY+WIDEVINE.
- **Input_MediaLive**: Recurso de entrada do MediaLive (ex: UDP_PUSH) que alimenta o canal de encoding.
- **Canal_MediaLive**: Canal do MediaLive que realiza o encoding do stream e envia para o MediaPackage V2.
- **Ingest_URL**: URL de ingestão fornecida pelo Canal_MPV2 após sua criação, usada como destino nas Destinations do Canal_MediaLive.
- **Template_JSON**: Arquivo JSON de referência contendo a configuração base de um recurso, usado como ponto de partida para criação de novos recursos.
- **Fluxo_Conversacional**: Interação guiada via chat onde o Bedrock_Agent coleta parâmetros do usuário passo a passo.
- **Rollback**: Processo de exclusão automática de recursos já criados quando uma etapa subsequente falha.
- **DRM_Resource_ID**: Identificador do recurso DRM usado pelo SPEKE Key Provider (ex: "Live_0001").
- **Segment_Duration**: Duração em segundos de cada segmento de vídeo nos endpoints.
- **Manifest_Window**: Janela de tempo em segundos do manifesto disponível para o player.
- **Startover_Window**: Janela de tempo em segundos que permite ao player voltar no conteúdo ao vivo.
- **ChannelClass**: Classificação do canal MediaLive que define a topologia de pipeline (SINGLE_PIPELINE ou STANDARD).
- **Failover**: Mecanismo de redundância onde o input secundário assume automaticamente em caso de perda do input primário.

## Requisitos

### Requisito 1: Coleta Conversacional de Parâmetros

**User Story:** Como operador de streaming, eu quero que o agente me guie passo a passo na coleta dos parâmetros necessários, para que eu não precise fornecer todas as informações de uma vez.

#### Critérios de Aceitação

1. WHEN o usuário solicita a criação de um canal completo, THE Bedrock_Agent SHALL iniciar o Fluxo_Conversacional perguntando o nome do canal.
2. WHEN o nome do canal é fornecido, THE Bedrock_Agent SHALL perguntar o Channel_Group do MediaPackage V2 (sugerindo o padrão "VRIO_CHANNELS").
3. WHEN o Channel_Group é confirmado, THE Bedrock_Agent SHALL perguntar o Template_JSON de referência para o Canal_MediaLive (aceitando busca parcial por nome, ex: "warner").
4. WHEN o template é selecionado, THE Bedrock_Agent SHALL perguntar os parâmetros dos endpoints apresentando valores padrão entre parênteses:
   - Segment_Duration em segundos (padrão: 6)
   - DRM_Resource_ID (padrão: "Live_XXXX" onde XXXX é derivado do nome)
   - Manifest_Window em segundos (padrão: 7200)
   - Startover_Window HLS em segundos (padrão: 900)
   - Startover_Window DASH em segundos (padrão: 14460)
   - TsIncludeDvbSubtitles (padrão: true)
   - MinBufferTimeSeconds para DASH (padrão: 2)
   - SuggestedPresentationDelaySeconds para DASH (padrão: 12)
5. THE Bedrock_Agent SHALL permitir que o usuário aceite todos os padrões de uma vez respondendo "usar padrões" ou "ok".
6. WHEN todos os parâmetros obrigatórios são coletados, THE Bedrock_Agent SHALL apresentar um resumo completo dos parâmetros e solicitar confirmação do usuário antes de iniciar a criação.
7. IF o usuário não confirmar os parâmetros, THEN THE Bedrock_Agent SHALL permitir que o usuário altere qualquer parâmetro individualmente sem reiniciar o fluxo.

### Requisito 2: Criação do Canal MediaPackage V2

**User Story:** Como operador de streaming, eu quero que o canal MediaPackage V2 seja criado primeiro no Channel Group correto, para que os endpoints e o MediaLive possam ser vinculados a ele.

#### Critérios de Aceitação

1. WHEN a criação orquestrada é confirmada pelo usuário, THE Orquestrador_de_Criacao SHALL criar o Canal_MPV2 como primeira etapa da sequência.
2. THE Orquestrador_de_Criacao SHALL criar o Canal_MPV2 dentro do Channel_Group especificado pelo usuário.
3. THE Orquestrador_de_Criacao SHALL definir o ChannelName do Canal_MPV2 com o mesmo nome fornecido pelo usuário para o canal (ex: "TESTE_KIRO").
4. THE Orquestrador_de_Criacao SHALL definir o InputType do Canal_MPV2 como "HLS".
5. WHEN o Canal_MPV2 é criado com sucesso, THE Orquestrador_de_Criacao SHALL extrair a Ingest_URL da resposta da API para uso na etapa de criação do Canal_MediaLive.
6. IF a criação do Canal_MPV2 falhar, THEN THE Orquestrador_de_Criacao SHALL retornar uma mensagem de erro descritiva ao usuário sem prosseguir para as etapas seguintes.

### Requisito 3: Criação dos Endpoints MediaPackage V2

**User Story:** Como operador de streaming, eu quero que os endpoints HLS e DASH sejam criados com todas as configurações de DRM, segmento e manifesto corretas, para que o conteúdo seja distribuído com proteção e qualidade adequadas.

#### Critérios de Aceitação

1. WHEN o Canal_MPV2 é criado com sucesso, THE Orquestrador_de_Criacao SHALL criar o Endpoint_HLS e o Endpoint_DASH como segunda etapa da sequência.
2. THE Orquestrador_de_Criacao SHALL nomear o Endpoint_HLS como "{nome_canal}_HLS" e o Endpoint_DASH como "{nome_canal}_DASH".
3. THE Orquestrador_de_Criacao SHALL definir o ContainerType como "CMAF" em ambos os endpoints.

**Configuração de Segmento (ambos endpoints):**

4. THE Orquestrador_de_Criacao SHALL configurar Segment.SegmentDurationSeconds com o valor fornecido pelo usuário (padrão: 6).
5. THE Orquestrador_de_Criacao SHALL definir Segment.SegmentName como "segment".
6. THE Orquestrador_de_Criacao SHALL definir Segment.TsUseAudioRenditionGroup como true.
7. THE Orquestrador_de_Criacao SHALL definir Segment.IncludeIframeOnlyStreams como false.
8. THE Orquestrador_de_Criacao SHALL configurar Segment.TsIncludeDvbSubtitles com o valor fornecido pelo usuário (padrão: true).

**Configuração de DRM/Encriptação:**

9. THE Orquestrador_de_Criacao SHALL configurar o Endpoint_HLS com Segment.Encryption.EncryptionMethod.CmafEncryptionMethod = "CBCS" e DrmSystems = ["FAIRPLAY"].
10. THE Orquestrador_de_Criacao SHALL configurar o Endpoint_DASH com Segment.Encryption.EncryptionMethod.CmafEncryptionMethod = "CENC" e DrmSystems = ["PLAYREADY", "WIDEVINE"].
11. THE Orquestrador_de_Criacao SHALL configurar SpekeKeyProvider.ResourceId com o DRM_Resource_ID fornecido pelo usuário.
12. THE Orquestrador_de_Criacao SHALL definir SpekeKeyProvider.EncryptionContractConfiguration com PresetSpeke20Audio = "SHARED" e PresetSpeke20Video = "SHARED".
13. THE Orquestrador_de_Criacao SHALL utilizar o SpekeKeyProvider.RoleArn e SpekeKeyProvider.Url fixos da infraestrutura (configurados via variáveis de ambiente).

**Configuração do Endpoint HLS:**

14. THE Orquestrador_de_Criacao SHALL configurar HlsManifests com ManifestName = "master".
15. THE Orquestrador_de_Criacao SHALL configurar HlsManifests.ManifestWindowSeconds com o valor fornecido pelo usuário (padrão: 7200).
16. THE Orquestrador_de_Criacao SHALL configurar StartoverWindowSeconds do Endpoint_HLS com o valor fornecido pelo usuário (padrão: 900).

**Configuração do Endpoint DASH:**

17. THE Orquestrador_de_Criacao SHALL configurar DashManifests com ManifestName = "manifest".
18. THE Orquestrador_de_Criacao SHALL configurar DashManifests.ManifestWindowSeconds com o valor fornecido pelo usuário (padrão: 7200).
19. THE Orquestrador_de_Criacao SHALL configurar DashManifests.MinBufferTimeSeconds com o valor fornecido pelo usuário (padrão: 2).
20. THE Orquestrador_de_Criacao SHALL configurar DashManifests.SuggestedPresentationDelaySeconds com o valor fornecido pelo usuário (padrão: 12).
21. THE Orquestrador_de_Criacao SHALL configurar DashManifests.SegmentTemplateFormat como "NUMBER_WITH_TIMELINE".
22. THE Orquestrador_de_Criacao SHALL configurar DashManifests.PeriodTriggers com ["AVAILS", "DRM_KEY_ROTATION", "SOURCE_CHANGES", "SOURCE_DISRUPTIONS"].
23. THE Orquestrador_de_Criacao SHALL configurar DashManifests.DrmSignaling como "INDIVIDUAL".
24. THE Orquestrador_de_Criacao SHALL configurar DashManifests.UtcTiming.TimingMode como "UTC_DIRECT".
25. THE Orquestrador_de_Criacao SHALL configurar DashManifests.MinUpdatePeriodSeconds com o mesmo valor do SegmentDurationSeconds.
26. THE Orquestrador_de_Criacao SHALL configurar StartoverWindowSeconds do Endpoint_DASH com o valor fornecido pelo usuário (padrão: 14460).

**Rollback:**

27. IF a criação de qualquer endpoint falhar, THEN THE Orquestrador_de_Criacao SHALL executar o Rollback do Canal_MPV2 e de qualquer endpoint já criado.

### Requisito 4: Criação dos Inputs MediaLive

**User Story:** Como operador de streaming, eu quero que os inputs do MediaLive sejam criados automaticamente com base no ChannelClass do template, para que a redundância seja configurada corretamente.

#### Critérios de Aceitação

1. WHEN os endpoints são criados com sucesso, THE Orquestrador_de_Criacao SHALL criar os Input_MediaLive como terceira etapa da sequência.
2. WHILE o ChannelClass do template é "SINGLE_PIPELINE", THE Orquestrador_de_Criacao SHALL criar dois inputs nomeados "{nome_canal}_INPUT_1" e "{nome_canal}_INPUT_2".
3. WHILE o ChannelClass do template é "STANDARD", THE Orquestrador_de_Criacao SHALL criar um único input nomeado "{nome_canal}_INPUT".
4. THE Orquestrador_de_Criacao SHALL detectar o tipo de input (ex: UDP_PUSH) a partir do template de referência.
5. WHILE o ChannelClass é "SINGLE_PIPELINE", THE Orquestrador_de_Criacao SHALL configurar o Failover automático no primeiro input, apontando para o segundo input como secundário.
6. IF a criação de qualquer input falhar, THEN THE Orquestrador_de_Criacao SHALL executar o Rollback dos endpoints, do Canal_MPV2 e de qualquer input já criado.

### Requisito 5: Criação do Canal MediaLive

**User Story:** Como operador de streaming, eu quero que o canal MediaLive seja criado a partir do template com as Destinations apontando para o MediaPackage V2, para que o stream seja entregue corretamente.

#### Critérios de Aceitação

1. WHEN os inputs são criados com sucesso, THE Orquestrador_de_Criacao SHALL criar o Canal_MediaLive como quarta e última etapa da sequência.
2. THE Orquestrador_de_Criacao SHALL criar o Canal_MediaLive a partir do Template_JSON selecionado pelo usuário.
3. THE Orquestrador_de_Criacao SHALL configurar Destinations.Settings.Url com a Ingest_URL obtida do Canal_MPV2.
4. THE Orquestrador_de_Criacao SHALL derivar Destinations.Id a partir do nome do canal, substituindo underscores por hífens.
5. THE Orquestrador_de_Criacao SHALL vincular os Input_MediaLive criados na etapa anterior ao Canal_MediaLive via InputAttachments.
6. IF a criação do Canal_MediaLive falhar, THEN THE Orquestrador_de_Criacao SHALL executar o Rollback de todos os recursos criados nas etapas anteriores (inputs, endpoints e Canal_MPV2).

### Requisito 6: Rollback Automático

**User Story:** Como operador de streaming, eu quero que recursos parcialmente criados sejam removidos automaticamente em caso de falha, para que não fiquem recursos órfãos na conta AWS.

#### Critérios de Aceitação

1. IF qualquer etapa da criação orquestrada falhar, THEN THE Orquestrador_de_Criacao SHALL excluir todos os recursos criados nas etapas anteriores na ordem inversa de criação.
2. THE Orquestrador_de_Criacao SHALL registrar cada recurso criado com sucesso em uma lista de rollback antes de prosseguir para a próxima etapa.
3. WHEN o rollback é executado, THE Orquestrador_de_Criacao SHALL registrar no log de auditoria (S3_Audit) cada exclusão realizada durante o rollback.
4. IF a exclusão de um recurso durante o rollback falhar, THEN THE Orquestrador_de_Criacao SHALL registrar o erro no log de auditoria e continuar tentando excluir os demais recursos.
5. WHEN o rollback é concluído, THE Orquestrador_de_Criacao SHALL retornar ao usuário uma mensagem indicando a falha original, os recursos que foram removidos com sucesso e os recursos que falharam na remoção (se houver).

### Requisito 7: Feedback de Progresso ao Usuário

**User Story:** Como operador de streaming, eu quero receber feedback em tempo real sobre o progresso da criação, para que eu saiba em qual etapa o processo se encontra.

#### Critérios de Aceitação

1. WHEN cada etapa da criação orquestrada é iniciada, THE Bedrock_Agent SHALL informar ao usuário qual recurso está sendo criado (ex: "Criando canal MediaPackage V2...").
2. WHEN cada etapa é concluída com sucesso, THE Bedrock_Agent SHALL informar ao usuário o resultado, incluindo o identificador do recurso criado.
3. WHEN todas as etapas são concluídas com sucesso, THE Bedrock_Agent SHALL apresentar um resumo final contendo os identificadores de todos os recursos criados e a Ingest_URL do Canal_MPV2.
4. WHEN todas as etapas são concluídas com sucesso, THE Bedrock_Agent SHALL disponibilizar o JSON de configuração do Canal_MediaLive para download pelo usuário.

### Requisito 8: Novo Endpoint na Lambda Configuradora

**User Story:** Como desenvolvedor, eu quero um endpoint dedicado na Lambda_Configuradora para a criação orquestrada, para que a lógica de sequenciamento e rollback fique encapsulada.

#### Critérios de Aceitação

1. THE Lambda_Configuradora SHALL expor um novo apiPath "/criarCanalOrquestrado" que aceita todos os parâmetros necessários para a criação completa.
2. WHEN o endpoint "/criarCanalOrquestrado" é invocado, THE Lambda_Configuradora SHALL executar as quatro etapas de criação na ordem: Canal_MPV2, Endpoints, Inputs, Canal_MediaLive.
3. THE Lambda_Configuradora SHALL aceitar os seguintes parâmetros no endpoint "/criarCanalOrquestrado": nome_canal, channel_group, template_resource_id, segment_duration, drm_resource_id, manifest_window_seconds, startover_window_hls_seconds, startover_window_dash_seconds, ts_include_dvb_subtitles, min_buffer_time_seconds e suggested_presentation_delay_seconds.
4. IF qualquer parâmetro obrigatório estiver ausente, THEN THE Lambda_Configuradora SHALL retornar erro 400 com a lista de parâmetros faltantes.
5. THE Lambda_Configuradora SHALL registrar no log de auditoria (S3_Audit) a operação completa de criação orquestrada, incluindo todos os recursos criados ou a falha com detalhes do rollback.

### Requisito 9: Integração com o Bedrock Agent

**User Story:** Como desenvolvedor, eu quero que o Bedrock Agent tenha acesso ao novo endpoint de criação orquestrada, para que o fluxo conversacional possa acionar a criação completa.

#### Critérios de Aceitação

1. THE Bedrock_Agent SHALL ter um Action Group configurado com acesso ao endpoint "/criarCanalOrquestrado" da Lambda_Configuradora.
2. THE Bedrock_Agent SHALL ser capaz de mapear os parâmetros coletados no Fluxo_Conversacional para os parâmetros do endpoint "/criarCanalOrquestrado".
3. WHEN o Bedrock_Agent recebe a resposta do endpoint, THE Bedrock_Agent SHALL interpretar o resultado e apresentar ao usuário de forma legível, incluindo identificadores dos recursos e status de cada etapa.

### Requisito 10: Criação de Recursos MediaPackage V2 na Lambda Configuradora

**User Story:** Como desenvolvedor, eu quero que a Lambda_Configuradora suporte a criação de canais e endpoints MediaPackage V2 via API, para que o orquestrador possa utilizá-los.

#### Critérios de Aceitação

1. THE Lambda_Configuradora SHALL suportar a criação de Canal_MPV2 via o endpoint "/criarRecurso" com servico="MediaPackage" e tipo_recurso="channel_v2".
2. THE Lambda_Configuradora SHALL suportar a criação de Endpoint_HLS e Endpoint_DASH via o endpoint "/criarRecurso" com servico="MediaPackage" e tipo_recurso="origin_endpoint_v2".
3. THE Lambda_Configuradora SHALL suportar a exclusão de Canal_MPV2 e endpoints V2 para viabilizar o Rollback.
4. WHEN um Canal_MPV2 é criado, THE Lambda_Configuradora SHALL retornar a Ingest_URL na resposta.
