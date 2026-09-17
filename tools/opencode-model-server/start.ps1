$ErrorActionPreference = "Stop"

$serverDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$serverScript = Join-Path $serverDirectory "server.py"
$python = (Get-Command python -ErrorAction Stop).Source
$logDirectory = Join-Path $env:LOCALAPPDATA "open-xiaoai"

if (-not (Test-Path -LiteralPath $serverScript)) {
    throw "Model server not found: $serverScript"
}
if (-not (Test-Path -LiteralPath $logDirectory)) {
    New-Item -ItemType Directory -Path $logDirectory | Out-Null
}

$existing = @(Get-NetTCPConnection -LocalPort 4399 -State Listen -ErrorAction SilentlyContinue)
if ($existing.Count -gt 0) {
    Write-Output "Model server is already running on port 4399."
    exit 0
}

$standardOutput = Join-Path $logDirectory "opencode-model-server.stdout.log"
$standardError = Join-Path $logDirectory "opencode-model-server.stderr.log"
$process = Start-Process -FilePath $python -ArgumentList @($serverScript) -WorkingDirectory $serverDirectory -WindowStyle Hidden -RedirectStandardOutput $standardOutput -RedirectStandardError $standardError -PassThru

Write-Output "Model server started with PID $($process.Id) at http://127.0.0.1:4399/."
