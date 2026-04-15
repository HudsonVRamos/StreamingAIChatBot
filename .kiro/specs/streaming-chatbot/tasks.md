# Plano de Implementação: Chatbot Inteligente para Gestão de Canais de Streaming

## Visão Geral

Implementação incremental usando AWS CDK (Python) para toda a infraestrutura e Python 3.12 para as funções Lambda. O plano segue uma abordagem bottom-up: primeiro a infraestrutura base (buckets S3, IAM), depois os componentes de backend (Lambdas, API Gateway, Bedrock), pipelines de ingestão, e por último o frontend e integração final.

## Tasks

- [x] 1. Configurar projeto CDK e estrutura base
  - [x] 1.1 Inicializar projeto CDK com Python e criar estrutura de diretórios
    - Criar `cdk.json`, `app.py`, `requirements.txt` com dependências CDK
    - Criar diretórios: `stacks/`, `lambdas/orquestradora/`, `lambdas/configuradora/`, `lambdas/exportadora/`, `lambdas/pipeline_config/`, `lambdas/pipeline_logs/`, `frontend/`, `tests/`
    - _Requisitos: 1.9_

  - [x] 1.2 Criar stack CDK de buckets S3
    - Criar bucket `S3_KBConfig` com prefixo `kb-config/`, Block Public Access, SSE
    - Criar bucket `S3_KBLogs` com prefixo `kb-logs/`, Block Public Access, SSE
    - Criar bucket `S3_Audit` com versionamento habilitado, lifecycle 365 dias, Block Public Access, SSE
    - Criar bucket `S3_Exports` com lifecycle policy de 24h para prefixo `exports/`, Block Public Access, SSE, sem versionamento
    - Criar bucket `S3_Frontend` com Static Website Hosting, Block Public Access
    - _Requisitos: 4.1, 6.1, 10.1, 10.2, 13.5, 14.5, 1.6_

- [x] 2. Checkpoint — Validar infraestrutura base
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Implementar Lambda_Orquestradora e API Gateway
  - [x] 3.1 Implementar código da Lambda_Orquestradora
    - Criar `lambdas/orquestradora/handler.py` com lógica de validação de input, invocação do Bedrock Agent e coleta de resposta
    - Validar campo `pergunta` (string não-vazia, rejeitar null/vazio/espaços)
    - Retornar HTTP 400 para payloads inválidos, HTTP 504 para timeout, HTTP 500 para erros internos
    - Variáveis de ambiente: `AGENT_ID`, `AGENT_ALIAS_ID`
    - _Requisitos: 2.2, 2.3, 2.4, 2.5_

  - [x] 3.2 Escrever teste de propriedade para rejeição de payloads inválidos
    - **Propriedade 3: Rejeição de payloads inválidos**
    - Gerar payloads JSON inválidos (campo ausente, null, string vazia, espaços, tipo incorreto) e verificar retorno HTTP 400
    - **Valida: Requisitos 2.4**

  - [x] 3.3 Escrever testes unitários para Lambda_Orquestradora
    - Testar invocação com pergunta válida retorna HTTP 200
    - Testar tratamento de timeout do Bedrock Agent retorna HTTP 504
    - Testar resposta vazia do agente retorna mensagem informativa
    - _Requisitos: 2.2, 2.3, 2.5_

  - [x] 3.4 Criar stack CDK para API Gateway e Lambda_Orquestradora
    - Criar REST API com endpoint `POST /chat` e Lambda Proxy Integration
    - Configurar CORS para domínio do CloudFront_Frontend
    - Configurar timeout de 29 segundos no API Gateway
    - Criar IAM role para Lambda com permissão `bedrock:InvokeAgent`
    - _Requisitos: 2.1, 2.2_

- [x] 4. Implementar validadores e normalizadores de dados
  - [x] 4.1 Implementar módulo de validação e normalização de Config_Enriquecida
    - Criar `lambdas/shared/validators.py` com funções de validação de campos obrigatórios (channel_id, serviço, tipo)
    - Criar `lambdas/shared/normalizers.py` com funções de normalização de configurações brutas de MediaLive, MediaPackage, MediaTailor e CloudFront para Config_Enriquecida
    - Implementar validação de tipos de dados e enums
    - _Requisitos: 5.2, 11.1, 11.2, 11.3_

  - [x] 4.2 Escrever teste de propriedade para normalização de configurações
    - **Propriedade 4: Normalização de configurações produz Config_Enriquecida válida**
    - Gerar configurações brutas aleatórias de MediaLive, MediaPackage, MediaTailor e CloudFront e verificar que a normalização produz Config_Enriquecida com todos os campos obrigatórios
    - **Valida: Requisitos 4.6, 5.2**

  - [x] 4.3 Implementar módulo de validação e normalização de Evento_Estruturado
    - Criar funções de normalização de logs brutos do CloudWatch para Evento_Estruturado (timestamp, canal, severidade, tipo_erro, descrição)
    - Implementar enriquecimento com causa provável, impacto estimado e recomendação de correção
    - _Requisitos: 7.2, 7.3, 11.4_

  - [x] 4.4 Escrever teste de propriedade para normalização de logs
    - **Propriedade 5: Normalização de logs produz Evento_Estruturado válido**
    - Gerar entradas de log brutas aleatórias e verificar que a normalização produz Evento_Estruturado com todos os campos obrigatórios
    - **Valida: Requisitos 7.2**

  - [x] 4.5 Escrever teste de propriedade para validação de campos obrigatórios
    - **Propriedade 8: Validação de campos obrigatórios com log de rejeição**
    - Gerar Config_Enriquecida e Evento_Estruturado com campos faltantes aleatórios e verificar rejeição com log identificando campos ausentes
    - **Valida: Requisitos 11.3, 11.4, 11.5**

  - [x] 4.6 Implementar validador de contaminação cruzada entre bases
    - Criar função que detecta se um registro de log está sendo ingerido no bucket de config e vice-versa
    - Registrar alerta quando contaminação for detectada
    - _Requisitos: 10.4, 10.5_

  - [x] 4.7 Escrever teste de propriedade para prevenção de contaminação cruzada
    - **Propriedade 7: Prevenção de contaminação cruzada entre bases de conhecimento**
    - Gerar registros de config e log misturados e verificar rejeição correta com alerta
    - **Valida: Requisitos 6.4, 10.4, 10.5**


- [x] 5. Checkpoint — Validar módulos de validação e normalização
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implementar Pipeline de Ingestão de Configurações
  - [x] 6.1 Implementar código da Lambda do Pipeline de Configurações
    - Criar `lambdas/pipeline_config/handler.py` com lógica de extração de configurações via APIs AWS (MediaLive, MediaPackage, MediaTailor, CloudFront)
    - Listar e descrever canais/recursos de cada serviço
    - Normalizar cada configuração em Config_Enriquecida usando módulo compartilhado
    - Validar campos obrigatórios e armazenar no S3 (`kb-config/`)
    - Implementar tratamento de erros: registrar falha com channel_id/distribution_id e continuar com próximo recurso
    - _Requisitos: 5.1, 5.2, 5.3, 5.4_

  - [x] 6.2 Escrever teste de propriedade para logs de erro com identificação
    - **Propriedade 6: Logs de erro contêm informações de identificação**
    - Gerar combinações de channel_id/distribution_id/configuration_name + tipos de erro e verificar que o log de erro contém identificador e motivo
    - **Valida: Requisitos 5.4, 7.5**

  - [x] 6.3 Escrever testes unitários para Pipeline de Configurações
    - Testar extração de configurações de cada serviço (MediaLive, MediaPackage, MediaTailor, CloudFront)
    - Testar tratamento de falha em API individual sem interromper pipeline
    - Testar priorização de JSON sobre texto bruto
    - _Requisitos: 5.1, 5.4, 11.2_

  - [x] 6.4 Criar stack CDK para Pipeline de Ingestão de Configurações
    - Criar Lambda com IAM role para `medialive:ListChannels`, `medialive:DescribeChannel`, `mediapackage:ListChannels`, `mediapackage:ListOriginEndpoints`, `mediatailor:ListPlaybackConfigurations`, `mediatailor:GetPlaybackConfiguration`, `cloudfront:ListDistributions`, `cloudfront:GetDistribution`, `s3:PutObject` no bucket KB_CONFIG
    - Criar regra EventBridge para execução agendada (ex: a cada 6 horas)
    - _Requisitos: 5.1, 5.3_

- [x] 7. Implementar Pipeline de Ingestão de Logs
  - [x] 7.1 Implementar código da Lambda do Pipeline de Logs
    - Criar `lambdas/pipeline_logs/handler.py` com lógica de coleta de logs do CloudWatch (MediaLive, MediaPackage, MediaTailor, CloudFront)
    - Normalizar cada log em Evento_Estruturado usando módulo compartilhado
    - Enriquecer com causa provável, impacto estimado e recomendação de correção
    - Validar campos obrigatórios e verificar contaminação cruzada
    - Armazenar no S3 (`kb-logs/`)
    - Implementar tratamento de erros: registrar falha com nome do serviço e continuar
    - _Requisitos: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [x] 7.2 Escrever testes unitários para Pipeline de Logs
    - Testar coleta e normalização de logs de cada serviço
    - Testar enriquecimento de eventos
    - Testar rejeição de dados de configuração no bucket de logs (contaminação cruzada)
    - Testar tratamento de falha em coleta de serviço individual
    - _Requisitos: 7.1, 7.2, 7.3, 7.5, 10.5_

  - [x] 7.3 Criar stack CDK para Pipeline de Ingestão de Logs
    - Criar Lambda com IAM role para `logs:FilterLogEvents`, `s3:PutObject` no bucket KB_LOGS
    - Criar regra EventBridge para execução agendada (ex: a cada 1 hora)
    - _Requisitos: 7.1, 7.4_

- [x] 8. Checkpoint — Validar pipelines de ingestão
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Configurar Bedrock Agent e Knowledge Bases
  - [x] 9.1 Criar stack CDK para Knowledge Bases (KB_CONFIG e KB_LOGS)
    - Criar KB_CONFIG com data source S3 (prefixo `kb-config/`), embeddings Amazon Titan V2, metadata filtering por channel_id, serviço, tipo
    - Criar KB_LOGS com data source S3 (prefixo `kb-logs/`), embeddings Amazon Titan V2, metadata filtering por timestamp, canal, severidade, tipo_erro
    - Configurar vector store (OpenSearch Serverless ou padrão Bedrock)
    - _Requisitos: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 6.1, 6.2, 6.3_

  - [x] 9.2 Criar stack CDK para Agente Bedrock com instruções e associações
    - Criar Agente Bedrock com modelo base (Amazon Nova ou Claude)
    - Configurar instruções do agente para classificação de intenção (configuração, configuração_acao, logs, ambos, exportação)
    - Associar KB_CONFIG e KB_LOGS ao agente com descrições apropriadas
    - Configurar resposta em português brasileiro
    - _Requisitos: 3.1, 3.2, 3.3, 3.4, 3.7, 3.8, 3.9_


- [x] 10. Implementar Lambda_Configuradora e Action_Group_Config
  - [x] 10.1 Implementar código da Lambda_Configuradora
    - Criar `lambdas/configuradora/handler.py` com lógica de criação e modificação de recursos
    - Implementar validação de JSON de configuração (campos obrigatórios, tipos, enums) para cada serviço (MediaLive, MediaPackage, MediaTailor, CloudFront)
    - Implementar chamadas às APIs AWS: `CreateChannel`, `UpdateChannel`, `CreateInput`, `UpdateInput`, `CreateOriginEndpoint`, `UpdateOriginEndpoint`, `PutPlaybackConfiguration`, `CreateDistribution`, `UpdateDistribution`
    - Para modificações: obter configuração atual antes de executar (para rollback info)
    - Implementar registro de log de auditoria no S3_Audit (sucesso e falha)
    - Variáveis de ambiente: `AUDIT_BUCKET`, `AUDIT_PREFIX`
    - _Requisitos: 12.1, 12.4, 12.5, 12.6, 12.7, 12.8, 12.9, 13.3, 13.4, 13.5, 13.6_

  - [x] 10.2 Escrever teste de propriedade para validação de JSON de configuração
    - **Propriedade 9: Validação de JSON de configuração gerado**
    - Gerar configurações JSON aleatórias para cada serviço e verificar que a validação identifica corretamente campos obrigatórios e estrutura
    - **Valida: Requisitos 12.1, 12.7**

  - [x] 10.3 Escrever teste de propriedade para completude do log de auditoria
    - **Propriedade 11: Completude do log de auditoria**
    - Gerar operações aleatórias (criação/modificação) com resultados variados (sucesso/falha) e verificar que o log contém todos os campos obrigatórios
    - **Valida: Requisitos 12.9, 13.3, 13.6**

  - [x] 10.4 Escrever teste de propriedade para informações de reversão
    - **Propriedade 12: Informações de reversão no log de auditoria**
    - Gerar operações bem-sucedidas de criação e modificação e verificar que o log contém informações de rollback adequadas
    - **Valida: Requisitos 13.4**

  - [x] 10.5 Escrever teste de propriedade para mensagem de erro descritiva
    - **Propriedade 13: Mensagem de erro descritiva da Lambda_Configuradora**
    - Gerar erros de API AWS variados (códigos, mensagens, serviços) e verificar que a mensagem retornada contém código e motivo
    - **Valida: Requisitos 12.8**

  - [x] 10.6 Escrever testes unitários para Lambda_Configuradora
    - Testar criação de canal MediaLive com JSON válido
    - Testar modificação de recurso existente com obtenção de config anterior
    - Testar rejeição de JSON com campos obrigatórios ausentes
    - Testar registro de auditoria em caso de falha na API AWS
    - _Requisitos: 12.4, 12.6, 12.7, 12.8, 13.3_

  - [x] 10.7 Criar stack CDK para Action_Group_Config e Lambda_Configuradora
    - Criar Lambda_Configuradora com IAM role para operações MediaLive, MediaPackage, MediaTailor, CloudFront e `s3:PutObject` no S3_Audit
    - Criar Action_Group_Config no Agente Bedrock com schema OpenAPI (criarRecurso, modificarRecurso)
    - Associar Lambda_Configuradora ao Action_Group_Config
    - _Requisitos: 12.4, 12.5, 13.5_

- [x] 11. Implementar Lambda_Exportadora e Action_Group_Export
  - [x] 11.1 Implementar código da Lambda_Exportadora
    - Criar `lambdas/exportadora/handler.py` com lógica de consulta filtrada nos buckets S3 (kb-config/, kb-logs/)
    - Implementar filtragem de dados por serviço, channel_id, parâmetros técnicos (configurações) e canal, severidade, tipo_erro, período (logs)
    - Implementar formatação CSV e JSON com colunas contextuais
    - Implementar upload para S3_Exports e geração de URL pré-assinada (60 min)
    - Implementar tratamento de "sem resultados" (não gerar arquivo)
    - Variáveis de ambiente: `KB_CONFIG_BUCKET`, `KB_CONFIG_PREFIX`, `KB_LOGS_BUCKET`, `KB_LOGS_PREFIX`, `EXPORTS_BUCKET`, `EXPORTS_PREFIX`, `PRESIGNED_URL_EXPIRY`
    - _Requisitos: 14.1, 14.2, 14.3, 14.4, 14.5, 14.7, 14.8, 14.9_

  - [x] 11.2 Escrever teste de propriedade para filtragem de configurações
    - **Propriedade 14: Filtragem correta na exportação de configurações**
    - Gerar conjuntos aleatórios de Config_Enriquecida + combinações de filtros e verificar que o resultado contém exclusivamente registros correspondentes
    - **Valida: Requisitos 14.1, 14.7**

  - [x] 11.3 Escrever teste de propriedade para filtragem de logs
    - **Propriedade 15: Filtragem correta na exportação de logs**
    - Gerar conjuntos aleatórios de Evento_Estruturado + combinações de filtros e verificar que o resultado contém exclusivamente registros correspondentes
    - **Valida: Requisitos 14.2, 14.7**

  - [x] 11.4 Escrever teste de propriedade para consolidação combinada
    - **Propriedade 16: Consolidação correta na exportação combinada**
    - Gerar dados de config e logs + filtros combinados e verificar que o arquivo contém dados de ambas as fontes sem perda ou duplicação
    - **Valida: Requisitos 14.3**

  - [x] 11.5 Escrever teste de propriedade para round-trip CSV/JSON
    - **Propriedade 17: Round-trip de formatação CSV/JSON**
    - Gerar conjuntos aleatórios de dados exportáveis, formatar como CSV/JSON, fazer parsing e verificar equivalência com dados originais
    - **Valida: Requisitos 14.4**

  - [x] 11.6 Escrever teste de propriedade para exportação sem resultados
    - **Propriedade 18: Exportação sem resultados não gera arquivo**
    - Gerar conjuntos de dados + filtros que não correspondem a nenhum registro e verificar que nenhum arquivo é gerado no S3_Exports
    - **Valida: Requisitos 14.8**

  - [x] 11.7 Escrever teste de propriedade para mensagem de erro na exportação
    - **Propriedade 19: Mensagem de erro descritiva na exportação**
    - Gerar cenários de erro variados (falha S3, formatação, upload) e verificar que a mensagem retornada contém motivo da falha
    - **Valida: Requisitos 14.9**

  - [x] 11.8 Escrever testes unitários para Lambda_Exportadora
    - Testar exportação de lista de canais com filtro low_latency em CSV
    - Testar exportação de erros dos últimos 7 dias em JSON
    - Testar exportação combinada de canal específico
    - Testar formato padrão CSV quando não especificado
    - Testar retorno de "sem resultados" quando filtros não correspondem
    - _Requisitos: 14.1, 14.2, 14.3, 14.4, 14.8_

  - [x] 11.9 Criar stack CDK para Action_Group_Export e Lambda_Exportadora
    - Criar Lambda_Exportadora com IAM role para `s3:GetObject`/`s3:ListBucket` nos buckets KB_CONFIG e KB_LOGS, e `s3:PutObject`/`s3:GetObject` no S3_Exports
    - Criar Action_Group_Export no Agente Bedrock com schema OpenAPI (exportarConfiguracoes, exportarLogs, exportarCombinado)
    - Associar Lambda_Exportadora ao Action_Group_Export
    - _Requisitos: 14.1, 14.5_

- [x] 12. Checkpoint — Validar componentes de backend
  - Ensure all tests pass, ask the user if questions arise.


- [x] 13. Implementar Frontend_Chat
  - [x] 13.1 Criar aplicação web estática do Frontend_Chat
    - Criar `frontend/index.html`, `frontend/styles.css`, `frontend/app.js`
    - Implementar campo de entrada de texto e área de exibição de mensagens em formato conversacional (chat bubbles)
    - Implementar indicador de carregamento durante processamento
    - Implementar envio de pergunta via `POST /chat` para API Gateway
    - Implementar exibição de resposta formatada na área de conversação
    - Implementar exibição de mensagens de erro descritivas (HTTP 400, 504, 500, falha de rede)
    - Implementar histórico de mensagens da sessão atual
    - _Requisitos: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [x] 13.2 Escrever teste de propriedade para exibição de resposta
    - **Propriedade 1: Exibição de resposta no chat**
    - Gerar strings aleatórias (incluindo unicode, HTML, strings longas) e verificar que o Frontend_Chat renderiza o conteúdo completo na área de conversação
    - **Valida: Requisitos 1.3**

  - [x] 13.3 Escrever teste de propriedade para preservação do histórico
    - **Propriedade 2: Preservação do histórico de mensagens**
    - Gerar sequências aleatórias de mensagens (1-50) e verificar que todas permanecem visíveis na ordem correta
    - **Valida: Requisitos 1.5**

  - [x] 13.4 Escrever testes unitários para Frontend_Chat
    - Testar renderização inicial com campo de entrada e área de mensagens
    - Testar exibição de indicador de carregamento ao submeter pergunta
    - Testar exibição de mensagens de erro para cada código HTTP
    - _Requisitos: 1.1, 1.2, 1.4_

- [x] 14. Criar stack CDK para hospedagem do Frontend (S3 + CloudFront)
  - Criar distribuição CloudFront_Frontend dedicada com origin S3_Frontend via OAC
  - Configurar Viewer Protocol Policy: `redirect-to-https`
  - Configurar Default Root Object: `index.html`
  - Configurar Custom Error Response: redirecionar 403/404 para `index.html` (SPA routing)
  - Configurar Cache Policy: Managed-CachingOptimized para assets estáticos
  - Configurar bucket policy do S3_Frontend para permitir `s3:GetObject` apenas via CloudFront OAC
  - _Requisitos: 1.6, 1.7, 1.8, 1.9_

- [x] 15. Checkpoint — Validar frontend e hospedagem
  - Ensure all tests pass, ask the user if questions arise.

- [x] 16. Integração final e wiring de todos os componentes
  - [x] 16.1 Criar stack CDK principal que compõe todas as sub-stacks
    - Criar `stacks/main_stack.py` que instancia e conecta todas as stacks (S3, API Gateway, Lambdas, Bedrock, CloudFront)
    - Passar referências entre stacks (bucket names, ARNs, agent IDs)
    - Configurar variáveis de ambiente das Lambdas com valores das stacks dependentes
    - Configurar CORS no API Gateway para o domínio do CloudFront_Frontend
    - _Requisitos: 1.7, 2.1, 3.1_

  - [x] 16.2 Escrever teste de propriedade para confirmação obrigatória
    - **Propriedade 10: Confirmação obrigatória antes de operações de escrita**
    - Gerar sequências de interação com e sem confirmação do usuário e verificar que nenhuma operação de escrita é executada sem confirmação explícita
    - **Valida: Requisitos 13.1, 13.2**

  - [x] 16.3 Escrever testes de integração
    - Testar fluxo completo pergunta-resposta (API Gateway → Lambda → Bedrock → KB)
    - Testar classificação de intenção para cada tipo (configuração, configuração_acao, logs, ambos, exportação)
    - Testar CORS entre CloudFront_Frontend e API Gateway
    - Testar acesso ao frontend via CloudFront_Frontend com HTTPS e OAC
    - _Requisitos: 2.1, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 8.1, 8.5, 8.6, 9.5, 9.6_

- [-] 17. Checkpoint final — Validar integração completa
  - Ensure all tests pass, ask the user if questions arise.

## Notas

- Tasks marcadas com `*` são opcionais e podem ser puladas para um MVP mais rápido
- Cada task referencia requisitos específicos para rastreabilidade
- Checkpoints garantem validação incremental
- Testes de propriedade validam propriedades universais de corretude (Hypothesis para Python)
- Testes unitários validam exemplos específicos e edge cases
- Deploy manual via `cdk deploy` após cada checkpoint
