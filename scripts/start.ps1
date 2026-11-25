# windesktopctl serve launcher.
#   .\scripts\start.ps1                 # local only (127.0.0.1:8765)
#   .\scripts\start.ps1 -Expose tailnet # also `tailscale serve` on :443
#   .\scripts\start.ps1 -Expose funnel  # PUBLIC internet - token only
param(
    [ValidateSet('local', 'tailnet', 'funnel')]
    [string]$Expose = 'local',
    [int]$Port = 8765,
    [switch]$ShowToken,
    [switch]$Copy
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$exe = "$root\.venv\Scripts\windesktopctl.exe"
if (-not (Test-Path $exe)) { throw "not installed - run scripts\install.ps1 first" }

$argv = @('serve', '--expose', $Expose, '--port', "$Port")
if ($ShowToken) { $argv += '--show-token' }
if ($Copy) { $argv += '--copy' }
& $exe @argv
exit $LASTEXITCODE
