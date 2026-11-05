# Queen control on Windows:  powershell -ExecutionPolicy Bypass -File scripts\queen_win.ps1 start|stop|status
param([string]$cmd = "status")
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "logs"
function Health { try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 http://127.0.0.1:8790/health).Content } catch { "health: DOWN ($($_.Exception.Message))" } }
switch ($cmd) {
  "start" {
    Start-Process -WindowStyle Hidden -FilePath (Join-Path $root ".venv\Scripts\pythonw.exe") -ArgumentList "scripts\supervise_queen_win.py" -WorkingDirectory $root
    Start-Sleep 8; Get-Content (Join-Path $logs "lifecycle.log") -Tail 3; Health
  }
  "stop" {
    New-Item -ItemType File -Force (Join-Path $logs "QUEEN_STOP") | Out-Null
    Start-Sleep 5; Get-Content (Join-Path $logs "lifecycle.log") -Tail 4
  }
  default {
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "supervise_queen_win.py|harness[\\/]src[\\/]server.ts" } | Select-Object ProcessId, Name | Format-Table -AutoSize
    Health
    Get-Content (Join-Path $logs "queen.log") -Tail 5 -ErrorAction SilentlyContinue
  }
}
