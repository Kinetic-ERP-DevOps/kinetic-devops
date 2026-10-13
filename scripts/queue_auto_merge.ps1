[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Repo,
    [string]$ConfigPath = ".\scripts\pr_branches.json",
    [switch]$IndependentOnly,
    [switch]$MarkReady,
    [switch]$Execute
)

$ErrorActionPreference = "Stop"

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Load-BranchesConfig {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "PR config not found at $Path"
    }
    $raw = Get-Content -LiteralPath $Path -Raw
    $loaded = $raw | ConvertFrom-Json
    if (-not $loaded) {
        throw "PR config exists but is empty: $Path"
    }
    return @($loaded)
}

function Get-PrNumberByHead {
    param(
        [string]$Repo,
        [string]$Head
    )
    $num = gh pr list --repo $Repo --head $Head --json number --jq ".[0].number" 2>$null
    if ([string]::IsNullOrWhiteSpace($num)) {
        return $null
    }
    return [int]$num
}

function Get-PrIsDraft {
    param(
        [string]$Repo,
        [int]$Number
    )
    $draft = gh pr view $Number --repo $Repo --json isDraft --jq ".isDraft" 2>$null
    return ($draft -eq "true")
}

if (-not (Test-CommandExists "gh")) {
    throw "GitHub CLI (gh) is required. Install it first."
}

$null = gh auth status 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "GitHub CLI is not authenticated. Run 'gh auth login' first."
}

$branches = Load-BranchesConfig -Path $ConfigPath
if ($IndependentOnly) {
    $heads = @($branches | ForEach-Object { $_.Head })
    $branches = @($branches | Where-Object { $_.Base -eq "main" -or -not ($heads -contains $_.Base) })
}

Write-Host "Repo: $Repo"
Write-Host "Config: $ConfigPath"
Write-Host "Execute: $($Execute.IsPresent)"
Write-Host "Independent only: $($IndependentOnly.IsPresent)"
Write-Host "Mark draft PRs ready: $($MarkReady.IsPresent)"
Write-Host ""

foreach ($item in $branches) {
    $head = [string]$item.Head
    $base = [string]$item.Base

    $prNumber = Get-PrNumberByHead -Repo $Repo -Head $head
    if (-not $prNumber) {
        Write-Warning "No open PR found for head '$head'. Skipping."
        continue
    }

    Write-Host "PR #$prNumber | $head -> $base"

    $isDraft = Get-PrIsDraft -Repo $Repo -Number $prNumber

    if (-not $Execute) {
        if ($isDraft) {
            if ($MarkReady) {
                Write-Host "  Dry run: gh pr ready $prNumber --repo $Repo"
            } else {
                Write-Host "  Draft PR detected; would skip unless -MarkReady is used."
            }
        }
        Write-Host "  Dry run: gh pr merge $prNumber --repo $Repo --squash --delete-branch --auto"
        continue
    }

    if ($isDraft) {
        if (-not $MarkReady) {
            Write-Warning "  PR #$prNumber is draft. Skipping. Re-run with -MarkReady to auto-ready draft PRs."
            continue
        }

        gh pr ready $prNumber --repo $Repo | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "  Failed to mark PR #$prNumber ready for review. Skipping."
            continue
        }
    }

    gh pr merge $prNumber --repo $Repo --squash --delete-branch --auto | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "  Failed to queue auto-merge for PR #$prNumber"
    } else {
        Write-Host "  Auto-merge queued for PR #$prNumber"
    }
}

if (-not $Execute) {
    Write-Host ""
    Write-Host "Dry run complete. Re-run with -Execute to queue auto-merge."
}
