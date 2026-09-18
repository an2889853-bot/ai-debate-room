# AI Debate Room 원클릭 설치 (setup.cmd가 실행). 아무것도 없는 Windows PC 기준으로 순서대로 설치하고, 이미 된 단계는 건너뜁니다.
# 로그인은 브라우저 창이 열리며 본인 구독 계정으로 합니다. 관리자 권한 불필요 (전부 사용자 폴더에 설치).
#
# 구현 메모: Windows PowerShell 5.1은 외부 프로그램의 stderr 안내문(pip notice 등)을 빨간 글씨로 보여 오류처럼 보인다.
# 그래서 외부 명령의 출력은 2>&1 로 받아 문자열로 바꿔 평범한 글씨로 찍고, 실패 여부는 종료 코드로만 판단한다.
# (cmd /c 로 감싸는 방식은 경로에 따옴표가 있으면 PowerShell이 \" 로 넘겨 깨지므로 쓰지 않는다 — 실기 확인 2026-09-18)
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8   # 파이썬·CLI의 UTF-8 출력(✅ 등)을 깨지지 않게
$env:PYTHONIOENCODING = "utf-8"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
function RefreshPath {
    $env:PATH = "$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:USERPROFILE\.local\bin;" +
                [Environment]::GetEnvironmentVariable("PATH", "User") + ";" + [Environment]::GetEnvironmentVariable("PATH", "Machine")
}
RefreshPath

function Step($t) { Write-Host ""; Write-Host "=== $t ===" -ForegroundColor Cyan }
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Run {
    # 외부 프로그램 실행: 출력은 그대로 화면에(빨간 글씨 없이), 반환값은 종료 코드 하나만
    param([string]$exe, [string[]]$argv = @())
    & $exe @argv 2>&1 | ForEach-Object { Write-Host ("" + $_) }
    return [int]$LASTEXITCODE
}
function Quiet {
    # 출력은 버리고 종료 코드만
    param([string]$exe, [string[]]$argv = @())
    & $exe @argv *> $null
    return [int]$LASTEXITCODE
}
function Fail($msg) { Write-Host ""; Write-Host "[문제] $msg" -ForegroundColor Red }

Write-Host "AI Debate Room 설치를 시작합니다. 폴더: $root" -ForegroundColor Green
Write-Host "필요한 것: Claude 구독(Claude Code CLI) + ChatGPT 구독(Codex CLI). 없는 쪽은 로그인 단계에서 창을 닫으면 됩니다."
$problems = 0
$wingetArgs = @("--exact", "--accept-source-agreements", "--accept-package-agreements", "--silent")

Step "0/6 winget (Windows 앱 설치 도구)"
if (Have winget) { Write-Host "있음" }
else {
    Fail "winget이 없습니다. Microsoft Store에서 '앱 설치 관리자(App Installer)'를 설치한 뒤(https://aka.ms/getwinget) 이 파일을 다시 실행하세요."
    Read-Host "Enter 를 누르면 닫힙니다"; exit 1
}

Step "1/6 uv (파이썬을 받아 주는 도구)"
if (Have uv) { Write-Host "이미 있음" }
else {
    Run "winget" (@("install", "--id", "astral-sh.uv") + $wingetArgs) | Out-Null
    RefreshPath
    if (-not (Have uv)) { $problems++; Fail "uv 설치를 확인하지 못했습니다. 이 창을 닫고 setup.cmd를 다시 실행해 보세요 (PATH 갱신)." }
}

Step "2/6 파이썬 3.14 가상환경 (.venv) + 패키지"
$py = ".venv\Scripts\python.exe"
if (Test-Path $py) { Write-Host ".venv 이미 있음" }
elseif (Have uv) {
    $rc = Run "uv" @("venv", "--seed", "--python", "3.14", ".venv")      # 3.14가 없으면 uv가 내려받는다
    if ($rc -ne 0) { $problems++; Fail "가상환경 생성 실패 (종료 코드 $rc)" }
}
if (Test-Path $py) {
    Write-Host "패키지 설치 중... (streamlit 등 수십 MB, 1~5분. 아래에 진행 줄이 천천히 올라옵니다)" -ForegroundColor Yellow
    $rc = Run $py @("-m", "pip", "install", "--disable-pip-version-check", "--progress-bar", "off", "-r", "requirements.txt")
    if ($rc -ne 0) { $problems++; Fail "패키지 설치 실패 (종료 코드 $rc) - 인터넷 연결을 확인하고 다시 실행하세요." } else { Write-Host "패키지 OK" }
}

Step "3/6 Streamlit 첫 실행 설정"
$st = Join-Path $HOME ".streamlit"
New-Item -ItemType Directory -Force $st | Out-Null
$cred = Join-Path $st "credentials.toml"
if (-not (Test-Path $cred)) { "[general]`nemail = `"`"" | Out-File -Encoding utf8 $cred; Write-Host "credentials.toml 작성" } else { Write-Host "이미 있음" }

Step "4/6 Claude Code CLI (Claude 구독)"
$claudeExe = Join-Path $HOME ".local\bin\claude.exe"
if (-not (Have claude) -and -not (Test-Path $claudeExe)) {
    Write-Host "설치 중... (1~2분)"
    try { Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression } catch { Fail "Claude Code CLI 설치 실패: $_" }
    RefreshPath
} else { Write-Host "이미 있음" }
$claudeCmd = if (Have claude) { "claude" } elseif (Test-Path $claudeExe) { $claudeExe } else { $null }
if ($claudeCmd) {
    $status = (& $claudeCmd auth status --json 2>&1 | ForEach-Object { "" + $_ }) -join "`n"
    if ($status -match '"loggedIn"\s*:\s*true') { Write-Host "Claude 로그인 되어 있음" }
    else {
        Write-Host "Claude 로그인 창을 엽니다 - 브라우저에서 Claude 구독 계정으로 로그인한 뒤 이 창으로 돌아오세요." -ForegroundColor Yellow
        Write-Host "  * 'Login successful' 이 보이면 자동으로 다음 단계로 갑니다."
        Write-Host "  * 'Paste code here if prompted >' 가 보이면 브라우저에 표시된 코드를 여기에 붙여넣고 Enter."
        Write-Host "  * 이 창을 닫거나 Ctrl+C 를 누르면 설치가 중단됩니다 (그래도 setup.cmd 를 다시 실행하면 이어집니다)."
        & $claudeCmd auth login
        $status = (& $claudeCmd auth status --json 2>&1 | ForEach-Object { "" + $_ }) -join "`n"
        if ($status -match '"loggedIn"\s*:\s*true') { Write-Host "Claude 로그인 확인" -ForegroundColor Green }
        else { $problems++; Fail "Claude 로그인이 확인되지 않았습니다. setup.cmd 를 다시 실행하면 로그인 창만 다시 뜹니다." }
    }
} else { $problems++; Fail "Claude Code CLI를 찾지 못했습니다. 수동: PowerShell에서  irm https://claude.ai/install.ps1 | iex  실행 후  claude auth login" }

Step "5/6 OpenAI Codex CLI (ChatGPT 구독)"
function FindCodex {
    $d = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter "OpenAI.Codex_*" -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($d) {
        # 본체는 codex-x86_64-pc-windows-msvc.exe. 같은 폴더의 codex-command-runner.exe(보조)를 집으면 "no pipe-in provided"로 실패한다 (실기 2026-09-18)
        $exe = Get-ChildItem $d.FullName -Filter "codex-x86_64-pc-windows-msvc.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $exe) {
            $exe = Get-ChildItem $d.FullName -Filter "codex*.exe" -Recurse -ErrorAction SilentlyContinue |
                   Where-Object { $_.Name -notmatch "runner" } | Select-Object -First 1
        }
        if ($exe) { return $exe.FullName }
    }
    if (Have codex) { return "codex" }
    return $null
}
$codexCmd = FindCodex
if (-not $codexCmd) {
    Write-Host "설치 중... (1~2분)"
    Run "winget" (@("install", "--id", "OpenAI.Codex") + $wingetArgs) | Out-Null
    RefreshPath
    $codexCmd = FindCodex
} else { Write-Host "이미 있음" }
if ($codexCmd) {
    if ((Quiet $codexCmd @("login", "status")) -eq 0) { Write-Host "Codex 로그인 되어 있음" }
    else {
        Write-Host "Codex 로그인 창을 엽니다 - 브라우저에서 ChatGPT 구독 계정으로 로그인한 뒤 이 창으로 돌아오세요." -ForegroundColor Yellow
        Write-Host "  * 브라우저에 완료 표시가 나오면 이 창은 자동으로 다음 단계로 갑니다. 창을 닫거나 Ctrl+C 를 누르지 마세요."
        & $codexCmd login
        if ((Quiet $codexCmd @("login", "status")) -eq 0) { Write-Host "Codex 로그인 확인" -ForegroundColor Green }
        else { $problems++; Fail "Codex 로그인이 확인되지 않았습니다. setup.cmd 를 다시 실행하면 로그인 창만 다시 뜹니다." }
    }
} else { $problems++; Fail "Codex CLI를 찾지 못했습니다. 수동: winget install --id OpenAI.Codex  실행 후  codex login" }

Step "6/6 점검 (두 CLI를 한 번씩 호출)"
if (Test-Path $py) {
    $rc = Run $py @("debate.py", "--check")
    if ($rc -eq 0) { Write-Host "점검 통과" -ForegroundColor Green } else { $problems++; Fail "점검에서 실패한 CLI가 있습니다 (위 ❌ 줄 참고). 로그인이 안 됐으면 setup.cmd를 다시 실행하세요." }
} else { $problems++; Fail ".venv가 없어 점검을 건너뜁니다." }

Write-Host ""
if ($problems -eq 0) {
    Write-Host "설치 끝. 이제 launch_ui.cmd 를 더블클릭하면 브라우저에 http://localhost:8501 이 열립니다." -ForegroundColor Green
    Write-Host "바탕화면 바로가기: launch_ui.cmd 오른쪽 클릭 -> 보내기 -> 바탕화면에 바로 가기 만들기."
} else {
    Write-Host "문제 $problems 개가 있었습니다. 위의 [문제] 줄을 확인하고 setup.cmd를 다시 실행하면 된 단계는 건너뜁니다." -ForegroundColor Yellow
}
Read-Host "Enter 를 누르면 이 창이 닫힙니다"
