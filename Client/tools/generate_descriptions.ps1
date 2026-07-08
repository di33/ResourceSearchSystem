param(
    [string]$DbPath = "G:\ResourceUpload\data\databases\pipeline.db",
    [string]$LogDir = "",
    [string]$Provider = "ksyun",
    [int]$Concurrency = 5
)

$ErrorActionPreference = "Continue"

Set-Location "G:\ResourceUpload"
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts;G:\ResourceUpload\Tools"

if (-not $LogDir) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $LogDir = "G:\ResourceUpload\data\logs\description_refresh_$stamp"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$summaryLog = Join-Path $LogDir "summary.log"
$python = "G:\ResourceUpload\.venv\Scripts\python.exe"
$types = @(
    "animation_sequence",
    "atlas",
    "audio_file",
    "font_file",
    "spriter",
    "spine_skeleton",
    "tiled_map",
    "tiled_tileset",
    "tileset",
    "single_image"
)

function Write-Summary {
    param([string]$Message)
    $line = "$(Get-Date -Format o) $Message"
    $line | Tee-Object -FilePath $summaryLog -Append
}

Write-Summary "START db=$DbPath provider=$Provider concurrency=$Concurrency log_dir=$LogDir"

foreach ($type in $types) {
    $typeLog = Join-Path $LogDir "$type.log"
    Write-Summary "TYPE_START $type"
    & $python -m ResourceProcessor.generate_descriptions `
        --db-path $DbPath `
        --llm-provider $Provider `
        --resource-type $type `
        --concurrency $Concurrency 2>&1 | Tee-Object -FilePath $typeLog -Append
    $exitCode = $LASTEXITCODE
    Write-Summary "TYPE_DONE $type exit=$exitCode"
    if ($exitCode -ne 0) {
        Write-Summary "STOP exit=$exitCode failed_type=$type"
        exit $exitCode
    }
}

Write-Summary "DONE"
