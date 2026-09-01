$ErrorActionPreference = "Stop"

$SkillRoot = Split-Path $PSScriptRoot -Parent
$InstancePath = Join-Path $SkillRoot ".scratch\instance.json"
$Fixture = Join-Path $SkillRoot "fixtures\article.html"

if (-not (Test-Path $InstancePath)) {
  Write-Error "No instance.json. Run launch.ps1 first."
}
if (-not (Test-Path $Fixture)) {
  Write-Error "Missing fixture $Fixture"
}

$inst = Get-Content -Raw -Path $InstancePath | ConvertFrom-Json
$Url = [string]$inst.url
if (-not $Url) { Write-Error "instance.json has no url" }
if ($Url -match ":8080") { Write-Error "Refuse to import into the user archive on port 8080." }

$tmpHeaders = Join-Path $env:TEMP "amber-verify-import-headers.txt"
$tmpBody = Join-Path $env:TEMP "amber-verify-import-body.txt"
Remove-Item -Force -ErrorAction SilentlyContinue $tmpHeaders, $tmpBody

$curl = Get-Command curl.exe -ErrorAction Stop
& $curl.Source -sS -D $tmpHeaders -o $tmpBody --max-redirs 0 -F "file=@${Fixture};filename=article.html;type=text/html" "$Url/import"
$curlCode = $LASTEXITCODE
if ($curlCode -ne 0 -and $curlCode -ne 47) {
  # 47 is CURLE_TOO_MANY_REDIRECTS when --max-redirs 0 hits a 303
  Write-Error "curl import failed with exit $curlCode"
}

$headers = Get-Content -Raw $tmpHeaders
$locationLine = ($headers -split "`r?`n") | Where-Object { $_ -match "^Location:" } | Select-Object -First 1
if (-not $locationLine) {
  Write-Error "Import did not return a Location header.`n$headers"
}

$location = ($locationLine -split ":", 2)[1].Trim()
if ($location -notmatch "^/[A-Za-z0-9]{5}$") {
  Write-Error "Unexpected Location: $location"
}

$sid = $location.Trim("/")
Write-Host "snapshot_id=$sid"
Write-Host "url=$Url/$sid"
