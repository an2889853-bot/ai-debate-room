# AI Debate Room — 프로젝트 지침 (CLAUDE.md)

이 파일은 Claude Code가 이 폴더에서 시작할 때 자동으로 읽는다. 이전 세션(2026-09-10)에서 정리한 컨텍스트와 환경 확인 결과를 담고 있다. **새 세션은 "현재 진행 상태" 절부터 이어간다.**

## 목표
Windows 11 + VS Code 환경에서 Claude와 GPT가 한 채팅창에서 자동으로 서로 검토·반박하며 하나의 답을 만드는 **로컬 AI Debate Room**을 만든다.

## 제약 (반드시 지킬 것)
- API 키 과금 방식은 쓰지 않는다. 구독 계정으로 로그인된 **Claude Code CLI**와 **OpenAI Codex CLI**를 Python `subprocess`로 호출한다.
- CLI 옵션은 버전마다 다르므로 **임의로 가정하지 말고 `--help`, `--version` 실제 출력을 먼저 확인**한 뒤 결정한다. help 출력은 `docs\` 폴더에 저장돼 있다 (`claude-help.txt`, `codex-help.txt`, `codex-exec-help.txt`, `codex-login-help.txt`).
- 코드는 한 번에 많이 주지 않는다. 사용자가 **직접 하나씩 실행하며 따라갈 수 있게 단계별로** 진행하고, 각 단계 결과를 확인한 뒤 다음으로 넘어간다. 이 세션은 사용자 PC에서 직접 실행되므로 확인 명령은 Claude가 PowerShell로 직접 실행해도 된다. 단, 설치처럼 시스템을 바꾸는 명령은 실행 전에 무엇을 설치하는지 한 줄로 알린다.
- 사용자는 전기공학 전공 자동화/로봇 인턴. Python 경험은 보통 수준으로 가정. 설명은 한국어.

## 토론 순서 (고정)
1. Claude · Initial — 사용자 질문 분석, 해결책 제시
2. GPT · Review — 사용자 질문 + Claude 답변을 보고 오류·허점·누락·개선점·대안 검토
3. Claude · Rebuttal — 전체 맥락을 보고, 맞는 지적은 수정, 틀린 지적은 근거로 반박, 2차 답변 작성
4. GPT · Recheck — 전체 토론을 보고 남은 문제와 1차 검토 반영 여부 확인
5. Claude · FINAL — 전체 토론을 종합해 사용자에게 보여줄 하나의 최종 답변 작성

각 단계에는 직전 답변만이 아니라 **지금까지의 전체 대화 컨텍스트**를 전달한다.

## MVP 범위
1. Python 실행 → 2. 질문 입력 → 3. Claude CLI 호출·저장 → 4. Codex CLI 호출·저장 → 5. Claude → 6. Codex → 7. Claude → 8. 5개 답변을 순서대로 출력.
Streamlit UI는 MVP 이후. 확장 후보: 진행 표시, 말풍선 구분, 접기/펼치기, FINAL 강조, 대화 기록, 새 대화, 사용자 중간 개입, 토론 횟수 조절, 역할 프롬프트 변경, 모드 선택(일반/코드 리뷰/PLC 검증/투자 분석), CLI별 오류 표시, 타임아웃, 긴 컨텍스트 요약.

## 확정된 설계 (debate.py에 구현됨)
- 매 단계를 **독립된 stateless CLI 호출**로 처리하고, 전체 transcript를 프롬프트에 포함해 전달한다. 프롬프트는 argv가 아니라 **stdin**으로 넘긴다 (Windows 명령줄 길이 제한·quoting 회피). 모든 subprocess는 `encoding="utf-8"`.
- Claude: `claude -p --output-format json --tools "" --no-session-persistence --strict-mcp-config --system-prompt <역할> [--model X] [--effort Y]`, cwd=`sandbox\`. 결과는 JSON의 `result` 필드, 실패는 `is_error`/`subtype`. **`--bare`는 OAuth를 읽지 않아 구독 계정으로 못 쓴다.**
- Codex: `codex exec --skip-git-repo-check --sandbox read-only --ephemeral --color never -C sandbox -o <임시파일> [-m X] [-c model_reasoning_effort=Y] -` (프롬프트 stdin). 마지막 메시지는 `-o` 파일에서 읽는다 (stdout엔 헤더·로그가 섞임). 시스템 프롬프트 옵션이 없어 역할 지시를 프롬프트 맨 앞에 붙인다. 미로그인 시 exit=1 + stderr에 `401 Unauthorized`, `-o` 파일 미생성.
- Codex 모델/effort: `codex debug models`가 카탈로그 JSON을 준다(`docs\codex-models.json`에 저장). `visibility=="list"`인 모델만 선택 가능: gpt-5.6-sol(기본, effort 기본 low), gpt-5.6-terra, gpt-5.6-luna, gpt-5.5. 모델별 지원 effort가 다름(sol/terra: low~ultra, luna: ~max, 5.5: ~xhigh). effort는 `-c model_reasoning_effort=high`처럼 따옴표 없이 넘긴다(TOML 파싱 실패 시 문자열 리터럴로 처리됨). 실제 적용값은 codex exec 시작 헤더(stderr)의 `model:`/`reasoning effort:` 줄로 확인하며 `call_codex()`가 meta에 담는다. `debate.py --list-codex-models`, `--codex-effort` 있음.
- Claude Code 세션 안에서 `claude -p`를 호출할 때는 env에서 `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`를 제거해야 중첩 차단을 피한다 (`clean_env()`). 제거하면 정상 동작 확인됨.
- 각 호출에 `timeout`(기본 900s)을 걸고, 실패 시 `CLIError(cli, stage, ...)`로 어느 CLI·어느 단계인지 표시. 부분 결과도 `runs\`에 저장.
- `run_debate(question, cfg, max_stage, on_event)`가 핵심 함수. `on_event`로 start/done/error 이벤트를 받으므로 Streamlit UI는 이 콜백으로 진행 상태를 그리면 된다.
- 구독 계정에는 사용량 제한이 있으므로 연속 토론 횟수에 주의. MVP 기본 Claude 모델은 속도 때문에 `sonnet` (`--claude-model opus` 등으로 변경 가능, `None`이면 settings.json 기본값 fable 5.1).

## 환경 확인 결과 (2026-09-10, 이 PC)
| 항목 | 상태 |
|---|---|
| Claude Code CLI | **2.1.267 독립 설치 완료** `C:\Users\LG\.local\bin\claude.exe` (PATH에 있음). VS Code 확장(2.1.263)과 로그인 정보(`~\.claude\.credentials.json`) 공유 확인 — `claude -p` 테스트 OK, 3~6초 |
| Codex CLI | **0.146.1 winget 설치 완료** (`winget install --id OpenAI.Codex`, Node.js 불필요). 실행 파일은 `%LOCALAPPDATA%\Microsoft\WinGet\Packages\OpenAI.Codex_Microsoft.Winget.Source_8wekyb3d8bbwe\codex-x86_64-pc-windows-msvc.exe`. winget이 `codex` 별칭 심볼릭 링크를 못 만들어 `~\.local\bin\codex.cmd` 래퍼를 직접 만들었다. debate.py는 exe를 직접 찾는다. **아직 로그인 안 됨** (`codex login status` → Not logged in). 기본 모델 gpt-5.6-sol |
| Python | uv가 설치한 **3.14.7** (`python3.14` 명령, `~\.local\bin\python3.14.exe`). `python`은 스토어 스텁이라 쓰지 말 것. 프로젝트 venv `.venv` 생성 완료 (`python3.14 -m venv .venv`) |
| Node.js / npm | 미설치 (필요 없음) |
| winget | 사용 가능 |

## 현재 진행 상태 (2026-09-10 14:10 기준)
- **MVP 완료.** 1~4단계 모두 끝남: 환경 준비 → CLI 테스트 → `debate.py` → `app.py`(Streamlit).
- Codex 로그인 완료 (`codex login status` → Logged in using ChatGPT). `python debate.py --check` → claude ✅ 3.3s, codex ✅ 7.3s.
- 5단계 전체 실행 검증 (`runs\20260910_140753.md`, Claude=sonnet, Codex 기본 gpt-5.6-sol): Initial 23.6s → Review 23.4s → Rebuttal 27.8s → Recheck 29.0s → FINAL 15.9s, 총 약 120s. 단계별 입력 길이 283→1190→2003→3299→4283자 (전체 맥락 누적). 타임아웃 기본 900s면 충분.
- `app.py` 검증: `streamlit.testing.v1.AppTest`로 렌더링 예외 0건, 이전 기록 불러오기 정상. headless 서버 health 200.
- streamlit 1.63.0을 `.venv`에 설치함 (Python 3.14와 호환 확인).
- Streamlit 첫 실행 시 `~\.streamlit\credentials.toml`이 없으면 콘솔에서 "Email:" 입력을 기다리느라 서버가 안 뜸(브라우저는 ERR_CONNECTION_REFUSED). `[general] email = ""` 파일을 만들어 해결함(2026-09-10).
- PowerShell 실행 정책이 기본(Restricted)이라 `Activate.ps1`이 막힘 → 사용자에게 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` 안내함. 활성화 없이 쓰려면 `.venv\Scripts\python.exe`를 직접 지정하거나 `start_ui.cmd`.

**실행 방법**
```powershell
cd C:\Users\LG\ai-debate-room
.\start_ui.cmd                      # 브라우저에 http://localhost:8501 열림
# 또는
.\.venv\Scripts\python.exe -m streamlit run app.py
# 콘솔 버전
.\.venv\Scripts\python.exe debate.py "질문"
# 테스트 (실제 CLI 호출 없음, 약 3초)
.\.venv\Scripts\python.exe -m pytest
```

**app.py가 지금 하는 것**: 맨 아래 채팅 입력창, 사용자/Claude(🟠)/GPT(🟢) 말풍선, 중간 4단계 접기/펼치기, FINAL 주황 테두리 강조, 상단 status에 "[n/5] 누구 응답 생성 중" 표시, 오류 시 단계·CLI명 표시, 사이드바(Claude 모델·effort, Codex 모델(카탈로그 4종 + 직접 입력)·모델별 effort 목록, 타임아웃·단계 수, 새 대화, runs\ 기록 불러오기, CLI 점검 버튼), 각 말풍선에 실제 적용된 모델/effort 표시, 같은 대화의 이어지는 질문에는 이전 라운드의 질문+FINAL을 참고 맥락으로 전달(`run_debate(prior=...)`).

**v2 구조 (2026-09-10 오후, 스트리밍·개입·모드·라운드·대화 저장)**
- 엔진: `_popen_stream()`이 모든 CLI 호출의 공통 실행기(stdin 프롬프트를 별도 스레드로 공급, stdout 라인 스트리밍, 0.25초 폴링으로 `on_tick`, `cancel` Event로 즉시 kill, 타임아웃). `call_claude(on_delta=...)`는 `-p --verbose --output-format stream-json --include-partial-messages`로 `content_block_delta/text_delta`를 누적해 넘기고 마지막 `type=result` 이벤트에서 결과·meta를 읽는다(stream-json은 --verbose 필수). **Codex `exec --json`은 `item.completed`(완성본)만 내보내고 델타가 없어 글자 단위 스트리밍 불가** → GPT 단계는 경과 시간만 표시. 기능 플래그(`codex features list`)에도 스트리밍 옵션 없음.
- 단계 계획: `plan_stages(rounds, mode)` → Initial → (Review→Rebuttal)×N → Recheck → FINAL. GPT의 Review/Recheck는 마지막 줄에 `[판정: 수정 필요]` 또는 `[판정: 추가 수정 불필요]`를 쓰도록 지시하고, `early_stop`이면 Review에서 '불필요' 판정 시 남은 Review/Rebuttal/Recheck를 건너뛰고 FINAL로 간다(`gpt_says_ok`).
- 모드 프리셋 `MODES`: general / code_review / plc(GX Works2·Q 시리즈 래더 검증 체크리스트) / invest. 시스템 규칙(`rules`) + 단계별 힌트(`hints`)를 덧붙인다.
- 사용자 중간 개입: 기록에 `{"who":"user","label":"개입"}` 항목으로 들어가 `[사용자 · 개입]`으로 전달되며 COMMON_RULES에 최우선 반영 규칙이 있다.
- 대화 저장: `chats\<YYYYmmdd_HHMM>_<주제슬러그>.json/.md` (대화 1개 = 여러 라운드). 제목은 질문 첫 줄에서 `make_title()`, 파일명은 `slugify()`(한글 유지, Windows 금지 문자 제거). 구 `runs\` 기록은 목록에 "(구 기록)"으로 표시되며 1라운드 대화로 열린다.
- UI(app.py): 단계 실행은 `start_stage_worker()`가 만든 **daemon 스레드**에서 돌고 `ss.live` dict로 부분 텍스트·경과 시간을 공유, `@st.fragment(run_every=0.5)`인 `live_view()`가 그 부분만 갱신한다 → 사이드바 조작으로 페이지가 재실행돼도 단계가 끊기지 않음. 단계가 끝나면 fragment가 `st.rerun()`으로 본문을 깨워 `run_active_stage()`가 결과를 기록에 붙이고 다음 단계/일시정지/완료를 결정한다. ⏹ 중단은 `cancel` Event → 프로세스 kill. "단계마다 멈춤"이면 `paused_controls()`(한마디 입력, ▶ 계속, ⏭ 바로 FINAL로, ⏹ 중단). 브라우저 새로고침은 session_state를 지우므로 진행 중 라운드는 사라진다(스레드는 끝까지 돌지만 결과는 버려짐) — 알려진 제한.
- 설정은 `ui_settings.json`에 저장(모델·effort·모드·라운드·자동 저장·소리 등). 자동 저장을 끄면 파일을 만들지 않고 새로고침 시 대화가 사라짐; 켜져 있으면 새 세션에서 최근 대화를 자동 복원. `.md 내려받기`, 삭제(2단계 확인), 완료 시 `st.toast` + `winsound` 알림.
- Streamlit AppTest로 종단 검증: 채팅 → Initial 스트리밍 → 일시정지 → 개입 → 계속 → 조기 종료 → FINAL → chats 저장 (scratchpad `test_app_v2.py`).

**순서 변경·진행 제어 (2026-09-10 15:50)**: `plan_stages(rounds, mode, first, final_who, custom)`. 기본 순서는 `default_plan(rounds, first, final_who)` = A 최초 → (B 검토 → A 반박)×N → B 재검사 → (final_who 또는 A) 최종. `custom=[(who, kind), ...]`로 임의 순서(`validate_plan`: 첫 단계 initial, 마지막 final, 각 1회). 지시문의 `{other}`는 최초 답변이면 다음 단계 작성자, 그 외엔 직전 단계 작성자 이름으로 채워져 순서를 바꿔도 맞는다. 조기 종료 판정은 `reviewer_says_ok()`(검토자가 누구든). UI: 사이드바 "순서" 라디오(기본/직접 편집) — 기본은 먼저 답하는 AI·최종 정리 AI·라운드 수, 직접 편집은 `st.data_editor` 표(AI, 역할)로 행 추가/삭제, `ss.plan_base`+`plan_nonce` 키로 편집 상태 관리(편집본을 다시 data로 넣으면 이중 적용되므로 base는 고정). 진행 제어: 실행 중 "⏸ 다음 단계 전에 멈춤"(`active.pause_next`)과 "⏹ 중단"; 일시정지 중 "▶ 계속 / 🔁 직전 단계 다시 생성 / ⏭ 바로 FINAL로 / ⏹ 중단"; 중단·오류로 끝난 마지막 라운드에는 "▶ 이어서 진행"(`resume_round`: 라운드를 conv에서 빼고 `plan_steps`로 계획을 복원해 active로 되돌림). 기본값 `pause_each=False`(사용자 요청: 자동 진행). 라운드 dict에 `plan_steps`, `first`, `final_who` 저장. 콘솔: `--first`, `--final-who`, `--plan claude:initial,gpt:review,...`.

**토론 단계 수 3/5 선택 (2026-09-16, 사용자 지정, 기본 3단계)**: `default_plan(rounds, first, final_who, stage_count)`, `plan_stages(..., stage_count)`, `run_debate(..., stage_count)`, 상수 `STAGE_COUNTS=[3, 5]`·`DEFAULT_STAGE_COUNT=3`. 3단계 = A 최초 → B 검토 → 최종, 5단계 = 기존 순서(최초 → 검토 → 반박 → 재검사 → 최종). 3단계에는 반박 단계가 없으므로 `plan_stages`가 FINAL 지시문의 `{fb_note}`에 '검토 지적을 항목별로 판정해(타당하면 반영, 틀리면 반박) 최종 답변에 녹여라'를 채운다 (계획에 rebuttal이 앞에 있으면 빈 문자열). 검토 단계의 `[판정: ...]` 줄은 그대로 요구하지만 3단계에서는 review 바로 뒤가 final이라 조기 종료로 건너뛸 단계가 없다. UI: 사이드바 '순서 → 기본'에 '대화 단계 수' 라디오(3/5), '검토 ↔ 반박 라운드 수'는 5단계일 때만 표시(3단계면 rounds=1 고정). 설정 키 `stage_count`, 라운드 dict에도 `stage_count` 저장(구 기록은 `round_stage_count()`가 plan_steps 길이 또는 rounds*2+3으로 역산, resume 폴백은 5). 말풍선 설정 요약은 '라운드 N' 대신 'N단계'. 콘솔 `--stages 3|5`(`--rounds`는 5단계일 때만 의미). **주의: `run["stages"]`는 이미 실행된 단계 항목 리스트라서 단계 수 키 이름은 반드시 `stage_count`를 쓴다.**

**저장소 위생 (2026-09-16, 하네스 엔지니어링 업그레이드 1단계)**: git 저장소로 전환. `.gitignore`는 .venv, __pycache__, chats\, runs\, ui_settings.json(개인 설정), sandbox\*(.gitkeep만 추적), *.bak 제외. `.gitattributes`는 text=auto, *.cmd만 CRLF. `requirements.txt`(streamlit==1.63.0, pypdf==6.18.0)·`requirements-dev.txt`(+pytest). `tests\`: `conftest.py`의 `isolated` 픽스처가 chats\·runs\·IMAGE_DIR를 임시 폴더로 돌리고 `find_claude/find_codex/claude_auth_status/codex_auth_status/list_codex_models/winsound.MessageBeep`를 가짜로 바꾸며 `ui_settings.json`을 테스트 전후로 보존한다(사이드바 조작이 설정을 저장하므로 위젯을 만지는 테스트는 반드시 `isolated` 사용). `fake_stages(review_ok)`는 `D.execute_stage`를 `FakeStages`로 바꿔 끼운다(`build_prompt`까지는 실제 경로를 태움). AppTest는 app.py를 같은 프로세스에서 실행하므로 `import debate as D`가 패치된 모듈을 그대로 본다(프로브로 확인). 가짜 단계는 즉시 끝나서 질문 제출 한 번의 run 안에서 라운드가 통째로 끝날 수 있다 — 테스트는 '시작됐는지'가 아니라 결과로 판단한다. 빈 custom 계획 `[]`은 '직접 편집 없음'으로 취급돼 기본 순서가 쓰인다. **규칙: 테스트에서 실제 claude/codex를 절대 호출하지 않는다** (사용량·로그인 보호). 실행 `.venv\Scripts\python.exe -m pytest` (33개, 약 3초). 코드를 고치면 테스트를 돌린 뒤 커밋한다.

**기본 모델 정책 (2026-09-10 15:30, 사용자 지정)**: Claude = `fable` 별칭 + effort `xhigh` (별칭이 최신 Fable을 자동 추적, 현재 claude-fable-5-1로 확인). Codex = `auto` + `xhigh`. `auto`는 `resolve_codex()`가 실행 시점에 카탈로그(`codex debug models`, 1시간 캐시 `cached_codex_models`) 최상위(priority 최소, visibility=list) 모델로 해석 — 현재 gpt-5.6-sol("Reliable agentic workhorse"), 새 모델이 맨 위로 오면 자동 전환. effort가 그 모델 미지원이면 지원 범위 안에서 가장 가까운 아래 단계로 낮추고(`EFFORT_ORDER`) meta.resolve_note에 기록(예: gpt-5.5+ultra→xhigh, luna+ultra→max). UI 기본값(`DEFAULT_SETTINGS`)과 `ui_settings.json`도 이 값. Config 기본값도 동일하므로 콘솔 `debate.py "질문"`도 같은 정책.

**이미지 첨부 (2026-09-10 15:10)**: `load_image_attachment()`가 PIL로 열어 긴 변 1568px로 축소·재인코딩(PNG/알파 유지, 아니면 JPEG 85) 후 `chats\_img\<시각>\` (자동 저장 꺼짐이면 %TEMP%)에 저장. Claude는 `--input-format stream-json`으로 `{"type":"user","message":{"content":[text, {"type":"image","source":{"type":"base64",...}}]}}` 한 줄을 stdin에 넣는다(도구 불필요, 검증됨 3.5s). Codex는 `-i 경로`(help의 -i/--image) — 재로그인 후 검증 완료(2026-09-10 15:40, 도형·색 정확히 인식, 6s). 매 단계 stateless 호출이므로 이미지도 매 단계 다시 전달. UI는 `st.image`로 표시.

**텍스트 없는 PDF → 쪽 이미지 (2026-09-10 16:00)**: 사용자가 "Microsoft Print to PDF"로 뽑은 지원서 PDF(8쪽 전부 JPEG, 글꼴 0개)를 올렸는데 추출 텍스트가 공백뿐이었음. `load_attachments(name, data, image_dir)`가 PDF 텍스트가 20자 미만이면 `_pdf_pages_to_images()`로 쪽을 PNG로 만들어(poppler `pdftoppm -r 130`, 없으면 pypdf `page.images`) 최대 12쪽을 이미지 첨부로 바꾼다(이름 "파일명 (n/N쪽)", 첫 장 warning에 안내). `_find_tool()`이 PATH 외에 winget Poppler 폴더도 찾는다(서버 프로세스 PATH에 poppler가 없을 수 있음). 검증: 8쪽 3.1s, Claude/Codex 모두 1쪽 섹션 정확히 판독. UI는 이미지를 한 줄 4장 열로 표시. app.py와 콘솔 모두 `load_attachments` 사용.

**계정 관리 (2026-09-10 15:10)**: 사이드바 "🔐 계정" 확장 패널. 상태 캐시는 `get_auth()`가 `AUTH_TTL`(120초)마다 자동 재조회하고 로그인 절차가 끝나면 종료 코드와 무관하게 실제 상태를 다시 확인한다(15:40 — 사용자가 터미널에서 로그인했는데 화면이 옛 캐시를 보여줘 "왜 자꾸 로그아웃이라 하냐"는 불만이 있었음). 상태는 `claude auth status --json`(loggedIn/email/subscriptionType)과 `codex login status`(exit 0 = Logged in). 로그아웃은 `claude auth logout` / `codex logout`(2단계 확인; **Claude 로그아웃은 VS Code Claude Code와 인증 파일 `~\.claude\.credentials.json`을 공유하므로 그쪽도 함께 풀림**). 로그인은 `LoginFlow` 클래스가 `claude auth login` / `codex login --device-auth`를 백그라운드로 띄우고 출력(ANSI 제거, 글자 단위 읽기)에서 URL·코드를 뽑아 UI에 표시, 2초마다 페이지 rerun으로 완료 감지. claude는 브라우저를 스스로 열고 'Paste code here if prompted >'를 기다리며 브라우저에 이미 로그인돼 있으면 코드 없이 'Login successful.'. **⚠ `codex login --device-auth`는 시작하는 순간 기존 로그인을 지운다** — 출력 형식 확인용으로 8초 띄웠다가 죽였더니 사용자 Codex가 로그아웃됨(2026-09-10 15:00 사고). 절대 테스트 목적으로 실행하지 말 것. `claude auth login`도 인증 파일을 바꾸므로 테스트 시 백업·복원했음.

**모델/effort 적용 검증 (2026-09-10)**: 사이드바 선택 → `CFG`(Config) → `call_claude`/`call_codex` 인자로 실제 전달됨을 AppTest 종단 테스트로 확인(opus/low + terra/high 선택 → 응답 JSON modelUsage=claude-opus-5, codex 헤더 model=gpt-5.6-terra effort=high). Claude effort는 thinking 토큰으로 확인(low=0, high≈50; low에서는 암산 문제를 틀림). Codex는 모델별 지원 범위 밖 effort(gpt-5.5+ultra)나 엉터리 값이면 API 400 오류 → `call_codex`가 stderr JSON의 `message`를 뽑아 "API 오류: Unsupported value..."로 표시. 사이드바 "🔍 현재 설정으로 CLI 점검" 버튼과 "현재 설정 →" 캡션으로 사용자가 직접 확인 가능.

**첨부 파일 (2026-09-10 추가)**: `st.chat_input(accept_file="multiple", file_type=UPLOAD_TYPES)`로 채팅창 📎 첨부. `debate.load_attachment(name, bytes)`가 텍스트(utf-8/cp949 자동 판별)·PDF(poppler `pdftotext` → `pypdf` 폴백, pypdf는 venv에 설치됨)를 텍스트로 바꾸고, 파일당 60,000자·전체 150,000자로 잘라 사용자 발언의 `[첨부 파일]` 블록으로 모든 단계에 전달. 이미지·바이너리는 아직 미지원(경고 표시). 콘솔은 `--file 경로` 반복 지정. 첨부 텍스트는 runs\*.json에도 저장됨.

**서버 재시작 규칙**: `debate.py`를 고친 뒤에는 실행 중인 Streamlit 서버를 **재시작**해야 한다. 서버는 새 `app.py`는 다시 읽지만 이미 import된 `debate` 모듈은 옛것을 쓰는 경우가 있어 `module 'debate' has no attribute ...` 오류가 난다(2026-09-10 발생). 재시작: 8501 포트 프로세스 종료 후 `launch_ui.cmd`. `.streamlit\config.toml`에 `toolbarMode="minimal"`(Deploy 버튼 숨김)과 `gatherUsageStats=false`를 둠.

**실행 파일·바로가기**: `launch_ui.cmd`는 8501 포트가 LISTENING이면 브라우저만 열고, 아니면 최소화 창으로 서버를 켠다(서버가 브라우저를 자동으로 엶). 바탕화면 `AI Debate Room.lnk`가 이 파일을 가리킴(WScript.Shell로 생성). `start_ui.cmd`는 항상 새 서버를 켜는 단순 버전. **.cmd 파일은 ASCII만 쓸 것**: cmd.exe가 배치 파일을 CP949로 읽어 UTF-8 한글 주석이 깨지면서 `'가' is not recognized` 같은 오류가 났음(2026-09-10).

**다음 확장 후보 (우선순위 순, 사용자와 상의)**: 사용자 중간 개입(특정 단계 뒤에 코멘트 삽입), 토론 라운드 수 조절(Review↔Rebuttal 반복), 모드 프리셋(일반/코드 리뷰/PLC 검증/투자 분석 = STAGES 지시문 세트 교체), 긴 컨텍스트 요약, 브라우저 새로고침 후에도 대화 유지(runs\ 자동 복원), 스트리밍 출력(`--output-format stream-json` / `codex exec --json`).

## 폴더 구조
```
ai-debate-room\
  CLAUDE.md            이 파일
  debate.py            토론 엔진 (CLI 호출, 단계 계획·실행, 첨부, 저장, 로그인)
  app.py               Streamlit 채팅 UI
  launch_ui.cmd        바탕화면 바로가기용 (서버 있으면 브라우저만, 없으면 최소화 창으로 서버)
  start_ui.cmd         항상 새 서버를 켜는 단순 버전
  requirements.txt     실행 의존성 (고정 버전) / requirements-dev.txt = +pytest
  tests\               pytest — conftest(픽스처), test_plan(엔진 단위), test_app(AppTest 종단). 실제 CLI 호출 없음
  .streamlit\          config.toml (Deploy 버튼 숨김, 사용 통계 끔)
  .venv\               Python 3.14 가상환경 (git 제외)
  docs\                CLI help 출력, codex-models.json(모델 카탈로그 캐시), full-run-test.log
  chats\               대화 저장 json/md (개인 데이터, git 제외)
  runs\                구 버전 라운드 기록 (읽기 전용, git 제외)
  sandbox\             CLI 호출용 빈 작업 폴더 (.gitkeep만 추적, 코드 두지 않음)
```
