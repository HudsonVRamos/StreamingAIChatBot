# Tarefas — Histórico de Auditoria (audit-history)

## Tarefa 1: Implementar funções do Consultor_Historico

- [x] 1.1 Criar função `listar_audit_keys(periodo_dias: int) -> list[str]` em `lambdas/configuradora/handler.py` que gera prefixos de data para os últimos N dias e lista objetos no S3_Audit com paginação (continuation token)
- [x] 1.2 Criar função `carregar_entradas_auditoria(keys: list[str]) -> list[dict]` que faz get_object para cada key, parseia JSON e ignora entradas corrompidas com log de warning
- [x] 1.3 Criar função `filtrar_por_recurso(entradas: list[dict], resource_id: str) -> list[dict]` que filtra entradas por resource_id (busca parcial, case-insensitive) incluindo busca em campos Name/Id/ChannelName/nome_canal dentro de configuracao_json_aplicada
- [x] 1.4 Criar função `filtrar_por_tipo_operacao(entradas: list[dict], tipo_operacao: str) -> list[dict]` que filtra por tipo_operacao (case-insensitive) quando fornecido, retorna todas quando vazio
- [x] 1.5 Criar função `formatar_entrada_timeline(entrada: dict) -> dict` que converte Entrada_Auditoria para formato de timeline com campos: data_hora, operacao, servico, recurso, resultado, usuario, detalhes, e condicionalmente erro e rollback
- [x] 1.6 Criar função `consultar_historico(resource_id: str, periodo_dias: int, tipo_operacao: str) -> dict` que orquestra listagem, carregamento, filtragem, ordenação DESC por timestamp, limite de 50 entradas, e retorna resposta estruturada com mensagem, recurso, periodo, total_encontrado, timeline

## Tarefa 2: Integrar roteamento no handler

- [x] 2.1 Adicionar branch `acao == "historico"` no bloco `/gerenciarRecurso` do handler, antes do fallback de ação inválida, com validação de resource_id obrigatório e tratamento de erro ClientError
- [x] 2.2 Adicionar validação de `periodo_dias` (deve ser inteiro positivo, default 7) com erro 400 para valores inválidos
- [x] 2.3 Atualizar mensagem de erro do fallback de ação inválida para incluir "historico" na lista de ações válidas

## Tarefa 3: Atualizar OpenAPI e CDK

- [x] 3.1 Adicionar `"historico"` ao enum `acao` em `/gerenciarRecurso` no arquivo `Help/openapi-config-v2.json`
- [x] 3.2 Adicionar propriedades `periodo_dias` (integer, default 7) e `tipo_operacao` (string, opcional) ao schema de `/gerenciarRecurso` no arquivo `Help/openapi-config-v2.json`
- [x] 3.3 Atualizar description do endpoint `/gerenciarRecurso` para incluir acao=historico
- [x] 3.4 Adicionar `audit_bucket.grant_read(configuradora_fn)` em `stacks/main_stack.py` após o `grant_put` existente

## Tarefa 4: Testes unitários

- [ ] 4.1 Criar arquivo `tests/test_audit_history.py` com testes unitários para: validação de parâmetros (resource_id ausente → 400), default periodo_dias=7, resposta vazia, erro S3 → 500, JSON corrompido ignorado
- [ ] 4.2 Adicionar testes unitários para formatação de entradas com resultado=falha (campo erro presente) e com rollback_info (campo rollback presente)

## Tarefa 5: Testes de propriedade (Hypothesis)

- [ ] 5.1 [PBT] Propriedade 1 — Geração de prefixos de data: para qualquer periodo_dias (1-365), verificar que listar_audit_keys gera exatamente N prefixos no formato correto sem duplicatas
- [ ] 5.2 [PBT] Propriedade 2 — Filtragem por recurso: para qualquer conjunto de entradas e filtro, todas as entradas retornadas contêm o filtro (case-insensitive) em resource_id ou campos de nome da config
- [ ] 5.3 [PBT] Propriedade 3 — Filtragem por tipo de operação: para qualquer conjunto de entradas e tipo_operacao, todas as entradas retornadas têm tipo_operacao correspondente; sem filtro retorna todas
- [ ] 5.4 [PBT] Propriedade 4 — Ordenação cronológica reversa: para qualquer conjunto de entradas, a timeline está ordenada por timestamp DESC
- [ ] 5.5 [PBT] Propriedade 5 — Formatação completa: para qualquer Entrada_Auditoria válida, a saída contém todos os campos obrigatórios e condicionais (erro quando falha, rollback quando presente)
- [ ] 5.6 [PBT] Propriedade 6 — Truncamento e contagem: para qualquer lista de entradas (0-200), timeline ≤ 50 e total_encontrado é correto
