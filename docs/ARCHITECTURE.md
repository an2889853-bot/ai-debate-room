# 아키텍처 — 코드가 지금 어떻게 동작하는가

이 문서는 **현재 동작**만 적는다 (날짜·경위 없음). 왜 이렇게 했는지, 언제 바뀌었는지는 [DECISIONS.md](DECISIONS.md).
동작이 바뀌면 이 문서를 같이 고친다.

## 1. 한눈에

```
사용자 질문 (+첨부)
  └─ plan_stages()  ─ 단계 계획 [{who, label, kind, instruction}, ...]   (3단계 기본 / 5단계 / 직접 편집)
       └─ 단계마다 execute_stage()
            ├─ build_prompt(): 이전 라운드 요약 + 지금까지의 전체 대화 기록 + 이번 단계 지시
            ├─ who == claude → call_claude()   (claude -p, stateless, 도구 없음, 스트리밍)
            └─ who == gpt    → call_codex()    (codex exec, read-only sandbox, 완성본만)
       └─ 검토 단계의 마지막 줄 [판정: ...] → early_stop이면 '추가 수정 불필요' 시 FINAL로 건너뜀
  └─ 라운드 dict → 대화(conv) → chats\<시각>_<주제>.json / .md
```

두 파일이 전부다. `debate.py`가 엔진(위 흐름 + 첨부 + 저장 + 계정 + 콘솔), `app.py`가 Streamlit UI.
매 단계는 **독립된 stateless CLI 호출**이고 모델은 기억이 없다 — 컨텍스트는 오직 프롬프트에 넣은 transcript뿐이다.

## 2. 엔진 — debate.py

### 2.1 CLI 호출

공통 실행기 `_popen_stream()`: 프롬프트는 argv가 아니라 **stdin**(별도 스레드로 공급 — Windows 명령줄 길이 제한·quoting 회피), 모든 subprocess `encoding="utf-8"`, cwd=`sandbox\`, env=`clean_env()`(`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` 제거 — Claude Code 세션 안에서 불러도 중첩 차단에 안 걸림), `CREATE_NO_WINDOW`, stdout 라인 스트리밍, 0.25초 폴링으로 `on_tick(경과초)`, `cancel` Event가 서면 즉시 kill, `timeout`(기본 900초) 초과 시 kill. 실패는 `CLIError(cli, stage, cancelled)`로 어느 CLI·어느 단계인지 담는다.

**Claude** `call_claude(cfg, system_prompt, prompt, stage, on_delta, on_tick, cancel, images)`
```
claude -p --output-format stream-json|json --tools "" --no-session-persistence --strict-mcp-config
       --system-prompt <규칙> [--model X] [--effort Y] [--verbose --include-partial-messages] [--input-format stream-json]
```
- `--tools ""` 도구 전부 비활성화(텍스트 토론만), `--no-session-persistence` 세션 파일 없음, `--strict-mcp-config` MCP 안 읽음.
- `on_delta`가 있으면 stream-json(`--verbose` 필수)으로 `content_block_delta/text_delta`를 누적해 넘기고, 마지막 `type=result` 이벤트에서 결과·meta(`modelUsage` 등)를 읽는다. 없으면 json 한 덩어리.
- 이미지가 있으면 `--input-format stream-json`으로 `{"type":"user","message":{"content":[text, {"type":"image","source":{"type":"base64",...}}]}}` 한 줄을 stdin에 넣는다 (도구 불필요).
- `--bare`는 OAuth를 읽지 않아 구독 계정으로 못 쓴다 → 사용하지 않음.

**Codex** `call_codex(cfg, prompt, stage, on_tick, cancel, images)`
```
codex exec --skip-git-repo-check --sandbox read-only --ephemeral --color never -C sandbox -o <임시파일>
           [-m X] [-c model_reasoning_effort=Y] [-i 이미지경로]... -
```
- 시스템 프롬프트 옵션이 없어 규칙을 프롬프트 맨 앞에 붙인다. 마지막 메시지는 `-o` 파일에서 읽는다(stdout엔 헤더·로그가 섞임).
- 실제 적용된 model/effort는 시작 헤더(stderr)의 `model:` / `reasoning effort:` 줄에서 뽑아 meta에 담는다.
- `exec --json`은 `item.completed`(완성본)만 내보내고 델타가 없어 **글자 단위 스트리밍 불가** → UI는 경과 시간만 표시.
- 미로그인: exit 1 + stderr `401 Unauthorized`, `-o` 파일 미생성. API 400(미지원 effort 등): stderr JSON의 `message`를 뽑아 "API 오류: ..."로 표시.
- effort는 `-c model_reasoning_effort=high`처럼 따옴표 없이 넘긴다(TOML 파싱 실패 시 문자열 리터럴로 처리됨).

**모델 정책** (`Config` 기본값 = UI `DEFAULT_SETTINGS` = `ui_settings.json`)
- Claude `fable` + `xhigh`. 별칭(`CLAUDE_MODELS = fable/opus/sonnet`)은 CLI가 최신 모델로 해석. effort `CLAUDE_EFFORTS = low~max`.
- Codex `auto` + `xhigh`. `resolve_codex(cfg)`가 실행 시점에 카탈로그의 최상위(priority 최소, visibility=list) 모델로 해석. 카탈로그는 `list_codex_models()` = `codex debug models` → 실패 시 `docs\codex-models.json` → 내장 `CODEX_MODELS_FALLBACK`; `cached_codex_models()`가 1시간 캐시. effort가 그 모델 미지원이면 `EFFORT_ORDER`에서 가장 가까운 아래 단계로 낮추고 `meta.resolve_note`에 기록.

### 2.2 단계 계획

- `STAGE_COUNTS = [3, 5]`, `DEFAULT_STAGE_COUNT = 3`.
- `default_plan(rounds, first, final_who, stage_count)` — A=먼저 답하는 AI, B=상대:
  - 3단계: A 최초(initial) → B 검토(review) → (final_who 또는 A) 최종(final)
  - 5단계: A 최초 → (B 검토 → A 반박(rebuttal))×rounds → B 재검사(recheck) → 최종. rounds≥2면 7, 9단계.
- `validate_plan(steps)` — 직접 편집 순서 검사: 첫 단계 initial, 마지막 final, 각 1회, who∈{claude,gpt}, kind∈KIND_LABEL. 빈 리스트는 `plan_stages`에서 '직접 편집 없음'으로 취급.
- `plan_stages(rounds, mode, first, final_who, custom, stage_count)` → `[{who, label, kind, instruction}]`. label은 `KIND_LABEL`(Initial/Review/Rebuttal/Recheck/FINAL), 같은 kind가 반복되면 "Review 2"처럼 번호. 지시문은 `STAGE_TEMPLATES[kind]`에 자리표시자를 채운 뒤 `MODES[mode]["hints"][kind]`를 덧붙인다:
  - `{other}` — 최초 답변이면 다음 단계 작성자(검토자), 그 외엔 직전 단계 작성자. 순서를 바꿔도 이름이 맞는다.
  - `{target}` (review) — 첫 검토는 "최초 답변", 이후는 "최신 수정본". `{round_note}` — 2회차부터 "이미 해결된 지적은 반복하지 말라".
  - `{fb_note}` (final) — 계획에 앞선 rebuttal이 없으면(3단계) "검토 지적을 항목별로 판정해(타당하면 반영, 틀리면 근거로 반박) 최종 답변에 녹여라"; 있으면 빈 문자열.
- 판정 계약: review/recheck 지시문 끝에 `VERDICT_RULE` — 답변 **맨 마지막 줄**에 `[판정: 수정 필요]` 또는 `[판정: 추가 수정 불필요]`. `reviewer_says_ok(entry)`가 `VERDICT_OK_RE`(공백 허용)로 판정하며 사용자 발언은 항상 False. `early_stop`이면 review 뒤 '불필요' 판정 시 남은 단계를 건너뛰고 final로 간다(run_debate와 app.py 양쪽에 같은 로직; 3단계는 review 바로 뒤가 final이라 건너뛸 게 없다).
- **지적 번호별 반영 계약 (default-FAIL)**: review/recheck 지시문에 `ISSUE_FORMAT_RULE` — 지적은 한 줄에 하나씩 `[지적 N] ...`(번호는 1부터, 없으면 `[지적 없음]`). 그에 답하는 rebuttal/final(`RESPOND_KINDS`)은 번호마다 `[반영 N] 한 줄` 또는 `[반박 N] 근거`를 써야 한다. `execute_stage()`가 `open_issues(history)`(직전 AI 단계가 review/recheck면 그 지적들, 사용자 개입은 건너뜀)로 검사 대상을 정해 프롬프트 끝에 `contract_block` "=== 처리해야 할 지적 ==="을 붙이고, 응답을 `check_contract()`로 검사해 빠진 번호가 있으면 `retry_note`를 덧붙여 **같은 단계를 재요청**(`MAX_CONTRACT_RETRIES=1`). 결과는 `entry["contract"] = {issues, resolved: {N: 반영|반박}, missing, retries, source}`(검사할 지적이 없으면 키 없음). 라운드 합계 `contract_summary(stages)` → `{issues, accepted, rejected, retries, missing: [(단계명, [N...])], ok}`. 재요청 후에도 빠지면 라운드가 "미처리 지적" 경고를 단다 — 모델이 "반영했다"고 말하는 것이 아니라 번호별 기록이 있어야 처리로 본다. 5단계에서는 rebuttal이 review의 지적을, final이 recheck의 지적을 검사받는다. 조기 종료로 rebuttal을 건너뛰어도 review에 지적이 남아 있으면 final이 검사받는다. 파싱은 굵게·목록 기호가 붙어도 인식(`ISSUE_RE`, `RESOLVE_RE`), 같은 번호는 지적은 첫 줄·판정은 마지막이 유효.
- 모드 프리셋 `MODES`: general / code_review / plc(GX Works2·Q 시리즈 래더 검증 체크리스트) / invest. 각각 `name`, `description`, `rules`(시스템 규칙에 덧붙임), `hints`(단계별 지시 덧붙임).

### 2.3 프롬프트

- 시스템 규칙 `system_rules(mode)` = `COMMON_RULES`(참가자 역할, 도구 금지, 질문 언어 따르기, 근거 없는 단정 금지, [첨부 파일] 활용, [사용자 · 개입] 최우선 반영) + 모드 rules. Claude엔 `--system-prompt`, Codex엔 프롬프트 앞에 붙임.
- `build_prompt(question, history, stage, plan_len, prior, attachments)`:
  ```
  [render_prior]  === 이전 대화 (이미 끝난 토론의 질문과 최종 답변, 참고용) ===  [이전 질문 k] / [이전 최종 답변 k]
  === 지금까지의 전체 대화 기록 (N개 발언) ===
  [사용자]\n질문 + [첨부 파일] 블록
  [Claude · Initial]\n...   [사용자 · 개입]\n...   [GPT · Review]\n...
  === 이번 단계 (n/plan_len): GPT · Review ===
  당신은 GPT입니다. <instruction>  머리말 붙이지 말고 본문만.
  ```
- 같은 대화의 이어지는 질문에는 이전 라운드들의 질문+FINAL(`final_of(run)`)이 `prior`로 들어간다.
- 반박/최종 단계에 직전 검토의 지적이 있으면 끝에 `=== 처리해야 할 지적 (직전 [Claude · Review]) ===` 블록(지적 목록 + "N = 1, 2, 3 각각에 [반영 N]/[반박 N]")이 붙고, 재요청이면 `=== 재요청 ===` 블록이 더 붙는다.
- 이미지는 매 단계 다시 전달된다 (`image_attachments()`), stateless이므로.

### 2.4 첨부

`load_attachments(name, bytes, image_dir) -> [attachment]` (UI·콘솔 공용):
- 텍스트류(`TEXT_UPLOAD_TYPES`: txt md csv json pdf log xml yaml toml ini cfg 코드 등): utf-8/cp949 자동 판별.
- PDF: poppler `pdftotext` → 없으면 `pypdf`. 추출 텍스트가 20자 미만이면(스캔·인쇄 PDF) `_pdf_pages_to_images()`로 쪽을 PNG로 만들어(`pdftoppm -r 130`, 없으면 pypdf `page.images`) 최대 `MAX_PDF_PAGES_AS_IMAGES=12`쪽을 이미지 첨부로 바꾼다(이름 "파일명 (n/N쪽)").
- 이미지(`IMAGE_UPLOAD_TYPES`: png jpg jpeg gif webp bmp): `load_image_attachment()`가 PIL로 열어 긴 변 `IMAGE_MAX_SIDE=1568px`로 축소·재인코딩(알파 있으면 PNG, 아니면 JPEG 85) 후 `image_dir`(기본 `chats\_img\<시각>\`, 자동 저장 꺼짐이면 %TEMP%)에 저장.
- 상한: 파일당 `MAX_FILE_CHARS=60,000`자, 전체 `MAX_TOTAL_CHARS=150,000`자. 텍스트는 `[첨부 파일]` 블록으로 모든 단계에 전달되고 저장 파일에도 남는다.
- `_find_tool(name)`: PATH → winget Poppler 폴더(서버 프로세스 PATH에 poppler가 없을 수 있음).

### 2.5 저장

- 대화 1개 = `chats\<YYYYmmdd_HHMM>_<주제슬러그>.json` + 같은 이름 `.md`. `new_conversation(question)` → `{id, title, created, updated, rounds: []}`. 제목 `make_title()`(질문 첫 줄, 마크다운 기호 제거, 28자), 파일명 `slugify()`(한글 유지, Windows 금지 문자 제거, 30자).
- 라운드 dict: `question, attachments, mode, rounds, stage_count, stages[], early_stopped, started, finished, prior_rounds, first, final_who, plan(라벨 목록), plan_steps([[who, kind], ...] — 재개용), config(Config asdict), contract(`contract_summary` 합계), status(done|stopped|error), error`.
  **주의**: `stages`는 실행된 단계 항목 목록이고 단계 수는 `stage_count`다.
- 단계 항목(entry): `{who, label, kind, content, elapsed, meta, prompt_chars, contract?}`. `contract`는 반박/최종이 직전 지적을 검사받았을 때만 있으며 JSON 저장 후엔 `resolved`의 키가 문자열이 된다. 사용자 개입은 `interjection_entry(text)` = `{who: "user", label: "개입", kind: "interjection", ...}`.
- `save_conversation(conv)`, `list_conversations()`(최신순; 구 `runs\` 기록은 `legacy=True`로 1라운드 대화처럼 포함), `load_conversation(path)`.

### 2.6 콘솔 `debate.py`

`python debate.py "질문"` 또는 `-q`. 옵션: `--file`(반복), `--mode`, `--stages 3|5`, `--rounds`(5단계일 때만 의미), `--first claude|gpt`, `--final-who`, `--plan claude:initial,gpt:review,...`, `--no-early-stop`, `--max-stage N`(테스트용), `--claude-model/--claude-effort`, `--codex-model/--codex-effort`, `--timeout`, `--check`(두 CLI 응답 확인), `--list-codex-models`, `--no-save`.
`run_debate(question, cfg, max_stage, on_event, prior, attachments, rounds, mode, early_stop, first, final_who, custom, stage_count)`가 계획을 순차 실행하며 `on_event`로 `start / done / error / skip` 이벤트를 보낸다. `console_event`는 done 항목에 `contract`가 있으면 🧾 한 줄(`contract_line`)을 더 찍고, run dict에 `contract` 합계가 들어간다. `.md` 내보내기도 항목 아래에 `> 🧾 ...`를 남긴다.

### 2.7 계정

`claude_auth_status(exe)` = `claude auth status --json` → `{loggedIn, email, subscriptionType, authMethod, detail}`. `codex_auth_status(exe)` = `codex login status`(exit 0 = Logged in). 로그아웃 `claude auth logout` / `codex logout`. `LoginFlow`는 `claude auth login` / `codex login --device-auth`를 백그라운드로 띄우고 출력(ANSI 제거, 글자 단위)에서 URL·코드를 뽑는다.
**위험**: `codex login --device-auth`는 시작하는 순간 기존 로그인을 지운다. Claude 로그아웃은 `~\.claude\.credentials.json`을 공유하는 VS Code Claude Code까지 풀린다.

### 2.8 실행 파일 탐색

`find_claude()`: PATH `claude` → `~\.local\bin\claude.exe`. `find_codex()`: PATH의 `.exe` → winget 설치 경로(`CODEX_WINGET_EXE`) → `codex.cmd` 래퍼. 없으면 `FileNotFoundError`에 설치 명령을 담아 올린다.

## 3. UI — app.py

### 3.1 설정과 세션

- `DEFAULT_SETTINGS`: claude_model/effort, codex_model/effort, timeout, mode, **stage_count**, rounds, early_stop, pause_each, autosave, beep, first, final_who("same"=먼저 답한 AI), use_custom, custom_plan. `load_settings()`는 `ui_settings.json`에서 아는 키만 덮어쓰고, 사이드바 값이 달라질 때마다 `save_settings()`가 저장한다 (→ 사이드바를 만지면 파일이 바뀐다).
- `st.session_state`: `settings, conv(현재 대화), active(진행 중 라운드), live(진행 중 단계 버퍼), pending_delete, auth, login_flow, pending_logout, exes`. autosave가 켜져 있으면 새 세션에서 최근 대화를 자동 복원.
- `busy = ss.active is not None` — 진행 중엔 사이드바 위젯과 입력창이 잠긴다.

### 3.2 사이드바

저장된 대화 선택(chats + 구 runs), 새 대화, `.md` 내려받기, 삭제(2단계 확인) · 모드 · **순서**: 라디오 "기본"(대화 단계 수 3/5, 먼저 답하는 AI, 최종 정리 AI, 5단계일 때만 검토↔반박 라운드 수) / "직접 편집"(`st.data_editor` 표 — AI, 역할; `ss.plan_base` + `plan_nonce` 키로 편집 상태 관리, 편집본을 다시 data로 넣으면 이중 적용되므로 base는 고정; "기본 순서로 되돌리기") · 조기 종료 체크 · 단계마다 멈춤 체크 · 순서 미리보기 캡션(`plan_preview`) · Claude 모델·effort · Codex 모델(카탈로그 + 직접 입력)·모델별 effort · 타임아웃 · 자동 저장 · 소리 · "현재 설정 →" 캡션 · "🔍 현재 설정으로 CLI 점검"(두 CLI를 실제로 한 번 호출) · "🔐 계정" 패널(상태 `get_auth()`가 `AUTH_TTL=120초`마다 재조회, 로그인/로그아웃).

### 3.3 라운드 실행

1. 채팅 입력(`st.chat_input(accept_file="multiple")`) → 첨부 변환 → `ss.active = {question, attachments, cfg, mode, rounds, stage_count, early_stop, plan, stages: [], prior, prior_rounds, status: "running", early_stopped, pause_next, first, final_who, started}` → rerun.
2. `run_active_stage()`: 다음 단계를 `start_stage_worker()`가 **daemon 스레드**로 실행(`D.execute_stage`), 공유 버퍼 `ss.live = {text, elapsed, done, entry, error, cancelled, cancel(Event), stage, t0}`.
3. `live_view()` — `@st.fragment(run_every=0.5)`: 부분 텍스트(Claude)·경과 시간을 그 부분만 갱신. 사이드바 조작으로 페이지가 재실행돼도 스레드는 끊기지 않는다. `done`이 되면 `st.rerun()`으로 본문을 깨운다.
4. 본문이 `entry`를 `active["stages"]`에 붙이고 조기 종료 판정 → 끝났으면 `finalize_active("done")`, `pause_each`거나 "⏸ 다음 단계 전에 멈춤"(`pause_next`)이면 `status="paused"`.
5. 일시정지 `paused_controls()`: 한마디 입력(개입 항목으로 삽입) + ▶ 계속 / 🔁 직전 단계 다시 생성(마지막 AI 항목 삭제 후 같은 단계 재실행) / ⏭ 바로 FINAL로 / ⏹ 중단. 실행 중엔 ⏸ 멈춤 예약과 ⏹ 중단(`cancel` Event → 프로세스 kill).
6. `finalize_active(status)`: 라운드 dict를 만들어 `ss.conv["rounds"]`에 붙이고 autosave면 저장, 완료 시 `st.toast` + `winsound` 알림. 중단·오류로 끝난 마지막 라운드에는 "▶ 이어서 진행" — `resume_round(idx)`가 라운드를 conv에서 빼고 `plan_steps`로 계획을 복원해 active로 되돌린다.
- 가짜/빠른 단계라면 한 번의 스크립트 실행 안에서 여러 단계가 연달아 끝날 수 있다(스레드가 폴링 전에 끝나면 곧바로 다음 단계로).
- **알려진 제한**: 브라우저 새로고침은 session_state를 지우므로 진행 중 라운드는 사라진다(스레드는 끝까지 돌지만 결과는 버려짐).

### 3.4 렌더링

`render_round(run)` → `render_user()`(질문, 첨부 목록, 이미지 4열, 설정 요약 `config_line` = "모드 · N단계 · Claude 모델/effort · GPT 모델/effort"; N은 `round_stage_count(run)` = plan_steps 길이 또는 구 기록은 rounds×2+3) + `render_entry()`(Claude 🟠 / GPT 🟢 / 사용자 🧑 말풍선, 중간 단계 접기, FINAL 주황 제목, 실제 적용 모델/effort 캡션 `used_models`, 계약 결과가 있으면 🧾 캡션 `D.contract_line`). 라운드 끝에 `render_contract_summary(stages)` — 지적이 하나라도 검사됐으면 전부 처리 시 "✅ 검토 지적 N건 전부 처리됨 (반영 a · 반박 b)" 캡션, 미처리가 있으면 `st.warning`("⚠ 미처리 지적: 단계 → 번호 ... FINAL을 그대로 믿기 전에 확인"). 조기 종료·중단 캡션.

## 4. 테스트 — tests\

- 원칙: **실제 claude/codex를 호출하지 않는다.** `conftest.isolated` 픽스처가 `D.CHATS/IMAGE_DIR/RUNS`를 임시 폴더로, `find_claude/find_codex/claude_auth_status/codex_auth_status/list_codex_models/winsound.MessageBeep`를 가짜로 바꾸고 `ui_settings.json`을 전후로 보존한다. `fake_stages(review_ok)`는 `D.execute_stage`를 `FakeStages`(단계별 고정 답변, `build_prompt`까지는 실제 경로, 호출 내역 기록)로 바꿔 끼운다.
- `test_plan.py`: 계획·검증·판정·프롬프트·콘솔 엔진(run_debate)·파일명. `test_contract.py`: 지적/판정 파싱, 프롬프트 블록, `execute_stage`의 재요청(`call_claude`/`call_codex`를 대역으로 바꿔 실제 재시도 경로를 태움), 라운드 합계, run_debate 계약. `test_app.py`: `streamlit.testing.v1.AppTest`로 실제 UI 종단(3단계 완주·저장, 5단계 완주, 조기 종료, 일시정지·개입·계속, 중단·이어서 진행). AppTest는 app.py를 같은 프로세스에서 실행하므로 `import debate as D`가 패치된 모듈을 본다.
- 실행 `.venv\Scripts\python.exe -m pytest` (약 3초).

## 5. 실행 환경

- `launch_ui.cmd`(바탕화면 바로가기 대상): 8501 포트가 LISTENING이면 브라우저만, 아니면 최소화 창으로 서버. `start_ui.cmd`: 항상 새 서버. **둘 다 ASCII만** (cmd.exe가 CP949로 읽음).
- `.streamlit\config.toml`: `toolbarMode="minimal"`(Deploy 버튼 숨김), `gatherUsageStats=false`. `~\.streamlit\credentials.toml`에 `[general] email=""`가 없으면 첫 실행이 "Email:" 입력에서 멈춘다.
- `debate.py`를 고치면 서버 **재시작** 필요 — 이미 import된 모듈은 옛것이라 `module 'debate' has no attribute ...`가 난다.
- 1인용 구조: 서버 PC의 구독 로그인으로 CLI를 실행한다. LAN에 열면 접속자 모두가 내 사용량을 쓰고 계정 패널까지 보인다 ([SETUP.md](SETUP.md) 참고).

## 6. 알려진 제한 / 다음 업그레이드

- Codex 글자 단위 스트리밍 불가, 새로고침 시 진행 중 라운드 유실, 긴 토론·첨부의 요약(압축) 없음.
- 검증의 '증거'는 아직 지적 번호별 반영/반박 기록뿐이다 — 반영했다고 적은 내용이 실제로 맞는지는 검사하지 않는다. 코드 실행·테스트 같은 외부 증거(3-2)와 도구 없는 독립 평가자(3-3)는 예정 ([DECISIONS.md](DECISIONS.md) 2026-09-16 항목).
