# Plano de Implementação: Criação Orquestrada de Canal

## Visão Geral

Implementação do fluxo de criação orquestrada de canal na Lambda_Configuradora existente. O trabalho envolve adicionar novas funções auxiliares (`delete_resource`, `_build_endpoint_config`, `_execute_orchestrated_creation`), estender `create_resource()` para suportar MediaPackage V2, adicionar o endpoint `/criarCanalOrquestrado` no handler, e atualizar o CDK stack com novas variáveis de ambiente e timeout. Todos os testes usam Python com Hypothesis para property-based testing.

## Tarefas

- [x] 1. Adicionar dataclasses e função `delete_resource()`
  - [x] 1.1 Criar dataclasses `RollbackEntry`, `OrchestrationParams` e `OrchestrationResult` em `lambdas/configuradora/handler.py`
    - Adicionar as três dataclasses conforme definido no design (seção Modelos de Dados)
    - `OrchestrationParams` deve ter valores padrão para todos os campos opcionais
    - _Requisitos: 8.3_
  - [x] 1.2 Implementar a função `delete_resource()` em `lambdas/configuradora/handler.py`
    - Suportar exclusão de: `channel_v2` e `origin_endpoint_v2` (MediaPackage V2), `input` e `channel` (MediaLive)
    - Para MPV2, usar `mediapackagev2_client.delete_channel()` e `mediapackagev2_client.delete_origin_endpoint()`
    - Para MediaLive, usar `medialive_client.delete_input()` e `medialive_client.delete_channel()`
    - Retornar dict com status da exclusão
    - _Requisitos: 6.1, 10.3_
  - [x] 1.3 Estender `create_resource()` para suportar `channel_v2` e `origin_endpoint_v2`
    - Adicionar bloco para `tipo_recurso == "channel_v2"` usando `mediapackagev2_client.create_channel()`
    - Adicionar bloco para `tipo_recurso == "origin_endpoint_v2"` usando `mediapackagev2_client.create_origin_endpoint()`
    - Retornar `resource_id` e `details` no mesmo formato dos outros recursos
    - _Requisitos: 10.1, 10.2, 10.4_

- [x] 2. Implementar funções de construção de endpoints e orquestração
  - [x] 2.1 Implementar `_build_endpoint_config()` em `lambdas/configuradora/handler.py`
    - Aceitar parâmetros do `OrchestrationParams` e tipo ("HLS" ou "DASH")
    - Montar payload completo com `Segment`, `Encryption`, `SpekeKeyProvider` (usando env vars `SPEKE_ROLE_ARN` e `SPEKE_URL`)
    - Para HLS: `CmafEncryptionMethod=CBCS`, `DrmSystems=["FAIRPLAY"]`, `HlsManifests` com `ManifestName="master"`
    - Para DASH: `CmafEncryptionMethod=CENC`, `DrmSystems=["PLAYREADY","WIDEVINE"]`, `DashManifests` com todos os campos (PeriodTriggers, DrmSignaling, UtcTiming, MinUpdatePeriodSeconds=segment_duration, etc.)
    - Campos fixos: `ContainerType="CMAF"`, `SegmentName="segment"`, `TsUseAudioRenditionGroup=true`, `IncludeIframeOnlyStreams=false`
    - _Requisitos: 3.1–3.26_
  - [x] 2.2 Escrever teste property-based para construção de endpoints
    - **Propriedade 1: Construção de endpoint reflete parâmetros do usuário**
    - **Valida: Requisitos 3.4, 3.8, 3.11, 3.15, 3.16, 3.18, 3.19, 3.20, 3.25, 3.26**
    - Criar `tests/test_property_orchestration.py` com Hypothesis
    - Gerar valores aleatórios para segment_duration, drm_resource_id, manifest_window_seconds, etc.
    - Verificar que os payloads HLS e DASH contêm exatamente os valores fornecidos nos campos corretos
    - Verificar que `DashManifests[0].MinUpdatePeriodSeconds == segment_duration`
  - [x] 2.3 Escrever teste property-based para convenções de nomenclatura
    - **Propriedade 5: Convenções de nomenclatura**
    - **Valida: Requisitos 3.2, 4.2, 4.3, 5.4**
    - Gerar nomes de canal aleatórios e verificar: endpoint HLS = `"{nome}_HLS"`, DASH = `"{nome}_DASH"`, inputs SINGLE_PIPELINE = `"{nome}_INPUT_1"` e `"{nome}_INPUT_2"`, input STANDARD = `"{nome}_INPUT"`, `Destinations.Id` = nome com underscores substituídos por hífens

- [x] 3. Implementar a função de extração de Ingest URL e rollback
  - [x] 3.1 Implementar `_extract_ingest_url()` em `lambdas/configuradora/handler.py`
    - Extrair a URL de ingestão da resposta da API `CreateChannel` do MPV2
    - Navegar em `IngestEndpoints` e retornar a primeira URL disponível
    - Lançar exceção se nenhuma URL for encontrada
    - _Requisitos: 2.5, 10.4_
  - [x] 3.2 Implementar `_execute_rollback()` em `lambdas/configuradora/handler.py`
    - Receber lista de `RollbackEntry` e excluir na ordem inversa
    - Usar `delete_resource()` para cada entrada
    - Registrar cada exclusão no audit log via `store_audit_log()`
    - Se a exclusão de um recurso falhar, registrar o erro e continuar com os demais
    - Retornar listas de recursos removidos e recursos com falha na remoção
    - _Requisitos: 6.1, 6.2, 6.3, 6.4, 6.5_
  - [x] 3.3 Escrever teste property-based para extração de Ingest URL
    - **Propriedade 2: Extração de Ingest URL**
    - **Valida: Requisitos 2.5, 10.4**
    - Gerar respostas simuladas da API com IngestEndpoints variados
    - Verificar que a URL retornada é não-vazia e corresponde a um dos endpoints
  - [x] 3.4 Escrever teste property-based para completude e ordenação do rollback
    - **Propriedade 3: Completude e ordenação do rollback**
    - **Valida: Requisitos 3.27, 4.6, 5.6, 6.1, 6.2**
    - Gerar listas de rollback com tamanhos variados e verificar que a ordem de exclusão é inversa à de criação
  - [x] 3.5 Escrever teste property-based para resiliência do rollback
    - **Propriedade 4: Resiliência do rollback**
    - **Valida: Requisitos 6.4, 6.5**
    - Gerar padrões aleatórios de falha (nenhuma, todas, aleatórias) durante exclusão
    - Verificar que todos os recursos são tentados e que o resultado classifica corretamente removidos vs. falha

- [x] 4. Checkpoint — Garantir que todos os testes passam
  - Garantir que todos os testes passam, perguntar ao usuário se houver dúvidas.

- [x] 5. Implementar a função principal de orquestração
  - [x] 5.1 Implementar `_execute_orchestrated_creation()` em `lambdas/configuradora/handler.py`
    - Executar as 4 etapas sequenciais: (1) Canal MPV2, (2) Endpoints HLS+DASH, (3) Inputs MediaLive, (4) Canal MediaLive
    - Etapa 1: Criar canal MPV2 com `create_resource("MediaPackage", "channel_v2", ...)`, extrair ingest_url
    - Etapa 2: Criar endpoints usando `_build_endpoint_config()` + `create_resource("MediaPackage", "origin_endpoint_v2", ...)`
    - Etapa 3: Reutilizar `_create_inputs_for_channel()` existente para criar inputs
    - Etapa 4: Usar `get_full_config()` para buscar template, aplicar modificações (Name, Destinations com ingest_url, InputAttachments), criar canal via `create_resource()`
    - Registrar cada recurso criado na `rollback_stack` (lista de `RollbackEntry`)
    - Em caso de falha, chamar `_execute_rollback(rollback_stack)`
    - Fazer upload do JSON do canal MediaLive via `upload_config_json()`
    - Retornar `OrchestrationResult` com todos os identificadores
    - _Requisitos: 2.1–2.6, 3.1–3.27, 4.1–4.6, 5.1–5.6, 6.1–6.5, 8.2_
  - [x] 5.2 Escrever teste property-based para passthrough da Ingest URL
    - **Propriedade 7: Passthrough da Ingest URL para Destinations**
    - **Valida: Requisitos 5.3**
    - Gerar ingest URLs aleatórias e verificar que aparecem em `Destinations[*].Settings[*].Url` do payload do canal MediaLive

- [x] 6. Adicionar endpoint `/criarCanalOrquestrado` no handler e validação
  - [x] 6.1 Adicionar bloco para `/criarCanalOrquestrado` na função `handler()` em `lambdas/configuradora/handler.py`
    - Extrair parâmetros via `_parse_parameters()` existente
    - Validar parâmetros obrigatórios (`nome_canal`, `channel_group`, `template_resource_id`) — retornar 400 com lista de faltantes
    - Aplicar valores padrão para parâmetros opcionais
    - Construir `OrchestrationParams` e chamar `_execute_orchestrated_creation()`
    - Registrar audit log completo (sucesso ou falha com rollback)
    - Retornar resposta no formato Bedrock Action Group com `_bedrock_response()`
    - Incluir `marcador_download` na resposta de sucesso para o frontend gerar botão de download
    - _Requisitos: 8.1, 8.2, 8.3, 8.4, 8.5, 7.1, 7.2, 7.3, 7.4_
  - [x] 6.2 Escrever teste property-based para validação de parâmetros obrigatórios
    - **Propriedade 6: Validação de parâmetros obrigatórios ausentes**
    - **Valida: Requisitos 8.4**
    - Gerar subconjuntos aleatórios dos 3 parâmetros obrigatórios ausentes
    - Verificar que o erro 400 lista exatamente os parâmetros faltantes

- [x] 7. Atualizar CDK stack e variáveis de ambiente
  - [x] 7.1 Atualizar `stacks/main_stack.py` com novas variáveis de ambiente e timeout
    - Adicionar `SPEKE_ROLE_ARN` e `SPEKE_URL` ao environment da Lambda_Configuradora
    - Aumentar timeout da Lambda_Configuradora de 60s para 120s (`Duration.seconds(120)`)
    - _Requisitos: 3.13, Design seção 7_

- [x] 8. Escrever testes unitários
  - [x] 8.1 Criar `tests/test_unit_orchestration.py` com testes unitários
    - Testar fluxo completo com sucesso (mock de todas as APIs)
    - Testar falha na etapa 1 (sem rollback)
    - Testar falha na etapa 2 (rollback do Canal MPV2)
    - Testar falha na etapa 3 (rollback de endpoints + Canal MPV2)
    - Testar falha na etapa 4 (rollback completo)
    - Testar validação de parâmetros ausentes e inválidos
    - Testar valores padrão dos parâmetros opcionais
    - Testar DRM config HLS (CBCS/FAIRPLAY) vs DASH (CENC/PLAYREADY+WIDEVINE)
    - Testar campos fixos dos endpoints (ContainerType, SegmentName, ManifestName)
    - _Requisitos: 2.1–2.6, 3.1–3.27, 4.1–4.6, 5.1–5.6, 6.1–6.5, 8.1–8.5_

- [x] 9. Checkpoint final — Garantir que todos os testes passam
  - Garantir que todos os testes passam, perguntar ao usuário se houver dúvidas.

## Notas

- Tarefas marcadas com `*` são opcionais e podem ser puladas para um MVP mais rápido
- Cada tarefa referencia requisitos específicos para rastreabilidade
- Checkpoints garantem validação incremental
- Testes property-based validam propriedades universais de corretude
- Testes unitários validam exemplos específicos e casos de borda
- O projeto já usa Hypothesis para property-based testing
