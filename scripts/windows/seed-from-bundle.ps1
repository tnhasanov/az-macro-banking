<#
.SYNOPSIS
  Seed the empty private Blob store from the bootstrap bundle, on Windows.

.DESCRIPTION
  Runs from the root of a checkout of this repository. In order, it:
    1. checks Git, Python (3.11+, 64-bit) and Node (20+);
    2. checks the nine source-archive parts against PARTS.SHA256SUMS and joins them, in order;
    3. lays the bundle out as <temp>\azmonitor-bootstrap\dataset\... and checks it against SHA256SUMS;
    4. creates .venv and installs this checkout (pip install -e ".[cloud]") and the Blob helper
       (npm ci --omit=dev in tools\blob);
    5. asks for the store's read-write token with hidden input (never echoed, never written to disk
       or to PowerShell history) and keeps it only in this process's environment;
    6. runs `python -m azmonitor.cloud.publish seed --from-bundle`, which restores the bundle into a
       scratch directory and checks every digest and SQLite's integrity, refuses a store that
       already holds a dataset, uploads, reads back what arrived, and records a backup that it
       restores once more to prove it.
  The token is removed from the environment and the joined bundle deleted when it finishes.

.PARAMETER Downloads
  The folder holding the files sent with the bundle: SHA256SUMS, PARTS.SHA256SUMS, current.json,
  state-20260920T093543Z-snapshot.tar.gz and raw-20260920T093543Z-snapshot.tar.gz.part-00 … part-08.

.PARAMETER RehearsalStoreDir
  For testing this script only: seed a local directory instead of Blob, without asking for a token.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\windows\seed-from-bundle.ps1 -Downloads "$HOME\Downloads"
#>
param(
    [Parameter(Mandatory = $true)] [string] $Downloads,
    [string] $RehearsalStoreDir = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Stamp = "20260920T093543Z-snapshot"
$StateName = "state-$Stamp.tar.gz"
$RawName = "raw-$Stamp.tar.gz"
$PartNames = 0..8 | ForEach-Object { "$RawName.part-{0:D2}" -f $_ }

function Fail([string] $message) { Write-Host "STOPPED: $message" -ForegroundColor Red; exit 1 }
function Step([string] $message) { Write-Host "`n== $message" -ForegroundColor Cyan }

function Read-Sums([string] $path) {
    # Lines of "<sha256>  <name>", as written by sha256sum.
    $sums = @{}
    foreach ($line in Get-Content -LiteralPath $path) {
        if ($line -match '^([0-9a-f]{64})\s+\*?(.+)$') { $sums[$Matches[2].Trim()] = $Matches[1] }
    }
    return $sums
}

function Assert-Hash([string] $file, [string] $expected, [string] $label) {
    $actual = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { Fail "$label does not match its checksum (expected $expected, got $actual)" }
    Write-Host "  ok  $label"
}

# ------------------------------------------------------------------------------------------ 1
Step "Checking Git, Python and Node"
if (-not (Test-Path -LiteralPath ".\azmonitor\cloud\publish.py")) {
    Fail "run this from the root of the az-macro-banking checkout"
}
try { $gitVersion = (& git --version) } catch { Fail "Git is not installed or not on PATH" }
Write-Host "  $gitVersion"
Write-Host "  code revision: $(& git rev-parse HEAD)"

# The first interpreter that meets the requirement: the launcher's default, then `python` on PATH.
# Arguments are splatted from an array so an empty one passes nothing at all to the executable.
$Python = $null
$found = @()
foreach ($candidate in @(@{ Exe = "py"; Args = @("-3") }, @{ Exe = "python"; Args = @() })) {
    $pyArgs = $candidate.Args
    try {
        $probe = & $candidate.Exe @pyArgs -c "import sys,struct; print('%d.%d %d' % (sys.version_info[0], sys.version_info[1], struct.calcsize('P')*8))"
        if ($LASTEXITCODE -ne 0 -or -not $probe) { continue }
    } catch { continue }
    $fields = "$probe".Trim().Split(" ")
    $found += "$($candidate.Exe) $($pyArgs -join ' '): Python $($fields[0]) ($($fields[1])-bit)"
    if ([version]$fields[0] -ge [version]"3.11" -and $fields[1] -eq "64") {
        $Python = $candidate
        Write-Host "  Python $($fields[0]) (64-bit), via '$($candidate.Exe) $($pyArgs -join ' ')'"
        break
    }
}
if (-not $Python) {
    if ($found.Count -gt 0) { Write-Host ("  found: " + ($found -join "; ")) }
    Fail "no 64-bit Python 3.11 or later was found (install Python 3.12, 64-bit, from python.org)"
}

try { $nodeVersion = (& node --version).Trim() } catch { Fail "Node.js is not installed or not on PATH (install Node 20 LTS or later)" }
if ([version]($nodeVersion.TrimStart("v")) -lt [version]"20.0.0") { Fail "Node $nodeVersion found; 20 or later is required" }
Write-Host "  Node $nodeVersion"

# ------------------------------------------------------------------------------------------ 2
Step "Checking and joining the nine source-archive parts"
$Downloads = (Resolve-Path -LiteralPath $Downloads).Path
foreach ($name in @("SHA256SUMS", "PARTS.SHA256SUMS", "current.json", $StateName) + $PartNames) {
    if (-not (Test-Path -LiteralPath (Join-Path $Downloads $name))) {
        Fail "$name is not in $Downloads. The names must be exactly as sent: a browser may add ' (1)' to a repeated download, or '.txt' to a file with no extension."
    }
}
$partSums = Read-Sums (Join-Path $Downloads "PARTS.SHA256SUMS")
foreach ($name in $PartNames) {
    if (-not $partSums.ContainsKey($name)) { Fail "PARTS.SHA256SUMS has no line for $name" }
    Assert-Hash (Join-Path $Downloads $name) $partSums[$name] $name
}

$Work = Join-Path ([System.IO.Path]::GetTempPath()) ("azmonitor-bootstrap-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$Bundle = Join-Path $Work "azmonitor-bootstrap"
$Dataset = Join-Path $Bundle "dataset"
New-Item -ItemType Directory -Path $Dataset -Force | Out-Null

$exit = 1
try {
    $joined = Join-Path $Dataset $RawName
    $out = [System.IO.File]::Create($joined)
    try {
        foreach ($name in $PartNames) {          # part-00, part-01, … part-08, in this order
            $in = [System.IO.File]::OpenRead((Join-Path $Downloads $name))
            try { $in.CopyTo($out) } finally { $in.Dispose() }
        }
    } finally { $out.Dispose() }
    Copy-Item -LiteralPath (Join-Path $Downloads "current.json") -Destination (Join-Path $Dataset "current.json")
    Copy-Item -LiteralPath (Join-Path $Downloads $StateName) -Destination (Join-Path $Dataset $StateName)

    # ------------------------------------------------------------------------------------------ 3
    Step "Checking the assembled bundle against SHA256SUMS"
    $sums = Read-Sums (Join-Path $Downloads "SHA256SUMS")
    foreach ($name in @("dataset/current.json", "dataset/$StateName", "dataset/$RawName")) {
        if (-not $sums.ContainsKey($name)) { Fail "SHA256SUMS has no line for $name" }
        Assert-Hash (Join-Path $Bundle ($name -replace "/", "\")) $sums[$name] $name
    }

    # ------------------------------------------------------------------------------------------ 4
    Step "Installing this checkout into .venv and the Blob helper"
    if (-not (Test-Path -LiteralPath ".\.venv\Scripts\python.exe")) {
        $pyArgs = $Python.Args
        & $Python.Exe @pyArgs -m venv .venv
        if ($LASTEXITCODE -ne 0) { Fail "could not create the virtual environment" }
    }
    $VenvPython = (Resolve-Path ".\.venv\Scripts\python.exe").Path
    & $VenvPython -m pip install --quiet --upgrade pip
    & $VenvPython -m pip install --quiet -e ".[cloud]"
    if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }
    & npm ci --omit=dev --prefix tools\blob --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { Fail "npm ci in tools\blob failed" }

    # ------------------------------------------------------------------------------------------ 5
    $env:AZMONITOR_PROFILE = "neutral"
    $env:PYTHONUTF8 = "1"
    # Nothing left over from another session may redirect where the dataset is read from or goes to.
    foreach ($name in @("AZMONITOR_DATA_DIR", "AZMONITOR_BLOB_PREFIX", "VERCEL_OIDC_TOKEN", "BLOB_STORE_ID")) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    if ($RehearsalStoreDir) {
        Step "Rehearsal: seeding the local directory $RehearsalStoreDir instead of Blob"
        $env:AZMONITOR_OBJECT_STORE_DIR = $RehearsalStoreDir
    } else {
        Step "The store's read-write token"
        # A local object-store setting would win over the token; make sure it cannot.
        Remove-Item Env:AZMONITOR_OBJECT_STORE_DIR -ErrorAction SilentlyContinue
        Write-Host "  Vercel -> Storage -> the store -> .env.local -> BLOB_READ_WRITE_TOKEN."
        Write-Host "  Paste it below; nothing is shown, and it is not saved to disk or to history."
        $secure = Read-Host "  Token" -AsSecureString
        $plain = [System.Net.NetworkCredential]::new("", $secure).Password
        if ($plain -notmatch '^vercel_blob_rw_[A-Za-z0-9]+_[A-Za-z0-9]+$') { Fail "that is not a Blob read-write token" }
        $env:BLOB_READ_WRITE_TOKEN = $plain
        $env:AZMONITOR_BLOB_AUTH = "read-write"
        Remove-Variable plain, secure
    }

    # ------------------------------------------------------------------------------------------ 6
    Step "Seeding: verify the bundle, refuse a store that holds a dataset, upload, read back, back up"
    & $VenvPython -m azmonitor.cloud.publish seed --from-bundle $Bundle
    $exit = $LASTEXITCODE
    switch ($exit) {
        0 { Write-Host "`nDONE: the store is seeded and a backup that restores intact is recorded." -ForegroundColor Green }
        4 { Write-Host "`nSTOPPED: the store already holds a dataset; nothing was uploaded." -ForegroundColor Yellow }
        default { Write-Host "`nSTOPPED (exit $exit): see the JSON above; the store was not changed unless it says seeded: true." -ForegroundColor Red }
    }
} finally {
    Remove-Item Env:BLOB_READ_WRITE_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:AZMONITOR_BLOB_AUTH -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Work -Recurse -Force -ErrorAction SilentlyContinue
}
exit $exit
