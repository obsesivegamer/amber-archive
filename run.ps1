$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = "C:\Python313\python.exe"
if (-not (Test-Path $py)) {
  $py = (Get-Command py -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
  if ($py) { $py = "py"; $pyArgs = @("-3") } else { throw "Python 3 is not installed." }
} else {
  $pyArgs = @()
}

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  Write-Host "Creating virtualenv..."
  & $py @pyArgs -m venv .venv
}

Write-Host "Installing Python packages..."
& $venvPy -m pip install -r requirements.txt
Write-Host "Installing Chromium for Playwright..."
& $venvPy -m playwright install chromium
Write-Host "Starting Amber at http://127.0.0.1:8080"
& $venvPy -m uvicorn app.main:app --host 127.0.0.1 --port 8080
