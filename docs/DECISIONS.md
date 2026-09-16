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

사용자가 "Microsoft Print to PDF"로 뽑은 지원서 PDF(8쪽 전부 JPEG, 글꼴 0개)를 올렸는데 추출 텍스트가 공백뿐. `load_attachments(name, data, image_dir)`가 PDF 텍스트 20자 미만이면 `_pdf_pages_to_images()`로 쪽을 PNG로(`pdftoppm -r 130`, 없으면 pypdf `page.images`) 최대 12쪽 이미지 첨부로 변환(이름 "파일명 (n/N쪽)", 첫 장 warning 안내). `_find_tool()`이 PATH 외 winget Poppler 폴더도 탐색(서버 프로세스 PATH에 poppler가 없을 수 있음). 검증: 8쪽 3.1s, Claude/Codex 모두 1쪽 섹션 정확히 판독. UI 이미지 4열. app.py·콘솔 모두 `load_attachments`.

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
