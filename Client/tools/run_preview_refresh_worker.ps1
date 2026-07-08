param(
    [ValidateSet("non-pack", "pack", "all")]
    [string]$Phase = "non-pack",
    [int]$WorkerCount = 1,
    [int]$WorkerIndex = 0,
    [string]$StatusRoot = "G:\ResourceUpload\data\runtime\preview-refresh\preview_refresh_status_20260623_220526",
    [string]$PreviewRenderer = "http://localhost:8200",
    [string]$ClientId = "resource-crawler",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$repoRoot = "G:\ResourceUpload"
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$dbPath = Join-Path $repoRoot "data\databases\pipeline.db"
$workDir = Join-Path $repoRoot "data"
$marker = "2026-06-23T13:51:27Z"

New-Item -ItemType Directory -Force -Path $StatusRoot | Out-Null

$env:PYTHONPATH = (Join-Path $repoRoot "client\Scripts") + ";" + (Join-Path $repoRoot "Tools")
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

$prefix = "worker_${Phase}_${WorkerIndex}"
$statusFile = Join-Path $StatusRoot "$prefix.status.log"
$outFile = Join-Path $StatusRoot "$prefix.out.log"
$errFile = Join-Path $StatusRoot "$prefix.err.log"

$arguments = @(
    "-u",
    "-m", "ResourceProcessor.generate_previews",
    "--db-path", $dbPath,
    "--work-dir", $workDir,
    "--preview-mode", "renderer",
    "--preview-renderer", $PreviewRenderer,
    "--client-id", $ClientId,
    "--marker", $marker,
    "--progress-every", "2000",
    "--phase", $Phase,
    "--worker-count", "$WorkerCount",
    "--worker-index", "$WorkerIndex",
    "--status-file", $statusFile
)
if ($Force) {
    $arguments += "--force"
}

& $python @arguments > $outFile 2> $errFile

exit $LASTEXITCODE
