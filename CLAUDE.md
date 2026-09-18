# AI Debate Room — 작업 지도 (CLAUDE.md)

Claude Code가 이 폴더에서 시작할 때 자동으로 읽는 **짧은 지도**다. 여기엔 목표·제약·작업 규칙·실행법·현재 상태·어디를 볼지만 두고, 세부는 `docs\`에 있다. **새 세션은 "현재 상태" 절을 먼저 읽고 이어간다.** 이 파일은 100줄 안팎으로 유지한다.

## 무엇인가
Windows 11 로컬에서 Claude와 GPT가 한 채팅창에서 서로 검토·반박하며 하나의 답을 만드는 Streamlit 앱.
구독 로그인된 **Claude Code CLI**와 **OpenAI Codex CLI**를 Python `subprocess`로 호출한다 (API 키 과금 없음). 코드는 `engine/`(엔진 패키지 12모듈) + `debate.py`(facade·콘솔 진입점) + `app.py`(UI).
공개 저장소: https://github.com/an2889853-bot/ai-debate-room (2026-09-18부터, GitHub Desktop으로 push). 외부인용 소개는 `README.md`, 프롬프트만 쓰는 사람용은 `docs/PROMPTS.md`(`tools/export_prompts.py`로 재생성).

## 문서 지도
| 알고 싶은 것 | 어디 |
|---|---|
| 코드가 지금 어떻게 동작하나 (CLI 호출, 단계 계획, 프롬프트, 첨부, 저장, UI 스레드, 테스트) | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 왜 이렇게 했나, 언제 무엇이 바뀌었나, 사고 기록 | [docs/DECISIONS.md](docs/DECISIONS.md) (시간순, 맨 아래가 최신) |
| 새 PC 설치, 다른 컴퓨터에서 접속, 문제 해결 표 | [docs/SETUP.md](docs/SETUP.md) |
| CLI 옵션 원문 | `docs/claude-help.txt`, `codex-help.txt`, `codex-exec-help.txt`, `codex-login-help.txt` |
| Codex 모델 카탈로그 캐시 | `docs/codex-models.json` |

## 제약 (반드시)
- API 키 과금 방식 금지. 구독 계정 CLI만 쓴다.
- CLI 옵션은 가정하지 말고 `--help` 원문(`docs\`)을 확인한 뒤 결정한다.
- 단계별로 진행하고 각 단계 결과를 확인한 뒤 다음으로. 확인 명령은 Claude가 직접 실행해도 되지만, 설치처럼 시스템을 바꾸는 명령은 실행 전에 무엇을 하는지 한 줄로 알린다.
- 사용자: 전기공학 전공 자동화/로봇 인턴, Python 보통 수준. 설명은 한국어.

## 작업 규칙
- **테스트·확인 목적으로 실제 claude/codex를 호출하지 않는다** (구독 사용량·로그인 보호). 특히 `codex login --device-auth`는 실행만 해도 기존 로그인이 지워진다. `claude auth logout`은 VS Code Claude Code까지 함께 로그아웃된다.
- `engine/`이나 `debate.py`를 고치면 8501 서버를 **재시작**한다 (import된 모듈이 옛것으로 남음). 8501 프로세스 종료 → `launch_ui.cmd`.
- `engine/` 모듈은 의존 순서(config → attachments → cli → contract → plan → evidence → usage → prompt → compact → runner → store → console)대로 앞쪽만 참조한다. 테스트에서 대역은 반드시 `conftest.patch_all()`로 바꾼다(star-import 때문에 같은 이름이 여러 모듈에 있음).
- `.cmd` 파일은 ASCII만 (cmd.exe가 CP949로 읽음).
- 코드 수정 → `pytest`(112개, 약 9초) → 커밋 → `git push`(GitHub Desktop의 Push origin으로도 됨). 지시문을 고쳤으면 `tools\export_prompts.py`로 `docs/PROMPTS.md`도 재생성. 테스트 대역(`conftest.FakeCLI`, `FakeStages`)은 실제 함수 시그니처를 따라가야 한다 — 인자를 추가하면 대역도 같이. 결정·사고는 `docs/DECISIONS.md` 맨 아래에 날짜와 함께 추가하고, 동작이 바뀌면 `docs/ARCHITECTURE.md`를 같이 고친다. 이 파일엔 상태·규칙만 갱신한다.
- 라운드 dict의 `stages`는 실행된 단계 목록, 단계 수는 `stage_count` (이름 충돌 주의).
- 사이드바를 만지면 `ui_settings.json`이 저장된다. 이 파일을 직접 고칠 땐 서버를 끄고 한다.

## 실행
```powershell
cd C:\Users\<이름>\ai-debate-room
.\launch_ui.cmd                                   # 서버 있으면 브라우저만, 없으면 켬 → http://localhost:8501
.\.venv\Scripts\python.exe debate.py "질문" [--stages 5] [--first gpt] [--mode plc] [--file 경로]
.\.venv\Scripts\python.exe debate.py --check      # 두 CLI 응답 확인 (실제 호출 2회)
.\.venv\Scripts\python.exe debate.py --stats      # 저장된 대화 전체 통계
.\.venv\Scripts\python.exe tools\probe_tools.py   # 도구 허용 실기 프로브 (실제 호출 3회 — 사람이 직접)
.\.venv\Scripts\python.exe -m pytest              # 테스트 (실제 CLI 호출 없음)
```

## 폴더 구조
```
ai-debate-room\
  CLAUDE.md / AGENTS.md   이 지도 (AGENTS.md는 Codex CLI용 안내)
  engine\                 토론 엔진 패키지 — config·attachments·cli·contract·plan·evidence·usage·prompt·compact·runner·store·console
  debate.py               engine의 facade + 콘솔 진입점 (`import debate as D`, `python debate.py`)
  app.py                  Streamlit 채팅 UI
  launch_ui.cmd           바탕화면 바로가기용 런처 / start_ui.cmd  항상 새 서버
  requirements.txt        실행 의존성 (고정 버전) / requirements-dev.txt  +pytest
  tests\                  conftest(픽스처·patch_all·가짜 단계·가짜 CLI), test_plan·test_contract·test_evidence·test_eval·test_usage·test_compact·test_tools·test_cycle3, test_app(AppTest 종단)
  tools\                  probe_tools.py — 도구 허용 실기 프로브 (pytest 아님, 원본 출력은 probe_out\ — git 제외)
  workspace\              도구를 켠 라운드의 작업 폴더 (대화별, git init, 단계마다 커밋 — git 제외)
  docs\                   ARCHITECTURE·DECISIONS·SETUP, CLI help 원문, codex-models.json
  .streamlit\             config.toml (Deploy 버튼 숨김, 통계 끔)
  .venv\                  Python 3.14 가상환경 (git 제외)
  chats\                  대화 저장 json/md (개인 데이터, git 제외) / runs\  구 기록 (읽기 전용, git 제외)
  sandbox\                CLI 호출용 빈 작업 폴더 (.gitkeep만 추적, 코드 두지 않음)
```

## 핵심 설계 (요약)
- 매 단계 = **독립된 stateless CLI 호출**. 지금까지의 전체 대화 기록을 stdin 프롬프트로 넘긴다. 모델에 기억은 없다.
- 계획 `plan_stages()`: **3단계(기본)** 최초 → 검토 → 최종 / **5단계** 최초 → 검토 → 반박 → 재검사 → 최종. 먼저 답하는 AI·최종 정리 AI 선택, 표로 직접 편집도 가능. 3단계의 FINAL은 검토 지적을 직접 판정·반영하라는 지시를 받는다.
- 검토 단계는 마지막 줄 `[판정: 수정 필요 | 추가 수정 불필요]` 계약. 조기 종료가 켜져 있으면 '불필요' 시 FINAL로 건너뛴다.
- **지적 번호별 반영 계약 (default-FAIL)**: 검토/재검사는 `[지적 N] …` 줄로, 반박/최종은 번호마다 `[반영 N]`/`[반박 N]` 줄로. `execute_stage()`가 검사해 빠진 번호가 있으면 같은 단계를 1회 재요청, 그래도 빠지면 라운드에 "⚠ 미처리 지적" 경고. 결과는 `entry["contract"]`, 라운드 `contract` 합계.
- **코드 블록 검사 (외부 증거)**: 답변의 ```` ```python ```` 블록은 항상 문법 검사(json/toml은 파싱), `run_code`를 켜면 `sandbox\_run`에서 실제 실행(30초, stdin 차단). 결과 `entry["evidence"]`가 다음 단계 프롬프트에 `[프로그램 검사 · …]` 블록으로 들어간다. 실행은 기본 꺼짐 — 모델 코드가 이 PC에서 그대로 돈다.
- **도구 허용 (기본 꺼짐 — 공유판. 사이드바에서 켜면 `ui_settings.json`에 저장돼 유지)**: 🌐 `web_search`(Claude WebSearch/WebFetch, Codex `-c web_search=live` — `--search`는 exec가 거부), 🛠 `tools`(대화별 `workspace\` 안에서 파일 읽기/쓰기 + 허용 목록 명령 python·pytest·pip·git·ls…, Claude `--allowedTools`+`acceptEdits`, Codex `workspace-write`). 허용 목록 밖은 자동 거부. 콘솔 `--web/--tools`. 추론·설계 검토처럼 근거가 이미 안에 있는 질문은 끄는 편이 낫다. **검색 범위** 기본 "최초 답변 + 평가"(`web_scope`) — 뒤 단계는 기록의 검색 결과를 재사용. **평가자는 읽기 전용**(Edit/Write 없음, Codex read-only), **요약자·점검은 도구 없음**. 진행 중 도구 사용은 `on_action`으로 실시간 표시. 사이드바 "📁 작업 폴더"에서 고아 폴더 정리, 대화 삭제 시 작업 폴더도 삭제. Codex는 `--add-dir`(venv·uv 파이썬 폴더)로 샌드박스 안 파이썬 실행. 단계당 도구 호출 권고 상한 `tool_budget` 8 / 평가자 5(지시문). 실행한 명령·결과·검색·파일 변경은 `entry["actions"]` → 기록에 `[프로그램 기록 · …]` 블록으로 들어가고 단계마다 git 커밋. **실기 검증 전** — `tools\probe_tools.py`를 사람이 실행해 확인.
- **컨텍스트 압축**: 기록이 `compact_chars`(기본 60,000자)를 넘으면 마지막 2단계만 원문, 그 앞은 Claude(effort low)가 요약한 `[요약 · 이전 단계 n개]` 블록으로. 라운드 캐시(내용 해시 키), 실패 시 앞부분 잘라 붙임. 계약·조기 종료·평가 판정은 항상 원문으로.
- **관측**: 단계 캡션에 토큰·API 환산 비용(Claude만 정확, GPT는 CLI가 안 찍어 `?`), 라운드 끝에 ⏱ 합계, 저장 dict `usage`. 전체 통계는 `debate.py --stats` 또는 사이드바 📊 (조기 종료율·계약 재요청·평가 PASS율까지).
- **독립 평가자**: `evaluate`(기본 켬)면 FINAL 뒤에 최종 정리를 안 쓴 쪽 AI가 `[평가: PASS|NEEDS_WORK]` + `[지적 N]`으로 채점. NEEDS_WORK면 `FINAL 2` + `Eval 2`를 1회만 추가(`adjust_plan_after()` — 조기 종료도 여기서, UI·콘솔 공용). FINAL 2는 평가자 지적을 반영 계약으로 검사받는다. `final_of()`는 FINAL 2 우선.
- 모델 정책: Claude `fable`+`xhigh`, Codex `auto`(카탈로그 최상위, 현재 gpt-5.6-sol)+`xhigh`(미지원이면 자동 하향).
- UI: 단계는 daemon 스레드에서 돌고 fragment가 0.5초마다 그린다. 일시정지·한마디 개입·다시 생성·바로 FINAL·중단·이어서 진행. 도구는 전부 꺼져 있고 Codex는 read-only 샌드박스.
- 저장: `chats\<시각>_<주제>.json/.md`, 설정 `ui_settings.json`. 첨부: 텍스트·PDF·이미지, 글자 없는 PDF는 쪽 이미지로.

## 현재 상태 (2026-09-16)
- 기능 완성, 서버 정상(포트 8501). 기본 3단계, 사용자 설정은 GPT 먼저 답함.
- 환경: Claude Code CLI 2.1.267(`~\.local\bin`), Codex 0.146.1(winget), Python 3.14.7(uv), streamlit 1.63.0, pypdf, pytest. 두 CLI 모두 구독 로그인됨.
- **하네스 엔지니어링 업그레이드 (6단계)**: 1) 저장소 위생 ✅ (git, pytest, requirements) → 2) 문서 분리 ✅ (이 지도 + docs/) → 3) 증거 기반 검증 루프 ✅ (3-1 지적 번호별 반영 계약, 3-2 코드 블록 검사·실행, 3-3 독립 평가자) → 4) 토큰·비용 관측 ✅ → 5) 컨텍스트 압축 ✅ → 6) engine/ 모듈 분리 ✅ (2026-09-17) → 7) 도구 허용·행동 기록 ✅ — Claude·Codex 모두 실기 검증(검색·파일·명령·거부·이벤트 형식·`--add-dir`로 샌드박스 파이썬 해결). 실제 토론 3라운드에서 계약·평가·재작성 루프 정상(재요청 0). 미결 없음. 계속 할 일: 실제 토론이 쌓이면 `--stats`와 평가 단계 조회 횟수를 보고 상한·지시문 조정. 배경과 각 단계 내용은 DECISIONS.md 2026-09-16 항목.
- 3단계(반영 계약·코드 검사·독립 평가)는 실제 모델이 `[지적 N]`/`[반영 N]`/`[평가: …]` 형식을 얼마나 지키는지 아직 실전 검증 전 — 다음 실제 토론에서 재요청 횟수·미처리 경고·평가 배지를 확인하고 자주 어긋나면 지시문을 손볼 것.
- 알려진 제한: Codex 글자 단위 스트리밍 불가(토큰은 도구를 켰을 때만 `--json`으로 잡힘), 브라우저 새로고침 시 진행 중 라운드 유실, 첨부는 요약 없이 150,000자 절단, 평가자·요약자도 모델이라 같은 오해를 공유할 수 있음. 🛠 도구는 허용 목록 안이라도 임의 코드 실행 — 믿을 수 있는 작업에서만, LAN 공유와 절대 같이 켜지 않기.
