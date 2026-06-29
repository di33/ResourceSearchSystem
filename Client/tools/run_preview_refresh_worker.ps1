param(
    [ValidateSet("non-pack", "pack", "all")]
    [string]$Phase = "non-pack",
    [int]$WorkerCount = 1,
    [int]$WorkerIndex = 0,
    [string]$StatusRoot = "G:\ResourceUpload\data\runtime\preview-refresh\preview_refresh_status_20260623_220526"
)

$ErrorActionPreference = "Stop"

$repoRoot = "G:\ResourceUpload"
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$script = "G:\ResourceCrawler\tmp\refresh_missing_previews.py"
$dbPath = Join-Path $repoRoot "data\databases\pipeline_rebuilt_20260608_150207.db"
$workDir = Join-Path $repoRoot "data\workdirs\test_workdir_rebuilt_20260608_150207"
$marker = "2026-06-23T13:51:27Z"

New-Item -ItemType Directory -Force -Path $StatusRoot | Out-Null

$env:PYTHONPATH = Join-Path $repoRoot "client\Scripts"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

$prefix = "worker_${Phase}_${WorkerIndex}"
$statusFile = Join-Path $StatusRoot "$prefix.status.log"
$outFile = Join-Path $StatusRoot "$prefix.out.log"
$errFile = Join-Path $StatusRoot "$prefix.err.log"

& $python -u $script `
    --db-path $dbPath `
    --work-dir $workDir `
    --resource-upload-root $repoRoot `
    --marker $marker `
    --progress-every 2000 `
    --phase $Phase `
    --worker-count $WorkerCount `
    --worker-index $WorkerIndex `
    --status-file $statusFile `
    > $outFile 2> $errFile

exit $LASTEXITCODE
