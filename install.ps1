<#
.SYNOPSIS
    Creates or updates the .venv for the FIFA GUI applications and verifies it.

.EXAMPLE
    .\install.bat                 double-click, or run from cmd
    .\install.bat -Hardware       lab PC: also install the camera and stage packages
    .\install.bat -Build          build machine: also install PyInstaller and pytest
    .\install.bat -InstallPython  install Python 3.12 with winget if none is found

.NOTES
    If PowerShell refuses with "running scripts is disabled on this system",
    use install.bat -- it passes -ExecutionPolicy Bypass for that one process.
    No admin rights and no machine policy change are needed.
#>
[CmdletBinding()]
param(
    [switch]$Hardware,            # zwoasi + libximc
    [switch]$Build,               # implies -Hardware, adds PyInstaller + pytest
    [switch]$Recreate,            # delete and rebuild .venv from scratch
    [switch]$NoDownload,          # never download the ZWO SDK; fail with instructions
    [switch]$InstallPython,       # install Python 3.12 via winget if none is found
    [string]$Wheelhouse = ""      # offline install from a folder of wheels
)

$ErrorActionPreference = 'Stop'
$Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $Root '.venv'
$VenvPy  = Join-Path $VenvDir 'Scripts\python.exe'

function Info($m) { Write-Host "[ .. ] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }

Write-Host "=== FIFA GUI environment installer ===" -ForegroundColor White
Info "Project root: $Root"

# ------------------------------------------------------------ 1. find Python 3.12
# The probe deliberately contains no quote characters: PowerShell strips embedded
# double quotes when splatting an argument array to a native executable, which
# silently turns the code into a SyntaxError. chr(80) is 'P' for struct.calcsize.
$PY_PROBE = 'import sys,struct;print(sys.version_info[0]*100+sys.version_info[1],sys.version_info[2],struct.calcsize(chr(80))*8)'

function Find-Python {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += ,@('py', @('-3.12')) }
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += ,@('python', @()) }
    $known = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (Test-Path $known) { $candidates += ,@($known, @()) }

    foreach ($c in $candidates) {
        try {
            # NB: not $args -- that is an automatic variable inside a function.
            $probe = $c[1] + @('-c', $PY_PROBE)
            # Capture everything before slicing: piping to Select-Object -First
            # terminates the pipeline early, which kills the native process and
            # leaves $LASTEXITCODE at -1 even though the command succeeded.
            $out = & $c[0] @probe
            if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
            $parts = (@($out)[0]).Trim() -split '\s+'
            $feature = [int]$parts[0]      # 312 for 3.12
            $micro   = $parts[1]
            $bits    = [int]$parts[2]
            $label   = "{0}.{1}.{2}" -f [math]::Floor($feature / 100), ($feature % 100), $micro
            if ($feature -ne 312) {
                Warn "Found Python $label at '$($c[0])' -- this project needs 3.12.x. Still looking."
                continue
            }
            if ($bits -ne 64) {
                Warn "Found a $bits-bit Python $label at '$($c[0])' -- a 64-bit one is required. Still looking."
                continue
            }
            return @{ Exe = $c[0]; Args = $c[1]; Ver = $label }
        } catch { }
    }
    return $null
}

# winget writes the new interpreter to PATH for *future* processes, so this
# one has to re-read the machine and user PATH before it can see it. The
# well-known per-user install directory is probed too, in case PATH was not
# updated at all.
function Update-PathFromRegistry {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Install-Python {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Warn "winget is not available on this machine, so Python cannot be installed automatically."
        return $false
    }
    Info "Installing Python 3.12 (64-bit) with winget. Windows may ask you to approve it ..."
    # --scope user keeps it out of Program Files, so no admin rights are needed.
    & winget install --id Python.Python.3.12 --architecture x64 --scope user `
        --exact --silent --accept-package-agreements --accept-source-agreements
    $rc = $LASTEXITCODE
    # winget reports 'no applicable upgrade' and friends as non-zero; the probe
    # below is the real test, so a non-zero code is a warning, not the end.
    if ($rc -ne 0) { Warn "winget exited with code $rc. Checking for Python anyway." }
    Update-PathFromRegistry
    return $true
}

$py = Find-Python
if (-not $py -and $InstallPython) {
    if (Install-Python) { $py = Find-Python }
    if ($py) { Ok "Python 3.12 installed." }
}
if (-not $py) {
    $hint = if ($InstallPython) {
        "The automatic install did not produce a usable Python 3.12."
    } else {
        "Re-run with -InstallPython to let winget install it for you."
    }
    Die @"
No 64-bit Python 3.12 found.
$hint
Install it by hand from
  https://www.python.org/downloads/release/python-31210/
ticking 'Add python.exe to PATH' and 'py launcher', then run this script again.
Python 3.13+ is not supported by this pinned dependency set, and a 32-bit
Python cannot load the camera and stage DLLs, which are x64 only.
If winget has just installed it, a new terminal may be needed for PATH to
carry over.
"@
}
Ok "Using Python $($py.Ver) 64-bit  ($($py.Exe) $($py.Args))"

# ------------------------------------------------------------------- 2. the venv
if ($Recreate -and (Test-Path $VenvDir)) {
    Info "Removing the existing .venv (-Recreate) ..."
    Remove-Item -Recurse -Force $VenvDir
}

if (Test-Path $VenvPy) {
    & $VenvPy -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Ok "Reusing the existing .venv"
    } else {
        Warn ".venv exists but its python.exe does not run. Recreating it."
        Remove-Item -Recurse -Force $VenvDir
    }
}
if (-not (Test-Path $VenvPy)) {
    Info "Creating .venv ..."
    $venvProbe = $py.Args + @('-m', 'venv', $VenvDir)
    & $py.Exe @venvProbe
    if ($LASTEXITCODE -ne 0) { Die "venv creation failed." }
    Ok ".venv created"
}

# ------------------------------------------------------------ 3. install packages
Info "Upgrading pip, setuptools and wheel ..."
& $VenvPy -m pip install --upgrade pip setuptools wheel --disable-pip-version-check --quiet
if ($LASTEXITCODE -ne 0) { Die "pip upgrade failed (no internet, or behind a proxy?)." }

if ($Build)          { $reqName = 'requirements-build.txt' }
elseif ($Hardware)   { $reqName = 'requirements-hardware.txt' }
else                 { $reqName = 'requirements.txt' }
$req = Join-Path $Root $reqName
if (-not (Test-Path $req)) { Die "Missing $req" }

$pipExtra = @()
if ($Wheelhouse) {
    if (-not (Test-Path $Wheelhouse)) { Die "Wheelhouse not found: $Wheelhouse" }
    $pipExtra = @('--no-index', '--find-links', $Wheelhouse)
    Info "Offline install from $Wheelhouse"
}

Info "Installing from $reqName  (~500 MB to download the first time -- be patient) ..."
& $VenvPy -m pip install @pipExtra -r $req
if ($LASTEXITCODE -ne 0) {
    Die @"
Dependency installation failed. The usual causes:
  * no internet, or a proxy      -> set HTTP_PROXY / HTTPS_PROXY
  * corporate TLS interception   -> pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org
  * no internet at all           -> on a connected machine run
                                      pip download -r $reqName -d wheelhouse
                                    copy the folder over, then
                                      .\install.bat -Wheelhouse <folder>
  * both PySide6 and PySide6-Essentials installed
                                 -> .venv\Scripts\python -m pip uninstall -y PySide6 PySide6-Addons
pip's own error is above.
"@
}

Info "Installing fifa_gui in editable mode ..."
& $VenvPy -m pip install -e $Root --no-deps --quiet
if ($LASTEXITCODE -ne 0) { Warn "Editable install failed; the apps will still run from the project root." }

# ------------------------------------------------- 4. the ZWO camera DLL
# ASICamera2.dll is ZWO's proprietary SDK binary and is not kept in this
# repository, so it is fetched once per machine.
if ($Hardware -or $Build) {
    Write-Host ""
    & (Join-Path $Root 'tools\fetch_asi_dll.ps1') @(if ($NoDownload) { '-NoDownload' })
    if ($LASTEXITCODE -ne 0) {
        Warn "ASICamera2.dll was not obtained; the camera will not connect and"
        Warn "the Calibrate app cannot be built until it is. See the message above."
    }
}

# --------------------------------------------------------------------- 5. verify
Write-Host ""
Info "Verifying the environment ..."
$verifyArgs = @((Join-Path $Root 'tools\verify_env.py'))
if ($Hardware -or $Build) { $verifyArgs += '--hardware' }
& $VenvPy @verifyArgs
$verifyRc = $LASTEXITCODE

# ------------------------------------------------------------- 6. manual steps
Write-Host ""
Write-Host "--- MANUAL STEPS that pip cannot do --------------------------" -ForegroundColor White
Write-Host @"
Needed only on the lab PC that talks to the hardware:

1) ZWO ASI camera USB driver
   https://www.zwoastro.com/downloads/developers  ->  'ASI Cameras' driver.
   Install it BEFORE plugging in the ASI294MM Pro, then confirm the camera
   appears in Device Manager as an ASI Camera rather than an unknown device.
   The driver is separate from ASICamera2.dll, which this installer fetches.
   Use a direct USB3 port, not a hub: a full RAW16 frame is 93 MB over the bus.

2) Standa / XIMC stage USB driver
   https://files.xisupport.com/  ->  ximc-2.x.x.zip  ->  ximc\win64\
   Device Manager -> the controller -> Update driver -> Browse -> that folder.
   It should then appear as a COM port (the app's URI looks like xi-com:\\.\COM5).
   Confirm it works in Standa's own XILab before blaming this software.

3) Microsoft Visual C++ 2013 Redistributable (x64)   <-- REQUIRED by the stage
   https://aka.ms/highdpimfc2013x64enu
   libximc probes for msvcr120.dll when it is imported and refuses to load
   without it. The check above reports whether you already have it.

4) Microsoft Visual C++ 2015-2022 Redistributable (x64)
   https://aka.ms/vs/17/release/vc_redist.x64.exe
   Needed by CPython 3.12, Qt 6 and the numpy wheels. Usually already present
   on Windows 10/11, but install it on a fresh image.
"@ -ForegroundColor Gray
Write-Host "--------------------------------------------------------------" -ForegroundColor White

if ($verifyRc -ne 0) { Die "Environment verification reported problems (see above)." }

Ok "Environment ready."
Write-Host ""
Write-Host "  Run the apps:   .\.venv\Scripts\python -m fifa_gui.apps.calculate_main" -ForegroundColor White
Write-Host "                  .\.venv\Scripts\python -m fifa_gui.apps.calibrate_main" -ForegroundColor White
Write-Host "  Build the exes: .\build.ps1" -ForegroundColor White
exit 0
