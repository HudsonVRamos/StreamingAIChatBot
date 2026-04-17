# Correção: Funcionalidade report_by_label

## Problema
Os exemplos de consultas de revenue por label não estavam funcionando:
- "Revenue canais broadpeak.io" → deveria retornar dados agregados por label "broadpeak"
- "Revenue canais Colombia" → deveria retornar dados agregados por label "Colombia"
- "Revenue canais com label broadpeak" → deveria retornar dados agregados

## Solução Implementada

### 1. Adicionado suporte para tipo virtual `report_by_label`
- `report_by_label` não existe como tipo no DynamoDB
- É um tipo virtual que busca dados de `report` e depois agrega por `supply_label_name`

### 2. Melhorias no filtro `canal_nome`
- Para `report_by_label`: busca em `supply_label_name` (campo principal)
- Para outros tipos: busca em múltiplos campos (canal_nome, nome, supply_tag_name, supply_label_name)
- Adicionado `supply_label_name` ao haystack de busca

### 3. Nova função `aggregate_report_by_label()`
- Agrupa registros por `supply_label_name`
- Soma métricas numéricas: requests, opportunities, impressions, revenue, cost
- Calcula médias ponderadas: fill_rate, rpm, cpm
- Ordena resultados por revenue (decrescente)

### 4. Novas colunas para report_by_label
```python
REPORT_BY_LABEL_COLUMNS = [
    "channel_id", "servico", "tipo",
    "supply_label_name", "canal_nome",
    "requests", "opportunities", "impressions",
    "fill_rate", "opp_fill_rate", "req_fill_rate",
    "total_impressions", "total_revenue", "revenue",
    "total_cost", "cpm", "rpm", "data_inicio", "data_fim",
]
```

### 5. Fluxo de processamento
1. Bedrock envia `tipo: "report_by_label"` + `canal_nome: "Colombia"`
2. `query_dynamodb_ads()` converte para `tipo: "report"` e busca dados
3. `filter_records()` filtra por `supply_label_name` contendo "colombia"
4. `aggregate_report_by_label()` agrupa e soma métricas por label
5. CSV é gerado com dados agregados

## Exemplos de Uso

### Exemplo 1: Revenue canais Colombia
```json
{
  "base_dados": "KB_ADS",
  "tipo": "report_by_label",
  "canal_nome": "colombia"
}
```
**Resultado**: CSV com 1 linha agregando todos os supply tags com label "Colombia"

### Exemplo 2: Revenue canais broadpeak.io
```json
{
  "base_dados": "KB_ADS",
  "tipo": "report_by_label",
  "canal_nome": "broadpeak"
}
```
**Resultado**: CSV com 1 linha agregando todos os supply tags com label "broadpeak.io"

### Exemplo 3: Revenue todos os labels
```json
{
  "base_dados": "KB_ADS",
  "tipo": "report_by_label"
}
```
**Resultado**: CSV com múltiplas linhas, uma por label, ordenadas por revenue

### Exemplo 4: Supply tags por device
```json
{
  "base_dados": "KB_ADS",
  "tipo": "supply_tag",
  "device": "android_tv"
}
```
**Resultado**: CSV com todos os supply tags que têm device "android_tv"

## Arquivos Modificados

### `lambdas/exportadora/handler.py`
1. **Linha ~160**: Adicionado `REPORT_BY_LABEL_COLUMNS`
2. **Linha ~880**: Modificado `filter_records()` para:
   - Adicionar `supply_label_name` ao haystack
   - Adicionar `tipo` aos `_skip_keys` quando for `report_by_label`
   - Filtrar por `supply_label_name` quando tipo for `report_by_label`
3. **Linha ~1000**: Nova função `aggregate_report_by_label()`
4. **Linha ~350**: Modificado `query_dynamodb_ads()` para converter `report_by_label` → `report`
5. **Linha ~1100**: Modificado `determine_columns()` para retornar `REPORT_BY_LABEL_COLUMNS`
6. **Linha ~1400**: Modificado `handler()` para aplicar agregação quando tipo for `report_by_label`

## Testes Realizados
✓ Filtro por canal_nome "Colombia" retorna 2 registros
✓ Filtro por canal_nome "broadpeak" retorna 1 registro
✓ Agregação de Colombia soma corretamente requests, impressions, revenue
✓ Agregação de todos os labels retorna 3 grupos ordenados por revenue
✓ Filtro por device "samsung_tv" funciona corretamente

## Deploy
Para aplicar as mudanças:
```bash
# Deploy da lambda exportadora
./deploy_lambdas.ps1
```

## Compatibilidade
- ✓ Mantém compatibilidade com tipos existentes (report, supply_tag, demand_tag)
- ✓ Não quebra consultas existentes
- ✓ Adiciona nova funcionalidade sem remover código existente
