# install.ps1 — first-time Windows setup + Desktop shortcut.
#
# Run once after cloning:
#   .\scripts\install.ps1
#
# Creates a venv, installs Lukav, and drops a "Lukav" shortcut on the
# Desktop pointing at scripts\Lukav.bat. After that, double-click the
# Desktop icon to launch.
#
# If PowerShell blocks the script, run once in the current session:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $repo

Write-Host "[lukav] repo: $repo"

# 1. venv
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[lukav] creating venv..."
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { py -3 -m venv .venv } else { python -m venv .venv }
}

# 2. install
Write-Host "[lukav] installing deps..."
& ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& ".venv\Scripts\python.exe" -m pip install --quiet -e ".[plaid,secrets,desktop]"

# 3. Desktop shortcut -> Lukav.bat
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcut = Join-Path $desktop "Lukav.lnk"
$target = Join-Path $repo "scripts\Lukav.bat"
$icon = Join-Path $repo "scripts\Lukav.bat"

$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($shortcut)
$lnk.TargetPath = $target
$lnk.WorkingDirectory = $repo
$lnk.IconLocation = "$icon, 0"
$lnk.Description = "Lukav — personal credit-card debt auditor"
$lnk.Save()

Write-Host ""
Write-Host "[lukav] install complete."
Write-Host "[lukav] shortcut: $shortcut"
Write-Host "[lukav] double-click it to launch."
