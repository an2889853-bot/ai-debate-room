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

엔진은 `engine/` 패키지, UI는 `app.py`. `debate.py`는 **얇은 facade + 콘솔 진입점**이다 — `engine.console`을 star-import 해 모든 공개 이름을 다시 내보내므로 `import debate as D`(app.py, tests)와 `python debate.py`가 그대로 동작한다.
매 단계는 **독립된 stateless CLI 호출**이고 모델은 기억이 없다 — 컨텍스트는 오직 프롬프트에 넣은 transcript뿐이다.

| 모듈 (의존 순서) | 책임 |
|---|---|
| `engine/config.py` | 경로(ROOT/SANDBOX/CHATS/RUNS), `Config`, 모델·effort 상수, `CLIError`, `find_claude/find_codex`, `clean_env`, `DISPLAY/OTHER` |
| `engine/attachments.py` | 텍스트/PDF/이미지 변환, 텍스트 없는 PDF → 쪽 이미지, `render_attachments` |
| `engine/cli.py` | `_popen_stream`, `call_claude`, `call_codex`, Codex 카탈로그·`resolve_codex`, 로그인 상태·로그아웃·`LoginFlow`, `TOKENS_USED_RE` |
| `engine/contract.py` | `COMMON_RULES`, 판정 계약, 지적 번호별 반영 계약, 독립 평가 판정, `adjust_plan_after`, `reviewer_says_ok` |
| `engine/plan.py` | `STAGE_TEMPLATES`, `KIND_*`, `MODES`, `default_plan/validate_plan/plan_stages/plan_preview`, `system_rules` |
| `engine/evidence.py` | 코드 블록 추출·검사·실행, `render_evidence`, `evidence_summary` |
| `engine/usage.py` | 토큰·비용 해석, 라운드 합계, `stats` |
| `engine/prompt.py` | `render_prior`, `render_transcript`(요약·증거 블록), `build_prompt`, `render_compaction` |
| `engine/compact.py` | 압축 필요 판정, `summarize_entries`, `compact_history`(라운드 캐시) |
| `engine/runner.py` | `execute_stage`, `interjection_entry`, `run_debate` |
| `engine/store.py` | 대화 저장/목록/불러오기/삭제, 제목·파일명, 마크다운, `final_of` |
| `engine/console.py` | `console_event`, `check_clis`, `main`(argparse) |

각 모듈은 앞선 모듈들을 `from .x import *`로 가져온다(순환 없음). 그래서 같은 함수 이름이 여러 모듈에 바인딩돼 있고, 테스트의 `conftest.patch_all()`은 facade와 `engine.*` 모두에서 그 이름을 바꾼다 — 호출부가 어느 모듈의 바인딩을 쓰든 가짜가 보이도록. 새 함수를 추가할 때는 의존 순서를 지켜 뒤쪽 모듈에서 앞쪽만 참조한다(앞쪽이 뒤쪽을 쓰면 `render_compaction`을 prompt로 옮긴 것처럼 옮긴다).

## 2. 엔진 — engine/ 패키지 (facade `debate.py`)

### 2.1 CLI 호출

공통 실행기 `_popen_stream()`: 프롬프트는 argv가 아니라 **stdin**(별도 스레드로 공급 — Windows 명령줄 길이 제한·quoting 회피), 모든 subprocess `encoding="utf-8"`, cwd=`sandbox\`, env=`clean_env()`(`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` 제거 — Claude Code 세션 안에서 불러도 중첩 차단에 안 걸림), `CREATE_NO_WINDOW`, stdout 라인 스트리밍, 0.25초 폴링으로 `on_tick(경과초)`, `cancel` Event가 서면 즉시 kill, `timeout`(기본 900초) 초과 시 kill. 실패는 `CLIError(cli, stage, cancelled)`로 어느 CLI·어느 단계인지 담는다.

**Claude** `call_claude(cfg, system_prompt, prompt, stage, on_delta, on_tick, cancel, images)`
```
claude -p --output-format stream-json|json --tools "" --no-session-persistence --strict-mcp-config
       --system-prompt <규칙> [--model X] [--effort Y] [--verbose --include-partial-messages] [--input-format stream-json]
```
- 도구는 `claude_tool_args(cfg)`가 정한다: 기본(둘 다 꺼짐) `--tools ""`(전부 비활성). `web_search`면 `--tools WebSearch,WebFetch --allowedTools "WebSearch WebFetch"`, `tools`면 `--tools Read,Glob,Grep,Edit,Write,Bash --allowedTools "Bash(python *) Bash(pytest *) Bash(pip *) Bash(git *) Bash(ls *) … Read Glob Grep Edit Write" --permission-mode acceptEdits`(둘 다면 합집합). `-p` 모드는 승인을 물을 수 없어 허용 목록 밖 명령은 자동 거부되고, 그 거부가 곧 경계다(`permission_denials`가 meta.denials로 남음). `--dangerously-skip-permissions`는 쓰지 않는다. `--no-session-persistence` 세션 파일 없음, `--strict-mcp-config` MCP 안 읽음. cwd는 `cwd_for(cfg)` = tools면 `cfg.workspace`, 아니면 `sandbox\`; env는 `clean_env(cfg.tools)`(tools면 `.venv\Scripts`를 PATH 앞에).
- 도구가 켜져 있으면 항상 stream-json이고 `ClaudeEventParser`가 `assistant` 메시지의 `tool_use`와 `user` 메시지의 `tool_result`를 짝지어 `meta.actions = [{tool, input(command/file_path/query/url), output(1,200자), error}]`로 모은다(같은 tool_use id는 한 번만).
- `on_delta`가 있으면 stream-json(`--verbose` 필수)으로 `content_block_delta/text_delta`를 누적해 넘기고, 마지막 `type=result` 이벤트에서 결과·meta를 읽는다. 없으면 json 한 덩어리. meta = `{session_id, duration_ms, usage(input/cache_creation/cache_read/output_tokens …), cost_usd(total_cost_usd — API 환산, 구독이면 실제 과금 아님), models}`.
- 이미지가 있으면 `--input-format stream-json`으로 `{"type":"user","message":{"content":[text, {"type":"image","source":{"type":"base64",...}}]}}` 한 줄을 stdin에 넣는다 (도구 불필요).
- `--bare`는 OAuth를 읽지 않아 구독 계정으로 못 쓴다 → 사용하지 않음.

**Codex** `call_codex(cfg, prompt, stage, on_tick, cancel, images)`
```
codex exec --skip-git-repo-check --ephemeral --color never -o <임시파일>
           --sandbox read-only|workspace-write -C <sandbox|workspace> [-c web_search=live] [--json]
           [-m X] [-c model_reasoning_effort=Y] [-i 이미지경로]... -
```
- 샌드박스·작업 폴더·웹·`--json`은 `codex_tool_args(cfg)`: tools면 `workspace-write`(아니면 `read-only`; `danger-full-access`는 쓰지 않음), `web_search`면 `-c web_search=live`(`--search`는 대화형 CLI 옵션이라 `exec`가 거부 — 실기 확인), 둘 중 하나라도 켜지면 `--json`으로 이벤트를 받아 `parse_codex_events()`가 `item.completed`의 command(명령·출력·exit)/search/file 항목을 `meta.actions`로, `turn.completed.usage`를 `meta.usage = {in, out, total}`로, 마지막 `agent_message`를 `-o` 파일이 비었을 때의 보완 텍스트로 쓴다. item 종류 이름은 버전마다 다를 수 있어 부분 일치(command/search/file·patch)로 본다 — **실기 검증 전**(tools/probe_tools.py).
- 시스템 프롬프트 옵션이 없어 규칙을 프롬프트 맨 앞에 붙인다. 마지막 메시지는 `-o` 파일에서 읽는다(stdout엔 헤더·로그가 섞임).
- 실제 적용된 model/effort는 시작 헤더(stderr)의 `model:` / `reasoning effort:` 줄에서 뽑아 meta에 담는다. 출력에 `tokens used: N` 줄이 있으면 `meta.usage = {"total": N}`(입력/출력 구분 없음), 없으면 `None` — 지금 버전은 안 찍는 것으로 보여 GPT 토큰은 대개 `?`.
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
- **독립 평가자 (evaluate 단계)**: `evaluate=True`면 `default_plan`이 맨 끝에 `(OTHER[최종 정리 AI], "evaluate")`를 붙인다(라벨 `Eval`, 한글 `평가`). 지시문 `STAGE_TEMPLATES["evaluate"]`: 답을 새로 쓰지 말고 FINAL만 채점 — 질문에 답했는가, 검토 지적이 반영/반박됐는가, 근거 없는 단정·모순, `[프로그램 검사]`와 어긋나는 주장 — 문제는 `[지적 N]`으로, 마지막 줄 `[평가: PASS]` / `[평가: NEEDS_WORK]`(`EVAL_RULE`, `eval_verdict()`가 마지막 것을 읽음). `validate_plan`: 평가는 최대 1회, 최종 정리 바로 뒤 맨 마지막에만. **계획 조정은 `adjust_plan_after(plan, done, entry, early_stop, eval_revise)` 한 곳**(UI·콘솔 공용): 검토가 '불필요'면 남은 단계를 건너뛰고 FINAL(과 그 뒤 Eval)로 → `{"type": "skip"}`; 평가가 NEEDS_WORK이고 `eval_revise`이며 아직 재작성 전이면 `FINAL 2`(원래 final 단계 복사 + "평가자의 지적을 반영해 다시 쓰라") + `Eval 2`를 붙인다 → `{"type": "revise"}`. 최대 1회. `evaluate`가 `REVIEW_KINDS`에 포함되므로 FINAL 2는 평가자의 `[지적 N]`을 반영 계약으로 검사받는다. `final_of()`는 마지막 final(FINAL 2가 있으면 그것). 라운드 합계 `eval_summary(stages)` → `{verdict, evals, revised}`. 콘솔 `--no-evaluate`, `--no-eval-revise`; `run_debate(evaluate=False, eval_revise=True)` — 함수 기본은 꺼짐, 콘솔·UI 기본은 켜짐.
- **코드 블록 검사 (외부 증거)**: 모든 단계의 답변에서 ```` ``` ```` 펜스 블록을 뽑아(`extract_code_blocks`) python은 `ast.parse` 문법 검사, json/toml은 파싱 검사를 **항상** 한다(`check_code_blocks`). `Config.run_code`(UI 체크박스 "🔬 코드 블록 실제 실행", 콘솔 `--run-code`, 기본 꺼짐)가 켜져 있으면 문법이 맞는 python 블록을 `sandbox\_run\<시각>\block.py`로 써서 `sys.executable -I -X utf8`로 실행(stdin 차단, `CODE_RUN_TIMEOUT=30`초, 실행 후 폴더 삭제)해 exit 코드와 출력 꼬리(`MAX_EVIDENCE_OUTPUT=1500`자)를 잡는다. 결과는 `entry["evidence"] = [{index(전체 펜스 순번), lang, lines, check: syntax|parse|run, ok, detail}]`, `render_transcript`가 그 항목 바로 뒤에 `[프로그램 검사 · Claude · Initial의 코드 블록 — 사람이 아니라 프로그램이 실제로 검사한 결과]` 블록을 넣어 다음 단계가 본다. `COMMON_RULES`에 "이 블록은 프로그램의 검사 결과이며 실패는 반드시 다루라, 통과가 논리의 정당성은 아니다" 규칙. 실행은 모델이 쓴 코드가 이 PC에서 그대로 도는 것이므로 사용자가 믿을 수 있는 주제에서만 켜도록 도움말에 경고.
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
- **컨텍스트 압축**: 전체 기록이 `Config.compact_chars`(기본 60,000자, 0=끄기; UI "긴 토론 요약 기준 (천 자)", 콘솔 `--compact-chars`)를 넘으면 마지막 `COMPACT_KEEP_LAST=2`개 항목만 원문으로 두고 그 앞은 `[요약 · 이전 단계 n개 (Claude·Initial ~ GPT·Review) — 프로그램이 길이를 줄이려고 Claude가 요약. 원문은 아래 최근 단계만]` 블록으로 대체한다(`compact_history` → `summarize_entries`: `call_claude`를 effort `low`로, `SUMMARY_RULES`는 머리말·[지적/반영/반박/판정/평가] 줄·프로그램 검사 결과를 번호째 보존하라고 지시, 3,000자 이내). 요약은 라운드 단위 캐시 dict(`compaction`, UI는 `active["compaction"]`, 콘솔은 `run_debate` 지역)에 요약 대상 내용의 해시를 키로 저장해 단계마다 다시 만들지 않으며, "직전 단계 다시 생성"으로 내용이 바뀌면 다시 요약한다. 요약 호출이 실패하면 `fallback_summary`(항목당 앞 800자)로 대체해 토론이 멈추지 않는다. 반영 계약(`open_issues`)과 조기 종료는 원문 기록으로 판단하므로 요약의 영향을 받지 않고, 계약 블록엔 지적 원문이 그대로 들어간다. 단계 항목에 `compacted = {count, method}`, 라운드 dict에 `compaction` 저장. `compaction=None`이면(기본) 압축 안 함 — `execute_stage`를 직접 부르는 옛 호출·테스트 호환.
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
- 라운드 dict: `question, attachments, mode, rounds, stage_count, stages[], early_stopped, started, finished, prior_rounds, first, final_who, plan(라벨 목록), plan_steps([[who, kind], ...] — 재개용), config(Config asdict), contract(`contract_summary` 합계), evaluation(`eval_summary`), usage(`usage_summary`), compaction(요약 캐시 — 모델이 실제로 본 요약문), workspace(도구를 켠 라운드의 작업 폴더), status(done|stopped|error), error`. 대화 dict에도 `workspace`(다음 라운드 재사용).
  **주의**: `stages`는 실행된 단계 항목 목록이고 단계 수는 `stage_count`다.
- 단계 항목(entry): `{who, label, kind, content, elapsed, meta, prompt_chars, contract?, evidence?, compacted?, actions?, denials?, workspace?}`. `contract`는 반박/최종이 직전 지적을 검사받았을 때만 있으며 JSON 저장 후엔 `resolved`의 키가 문자열이 된다. `evidence`는 답변에 검사 가능한 코드 블록이 있을 때만. 사용자 개입은 `interjection_entry(text)` = `{who: "user", label: "개입", kind: "interjection", ...}`.
- `save_conversation(conv)`, `list_conversations()`(최신순; 구 `runs\` 기록은 `legacy=True`로 1라운드 대화처럼 포함), `load_conversation(path)`.

### 2.6 콘솔 `debate.py`

`python debate.py "질문"` 또는 `-q`. 옵션: `--file`(반복), `--mode`, `--stages 3|5`, `--rounds`(5단계일 때만 의미), `--first claude|gpt`, `--final-who`, `--plan claude:initial,gpt:review,...`, `--no-early-stop`, `--max-stage N`(테스트용), `--claude-model/--claude-effort`, `--codex-model/--codex-effort`, `--timeout`, `--run-code`, `--compact-chars N`, `--no-web`, `--no-tools`(도구는 기본 켜짐), `--web-scope initial_eval|initial|all`, `--check`(두 CLI 응답 확인), `--list-codex-models`, `--stats`(저장된 대화 전체 통계), `--no-save`.
`run_debate(question, cfg, max_stage, on_event, prior, attachments, rounds, mode, early_stop, first, final_who, custom, stage_count)`가 계획을 순차 실행하며 `on_event`로 `start / done / error / skip / revise` 이벤트를 보낸다. `console_event`는 done 항목에 `contract`가 있으면 🧾 한 줄(`contract_line`)을, `evidence`가 있으면 🔬 요약(`evidence_summary`)과 검사 블록을 더 찍고, run dict에 `contract` 합계가 들어간다. `.md` 내보내기도 항목 아래에 `> 🧾 ...` / `> 🔬 ...`를 남긴다.

### 2.7 계정

`claude_auth_status(exe)` = `claude auth status --json` → `{loggedIn, email, subscriptionType, authMethod, detail}`. `codex_auth_status(exe)` = `codex login status`(exit 0 = Logged in). 로그아웃 `claude auth logout` / `codex logout`. `LoginFlow`는 `claude auth login` / `codex login --device-auth`를 백그라운드로 띄우고 출력(ANSI 제거, 글자 단위)에서 URL·코드를 뽑는다.
**위험**: `codex login --device-auth`는 시작하는 순간 기존 로그인을 지운다. Claude 로그아웃은 `~\.claude\.credentials.json`을 공유하는 VS Code Claude Code까지 풀린다.

### 2.8 도구 허용과 행동 기록 (하네스 방식: 끄지 않고 범위를 정해 기록한다)

- `Config.web_search`(🌐 웹 검색 허용) / `Config.tools`(🛠 파일·명령 허용) / `Config.workspace`. **엔진 `Config` 기본은 꺼짐**(라이브러리·테스트 안전), **UI `DEFAULT_SETTINGS`와 콘솔 기본은 켜짐**(사용자 지정 2026-09-17) — 사이드바에서 끄거나 콘솔 `--no-web`, `--no-tools`.
- **규칙 문구**가 바뀐다: `system_rules(mode, tools, web)` = `COMMON_RULES_HEAD` + `tool_rules(tools, web)` + `COMMON_RULES_TAIL` + 모드 규칙. 꺼져 있으면 "어떤 도구도 사용하지 말고"; tools면 "작업 폴더 안에서 파일 읽기/쓰기와 허용된 명령 실행 가능, 허용 목록 밖은 거부, 실행 결과는 프로그램이 기록에 남기니 꾸미지 말고 그대로 인용, 주장 전에 실행으로 확인"; web이면 "검색으로 얻은 사실엔 출처 URL·확인 시각". web이면 투자 모드의 "최신 시세·뉴스는 알 수 없으므로"도 "웹 검색으로 확인하라"로 바뀐다. `COMMON_RULES`는 꺼진 기본 규칙(하위 호환).
- **작업 폴더**: tools를 켠 라운드는 `workspace\<시각>_<질문 앞 20자>\`에서 돈다(`new_workspace()` — mkdir + `git init`). UI는 같은 대화의 다음 라운드가 `conv["workspace"]`를 재사용하고, 콘솔 `run_debate`는 라운드마다 새로 만든다. 단계가 끝날 때마다 `workspace_commit(ws, "Claude · Initial")`이 `git status --porcelain`으로 바뀐 파일을 모아 커밋해 `entry["workspace"] = {changed, commit}`로 남긴다 — 되돌리기·검토는 그 폴더의 git log. `workspace\`는 git 제외(개인 데이터).
- **행동 기록**: `execute_stage`가 `meta.actions/denials`를 `entry["actions"]`, `entry["denials"]`로 꺼내고, `render_transcript`가 그 항목 뒤에 `[프로그램 기록 · Claude · Initial의 도구 사용 — 사람이 아니라 프로그램이 기록한 실제 명령과 결과]` 블록(`render_actions`: `- Bash: python hello.py → hi 42`, `- WebSearch: …`, `- ⚠ 거부됨 (허용 목록 밖): …`, `- 작업 폴더 변경: a.py (git 커밋 abc1234)`)을 넣는다. 그래서 상대 AI와 평가자는 "실제로 무슨 명령을 돌렸고 뭐가 나왔는지"를 보고 반박·채점하며(`COMMON_RULES_TAIL`: 기록과 다른 주장은 기록을 믿으라), 웹 검색 결과도 URL과 함께 기록에 남는다. UI 캡션 `actions_summary`("도구 3회 — Bash 2 · WebSearch 1 (실패 1) · 거부 1") + 상세 코드 블록, 콘솔 🛠, .md `> 🛠`.
- **단계별 범위** (`execute_stage`가 `stage_cfg = replace(cfg, web_search=web_on, readonly=…)`로 정함): 웹은 `Config.web_scope`에 따라 — `initial_eval`(기본: 최초 답변 + 평가), `initial`, `all`(`web_allowed(scope, kind)`). 검색이 꺼진 단계엔 규칙 `web_reuse`("앞 단계 [프로그램 기록]의 검색 결과를 재사용하고 추가 확인은 '확인 필요'")가 붙는다 — 최초 답변의 검색 결과가 기록으로 모든 단계에 남기 때문에 검토·반박이 다시 검색할 이유가 적고, 실측(2026-09-17, 전체 단계 검색)에서 4단계 11분·토큰 100만+가 나와 기본을 줄였다. **평가자는 읽기 전용**(`readonly=True`): Claude는 Edit/Write 없이 Read/Glob/Grep/Bash(허용 목록)/Web, `acceptEdits` 없음; Codex는 `read-only` 샌드박스 — Anthropic 평가자 패턴(쓰기 없는 새 컨텍스트 채점자). 단, Bash(python)로 파일을 쓰는 것까지는 막지 못한다. **요약자(압축)는 도구·웹 없이** 호출된다.
- **경계와 위험**: 허용 목록(`TOOL_ALLOW_CMDS` = python·pytest·pip·git·ls·dir·cat·type·echo·mkdir) 안이라도 `python`은 임의 코드 실행이다. 폴더 격리·허용 목록·타임아웃·⏹ 중단은 실수 방지지 보안 경계가 아니고, 웹이 열리면 데이터가 밖으로 나갈 수 있다. 사용자 지정으로 두 토글 모두 기본 켜짐이며 도움말에 경고가 있다 — 추론·설계 검토처럼 근거가 이미 안에 있는 질문은 끄는 편이 낫다(6절, DECISIONS 2026-09-17 참고).
- **실시간 표시**: `call_claude`/`call_codex`/`execute_stage`의 `on_action(actions)` 콜백 — Claude는 stream-json의 assistant/user 이벤트마다, Codex는 `--json` 줄을 `CodexLiveParser`가 읽어 `item.started`(running) → `item.completed`로 갱신할 때마다 누적 목록을 넘긴다. UI 워커가 `live["actions"]`에 넣고 `live_view`가 "🛠 Bash: python … · WebSearch: …" 캡션으로 그린다(최근 4개 + 총 횟수). 최종 기록은 단계가 끝난 뒤 전체 stdout으로 다시 만든다(`parse_codex_events`, `ClaudeEventParser`).
- **작업 폴더 관리**: `list_workspaces()`(이름·크기·`linked` — 저장된 대화나 라운드가 가리키는가), `delete_workspace()`(`WORKSPACES` 밖이면 거부, 읽기 전용 .git 파일도 삭제), `delete_conversation()`은 대화가 가리키는 작업 폴더도 함께 지운다. 사이드바 "📁 작업 폴더" 확장에 개수·용량과 "🧹 연결 안 된 작업 폴더 삭제" 버튼(프로브·삭제된 대화·자동 저장 끈 라운드의 폴더).
- **점검·요약은 도구 없이**: 사이드바 "🔍 CLI 점검"과 콘솔 `--check`는 `tools=False, web_search=False`로 최소 호출. 압축 요약 입력에는 `[프로그램 기록]`(명령·검색 결과·URL)도 포함되고 `SUMMARY_RULES`가 핵심을 보존하라고 지시한다. Codex `--json` 모드에서 시작 헤더가 없으면 모델·effort 표시는 요청값으로 보완하고 `resolve_note`에 "요청값(헤더 없음)"을 남긴다.
- **기록 정리**: 명령은 `_tidy_command`가 셸 래퍼(`"…powershell.exe" -Command '…'`, `bash -lc`)와 앞의 `cd <workspace> &&`를 벗겨 실제 명령만 남기고, 파일 경로는 `_rel`로 workspace 상대 경로로. Codex `exit_code`는 문자열로 와도 숫자로, `status: failed`도 실패로 본다. 실기 출력(2026-09-17)으로 확인한 형식.

### 2.9 관측 — 토큰·비용·시간

- `usage_of(entry)` → `{in, out, total, cost_usd}`: Claude는 입력=input+cache_creation+cache_read, 출력=output, 비용=meta.cost_usd; GPT는 총 토큰만(알 때); 모르면 None. `usage_line(entry)` 캡션 한 줄("12.0k→6.8k 토큰 · API 환산 $0.123" / "총 4.3k 토큰" / 빈 문자열).
- `usage_summary(stages)` 라운드 합계 `{stages, elapsed, tokens, known(토큰을 아는 단계 수), cost_usd, by_who}`, `usage_summary_line()` — "⏱ 합계 127s (3단계) · 토큰 23.1k (Claude 18.7k · GPT ?) · API 환산 $0.12 · 토큰 미확인 1단계". 라운드 dict와 run dict에 `usage`로 저장.
- `stats(conversations)` 저장된 대화 전체 집계(라운드·완료·조기 종료, AI별 단계 수·평균 시간, 토큰·비용·총 시간, 반영 계약 지적/반영/반박/재요청/미처리 라운드, 독립 평가 PASS/NEEDS_WORK/재작성) + `stats_lines()` 사람이 읽는 줄. 구 기록처럼 `contract`/`evaluation` 키가 없으면 단계에서 다시 계산. 콘솔 `debate.py --stats`, 사이드바 "📊 통계" 확장(`load_stats` — (경로, updated) 튜플로 `st.cache_data` 10분).

### 2.10 실행 파일 탐색

`find_claude()`: PATH `claude` → `~\.local\bin\claude.exe`. `find_codex()`: PATH의 `.exe` → winget 설치 경로(`CODEX_WINGET_EXE`) → `codex.cmd` 래퍼. 없으면 `FileNotFoundError`에 설치 명령을 담아 올린다.

## 3. UI — app.py

### 3.1 설정과 세션

- `DEFAULT_SETTINGS`: claude_model/effort, codex_model/effort, timeout, mode, **stage_count**, rounds, early_stop, pause_each, autosave, beep, first, final_who("same"=먼저 답한 AI), use_custom, custom_plan. `load_settings()`는 `ui_settings.json`에서 아는 키만 덮어쓰고, 사이드바 값이 달라질 때마다 `save_settings()`가 저장한다 (→ 사이드바를 만지면 파일이 바뀐다).
- `st.session_state`: `settings, conv(현재 대화), active(진행 중 라운드), live(진행 중 단계 버퍼), pending_delete, auth, login_flow, pending_logout, exes`. autosave가 켜져 있으면 새 세션에서 최근 대화를 자동 복원.
- `busy = ss.active is not None` — 진행 중엔 사이드바 위젯과 입력창이 잠긴다.

### 3.2 사이드바

저장된 대화 선택(chats + 구 runs), "📊 통계 (저장된 대화 전체)" 확장, "📁 작업 폴더" 확장(정리 버튼), 새 대화, `.md` 내려받기, 삭제(2단계 확인 — 작업 폴더도 함께) · 모드 · **"🛠 도구 — 웹 검색 · 파일·명령 · 코드 실행" 확장(접힘)** 안에 코드 실행·웹 검색·검색 범위·파일·명령 토글 · **순서**: 라디오 "기본"(대화 단계 수 3/5, 먼저 답하는 AI, 최종 정리 AI, 5단계일 때만 검토↔반박 라운드 수) / "직접 편집"(`st.data_editor` 표 — AI, 역할; `ss.plan_base` + `plan_nonce` 키로 편집 상태 관리, 편집본을 다시 data로 넣으면 이중 적용되므로 base는 고정; "기본 순서로 되돌리기") · 조기 종료 체크 · 단계마다 멈춤 체크 · "🧑‍⚖️ FINAL 뒤 독립 평가"(기본 켬; 직접 편집이면 표에 '평가' 행을 넣어야 함) · "NEEDS_WORK면 FINAL 1회 재작성 후 재평가"(기본 켬) · "🔬 코드 블록 실제 실행"(모드 아래, 기본 끔) · "🌐 웹 검색 허용 (실시간 확인)"(기본 켬) · "검색 범위"(최초 답변 + 평가 / 최초 답변만 / 전체 단계, 웹이 꺼지면 비활성) · "🛠 파일·명령 허용 (대화별 workspace)"(기본 켬) · 순서 미리보기 캡션(`plan_preview`) · **"🧠 모델 · 실행 설정" 확장(접힘)** 안에 Claude 모델·effort · Codex 모델(카탈로그 + 직접 입력)·모델별 effort · 타임아웃 · 긴 토론 요약 기준(천 자) · 자동 저장 · 소리 · "현재 설정 →" 캡션 · "🔍 현재 설정으로 CLI 점검"(두 CLI를 실제로 한 번 호출) · "🔐 계정" 패널(상태 `get_auth()`가 `AUTH_TTL=120초`마다 재조회, 로그인/로그아웃).

### 3.3 라운드 실행

1. 채팅 입력(`st.chat_input(accept_file="multiple")`) → 첨부 변환 → tools면 작업 폴더 결정(`conv["workspace"]` 재사용 또는 `new_workspace(question)`, `cfg.workspace`에 기록) → `ss.active = {question, attachments, cfg, mode, rounds, stage_count, early_stop, plan, stages: [], prior, prior_rounds, status: "running", early_stopped, pause_next, first, final_who, workspace, started}` → rerun.
2. `run_active_stage()`: 다음 단계를 `start_stage_worker()`가 **daemon 스레드**로 실행(`D.execute_stage`), 공유 버퍼 `ss.live = {text, elapsed, done, entry, error, cancelled, cancel(Event), stage, t0}`.
3. `live_view()` — `@st.fragment(run_every=0.5)`: 부분 텍스트(Claude)·경과 시간·진행 중 도구 사용(`live["actions"]`)을 그 부분만 갱신. 사이드바 조작으로 페이지가 재실행돼도 스레드는 끊기지 않는다. `done`이 되면 `st.rerun()`으로 본문을 깨운다.
4. 본문이 `entry`를 `active["stages"]`에 붙이고 `D.adjust_plan_after()`로 계획을 조정(조기 종료 건너뛰기 → `early_stopped`, 평가 NEEDS_WORK → FINAL 2·Eval 2 추가) → 끝났으면 `finalize_active("done")`, `pause_each`거나 "⏸ 다음 단계 전에 멈춤"(`pause_next`)이면 `status="paused"`.
5. 일시정지 `paused_controls()`: 한마디 입력(개입 항목으로 삽입) + ▶ 계속 / 🔁 직전 단계 다시 생성(마지막 AI 항목 삭제 후 같은 단계 재실행) / ⏭ 바로 FINAL로 / ⏹ 중단. 실행 중엔 ⏸ 멈춤 예약과 ⏹ 중단(`cancel` Event → 프로세스 kill).
6. `finalize_active(status)`: 라운드 dict를 만들어 `ss.conv["rounds"]`에 붙이고 autosave면 저장, 완료 시 `st.toast` + `winsound` 알림. 중단·오류로 끝난 마지막 라운드에는 "▶ 이어서 진행" — `resume_round(idx)`가 라운드를 conv에서 빼고 `plan_steps`로 계획을 복원해 active로 되돌린다.
- 가짜/빠른 단계라면 한 번의 스크립트 실행 안에서 여러 단계가 연달아 끝날 수 있다(스레드가 폴링 전에 끝나면 곧바로 다음 단계로).
- **알려진 제한**: 브라우저 새로고침은 session_state를 지우므로 진행 중 라운드는 사라진다(스레드는 끝까지 돌지만 결과는 버려짐).

### 3.4 렌더링

`render_round(run)` → `render_user()`(질문, 첨부 목록, 이미지 4열, 설정 요약 `config_line` = "모드 · N단계 · Claude 모델/effort · GPT 모델/effort"; N은 `round_stage_count(run)` = `count_debate_stages(plan_steps)` — 평가 단계와 재작성 FINAL 2는 세지 않음, 구 기록은 rounds×2+3) + `render_entry()`(Claude 🟠 / GPT 🟢 / 사용자 🧑 말풍선, 중간 단계 접기, FINAL·FINAL 2 주황 제목, 평가 항목은 제목에 "✅ PASS / ⚠ NEEDS_WORK" 배지, 실제 적용 모델/effort + 토큰·비용 캡션 `used_models` + `D.usage_line`, 계약 결과가 있으면 🧾 캡션 `D.contract_line`, 코드 검사 결과가 있으면 `render_evidence_ui` — 🔬 요약 캡션, 실패가 있으면 ❌와 상세 코드 블록; 도구 사용이 있으면 `render_actions_ui` — 🛠/⚠ 요약 캡션과 명령·결과·거부·파일 변경 상세). 라운드에 `workspace`가 있으면 "📁 작업 폴더: …" 캡션. 라운드 끝에 `render_contract_summary(stages)` — 지적이 하나라도 검사됐으면 전부 처리 시 "✅ 검토 지적 N건 전부 처리됨 (반영 a · 반박 b)" 캡션, 미처리가 있으면 `st.warning`("⚠ 미처리 지적: 단계 → 번호 ... FINAL을 그대로 믿기 전에 확인"). `render_eval_summary()` — "🧑‍⚖️ 독립 평가: PASS (FINAL 재작성 후 재평가)" 캡션 또는 NEEDS_WORK 경고. 그 아래 `usage_summary_line` 캡션(합계 시간·토큰·API 환산 비용). 조기 종료·중단 캡션. 일시정지의 "⏭ 바로 FINAL로"는 다음 단계가 final/evaluate면 비활성.

## 4. 테스트 — tests\

- 원칙: **실제 claude/codex를 호출하지 않는다.** `conftest.isolated` 픽스처가 `CHATS/IMAGE_DIR/RUNS`를 임시 폴더로, `find_claude/find_codex/claude_auth_status/codex_auth_status/list_codex_models/winsound.MessageBeep`를 가짜로 바꾸고 `ui_settings.json`을 전후로 보존한다. 대역 교체는 항상 `conftest.patch_all(monkeypatch, 이름, 값)` — facade와 모든 `engine.*` 모듈의 같은 이름을 함께 바꾼다(`monkeypatch.setattr(D, ...)`만 쓰면 engine 내부 호출엔 안 먹는다). `fake_stages(review_ok)`는 `D.execute_stage`를 `FakeStages`(단계별 고정 답변, `build_prompt`까지는 실제 경로, 호출 내역 기록)로 바꿔 끼운다.
- `test_plan.py`: 계획·검증·판정·프롬프트·콘솔 엔진(run_debate)·파일명. `test_contract.py`: 지적/판정 파싱, 프롬프트 블록, `execute_stage`의 재요청(`call_claude`/`call_codex`를 대역으로 바꿔 실제 재시도 경로를 태움), 라운드 합계, run_debate 계약. `test_eval.py`: 평가 계획·검증 규칙·판정 파싱·`adjust_plan_after`(건너뛰기·재작성 1회)·`final_of`·run_debate 종단(NEEDS_WORK → FINAL 2 계약 검사 → PASS). `test_cycle3.py`: Codex 실시간 파서(running→완료), call_claude on_action 경로(`_popen_stream` 대역), execute_stage 콜백 전달, 요약 입력의 도구 기록, workspace 목록·삭제·대화 삭제 연동, 콘솔 `--check` 도구 끔. `test_tools.py`: 도구 인자(허용 목록·permission-mode·샌드박스·web_search=live/--json), `cwd_for`/`clean_env(tools)`, Claude 이벤트 파서(중복 id·is_error), Codex JSONL 파서(usage 포함), 규칙 문구 변형, 기록 블록·transcript, execute_stage의 actions 전달, workspace 생성·git 커밋(git 있을 때), run_debate의 workspace. `tools/probe_tools.py`는 pytest가 아닌 **실기 프로브**(구독 사용량 사용, 사람이 직접 실행). `test_compact.py`: 압축 필요 판정, 내용 해시 캐시, 요약 effort low, 실패 시 기계적 요약, 프롬프트 반영, run_debate 종단(5단계에서 요약 2회). `test_usage.py`: 토큰·비용 해석(Claude/GPT/모름), 캡션 문자열, Codex `tokens used` 정규식, 라운드 합계, 전체 통계·빈 통계. `test_evidence.py`: 펜스 추출, 문법/파싱 검사, 실제 파이썬 실행(성공·exit 코드·타임아웃·stdin 차단), 다음 단계 프롬프트 전달, run_code 옵션. `test_app.py`: `streamlit.testing.v1.AppTest`로 실제 UI 종단(3단계 완주·저장, 5단계 완주, 조기 종료, 일시정지·개입·계속, 중단·이어서 진행). AppTest는 app.py를 같은 프로세스에서 실행하므로 `import debate as D`가 패치된 모듈을 본다.
- 실행 `.venv\Scripts\python.exe -m pytest` (약 3초).

## 5. 실행 환경

- `launch_ui.cmd`(바탕화면 바로가기 대상): 8501 포트가 LISTENING이면 브라우저만, 아니면 최소화 창으로 서버. `start_ui.cmd`: 항상 새 서버. **둘 다 ASCII만** (cmd.exe가 CP949로 읽음).
- `.streamlit\config.toml`: `toolbarMode="minimal"`(Deploy 버튼 숨김), `gatherUsageStats=false`. `~\.streamlit\credentials.toml`에 `[general] email=""`가 없으면 첫 실행이 "Email:" 입력에서 멈춘다.
- `debate.py`나 `engine/`을 고치면 서버 **재시작** 필요 — 이미 import된 모듈은 옛것이라 `module 'debate' has no attribute ...`가 난다.
- 1인용 구조: 서버 PC의 구독 로그인으로 CLI를 실행한다. LAN에 열면 접속자 모두가 내 사용량을 쓰고 계정 패널까지 보인다 ([SETUP.md](SETUP.md) 참고). 🛠 도구를 켠 채 LAN에 열면 접속자가 이 PC에서 명령을 돌리게 되므로 절대 같이 쓰지 않는다.

## 6. 알려진 제한 / 다음 업그레이드

- Codex 글자 단위 스트리밍 불가, 새로고침 시 진행 중 라운드 유실. 첨부는 요약하지 않고 150,000자에서 자른다(기록 압축과 별개).
- 검증의 '증거'는 지적 번호별 반영/반박 기록, 코드 블록 검사(문법·파싱·옵션 실행), 상대 AI의 독립 평가(PASS/NEEDS_WORK + 1회 재작성)까지다 — 코드가 요구사항을 만족하는지는 여전히 모델 판단이고(실행은 스크립트 단위, 테스트 러너 없음), 평가자도 모델이라 같은 편향을 공유할 수 있다.
