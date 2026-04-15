# Plano de Implementação: Migração S3 → DynamoDB

## Visão Geral

Adicionar DynamoDB como camada de consulta rápida para o chatbot, com dual-write nas pipelines (S3 + DynamoDB), migração da Exportadora para consultar DynamoDB primeiro com fallback S3, e script de migração de dados existentes.

## Tarefas

- [x] 1. Criar CDK Stack com tabelas DynamoDB
  - [x] 1.1 Criar arquivo `stacks/dynamodb_stack.py`
    - Criar tabela `StreamingConfigs` com PK=`PK` (String), SK=`SK` (String), billing PAY_PER_REQUEST, point-in-time recovery, encryption at rest
    - Adicionar GSI `GSI_NomeCanal` com PK=`servico` (String), SK=`nome_canal` (String)
    - Criar tabela `StreamingLogs` com PK=`PK` (String), SK=`SK` (String), billing PAY_PER_REQUEST, TTL no atributo `ttl`
    - Adicionar GSI `GSI_Severidade` com PK=`severidade` (String), SK=`SK` (String)
    - Expor `self.configs_table` e `self.logs_table` como propriedades
    - _Requisitos: 1.1–1.12_

  - [x] 1.2 Integrar DynamoDB Stack no `stacks/main_stack.py`
    - Instanciar tabelas no main_stack (inline ou via DynamoDBStack)
    - Passar table names como variáveis de ambiente para Pipeline_Config, Pipeline_Logs e Exportadora
    - Adicionar CfnOutput para os nomes das tabelas
    - _Requisitos: 1.1, 1.7_

  - [x] 1.3 Configurar permissões IAM
    - Pipeline_Config: `dynamodb:PutItem` na tabela StreamingConfigs
    - Pipeline_Logs: `dynamodb:PutItem` na tabela StreamingLogs
    - Exportadora: `dynamodb:Query`, `dynamodb:Scan`, `dynamodb:GetItem` em ambas as tabelas e GSIs
    - Configuradora: `dynamodb:Query`, `dynamodb:GetItem` na tabela StreamingConfigs
    - _Requisitos: 4.1–4.4_

- [x] 2. Implementar dual-write na Pipeline_Config
  - [x] 2.1 Adicionar função `_write_config_to_dynamodb()` em `lambdas/pipeline_config/handler.py`
    - Criar cliente DynamoDB com adaptive retry
    - Transformar Config_Enriquecida no formato DynamoDB: PK=`{servico}#{tipo}`, SK=`{resource_id}`
    - Incluir campos de nível superior: servico, nome_canal, channel_id, tipo, estado, regiao
    - Gravar registro completo no campo `data` como JSON string
    - Implementar fail-open: try/except com log de erro, sem interromper fluxo S3
    - _Requisitos: 2.1–2.4, 6.1_

  - [x] 2.2 Integrar dual-write no fluxo existente de `_parallel_store()`
    - Após gravação bem-sucedida no S3, chamar `_write_config_to_dynamodb()`
    - Ler table name da variável de ambiente `CONFIGS_TABLE_NAME`
    - _Requisitos: 2.1, 2.4_

- [ ] 3. Implementar dual-write na Pipeline_Logs
  - [x] 3.1 Adicionar função `_write_log_to_dynamodb()` em `lambdas/pipeline_logs/handler.py`
    - Criar cliente DynamoDB com adaptive retry
    - Transformar Evento_Estruturado no formato DynamoDB: PK=`{servico}#{canal}`, SK=`{timestamp}#{metrica_nome}`
    - Incluir campos de nível superior: severidade, tipo_erro, canal, servico_origem, metrica_nome, metrica_valor
    - Calcular TTL: epoch timestamp de 30 dias no futuro
    - Gravar registro completo no campo `data` como JSON string
    - Implementar fail-open
    - _Requisitos: 2.5–2.8, 6.1_

  - [x] 3.2 Integrar dual-write no fluxo existente de gravação de eventos
    - Após gravação bem-sucedida no S3, chamar `_write_log_to_dynamodb()`
    - Ler table name da variável de ambiente `LOGS_TABLE_NAME`
    - _Requisitos: 2.5, 2.8_

- [ ] 4. Checkpoint — Deploy e verificar dual-write
  - Deploy CDK para criar tabelas
  - Executar pipelines manualmente e verificar dados no DynamoDB
  - Verificar que S3 continua sendo populado normalmente

- [ ] 5. Migrar dados existentes do S3 para DynamoDB
  - [x] 5.1 Criar script `scripts/migrate_s3_to_dynamodb.py`
    - Listar todos os JSONs no KB_CONFIG bucket (prefixo kb-config/)
    - Ler cada arquivo, transformar no formato DynamoDB, gravar com batch_write_item (25 itens/batch)
    - Listar JSONs no KB_LOGS bucket (prefixo kb-logs/) — limitar aos últimos 30 dias
    - Transformar e gravar com batch_write_item
    - Logar progresso a cada 100 itens
    - Implementar idempotência (put_item sobrescreve se já existe)
    - _Requisitos: 5.1–5.4_

  - [ ] 5.2 Executar migração
    - Rodar script localmente com credenciais AWS
    - Verificar contagem de itens nas tabelas

- [ ] 6. Migrar Exportadora para consultar DynamoDB
  - [x] 6.1 Criar funções `query_dynamodb_configs()` e `query_dynamodb_logs()` em `lambdas/exportadora/handler.py`
    - `query_dynamodb_configs(table_name, filtros)`: Query por PK quando servico presente, GSI_NomeCanal para busca por nome, Scan para listagem completa
    - `query_dynamodb_logs(table_name, filtros)`: Query por PK+SK range para canal+período, GSI_Severidade para filtro por severidade, Scan com FilterExpression para outros casos
    - Implementar paginação (LastEvaluatedKey) para resultados grandes
    - Deserializar campo `data` de cada item para retornar registros completos
    - _Requisitos: 3.1–3.4_

  - [x] 6.2 Alterar `query_s3_data()` para usar DynamoDB com fallback S3
    - Renomear `query_s3_data()` atual para `_query_s3_data_legacy()`
    - Criar nova `query_s3_data()` que tenta DynamoDB primeiro
    - Se DynamoDB falhar (exception), logar warning e chamar `_query_s3_data_legacy()`
    - Ler table names das variáveis de ambiente `CONFIGS_TABLE_NAME` e `LOGS_TABLE_NAME`
    - Determinar qual tabela usar baseado no bucket/prefix passado
    - _Requisitos: 3.5, 3.6, 6.2, 6.3_

  - [x] 6.3 Verificar que formato de resposta permanece idêntico
    - Os registros retornados do DynamoDB (campo `data`) devem ter a mesma estrutura dos JSONs do S3
    - Testar com exportação CSV e JSON para garantir compatibilidade
    - _Requisitos: 6.3_

- [ ] 7. Checkpoint — Testar consultas end-to-end
  - Testar via chatbot: "Quantos canais MediaLive existem?"
  - Testar via chatbot: "Quais canais tiveram alertas nas últimas 24 horas?"
  - Testar via chatbot: "Exportar canais Globo em CSV"
  - Verificar que latência caiu significativamente
  - Verificar que fallback S3 funciona (desabilitar DynamoDB temporariamente)

- [x] 8. Otimizar Configuradora para usar DynamoDB (opcional)
  - [x] 8.1 Adicionar busca rápida de recursos via DynamoDB na Configuradora
    - Para `obterConfiguracao` com busca parcial por nome, usar GSI_NomeCanal com begins_with
    - Para `list_resources()`, usar Query por PK em vez de chamadas API AWS
    - Manter chamadas API AWS como fallback para dados em tempo real
    - _Requisitos: 4.4_

- [x] 9. Checkpoint final
  - Verificar performance de todas as consultas
  - Verificar custos no AWS Cost Explorer após 24h
  - Verificar que Bedrock Knowledge Base continua funcional (S3 inalterado)

## Notas

- O S3 continua como fonte de verdade para o Bedrock Knowledge Base (RAG)
- O DynamoDB é a fonte primária para consultas diretas (Exportadora, Configuradora)
- A migração é incremental: cada etapa pode ser deployada independentemente
- O fallback S3 garante que nada quebra se o DynamoDB tiver problemas
- Custo estimado: ~$3/mês com o volume atual
