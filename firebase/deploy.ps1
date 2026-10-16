# MyContractApp one-click deploy for Windows (PowerShell 5+).
# Usage (inside the "firebase" folder):  powershell -ExecutionPolicy Bypass -File .\deploy.ps1
# Messages are in English on purpose: Windows PowerShell 5 mis-reads non-ASCII text in scripts.

$ErrorActionPreference = 'Stop'
$ProjectId = 'mycontractapp-911'
$SiteUrl   = 'https://mycontractapp-911.web.app'
$Here      = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here

function Step($n, $msg) { Write-Host ""; Write-Host "[$n/7] $msg" -ForegroundColor Cyan }
function Fail($msg)     { Write-Host ""; Write-Host "STOPPED: $msg" -ForegroundColor Red; Write-Host "Nothing was deployed after this point. Send this screen to Manus."; exit 1 }
function Ok($msg)       { Write-Host "  OK  $msg" -ForegroundColor Green }

# ---------------------------------------------------------------- 1. tools
Step 1 'Checking Node.js and Firebase CLI'
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail 'Node.js is not installed. Install the LTS version from https://nodejs.org then run this script again.' }
Ok ("node " + (node --version))
if (-not (Get-Command firebase -ErrorAction SilentlyContinue)) {
  Write-Host '  Installing firebase-tools (one time)...'
  npm install -g firebase-tools | Out-Host
  if (-not (Get-Command firebase -ErrorAction SilentlyContinue)) { Fail 'firebase-tools install failed. Run: npm install -g firebase-tools' }
}
Ok ("firebase " + (firebase --version))

# ---------------------------------------------------------------- 2. python
Step 2 'Finding Python (3.12 preferred, 3.11 / 3.13 accepted)'
$py = $null; $ver = $null
foreach ($v in @('3.12', '3.11', '3.13')) {
  if (Get-Command py -ErrorAction SilentlyContinue) {
    & py "-$v" -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = @('py', "-$v"); $ver = $v; break }
  }
}
if (-not $py -and (Get-Command python -ErrorAction SilentlyContinue)) {
  $pv = (& python -c "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null)
  if ($pv -in @('3.11', '3.12', '3.13')) { $py = @('python'); $ver = $pv }
}
if (-not $py) { Fail 'Python 3.11-3.13 not found. Install Python 3.12 from https://www.python.org/downloads/ (tick "Add to PATH").' }
$runtime = 'python' + $ver.Replace('.', '')
$fj = Join-Path $Here 'firebase.json'
$json = [System.IO.File]::ReadAllText($fj) -replace '"runtime":\s*"python3\d+"', ('"runtime": "' + $runtime + '"')
[System.IO.File]::WriteAllText($fj, $json, (New-Object System.Text.UTF8Encoding $false))
Ok "Python $ver  (functions runtime = $runtime)"

# ---------------------------------------------------------------- 3. venv
Step 3 'Preparing functions\venv and installing packages (first time takes 1-3 minutes)'
$venvPy = Join-Path $Here 'functions\venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
  $exe = $py[0]; $rest = @(); if ($py.Count -gt 1) { $rest = $py[1..($py.Count - 1)] }
  & $exe @rest -m venv (Join-Path $Here 'functions\venv')
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { Fail 'Could not create functions\venv.' }
}
& $venvPy -m pip install --disable-pip-version-check -q --upgrade pip | Out-Null
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $Here 'functions\requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail 'pip install failed (see the error above).' }
& $venvPy -c "import sys; sys.path.insert(0, r'$Here\functions'); import translation, documents, signature_service; print('import ok')"
if ($LASTEXITCODE -ne 0) { Fail 'Python modules failed to import.' }
Ok 'packages installed'

# ---------------------------------------------------------------- 4. .env
Step 4 'Preparing functions\.env (keeps your existing signing key if found)'
$envFile = Join-Path $Here 'functions\.env'
$key = $null
if (Test-Path $envFile) {
  $m = Select-String -Path $envFile -Pattern '^SIGNING_TOKEN_ENCRYPTION_KEY=(.+)$' | Select-Object -First 1
  if ($m) { $key = $m.Matches[0].Groups[1].Value.Trim() }
}
if (-not $key) {
  $roots = @("$env:USERPROFILE\iCloudDrive", "$env:USERPROFILE\OneDrive", "$env:USERPROFILE\Documents", "$env:USERPROFILE\Desktop") | Where-Object { Test-Path $_ }
  foreach ($r in $roots) {
    $old = Get-ChildItem -Path $r -Filter '.env' -Recurse -Force -Depth 6 -ErrorAction SilentlyContinue |
      Where-Object { $_.FullName -match 'MyContractApp|E-ContractApp' -and $_.FullName -notmatch 'venv|node_modules' -and $_.FullName -ne $envFile } |
      Select-Object -First 1
    if ($old) {
      $m = Select-String -Path $old.FullName -Pattern '^SIGNING_TOKEN_ENCRYPTION_KEY=(.+)$' | Select-Object -First 1
      if ($m) { $key = $m.Matches[0].Groups[1].Value.Trim(); Ok "reused signing key from $($old.FullName)"; break }
    }
  }
}
if (-not $key) {
  $bytes = New-Object byte[] 32; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $key = [Convert]::ToBase64String($bytes); Ok 'generated a new signing key'
}
$lines = @()
if (Test-Path $envFile) { $lines = Get-Content $envFile | Where-Object { $_ -notmatch '^(APP_BASE_URL|APP_ENV|SIGNING_TOKEN_ENCRYPTION_KEY|TRANSLATE_FAKE|STORAGE_EMULATOR_HOST)=' } }
$lines += "APP_BASE_URL=$SiteUrl"; $lines += 'APP_ENV=production'; $lines += "SIGNING_TOKEN_ENCRYPTION_KEY=$key"
[System.IO.File]::WriteAllLines($envFile, $lines)
$local = Join-Path $Here 'functions\.env.local'; if (Test-Path $local) { Remove-Item $local -Force }
Ok 'functions\.env ready (SMTP lines, if any, were kept)'

# ---------------------------------------------------------------- 5. login
Step 5 'Checking Firebase login'
$who = (firebase login:list 2>&1 | Out-String)
if ($who -match 'No authorized accounts' -or $who -notmatch '@') {
  Write-Host '  A browser window will open. Sign in with the Google account that owns mycontractapp-911.'
  firebase login
  if ($LASTEXITCODE -ne 0) { Fail 'Firebase login failed.' }
}
Ok 'logged in'

# ---------------------------------------------------------------- 6. deploy
Step 6 'Deploying API (Cloud Functions) + website (Hosting) - about 3-6 minutes'
Write-Host '  If asked to DELETE other functions, answer N (keeps them).'
firebase deploy --only "functions,hosting" --project $ProjectId
if ($LASTEXITCODE -ne 0) { Fail 'firebase deploy (functions,hosting) failed. Copy the red error text above to Manus.' }
Ok 'functions + hosting deployed'
Write-Host '  Deploying Storage rules (needs Storage enabled in Firebase console)...'
firebase deploy --only storage --project $ProjectId
if ($LASTEXITCODE -ne 0) {
  Write-Host '  WARNING: Storage rules not deployed. Open https://console.firebase.google.com/project/mycontractapp-911/storage , click "Get started", then run:  firebase deploy --only storage' -ForegroundColor Yellow
} else { Ok 'storage rules deployed' }

# ---------------------------------------------------------------- 7. verify
Step 7 'Verifying the live site'
Start-Sleep -Seconds 5
try {
  $h = Invoke-RestMethod -Uri "$SiteUrl/api/health" -TimeoutSec 30
  if ($h.version -eq '2026-10-16') { Ok "API is live: $($h.service) $($h.version)" } else { Write-Host "  WARNING: API answered but version is '$($h.version)'. Wait 1 minute and refresh." -ForegroundColor Yellow }
} catch { Write-Host "  WARNING: health check failed: $($_.Exception.Message)" -ForegroundColor Yellow }

Write-Host ""
Write-Host "DONE. Open $SiteUrl" -ForegroundColor Green
Write-Host 'For English translation, enable Cloud Translation API once:'
Write-Host '  https://console.cloud.google.com/apis/library/translate.googleapis.com?project=mycontractapp-911'
