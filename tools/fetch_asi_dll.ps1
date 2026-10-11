<#
.SYNOPSIS
    Puts ASICamera2.dll (x64) into vendor\zwo\, fetching it if necessary.

.DESCRIPTION
    ASICamera2.dll is ZWO's proprietary SDK binary and is deliberately NOT kept
    in this repository, so it has to be obtained once per machine. In order,
    this script will:

      1. do nothing if vendor\zwo\ASICamera2.dll already exists (unless -Force)
      2. take it from -From, which may be the DLL itself, the ASI_Camera_SDK zip,
         the inner ASI_Windows_SDK zip, or an unpacked "ASI SDK" folder
      3. look for an SDK already on this machine
      4. download the SDK from ZWO

.EXAMPLE
    .\tools\fetch_asi_dll.ps1
    .\tools\fetch_asi_dll.ps1 -From C:\Downloads\ASI_Camera_SDK.zip
    .\tools\fetch_asi_dll.ps1 -From "D:\ASI SDK\lib\x64\ASICamera2.dll" -Force

.NOTES
    The download is ~112 MB because ZWO ships the Android, Linux and Windows
    SDKs in one archive; only the 2.9 MB Windows x64 DLL is kept.
#>
[CmdletBinding()]
param(
    [string]$From = "",
    [switch]$Force,
    [switch]$NoDownload,
    # GET only: this endpoint answers HEAD with 404, then redirects to a signed
    # URL serving ASI_Camera_SDK.zip.
    [string]$Url = "https://dl.zwoastro.com/software?app=DeveloperCameraSdk&platform=windows86&region=Overseas"
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Target = Join-Path $Root 'vendor\zwo\ASICamera2.dll'

function Info($m) { Write-Host "[ .. ] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }

function Test-Amd64Dll([string]$Path) {
    # A 32-bit ASICamera2.dll sits right next to the 64-bit one in the SDK, and
    # loading the wrong one gives a baffling "not a valid Win32 application".
    try {
        $fs = [System.IO.File]::OpenRead($Path)
        try {
            $br = New-Object System.IO.BinaryReader($fs)
            $fs.Position = 0x3C
            $peOffset = $br.ReadInt32()
            $fs.Position = $peOffset
            if ($br.ReadUInt32() -ne 0x00004550) { return $false }   # "PE\0\0"
            return ($br.ReadUInt16() -eq 0x8664)                      # IMAGE_FILE_MACHINE_AMD64
        } finally { $fs.Dispose() }
    } catch { return $false }
}

function Save-Dll([byte[]]$Bytes, [string]$Source) {
    # Validate in a temp file first: a rejected -Force replacement must not
    # destroy a perfectly good DLL that was already in place.
    $staging = Join-Path ([System.IO.Path]::GetTempPath()) ("asi_{0}.dll" -f ([guid]::NewGuid().ToString('N')))
    [System.IO.File]::WriteAllBytes($staging, $Bytes)
    try {
        if (-not (Test-Amd64Dll $staging)) {
            Die "The file taken from $Source is not a 64-bit DLL. Use the one from the SDK's lib\x64 folder."
        }
        $dir = Split-Path -Parent $Target
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        Move-Item $staging $Target -Force
    } finally {
        Remove-Item $staging -Force -ErrorAction SilentlyContinue
    }
    Ok ("ASICamera2.dll ({0:N1} MB) written to vendor\zwo\  [from {1}]" -f ($Bytes.Length / 1MB), $Source)
    exit 0
}

# Pull "ASI SDK/lib/x64/ASICamera2.dll" out of an SDK zip, descending through
# the outer archive if this is the combined download.
function Get-DllFromZipBytes([byte[]]$ZipBytes, [string]$Source) {
    Add-Type -AssemblyName System.IO.Compression | Out-Null
    $stream = New-Object System.IO.MemoryStream(, $ZipBytes)
    $zip = New-Object System.IO.Compression.ZipArchive($stream, [System.IO.Compression.ZipArchiveMode]::Read)
    try {
        $entry = $zip.Entries | Where-Object {
            $_.FullName -replace '\\', '/' -match '(?i)lib/x64/ASICamera2\.dll$'
        } | Select-Object -First 1
        if ($entry) {
            $ms = New-Object System.IO.MemoryStream
            $es = $entry.Open(); $es.CopyTo($ms); $es.Dispose()
            return $ms.ToArray()
        }
        # Combined download: ASI_Camera_SDK/ASI_Windows_SDK_Vx.xx.zip
        $inner = $zip.Entries | Where-Object {
            $_.Name -match '(?i)^ASI_Windows_SDK.*\.zip$'
        } | Select-Object -First 1
        if ($inner) {
            Info "Descending into $($inner.Name)"
            $ms = New-Object System.IO.MemoryStream
            $es = $inner.Open(); $es.CopyTo($ms); $es.Dispose()
            return Get-DllFromZipBytes $ms.ToArray() "$Source -> $($inner.Name)"
        }
        return $null
    } finally { $zip.Dispose(); $stream.Dispose() }
}

Write-Host "=== ASICamera2.dll ===" -ForegroundColor White

# ------------------------------------------------------------------ 1. present
if ((Test-Path $Target) -and -not $Force) {
    if (Test-Amd64Dll $Target) {
        Ok ("already present ({0:N1} MB). Use -Force to replace it." -f ((Get-Item $Target).Length / 1MB))
        exit 0
    }
    Warn "vendor\zwo\ASICamera2.dll is not a 64-bit DLL; replacing it."
}

# --------------------------------------------------------------------- 2. -From
if ($From) {
    if (-not (Test-Path $From)) { Die "Not found: $From" }
    $item = Get-Item $From

    if ($item.PSIsContainer) {
        $found = Get-ChildItem $item.FullName -Recurse -Filter 'ASICamera2.dll' -ErrorAction SilentlyContinue |
                 Where-Object { $_.FullName -match '(?i)[\\/]x64[\\/]' } | Select-Object -First 1
        if (-not $found) { Die "No lib\x64\ASICamera2.dll under $From" }
        Save-Dll ([System.IO.File]::ReadAllBytes($found.FullName)) $found.FullName
    }
    if ($item.Extension -ieq '.dll') {
        Save-Dll ([System.IO.File]::ReadAllBytes($item.FullName)) $item.FullName
    }
    if ($item.Extension -ieq '.zip') {
        $bytes = Get-DllFromZipBytes ([System.IO.File]::ReadAllBytes($item.FullName)) $item.Name
        if (-not $bytes) { Die "No lib/x64/ASICamera2.dll inside $From" }
        Save-Dll $bytes $item.Name
    }
    Die "Don't know what to do with $From (expected a .dll, a .zip or a folder)."
}

# ----------------------------------------------------- 3. already on this machine
Info "Looking for an SDK already on this machine ..."
$searchRoots = @(
    $env:ProgramFiles, ${env:ProgramFiles(x86)},
    (Join-Path $env:USERPROFILE 'Downloads'),
    (Join-Path $env:USERPROFILE 'Documents')
) | Where-Object { $_ -and (Test-Path $_) }

foreach ($root in $searchRoots) {
    $hit = Get-ChildItem $root -Recurse -Filter 'ASICamera2.dll' -ErrorAction SilentlyContinue -Depth 6 |
           Where-Object { $_.FullName -match '(?i)[\\/]x64[\\/]' } | Select-Object -First 1
    if ($hit) {
        Info "Found $($hit.FullName)"
        Save-Dll ([System.IO.File]::ReadAllBytes($hit.FullName)) $hit.FullName
    }
}
Info "None found locally."

# ------------------------------------------------------------------ 4. download
if ($NoDownload) {
    Die @"
ASICamera2.dll is missing and -NoDownload was given.

Get it yourself from https://www.zwoastro.com/software/product-sdk/
(ASI Camera SDK, Windows), then either copy
  ASI SDK\lib\x64\ASICamera2.dll  ->  vendor\zwo\ASICamera2.dll
or re-run:
  .\tools\fetch_asi_dll.ps1 -From <the downloaded zip>
"@
}

Info "Downloading the ASI Camera SDK from ZWO (~112 MB: the archive carries the"
Info "Android and Linux SDKs too; only the 2.9 MB Windows x64 DLL is kept) ..."
$previousProgress = $ProgressPreference
$ProgressPreference = 'SilentlyContinue'   # the progress bar makes PS 5.1 downloads crawl
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("asi_sdk_{0}.zip" -f ([guid]::NewGuid().ToString('N')))
try {
    # GET, never HEAD: the endpoint 404s on HEAD, then redirects to a signed URL.
    Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing -MaximumRedirection 10 -TimeoutSec 900
} catch {
    Die @"
Download failed: $($_.Exception.Message)

Download it by hand from https://www.zwoastro.com/software/product-sdk/
(ASI Camera SDK, Windows) and then run:
  .\tools\fetch_asi_dll.ps1 -From <the downloaded zip>
"@
} finally {
    $ProgressPreference = $previousProgress
}

try {
    $size = (Get-Item $tmp).Length
    Info ("Downloaded {0:N1} MB; extracting ..." -f ($size / 1MB))
    $bytes = Get-DllFromZipBytes ([System.IO.File]::ReadAllBytes($tmp)) 'ZWO download'
    if (-not $bytes) { Die "The archive did not contain lib/x64/ASICamera2.dll. ZWO may have changed its layout." }
    Save-Dll $bytes 'ZWO download'
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}
