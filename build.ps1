<#
.SYNOPSIS
Builds the Windows distribution of VPN Launcher: a PyInstaller folder and an Inno Setup installer.

.DESCRIPTION
Output:
  dist\VPN Launcher\                 runnable folder
  dist\VpnLauncherSetup-<ver>.exe    installer (needs Inno Setup 6; skipped with a warning when it is missing)

No profiles, settings or keys are bundled: the program keeps its data in %APPDATA%\VpnLauncher.

.EXAMPLE
.\build.ps1 -Version 1.0.0

.EXAMPLE
# Use an interpreter other than the checkout's own virtual environment.
.\build.ps1 -Version 1.0.0 -Python C:\tools\venv\Scripts\python.exe
#>
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version,
    # Needs the packages from requirements.txt.
    [string]$Python = (Join-Path $PSScriptRoot ".venv\Scripts\python.exe")
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$source = "$here\src\vpn_launcher.py"
$script = "$here\src\vpn_connect_op.ps1"
$icon = "$here\assets\app.ico"  # from mancubus-lab-brand-assets: tool-vpn-launcher.ico

foreach ($path in $Python, $source, $script, $icon) {
    if (-not (Test-Path $path)) { throw "Missing: $path" }
}

# The program reads its version from this file (About window, update check).
New-Item -ItemType Directory -Force "$here\build" | Out-Null
[IO.File]::WriteAllText("$here\build\version.txt", $Version)

& $Python -m PyInstaller --noconfirm --clean --windowed --name "VPN Launcher" --icon $icon `
    --add-data "$script;." --add-data "$here\build\version.txt;." --add-data "$here\assets\mancubus-lab.png;." `
    --add-data "$here\assets\app.ico;." --add-data "$here\assets\icons;icons" --add-data "$here\assets\tokens.json;." `
    --collect-data sv_ttk `
    --distpath "$here\dist" --workpath "$here\build" --specpath "$here\build" $source
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    Write-Warning "Inno Setup 6 was not found: only the folder dist\VPN Launcher was built."
    return
}
& $iscc "/DAppVersion=$Version" "/DIconFile=$icon" "$here\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
"Built: $here\dist\VpnLauncherSetup-$Version.exe"
