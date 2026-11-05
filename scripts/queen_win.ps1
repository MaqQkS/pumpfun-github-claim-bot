# Queen control on Windows:
#   powershell -ExecutionPolicy Bypass -File scripts\queen_win.ps1 start|stop|status
#   powershell -ExecutionPolicy Bypass -File scripts\queen_win.ps1 deep start|stop|status
# Deep mode listens on :8791 (grok-4.7). Do NOT start deep in production until approved.
param(
  [Parameter(Position=0)][string]$cmd = "status",
  [Parameter(Position=1)][string]$cmd2 = ""
)
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "logs"
$mode = "standard"
$action = $cmd
if ($cmd -eq "deep") {
  $mode = "deep"
  $action = if ($cmd2) { $cmd2 } else { "status" }
}
$port = if ($mode -eq "deep") { 8791 } else { 8790 }
$sup = if ($mode -eq "deep") { "scripts\supervise_queen_deep_win.py" } else { "scripts\supervise_queen_win.py" }
$stopFile = if ($mode -eq "deep") { "QUEEN_DEEP_STOP" } else { "QUEEN_STOP" }
$lifeTag = if ($mode -eq "deep") { "queen-deep" } else { "queen" }
function Health {
  try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:$port/health").Content }
  catch { "health: DOWN ($($_.Exception.Message))" }
}
switch ($action) {
  "start" {
    if ($mode -eq "deep") {
      Write-Host "NOTE: deep Queen is for /d re only. Leave RESEARCH_DEEP_ENABLED=false until approved."
    }
    Start-Process -WindowStyle Hidden -FilePath (Join-Path $root ".venv\Scripts\pythonw.exe") -ArgumentList $sup -WorkingDirectory $root
    Start-Sleep 8; Get-Content (Join-Path $logs "lifecycle.log") -Tail 3 -ErrorAction SilentlyContinue; Health
  }
  "stop" {
    New-Item -ItemType File -Force (Join-Path $logs $stopFile) | Out-Null
    Start-Sleep 5; Get-Content (Join-Path $logs "lifecycle.log") -Tail 4 -ErrorAction SilentlyContinue
  }
  default {
    $pat = if ($mode -eq "deep") { "supervise_queen_deep_win.py|PORT[=:]8791" } else { "supervise_queen_win.py|harness[\\/]src[\\/]server.ts" }
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $pat } | Select-Object ProcessId, Name | Format-Table -AutoSize
    Write-Host "mode=$mode port=$port"
    Health
    $logName = if ($mode -eq "deep") { "queen-deep.log" } else { "queen.log" }
    Get-Content (Join-Path $logs $logName) -Tail 5 -ErrorAction SilentlyContinue
  }
}
