# windesktopctl installer - finds Python >=3.11, creates .venv, installs
# editable, prints the `devin mcp add` command to register the stdio server.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Invoke-Py([string[]]$cmd, [string[]]$extra) {
    $rest = @()
    if ($cmd.Count -gt 1) { $rest = $cmd[1..($cmd.Count - 1)] }
    & $cmd[0] @rest @extra
}

function Test-Py311([string[]]$cmd) {
    try {
        Invoke-Py $cmd @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)') 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$pyCmd = $null
foreach ($cand in @(@('py', '-3'), @('python'), @('py'))) {
    if (Test-Py311 $cand) { $pyCmd = $cand; break }
}
if (-not $pyCmd) { throw "Python >=3.11 not found (tried 'py -3', 'python'). Install Python first." }
Write-Host "using: $($pyCmd -join ' ')"

if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    Write-Host "creating .venv ..."
    Invoke-Py $pyCmd @('-m', 'venv', "$root\.venv")
}
$venvPy = "$root\.venv\Scripts\python.exe"
& $venvPy -m pip install -e "$root"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$mcp = "$root\.venv\Scripts\windesktopctl-mcp.exe"
Write-Host ""
Write-Host "Done. Register the MCP server (stdio) with:"
Write-Host "  devin mcp add -s user windesktopctl -- `"$mcp`""
Write-Host ""
Write-Host "To serve over HTTP instead:  .\start.cmd -Expose tailnet   (see README)"
