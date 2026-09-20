$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "This installer is for Windows. On macOS or Linux, use install.sh."
}

$EtnaPackage = "etna-mcp>=1.0.0b39"
if ($env:ETNA_PACKAGE) {
    $EtnaPackage = $env:ETNA_PACKAGE
}

$EtnaPython = "3.12"
if ($env:ETNA_PYTHON) {
    $EtnaPython = $env:ETNA_PYTHON
}

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "Etna > $Message"
}

function Resolve-Uv {
    $command = Get-Command uv `
        -CommandType Application `
        -ErrorAction SilentlyContinue

    if ($command) {
        return $command.Source
    }

    $candidates = @()

    if ($env:UV_INSTALL_DIR) {
        $candidates += Join-Path $env:UV_INSTALL_DIR "uv.exe"
    }

    if ($env:XDG_BIN_HOME) {
        $candidates += Join-Path $env:XDG_BIN_HOME "uv.exe"
    }

    if ($env:XDG_DATA_HOME) {
        $dataParent = Split-Path -Parent $env:XDG_DATA_HOME

        if ($dataParent) {
            $candidates += Join-Path `
                (Join-Path $dataParent "bin") `
                "uv.exe"
        }
    }

    if ($env:USERPROFILE) {
        $candidates += Join-Path `
            $env:USERPROFILE `
            ".local\bin\uv.exe"
    }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }

    return $null
}

function Assert-NativeSuccess([string]$Description) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

$uv = Resolve-Uv

if (-not $uv) {
    Write-Step "Installing uv"

    $previousNoModifyPath = $env:UV_NO_MODIFY_PATH

    try {
        $env:UV_NO_MODIFY_PATH = "1"

        $uvInstaller = Invoke-RestMethod `
            "https://astral.sh/uv/install.ps1"

        Invoke-Expression $uvInstaller
    }
    finally {
        if ($null -eq $previousNoModifyPath) {
            Remove-Item Env:UV_NO_MODIFY_PATH `
                -ErrorAction SilentlyContinue
        }
        else {
            $env:UV_NO_MODIFY_PATH = $previousNoModifyPath
        }
    }

    $uv = Resolve-Uv
}

if (-not $uv) {
    throw "uv was installed but could not be located."
}

Write-Step "Installing Etna"

& $uv tool install `
    --python $EtnaPython `
    --force `
    $EtnaPackage

Assert-NativeSuccess "Etna installation"

$toolBinOutput = & $uv tool dir --bin
Assert-NativeSuccess "Locating the uv tool directory"

$toolBin = ($toolBinOutput | Out-String).Trim()

if (-not $toolBin) {
    throw "uv did not report its tool executable directory."
}

# Persist the uv/Etna executable directory for future shells.
#
# This is best-effort because the profile can already contain the correct
# entry even though this PowerShell process still has its old PATH.
& $uv tool update-shell *> $null

$etna = Join-Path $toolBin "etna.exe"

if (-not (Test-Path -LiteralPath $etna -PathType Leaf)) {
    throw "Etna was installed but its launcher was not found at $etna."
}

Write-Step "Initializing Etna"

& $etna init
Assert-NativeSuccess "Etna initialization"

Write-Host ""
Write-Host "Etna installation complete."

if (-not (
    Get-Command etna `
        -CommandType Application `
        -ErrorAction SilentlyContinue
)) {
    Write-Host "Open a new terminal before running etna directly."
}
