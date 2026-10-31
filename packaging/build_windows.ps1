# Builds dist\SehatDesk-Setup-<version>.exe. Run on Windows from anywhere:
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# GitHub runs this through .github/workflows/windows-release.yml, with -SkipInstall.
# Needs: Python 3.11 on PATH and Inno Setup 6.
# PDF export: install MSYS2 and run `pacman -S mingw-w64-x86_64-pango` to bundle its libraries.
# The build fails without them, so no installer ships without PDF download.
param([switch]$SkipInstall)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$version = (Get-Content "packaging\VERSION" -Raw).Trim()

function Run($exe) {
    & $exe @args
    if ($LASTEXITCODE -ne 0) { throw "$exe $args failed with exit code $LASTEXITCODE" }
}

if (-not $SkipInstall) {
    Write-Host "== Dependencies"
    Run python -m pip install --quiet -r requirements.txt -r requirements-build.txt
}

Write-Host "== Static files"
Run python manage.py collectstatic --noinput --clear --verbosity 0

Write-Host "== sehat_desk.exe"
if (Test-Path "dist\sehat_desk") { Remove-Item -Recurse -Force "dist\sehat_desk" }
Run python -m PyInstaller --noconfirm --clean --distpath dist --workpath build "packaging\sehat_desk.spec"
$exe = "dist\sehat_desk\sehat_desk.exe"

$msys = "C:\msys64\mingw64\bin"
if (Test-Path "$msys\libpango-1.0-0.dll") {
    Write-Host "== PDF libraries"
    $gtk = "dist\sehat_desk\_internal\gtk"
    New-Item -ItemType Directory -Force $gtk | Out-Null
    Copy-Item "$msys\*.dll" $gtk
    Copy-Item "packaging\fonts.conf" $gtk
} else {
    throw "MSYS2 Pango not found: install it so this build can make PDFs."
}

Write-Host "== Checking the bundle"
$work = Join-Path $env:TEMP ("sehatdesk-check-" + [guid]::NewGuid())
New-Item -ItemType Directory $work | Out-Null
$env:SEHATDESK_DATA_DIR = $work
try {
    $report = Join-Path $work "self-test.json"
    $proc = Start-Process $exe -ArgumentList "--self-test", "`"$report`"" -Wait -PassThru
    Get-Content $report | Write-Host
    if ($proc.ExitCode -ne 0) { throw "Self-test failed" }

    # A real start on an empty data folder: migrations, then the health page.
    $server = Start-Process $exe -ArgumentList "--server", "--port", "18765" -PassThru
    $healthy = $false
    foreach ($i in 1..120) {
        Start-Sleep -Seconds 2
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:18765/health/ready/" -TimeoutSec 3
            if ($health.database -eq "ok") { $healthy = $true; break }
        } catch {}
        if ($server.HasExited) { break }
    }
    if (-not $healthy) {
        Get-Content (Join-Path $work "logs\server.log") -ErrorAction SilentlyContinue | Write-Host
        throw "The server did not become healthy"
    }
    Stop-Process $server -Force
    Write-Host "Server started and answered."
} finally {
    Remove-Item Env:\SEHATDESK_DATA_DIR
    Get-Process sehat_desk -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}

Write-Host "== Installer"
$iscc = @("$env:ProgramFiles (x86)\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 is not installed (https://jrsoftware.org/isdl.php)" }
Run $iscc "/DAppVersion=$version" "packaging\installer.iss"

$installer = "dist\SehatDesk-Setup-$version.exe"
(Get-FileHash $installer -Algorithm SHA256).Hash | Set-Content "$installer.sha256"
Write-Host "Done: $installer"
