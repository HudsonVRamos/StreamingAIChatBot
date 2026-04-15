# Plano de Implementação: Comparação de Canais

## Visão Geral

Adicionar o endpoint `/compararRecursos` à Lambda_Configuradora para comparar lado a lado as configurações de dois recursos de streaming. Implementar a função pura `compare_configs()` com comparação recursiva campo a campo, atualizar o schema OpenAPI do Action_Group_Config, adicionar sugestões de comparação no frontend e criar testes unitários e de propriedade.

## Tarefas

- [x] 1. Implementar a função pura de comparação e constantes
  - [x] 1.1 Criar o set `_COMPARISON_IGNORE_FIELDS` e a função `_strip_comparison_fields(config)`
    - Definir em `lambdas/configuradora/handler.py` o set com campos read-only: Arn, Id, ChannelId, State, Tags, ResponseMetadata, PipelinesRunningCount, EgressEndpoints, Maintenance, ETag, CreatedAt, ModifiedAt
    - Implementar `_strip_comparison_fields(config)` que remove recursivamente esses campos de um dict
    - _Requisitos: 2.6_

  - [x] 1.2 Implementar a função `compare_configs(config_a, config_b, path="")`
    - Função pura recursiva que recebe dois dicts e retorna `{"campos_iguais": [...], "campos_diferentes": [...], "campos_exclusivos": [...]}`
    - Se ambos os valores são `dict`: recursar com path atualizado
    - Se ambos os valores são `list`: comparar por índice, recursar em elementos dict
    - Se valores escalares iguais: adicionar a `campos_iguais`
    - Se valores escalares diferentes: adicionar a `campos_diferentes` com `valor_recurso_1` e `valor_recurso_2`
    - Se chave existe em apenas um: adicionar a `campos_exclusivos` com `presente_em`
    - _Requisitos: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 1.3 Implementar o dicionário `CATEGORY_PREFIXES` e a função `_build_comparison_summary(result, name_1, name_2)`
    - Definir `CATEGORY_PREFIXES` mapeando categorias (vídeo, áudio, legendas, outputs, inputs, drm, failover, rede) para prefixos de campos
    - Implementar `_build_comparison_summary()` que gera o `resumo_textual` em português agrupando diferenças por categoria
    - _Requisitos: 5.2, 5.4_

  - [x] 1.4 Implementar lógica de truncamento a 50 diferenças
    - Quando `campos_diferentes` exceder 50 entradas, truncar a lista mantendo os 50 primeiros
    - Manter `total_campos_diferentes` com o valor real (não truncado)
    - _Requisitos: 5.3_

- [ ] 2. Testes da função de comparação
  - [ ]* 2.1 Escrever teste de propriedade para particionamento completo dos campos
    - **Propriedade 1: Particionamento completo dos campos**
    - Usar `st.recursive()` para gerar dicts aninhados com profundidade variável
    - Verificar que todo caminho folha presente em `config_a` ou `config_b` aparece em exatamente uma das três categorias
    - **Valida: Requisitos 1.2, 2.1, 2.2, 2.3, 2.4, 2.5**

  - [ ]* 2.2 Escrever teste de propriedade para exclusão de campos ignorados
    - **Propriedade 2: Exclusão de campos ignorados**
    - Gerar dicts com campos de `_COMPARISON_IGNORE_FIELDS` injetados aleatoriamente
    - Verificar que nenhum campo ignorado aparece em qualquer categoria da saída após strip
    - **Valida: Requisitos 2.6**

  - [ ]* 2.3 Escrever teste de propriedade para consistência dos contadores
    - **Propriedade 3: Consistência estrutural dos contadores**
    - Verificar que `total_campos_iguais == len(campos_iguais)`, `total_campos_diferentes == len(campos_diferentes)` (ou total real quando truncado), `total_campos_exclusivos == len(campos_exclusivos)`
    - **Valida: Requisitos 5.1, 5.2**

  - [ ]* 2.4 Escrever teste de propriedade para truncamento a 50 diferenças
    - **Propriedade 4: Truncamento a 50 diferenças**
    - Gerar dois dicts com mais de 50 campos diferentes
    - Verificar que `campos_diferentes` tem no máximo 50 entradas e `total_campos_diferentes` reflete o total real
    - **Valida: Requisitos 5.3**

  - [ ]* 2.5 Escrever testes unitários para `compare_configs()` e `_strip_comparison_fields()`
    - Testar dicts idênticos: `campos_diferentes` e `campos_exclusivos` vazios
    - Testar dicts completamente diferentes: `campos_iguais` vazio
    - Testar listas de tamanhos diferentes: elementos extras em `campos_exclusivos`
    - Testar campos aninhados profundos (3+ níveis)
    - Testar remoção de campos read-only
    - _Requisitos: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

- [ ] 3. Checkpoint — Verificar testes da função de comparação
  - Garantir que todos os testes passam, perguntar ao usuário se houver dúvidas.

- [x] 4. Implementar a rota `/compararRecursos` no handler
  - [x] 4.1 Adicionar o bloco `if api_path == "/compararRecursos":` no `handler()` de `lambdas/configuradora/handler.py`
    - Extrair parâmetros: `resource_id_1`, `resource_id_2`, `servico` (default "MediaLive"), `tipo_recurso` (default "channel")
    - Validar que `resource_id_1` e `resource_id_2` estão presentes
    - Validar que `servico` está em `VALID_SERVICOS` e `tipo_recurso` em `VALID_TIPOS_RECURSO[servico]`
    - _Requisitos: 1.1, 1.3, 1.4, 7.1, 7.2_

  - [x] 4.2 Implementar busca de configurações e tratamento de desambiguação
    - Chamar `get_full_config()` para cada recurso
    - Se algum retornar `multiplos_resultados`, retornar candidatos indicando qual recurso precisa desambiguação
    - Se tipos de recurso forem incompatíveis, retornar erro descritivo
    - _Requisitos: 1.1, 1.5, 7.3_

  - [x] 4.3 Integrar comparação, resumo, truncamento e resposta
    - Chamar `_strip_comparison_fields()` em ambas as configurações
    - Chamar `compare_configs()` com os dicts limpos
    - Chamar `_build_comparison_summary()` para gerar resumo textual
    - Aplicar truncamento a 50 diferenças se necessário
    - Montar resposta `Comparação_Estruturada` com nomes, serviço, contadores e resumo
    - Registrar audit log via `build_audit_log()` + `store_audit_log()`
    - Retornar via `_bedrock_response()`
    - _Requisitos: 1.1, 1.2, 1.6, 5.1, 5.2, 5.3, 5.4_

  - [ ]* 4.4 Escrever testes unitários para a rota `/compararRecursos`
    - Mock de `get_full_config()` para retornar configurações de teste
    - Testar desambiguação (mock retornando `multiplos_resultados`)
    - Testar erro AWS (mock levantando `ClientError`)
    - Testar tipos incompatíveis (MediaLive/channel vs MediaPackage/origin_endpoint)
    - Testar serviço padrão quando `servico` não fornecido
    - _Requisitos: 1.1, 1.3, 1.5, 1.6, 7.1, 7.2, 7.3_

- [x] 5. Atualizar schema OpenAPI do Action_Group_Config
  - [x] 5.1 Adicionar o path `/compararRecursos` ao `Help/openapi-config-v2.json`
    - Parâmetros: `resource_id_1` (string, obrigatório), `resource_id_2` (string, obrigatório), `servico` (string, opcional, enum dos 4 serviços), `tipo_recurso` (string, opcional)
    - Descrição em português indicando que aceita nomes parciais ou IDs numéricos
    - Verificar que o total de paths não excede 9 (6 existentes + 1 novo = 7)
    - _Requisitos: 3.1, 3.2, 3.3_

- [ ] 6. Checkpoint — Verificar integração handler + schema
  - Garantir que todos os testes passam, perguntar ao usuário se houver dúvidas.

- [x] 7. Atualizar frontend com sugestões de comparação
  - [x] 7.1 Adicionar seção "🔀 Comparar" na sidebar do `frontend/chat.html`
    - Inserir nova seção entre as seções existentes com `sidebar-title` "🔀 Comparar"
    - Adicionar botões `suggestion-btn` com textos de comparação comuns (ex: "Compare o canal WARNER com o canal ESPN", "Compare os endpoints HLS do WARNER e do ESPN")
    - Seguir o mesmo padrão de `onclick="useSuggestion(this)"` dos botões existentes
    - _Requisitos: 6.1, 6.2_

- [x] 8. Atualizar prompt do Agente Bedrock
  - [x] 8.1 Adicionar rota de prioridade para comparação no prompt do agente
    - Incluir palavras-chave: "comparar", "compare", "diferença entre", "diff", "versus", "vs"
    - Instruir o agente a extrair `resource_id_1`, `resource_id_2` e `servico` da mensagem
    - Instruir o agente a formatar a resposta como tabela legível em português
    - Instruir o agente a solicitar desambiguação quando `multiplos_resultados` for retornado
    - _Requisitos: 4.1, 4.2, 4.3, 4.4_

- [ ] 9. Checkpoint final — Verificar todos os testes e integração
  - Garantir que todos os testes passam, perguntar ao usuário se houver dúvidas.

## Notas

- Tarefas marcadas com `*` são opcionais e podem ser puladas para um MVP mais rápido
- Cada tarefa referencia requisitos específicos para rastreabilidade
- Checkpoints garantem validação incremental
- Testes de propriedade validam propriedades universais de corretude
- Testes unitários validam exemplos específicos e edge cases
