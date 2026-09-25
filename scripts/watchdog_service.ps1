<#
Vector Lake watchdog wrapper for the scheduled task "VectorLake-Watchdog".

Why this exists: ``watchdog_sync.py`` is a foreground process whose only log sink is stderr,
and Task Scheduler cannot redirect it. This wrapper owns exactly one start -- it pins the
working directory to the repository root, resolves the interpreter, pins UTF-8 so Chinese
page names survive in the log, and then runs the watchdog in the foreground until it exits.

The task's repetition trigger runs with MultipleInstances=IgnoreNew, so it only starts a new
wrapper after the previous one returned: one running watchdog, and a bounded relaunch after a
crash or a hard kill. The watchdog itself keeps exactly one instance through
``.meta/.watchdog.instance.lock``, so a manual double start is rejected, not duplicated.
#>
[CmdletBinding()]
param(
    [string]$Python = '',
    [int]$KeepLogs = 10
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$scratch = Join-Path $root 'scratch'
New-Item -ItemType Directory -Path $scratch -Force | Out-Null

if ($Python) {
    $exe = $Python
}
else {
    $candidate = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
    $exe = if (Test-Path $candidate) { $candidate } else { (Get-Command python.exe).Source }
}
if (-not (Test-Path $exe)) { throw "python interpreter not found: $exe" }

# A self-healing task can restart many times in a day; keep the newest N per stream.
foreach ($stream in 'out', 'err') {
    Get-ChildItem -Path $scratch -Filter "watchdog_service-*-$stream.log" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $KeepLogs |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$out = Join-Path $scratch "watchdog_service-$stamp-out.log"
$err = Join-Path $scratch "watchdog_service-$stamp-err.log"

# The MCP server runs with the same pair; without them the log mangles non-ASCII page names.
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
Set-Location $root

$process = Start-Process -FilePath $exe -ArgumentList 'watchdog_sync.py' `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $out `
    -RedirectStandardError $err `
    -PassThru -Wait

exit $process.ExitCode
