# 결정·사고 기록 (DECISIONS.md)

시간순. **새 항목은 맨 아래에** `## YYYY-MM-DD HH:MM — 제목` 으로 추가한다. 바뀐 결정은 지우지 말고 "→ 대체됨(날짜)"를 붙인다.
현재 동작의 정리는 [ARCHITECTURE.md](ARCHITECTURE.md), 설치·접속은 [SETUP.md](SETUP.md).

---

## 2026-09-10 — 목표·제약·MVP 범위 (프로젝트 시작)

**목표**: Windows 11 + VS Code 환경에서 Claude와 GPT가 한 채팅창에서 자동으로 서로 검토·반박하며 하나의 답을 만드는 로컬 AI Debate Room.

**제약**
- API 키 과금 방식은 쓰지 않는다. 구독 계정으로 로그인된 Claude Code CLI와 OpenAI Codex CLI를 Python `subprocess`로 호출한다.
- CLI 옵션은 버전마다 다르므로 임의로 가정하지 말고 `--help`, `--version` 실제 출력을 먼저 확인한 뒤 결정한다. help 출력은 `docs\`에 저장 (`claude-help.txt`, `codex-help.txt`, `codex-exec-help.txt`, `codex-login-help.txt`).
- 코드는 한 번에 많이 주지 않는다. 사용자가 직접 하나씩 실행하며 따라갈 수 있게 단계별로 진행하고 각 단계 결과를 확인한 뒤 다음으로. 확인 명령은 Claude가 PowerShell로 직접 실행해도 되지만, 설치처럼 시스템을 바꾸는 명령은 실행 전에 한 줄로 알린다.
- 사용자는 전기공학 전공 자동화/로봇 인턴. Python 경험은 보통 수준. 설명은 한국어.

**토론 순서 (당시 고정, 5단계)** → 대체됨(2026-09-16: 3단계 기본, 5단계 선택)
1. Claude · Initial — 질문 분석, 해결책 제시
2. GPT · Review — 질문 + Claude 답변의 오류·허점·누락·개선점·대안 검토
3. Claude · Rebuttal — 맞는 지적은 수정, 틀린 지적은 근거로 반박, 2차 답변
4. GPT · Recheck — 남은 문제와 1차 검토 반영 여부 확인
5. Claude · FINAL — 전체 토론을 종합한 하나의 최종 답변
각 단계에는 직전 답변만이 아니라 지금까지의 전체 대화 컨텍스트를 전달한다.

**MVP 범위**: Python 실행 → 질문 입력 → Claude CLI 호출·저장 → Codex CLI 호출·저장 → Claude → Codex → Claude → 5개 답변 순서대로 출력. Streamlit UI는 MVP 이후. 확장 후보(당시): 진행 표시, 말풍선 구분, 접기/펼치기, FINAL 강조, 대화 기록, 새 대화, 사용자 중간 개입, 토론 횟수 조절, 역할 프롬프트 변경, 모드 선택(일반/코드 리뷰/PLC 검증/투자 분석), CLI별 오류 표시, 타임아웃, 긴 컨텍스트 요약. — 긴 컨텍스트 요약 외에는 09-10 안에 전부 구현됨.

## 2026-09-10 — 환경 확인 결과 (이 PC)

| 항목 | 상태 |
|---|---|
| Claude Code CLI | **2.1.267 독립 설치** `C:\Users\LG\.local\bin\claude.exe` (PATH에 있음). VS Code 확장(2.1.263)과 로그인 정보(`~\.claude\.credentials.json`) 공유 확인 — `claude -p` 테스트 OK, 3~6초 |
| Codex CLI | **0.146.1 winget 설치** (`winget install --id OpenAI.Codex`, Node.js 불필요). 실행 파일 `%LOCALAPPDATA%\Microsoft\WinGet\Packages\OpenAI.Codex_Microsoft.Winget.Source_8wekyb3d8bbwe\codex-x86_64-pc-windows-msvc.exe`. winget이 `codex` 별칭 심볼릭 링크를 못 만들어 `~\.local\bin\codex.cmd` 래퍼를 직접 만들었다. debate.py는 exe를 직접 찾는다. 기본 모델 gpt-5.6-sol |
| Python | uv가 설치한 **3.14.7** (`python3.14` 명령, `~\.local\bin\python3.14.exe`). `python`은 스토어 스텁이라 쓰지 말 것. 프로젝트 venv `.venv` (`python3.14 -m venv .venv`) |
| Node.js / npm | 미설치 (필요 없음) |
| winget | 사용 가능 |

## 2026-09-10 — 확정된 설계 (MVP debate.py)

- 매 단계를 독립된 stateless CLI 호출로 처리하고 전체 transcript를 프롬프트에 포함해 전달한다. 프롬프트는 argv가 아니라 stdin (Windows 명령줄 길이 제한·quoting 회피). 모든 subprocess `encoding="utf-8"`.
- Claude: `claude -p --output-format json --tools "" --no-session-persistence --strict-mcp-config --system-prompt <역할> [--model X] [--effort Y]`, cwd=`sandbox\`. 결과는 JSON `result`, 실패는 `is_error`/`subtype`. **`--bare`는 OAuth를 읽지 않아 구독 계정으로 못 쓴다.**
- Codex: `codex exec --skip-git-repo-check --sandbox read-only --ephemeral --color never -C sandbox -o <임시파일> [-m X] [-c model_reasoning_effort=Y] -` (프롬프트 stdin). 마지막 메시지는 `-o` 파일에서(stdout엔 헤더·로그 혼재). 시스템 프롬프트 옵션이 없어 역할 지시를 프롬프트 맨 앞에. 미로그인 시 exit=1 + stderr `401 Unauthorized`, `-o` 미생성.
- Codex 모델/effort: `codex debug models`가 카탈로그 JSON을 준다(`docs\codex-models.json`에 저장). `visibility=="list"`만 선택 가능: gpt-5.6-sol(기본, effort 기본 low), gpt-5.6-terra, gpt-5.6-luna, gpt-5.5. 지원 effort가 모델별로 다름(sol/terra: low~ultra, luna: ~max, 5.5: ~xhigh). effort는 `-c model_reasoning_effort=high`처럼 따옴표 없이(TOML 파싱 실패 시 문자열 리터럴). 실제 적용값은 codex exec 시작 헤더(stderr)의 `model:`/`reasoning effort:` 줄 → `call_codex()`가 meta에 담는다. `--list-codex-models`, `--codex-effort` 추가.
- Claude Code 세션 안에서 `claude -p`를 부를 때 env의 `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`를 제거해야 중첩 차단을 피한다(`clean_env()`). 제거하면 정상 동작 확인.
- 각 호출에 `timeout`(기본 900s), 실패 시 `CLIError(cli, stage, ...)`. 부분 결과도 저장.
- `run_debate(question, cfg, max_stage, on_event)`가 핵심. `on_event`로 start/done/error → UI가 진행 상태를 그림.
- 구독 계정 사용량 제한이 있으니 연속 토론 횟수 주의. MVP 기본 Claude 모델은 속도 때문에 `sonnet` → 대체됨(09-10 15:30 기본 모델 정책: fable+xhigh).

## 2026-09-10 14:10 — MVP 완료

- 1~4단계 끝: 환경 준비 → CLI 테스트 → `debate.py` → `app.py`(Streamlit).
- Codex 로그인 완료(`codex login status` → Logged in using ChatGPT). `debate.py --check` → claude ✅ 3.3s, codex ✅ 7.3s.
- 5단계 전체 실행 검증(`runs\20260910_140753.md`, Claude=sonnet, Codex gpt-5.6-sol): Initial 23.6s → Review 23.4s → Rebuttal 27.8s → Recheck 29.0s → FINAL 15.9s, 총 약 120s. 단계별 입력 길이 283→1190→2003→3299→4283자(전체 맥락 누적). 타임아웃 900s면 충분.
- `app.py` 검증: AppTest 렌더링 예외 0건, 이전 기록 불러오기 정상, headless health 200. streamlit 1.63.0 설치(Python 3.14 호환 확인).
- **사고/해결**: Streamlit 첫 실행 시 `~\.streamlit\credentials.toml`이 없으면 콘솔에서 "Email:" 입력을 기다려 서버가 안 뜸(브라우저 ERR_CONNECTION_REFUSED) → `[general] email = ""` 파일로 해결.
- PowerShell 실행 정책 기본(Restricted)이라 `Activate.ps1` 막힘 → `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` 안내. 활성화 없이 쓰려면 `.venv\Scripts\python.exe` 직접 지정 또는 `start_ui.cmd`.
- app.py 1차 기능: 채팅 입력창, 사용자/Claude(🟠)/GPT(🟢) 말풍선, 중간 단계 접기, FINAL 강조, 진행 status, 오류 시 단계·CLI명, 사이드바(모델·effort, Codex 카탈로그+직접 입력, 타임아웃·단계 수, 새 대화, runs 불러오기, CLI 점검), 실제 적용 모델 표시, 이어지는 질문에 이전 라운드 질문+FINAL을 `prior`로 전달.

## 2026-09-10 오후 — v2: 스트리밍·개입·모드·라운드·대화 저장

- 엔진: `_popen_stream()` 공통 실행기(stdin 별도 스레드, stdout 라인 스트리밍, 0.25초 `on_tick`, `cancel` Event kill, 타임아웃). `call_claude(on_delta=...)`는 `-p --verbose --output-format stream-json --include-partial-messages`로 `content_block_delta/text_delta` 누적, 마지막 `type=result`에서 결과·meta(stream-json은 --verbose 필수). **Codex `exec --json`은 `item.completed`만 내보내 델타가 없어 글자 단위 스트리밍 불가** → GPT 단계는 경과 시간만. `codex features list`에도 스트리밍 옵션 없음.
- 단계 계획 `plan_stages(rounds, mode)`: Initial → (Review→Rebuttal)×N → Recheck → FINAL. Review/Recheck 마지막 줄에 `[판정: 수정 필요]` / `[판정: 추가 수정 불필요]`, `early_stop`이면 Review '불필요' 시 남은 단계 건너뛰고 FINAL(`gpt_says_ok`, 후에 `reviewer_says_ok`로 일반화).
- 모드 프리셋 `MODES`: general / code_review / plc(GX Works2·Q 시리즈 래더 검증 체크리스트) / invest — 시스템 규칙 + 단계별 힌트.
- 사용자 중간 개입: `{"who":"user","label":"개입"}` 항목 → `[사용자 · 개입]`으로 전달, COMMON_RULES에 최우선 반영 규칙.
- 대화 저장: `chats\<YYYYmmdd_HHMM>_<주제슬러그>.json/.md`(대화 1개 = 여러 라운드). `make_title()`, `slugify()`. 구 `runs\`는 "(구 기록)"으로 1라운드 대화로 열림.
- UI: 단계 실행은 daemon 스레드 + `ss.live` 공유 + `@st.fragment(run_every=0.5)` `live_view()` → 사이드바 조작으로 재실행돼도 단계가 안 끊김. 끝나면 fragment가 `st.rerun()`. ⏹ 중단 = cancel → kill. "단계마다 멈춤"이면 `paused_controls()`. **알려진 제한**: 브라우저 새로고침은 session_state를 지워 진행 중 라운드가 사라짐.
- 설정 `ui_settings.json`(모델·effort·모드·라운드·자동 저장·소리). 자동 저장 끄면 파일 안 만들고 새로고침 시 대화 사라짐, 켜면 새 세션에서 최근 대화 자동 복원. `.md 내려받기`, 삭제(2단계 확인), 완료 시 `st.toast` + `winsound`.
- AppTest 종단 검증(채팅 → Initial 스트리밍 → 일시정지 → 개입 → 계속 → 조기 종료 → FINAL → chats 저장)을 scratchpad `test_app_v2.py`로 돌림 → 저장소에 남기지 않아 사라짐 (09-16 tests\로 정식화).

## 2026-09-10 — 첨부 파일 (텍스트·PDF)

`st.chat_input(accept_file="multiple", file_type=UPLOAD_TYPES)` 📎. `load_attachment(name, bytes)`가 텍스트(utf-8/cp949 자동 판별)·PDF(poppler `pdftotext` → `pypdf` 폴백)를 텍스트로, 파일당 60,000자·전체 150,000자로 잘라 `[첨부 파일]` 블록으로 모든 단계에 전달. 콘솔 `--file` 반복. 첨부 텍스트는 저장 파일에도. 이미지·바이너리는 당시 미지원 → 대체됨(15:10 이미지 첨부).

## 2026-09-10 15:00 — 사고: `codex login --device-auth`가 기존 로그인을 지움

로그인 UI의 출력 형식을 확인하려고 `codex login --device-auth`를 8초 띄웠다가 죽였더니 사용자 Codex가 로그아웃됨. **시작하는 순간 기존 로그인이 지워진다. 절대 테스트 목적으로 실행하지 말 것.** `claude auth login`도 인증 파일을 바꾸므로 테스트 시 백업·복원했음.

## 2026-09-10 15:10 — 계정 관리 패널, 이미지 첨부

- 사이드바 "🔐 계정" 확장 패널. 상태는 `claude auth status --json`(loggedIn/email/subscriptionType)과 `codex login status`(exit 0 = Logged in). 로그아웃 `claude auth logout` / `codex logout`(2단계 확인). **Claude 로그아웃은 VS Code Claude Code와 `~\.claude\.credentials.json`을 공유하므로 그쪽도 함께 풀림.** 로그인은 `LoginFlow`가 `claude auth login` / `codex login --device-auth`를 백그라운드로 띄우고 출력(ANSI 제거, 글자 단위)에서 URL·코드를 뽑아 표시, 2초마다 rerun으로 완료 감지. claude는 브라우저를 스스로 열고 'Paste code here if prompted >'를 기다리며 브라우저에 이미 로그인돼 있으면 코드 없이 'Login successful.'.
- 이미지 첨부: `load_image_attachment()`가 PIL로 긴 변 1568px 축소·재인코딩(PNG/알파 유지, 아니면 JPEG 85) 후 `chats\_img\<시각>\`(자동 저장 꺼짐이면 %TEMP%) 저장. Claude는 `--input-format stream-json`으로 base64 이미지 블록을 stdin에(도구 불필요, 3.5s 검증). Codex는 `-i 경로` — 재로그인 후 15:40 검증(도형·색 정확히 인식, 6s). stateless라 이미지도 매 단계 재전달. UI `st.image`.

## 2026-09-10 15:30 — 기본 모델 정책 (사용자 지정)

Claude = `fable` 별칭 + `xhigh`(별칭이 최신 Fable을 자동 추적, 당시 claude-fable-5-1). Codex = `auto` + `xhigh`. `auto`는 `resolve_codex()`가 실행 시점 카탈로그(`codex debug models`, 1시간 캐시 `cached_codex_models`) 최상위(priority 최소, visibility=list) 모델로 해석 — 당시 gpt-5.6-sol("Reliable agentic workhorse"), 새 모델이 맨 위로 오면 자동 전환. effort 미지원이면 `EFFORT_ORDER`에서 가장 가까운 아래 단계로 낮추고 `meta.resolve_note`에 기록(예: gpt-5.5+ultra→xhigh, luna+ultra→max). `DEFAULT_SETTINGS`·`ui_settings.json`·`Config` 기본값 모두 동일 → 콘솔도 같은 정책.

## 2026-09-10 15:40 — 로그인 상태 캐시 불만 → 자동 재조회

사용자가 터미널에서 로그인했는데 화면이 옛 캐시를 보여줘 "왜 자꾸 로그아웃이라 하냐"는 불만. `get_auth()`가 `AUTH_TTL`(120초)마다 자동 재조회하고, 로그인 절차가 끝나면 종료 코드와 무관하게 실제 상태를 다시 확인하도록 고침.

## 2026-09-10 15:50 — 순서 변경·진행 제어

`plan_stages(rounds, mode, first, final_who, custom)`. 기본 순서 `default_plan(rounds, first, final_who)` = A 최초 → (B 검토 → A 반박)×N → B 재검사 → (final_who 또는 A) 최종. `custom=[(who, kind), ...]`로 임의 순서(`validate_plan`: 첫 단계 initial, 마지막 final, 각 1회). `{other}`는 최초 답변이면 다음 단계 작성자, 그 외엔 직전 작성자로 채워져 순서를 바꿔도 맞음. 조기 종료 판정 `reviewer_says_ok()`(검토자가 누구든). UI: 사이드바 "순서" 라디오(기본/직접 편집) — 직접 편집은 `st.data_editor`(AI, 역할), `ss.plan_base`+`plan_nonce`로 편집 상태 관리(편집본을 다시 data로 넣으면 이중 적용되므로 base 고정). 진행 제어: 실행 중 "⏸ 다음 단계 전에 멈춤"(`pause_next`)·"⏹ 중단"; 일시정지 중 "▶ 계속 / 🔁 직전 단계 다시 생성 / ⏭ 바로 FINAL로 / ⏹ 중단"; 중단·오류 라운드에 "▶ 이어서 진행"(`resume_round`). 기본 `pause_each=False`(사용자 요청: 자동 진행). 라운드 dict에 `plan_steps`, `first`, `final_who`. 콘솔 `--first`, `--final-who`, `--plan`.

## 2026-09-10 16:00 — 텍스트 없는 PDF → 쪽 이미지

사용자가 "Microsoft Print to PDF"로 인쇄해 만든 PDF(8쪽 전부 JPEG, 글꼴 0개)를 올렸는데 추출 텍스트가 공백뿐. `load_attachments(name, data, image_dir)`가 PDF 텍스트 20자 미만이면 `_pdf_pages_to_images()`로 쪽을 PNG로(`pdftoppm -r 130`, 없으면 pypdf `page.images`) 최대 12쪽 이미지 첨부로 변환(이름 "파일명 (n/N쪽)", 첫 장 warning 안내). `_find_tool()`이 PATH 외 winget Poppler 폴더도 탐색(서버 프로세스 PATH에 poppler가 없을 수 있음). 검증: 8쪽 3.1s, Claude/Codex 모두 1쪽 섹션 정확히 판독. UI 이미지 4열. app.py·콘솔 모두 `load_attachments`.

## 2026-09-10 — 모델/effort 적용 검증

사이드바 선택 → `CFG` → `call_claude`/`call_codex` 인자로 실제 전달됨을 AppTest로 확인(opus/low + terra/high → 응답 JSON modelUsage=claude-opus-5, codex 헤더 model=gpt-5.6-terra effort=high). Claude effort는 thinking 토큰으로 확인(low=0, high≈50; low에선 암산 문제를 틀림). Codex 미지원 effort(gpt-5.5+ultra)·엉터리 값은 API 400 → `call_codex`가 stderr JSON `message`를 뽑아 "API 오류: Unsupported value..." 표시. 사이드바 "🔍 현재 설정으로 CLI 점검"·"현재 설정 →" 캡션.

## 2026-09-10 — 사고: debate.py 수정 후 서버 미재시작

서버는 새 `app.py`는 다시 읽지만 이미 import된 `debate` 모듈은 옛것을 써서 `module 'debate' has no attribute ...`. **규칙: `debate.py`를 고치면 8501 프로세스 종료 후 `launch_ui.cmd`로 재시작.** `.streamlit\config.toml`에 `toolbarMode="minimal"`(Deploy 숨김), `gatherUsageStats=false`.

## 2026-09-10 — 실행 파일·바로가기, 사고: .cmd 한글 주석

`launch_ui.cmd`: 8501 LISTENING이면 브라우저만, 아니면 최소화 창으로 서버(서버가 브라우저를 엶). 바탕화면 `AI Debate Room.lnk`가 이 파일을 가리킴(WScript.Shell로 생성). `start_ui.cmd`: 항상 새 서버. **사고**: cmd.exe가 배치 파일을 CP949로 읽어 UTF-8 한글 주석이 깨지며 `'가' is not recognized` 오류 → **.cmd 파일은 ASCII만.**

## 2026-09-16 — 토론 단계 수 3/5 선택, 기본 3단계 (사용자 지정)

사용자 요청: "총 대화 5번을 3번이나 5번으로 고를 수 있게, 기본 3번. 3번이면 초기 → 중간 피드백 → 피드백 반영한 최종본". 구현: `default_plan(rounds, first, final_who, stage_count)`, `plan_stages(..., stage_count)`, `run_debate(..., stage_count)`, `STAGE_COUNTS=[3, 5]`, `DEFAULT_STAGE_COUNT=3`. 3단계 = A 최초 → B 검토 → 최종, 5단계 = 기존 순서. 3단계엔 반박이 없으므로 FINAL 지시문 `{fb_note}`에 "검토 지적을 항목별로 판정해(타당하면 반영, 틀리면 반박) 최종 답변에 녹여라"를 채움(앞에 rebuttal이 있으면 빈 문자열). 검토의 `[판정]` 줄은 그대로 요구하되 3단계는 review 바로 뒤가 final이라 조기 종료로 건너뛸 게 없음. UI: '순서 → 기본'에 '대화 단계 수' 라디오, '검토 ↔ 반박 라운드 수'는 5단계일 때만(3단계는 rounds=1 고정). 설정 키·라운드 dict `stage_count`(구 기록은 `round_stage_count()`가 plan_steps 길이 또는 rounds×2+3, resume 폴백 5). 말풍선 요약 '라운드 N' → 'N단계'. 콘솔 `--stages 3|5`. **주의: `run["stages"]`는 실행된 단계 목록이라 단계 수 키는 반드시 `stage_count`.** 검증: 3/5단계×4모드 계획 생성, AppTest 렌더링 0건, 서버 재시작 완료.

## 2026-09-16 — 하네스 엔지니어링 업그레이드 계획 (6단계)

사용자 질문 "하네스 엔지니어링과 뭐가 다르고 같아지려면?"에 대한 정리. 하네스 엔지니어링(OpenAI 2026-02 글, Anthropic 2025-11 "Effective harnesses for long-running agents")은 코딩 에이전트가 코드베이스에서 안정적으로 일하게 하는 환경 설계 — 저장소를 기록의 원본으로, AGENTS.md는 100줄 목차 + docs/ 세부, 구조 제약을 린터로 강제, 테스트·브라우저·텔레메트리 피드백 루프, 도구 없는 독립 평가자, default-FAIL 증거 계약, PROGRESS 파일 인수인계. 이 프로젝트는 "두 모델이 토론하는 작은 하네스"라 대상이 다르며, 뼈대(stateless 호출·판정 계약·취소/개입·샌드박스)는 이미 있고 부족한 건 저장소 위생과 증거 기반 검증.

순서: **1) 저장소 위생**(git·pytest·requirements) → **2) 문서 분리**(CLAUDE.md 지도화, docs/ARCHITECTURE·DECISIONS·SETUP) → 3) 증거 기반 검증 루프(검토 지적 번호별 반영 계약 default-FAIL, code_review 모드에서 코드 블록 실제 실행, FINAL 뒤 도구 없는 독립 평가자 PASS/NEEDS_WORK) → 4) 관측성(단계별 토큰·비용, chats 집계) → 5) 컨텍스트 압축(긴 라운드 요약) → 6) debate.py 모듈 분리(cli/plan/attachments/store).
참고 저장소: anthropics/cwc-long-running-agents(evaluator.md, test-results.json, kill-switch/steer 훅 — 골라 쓰기용), Gizele1/harness-init(AGENTS.md+docs 스캐폴드), ai-boost/awesome-harness-engineering.

## 2026-09-16 — 1단계: 저장소 위생 (커밋 a851d8a)

- git 저장소로 전환. `.gitignore`: .venv, __pycache__, chats\, runs\, ui_settings.json(개인 설정), sandbox\*(.gitkeep만 추적), *.bak. `.gitattributes`: text=auto, *.cmd만 CRLF. 로컬 git 사용자 설정(전역 없음).
- `requirements.txt`(streamlit==1.63.0, pypdf==6.18.0), `requirements-dev.txt`(+pytest). pytest를 .venv에 설치.
- `tests\`: `conftest.py`의 `isolated` 픽스처가 chats\·runs\·IMAGE_DIR를 임시 폴더로 돌리고 `find_claude/find_codex/claude_auth_status/codex_auth_status/list_codex_models/winsound.MessageBeep`를 가짜로, `ui_settings.json`을 전후로 보존(사이드바 조작이 설정을 저장하므로). `fake_stages(review_ok)`가 `D.execute_stage`를 `FakeStages`로(`build_prompt`까지 실제 경로). `test_plan.py` 엔진 단위 20개, `test_app.py` AppTest 종단 6개 → 33개, 약 3초.
- 확인된 사실: AppTest는 app.py를 같은 프로세스에서 실행해 `import debate as D`가 패치된 모듈을 본다(프로브). 가짜 단계는 즉시 끝나 질문 제출 한 번의 run 안에서 라운드가 통째로 끝날 수 있다 → 테스트는 '시작됐는지'가 아니라 결과로 판단. 빈 custom 계획 `[]`은 '직접 편집 없음'.
- **규칙: 테스트에서 실제 claude/codex를 절대 호출하지 않는다** (사용량·로그인 보호). 코드를 고치면 pytest → 커밋.

## 2026-09-16 — 2단계: 문서 분리

24KB 시간순 일지였던 CLAUDE.md를 **100줄 안팎의 지도**(목표·제약·작업 규칙·실행·구조·현재 상태·문서 지도)로 줄이고, 내용을 `docs\ARCHITECTURE.md`(현재 동작, 날짜 없음), `docs\DECISIONS.md`(이 파일, 시간순 결정·사고), `docs\SETUP.md`(새 PC 설치·다른 컴퓨터 접속·문제 해결)로 옮김. Codex CLI로 개발할 때를 위해 `AGENTS.md`가 CLAUDE.md를 가리킴(토론 실행 시 Codex는 `sandbox\`를 cwd로 불리므로 이 파일을 읽지 않음). 이후 규칙: 결정·사고는 이 파일 맨 아래에, 동작 변경은 ARCHITECTURE.md에, CLAUDE.md는 짧게 유지.

## 2026-09-16 — 3-1단계: 지적 번호별 반영 계약 (default-FAIL)

**문제**: 검토가 "수정 필요"라고 해도 반박/최종이 그 지적을 실제로 다뤘는지 아무도 확인하지 않았다 — 검증이 모델의 말에서 끝났다.
**결정**: 검토/재검사는 지적을 `[지적 N] ...` 한 줄씩(없으면 `[지적 없음]`), 반박/최종은 번호마다 `[반영 N] 한 줄` 또는 `[반박 N] 근거`를 쓰도록 계약을 두고, `execute_stage()`가 프로그램으로 검사한다. 빠진 번호가 있으면 빠진 번호를 명시해 **같은 단계를 1회 재요청**(`MAX_CONTRACT_RETRIES=1`), 그래도 빠지면 `entry["contract"]["missing"]`에 남기고 라운드에 "⚠ 미처리 지적" 경고(default-FAIL: 번호별 기록이 없으면 처리된 것으로 보지 않는다). 검사 대상은 `open_issues(history)` = 직전 AI 단계가 검토/재검사일 때 그 지적들(사용자 개입은 건너뜀) → 5단계에선 반박이 검토를, 최종이 재검사를 검사받고, 조기 종료로 반박을 건너뛰어도 검토에 지적이 있으면 최종이 검사받는다.
**설계 선택**: (1) 검사 로직을 UI·콘솔이 공유하는 `execute_stage` 한 곳에 둠(테스트의 `FakeStages`는 이를 통째로 대체하므로 재요청 경로는 `call_claude/call_codex`를 대역으로 바꾼 `test_contract.py`가 검증). (2) 무한 재요청 대신 1회 + 경고 — 모델이 끝내 형식을 안 지키면 사용자가 보고 판단. (3) 정규식은 굵게·목록 기호를 허용하고 번호는 검토마다 1부터(재검사는 이전 번호를 언급할 때 '1차 검토의 지적 2'처럼 쓰라고 지시). (4) 검토 지시문의 "1) 틀린 부분 2) ..." 항목 번호가 `[지적 N]`과 헷갈려 "·" 구분으로 바꿈. (5) 3단계 FINAL의 `{fb_note}`도 태그 형식으로 재작성(문구 앞부분은 유지해 기존 테스트 호환).
**표시**: 단계 캡션 "🧾 Claude · Review의 지적 2건 전부 처리 — 반영 1 · 반박 1", 라운드 끝 "✅ 검토 지적 N건 전부 처리됨" 또는 `st.warning` "⚠ 미처리 지적: 단계 → 번호". 콘솔 🧾 줄, .md `> 🧾`. 라운드 dict `contract` 합계, run_debate run dict도 동일.
**검증**: `tests/test_contract.py` 14개(파싱, 프롬프트 블록, 재요청 성공/소진/0회, 지적 없음, 합계, run_debate 종단) + test_app에 ✅ 캡션과 ⚠ 경고 시나리오 → 총 47개 통과. 실제 모델이 형식을 얼마나 잘 지키는지는 다음 실제 토론에서 확인 필요(재요청 횟수가 캡션에 보임).
**남은 것**: 3-2 code_review 모드에서 코드 블록 실제 실행(외부 증거), 3-3 FINAL 뒤 도구 없는 독립 평가자.

## 2026-09-16 — 3-2단계: 코드 블록 검사 (외부 증거)

**문제**: 코드가 오가는 토론에서 "이 코드는 동작합니다"가 모델의 주장일 뿐, 프로그램이 확인한 사실이 토론에 들어가지 않았다.
**결정**: 모든 단계 답변의 ```` ``` ```` 블록을 프로그램이 검사해 결과를 다음 단계 프롬프트에 `[프로그램 검사 · …]` 블록으로 넣는다. python은 `ast.parse` 문법 검사, json/toml은 파싱 — **항상**. 실행은 `Config.run_code`(사이드바 "🔬 코드 블록 실제 실행", 콘솔 `--run-code`)를 켰을 때만: `sandbox\_run\<시각>\block.py`를 venv 파이썬 `-I -X utf8`로, stdin 차단, 30초 제한, 실행 후 폴더 삭제, exit 코드+출력 꼬리 1500자.
**설계 선택**: (1) 모드에 상관없이 검사(태그가 python/json/toml인 블록만; PLC 래더 `text` 블록은 무시) — 문법 검사는 부작용이 없다. (2) **실행은 기본 꺼짐** — 모델이 쓴 코드가 이 PC에서 사용자 권한으로 그대로 돌기 때문. 폴더 격리와 타임아웃은 실수 방지용이지 보안 경계가 아니라는 점을 도움말에 적음. (3) 문법 오류 블록은 실행하지 않고, 실행 결과 실패(NameError 등)는 '틀렸다'가 아니라 '이 조각만으로는 안 돈다'일 수 있으므로 판단은 검토 AI에 맡기고 COMMON_RULES에 "실패는 반드시 다루되 통과가 논리의 정당성은 아니다"를 넣음. (4) 증거는 별도 항목이 아니라 `entry["evidence"]`에 붙이고 transcript 렌더링 때 그 항목 뒤에 프로그램 머리말로 삽입 — 모델의 말과 섞이지 않게.
**표시**: 단계 캡션 "🔬 코드 검사 2개 블록 — OK 1 · 실패 1 (문법·파싱만)", 실패가 있으면 ❌와 상세. 콘솔·.md에도.
**검증**: `tests/test_evidence.py` 10개 — 실제 파이썬 실행(출력·exit 3·무한 루프 1초 타임아웃·`input()` EOFError), 실행 폴더 정리, execute_stage → 다음 단계 프롬프트 전달, run_code 꺼짐이면 실행 안 함. FakeStages도 실제 `check_code_blocks`를 태워 UI 테스트에 🔬 캡션 확인. 총 57개.
**한계**: 스크립트 단위 실행이라 pytest 같은 테스트 러너나 여러 파일 프로젝트는 못 돌린다. 코드가 요구사항을 만족하는지는 여전히 모델 판단.

## 2026-09-16 — 3-3단계: FINAL 뒤 독립 평가자

**문제**: FINAL을 쓴 모델이 스스로 "완결됐다"고 끝냈다. 다른 눈으로 최종본만 채점하는 단계가 없었다 (Anthropic `evaluator.md` 패턴 — 새 컨텍스트, 쓰기 권한 없는 평가자).
**결정**: `evaluate` 단계 종류 추가. 최종 정리를 쓰지 않은 쪽 AI가 새 stateless 호출로 FINAL만 채점: `[지적 N]` + 마지막 줄 `[평가: PASS]`/`[평가: NEEDS_WORK]`. NEEDS_WORK면(옵션 `eval_revise`) `FINAL 2` + `Eval 2`를 붙여 **1회만** 재작성·재평가. 평가자를 `REVIEW_KINDS`에 넣어 FINAL 2가 평가자의 지적을 3-1 반영 계약으로 검사받게 함 — 평가 → 재작성 → 검사 → 재평가가 하나의 루프.
**설계 선택**: (1) 조기 종료와 재작성이라는 두 가지 계획 조정을 `adjust_plan_after()` 한 함수로 통합해 UI(`run_active_stage`)와 콘솔(`run_debate`)의 중복 로직을 없앰. (2) 재작성은 1회 상한 — 두 모델이 계속 엇갈리면 사람이 봐야 한다. (3) 함수 기본은 `evaluate=False`(기존 테스트·API 호환), UI·콘솔 기본은 켜짐(호출 1~3회 추가 = 구독 사용량 증가, 도움말에 명시). (4) 직접 편집 계획에서는 체크박스가 아니라 표에 '평가' 행을 넣는다(`validate_plan`: 최종 정리 바로 뒤 맨 마지막, 1회). (5) 단계 수 캡션은 평가·FINAL 2를 세지 않는다(`count_debate_stages`) — 사용자가 고른 "3단계"와 화면이 일치하도록. (6) `final_of`는 FINAL 2 우선 — 다음 라운드의 `prior`와 저장 목록이 재작성본을 쓴다.
**표시**: 평가 항목 제목에 ✅ PASS / ⚠ NEEDS_WORK 배지, 라운드 끝 "🧑‍⚖️ 독립 평가: PASS (FINAL 재작성 후 재평가)" 또는 경고. 콘솔 🔁/🧑‍⚖️ 줄, .md에도.
**검증**: `tests/test_eval.py` 10개 + UI 종단 1개(평가 → NEEDS_WORK → FINAL 2 계약 통과 → Eval 2 PASS, 단계 수 캡션 "3단계") → 68개. 처음 돌렸을 때 `validate_plan`의 검사 순서가 바뀌어 "마지막 단계" 오류 메시지가 "한 번만"으로 나오는 회귀를 테스트가 잡아 고침.
**한계**: 평가자도 모델이라 FINAL 작성자와 같은 오해를 공유할 수 있다. 실제 모델이 `[평가: ...]` 형식을 지키는지는 실전 확인 필요(형식이 없으면 "판정 형식 없음"으로 표시되고 재작성은 일어나지 않음).

## 2026-09-16 — 4단계: 관측 (토큰·비용·시간, 전체 통계)

**문제**: 단계별 시간과 모델명만 남아 "이 토론이 얼마나 썼는지", "계약·평가가 실제로 얼마나 작동하는지"를 숫자로 볼 수 없었다.
**결정**: Claude 결과 JSON의 `usage`(이미 저장 중)와 `total_cost_usd`(새로 저장, API 환산 — 구독이라 실제 과금 아님)를 `usage_of()`로 해석해 단계 캡션·라운드 합계·저장 dict(`usage`)에 넣는다. Codex는 CLI 출력의 `tokens used: N`을 찾되 없으면 `?` — 현재 `codex exec` 출력엔 그 줄이 없어 GPT 토큰은 대개 모름(`--json`으로 바꾸면 `turn.completed`의 usage를 받을 수 있지만 헤더 파싱·`-o` 동작이 바뀔 수 있어 실기 검증 없이는 안 바꿈). 저장된 대화 전체 집계 `stats()`: 라운드·조기 종료율, AI별 단계 수·평균 시간, 토큰·비용, 반영 계약(지적/반영/반박/재요청/미처리 라운드), 독립 평가(PASS/NEEDS_WORK/재작성). 콘솔 `--stats`, 사이드바 "📊 통계" 확장.
**실기 확인**: `debate.py --stats` → 대화 6개(구 runs 2개 포함)·라운드 9개, Claude 14단계 평균 61.1s, GPT 16단계 평균 41.5s, Claude 토큰 227.6k. 비용은 옛 기록에 `cost_usd`가 없어 $0.00 — 이후 라운드부터 쌓인다.
**검증**: `tests/test_usage.py` 7개 + UI 캡션·저장 dict 확인 → 75개. FakeStages meta를 실제 CLI와 같은 모양으로 바꿔 UI 경로도 토큰을 태움.
**한계**: GPT 토큰 미확인, 비용은 Claude API 단가 환산치. (테스트 총 시간은 약 8초 — 처음 한 번 14초가 나왔지만 재실행에서 7.7초, 가장 느린 테스트가 1.4초라 일회성 지연이었다.)

## 2026-09-16 — 5단계: 컨텍스트 압축 (긴 토론 요약)

**문제**: 매 단계에 전체 기록을 그대로 넣어 5단계+평가+재작성이면 입력이 수만 자로 불고, 첨부가 크면 더하다. 프로젝트 시작 때부터 TODO였던 "긴 컨텍스트 요약".
**결정**: 기록이 `compact_chars`(기본 60,000자)를 넘으면 마지막 2개 항목만 원문, 그 앞은 Claude(effort low)가 요약한 블록으로 대체. 요약은 라운드 캐시에 **요약 대상 내용의 해시**를 키로 저장 — 같은 대상이면 재사용, "직전 단계 다시 생성"으로 내용이 바뀌면 재요약. 요약 호출 실패 시 항목당 앞 800자를 잘라 붙이는 기계적 요약으로 대체(토론이 요약 때문에 멈추지 않게).
**설계 선택**: (1) 요약 블록에 "프로그램이 길이를 줄이려고 Claude가 요약, 원문은 아래 최근 단계만"이라는 머리말을 붙여 모델이 요약을 원문으로 착각하지 않게. (2) `SUMMARY_RULES`가 [지적/반영/반박/판정/평가] 줄과 프로그램 검사 결과를 번호째 보존하라고 지시 — 반영 계약·평가 맥락이 요약을 거쳐도 유지되도록. (3) 반영 계약(`open_issues`)·조기 종료·평가 판정은 항상 **원문 기록**으로 계산하고 계약 블록엔 지적 원문이 들어가므로 요약 품질이 검증 논리를 흔들지 않는다. (4) `compaction=None`이면 압축 안 함 — 기존 호출·테스트 호환, UI는 `active["compaction"]`, 콘솔은 `run_debate` 지역 dict. (5) 첨부는 별도(150,000자 절단) — 첨부까지 요약하면 근거 원문이 사라지므로 이번엔 손대지 않음.
**표시**: 요약이 들어간 단계 캡션 "🗜 이전 단계 n개를 요약해 전달 (Claude 요약 | 요약 호출 실패 → 앞부분만 잘라 붙임)", 라운드 dict `compaction`에 요약문 저장(모델이 실제로 본 것을 나중에 확인 가능).
**검증**: `tests/test_compact.py` 9개(필요 판정, 캐시 히트/미스, effort low, 실패 폴백, 프롬프트 치환, execute_stage 표시, run_debate 5단계에서 요약 2회) → 84개.
**한계**: 요약도 모델 호출이라 라운드당 1~2회 추가 호출·시간. 요약 품질은 검증하지 않는다(다음 단계 모델이 요약을 근거로 틀릴 수 있음). 기준 60,000자는 경험값 — 실제 긴 토론에서 조정 필요.

## 2026-09-17 — 6단계: debate.py → engine/ 패키지 분리

**문제**: `debate.py`가 1,837줄·80여 함수로 CLI 실행·계획·프롬프트·첨부·검증·관측·저장·콘솔이 한 파일에 있었다. 하네스 엔지니어링의 "경계가 뚜렷한 구조" 원칙과 가장 멀었다.
**결정**: 12개 모듈의 `engine/` 패키지로 나누고 `debate.py`는 facade + 콘솔 진입점으로 남김(`import debate as D`, `python debate.py`, `launch_ui.cmd`, 문서의 명령 전부 그대로). 의존 순서 config → attachments → cli → contract → plan → evidence → usage → prompt → compact → runner → store → console, 각 모듈은 앞선 모듈만 star-import(순환 없음).
**설계 선택**: (1) 파일 순서대로 자르면 compact가 prompt를 참조해 순환이 생겨 import 순서를 파일 순서와 분리했고, 순환을 끊기 위해 `DISPLAY/OTHER`→config, `TOKENS_USED_RE`→cli, `reviewer_says_ok`→contract, `render_compaction`→prompt로 옮김. (2) 모듈 간 star-import는 이름 바인딩을 여러 모듈에 남기므로 테스트 대역을 한 곳만 바꾸면 안 먹는다 → `conftest.patch_all()`이 facade와 `engine.*` 전부를 바꾸도록 하고 모든 테스트를 그 헬퍼로 통일(분리 전 모놀리스에서도 통과 확인). (3) 밑줄 이름(`_run_python` 등)은 star-import에 안 실리므로 facade에서 명시 export. (4) 분리는 스크립트로(앵커 문자열 기준 슬라이스) 수행해 손으로 옮기다 생기는 누락을 피했고, 첫 시도에서 `render_transcript` NameError가 나 git으로 되돌린 뒤 순서를 고쳐 재실행.
**검증**: 84개 전부 통과, `debate.py --stats/--help` 정상, AppTest 렌더링 예외 0. 첫 실행에서 테스트가 125초 걸렸으나 재실행 5초(새 파일 생성 직후 일회성 지연).
**남은 것**: `app.py`(869줄)는 아직 한 파일 — 사이드바/라운드 실행/렌더링으로 나눌 수 있지만 Streamlit 스크립트 특성상 이득이 작아 보류. 하네스 업그레이드 6단계 전부 완료.

## 2026-09-17 — 7단계: 도구 허용(웹 검색 / 파일·명령)과 행동 기록

**계기**: 사용자가 뉴스를 물었더니 "웹 검색과 실시간 확인이 금지"라는 답을 받고 "원래 하네스 엔지니어링은 이런 걸 다 막나?"라고 물음. 답은 "아니다 — 하네스는 끄는 게 아니라 범위를 정하고 기록한다(Humans steer, agents execute; 도구가 없는 건 평가자뿐)". 사용자 결정: 작업 폴더는 대화별 격리 `workspace\`, 명령은 허용 목록.
**결정**: 토글 둘(기본 꺼짐) — 🌐 `web_search`(Claude `--tools WebSearch,WebFetch --allowedTools …`, Codex `--search`), 🛠 `tools`(Claude `--tools Read,Glob,Grep,Edit,Write,Bash --allowedTools "Bash(python *) Bash(pytest *) Bash(pip *) Bash(git *) Bash(ls *) …" --permission-mode acceptEdits`, Codex `--sandbox workspace-write -C workspace`). `-p` 모드는 승인을 물을 수 없으므로 허용 목록 밖은 자동 거부되고 그 거부가 경계(`permission_denials`도 기록). `--dangerously-skip-permissions`/`danger-full-access`는 쓰지 않음. 규칙 문구가 도구 상태에 맞게 바뀜(`tool_rules`).
**행동 기록이 핵심**: Claude stream-json의 tool_use/tool_result, Codex `--json`의 item 이벤트를 파싱해 `entry["actions"]`로 저장하고 대화 기록에 `[프로그램 기록 · …의 도구 사용]` 블록으로 삽입 — 상대 AI·평가자가 실제 명령·결과·검색·파일 변경을 보고 판단하며(규칙: 기록과 다른 주장은 기록을 믿으라), 웹 검색 결과도 URL과 함께 남는다. 이로써 이전 항목의 "실시간 확인 결과가 기록에 안 남는" 문제를 해결. 작업 폴더는 단계마다 `git commit`(`workspace_commit`)해 파일 변경이 `entry["workspace"]`와 git log에 남는다. `clean_env(tools=True)`는 `.venv\Scripts`를 PATH 앞에 둬 모델의 `python`이 스토어 스텁이 아니라 venv를 잡게 함.
**설계 선택**: (1) 웹과 파일·명령을 별도 토글로 — 뉴스 확인엔 웹만 켜면 되고 사고 범위가 다르다. (2) Codex `--json`은 도구를 켰을 때만 — 기본 경로(-o 파일·헤더 파싱)는 검증된 그대로. `--json`이면 `turn.completed.usage`로 GPT 토큰도 잡힌다(부수 효과). (3) 대화별 workspace를 conv에 저장해 이어지는 라운드가 같은 폴더를 씀. (4) 평가자에게도 같은 Config가 가므로 도구가 열린다 — 검증용 실행은 허용하되 채점 규칙은 그대로.
**검증**: `tests/test_tools.py` 11개(인자·파서·규칙·기록·git 커밋·run_debate) + UI 토글 1개 → 96개. **실기 검증은 못 함**: 도구 권한을 가진 CLI 에이전트를 띄우는 프로브가 이 세션의 자동 모드 분류기에 막혔다(우회하지 않음). `tools/probe_tools.py`(Claude 2회·Codex 1회, 저렴한 설정, 원본 출력 저장)를 사용자가 직접 실행해 이벤트 형식(특히 Codex `--json` item 이름)과 허용 목록 동작을 확인한 뒤 파서를 맞춰야 한다.
**위험(문서·도움말에 명시)**: 허용 목록 안이라도 `python`은 임의 코드 실행. 격리·타임아웃·중단은 실수 방지지 보안 경계가 아님. 도구를 켠 채 LAN 공유 금지.

## 2026-09-17 — 도구 토글 기본값을 켜짐으로 (사용자 지정)

사용자 질문 "기본을 꺼둔 이유가 토큰·시간 때문이냐, 켜면 성능이 좋은 거냐"에 대한 정리: 꺼둔 이유는 위험 범위 > 재현성 > 시간·사용량 순이고, 성능은 질문 종류에 따라 갈린다 — 최신 사실·코드 실행이 필요한 질문은 켜는 게 확실히 낫고, PLC 래더·설계 검토·첨부 분석처럼 근거가 이미 안에 있는 질문은 검색이 추론을 밀어내고 두 AI를 같은 검색 결과에 묶어 독립 검토가 약해질 수 있다. 사용자 결정: **🌐·🛠 둘 다 기본 켜짐, 필요하면 사이드바에서 끔.** 구현: `DEFAULT_SETTINGS` True, 콘솔 `--web/--tools` → `--no-web/--no-tools`. 엔진 `Config` 기본은 꺼진 채 둠(라이브러리 호출·테스트가 실수로 도구를 켜지 않게 — 독립 평가 때와 같은 원칙). 테스트: `isolated` 픽스처가 `WORKSPACES`를 임시 폴더로 돌려 UI 테스트가 프로젝트에 workspace를 만들지 않게 함, 기본 켜짐/끔 시나리오 2개 → 97개.
모드별 기본값(예: PLC 검증은 끔)은 제안만 하고 아직 넣지 않음.

## 2026-09-17 — 7단계 실기 검증 (사용자가 tools/probe_tools.py 실행)

**Claude — 설계대로 동작**: 웹 프로브는 `WebSearch` tool_use가 파서에 잡히고 답에 출처 URL·확인 시각이 붙음(sonnet/low, API 환산 $0.064). 파일·명령 프로브는 `Write hello.py → Bash "python hello.py" → hi 42`가 기록되고 workspace에 git 커밋(`fcfe9f4`)됐으며, 허용 목록 밖 `curl --version`은 **"This command requires approval"로 거부**되고 result의 `permission_denials`에 남았다 — 허용 목록이 실제 경계로 작동. `permissionMode: acceptEdits`, 도구 목록도 init 이벤트에서 확인.
**Codex — 실패 → 수정**: `codex exec --search`가 `unexpected argument '--search'`. 도움말의 `--search`는 최상위(대화형 CLI) 옵션이었다. `codex features list`에서 `web_search_request`/`web_search_cached`는 deprecated, 새 방식은 설정 키 → `-c web_search=live`로 변경. 키를 모르는 버전이면 `--strict-config`가 아니라서 무시되고 검색만 안 되므로 라운드는 깨지지 않는다. Codex `--json` 이벤트 형식(item 종류 이름)은 아직 실기 미확인 — 프로브에 `--only codex`와 이벤트 종류 통계 출력을 넣어 재실행 요청.

## 2026-09-17 — 실기 데이터로 고친 것: 요약자·평가자 범위, Codex 파서, 검색 범위

**실기 데이터**: (1) 11:01 Codex 프로브 — `-c web_search=live`로 검색 성공, 이벤트는 `item.started/completed` 쌍에 `command_execution{command, aggregated_output, exit_code, status}` / `web_search{query, action{type, query}}` / `file_change{changes[{path, kind}]}` / `agent_message` / `turn.completed{usage}`. 명령은 `"…powershell.exe" -Command '…'` 래퍼로 오고, **샌드박스 안에서 venv 파이썬(uv 트램폴린)이 "did not find executable at <uv 경로>"로 실패**(Get-Command는 venv 경로를 가리켰음 — 샌드박스가 workspace 밖 실행 파일 접근을 막는 것으로 보임). (2) 09:12 실제 토론 2라운드 — 1라운드(도구 끔) `[지적 3]`→`[반영 3]` 재요청 0, Eval PASS; 2라운드(도구+웹 전체 단계) `[지적 5]`→5/5, PASS, 그러나 **11분, Claude API 환산 $3, GPT 입력 516k+410k 토큰** — 단계마다 검색을 반복한 비용.
**결정**: ① 요약자(압축)는 `tools=False, web_search=False`로 호출 — 같은 Config가 넘어가 요약 중에 도구가 열려 있던 결함. ② 평가자는 `readonly=True` — Claude Edit/Write 제외·`acceptEdits` 없음, Codex `read-only`(Anthropic 평가자 패턴). 채점자가 FINAL 뒤에 파일을 고칠 수 있던 결함. ③ Codex 파서: `_tidy_command`(셸 래퍼·`cd <ws> &&` 제거), `_rel`(workspace 상대 경로), `exit_code` 문자열 정규화, `status: failed`, `action.query` 폴백 — 실기 출력을 축약한 테스트 고정. ④ **검색 범위 `web_scope`** 기본 `initial_eval`(최초 답변 + 평가): 최초 답변의 검색 결과는 기록으로 남아 뒤 단계가 재사용(규칙 `web_reuse`), 평가자만 다시 확인. 라운드당 검색 4회→2회. `initial`/`all` 선택 가능(UI 선택 상자, 콘솔 `--web-scope`).
**미결**: Codex 샌드박스의 파이썬 실행 — 프로브 `--only codex`가 `--add-dir <.venv> <uv 폴더>`로 열리는지 시험하도록 바꿈. 안 되면 "Codex는 파일·git·PowerShell 명령, 파이썬 실행은 Claude" 제한으로 문서화.
**검증**: 102개(평가자 읽기 전용 인자, 범위 판정, 단계별 cfg 적용 spy, 파서 실기 형식, 요약자 도구 없음, UI 기본 범위).

## 2026-09-17 — 사이클 3: 코드 검토로 찾은 결함 + 실시간 도구 표시 + 작업 폴더 정리 + 사이드바 정리

사용자 지시 "계속 피드백하면서 진행, 오류 수정". 실패한 라운드·서버 오류는 없어 코드 검토로 잠재 결함을 찾아 고침.
**결함 수정**: ① 사이드바 "CLI 점검"과 콘솔 `--check`가 도구가 켜진 Config로 호출됐음 → 점검은 `tools=False, web_search=False`. ② 압축 요약 입력에 `[프로그램 기록]`(검색 결과·URL·명령 결과)이 빠져 요약을 거치면 근거가 사라졌음 → 포함 + `SUMMARY_RULES` 보존 지시. ③ 대화를 삭제해도 작업 폴더가 남았음 → `delete_conversation`이 연결된 workspace도 삭제. ④ Codex `--json` 모드에서 시작 헤더가 없으면 모델·effort가 None으로 표시될 수 있음 → 요청값으로 보완하고 `resolve_note`에 표시.
**개선**: 실시간 도구 표시(`on_action` 콜백, `CodexLiveParser`가 `item.started`부터 보여줌 — GPT 단계는 글자 스트리밍이 없어 이게 유일한 진행 표시), "📁 작업 폴더" 확장(개수·용량·고아 폴더 정리 버튼), 사이드바를 "🛠 도구"·"🧠 모델 · 실행 설정" expander로 접어 기본 화면을 짧게(토글 12개 → 순서·평가만 노출).
**설계 선택**: 실시간 표시는 최종 기록과 분리 — 스트리밍 파서는 UI용 임시, 저장되는 `entry["actions"]`는 전체 stdout을 다시 파싱한 결과(중복·순서 문제 방지). 작업 폴더 삭제는 `WORKSPACES` 밖이면 거부하고 읽기 전용 .git 파일은 chmod 후 삭제. expander 정리는 줄 범위를 들여쓰는 스크립트로 수행해 위젯 코드는 그대로.
**검증**: 109개(사이클 3 테스트 7개 + UI expander·정리 버튼 1개). 첫 실행에서 FakeCLI 대역 시그니처에 `on_action`이 없어 19개가 깨졌고 대역을 맞춰 통과 — 대역은 실제 시그니처를 따라가야 한다는 교훈(`conftest.FakeCLI`).

## 2026-09-17 — 사이클 4: 실기 데이터 2건 반영 (Codex 파이썬 해결, 도구 호출 상한)

**실기 1 — Codex 프로브 `--only codex`(--add-dir 시험)**: `Edit hello2.py → Bash python hello2.py → hi 43`, WebSearch, `Get-Date` 모두 성공, stderr 비어 있음, 토큰 73k. → `CODEX_ADD_DIRS`(venv, uv 기반 파이썬 폴더)를 `codex_tool_args`가 tools일 때 `--add-dir`로 넣도록 정식 적용. 프로브의 임시 패치는 제거.
**실기 2 — 실제 토론 "최근뉴스 정리해줘"(새 기본: 도구 켬, 검색 범위 최초+평가)**: 6단계 489초(전체 단계 검색이던 전날 2라운드 665초보다 27% 단축). GPT 최초 답변이 WebSearch 5회(223k 토큰), Claude 검토 `[지적 7]`(Bash `ls`/`git` 1회, exit 128 실패 — 쓸모없는 호출), GPT FINAL 7/7 반영, **Claude 평가 NEEDS_WORK**(`[지적 5]`, WebFetch 6회 중 3회 "unable to fetch"(yna·apnews 차단) + WebSearch, 145초, $1.14), GPT FINAL 2 5/5 반영, Claude Eval 2 **PASS**(Bash date + WebFetch/WebSearch 4회, 98초, $0.90). 반영 계약 12/12, 재요청 0, 재작성 루프 정상. 비용은 평가자 두 번이 절반 이상 — 평가자가 사실 확인에 열심인 건 좋으나 상한이 필요.
**결정**: ① 단계당 도구 호출 권고 상한 `tool_budget`(8)·평가자 `eval_tool_budget`(5)을 규칙 문구로(이 CLI엔 `--max-turns`가 없고 `--max-budget-usd`는 구독에서 의미가 불명확해 안 씀). ② 웹 규칙에 "조회 실패 URL 재시도 금지". ③ 평가자 지시문에 "핵심 주장 5개 이내". ④ UI 도구 확장에 상한 입력.
**검증**: 112개(add-dir 인자, 상한 문구·평가자 적용 spy, 평가자 지시문, UI 기본값). 실기 효과는 다음 실제 토론에서 평가 단계의 조회 횟수·시간으로 확인.

## 2026-09-18 — 공유판 준비 (GitHub 공개)

사용자가 외부에 써보라고 공유하길 원함 → GitHub 공개 저장소로. 준비: `README.md`(외부인용 — 무엇·요구사항·5분 설치·사용법·도구 경고·문서 지도), `LICENSE`(MIT), `docs/PROMPTS.md`(`tools/export_prompts.py`가 엔진 상수에서 자동 생성 — 코드 없이 프롬프트만 쓰려는 사람용), 문서의 개인 경로 일반화, 개인 언급 1건 정리.
**기본값 결정**: 🌐·🛠 도구 토글을 **기본 꺼짐**으로 되돌림(UI·콘솔·엔진 모두). 이유: 모르는 사람이 받아 첫 실행에 "모델이 내 PC에서 명령을 돌린다"는 나쁜 놀라움이고, 하네스 원칙(사람이 켜는 결정)에도 맞다. 소유자의 개인 설정은 `ui_settings.json`(git 제외)에 켜진 값을 넣어 두어 그대로 유지. 콘솔 플래그는 `--web/--tools`(켜기)로 복귀.
git 사용자 안내: 저장소는 이미 로컬 git으로 관리 중(커밋 15개), 브랜치를 `main`으로 바꾸고 GitHub 원격을 붙여 푸시. 개인 데이터(chats/, workspace/, runs/, ui_settings.json, tools/probe_out/)는 `.gitignore`로 제외돼 올라가지 않는다.
**결과**: 사용자가 GitHub Desktop(winget 설치, 바탕화면 바로가기)으로 로그인 → Add local repository → Publish. 공개 저장소 https://github.com/an2889853-bot/ai-debate-room (계정 `an2889853-bot`). 테스트 격리 수정(테스트 중 `ui_settings.json`을 빈 설정으로 — 소유자의 "도구 켜짐" 설정이 기본값 테스트에 섞여 2개가 깨졌던 것)도 함께 올라감.
