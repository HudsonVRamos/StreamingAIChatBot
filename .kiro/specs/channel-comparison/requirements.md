# Documento de Requisitos — Comparação de Canais

## Introdução

Esta funcionalidade adiciona ao Streaming Chatbot a capacidade de comparar lado a lado as configurações de dois recursos de streaming. Quando o usuário solicita uma comparação (ex: "Compare a configuração do canal WARNER com o canal ESPN"), o sistema busca a configuração completa de ambos os recursos, executa uma comparação campo a campo e retorna um resultado estruturado destacando campos iguais, campos diferentes (com valores de ambos) e campos presentes em apenas um dos recursos. A comparação suporta MediaLive channels, MediaPackage V2 endpoints, MediaTailor playback configurations e CloudFront distributions. O resultado é apresentado em formato de tabela legível no chat.

## Glossário

- **Lambda_Configuradora**: Função Lambda existente que executa operações de leitura e escrita nos serviços de streaming AWS (MediaLive, MediaPackage V2, MediaTailor, CloudFront). Já possui a função `get_full_config()` para buscar configurações completas.
- **Action_Group_Config**: Action Group do Bedrock Agent que invoca a Lambda_Configuradora. Possui schema OpenAPI com 6 paths atualmente (limite de 11 total no agente, 2 usados pelo Action_Group_Export).
- **Agente_Bedrock**: Agente Amazon Bedrock com modelo Amazon Nova Pro que classifica intenções e roteia para Knowledge Bases ou Action Groups.
- **Comparação_Estruturada**: Resultado da comparação contendo três seções: campos iguais, campos diferentes (com valor de cada recurso) e campos exclusivos (presentes em apenas um recurso).
- **Busca_Fuzzy**: Mecanismo existente de resolução de nomes por substring case-insensitive, usado para localizar recursos por nome parcial.
- **Recurso_Streaming**: Qualquer recurso gerenciado pelo chatbot: canal MediaLive, endpoint MediaPackage V2, configuração MediaTailor ou distribuição CloudFront.
- **Frontend_Chat**: Interface web (chat.html) com tema escuro, sidebar de sugestões e área de chat conversacional.

## Requisitos

### Requisito 1: Endpoint de Comparação na Lambda_Configuradora

**User Story:** Como operador de NOC, eu quero solicitar a comparação de dois recursos de streaming pelo chat, para que eu possa identificar rapidamente diferenças de configuração entre eles.

#### Critérios de Aceitação

1. WHEN o Agente_Bedrock envia uma requisição para o path `/compararRecursos` com `resource_id_1`, `resource_id_2` e `servico`, THE Lambda_Configuradora SHALL buscar a configuração completa de ambos os recursos usando a função `get_full_config()` existente.
2. WHEN ambas as configurações são obtidas com sucesso, THE Lambda_Configuradora SHALL executar uma comparação campo a campo recursiva e retornar uma Comparação_Estruturada contendo: campos iguais (`campos_iguais`), campos diferentes com valores de ambos (`campos_diferentes`), e campos presentes em apenas um recurso (`campos_exclusivos`).
3. WHEN o parâmetro `servico` não é fornecido, THE Lambda_Configuradora SHALL assumir `MediaLive` como serviço padrão e `channel` como tipo de recurso padrão.
4. WHEN o parâmetro `tipo_recurso` é fornecido, THE Lambda_Configuradora SHALL usar o tipo de recurso especificado para buscar as configurações.
5. IF um dos `resource_id` não for encontrado ou a busca fuzzy retornar múltiplos candidatos para um dos recursos, THEN THE Lambda_Configuradora SHALL retornar uma resposta com `multiplos_resultados: true` e a lista de candidatos, indicando qual dos dois recursos precisa de desambiguação.
6. IF ocorrer um erro na API AWS ao buscar qualquer uma das configurações, THEN THE Lambda_Configuradora SHALL retornar uma mensagem de erro descritiva indicando qual recurso falhou e o código de erro AWS.

### Requisito 2: Algoritmo de Comparação Campo a Campo

**User Story:** Como engenheiro de streaming, eu quero que a comparação identifique diferenças em todos os níveis de aninhamento do JSON, para que eu possa ver diferenças em codec, bitrate, resolução, DRM, outputs, áudios, legendas, failover e inputs.

#### Critérios de Aceitação

1. THE Lambda_Configuradora SHALL comparar recursivamente todos os campos aninhados dos dois JSONs de configuração, percorrendo dicionários em profundidade.
2. WHEN dois campos do mesmo caminho possuem valores escalares diferentes, THE Lambda_Configuradora SHALL incluir o caminho completo do campo (ex: `EncoderSettings.VideoDescriptions[0].CodecSettings.H264Settings.Bitrate`) na seção `campos_diferentes` com o valor de cada recurso.
3. WHEN dois campos do mesmo caminho possuem valores escalares idênticos, THE Lambda_Configuradora SHALL incluir o caminho do campo na seção `campos_iguais`.
4. WHEN um campo existe em apenas um dos recursos, THE Lambda_Configuradora SHALL incluir o caminho do campo na seção `campos_exclusivos` indicando em qual recurso o campo está presente.
5. WHEN dois campos do mesmo caminho são listas, THE Lambda_Configuradora SHALL comparar os elementos por posição (índice) e reportar diferenças por elemento.
6. THE Lambda_Configuradora SHALL ignorar campos de metadados somente-leitura (Arn, Id, State, Tags, ResponseMetadata, PipelinesRunningCount, EgressEndpoints) na comparação, para focar apenas em campos de configuração relevantes.

### Requisito 3: Schema OpenAPI para o Endpoint de Comparação

**User Story:** Como desenvolvedor, eu quero que o endpoint de comparação esteja definido no schema OpenAPI do Action_Group_Config, para que o Agente_Bedrock possa invocar a comparação automaticamente.

#### Critérios de Aceitação

1. THE Action_Group_Config SHALL incluir o path `/compararRecursos` no schema OpenAPI com os parâmetros: `resource_id_1` (string, obrigatório), `resource_id_2` (string, obrigatório), `servico` (string, opcional, enum dos 4 serviços) e `tipo_recurso` (string, opcional).
2. THE schema OpenAPI SHALL conter uma descrição clara do endpoint em português indicando que aceita nomes parciais ou IDs numéricos para ambos os recursos.
3. WHILE o schema OpenAPI do Action_Group_Config possui 6 paths existentes, THE schema SHALL manter o total de paths dentro do limite de 9 (11 total menos 2 do Action_Group_Export) após a adição do novo path.

### Requisito 4: Roteamento do Agente Bedrock para Comparação

**User Story:** Como operador de NOC, eu quero que o chatbot reconheça automaticamente pedidos de comparação em linguagem natural, para que eu possa simplesmente digitar "compare o canal X com o canal Y" e obter o resultado.

#### Critérios de Aceitação

1. WHEN o usuário envia uma mensagem contendo palavras-chave de comparação ("comparar", "compare", "diferença entre", "diff", "versus", "vs"), THE Agente_Bedrock SHALL classificar a intenção como comparação e rotear para o Action_Group_Config com a operação `compararRecursos`.
2. THE Agente_Bedrock SHALL extrair os dois identificadores de recurso da mensagem do usuário e o serviço (se mencionado) para compor os parâmetros da chamada.
3. WHEN o resultado da comparação é recebido, THE Agente_Bedrock SHALL formatar a resposta como uma tabela legível em português, agrupando campos iguais, campos diferentes e campos exclusivos.
4. WHEN a comparação retorna `multiplos_resultados` para um dos recursos, THE Agente_Bedrock SHALL apresentar a lista de candidatos ao usuário e solicitar que escolha qual recurso usar na comparação.

### Requisito 5: Formatação da Resposta de Comparação

**User Story:** Como operador de NOC, eu quero que o resultado da comparação seja apresentado de forma clara e organizada no chat, para que eu possa identificar rapidamente as diferenças relevantes.

#### Critérios de Aceitação

1. THE Lambda_Configuradora SHALL retornar a Comparação_Estruturada com os nomes dos dois recursos comparados, o serviço, e contadores de campos iguais, diferentes e exclusivos.
2. THE Lambda_Configuradora SHALL incluir um campo `resumo_textual` na resposta contendo uma descrição em português das principais diferenças encontradas (codec, bitrate, resolução, DRM, outputs, áudios, legendas, failover, inputs).
3. WHEN a quantidade de campos diferentes exceder 50, THE Lambda_Configuradora SHALL incluir apenas os 50 campos mais relevantes na resposta e indicar o total de diferenças encontradas.
4. THE Lambda_Configuradora SHALL agrupar os campos diferentes por categoria (vídeo, áudio, legendas, outputs, inputs, DRM, failover, rede) no resumo textual para facilitar a leitura.

### Requisito 6: Sugestões de Comparação no Frontend

**User Story:** Como operador de NOC, eu quero ter sugestões de comparação na sidebar do chat, para que eu possa iniciar comparações com um clique.

#### Critérios de Aceitação

1. THE Frontend_Chat SHALL incluir uma nova seção "🔀 Comparar" na sidebar com botões de sugestão para comparações comuns.
2. WHEN o usuário clica em um botão de sugestão de comparação, THE Frontend_Chat SHALL inserir o texto da sugestão no campo de entrada do chat, seguindo o mesmo comportamento dos botões de sugestão existentes.

### Requisito 7: Suporte a Múltiplos Serviços na Comparação

**User Story:** Como engenheiro de streaming, eu quero comparar recursos de qualquer serviço de streaming suportado, para que eu possa analisar diferenças em endpoints MediaPackage, configurações MediaTailor e distribuições CloudFront além de canais MediaLive.

#### Critérios de Aceitação

1. THE Lambda_Configuradora SHALL suportar comparação de recursos dos seguintes serviços e tipos: MediaLive (channel, input), MediaPackage (channel, origin_endpoint), MediaTailor (playback_configuration) e CloudFront (distribution).
2. WHEN os dois recursos pertencem ao mesmo serviço e tipo, THE Lambda_Configuradora SHALL executar a comparação normalmente.
3. IF os dois `resource_id` resolverem para tipos de recurso diferentes (ex: um canal MediaLive e um endpoint MediaPackage), THEN THE Lambda_Configuradora SHALL retornar um erro informando que apenas recursos do mesmo serviço e tipo podem ser comparados.
