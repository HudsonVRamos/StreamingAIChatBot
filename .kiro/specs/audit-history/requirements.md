# Documento de Requisitos — Histórico de Auditoria (audit-history)

## Introdução

Esta funcionalidade permite que o usuário consulte o histórico de mudanças realizadas em recursos de streaming (canais, endpoints, distribuições) através do chatbot. O sistema consulta os logs de auditoria armazenados no S3_Audit (bucket de auditoria) e apresenta uma timeline ordenada das operações realizadas sobre um recurso específico. O acesso é feito via `/gerenciarRecurso` com `acao=historico`, reutilizando o path existente na OpenAPI e evitando o limite de 9 paths.

## Glossário

- **Lambda_Configuradora**: Função Lambda responsável por criar, modificar, excluir e gerenciar recursos de streaming. Já armazena logs de auditoria no S3_Audit.
- **S3_Audit**: Bucket S3 que armazena os logs de auditoria com prefixo `audit/` e estrutura de chave `audit/YYYY/MM/DD/YYYYMMDDTHHMMSSz-{operation_id}.json`.
- **Entrada_Auditoria**: Objeto JSON armazenado no S3_Audit contendo os campos: `timestamp`, `usuario_id`, `tipo_operacao`, `servico_aws`, `tipo_recurso`, `resource_id`, `configuracao_json_aplicada`, `resultado`, `erro`, `rollback_info`.
- **Timeline**: Lista ordenada de Entrada_Auditoria filtrada por recurso e período, apresentada do mais recente para o mais antigo.
- **Consultor_Historico**: Módulo dentro da Lambda_Configuradora responsável por listar, filtrar e formatar os logs de auditoria do S3_Audit.
- **Periodo_Consulta**: Intervalo de tempo em dias para a busca de logs. Padrão: 7 dias.
- **Agente_Bedrock**: Agente de IA que interpreta perguntas do usuário em linguagem natural e roteia para a ação correta na Lambda_Configuradora.

## Requisitos

### Requisito 1: Roteamento da ação historico

**User Story:** Como operador de streaming, eu quero consultar o histórico de mudanças de um recurso pelo chatbot, para que eu possa entender o que foi alterado recentemente.

#### Critérios de Aceitação

1. WHEN a Lambda_Configuradora receber uma requisição em `/gerenciarRecurso` com `acao=historico`, THE Lambda_Configuradora SHALL rotear a requisição para o Consultor_Historico.
2. WHEN o parâmetro `acao` for `historico` e o parâmetro `resource_id` estiver ausente, THE Lambda_Configuradora SHALL retornar erro 400 com a mensagem "Para acao=historico, parâmetro obrigatório: resource_id".
3. THE Lambda_Configuradora SHALL aceitar o parâmetro opcional `periodo_dias` com valor padrão de 7 dias para a ação `historico`.
4. THE Lambda_Configuradora SHALL aceitar o parâmetro opcional `tipo_operacao` para filtrar por tipo de operação (criacao, modificacao, exclusao, criacao_orquestrada, rollback, criacao_template).

### Requisito 2: Listagem de arquivos de auditoria no S3

**User Story:** Como operador de streaming, eu quero que o sistema busque todos os logs de auditoria dentro do período solicitado, para que nenhuma mudança relevante seja omitida.

#### Critérios de Aceitação

1. WHEN o Consultor_Historico receber uma consulta com `periodo_dias=N`, THE Consultor_Historico SHALL listar todos os objetos no S3_Audit com prefixo `audit/YYYY/MM/DD/` para cada dia dentro dos últimos N dias (inclusive o dia atual).
2. WHEN o S3_Audit contiver mais de 1000 objetos em um prefixo de data, THE Consultor_Historico SHALL utilizar paginação (continuation token) para listar todos os objetos.
3. IF o S3_Audit retornar um erro de acesso, THEN THE Lambda_Configuradora SHALL retornar erro 500 com a mensagem "Erro ao acessar logs de auditoria: {detalhes}".

### Requisito 3: Filtragem por recurso

**User Story:** Como operador de streaming, eu quero filtrar o histórico por um recurso específico (por nome ou ID), para que eu veja apenas as mudanças relevantes ao recurso que me interessa.

#### Critérios de Aceitação

1. WHEN o Consultor_Historico receber um `resource_id`, THE Consultor_Historico SHALL filtrar as Entrada_Auditoria onde o campo `resource_id` contenha o valor informado (busca parcial, case-insensitive).
2. WHEN o campo `resource_id` da Entrada_Auditoria não corresponder ao filtro, THE Consultor_Historico SHALL verificar se o campo `configuracao_json_aplicada` contém o valor informado em qualquer campo de nome (Name, Id, ChannelName, nome_canal).
3. WHEN nenhuma Entrada_Auditoria corresponder ao filtro, THE Consultor_Historico SHALL retornar uma resposta com lista vazia e a mensagem "Nenhuma alteração encontrada para '{resource_id}' nos últimos {periodo_dias} dias."

### Requisito 4: Filtragem por tipo de operação

**User Story:** Como operador de streaming, eu quero filtrar o histórico por tipo de operação, para que eu possa focar apenas em criações, modificações ou exclusões.

#### Critérios de Aceitação

1. WHERE o parâmetro `tipo_operacao` for fornecido, THE Consultor_Historico SHALL filtrar as Entrada_Auditoria onde o campo `tipo_operacao` corresponda ao valor informado (case-insensitive).
2. WHERE o parâmetro `tipo_operacao` não for fornecido, THE Consultor_Historico SHALL retornar todas as Entrada_Auditoria que correspondam ao filtro de recurso, sem filtrar por tipo de operação.

### Requisito 5: Ordenação e formatação da timeline

**User Story:** Como operador de streaming, eu quero ver as mudanças em ordem cronológica reversa (mais recente primeiro), para que eu identifique rapidamente as últimas alterações.

#### Critérios de Aceitação

1. THE Consultor_Historico SHALL ordenar as Entrada_Auditoria pelo campo `timestamp` em ordem decrescente (mais recente primeiro).
2. THE Consultor_Historico SHALL formatar cada Entrada_Auditoria na timeline com os campos: `data_hora` (timestamp formatado em pt-BR), `operacao` (tipo_operacao), `servico` (servico_aws), `recurso` (resource_id), `resultado` (sucesso/falha), `usuario` (usuario_id), `detalhes` (resumo da configuracao_json_aplicada).
3. THE Consultor_Historico SHALL limitar a resposta a no máximo 50 entradas para evitar respostas excessivamente longas.
4. WHEN a timeline contiver mais de 50 entradas, THE Consultor_Historico SHALL incluir na resposta o campo `total_encontrado` com o número total e a mensagem "Exibindo as 50 alterações mais recentes de {total} encontradas."

### Requisito 6: Resposta estruturada

**User Story:** Como operador de streaming, eu quero receber uma resposta clara e estruturada com o histórico, para que eu possa entender rapidamente o que aconteceu.

#### Critérios de Aceitação

1. THE Lambda_Configuradora SHALL retornar a resposta no formato: `mensagem` (resumo textual), `recurso` (identificador consultado), `periodo` (período consultado), `total_encontrado` (quantidade total de entradas), `timeline` (lista de entradas formatadas).
2. WHEN a timeline contiver entradas com `resultado=falha`, THE Consultor_Historico SHALL incluir o campo `erro` com o código e mensagem de erro da Entrada_Auditoria original.
3. WHEN a timeline contiver entradas com `rollback_info` não nulo, THE Consultor_Historico SHALL incluir o campo `rollback` com os detalhes do rollback.

### Requisito 7: Integração com OpenAPI e Agente Bedrock

**User Story:** Como operador de streaming, eu quero perguntar em linguagem natural sobre o histórico de mudanças, para que eu não precise conhecer a API diretamente.

#### Critérios de Aceitação

1. THE OpenAPI SHALL incluir `historico` na lista de valores do enum `acao` do path `/gerenciarRecurso`.
2. THE OpenAPI SHALL incluir os parâmetros `periodo_dias` (integer, padrão 7) e `tipo_operacao` (string, opcional) no schema de `/gerenciarRecurso`.
3. THE Agente_Bedrock SHALL incluir uma rota de prioridade para perguntas sobre histórico de mudanças com palavras-chave: "histórico", "mudanças", "alterações", "o que mudou", "quem alterou", "auditoria".
4. WHEN o usuário perguntar "O que mudou no canal X nos últimos Y dias?", THE Agente_Bedrock SHALL extrair o resource_id (X) e periodo_dias (Y) e invocar `/gerenciarRecurso` com `acao=historico`.

### Requisito 8: Permissão de leitura no S3_Audit

**User Story:** Como administrador da infraestrutura, eu quero que a Lambda_Configuradora tenha permissão de leitura no bucket de auditoria, para que a consulta de histórico funcione corretamente.

#### Critérios de Aceitação

1. THE Lambda_Configuradora SHALL ter permissão `s3:GetObject` e `s3:ListBucket` no S3_Audit para o prefixo `audit/`.
2. THE CDK Stack SHALL conceder `grant_read` no S3_Audit para a Lambda_Configuradora (atualmente possui apenas `grant_put`).
