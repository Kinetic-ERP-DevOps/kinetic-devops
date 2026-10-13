[CmdletBinding()]
param(
    [string]$Remote = "public",
    [Parameter(Mandatory = $true)][string]$Repo,
    [string]$ConfigPath = ".\scripts\pr_branches.json",
    [string]$ExampleConfigPath = ".\scripts\pr_branches.example.json",
    [switch]$Ready,
    [switch]$PushOnly,
    [switch]$IndependentOnly,
    [switch]$InitConfig,
    [switch]$KeepDraftFiles
)

$ErrorActionPreference = "Stop"

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Ensure-BodyFile {
    param(
        [string]$Path,
        [string]$Title,
        [string]$Body
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        $content = @(
            "# Title",
            $Title,
            "",
            "# Body",
            $Body
        ) -join "`r`n"
        Set-Content -LiteralPath $Path -Value $content -Encoding UTF8
    }
}

function Read-TitleAndBody {
    param([string]$Path)

    $lines = Get-Content -LiteralPath $Path
    $titleIndex = $lines.IndexOf("# Title")
    $bodyIndex = $lines.IndexOf("# Body")

    if ($titleIndex -lt 0 -or $bodyIndex -lt 0 -or $bodyIndex -le $titleIndex) {
        throw "Invalid PR draft format in $Path"
    }

    $title = ($lines[($titleIndex + 1)..($bodyIndex - 1)] -join "`n").Trim()
    $body = ($lines[($bodyIndex + 1)..($lines.Count - 1)] -join "`n").Trim()

    if ([string]::IsNullOrWhiteSpace($title)) {
        throw "PR title is empty in $Path"
    }

    return @{ Title = $title; Body = $body }
}

function Remove-DraftArtifacts {
    param([string]$Dir)
    if (Test-Path -LiteralPath $Dir) {
        Remove-Item -LiteralPath $Dir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Test-GhAuth {
    $null = gh auth status 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Initialize-ConfigFromExample {
    param(
        [string]$SourcePath,
        [string]$TargetPath
    )

    if (-not (Test-Path -LiteralPath $SourcePath)) {
        throw "Example config not found: $SourcePath"
    }
    if (Test-Path -LiteralPath $TargetPath) {
        throw "Target config already exists: $TargetPath"
    }

    $targetDir = Split-Path -Parent $TargetPath
    if ($targetDir -and -not (Test-Path -LiteralPath $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }

    Copy-Item -LiteralPath $SourcePath -Destination $TargetPath -Force
    Write-Host "Created PR config from example: $TargetPath"
}

function Load-BranchesConfig {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        $raw = Get-Content -LiteralPath $Path -Raw
        $loaded = $raw | ConvertFrom-Json
        if (-not $loaded) {
            throw "PR config exists but is empty: $Path"
        }
        return @($loaded)
    }

    throw "PR config not found at $Path. Run with -InitConfig to scaffold from the example config."
}

function Validate-BranchesConfig {
    param([array]$Branches)

    if (-not $Branches -or $Branches.Count -eq 0) {
        throw "PR config contains no entries."
    }

    $required = @("Head", "Base", "Title", "Body")
    $seen = @{}

    for ($i = 0; $i -lt $Branches.Count; $i++) {
        $entry = $Branches[$i]
        $idx = $i + 1

        foreach ($key in $required) {
            if (-not ($entry.PSObject.Properties.Name -contains $key)) {
                throw "Config entry #$idx is missing required key '$key'."
            }
            $value = [string]$entry.$key
            if ([string]::IsNullOrWhiteSpace($value)) {
                throw "Config entry #$idx has empty value for '$key'."
            }
        }

        $head = [string]$entry.Head
        $base = [string]$entry.Base

        if ($head -eq $base) {
            throw "Config entry #$idx has Head equal to Base ('$head')."
        }
        if ($seen.ContainsKey($head)) {
            throw "Duplicate Head found in config: '$head'."
        }
        $seen[$head] = $true
    }
}

if ($InitConfig) {
    Initialize-ConfigFromExample -SourcePath $ExampleConfigPath -TargetPath $ConfigPath
    exit 0
}

if (-not (Test-CommandExists "gh")) {
    throw "GitHub CLI (gh) is required. Install it first."
}

if (-not $PushOnly) {
    if (-not (Test-GhAuth)) {
        throw "GitHub CLI is not authenticated. Run 'gh auth login' before creating PRs."
    }
}

$branches = Load-BranchesConfig -Path $ConfigPath
Validate-BranchesConfig -Branches $branches
if ($IndependentOnly) {
    $heads = @($branches | ForEach-Object { $_.Head })
    $branches = @($branches | Where-Object { $_.Base -eq "main" -or -not ($heads -contains $_.Base) })
}

$draftDir = Join-Path (Get-Location) "temp/pr_drafts"
New-Item -ItemType Directory -Path $draftDir -Force | Out-Null

$createAsDraft = -not $Ready.IsPresent

Write-Host "Using remote: $Remote"
Write-Host "Using repo:   $Repo"
Write-Host "Config path:  $ConfigPath"
Write-Host "Example cfg:  $ExampleConfigPath"
Write-Host "Draft mode:   $createAsDraft"
Write-Host "Push only:    $($PushOnly.IsPresent)"
Write-Host "Independent:  $($IndependentOnly.IsPresent)"
Write-Host "Keep drafts:  $($KeepDraftFiles.IsPresent)"
Write-Host ""

try {
    foreach ($pr in $branches) {
        $head = $pr.Head
        $base = $pr.Base
        $title = $pr.Title
        $body = $pr.Body

        Write-Host "============================================================"
        Write-Host "Head: $head"
        Write-Host "Base: $base"

        $branchExists = git show-ref --verify --quiet "refs/heads/$head"; $existsCode = $LASTEXITCODE
        if ($existsCode -ne 0) {
            Write-Warning "Skipping: local branch not found: $head"
            continue
        }

        git push -u $Remote $head | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Push failed for $head (exit=$LASTEXITCODE). Skipping PR creation."
            continue
        }

        if ($PushOnly) {
            continue
        }

        $draftPath = Join-Path $draftDir (($head -replace "/", "_") + ".md")
        Ensure-BodyFile -Path $draftPath -Title $title -Body $body

        if (Test-CommandExists "code") {
            code --wait $draftPath
        } else {
            Write-Host "VS Code CLI not found; opening Notepad for review."
            Start-Process notepad.exe $draftPath -Wait
        }

        $existingPr = gh pr list --repo $Repo --head $head --json number --jq ".[0].number" 2>$null
        if ($existingPr) {
            Write-Host "PR already exists for ${head}: #$existingPr"
            continue
        }

        $parsed = Read-TitleAndBody -Path $draftPath

        $confirm = Read-Host "Create PR for $head now? (y/n)"
        if ($confirm -ne "y") {
            Write-Host "Skipped PR creation for $head"
            continue
        }

        $bodyOnlyPath = Join-Path $draftDir (($head -replace "/", "_") + ".body.md")
        Set-Content -LiteralPath $bodyOnlyPath -Value $parsed.Body -Encoding UTF8

        $args = @(
            "pr", "create",
            "--repo", $Repo,
            "--base", $base,
            "--head", $head,
            "--title", $parsed.Title,
            "--body-file", $bodyOnlyPath
        )
        if ($createAsDraft) {
            $args += "--draft"
        }

        gh @args | Out-Host
    }
}
finally {
    if (-not $KeepDraftFiles -and -not $PushOnly) {
        Remove-DraftArtifacts -Dir $draftDir
        Write-Host "Removed temp draft files: $draftDir"
    }
}

Write-Host ""
if ($KeepDraftFiles -or $PushOnly) {
    Write-Host "Done. PR drafts are in: $draftDir"
} else {
    Write-Host "Done."
}
