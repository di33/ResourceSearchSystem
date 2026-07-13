param(
    [string]$PackageDir = "Tools\spine_preview",
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OutputDir) {
    $OutputDir = Join-Path $scriptDir "npm"
}

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "npm was not found on PATH."
}

$packageJson = Join-Path $PackageDir "package.json"
$packageLock = Join-Path $PackageDir "package-lock.json"
if (-not (Test-Path -LiteralPath $packageJson) -or -not (Test-Path -LiteralPath $packageLock)) {
    throw "package.json and package-lock.json are required in $PackageDir"
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$workDir = Join-Path $OutputDir ".work"
Remove-Item -LiteralPath $workDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $workDir | Out-Null
Copy-Item -LiteralPath $packageJson -Destination (Join-Path $workDir "package.json")
Copy-Item -LiteralPath $packageLock -Destination (Join-Path $workDir "package-lock.json")

Push-Location $workDir
try {
    npm ci --omit=dev --ignore-scripts --cache $OutputDir --prefer-online --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) {
        throw "npm ci failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $workDir -Recurse -Force -ErrorAction SilentlyContinue
}

$manifestPath = Join-Path $OutputDir "manifest.json"
$packageLockJson = Get-Content -Raw -LiteralPath $packageLock | ConvertFrom-Json -AsHashtable
$dependencies = @()
if ($packageLockJson.ContainsKey("packages") -and $packageLockJson["packages"].ContainsKey("")) {
    $root = $packageLockJson["packages"][""]
    if ($root.ContainsKey("dependencies")) {
        $dependencies = $root["dependencies"].GetEnumerator() |
            Sort-Object Name |
            ForEach-Object { [pscustomobject]@{ name = $_.Name; version = $_.Value } }
    }
}

[ordered]@{
    generated_at = (Get-Date).ToString("o")
    package_dir = $PackageDir
    cache_dir = $OutputDir
    dependencies = $dependencies
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Host "Vendored npm cache written to $OutputDir"
