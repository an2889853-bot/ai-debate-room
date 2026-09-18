# 설치·접속·문제 해결 (SETUP.md)

## 1. 새 PC에 설치 (Windows)

이 앱은 **서버 PC에 로그인된 Claude Code CLI·Codex CLI를 직접 실행하는 1인용 구조**다. 다른 PC에서 쓰려면 그 PC에 똑같이 설치하는 것이 정석이다.

**가져갈 것**: 이 폴더를 통째로 복사하되 `.venv\`, `__pycache__\`, `chats\`(개인 기록, 원하면), `runs\`는 빼도 된다 (약 0.6MB). `engine\` 폴더(엔진 패키지)가 빠지면 `debate.py`가 import 오류를 낸다. git을 쓰면 `git clone`이 곧 이 목록이다. `sandbox\` 빈 폴더는 필요하다(CLI 호출 작업 폴더 — git에는 `.gitkeep`으로 들어 있음). `ui_settings.json`을 가져가면 사이드바 설정이 그대로 따라간다.

**원클릭**: 폴더 안 `setup.cmd`를 더블클릭 → `tools\setup.ps1`이 uv·파이썬 3.14·.venv·패키지·Streamlit 설정·Claude Code CLI·Codex CLI를 설치하고(이미 있으면 건너뜀) 로그인 안 된 CLI는 로그인 창을 띄운 뒤 `debate.py --check`로 점검합니다. 실행 정책은 `.cmd`가 `-ExecutionPolicy Bypass`로 우회하므로 바꿀 필요 없습니다.

**직접 (PowerShell)**:
```powershell
# 1) 두 CLI 설치 + 구독 계정 로그인
irm https://claude.ai/install.ps1 | iex        # Claude Code CLI → ~\.local\bin\claude.exe
claude auth login
winget install --id OpenAI.Codex               # Codex CLI (Node.js 불필요; 앱이 winget 설치 경로를 직접 찾음)
codex login

# 2) Python 3.11 이상 (3.14.7로 검증). 스토어 'python' 스텁은 쓰지 말 것
winget install astral-sh.uv
uv python install 3.14

# 3) 프로젝트 폴더에서 가상환경 + 의존성
cd C:\Users\<이름>\ai-debate-room
python3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt        # 개발·테스트까지: requirements-dev.txt

# 4) Streamlit 첫 실행이 'Email:' 입력에서 멈추지 않도록
New-Item -ItemType Directory -Force "$HOME\.streamlit" | Out-Null
"[general]`nemail = `"`"" | Out-File -Encoding utf8 "$HOME\.streamlit\credentials.toml"

# 5) 점검 후 실행
.venv\Scripts\python.exe debate.py --check     # claude ✅ / codex ✅
.\launch_ui.cmd                                # http://localhost:8501
```

선택 사항:
- 글자 없는(스캔·인쇄) PDF 첨부까지 쓰려면 winget으로 Poppler 설치 (`pdftoppm`, `pdftotext`). 없으면 pypdf로 폴백하되 느리고 품질이 낮다.
- 바탕화면 바로가기:
  ```powershell
  $s = (New-Object -ComObject WScript.Shell).CreateShortcut("$HOME\Desktop\AI Debate Room.lnk")
  $s.TargetPath = "C:\Users\<이름>\ai-debate-room\launch_ui.cmd"
  $s.WorkingDirectory = "C:\Users\<이름>\ai-debate-room"
  $s.Save()
  ```
- PowerShell에서 `.venv\Scripts\Activate.ps1`을 쓰고 싶으면 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. 안 해도 `.venv\Scripts\python.exe`를 직접 부르면 된다.

**Mac/Linux**: 코드가 Windows 전용 부분을 포함한다 (`winsound`, winget 경로 탐색, `.cmd` 런처, `CREATE_NO_WINDOW`). 그쪽에서 쓰려면 그 부분을 손봐야 한다.

## 2. 이 PC 서버에 다른 컴퓨터로 접속 (같은 공유기 안)

1. `.streamlit\config.toml`에 추가하고 서버 재시작:
   ```toml
   [server]
   address = "0.0.0.0"
   ```
2. 관리자 PowerShell에서 방화벽(사설망·같은 서브넷만 허용):
   ```powershell
   New-NetFirewallRule -DisplayName "Streamlit 8501" -Direction Inbound -Protocol TCP -LocalPort 8501 -Profile Private -RemoteAddress LocalSubnet -Action Allow
   ```
3. 다른 컴퓨터 브라우저에서 `http://<이 PC의 IPv4>:8501`. IP는 `Get-NetIPAddress -AddressFamily IPv4`. PC가 이더넷·Wi-Fi 두 네트워크에 물려 있으면 상대 컴퓨터가 붙은 쪽 주소를 쓴다.

**단점·주의**
- 이 PC가 켜져 있어야 하고 모든 CLI 호출이 여기서 **내 구독 사용량**으로 돌아간다.
- 로그인 없이 열린다 — 같은 네트워크의 누구나 토론을 돌리고, 사이드바 "🔐 계정"의 로그아웃 버튼까지 누를 수 있다(Claude 로그아웃은 VS Code Claude Code도 풀림). `ui_settings.json`·`chats\`도 공유된다.
- 집 밖에서 쓰려면 포트포워딩 말고 Tailscale 같은 VPN. 구독 계정을 타인이 쓰게 하는 것은 약관 문제가 될 수 있다.

## 3. 문제 해결

| 증상 | 원인 → 조치 |
|---|---|
| 브라우저 ERR_CONNECTION_REFUSED, 콘솔에 `Email:` | `~\.streamlit\credentials.toml` 없음 → 위 4)번 |
| `module 'debate' has no attribute ...` | `debate.py`를 고쳤는데 서버가 옛 모듈을 씀 → 8501 프로세스 종료 후 `launch_ui.cmd` |
| `'가' is not recognized` 같은 배치 오류 | `.cmd`에 한글(UTF-8) 주석 → .cmd는 ASCII만 |
| `python`이 스토어를 엶 | 스토어 스텁 → `python3.14` 또는 `.venv\Scripts\python.exe` |
| codex 단계에서 `401 Unauthorized` | Codex 로그아웃 상태 → `codex login`. **`codex login --device-auth`는 실행만 해도 기존 로그인이 지워진다** |
| claude 단계가 바로 실패 (Claude Code 세션 안에서 실행 시) | 중첩 세션 차단 → `clean_env()`가 `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`를 제거함. 수동 실행이라면 같은 변수를 지울 것 |
| Codex effort 선택 시 "API 오류: Unsupported value" | 그 모델이 지원하지 않는 effort → 사이드바의 모델별 effort 목록에서 선택 (`auto`면 자동 하향) |
| 스캔 PDF가 빈 텍스트로 들어감 | 텍스트 20자 미만이면 쪽 이미지로 자동 변환됨. Poppler가 없으면 pypdf 폴백 → Poppler 설치 권장 |
| 로그인했는데 화면이 "로그아웃"이라 함 | 상태 캐시 120초 → 잠시 뒤 자동 갱신, 또는 계정 패널의 다시 확인 |
| 서버 재시작 / 포트 확인 | `Get-NetTCPConnection -LocalPort 8501 -State Listen` → `Stop-Process -Id <PID>` → `launch_ui.cmd`; health `http://localhost:8501/_stcore/health` |
