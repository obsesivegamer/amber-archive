$ErrorActionPreference = "Stop"

$SkillRoot = Split-Path $PSScriptRoot -Parent
$ScratchDir = Join-Path $SkillRoot ".scratch"
$InstancePath = Join-Path $ScratchDir "instance.json"
$EvidenceDir = Join-Path $SkillRoot "evidence"

if (Test-Path $InstancePath) {
  $inst = Get-Content -Raw -Path $InstancePath | ConvertFrom-Json
  $pidValue = [int]$inst.pid
  if ($pidValue -gt 0) {
    try {
      $proc = Get-Process -Id $pidValue -ErrorAction Stop
      Write-Host "Stopping pid $pidValue ($($proc.ProcessName))"
      Stop-Process -Id $pidValue -Force
      Start-Sleep -Milliseconds 400
    } catch {
      Write-Host "pid $pidValue already gone"
    }
  }
} else {
  Write-Host "No instance.json; nothing to kill"
}

if (Test-Path $ScratchDir) {
  Remove-Item -Recurse -Force -Path $ScratchDir
  Write-Host "Removed $ScratchDir"
}

if (Test-Path $EvidenceDir) {
  Write-Host "Kept evidence at $EvidenceDir"
} else {
  Write-Host "No evidence directory yet"
}

Write-Host "Cleanup done"
