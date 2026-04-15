# Requisitos — Migração de S3 para DynamoDB

## Introdução

A plataforma de streaming chatbot atualmente usa buckets S3 (KB_CONFIG e KB_LOGS) como "banco de dados" para configurações de canais e logs de métricas. A Exportadora lista e lê milhares de arquivos JSON individuais do S3 para cada consulta, resultando em latências de 30-120s. Esta migração adiciona DynamoDB como camada de consulta rápida, mantendo o S3 como backup/archive e fonte para o Bedrock Knowledge Base (RAG).

## Glossário

- **KB_CONFIG**: Bucket S3 que armazena configurações enriquecidas dos recursos de streaming (~220 canais MediaLive, ~30 MediaPackage, ~15 MediaTailor, ~20 CloudFront). Atualizado a cada 6h pela Pipeline_Config.
- **KB_LOGS**: Bucket S3 que armazena eventos estruturados (Evento_Estruturado) com métricas classificadas por severidade. Atualizado a cada 1h pela Pipeline_Logs. Gera ~58k arquivos/dia.
- **Pipeline_Config**: Lambda que extrai configs dos serviços AWS e grava no KB_CONFIG a cada 6h.
- **Pipeline_Logs**: Lambda que coleta métricas do CloudWatch, classifica severidade e grava no KB_LOGS a cada 1h.
- **Lambda_Exportadora**: Lambda que consulta KB_CONFIG e KB_LOGS para exportar dados filtrados em CSV/JSON.
- **Config_Enriquecida**: Registro JSON normalizado de configuração de um recurso (channel_id, nome_canal, servico, tipo, estado, etc.).
- **Evento_Estruturado**: Registro JSON normalizado de um evento de métrica (timestamp, canal, severidade, tipo_erro, metrica_nome, metrica_valor, etc.).
- **Dual-write**: Estratégia onde as pipelines gravam simultaneamente no S3 (para RAG/backup) e no DynamoDB (para consultas rápidas).
- **TTL**: Time-To-Live do DynamoDB — expiração automática de itens antigos.

## Requisitos

### 1. Tabelas DynamoDB

#### 1.1 Tabela StreamingConfigs
1. THE CDK Stack SHALL criar uma tabela DynamoDB `StreamingConfigs` com billing mode PAY_PER_REQUEST (on-demand).
2. THE tabela SHALL ter partition key `PK` (String) no formato `{servico}#{tipo_recurso}` (ex: `MediaLive#channel`).
3. THE tabela SHALL ter sort key `SK` (String) no formato `{resource_id}` (ex: `1234567` para MediaLive, `VRIO_CHANNELS/CANAL` para MPV2).
4. THE tabela SHALL ter um GSI `GSI_NomeCanal` com partition key `servico` (String) e sort key `nome_canal` (String) para buscas por nome.
5. THE tabela SHALL ter encryption at rest habilitado (AWS managed key).
6. THE tabela SHALL ter point-in-time recovery habilitado.

#### 1.2 Tabela StreamingLogs
7. THE CDK Stack SHALL criar uma tabela DynamoDB `StreamingLogs` com billing mode PAY_PER_REQUEST.
8. THE tabela SHALL ter partition key `PK` (String) no formato `{servico}#{canal}` (ex: `MediaLive#0001_WARNER_CHANNEL`).
9. THE tabela SHALL ter sort key `SK` (String) no formato `{timestamp}#{metrica_nome}` (ex: `2024-01-15T10:30:00Z#ActiveAlerts`).
10. THE tabela SHALL ter um GSI `GSI_Severidade` com partition key `severidade` (String) e sort key `SK` (String) para filtrar por severidade.
11. THE tabela SHALL ter TTL habilitado no atributo `ttl` com expiração de 30 dias.
12. THE tabela SHALL ter encryption at rest habilitado.

### 2. Dual-Write nas Pipelines

#### 2.1 Pipeline_Config
13. THE Pipeline_Config SHALL gravar cada Config_Enriquecida no DynamoDB `StreamingConfigs` além do S3.
14. THE gravação no DynamoDB SHALL usar `put_item` com o registro completo como atributos.
15. THE Pipeline_Config SHALL incluir os campos `servico`, `nome_canal`, `channel_id`, `tipo`, `estado` como atributos de nível superior para indexação.
16. THE falha na gravação DynamoDB SHALL ser logada mas NÃO SHALL interromper a gravação no S3 (fail-open).

#### 2.2 Pipeline_Logs
17. THE Pipeline_Logs SHALL gravar cada Evento_Estruturado no DynamoDB `StreamingLogs` além do S3.
18. THE gravação SHALL incluir um atributo `ttl` com valor epoch timestamp de 30 dias no futuro.
19. THE Pipeline_Logs SHALL incluir os campos `canal`, `severidade`, `tipo_erro`, `servico_origem`, `metrica_nome`, `metrica_valor` como atributos de nível superior.
20. THE falha na gravação DynamoDB SHALL ser logada mas NÃO SHALL interromper a gravação no S3 (fail-open).

### 3. Consultas na Exportadora via DynamoDB

#### 3.1 Consulta de Configurações
21. THE Lambda_Exportadora SHALL consultar a tabela `StreamingConfigs` em vez de listar/ler arquivos do S3 KB_CONFIG.
22. THE consulta por serviço SHALL usar Query com PK = `{servico}#{tipo_recurso}`.
23. THE consulta por nome de canal SHALL usar o GSI `GSI_NomeCanal` com `begins_with` ou `contains` no nome.
24. THE consulta sem filtros SHALL usar Scan com paginação.
25. THE Exportadora SHALL manter fallback para S3 caso a tabela DynamoDB esteja indisponível.

#### 3.2 Consulta de Logs
26. THE Lambda_Exportadora SHALL consultar a tabela `StreamingLogs` em vez de listar/ler arquivos do S3 KB_LOGS.
27. THE consulta por canal + período SHALL usar Query com PK = `{servico}#{canal}` e SK between `{inicio}` e `{fim}`.
28. THE consulta por severidade SHALL usar o GSI `GSI_Severidade` com PK = `{severidade}` e SK range para período.
29. THE consulta por período sem canal SHALL usar Scan com FilterExpression no SK.
30. THE Exportadora SHALL manter fallback para S3 caso a tabela DynamoDB esteja indisponível.

### 4. Permissões IAM

31. THE Pipeline_Config Lambda SHALL ter permissão `dynamodb:PutItem` na tabela `StreamingConfigs`.
32. THE Pipeline_Logs Lambda SHALL ter permissão `dynamodb:PutItem` na tabela `StreamingLogs`.
33. THE Lambda_Exportadora SHALL ter permissões `dynamodb:Query`, `dynamodb:Scan`, `dynamodb:GetItem` em ambas as tabelas e seus GSIs.
34. THE Lambda_Configuradora SHALL ter permissão `dynamodb:Query`, `dynamodb:GetItem` na tabela `StreamingConfigs` para buscas rápidas de recursos.

### 5. Migração de Dados Existentes

35. THE sistema SHALL incluir um script de migração one-time que lê todos os arquivos JSON do S3 KB_CONFIG e KB_LOGS e os grava nas tabelas DynamoDB correspondentes.
36. THE script de migração SHALL ser idempotente (pode ser executado múltiplas vezes sem duplicar dados).
37. THE script SHALL usar batch_write_item com chunks de 25 itens para eficiência.
38. THE script SHALL logar progresso a cada 100 itens migrados.

### 6. Compatibilidade

39. THE S3 buckets KB_CONFIG e KB_LOGS SHALL continuar existindo e sendo populados (dual-write) para manter o Bedrock Knowledge Base funcional.
40. THE Exportadora SHALL tentar DynamoDB primeiro e fazer fallback para S3 em caso de erro.
41. THE formato de resposta da Exportadora SHALL permanecer idêntico ao atual (mesmos campos, mesma estrutura JSON/CSV).

### 7. Performance

42. THE consulta de configurações por serviço SHALL completar em menos de 2 segundos.
43. THE consulta de logs por canal + período (24h) SHALL completar em menos de 3 segundos.
44. THE consulta de logs por severidade SHALL completar em menos de 5 segundos.
45. THE custo estimado mensal SHALL ser inferior a $10 USD com o volume atual (~220 configs + ~58k logs/dia).
