[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Repo,
    [string]$Branch = "main",
    [string[]]$RequiredChecks = @(),
    [int]$RequiredApprovals = 1,
    [ValidateSet("standard", "solo")]
    [string]$Mode = "standard",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

if (-not (Test-CommandExists "gh")) {
    throw "GitHub CLI (gh) is required. Install it first."
}

$null = gh auth status 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "GitHub CLI is not authenticated. Run 'gh auth login' first."
}

$requiredStatusChecks = $null
if ($RequiredChecks.Count -gt 0) {
    $requiredStatusChecks = @{
        strict = $true
        contexts = $RequiredChecks
    }
}

$effectiveApprovals = $RequiredApprovals
if ($Mode -eq "solo") {
    $effectiveApprovals = 0
}

$payload = @{
    required_status_checks = $requiredStatusChecks
    enforce_admins = $true
    required_pull_request_reviews = @{
        dismiss_stale_reviews = $true
        require_code_owner_reviews = $false
        required_approving_review_count = $effectiveApprovals
        require_last_push_approval = $false
    }
    restrictions = $null
    required_linear_history = $false
    allow_force_pushes = $false
    allow_deletions = $false
    block_creations = $false
    required_conversation_resolution = $true
    lock_branch = $false
    allow_fork_syncing = $false
} | ConvertTo-Json -Depth 6

Write-Host "Repo:   $Repo"
Write-Host "Branch: $Branch"
Write-Host "Mode:   $Mode"
Write-Host "Require approvals: $effectiveApprovals"
if ($RequiredChecks.Count -gt 0) {
    Write-Host "Required checks: $($RequiredChecks -join ', ')"
} else {
    Write-Host "Required checks: <none configured by script>"
}
if ($Mode -eq "solo") {
    Write-Host "NOTE: Solo mode allows merging without reviewer approval. Restore standard mode after merge window."
}
Write-Host ""

if (-not $Apply) {
    Write-Host "Dry run only. Use -Apply to enforce branch protection."
    Write-Host "Payload preview:"
    Write-Host $payload
    exit 0
}

$endpoint = "repos/$Repo/branches/$Branch/protection"
Write-Host "Applying protection via: $endpoint"
$payload | gh api --method PUT --input - --header "Accept: application/vnd.github+json" $endpoint | Out-Host

if ($LASTEXITCODE -ne 0) {
    throw "Failed to apply branch protection. Check the error message above."
}

Write-Host "Branch protection applied."
