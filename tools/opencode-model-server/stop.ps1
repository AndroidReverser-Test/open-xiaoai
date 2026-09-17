$listenerProcessIds = @(
    Get-NetTCPConnection -LocalPort 4399 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
)
$processes = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match "^python(?:\.exe)?$" -and (
        $_.CommandLine -like "*opencode-model-server*server.py*" -or
        ($listenerProcessIds -contains $_.ProcessId -and $_.CommandLine -match "(?:^|\s)server\.py(?:\s|$)")
    )
})
if ($processes.Count -eq 0) {
    Write-Output "Model server is not running."
    exit 0
}

$processes | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
    Write-Output "Stopped model server PID $($_.ProcessId)."
}
