# AI Debate Room 원클릭 설치 (setup.cmd가 실행). 이미 된 단계는 건너뜁니다. 로그인은 브라우저 창이 열리며 본인 구독 계정으로.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PATH = "$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:USERPROFILE\.local\bin;$env:PATH"

function Step($t) { Write-Host ""; Write-Host "=== $t ===" -ForegroundColor Cyan }
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function WingetInstall($id) {
    winget install --id $id --exact --accept-source-agreements --accept-package-agreements --silent
    $env:PATH = "$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:USERPROFILE\.local\bin;$env:PATH"
}

Write-Host "AI Debate Room 설치를 시작합니다. 폴더: $root" -ForegroundColor Green
Write-Host "필요한 것: Claude 구독(Claude Code CLI) + ChatGPT 구독(Codex CLI). 없는 쪽은 로그인 단계에서 창을 닫으면 됩니다."

Step "1/6 uv (파이썬을 받아 주는 도구)"
if (-not (Have uv)) { WingetInstall "astral-sh.uv" } else { Write-Host "이미 있음" }

Step "2/6 파이썬 3.14 가상환경 (.venv) + 패키지"
if (-not (Test-Path ".venv\Scripts\python.exe")) { uv venv --seed --python 3.14 .venv } else { Write-Host ".venv 이미 있음" }
& ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "패키지 설치 실패 - 인터넷 연결을 확인하고 다시 실행하세요." -ForegroundColor Red }

Step "3/6 Streamlit 첫 실행 설정"
$st = Join-Path $HOME ".streamlit"
New-Item -ItemType Directory -Force $st | Out-Null
$cred = Join-Path $st "credentials.toml"
if (-not (Test-Path $cred)) { "[general]`nemail = `"`"" | Out-File -Encoding utf8 $cred; Write-Host "credentials.toml 작성" } else { Write-Host "이미 있음" }

Step "4/6 Claude Code CLI"
$claudeExe = Join-Path $HOME ".local\bin\claude.exe"
if (-not (Have claude) -and -not (Test-Path $claudeExe)) {
    Write-Host "설치 중..."
    Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
} else { Write-Host "이미 있음" }
$claudeCmd = if (Have claude) { "claude" } else { $claudeExe }
$loggedIn = $false
try { $status = & $claudeCmd auth status --json 2>$null | Out-String; if ($status -match '"loggedIn"\s*:\s*true') { $loggedIn = $true } } catch {}
if ($loggedIn) { Write-Host "Claude 로그인 되어 있음" }
else { Write-Host "Claude 로그인 창을 엽니다 (Claude 구독 계정으로 로그인)..." -ForegroundColor Yellow; & $claudeCmd auth login }

Step "5/6 OpenAI Codex CLI"
function FindCodex {
    $d = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter "OpenAI.Codex_*" -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($d) { return (Join-Path $d.FullName "codex-x86_64-pc-windows-msvc.exe") }
    if (Have codex) { return "codex" }
    return $null
}
$codexCmd = FindCodex
if (-not $codexCmd) { Write-Host "설치 중..."; WingetInstall "OpenAI.Codex"; $codexCmd = FindCodex } else { Write-Host "이미 있음" }
if ($codexCmd) {
    & $codexCmd login status 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "Codex 로그인 되어 있음" }
    else { Write-Host "Codex 로그인 창을 엽니다 (ChatGPT 구독 계정으로 로그인)..." -ForegroundColor Yellow; & $codexCmd login }
} else { Write-Host "Codex CLI를 찾지 못했습니다. 수동: winget install --id OpenAI.Codex" -ForegroundColor Red }

Step "6/6 점검 (두 CLI를 한 번씩 호출)"
& ".venv\Scripts\python.exe" debate.py --check

Write-Host ""
Write-Host "설치 끝. 이제 launch_ui.cmd 를 더블클릭하면 브라우저에 http://localhost:8501 이 열립니다." -ForegroundColor Green
Write-Host "바탕화면 바로가기를 만들려면 launch_ui.cmd 를 오른쪽 클릭 -> 보내기 -> 바탕화면."
Read-Host "Enter 를 누르면 이 창이 닫힙니다"
