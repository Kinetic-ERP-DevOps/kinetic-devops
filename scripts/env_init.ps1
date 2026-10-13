# env_init.ps1 -- PowerShell wrapper for Kinetic environment initialization
#
# Usage (dot-source):
#   . .\scripts\env_init.ps1 [env-name] [venv-path]
#
# This script:
# 1. Activates a Python venv (if provided)
# 2. Calls scripts/env_init.py to resolve stored Kinetic configuration
# 3. Generates and dot-sources a randomized temp .ps1 file to set environment variables
# 4. Sets PYTHONPATH so helper scripts can import the kinetic_devops package
#
param(
    [string]$EnvName = "",
    [string]$VenvPath = "venv",
    [string]$TaxConfigDb = ""
)

$ErrorActionPreference = "Stop"

# 1. Resolve repo root
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Split-Path -Parent $scriptDir

# 2. Activate venv if it exists
if (Test-Path "$VenvPath\Scripts\Activate.ps1") {
    Write-Host "Python venv: $VenvPath"
    & "$VenvPath\Scripts\Activate.ps1"
} else {
    Write-Host "No venv at '$VenvPath', using system Python."
}

# 3. Prepend repo root to PYTHONPATH so the helper scripts can import kinetic_devops,
#    without discarding any existing entries.
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$rootDir$([System.IO.Path]::PathSeparator)$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $rootDir
}

# Optional local AI settings live in a separate, ignored file.
$localAiConfig = Join-Path $scriptDir 'ai_env.local.ps1'
if (Test-Path $localAiConfig) {
    . $localAiConfig
}


# 4. Call env_init.py to generate env variables
Write-Host "Kinetic environment initialization..."
$taxConfigArg = @()
if ($TaxConfigDb) {
    $taxConfigArg = @("--taxconfig-db", $TaxConfigDb)
    Write-Host "[env-init] Overriding KINETIC_TAXCONFIG_DB: $TaxConfigDb"
}

$targetEnv = if ($EnvName) { $EnvName } else { "dev" }
$initOutput = python "$scriptDir\env_init.py" $targetEnv @taxConfigArg @args
$batEnvFile = $null
$psEnvFile = $null

foreach ($line in $initOutput) {
    if ($line -like "WRITTEN_TO=*") {
        $batEnvFile = $line.Substring("WRITTEN_TO=".Length)
        continue
    }
    if ($line -like "WRITTEN_PS1=*") {
        $psEnvFile = $line.Substring("WRITTEN_PS1=".Length)
        continue
    }
    Write-Host $line
}

# 5. Dot-source the generated PowerShell env file if it exists
if ($psEnvFile -and (Test-Path $psEnvFile)) {
    Write-Host "Loading environment variables from $psEnvFile"
    . $psEnvFile
} else {
    Write-Host "WARNING: generated PowerShell env file not found. Environment variables may not be set."
}

# 6. Clean up generated temp files (after sourcing)
if ($batEnvFile -or $psEnvFile) {
    $cleanupArgs = @("$scriptDir\env_init.py", "--cleanup-only")
    if ($batEnvFile) {
        $cleanupArgs += @("--cleanup-path", $batEnvFile)
    }
    if ($psEnvFile) {
        $cleanupArgs += @("--cleanup-path", $psEnvFile)
    }
    python @cleanupArgs | Out-Null
}

if ($psEnvFile -and (Test-Path $psEnvFile)) {
    Remove-Item -Force $psEnvFile -ErrorAction SilentlyContinue | Out-Null
}
if ($batEnvFile -and (Test-Path $batEnvFile)) {
    Remove-Item -Force $batEnvFile -ErrorAction SilentlyContinue | Out-Null
}

Write-Host "Environment initialized with PYTHONPATH set to $rootDir"
