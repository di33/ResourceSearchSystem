param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$RequirementsPath = "preview_renderer\requirements.txt",
    [string]$OutputDir = "",
    [string]$Platform = "manylinux2014_x86_64",
    [string]$PythonVersion = "312",
    [string]$Implementation = "cp",
    [string]$Abi = "cp312",
    [string[]]$PrebuiltPurePythonPackages = @("rectpack>=0.2.2"),
    [string[]]$TargetOnlyPackages = @("uvloop>=0.15.1")
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OutputDir) {
    $OutputDir = Join-Path $scriptDir "pip"
}

if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

function Test-PipAvailable {
    param([string]$Executable)

    try {
        & $Executable -m pip --version *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Ensure-PipAvailable {
    param([string]$Executable)

    if (Test-PipAvailable -Executable $Executable) {
        return $true
    }

    try {
        Write-Host "Python at $Executable does not have pip; running ensurepip."
        & $Executable -m ensurepip --upgrade
        if ($LASTEXITCODE -ne 0) {
            return $false
        }
    } catch {
        return $false
    }

    return (Test-PipAvailable -Executable $Executable)
}

if (-not (Ensure-PipAvailable -Executable $Python)) {
    throw "Could not find or bootstrap pip for Python executable '$Python'. Pass -Python with a Python that can run pip."
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

foreach ($package in $PrebuiltPurePythonPackages) {
    Write-Host "Building pure Python wheel for $package"
    & $Python -m pip wheel `
        --wheel-dir $OutputDir `
        --no-deps `
        --retries 10 `
        --timeout 60 `
        $package
    if ($LASTEXITCODE -ne 0) {
        throw "pip wheel failed for $package with exit code $LASTEXITCODE"
    }
}

$arguments = @(
    "-m", "pip", "download",
    "--dest", $OutputDir,
    "--find-links", $OutputDir,
    "--requirement", $RequirementsPath,
    "--platform", $Platform,
    "--python-version", $PythonVersion,
    "--implementation", $Implementation,
    "--abi", $Abi,
    "--only-binary", ":all:",
    "--retries", "10",
    "--timeout", "60"
)

Write-Host "Downloading Python wheels to $OutputDir"
& $Python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "pip download failed with exit code $LASTEXITCODE"
}

foreach ($package in $TargetOnlyPackages) {
    $targetArguments = @(
        "-m", "pip", "download",
        "--dest", $OutputDir,
        "--find-links", $OutputDir,
        "--platform", $Platform,
        "--python-version", $PythonVersion,
        "--implementation", $Implementation,
        "--abi", $Abi,
        "--only-binary", ":all:",
        "--retries", "10",
        "--timeout", "60",
        $package
    )

    Write-Host "Downloading target-only wheel for $package"
    & $Python @targetArguments
    if ($LASTEXITCODE -ne 0) {
        throw "pip download failed for target-only package $package with exit code $LASTEXITCODE"
    }
}

$manifestPath = Join-Path $OutputDir "manifest.json"
$files = Get-ChildItem -LiteralPath $OutputDir -File |
    Where-Object { $_.Name -match "\.(whl|tar\.gz|zip)$" } |
    Sort-Object Name |
    ForEach-Object {
        [pscustomobject]@{
            file = $_.Name
            size = $_.Length
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
        }
    }

[ordered]@{
    generated_at = (Get-Date).ToString("o")
    requirements = $RequirementsPath
    platform = $Platform
    python_version = $PythonVersion
    implementation = $Implementation
    abi = $Abi
    file_count = @($files).Count
    files = $files
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Host "Vendored pip wheels written to $OutputDir"
