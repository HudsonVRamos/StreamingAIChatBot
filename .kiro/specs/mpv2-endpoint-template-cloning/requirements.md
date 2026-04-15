# Documento de Requisitos — Clonagem de Endpoints MPV2 a partir de Template

## Introdução

Este documento especifica os requisitos para aprimorar o fluxo de criação orquestrada de canal (`_execute_orchestrated_creation`) na Lambda Configuradora, de modo que os endpoints MediaPackage V2 (HLS e DASH) sejam clonados a partir dos endpoints do template selecionado, em vez de serem criados com parâmetros hardcoded via `_build_endpoint_config`. Atualmente, o template MediaLive é usado apenas para configuração do encoder (video descriptions, audio descriptions, output groups), enquanto os endpoints MPV2 são sempre criados com valores fixos. Com esta funcionalidade, toda a configuração do endpoint de origem — incluindo segment settings, manifest settings, SCTE filters, encryption method e DRM systems — será preservada do template, alterando apenas os campos específicos do novo canal (nomes, DRM ResourceId, CDN auth policy, SPEKE URL/RoleArn).

## Glossário

- **Lambda_Configuradora**: Lambda responsável por executar chamadas às APIs AWS (MediaLive, MediaPackage V2, MediaTailor, CloudFront) para criar, modificar e consultar recursos de streaming.
- **Orquestrador_de_Criacao**: Módulo dentro da Lambda_Configuradora que coordena a criação sequencial de múltiplos recursos e executa rollback em caso de falha (função `_execute_orchestrated_creation`).
- **Canal_MediaLive_Template**: Canal MediaLive existente selecionado como template de referência para a criação de um novo canal.
- **Canal_MPV2_Template**: Canal MediaPackage V2 associado ao Canal_MediaLive_Template, identificado a partir das Destinations do template.
- **Endpoint_Template**: Origin endpoint (HLS ou DASH) existente no Canal_MPV2_Template, cuja configuração será clonada para o novo canal.
- **Endpoint_Clonado**: Novo origin endpoint criado com a configuração copiada do Endpoint_Template, com campos específicos substituídos para o novo canal.
- **MediaPackageSettings_Format**: Formato de Destination do MediaLive que usa `MediaPackageSettings` com `ChannelGroup` e `ChannelName` para integração direta com MPV2 (ex: AWS_LL_CHANNEL).
- **CMAF_Ingest_Format**: Formato de Destination do MediaLive que usa `Settings` com uma URL de ingestão CMAF apontando para o MPV2 (ex: 0001_WARNER_CHANNEL).
- **Channel_Group**: Agrupamento lógico de canais no MediaPackage V2 (ex: "VRIO_CHANNELS").
- **DRM_Resource_ID**: Identificador do recurso DRM usado pelo SPEKE Key Provider, seguindo o padrão `Live_{nome_canal}`.
- **CDN_Auth_Policy**: Política de acesso ao endpoint que restringe requisições via header CDN, vinculada ao ARN do endpoint específico.
- **SPEKE_URL**: URL do SPEKE Key Provider configurada via variável de ambiente, usada para solicitar chaves DRM.
- **SPEKE_ROLE_ARN**: ARN do IAM Role que o MediaPackage V2 assume para invocar o SPEKE Key Provider, configurado via variável de ambiente.
- **Campos_Imutaveis_Template**: Campos do Endpoint_Template que são copiados sem alteração: `ContainerType`, `Segment` (exceto `Encryption.SpekeKeyProvider`), manifests (HLS/DASH/LowLatencyHLS), `StartoverWindowSeconds`.
- **Campos_Substituidos**: Campos que são recalculados para o novo canal: `ChannelGroupName`, `ChannelName`, `OriginEndpointName`, `Encryption.SpekeKeyProvider.ResourceId`, `Encryption.SpekeKeyProvider.RoleArn`, `Encryption.SpekeKeyProvider.Url`.
- **Ingest_URL**: URL de ingestão fornecida pelo Canal MPV2 após sua criação, usada como destino nas Destinations do Canal_MediaLive.

## Requisitos

### Requisito 1: Detecção do Canal MPV2 do Template via MediaPackageSettings

**User Story:** Como operador de streaming, eu quero que o sistema detecte automaticamente qual canal MPV2 está associado ao template MediaLive quando o template usa o formato MediaPackageSettings, para que os endpoints corretos sejam identificados para clonagem.

#### Critérios de Aceitação

1. WHEN o Canal_MediaLive_Template possui Destinations com `MediaPackageSettings` contendo `ChannelGroup` e `ChannelName`, THE Orquestrador_de_Criacao SHALL extrair o `ChannelGroup` e `ChannelName` do primeiro `MediaPackageSettings` encontrado.
2. WHEN o `ChannelGroup` e `ChannelName` são extraídos, THE Orquestrador_de_Criacao SHALL usar esses valores para identificar o Canal_MPV2_Template.
3. IF o Canal_MediaLive_Template possui Destinations com `MediaPackageSettings` vazio (lista vazia), THEN THE Orquestrador_de_Criacao SHALL tratar como formato CMAF_Ingest_Format e tentar a detecção via URL.

### Requisito 2: Detecção do Canal MPV2 do Template via URL de Ingestão CMAF

**User Story:** Como operador de streaming, eu quero que o sistema detecte automaticamente qual canal MPV2 está associado ao template MediaLive quando o template usa URLs de ingestão CMAF, para que os endpoints corretos sejam identificados para clonagem.

#### Critérios de Aceitação

1. WHEN o Canal_MediaLive_Template possui Destinations com `Settings` contendo uma URL no formato `https://*.mediapackagev2.*.amazonaws.com/in/v1/{ChannelGroup}/{index}/{ChannelName}/*`, THE Orquestrador_de_Criacao SHALL extrair o `ChannelGroup` e `ChannelName` a partir da URL.
2. WHEN o `ChannelGroup` e `ChannelName` são extraídos da URL, THE Orquestrador_de_Criacao SHALL usar esses valores para identificar o Canal_MPV2_Template.
3. IF a URL de ingestão não corresponder ao padrão esperado do MediaPackage V2, THEN THE Orquestrador_de_Criacao SHALL registrar um aviso no log e prosseguir com a criação de endpoints usando `_build_endpoint_config` (comportamento atual como fallback).

### Requisito 3: Listagem dos Endpoints do Canal MPV2 Template

**User Story:** Como operador de streaming, eu quero que o sistema busque todos os endpoints existentes no canal MPV2 do template, para que suas configurações possam ser clonadas.

#### Critérios de Aceitação

1. WHEN o Canal_MPV2_Template é identificado, THE Orquestrador_de_Criacao SHALL listar todos os origin endpoints do Canal_MPV2_Template usando a API `mediapackagev2_client.list_origin_endpoints`.
2. WHEN os endpoints são listados, THE Orquestrador_de_Criacao SHALL buscar a configuração completa de cada endpoint usando `mediapackagev2_client.get_origin_endpoint`.
3. IF o Canal_MPV2_Template não possuir nenhum endpoint, THEN THE Orquestrador_de_Criacao SHALL registrar um aviso no log e prosseguir com a criação de endpoints usando `_build_endpoint_config` (fallback).
4. IF a chamada à API `list_origin_endpoints` ou `get_origin_endpoint` falhar, THEN THE Orquestrador_de_Criacao SHALL registrar o erro no log e prosseguir com `_build_endpoint_config` (fallback).

### Requisito 4: Clonagem da Configuração do Endpoint

**User Story:** Como operador de streaming, eu quero que a configuração completa de cada endpoint do template seja copiada para o novo canal, preservando segment settings, manifest settings e SCTE filters, para que o novo canal tenha a mesma qualidade de entrega do template.

#### Critérios de Aceitação

1. THE Orquestrador_de_Criacao SHALL copiar os seguintes campos do Endpoint_Template sem alteração: `ContainerType`, `Segment.SegmentDurationSeconds`, `Segment.SegmentName`, `Segment.TsUseAudioRenditionGroup`, `Segment.IncludeIframeOnlyStreams`, `Segment.TsIncludeDvbSubtitles`, `Segment.Scte` (se presente).
2. THE Orquestrador_de_Criacao SHALL copiar os manifests do Endpoint_Template sem alteração de estrutura: `HlsManifests`, `LowLatencyHlsManifests`, `DashManifests` (incluindo todos os sub-campos como `ManifestWindowSeconds`, `MinUpdatePeriodSeconds`, `PeriodTriggers`, `DrmSignaling`, `UtcTiming`, `Compactness`, `ScteHls`).
3. THE Orquestrador_de_Criacao SHALL copiar o `StartoverWindowSeconds` do Endpoint_Template sem alteração.
4. THE Orquestrador_de_Criacao SHALL copiar o `Segment.Encryption.EncryptionMethod` do Endpoint_Template sem alteração (preservando CBCS ou CENC conforme o template).
5. THE Orquestrador_de_Criacao SHALL copiar a lista `Segment.Encryption.SpekeKeyProvider.DrmSystems` do Endpoint_Template sem alteração (preservando FAIRPLAY, PLAYREADY, WIDEVINE conforme o template).
6. THE Orquestrador_de_Criacao SHALL copiar o `Segment.Encryption.SpekeKeyProvider.EncryptionContractConfiguration` do Endpoint_Template sem alteração.

### Requisito 5: Substituição dos Campos Específicos do Novo Canal

**User Story:** Como operador de streaming, eu quero que os campos de identificação e credenciais sejam atualizados para o novo canal, para que o endpoint clonado funcione corretamente com o novo canal MPV2 e o DRM.

#### Critérios de Aceitação

1. THE Orquestrador_de_Criacao SHALL substituir o `ChannelGroupName` do Endpoint_Clonado pelo Channel_Group do novo canal.
2. THE Orquestrador_de_Criacao SHALL substituir o `ChannelName` do Endpoint_Clonado pelo nome do novo canal.
3. THE Orquestrador_de_Criacao SHALL gerar o `OriginEndpointName` do Endpoint_Clonado substituindo o nome do canal template pelo nome do novo canal no `OriginEndpointName` original (ex: `0001_WARNER_CHANNEL_HLS` → `NOVO_CANAL_HLS`).
4. THE Orquestrador_de_Criacao SHALL substituir o `Segment.Encryption.SpekeKeyProvider.ResourceId` pelo DRM_Resource_ID do novo canal (padrão: `Live_{nome_canal}`).
5. THE Orquestrador_de_Criacao SHALL substituir o `Segment.Encryption.SpekeKeyProvider.RoleArn` pelo valor da variável de ambiente `SPEKE_ROLE_ARN`.
6. THE Orquestrador_de_Criacao SHALL substituir o `Segment.Encryption.SpekeKeyProvider.Url` pelo valor da variável de ambiente `SPEKE_URL`.

### Requisito 6: Aplicação da CDN Auth Policy nos Endpoints Clonados

**User Story:** Como operador de streaming, eu quero que a política de CDN auth seja aplicada aos endpoints clonados com o ARN correto do novo endpoint, para que o acesso via CloudFront funcione corretamente.

#### Critérios de Aceitação

1. WHEN um Endpoint_Clonado é criado com sucesso, THE Orquestrador_de_Criacao SHALL aplicar a CDN_Auth_Policy usando o ARN do novo endpoint (construído com o novo `ChannelGroupName`, `ChannelName` e `OriginEndpointName`).
2. THE Orquestrador_de_Criacao SHALL manter o mesmo comportamento existente de CDN auth: usar `CDN_SECRET_ARN` e `CDN_SECRET_ROLE_ARN` das variáveis de ambiente para configurar `CdnAuthConfiguration`.
3. IF a aplicação da CDN_Auth_Policy falhar, THEN THE Orquestrador_de_Criacao SHALL registrar um aviso no log e continuar a criação (mesmo comportamento atual de tolerância a falha na policy).

### Requisito 7: Remoção de Campos Read-Only do Endpoint Template

**User Story:** Como desenvolvedor, eu quero que campos read-only retornados pela API `get_origin_endpoint` sejam removidos antes de criar o novo endpoint, para que a chamada `create_origin_endpoint` não falhe por campos inválidos.

#### Critérios de Aceitação

1. THE Orquestrador_de_Criacao SHALL remover os seguintes campos read-only do Endpoint_Template antes de criar o Endpoint_Clonado: `Arn`, `CreatedAt`, `ModifiedAt`, `ETag`, `Tags`.
2. THE Orquestrador_de_Criacao SHALL remover campos de URL gerados pela API dos manifests: `Url` dentro de cada entrada em `HlsManifests`, `LowLatencyHlsManifests`, `DashManifests`.
3. IF o Endpoint_Template contiver campos desconhecidos ou adicionais não esperados pela API `create_origin_endpoint`, THEN THE Orquestrador_de_Criacao SHALL ignorar esses campos sem causar falha.

### Requisito 8: Fallback para Criação com Parâmetros Hardcoded

**User Story:** Como operador de streaming, eu quero que o sistema continue funcionando mesmo quando não for possível clonar endpoints do template, para que a criação de canais não seja bloqueada.

#### Critérios de Aceitação

1. IF o Canal_MPV2_Template não puder ser identificado (URL não reconhecida, MediaPackageSettings vazio sem URL), THEN THE Orquestrador_de_Criacao SHALL criar os endpoints usando `_build_endpoint_config` com os parâmetros fornecidos pelo usuário (comportamento atual).
2. IF a listagem ou busca de endpoints do Canal_MPV2_Template falhar por erro de API, THEN THE Orquestrador_de_Criacao SHALL criar os endpoints usando `_build_endpoint_config` (fallback).
3. WHEN o fallback é utilizado, THE Orquestrador_de_Criacao SHALL registrar no log o motivo pelo qual a clonagem não foi possível e que o fallback foi ativado.
4. THE Orquestrador_de_Criacao SHALL manter a função `_build_endpoint_config` existente sem alterações, para uso como fallback.

### Requisito 9: Suporte a Múltiplos Tipos de Endpoint

**User Story:** Como operador de streaming, eu quero que todos os tipos de endpoint do template sejam clonados (incluindo endpoints com LowLatencyHlsManifests), para que canais low-latency sejam replicados corretamente.

#### Critérios de Aceitação

1. THE Orquestrador_de_Criacao SHALL clonar endpoints que possuem `HlsManifests` (HLS padrão).
2. THE Orquestrador_de_Criacao SHALL clonar endpoints que possuem `LowLatencyHlsManifests` (HLS low-latency).
3. THE Orquestrador_de_Criacao SHALL clonar endpoints que possuem `DashManifests` (DASH padrão).
4. WHEN o Canal_MPV2_Template possui mais de dois endpoints, THE Orquestrador_de_Criacao SHALL clonar todos os endpoints encontrados (não apenas HLS e DASH).
5. THE Orquestrador_de_Criacao SHALL registrar cada Endpoint_Clonado na lista de rollback para exclusão em caso de falha.

### Requisito 10: Geração do Nome do Endpoint Clonado

**User Story:** Como operador de streaming, eu quero que os nomes dos endpoints clonados sigam as convenções de nomenclatura do ambiente, para que sejam consistentes com os canais existentes.

#### Critérios de Aceitação

1. WHEN o novo canal é um canal padrão (sem "LL" no nome), THE Orquestrador_de_Criacao SHALL gerar endpoints com sufixos `_HLS` e `_DASH` (ex: `NOVO_CANAL_HLS`, `NOVO_CANAL_DASH`).
2. WHEN o novo canal é um canal Low-Latency (com "LL" no nome), THE Orquestrador_de_Criacao SHALL gerar endpoints com sufixos baseados na encryption: `_CBCS` para HLS/FAIRPLAY e `_CENC` para DASH/PLAYREADY+WIDEVINE (ex: `NOVO_CANAL_LL_CBCS`, `NOVO_CANAL_LL_CENC`).
3. THE Orquestrador_de_Criacao SHALL detectar o tipo de encryption do endpoint template (`CmafEncryptionMethod`: CBCS ou CENC) para determinar o sufixo correto em canais Low-Latency.
4. THE Orquestrador_de_Criacao SHALL detectar o tipo de manifest do endpoint template (HlsManifests/LowLatencyHlsManifests → HLS, DashManifests → DASH) para determinar o sufixo correto em canais padrão.
5. THE Orquestrador_de_Criacao SHALL preservar as configurações de encryption de cada endpoint do template (FAIRPLAY para CBCS, PLAYREADY+WIDEVINE para CENC) independentemente do tipo de canal.

### Requisito 11: Integração com o Fluxo de Rollback Existente

**User Story:** Como desenvolvedor, eu quero que os endpoints clonados sejam integrados ao mecanismo de rollback existente, para que recursos órfãos não fiquem na conta AWS em caso de falha.

#### Critérios de Aceitação

1. WHEN um Endpoint_Clonado é criado com sucesso, THE Orquestrador_de_Criacao SHALL adicionar uma entrada `RollbackEntry` com `servico="MediaPackage"`, `tipo_recurso="origin_endpoint_v2"`, e os identificadores corretos (`channel_group`, `channel_name`, `endpoint_name`).
2. IF a criação de qualquer Endpoint_Clonado falhar, THEN THE Orquestrador_de_Criacao SHALL executar o rollback de todos os recursos já criados (Canal_MPV2 e endpoints anteriores) na ordem inversa.
3. THE Orquestrador_de_Criacao SHALL manter o mesmo comportamento de rollback best-effort existente (continuar tentando excluir demais recursos mesmo se uma exclusão falhar).

