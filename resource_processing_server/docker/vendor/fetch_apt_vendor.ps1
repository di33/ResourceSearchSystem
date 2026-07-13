param(
    [switch]$IncludeBlender,
    [string[]]$ExtraPackage = @(),
    [string]$Suite = "trixie",
    [string]$Architecture = "amd64",
    [string]$Mirror = "http://deb.debian.org/debian",
    [string]$SecurityMirror = "http://deb.debian.org/debian-security",
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$dockerDir = Split-Path -Parent $scriptDir
if (-not $OutputDir) {
    $OutputDir = Join-Path $scriptDir "apt"
}

$basePackagesPath = Join-Path $dockerDir "apt-packages.txt"
$blenderPackagesPath = Join-Path $dockerDir "apt-packages.blender.txt"
$indexDir = Join-Path $OutputDir ".indexes"
$manifestPath = Join-Path $OutputDir "manifest.json"
$repoPackagesPath = Join-Path $OutputDir "Packages"
$repoPackagesGzPath = Join-Path $OutputDir "Packages.gz"

if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

function Read-PackageList($Path) {
    Get-Content -Path $Path |
        ForEach-Object { ($_ -replace "#.*$", "").Trim() } |
        Where-Object { $_ }
}

function Invoke-WithRetry {
    param(
        [scriptblock]$Action,
        [string]$Label,
        [int]$Retries = 8
    )

    $attempt = 0
    while ($true) {
        try {
            return & $Action
        }
        catch {
            $attempt += 1
            if ($attempt -ge $Retries) {
                throw
            }
            $delay = [Math]::Min(120, $attempt * 10)
            Write-Warning "$Label failed: $($_.Exception.Message). Retrying in $delay seconds ($attempt/$Retries)."
            Start-Sleep -Seconds $delay
        }
    }
}

function Test-Sha256 {
    param(
        [string]$Path,
        [string]$ExpectedSha256
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    if (-not $ExpectedSha256) {
        return $true
    }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
    return $actual -eq $ExpectedSha256.ToLowerInvariant()
}

function Save-Url {
    param(
        [string]$Uri,
        [string]$Path,
        [string]$Sha256 = ""
    )

    if (Test-Sha256 -Path $Path -ExpectedSha256 $Sha256) {
        Write-Host "OK $([IO.Path]::GetFileName($Path))"
        return
    }

    $partialPath = "$Path.partial"
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null

    Invoke-WithRetry -Label $Uri -Action {
        if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
            & curl.exe -fL --retry 8 --retry-delay 5 --retry-all-errors --connect-timeout 30 --speed-time 60 --speed-limit 1024 -C - -o $partialPath $Uri
            if ($LASTEXITCODE -ne 0) {
                throw "curl.exe exited with $LASTEXITCODE"
            }
        }
        else {
            Invoke-WebRequest -Uri $Uri -OutFile $partialPath
        }

        if (-not (Test-Sha256 -Path $partialPath -ExpectedSha256 $Sha256)) {
            Remove-Item -LiteralPath $partialPath -Force -ErrorAction SilentlyContinue
            throw "SHA256 mismatch for $Uri"
        }

        Move-Item -LiteralPath $partialPath -Destination $Path -Force
    }
}

function Test-UrlExists {
    param(
        [string]$Uri
    )

    if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
        $status = & curl.exe -sS -L -I -o NUL -w "%{http_code}" --connect-timeout 20 $Uri
        return $LASTEXITCODE -eq 0 -and $status -match "^2"
    }

    try {
        $response = Invoke-WebRequest -Uri $Uri -Method Head -TimeoutSec 20
        return [int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 300
    }
    catch {
        return $false
    }
}

function Expand-AptIndex {
    param(
        [string]$ArchivePath,
        [string]$OutputPath
    )

    if ($ArchivePath.EndsWith(".gz", [StringComparison]::OrdinalIgnoreCase)) {
        $inputStream = [IO.File]::OpenRead($ArchivePath)
        try {
            $gzipStream = [IO.Compression.GzipStream]::new($inputStream, [IO.Compression.CompressionMode]::Decompress)
            try {
                $outputStream = [IO.File]::Create($OutputPath)
                try {
                    $gzipStream.CopyTo($outputStream)
                }
                finally {
                    $outputStream.Dispose()
                }
            }
            finally {
                $gzipStream.Dispose()
            }
        }
        finally {
            $inputStream.Dispose()
        }
        return
    }

    if ($ArchivePath.EndsWith(".xz", [StringComparison]::OrdinalIgnoreCase)) {
        $scriptPath = Join-Path ([IO.Path]::GetTempPath()) "resource-upload-expand-xz.py"
        $code = @'
import lzma
import sys
from pathlib import Path
src = Path(sys.argv[1])
dst = Path(sys.argv[2])
dst.write_bytes(lzma.decompress(src.read_bytes()))
'@
        Set-Content -LiteralPath $scriptPath -Value $code -Encoding UTF8
        try {
            & $Python $scriptPath $ArchivePath $OutputPath
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to decompress xz apt index with $Python"
            }
        }
        finally {
            Remove-Item -LiteralPath $scriptPath -Force -ErrorAction SilentlyContinue
        }
        return
    }

    throw "Unsupported apt index archive: $ArchivePath"
}

function Save-AptIndex {
    param(
        [string]$BaseUri,
        [string]$OutputBasePath,
        [switch]$Optional
    )

    foreach ($extension in @(".xz", ".gz")) {
        $uri = "$BaseUri$extension"
        if (-not (Test-UrlExists -Uri $uri)) {
            continue
        }
        $archivePath = "$OutputBasePath$extension"
        Save-Url -Uri $uri -Path $archivePath
        return $archivePath
    }

    if ($Optional) {
        return $null
    }
    throw "No apt index found for $BaseUri (.xz or .gz)."
}

function Write-GzipFile {
    param(
        [string]$SourcePath,
        [string]$DestinationPath
    )

    $inputStream = [IO.File]::OpenRead($SourcePath)
    try {
        $outputStream = [IO.File]::Create($DestinationPath)
        try {
            $gzipStream = [IO.Compression.GzipStream]::new($outputStream, [IO.Compression.CompressionLevel]::Optimal)
            try {
                $inputStream.CopyTo($gzipStream)
            }
            finally {
                $gzipStream.Dispose()
            }
        }
        finally {
            $outputStream.Dispose()
        }
    }
    finally {
        $inputStream.Dispose()
    }
}

function Read-DebianPackages {
    param(
        [string]$Path,
        [string]$RepoBase,
        [string]$RepoName
    )

    $records = New-Object System.Collections.Generic.List[object]
    $fields = @{}
    $currentField = $null

    function Add-CurrentRecord {
        if ($fields.ContainsKey("Package")) {
            $record = [ordered]@{}
            foreach ($key in $fields.Keys) {
                $record[$key] = $fields[$key]
            }
            $record["RepoBase"] = $RepoBase
            $record["RepoName"] = $RepoName
            $records.Add([pscustomobject]$record)
        }
    }

    foreach ($line in Get-Content -LiteralPath $Path) {
        if (-not $line.Trim()) {
            Add-CurrentRecord
            $fields = @{}
            $currentField = $null
            continue
        }

        if ($line.StartsWith(" ") -and $currentField) {
            $fields[$currentField] = "$($fields[$currentField]) $($line.Trim())"
            continue
        }

        $colon = $line.IndexOf(":")
        if ($colon -lt 1) {
            continue
        }
        $currentField = $line.Substring(0, $colon)
        $fields[$currentField] = $line.Substring($colon + 1).Trim()
    }

    Add-CurrentRecord
    return $records
}

function Split-DebianVersion {
    param([string]$Version)

    $epoch = 0
    $rest = $Version
    $colon = $rest.IndexOf(":")
    if ($colon -ge 0) {
        $epochText = $rest.Substring(0, $colon)
        [int]::TryParse($epochText, [ref]$epoch) | Out-Null
        $rest = $rest.Substring($colon + 1)
    }

    $revision = "0"
    $dash = $rest.LastIndexOf("-")
    if ($dash -ge 0) {
        $revision = $rest.Substring($dash + 1)
        $rest = $rest.Substring(0, $dash)
    }

    return [pscustomobject]@{
        Epoch = $epoch
        Upstream = $rest
        Revision = $revision
    }
}

function Get-DebianCharOrder {
    param(
        [string]$Text,
        [int]$Index
    )

    if ($Index -ge $Text.Length) {
        return 0
    }

    $char = $Text[$Index]
    if ($char -eq "~") {
        return -1
    }
    if (($char -ge "A" -and $char -le "Z") -or ($char -ge "a" -and $char -le "z")) {
        return [int][char]$char
    }
    return ([int][char]$char) + 256
}

function Compare-DebianVersionPart {
    param(
        [string]$A,
        [string]$B
    )

    $aIndex = 0
    $bIndex = 0

    while ($aIndex -lt $A.Length -or $bIndex -lt $B.Length) {
        while (
            ($aIndex -lt $A.Length -and -not [char]::IsDigit($A[$aIndex])) -or
            ($bIndex -lt $B.Length -and -not [char]::IsDigit($B[$bIndex]))
        ) {
            $aOrder = Get-DebianCharOrder -Text $A -Index $aIndex
            $bOrder = Get-DebianCharOrder -Text $B -Index $bIndex
            if ($aOrder -ne $bOrder) {
                return [Math]::Sign($aOrder - $bOrder)
            }
            if ($aIndex -lt $A.Length) {
                $aIndex += 1
            }
            if ($bIndex -lt $B.Length) {
                $bIndex += 1
            }
        }

        while ($aIndex -lt $A.Length -and $A[$aIndex] -eq "0") {
            $aIndex += 1
        }
        while ($bIndex -lt $B.Length -and $B[$bIndex] -eq "0") {
            $bIndex += 1
        }

        $aStart = $aIndex
        while ($aIndex -lt $A.Length -and [char]::IsDigit($A[$aIndex])) {
            $aIndex += 1
        }
        $bStart = $bIndex
        while ($bIndex -lt $B.Length -and [char]::IsDigit($B[$bIndex])) {
            $bIndex += 1
        }

        $aLength = $aIndex - $aStart
        $bLength = $bIndex - $bStart
        if ($aLength -ne $bLength) {
            return [Math]::Sign($aLength - $bLength)
        }

        for ($i = 0; $i -lt $aLength; $i += 1) {
            $aChar = $A[$aStart + $i]
            $bChar = $B[$bStart + $i]
            if ($aChar -ne $bChar) {
                return [Math]::Sign(([int][char]$aChar) - ([int][char]$bChar))
            }
        }
    }

    return 0
}

function Compare-DebianVersion {
    param(
        [string]$A,
        [string]$B
    )

    $aVersion = Split-DebianVersion -Version $A
    $bVersion = Split-DebianVersion -Version $B

    if ($aVersion.Epoch -ne $bVersion.Epoch) {
        return [Math]::Sign($aVersion.Epoch - $bVersion.Epoch)
    }

    $upstreamCompare = Compare-DebianVersionPart -A $aVersion.Upstream -B $bVersion.Upstream
    if ($upstreamCompare -ne 0) {
        return $upstreamCompare
    }

    return (Compare-DebianVersionPart -A $aVersion.Revision -B $bVersion.Revision)
}

function Format-LocalAptRecord {
    param(
        [object]$Record,
        [string]$LocalFilename
    )

    $skip = @{
        RepoBase = $true
        RepoName = $true
        Filename = $true
    }
    $preferred = @(
        "Package",
        "Version",
        "Installed-Size",
        "Maintainer",
        "Architecture",
        "Replaces",
        "Depends",
        "Pre-Depends",
        "Recommends",
        "Suggests",
        "Breaks",
        "Conflicts",
        "Provides",
        "Section",
        "Priority",
        "Homepage",
        "Description",
        "Size",
        "MD5sum",
        "SHA256"
    )

    $fieldNames = New-Object System.Collections.Generic.List[string]
    foreach ($field in $preferred) {
        if ($Record.PSObject.Properties[$field]) {
            $fieldNames.Add($field)
        }
    }
    foreach ($property in $Record.PSObject.Properties) {
        if ($skip.ContainsKey($property.Name)) {
            continue
        }
        if (-not $fieldNames.Contains($property.Name)) {
            $fieldNames.Add($property.Name)
        }
    }

    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($field in $fieldNames) {
        $value = $Record.$field
        if ($null -ne $value -and "$value" -ne "") {
            $lines.Add("${field}: $value")
        }
    }
    $lines.Add("Filename: ./$LocalFilename")
    return ($lines -join "`n")
}

function Get-DependencyNames {
    param(
        [string]$DependencyText,
        [hashtable]$PackageIndex,
        [hashtable]$ProvidesIndex
    )

    $names = New-Object System.Collections.Generic.List[string]
    if (-not $DependencyText) {
        return $names
    }

    foreach ($group in ($DependencyText -split ",")) {
        $chosen = $null
        $fallback = $null
        foreach ($alternative in ($group -split "\|")) {
            $name = $alternative.Trim()
            $name = $name -replace "\s*\(.*?\)", ""
            $name = $name -replace "\s*\[.*?\]", ""
            $name = $name -replace ":[A-Za-z0-9-]+$", ""
            $name = $name.Trim()
            if (-not $fallback) {
                $fallback = $name
            }
            $resolved = Resolve-PackageName -Name $name -PackageIndex $PackageIndex -ProvidesIndex $ProvidesIndex
            if ($resolved) {
                $chosen = $resolved
                break
            }
        }
        if ($chosen) {
            $names.Add($chosen)
        }
        elseif ($fallback) {
            Write-Warning "No concrete package found for dependency alternative '$group'."
        }
    }
    return $names
}

function Get-ProvidedNames {
    param(
        [string]$ProvidesText
    )

    $names = New-Object System.Collections.Generic.List[string]
    if (-not $ProvidesText) {
        return $names
    }

    foreach ($item in ($ProvidesText -split ",")) {
        $name = $item.Trim()
        $name = $name -replace "\s*\(.*?\)", ""
        $name = $name -replace "\s*\[.*?\]", ""
        $name = $name -replace ":[A-Za-z0-9-]+$", ""
        $name = $name.Trim()
        if ($name) {
            $names.Add($name)
        }
    }
    return $names
}

function Resolve-PackageName {
    param(
        [string]$Name,
        [hashtable]$PackageIndex,
        [hashtable]$ProvidesIndex
    )

    if ($PackageIndex.ContainsKey($Name)) {
        return $Name
    }
    if ($ProvidesIndex.ContainsKey($Name)) {
        $provider = $ProvidesIndex[$Name][0]
        Write-Host "Using provider $($provider.Package) for virtual package $Name"
        return $provider.Package
    }
    return $null
}

$packages = @(Read-PackageList $basePackagesPath)
if ($IncludeBlender) {
    $packages += @(Read-PackageList $blenderPackagesPath)
}
$packages += @($ExtraPackage | Where-Object { $_ })
$rootPackages = @($packages | Select-Object -Unique | Sort-Object)

if (-not $rootPackages) {
    throw "No packages selected."
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
New-Item -ItemType Directory -Force -Path $indexDir | Out-Null

$repos = @(
    @{ Name = "security"; Base = $SecurityMirror; Index = "$SecurityMirror/dists/$Suite-security/main/binary-$Architecture/Packages"; Optional = $false },
    @{ Name = "updates"; Base = $Mirror; Index = "$Mirror/dists/$Suite-updates/main/binary-$Architecture/Packages"; Optional = $true },
    @{ Name = "main"; Base = $Mirror; Index = "$Mirror/dists/$Suite/main/binary-$Architecture/Packages"; Optional = $false }
)

$packageIndex = @{}
$providesIndex = @{}
$allRecords = New-Object System.Collections.Generic.List[object]
foreach ($repo in $repos) {
    $safeName = "$($repo.Name)-Packages"
    $packagesPath = Join-Path $indexDir $safeName

    try {
        $indexArchivePath = Save-AptIndex -BaseUri $repo.Index -OutputBasePath (Join-Path $indexDir $safeName) -Optional:([bool]$repo.Optional)
    }
    catch {
        if ($repo.Optional) {
            Write-Warning "Skipping optional apt index $($repo.Index): $($_.Exception.Message)"
            continue
        }
        throw
    }

    if (-not $indexArchivePath) {
        Write-Warning "Skipping optional apt index $($repo.Index): no .xz or .gz index found."
        continue
    }

    Expand-AptIndex -ArchivePath $indexArchivePath -OutputPath $packagesPath
    foreach ($record in Read-DebianPackages -Path $packagesPath -RepoBase $repo.Base -RepoName $repo.Name) {
        $allRecords.Add($record)
    }
}

foreach ($record in $allRecords) {
    $name = $record.Package
    if (-not $packageIndex.ContainsKey($name)) {
        $packageIndex[$name] = $record
        continue
    }

    $existing = $packageIndex[$name]
    if ((Compare-DebianVersion -A $record.Version -B $existing.Version) -gt 0) {
        $packageIndex[$name] = $record
    }
}

foreach ($record in $packageIndex.Values) {
    foreach ($providedName in Get-ProvidedNames -ProvidesText $record.Provides) {
        if (-not $providesIndex.ContainsKey($providedName)) {
            $providesIndex[$providedName] = New-Object System.Collections.Generic.List[object]
        }
        $providers = $providesIndex[$providedName]
        $samePackageProvider = $providers | Where-Object { $_.Package -eq $record.Package } | Select-Object -First 1
        if (-not $samePackageProvider) {
            $providers.Add($record)
        }
        elseif ((Compare-DebianVersion -A $record.Version -B $samePackageProvider.Version) -gt 0) {
            $providers.Remove($samePackageProvider)
            $providers.Add($record)
        }
    }
}

foreach ($providedName in @($providesIndex.Keys)) {
    $providers = @($providesIndex[$providedName] | Sort-Object Package)
    if ($providers.Count -gt 1) {
        $best = $providers[0]
        foreach ($provider in $providers) {
            if ((Compare-DebianVersion -A $provider.Version -B $best.Version) -gt 0) {
                $best = $provider
            }
        }
        $list = New-Object System.Collections.Generic.List[object]
        $list.Add($best)
        foreach ($provider in $providers) {
            if ($provider.Package -ne $best.Package) {
                $list.Add($provider)
            }
        }
        $providesIndex[$providedName] = $list
    }
}

$queue = [System.Collections.Generic.Queue[string]]::new()
$seen = @{}
$selected = @{}
foreach ($name in $rootPackages) {
    $queue.Enqueue($name)
}

while ($queue.Count -gt 0) {
    $requestedName = $queue.Dequeue()
    $name = Resolve-PackageName -Name $requestedName -PackageIndex $packageIndex -ProvidesIndex $providesIndex
    if (-not $name) {
        throw "Package '$requestedName' was not found in $Suite apt indexes for $Architecture."
    }
    if ($seen.ContainsKey($name)) {
        continue
    }
    $seen[$name] = $true

    $record = $packageIndex[$name]
    $selected[$name] = $record

    foreach ($dep in Get-DependencyNames -DependencyText $record.'Pre-Depends' -PackageIndex $packageIndex -ProvidesIndex $providesIndex) {
        if (-not $seen.ContainsKey($dep)) {
            $queue.Enqueue($dep)
        }
    }
    foreach ($dep in Get-DependencyNames -DependencyText $record.Depends -PackageIndex $packageIndex -ProvidesIndex $providesIndex) {
        if (-not $seen.ContainsKey($dep)) {
            $queue.Enqueue($dep)
        }
    }
}

$downloaded = New-Object System.Collections.Generic.List[object]
$repoRecords = New-Object System.Collections.Generic.List[string]
foreach ($name in ($selected.Keys | Sort-Object)) {
    $record = $selected[$name]
    $filename = $record.Filename
    if (-not $filename) {
        throw "Package '$name' has no Filename field."
    }

    $uri = "$($record.RepoBase.TrimEnd('/'))/$filename"
    $leaf = Split-Path -Leaf $filename
    $outPath = Join-Path $OutputDir $leaf
    Save-Url -Uri $uri -Path $outPath -Sha256 $record.SHA256
    $repoRecords.Add((Format-LocalAptRecord -Record $record -LocalFilename $leaf))

    $downloaded.Add([pscustomobject]@{
        package = $name
        version = $record.Version
        file = $leaf
        repo = $record.RepoName
        sha256 = $record.SHA256
        size = $record.Size
    })
}

($repoRecords -join "`n`n") + "`n" | Set-Content -LiteralPath $repoPackagesPath -Encoding UTF8
Write-GzipFile -SourcePath $repoPackagesPath -DestinationPath $repoPackagesGzPath

$manifest = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    suite = $Suite
    architecture = $Architecture
    root_packages = $rootPackages
    repo_index = "Packages"
    repo_index_gzip = "Packages.gz"
    package_count = $downloaded.Count
    packages = $downloaded
}

$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Host "Vendored apt packages written to $OutputDir"
Write-Host "Packages: $($downloaded.Count)"
