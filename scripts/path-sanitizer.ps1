# 1. Get raw strings from Registry
$sysRaw = [Environment]::GetEnvironmentVariable("Path", "Machine")
$userRaw = [Environment]::GetEnvironmentVariable("Path", "User")

# 2. Convert to clean arrays
$sysList = $sysRaw.Split(';', [System.StringSplitOptions]::RemoveEmptyEntries) | ForEach-Object { $_.Trim().TrimEnd('\') }
$userList = $userRaw.Split(';', [System.StringSplitOptions]::RemoveEmptyEntries) | ForEach-Object { $_.Trim().TrimEnd('\') }

# 3. Logic
$redundant = $userList | Where-Object { $sysList -contains $_ }
$uniqueUser = $userList | Where-Object { $sysList -notcontains $_ }

# --- Results ---
Write-Host "--- PATH AUDIT RESULTS ---" -ForegroundColor Cyan

if ($redundant) {
    Write-Host "[!] Found in BOTH System and User paths (Redundant):" -ForegroundColor Yellow
    foreach ($path in $redundant) { Write-Host "  -> $path" }
} else {
    Write-Host "[OK] No redundant overlaps found." -ForegroundColor Green
}

Write-Host "`n[+] Unique User-specific paths (Keep these):" -ForegroundColor Blue
foreach ($path in $uniqueUser) { Write-Host "  -> $path" }

# 1. Get the lists again
$sysRaw = [Environment]::GetEnvironmentVariable("Path", "Machine")
$userRaw = [Environment]::GetEnvironmentVariable("Path", "User")

# 2. Filter out the junk (duplicates, empties, and trailing slashes)
$sysList = $sysRaw.Split(';', [System.StringSplitOptions]::RemoveEmptyEntries) | ForEach-Object { $_.Trim().TrimEnd('\') }
$userList = $userRaw.Split(';', [System.StringSplitOptions]::RemoveEmptyEntries) | ForEach-Object { $_.Trim().TrimEnd('\') }

# 3. Create the "Lean" User Path (Only keep items NOT in System Path)
$cleanUserPathArray = $userList | Where-Object { $sysList -notcontains $_ } | Select-Object -Unique
$cleanUserPathString = $cleanUserPathArray -join ';'

# 4. PERFORM THE UPDATE (The "Surgery")
[Environment]::SetEnvironmentVariable("Path", $cleanUserPathString, "User")

Write-Host "--- SUCCESS ---" -ForegroundColor Green
Write-Host "Your User Path has been cleaned of System duplicates."
Write-Host "New User Path: $cleanUserPathString" -ForegroundColor Cyan
Write-Host "`n[!] IMPORTANT: You must RESTART your terminal (or VS Code) for changes to take effect." -ForegroundColor Yellow


$userRaw = [Environment]::GetEnvironmentVariable("Path", "User")
$userList = $userRaw.Split(';', [System.StringSplitOptions]::RemoveEmptyEntries)

Write-Host "--- VALIDATING USER PATHS ---" -ForegroundColor Cyan
foreach ($path in $userList) {
    if (Test-Path $path) {
        Write-Host "[OK] $path" -ForegroundColor Green
    } else {
        Write-Host "[MISSING] $path" -ForegroundColor Red
    }
}