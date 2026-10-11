<#
.SYNOPSIS
    Builds both FIFA .exe bundles and the distributable zips.

.EXAMPLE
    .\build.ps1                     build both
    .\build.ps1 -Only Calculate     build one
    .\build.ps1 -DebugConsole       console window, for diagnosing a frozen crash
#>
[CmdletBinding()]
param(
    [ValidateSet('Both', 'Calibrate', 'Calculate')] [string]$Only = 'Both',
    [switch]$DebugConsole,
    [switch]$SkipZip
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPy = Join-Path $Root '.venv\Scripts\python.exe'
$Dist = Join-Path $Root 'dist'
$Build = Join-Path $Root 'build'
$Stamp = Get-Date -Format 'yyyyMMdd'

function Info($m) { Write-Host "[ .. ] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Die($m)  { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }

function Invoke-Native {
    # PyInstaller and pip write progress to stderr. In Windows PowerShell 5.1
    # that becomes a NativeCommandError, which $ErrorActionPreference='Stop'
    # turns into an abort even when the command succeeded. Stringify the stream
    # and judge the result by $LASTEXITCODE instead.
    param([Parameter(Mandatory)][string]$Exe, [string[]]$Arguments, [switch]$Quiet)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object {
            if (-not $Quiet) { Write-Host "       $_" -ForegroundColor DarkGray }
        }
    } finally {
        $ErrorActionPreference = $previous
    }
}

if (-not (Test-Path $VenvPy)) { Die "No .venv - run install.bat -Build first." }
Invoke-Native $VenvPy @('-c', 'import PyInstaller') -Quiet
if ($LASTEXITCODE -ne 0) { Die "PyInstaller is missing - run: .\install.bat -Build" }

$versionOut = & $VenvPy -c "import fifa_gui;print(fifa_gui.__version__)"
$Version = (@($versionOut)[0]).Trim()
Info "Building version $Version"

if ($DebugConsole) {
    $env:FIFA_BUILD_CONSOLE = '1'
    Info "Console builds enabled (tracebacks will be visible)"
} else {
    Remove-Item Env:\FIFA_BUILD_CONSOLE -ErrorAction SilentlyContinue
}

# Always clean: a stale build\ cache after an excludes change silently produces
# a bundle that only works on this machine.
foreach ($dir in @($Build, $Dist)) {
    if (Test-Path $dir) {
        Info "Cleaning $dir"
        try {
            Remove-Item -Recurse -Force $dir
        } catch {
            # Antivirus sometimes holds a handle on a just-built exe.
            Start-Sleep -Seconds 3
            Remove-Item -Recurse -Force $dir
        }
    }
}

function Build-App([string]$Spec, [string]$Name) {
    Info "Building $Name from $Spec ..."
    Invoke-Native $VenvPy @('-m', 'PyInstaller', '--noconfirm', '--clean',
                            '--log-level=WARN', (Join-Path $Root $Spec))
    if ($LASTEXITCODE -ne 0) { Die "$Name build failed." }
    $outDir = Join-Path $Dist $Name
    if (-not (Test-Path (Join-Path $outDir "$Name.exe"))) { Die "$Name.exe was not produced." }
    $mb = [math]::Round((Get-ChildItem $outDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
    Ok "$Name -> $outDir  ($mb MB)"
    return $outDir
}

if ($Only -in @('Both', 'Calculate')) {
    $out = Build-App 'fifa_calculate.spec' 'FIFA Calculate'
    # Nothing from the dev tree should have leaked in.
    $big = Get-ChildItem $out -Recurse -File | Where-Object { $_.Length -gt 40MB }
    if ($big) { Die "Unexpectedly large files in the bundle: $($big.Name -join ', ')" }
    $tk = Get-ChildItem $out -Recurse -Filter 'tcl*.dll' -ErrorAction SilentlyContinue
    if ($tk) { Die "Tk leaked into the bundle: $($tk.Name -join ', ')" }
}

if ($Only -in @('Both', 'Calibrate')) {
    $out = Build-App 'fifa_calibrate.spec' 'FIFA Calibrate'

    # Put ASICamera2.dll next to the exe so a user can drop in a newer SDK by
    # overwriting one file. The bundled copy under _internal\vendor\zwo stays
    # as the fallback (see fifa_gui/hardware/asi_loader.py for the search order).
    $asi = Join-Path $Root 'vendor\zwo\ASICamera2.dll'
    if (-not (Test-Path $asi)) {
        Die "Missing $asi`n       It is ZWO's SDK binary and is not kept in this repository.`n       Run:  .\tools\fetch_asi_dll.ps1"
    }
    Copy-Item $asi $out -Force
    Ok "ASICamera2.dll placed next to the exe"

    # The stage DLLs are package data, so assert they really made it in.
    $lx = Join-Path $out '_internal\libximc\library-files\win64\libximc.dll'
    if (-not (Test-Path $lx)) { Die "libximc DLLs are MISSING from the bundle: expected $lx" }
    Ok "libximc win64 DLLs present in the bundle"
}

if (-not $SkipZip) {
    foreach ($app in @('FIFA Calculate', 'FIFA Calibrate')) {
        $src = Join-Path $Dist $app
        if (-not (Test-Path $src)) { continue }
        $zip = Join-Path $Dist ("{0}-{1}-{2}-win64.zip" -f ($app -replace ' ', '-'), $Version, $Stamp)
        Info "Zipping $app ..."
        Compress-Archive -Path $src -DestinationPath $zip -CompressionLevel Optimal -Force
        $mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
        Ok "$([System.IO.Path]::GetFileName($zip))  ($mb MB)"
    }
}

Write-Host ""
Ok "Done. Output in $Dist"
Write-Host "  Extract the whole folder before running the exe - launching it from" -ForegroundColor Gray
Write-Host "  inside Explorer's zip preview fails, because _internal is left behind." -ForegroundColor Gray
