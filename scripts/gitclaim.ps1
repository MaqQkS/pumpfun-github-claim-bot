# GitClaimed control on Windows:  powershell -File scripts\gitclaim.ps1 start|stop|status
param([string]$cmd = "status")
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "logs"
switch ($cmd) {
  "start" {
    Start-Process -WindowStyle Hidden -FilePath (Join-Path $root ".venv\Scripts\pythonw.exe") -ArgumentList "scripts\supervise_win.py" -WorkingDirectory $root
    Start-Sleep 3; Get-Content (Join-Path $logs "lifecycle.log") -Tail 3
  }
  "stop" {
    New-Item -ItemType File -Force (Join-Path $logs "STOP") | Out-Null
    Start-Sleep 8; Get-Content (Join-Path $logs "lifecycle.log") -Tail 4
  }
  default {
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match "supervise_win.py|bot.py" -and $_.CommandLine -notmatch "grok-street" } | Select-Object ProcessId, CommandLine | Format-Table -AutoSize -Wrap
    Get-Content (Join-Path $logs "lifecycle.log") -Tail 5
    Get-Content (Join-Path $root "bot.log") -Tail 5
  }
}
