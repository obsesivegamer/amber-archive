$ErrorActionPreference = "Stop"

$ScriptsDir = $PSScriptRoot
$SkillRoot = Split-Path $ScriptsDir -Parent
$RepoRoot = (Resolve-Path (Join-Path $SkillRoot "..\..\..")).Path
$ScratchDir = Join-Path $SkillRoot ".scratch"
$DataDir = Join-Path $ScratchDir "data"
$InstancePath = Join-Path $ScratchDir "instance.json"
$LogOut = Join-Path $ScratchDir "uvicorn.out.log"
$LogErr = Join-Path $ScratchDir "uvicorn.err.log"
$VenvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"

$HostName = "127.0.0.1"
$Port = 18080
if ($env:AMBER_VERIFY_PORT) {
  $Port = [int]$env:AMBER_VERIFY_PORT
}

if ($Port -eq 8080) {
  Write-Error "Refuse to launch verification on port 8080 (user archive). Unset AMBER_VERIFY_PORT or pick another port."
}

if (-not (Test-Path $VenvPy)) {
  Write-Error "Missing $VenvPy. Create the venv from the README (.\run.ps1 or python -m venv .venv) then install requirements and Playwright Chromium."
}

function Get-Instance {
  if (-not (Test-Path $InstancePath)) { return $null }
  return Get-Content -Raw -Path $InstancePath | ConvertFrom-Json
}

function Test-PidAlive([int]$ProcessId) {
  try {
    $p = Get-Process -Id $ProcessId -ErrorAction Stop
    return $null -ne $p
  } catch {
    return $false
  }
}

function Test-HttpReady([string]$Url) {
  try {
    $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
    return ($resp.StatusCode -eq 200 -and $resp.Content -match "time capsule")
  } catch {
    return $false
  }
}

$existing = Get-Instance
if ($existing) {
  $alive = Test-PidAlive ([int]$existing.pid)
  $ready = Test-HttpReady ([string]$existing.url)
  if ($alive -and $ready) {
    Write-Host "Amber verify instance already running at $($existing.url) (pid $($existing.pid))"
    Write-Host "Amber verify instance ready at $($existing.url)"
    exit 0
  }
  Write-Host "Stale instance.json (alive=$alive ready=$ready). Starting a new process."
}

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
Remove-Item -Force -ErrorAction SilentlyContinue $LogOut, $LogErr

$portTaken = $false
try {
  $listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  if ($listen) { $portTaken = $true }
} catch {
  try {
    $client = New-Object System.Net.Sockets.TcpClient
    $client.Connect($HostName, $Port)
    $client.Close()
    $portTaken = $true
  } catch {
    $portTaken = $false
  }
}
if ($portTaken) {
  Write-Error "Port $Port is already in use and is not a healthy verify instance. Pick AMBER_VERIFY_PORT or run cleanup.ps1 if you started that process."
}

$env:AMBER_DATA_DIR = $DataDir
$env:AMBER_HOST = $HostName
$env:AMBER_PORT = "$Port"
Remove-Item Env:AMBER_ALLOW_PRIVATE -ErrorAction SilentlyContinue

$argList = @(
  "-m", "uvicorn", "app.main:app",
  "--host", $HostName,
  "--port", "$Port"
)

$proc = Start-Process -FilePath $VenvPy -ArgumentList $argList -WorkingDirectory $RepoRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput $LogOut -RedirectStandardError $LogErr
$Url = "http://${HostName}:${Port}"

$deadline = (Get-Date).AddSeconds(30)
$ready = $false
while ((Get-Date) -lt $deadline) {
  if (-not (Test-PidAlive $proc.Id)) {
    $err = ""
    if (Test-Path $LogErr) { $err = Get-Content -Raw $LogErr }
    Write-Error "uvicorn exited during launch.`n$err"
  }
  if (Test-HttpReady $Url) {
    $ready = $true
    break
  }
  Start-Sleep -Milliseconds 300
}

if (-not $ready) {
  try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch { }
  $err = ""
  if (Test-Path $LogErr) { $err = Get-Content -Raw $LogErr }
  Write-Error "Timed out waiting for $Url.`n$err"
}

$instance = [ordered]@{
  pid      = $proc.Id
  host     = $HostName
  port     = $Port
  url      = $Url
  data_dir = $DataDir
  repo     = $RepoRoot
  started  = (Get-Date).ToUniversalTime().ToString("o")
}
$instance | ConvertTo-Json | Set-Content -Path $InstancePath -Encoding utf8

Write-Host "Amber verify instance ready at $Url"
Write-Host "pid=$($proc.Id) data_dir=$DataDir"
