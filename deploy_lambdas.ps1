# =============================================================
# Deploy script — zipa e atualiza as Lambdas no AWS (Windows)
# Execute do diretório raiz do projeto:
#   powershell -ExecutionPolicy Bypass -File deploy_lambdas.ps1
#
# Para deploy de uma lambda específica:
#   powershell -ExecutionPolicy Bypass -File deploy_lambdas.ps1 -Only pipeline_ads
#   powershell -ExecutionPolicy Bypass -File deploy_lambdas.ps1 -Only pipeline_logs
#   powershell -ExecutionPolicy Bypass -File deploy_lambdas.ps1 -Only pipeline_config
#   powershell -ExecutionPolicy Bypass -File deploy_lambdas.ps1 -Only exportadora
# =============================================================

param([string]$Only = "")

$ErrorActionPreference = "Stop"

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  Deploy das Lambdas - Streaming Chatbot" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

# --- REGIAO: Lambdas estao em us-east-1 ---
$REGION = "us-east-1"

# --- Nomes das funcoes ---
$CONFIGURADORA   = "StreamingChatbotStack-ConfiguradoraFunction8C3D631-iH4bsa38s3jZ"
$EXPORTADORA     = "StreamingChatbotStack-ExportadoraFunctionF7DCB910-tR185Y8NQSVn"
$ORQUESTRADORA   = "StreamingChatbotStack-OrquestradoraFunctionC93F4B4-9i6FWg7EVPqV"
$PIPELINE_ADS    = "StreamingChatbotPipelineA-PipelineAdsFunctionB6C11-hAyAMXXJRxcy"
$PIPELINE_ADS_BATCH = "StreamingChatbotPipelineA-PipelineAdsBatchFunction-wWO4nZur4DDQ"
$PIPELINE_LOGS   = "StreamingChatbotStack-PipelineLogsFunctionE340BB88-5SkoNySBybw4"
$PIPELINE_CONFIG = "StreamingChatbotStack-PipelineConfigFunction079AFC-k9oe8dUaRswq"

# --- Helper: zip e deploy ---
function Deploy-Lambda {
    param(
        [string]$Name,
        [string]$SourceDir,
        [string]$FunctionName,
        [bool]$HasShared = $false,
        [string[]]$SharedFiles = @("__init__.py", "normalizers.py", "validators.py"),
        [string[]]$ExtraDeps = @()
    )
    $zipFile = "${Name}_deploy.zip"
    $tempDir = "temp_zip_${Name}"
    Write-Host "  Zipando $Name..." -ForegroundColor Yellow
    if (Test-Path $zipFile) { Remove-Item $zipFile -Force }
    if (Test-Path $tempDir) { Remove-Item $tempDir -Recurse -Force }
    New-Item -ItemType Directory -Path $tempDir | Out-Null
    Copy-Item "$SourceDir\handler.py" "$tempDir\handler.py"
    Copy-Item "$SourceDir\__init__.py" "$tempDir\__init__.py"
    if ($HasShared) {
        New-Item -ItemType Directory -Path "$tempDir\shared" | Out-Null
        foreach ($f in $SharedFiles) {
            $src = "$SourceDir\shared\$f"
            if (Test-Path $src) { Copy-Item $src "$tempDir\shared\$f" }
        }
    }
    foreach ($dep in $ExtraDeps) {
        if (Test-Path "$SourceDir\$dep") {
            Copy-Item "$SourceDir\$dep" "$tempDir\$dep" -Recurse
        }
    }
    Compress-Archive -Path "$tempDir\*" -DestinationPath $zipFile -Force
    Remove-Item $tempDir -Recurse -Force
    Write-Host "  Deploy $Name -> $FunctionName..." -ForegroundColor Yellow
    aws lambda update-function-code --function-name $FunctionName --zip-file "fileb://$zipFile" --region $REGION
    Remove-Item $zipFile -Force
    Write-Host "  $Name OK!" -ForegroundColor Green
}

# --- Mapa de deploys ---
function Run-AllDeploys {

    # --- 1. Configuradora ---
    Write-Host ""
    Write-Host "[1/6] Configuradora" -ForegroundColor Cyan
    Deploy-Lambda -Name "configuradora" -SourceDir "lambdas\configuradora" -FunctionName $CONFIGURADORA -HasShared $true

    # --- 2. Exportadora ---
    Write-Host ""
    Write-Host "[2/6] Exportadora" -ForegroundColor Cyan
    Deploy-Lambda -Name "exportadora" -SourceDir "lambdas\exportadora" -FunctionName $EXPORTADORA -HasShared $false

    # --- 3. Orquestradora ---
    Write-Host ""
    Write-Host "[3/6] Orquestradora" -ForegroundColor Cyan
    Deploy-Lambda -Name "orquestradora" -SourceDir "lambdas\orquestradora" -FunctionName $ORQUESTRADORA -HasShared $false

    # --- 4. Pipeline Ads ---
    Write-Host ""
    Write-Host "[4/7] Pipeline Ads" -ForegroundColor Cyan
    Deploy-Lambda -Name "pipeline_ads" -SourceDir "lambdas\pipeline_ads" -FunctionName $PIPELINE_ADS `
        -HasShared $true `
        -SharedFiles @("__init__.py", "normalizers.py", "auth.py") `
        -ExtraDeps @("requests", "certifi", "charset_normalizer", "idna", "urllib3")

    # --- 5. Pipeline Logs ---
    Write-Host ""
    Write-Host "[5/6] Pipeline Logs" -ForegroundColor Cyan
    Deploy-Lambda -Name "pipeline_logs" -SourceDir "lambdas\pipeline_logs" -FunctionName $PIPELINE_LOGS `
        -HasShared $true

    # --- 6. Pipeline Config ---
    Write-Host ""
    Write-Host "[6/6] Pipeline Config" -ForegroundColor Cyan
    Deploy-Lambda -Name "pipeline_config" -SourceDir "lambdas\pipeline_config" -FunctionName $PIPELINE_CONFIG `
        -HasShared $true
}

# --- Execução: individual ou completa ---
if ($Only -ne "") {
    Write-Host ""
    Write-Host "Deploy individual: $Only" -ForegroundColor Cyan
    switch ($Only.ToLower()) {
        "configuradora"   { Deploy-Lambda -Name "configuradora"   -SourceDir "lambdas\configuradora"   -FunctionName $CONFIGURADORA   -HasShared $true }
        "exportadora"     { Deploy-Lambda -Name "exportadora"     -SourceDir "lambdas\exportadora"     -FunctionName $EXPORTADORA     -HasShared $false }
        "orquestradora"   { Deploy-Lambda -Name "orquestradora"   -SourceDir "lambdas\orquestradora"   -FunctionName $ORQUESTRADORA   -HasShared $false }
        "pipeline_ads"    {
            Deploy-Lambda -Name "pipeline_ads" -SourceDir "lambdas\pipeline_ads" -FunctionName $PIPELINE_ADS `
                -HasShared $true `
                -SharedFiles @("__init__.py", "normalizers.py", "auth.py") `
                -ExtraDeps @("requests", "certifi", "charset_normalizer", "idna", "urllib3")
        }
        "pipeline_logs"   { Deploy-Lambda -Name "pipeline_logs"   -SourceDir "lambdas\pipeline_logs"   -FunctionName $PIPELINE_LOGS   -HasShared $true }
        "pipeline_config" { Deploy-Lambda -Name "pipeline_config" -SourceDir "lambdas\pipeline_config" -FunctionName $PIPELINE_CONFIG -HasShared $true }
        default { Write-Host "Lambda '$Only' nao reconhecida." -ForegroundColor Red; exit 1 }
    }
} else {
    Run-AllDeploys
}

# --- Frontend (S3) — apenas no deploy completo ---
if ($Only -eq "") {
    Write-Host ""
    Write-Host "[+] Frontend (S3)" -ForegroundColor Cyan
    try {
        $FRONTEND_BUCKET = aws cloudformation describe-stacks `
            --stack-name StreamingChatbotStack `
            --query "Stacks[0].Outputs[?OutputKey=='FrontendBucketName'].OutputValue" `
            --output text --region $REGION 2>$null

        if ($FRONTEND_BUCKET -and $FRONTEND_BUCKET -ne "None") {
            aws s3 sync frontend/ "s3://$FRONTEND_BUCKET/" --delete --region $REGION
            Write-Host "      Frontend atualizado!" -ForegroundColor Green
        } else {
            Write-Host "      Bucket do frontend nao encontrado. Faca upload manual." -ForegroundColor DarkYellow
        }
    } catch {
        Write-Host "      Erro ao buscar bucket do frontend. Faca upload manual." -ForegroundColor DarkYellow
    }
}

Write-Host ""
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  Deploy concluido!" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host ""
