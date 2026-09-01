$ErrorActionPreference = "Stop"

$SkillRoot = Split-Path $PSScriptRoot -Parent
$RepoRoot = (Resolve-Path (Join-Path $SkillRoot "..\..\..")).Path
$ScratchDir = Join-Path $SkillRoot ".scratch"
$InstancePath = Join-Path $ScratchDir "instance.json"
$RepoData = Join-Path $RepoRoot "data"

function Fail([string]$Message) {
  Write-Host "DOCTOR FAIL: $Message"
  exit 1
}

if (-not (Test-Path $InstancePath)) {
  Fail "No instance.json at $InstancePath. Run launch.ps1."
}

$inst = Get-Content -Raw -Path $InstancePath | ConvertFrom-Json
$pidValue = [int]$inst.pid
$url = [string]$inst.url
$port = [int]$inst.port
$dataDir = [string]$inst.data_dir

if ($port -eq 8080) {
  Fail "Instance port is 8080. That is the user archive. Do not drive it."
}

if ($url -match ":8080") {
  Fail "Instance URL is $url. Refuse to drive the user archive."
}

try {
  $proc = Get-Process -Id $pidValue -ErrorAction Stop
} catch {
  Fail "pid $pidValue is not running. Run cleanup.ps1 then launch.ps1."
}

$scratchResolved = [System.IO.Path]::GetFullPath($ScratchDir)
$dataResolved = [System.IO.Path]::GetFullPath($dataDir)
$repoDataResolved = [System.IO.Path]::GetFullPath($RepoData)

if (-not $dataResolved.StartsWith($scratchResolved, [System.StringComparison]::OrdinalIgnoreCase)) {
  Fail "data_dir $dataResolved is not under $scratchResolved."
}

if ($dataResolved.StartsWith($repoDataResolved, [System.StringComparison]::OrdinalIgnoreCase)) {
  Fail "data_dir points at repo data/. Refuse to drive."
}

$dbPath = Join-Path $dataResolved "amber.sqlite3"
if (-not (Test-Path $dbPath)) {
  Fail "SQLite missing at $dbPath."
}

try {
  $homePage = Invoke-WebRequest -Uri "$url/" -UseBasicParsing -TimeoutSec 5
} catch {
  Fail "GET $url/ failed: $($_.Exception.Message)"
}

if ($homePage.StatusCode -ne 200) {
  Fail "GET $url/ returned $($homePage.StatusCode)"
}
if ($homePage.Content -notmatch "time capsule for web pages") {
  Fail "Homepage did not contain the Amber tagline."
}

try {
  $about = Invoke-WebRequest -Uri "$url/about" -UseBasicParsing -TimeoutSec 5
} catch {
  Fail "GET $url/about failed: $($_.Exception.Message)"
}
if ($about.StatusCode -ne 200) {
  Fail "GET $url/about returned $($about.StatusCode)"
}

Write-Host "DOCTOR OK"
Write-Host "url=$url"
Write-Host "pid=$pidValue ($($proc.ProcessName))"
Write-Host "data_dir=$dataResolved"
Write-Host "db=$dbPath"
exit 0
