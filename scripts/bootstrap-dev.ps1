#Requires -Version 5.1
# ---------------------------------------------------------------------------
# WINDOWS DEVELOPER BOOTSTRAP SCRIPT (2026 Edition)
# Stack: UniGet + Scoop + uv + VS Code
# Supports: Windows PowerShell 5.1 (required), PowerShell 7+
# ---------------------------------------------------------------------------

param(
    [Parameter(Mandatory=$false)]
    [switch]$InstallProfile
)

$TranscriptPath = $null

function Get-TargetProfilePaths {
    # Accounts for the different profile locations between Windows PowerShell and PowerShell Core
    $docs = Join-Path $HOME "Documents"
    @(
        Join-Path $docs "WindowsPowerShell\Microsoft.PowerShell_profile.ps1"  # Windows PowerShell 5.1
        Join-Path $docs "PowerShell\Microsoft.PowerShell_profile.ps1"         # PowerShell 7+
    ) | Select-Object -Unique
}

try {
    # Start an auditable log for troubleshooting and EDR review
    $LogDir = Join-Path $HOME "Documents\BootstrapLogs"
    if (!(Test-Path $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }

    # Start-Transcript supports -OutputDirectory and -IncludeInvocationHeader in Windows PowerShell 5.1+
    $TranscriptInfo = Start-Transcript -OutputDirectory $LogDir -Append -IncludeInvocationHeader
    $TranscriptPath = $TranscriptInfo.Path

    Clear-Host
    Write-Host "Transcript file: $TranscriptPath"

    # Security: Use OS-managed TLS policy
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::SystemDefault

    # Block elevated execution explicitly (Principle of Least Privilege)
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $currentPrincipal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
    if ($currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "SECURITY ERROR: Do not run this script as Administrator." -ForegroundColor Red
        exit 1
    }

    $ManualUninstallList = @(
        "Python & PyLauncher (all versions)",
        "Postman (Standard Installer)",
        "Ollama (Close from tray first)",
        "VS Code (User or System Installers)",
        "Miniforge / Anaconda / Miniconda"
    )

    Write-Host "======================================================" -ForegroundColor Cyan
    Write-Host "    TEAM BOOTSTRAP: CORE PYTHON DS ENVIRONMENT" -ForegroundColor Cyan
    Write-Host "======================================================" -ForegroundColor Cyan

    Write-Host "`n[ STEP 1: PREPARATION ]" -ForegroundColor Yellow
    Write-Host "To ensure a clean environment, please perform these actions MANUALLY:"
    Write-Host "1. BACKUP: Open VS Code and 'Export Profile' to your Desktop."
    Write-Host "2. CLEANUP: Remove these apps via Windows Settings -> Installed Apps."
    foreach ($app in $ManualUninstallList) {
        Write-Host "   [ ] $app" -ForegroundColor White
    }
    Write-Host "------------------------------------------------------"
    Write-Host "Note: Scoop installs developer tools from public GitHub repositories; it is user-space tooling and not a security boundary." -ForegroundColor Yellow
    Write-Host "Note: uv installs Python tools and dependencies from public package repositories (e.g., PyPI); it is developer tooling and not a security boundary." -ForegroundColor Yellow
    Write-Host "------------------------------------------------------" -ForegroundColor White
    Write-Host "Recommended: Run this script once with the -InstallProfile option to add helpful guardrails and trust reminders to your PowerShell profile for all future terminal sessions." -ForegroundColor Cyan

    $Confirm = Read-Host "`nAre you ready to proceed? (y/n)"
    if ($Confirm -ne 'y') {
        Write-Host "`nAborting. Log saved to $TranscriptPath" -ForegroundColor Red
        exit
    }

    Write-Host "`n[ STEP 2: INSTALLATION ]" -ForegroundColor Green
    Write-Host "Installing Core Stack via WinGet and Scoop..."

    # 1. System Managers
    Write-Host "`n[1/3] Installing Managers..." -ForegroundColor Green

    # Non-fatal WinGet install for UniGetUI
    $WingetLog = Join-Path $env:TEMP "uniget_install.log"
    try {
        winget install --id MartiCliment.UniGetUI -e --source winget --silent --accept-source-agreements --accept-package-agreements --log $WingetLog
        if ($LASTEXITCODE -ne 0) {
            Write-Host "WinGet did not install UniGetUI (exit=$LASTEXITCODE). Continuing..." -ForegroundColor Yellow
        }
    } catch {
        Write-Host "WinGet failed while installing UniGetUI. Continuing..." -ForegroundColor Yellow
    }

    # Use Process-scoped policy to avoid permanently weakening system security
    Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process -Force

    # Official Scoop Install
    if (!(Get-Command scoop -ErrorAction SilentlyContinue)) {
        Write-Host "Scoop not found. Installing via official channel..." -ForegroundColor Gray
        try {
            Invoke-RestMethod -Uri "https://get.scoop.sh" | Invoke-Expression
        } catch {
            Write-Host "CRITICAL ERROR: Failed to install Scoop." -ForegroundColor Red
            throw
        }
    }

    # 2. Main Stack
    Write-Host "[2/3] Installing Core Dev Stack..." -ForegroundColor Green
    scoop install git

    # Add extras for VS Code
    scoop bucket add extras | Out-Null

    # The 'Bare Minimum' easy-to-maintain core
    $apps = @("uv", "vscode", "gh")
    foreach ($app in $apps) {
        Write-Host "Installing $app..." -ForegroundColor Gray
        scoop install $app
    }

    # 3. PowerShell Profile Injection (PS 5.1 + PS 7)
    if ($InstallProfile) {
        Write-Host "[3/3] Writing PowerShell Profiles (5.1 + 7)..." -ForegroundColor Green

        $ProfileBlock = @'
# --- Core Dev Bootstrap (2026) ---
# Educational Shunts: Reinforcing 'uv' workflow

$script:ScoopCommand = $null
$script:UvCommand = $null

function scoop {
    Write-Host "Note: Scoop installs developer tools from public GitHub repositories; it is user-space tooling and not a security boundary." -ForegroundColor Yellow

    if (-not $script:ScoopCommand) {
        $script:ScoopCommand = Get-Command scoop -CommandType ExternalScript, Application | Select-Object -First 1
    }

    if ($script:ScoopCommand) {
        & $script:ScoopCommand @args
    } else {
        Write-Error "Scoop executable not found in PATH."
    }
}

function uv {
    Write-Host "Note: uv installs Python tools and dependencies from public package repositories (e.g., PyPI); it is developer tooling and not a security boundary." -ForegroundColor Yellow

    if (-not $script:UvCommand) {
        $script:UvCommand = Get-Command uv -CommandType Application | Select-Object -First 1
    }

    if ($script:UvCommand) {
        & $script:UvCommand @args
    } else {
        Write-Error "uv not found in PATH."
    }
}

function python {
    Write-Host "REMINDER: Using 'uv run' for project-isolated execution." -ForegroundColor Cyan
    uv run python @args
}

function python3 {
    Write-Host "REMINDER: Using 'uv run' for project-isolated execution." -ForegroundColor Cyan
    uv run python @args
}

function pip {
    if ($args -contains "install") {
        Write-Host "ADVICE: For projects with a pyproject.toml, use 'uv add <package>' instead of pip install." -ForegroundColor Cyan
    } else {
        Write-Host "STOP: Use 'uv add' (projects) or 'uv pip' (pip-compat) to manage dependencies." -ForegroundColor Yellow
    }
    Write-Host ("Executing: uv pip {0}" -f ($args -join ' ')) -ForegroundColor Gray
    uv pip @args
}

# Ensure VS Code 'code' command is always available from CLI
if (!(Get-Command code -ErrorAction SilentlyContinue)) {
    $shim = Join-Path $env:USERPROFILE "scoop\shims\code.cmd"
    if (Test-Path $shim) { Set-Alias -Name code -Value $shim }
}

if (-not $global:DevTrustNoticeShown) {
    Write-Host "------------------------------------------------------"
    Write-Host "Note: Scoop installs developer tools from public GitHub repositories; it is user-space tooling and not a security boundary." -ForegroundColor Yellow
    Write-Host "Note: uv installs Python tools and dependencies from public package repositories (e.g., PyPI); it is developer tooling and not a security boundary." -ForegroundColor Yellow
    Write-Host "------------------------------------------------------"
    $global:DevTrustNoticeShown = $true
}

# --- End Core Dev Bootstrap (2026) ---
'@

        $markerBegin = "# --- Core Dev Bootstrap (2026) ---"
        $markerEnd = "# --- End Core Dev Bootstrap (2026) ---"

        foreach ($p in Get-TargetProfilePaths) {
            $dir = Split-Path -Path $p
            New-Item -ItemType Directory -Path $dir -Force | Out-Null

            if (Test-Path $p) {
                $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
                Copy-Item -Path $p -Destination ($p + ".bak_" + $stamp) -Force
                $content = Get-Content -Path $p
            } else {
                New-Item -Type File -Path $p -Force | Out-Null
                $content = @()
            }

            # Find existing block to make the script safely re-runnable for updates
            $beginLine = -1
            $endLine = -1
            for ($i = 0; $i -lt $content.Length; $i++) {
                if ($content[$i] -match '--- Core Dev Bootstrap \(') { $beginLine = $i }
                if ($content[$i] -match '--- End Core Dev Bootstrap \(') { $endLine = $i }
            }

            if ($beginLine -ge 0 -and $endLine -gt $beginLine) {
                Write-Host "Updating existing bootstrap block in profile: $p" -ForegroundColor Gray
                $preBlock = $content[0..($beginLine-1)]
                $postBlock = $content[($endLine+1)..$content.GetUpperBound(0)]
                ($preBlock + $ProfileBlock.Split([System.Environment]::NewLine) + $postBlock) | Set-Content -Path $p
            } else {
                Add-Content -Path $p -Value "`n$ProfileBlock`n"
                Write-Host "Adding new bootstrap block to profile: $p" -ForegroundColor Gray
            }
        }
    } else {
        Write-Host "[3/3] Skipping Profile modification (no -InstallProfile flag)." -ForegroundColor Gray
    }

    Write-Host "`n======================================================" -ForegroundColor Cyan
    Write-Host "                MODERN WORKFLOW GUIDE" -ForegroundColor Cyan
    Write-Host "======================================================" -ForegroundColor Cyan
    Write-Host "Your environment is now managed by 'uv'. Forget manual pip/conda install."
    Write-Host ""
    Write-Host "1. START A PROJECT:  'uv init my-project' then 'cd my-project'"
    Write-Host "2. ADD PACKAGES:     'uv add pandas scikit-learn matplotlib'"
    Write-Host "3. RUN SCRIPTS:      'uv run main.py' (Automatically manages venv)"
    Write-Host "4. VS CODE:          'code .' (Select the '.venv' interpreter inside VS Code)"
    Write-Host "5. UPDATE TOOLS:     'scoop update *' updates your entire dev stack"
    Write-Host "------------------------------------------------------"
    Write-Host "BOOTSTRAP COMPLETE!" -ForegroundColor Green
    Write-Host "Next step: Restart your terminal (recommended), or run '. `$PROFILE' once to apply the updated PowerShell profile and activate the new dev workflow." -ForegroundColor Cyan

}
catch {
    Write-Host "`nAn error occurred: $($_.Exception.Message)" -ForegroundColor Red
    throw
}
finally {
    if ($TranscriptPath) {
        try { Stop-Transcript | Out-Null } catch { }
    }
}