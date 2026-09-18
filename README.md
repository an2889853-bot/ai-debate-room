# AI Debate Room

Claude와 GPT가 한 채팅창에서 **서로 검토·반박하며 하나의 답을 만드는** 로컬 토론 앱입니다.
API 키 과금 없이, 이미 구독 중인 **Claude Code CLI**와 **OpenAI Codex CLI**를 그대로 불러 씁니다.

> *A local Streamlit app where Claude and GPT debate each other (initial → review → final, with an independent evaluator), driven through the Claude Code and Codex CLIs you already subscribe to. Korean UI.*

```mermaid
flowchart LR
    Q[질문 + 첨부] --> A[A · 최초 답변]
    A --> B[B · 검토<br/>지적 1, 2, 3…]
    B --> F[A · 최종 답변<br/>반영 1 · 반박 2 …]
    F --> E[B · 독립 평가<br/>PASS / NEEDS_WORK]
    E -- NEEDS_WORK --> F2[A · FINAL 2] --> E2[B · 재평가]
```

A/B는 Claude·GPT 중 고를 수 있고, 5단계(검토 → 반박 → 재검사)로 늘리거나 순서를 표로 직접 짤 수도 있습니다.

## 왜 그냥 챗봇과 다른가

- **프로그램이 검사하는 토론** — 검토자의 지적은 `[지적 N]`, 답변자는 번호마다 `[반영 N]`/`[반박 N]`을 써야 하고, 빠지면 프로그램이 같은 단계를 재요청합니다. 그래도 빠지면 라운드에 "미처리 지적" 경고가 붙습니다.
- **코드 블록은 실제로 검사** — 답변 속 `python` 블록은 문법 검사(옵션으로 실제 실행), 결과가 다음 단계 프롬프트에 `[프로그램 검사]` 블록으로 들어갑니다.
- **독립 평가자** — 최종 답변을 쓰지 않은 쪽 AI가 새 호출로 채점하고, NEEDS_WORK면 한 번 다시 씁니다.
- **도구를 켤 수 있음 (선택)** — 웹 검색(출처 URL 기록), 대화별 격리 폴더 안에서 파일·명령(허용 목록). 실행한 명령·결과·검색은 전부 대화 기록에 남아 상대 AI와 평가자가 그걸 보고 판단합니다.
- **긴 토론은 자동 요약**, 토큰·시간·비용은 단계마다 표시, 저장된 대화 전체 통계(`--stats`).

## 필요한 것

| 항목 | 비고 |
|---|---|
| Windows 10/11 | Mac/Linux는 `.cmd` 런처·`winsound`·winget 경로를 손봐야 합니다 |
| **Claude Code CLI** + Claude 구독 | `irm https://claude.ai/install.ps1 \| iex` → `claude auth login` |
| **OpenAI Codex CLI** + ChatGPT 구독 | `winget install --id OpenAI.Codex` → `codex login` |
| Python 3.11+ (3.14 검증) | `winget install astral-sh.uv` → `uv python install 3.14` |

두 CLI 모두 **로그인된 구독 계정**으로 동작합니다. 토론 한 라운드에 CLI 호출이 4~8회 들어가니 구독 사용량 한도를 염두에 두세요.

## 5분 설치

**처음 받는 분은 [docs/GUIDE.md](docs/GUIDE.md)만 따라 하세요** — ZIP 받기 → `setup.cmd` 더블클릭(파이썬·패키지·두 CLI 설치 + 각자 구독 계정 로그인 창 + 점검) → `launch_ui.cmd` 더블클릭. git이 없어도 됩니다.

직접 하려면:
```powershell
git clone https://github.com/an2889853-bot/ai-debate-room.git
cd ai-debate-room
python3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
New-Item -ItemType Directory -Force "$HOME\.streamlit" | Out-Null
"[general]`nemail = `"`"" | Out-File -Encoding utf8 "$HOME\.streamlit\credentials.toml"   # Streamlit 첫 실행이 Email 입력에서 멈추지 않게
.venv\Scripts\python.exe debate.py --check      # claude ✅ / codex ✅ 이면 준비 끝
.\launch_ui.cmd                                  # http://localhost:8501
```

자세한 설치·문제 해결은 [docs/SETUP.md](docs/SETUP.md).

## 사용법

1. 아래 입력창에 질문을 쓰거나 📎로 파일(텍스트·PDF·이미지)을 붙입니다.
2. 사이드바에서 **모드**(일반 / 코드 리뷰 / PLC 검증 / 투자 분석), **단계 수**(3 또는 5), **먼저 답하는 AI**를 고릅니다.
3. 진행 중엔 ⏸ 멈춤 → 한마디 끼워넣기 → ▶ 계속, ⏭ 바로 FINAL로, ⏹ 중단이 됩니다.
4. 결과는 `chats\` 폴더에 json/md로 저장되고 사이드바에서 다시 열 수 있습니다.

콘솔에서도 됩니다: `.venv\Scripts\python.exe debate.py "질문" --stages 5 --mode plc --file 회로.txt`

옵션 하나하나의 뜻과 화면 표시(🧾 🔬 🧑‍⚖️ 🛠 …) 읽는 법, 상황별 추천 설정은 [docs/USAGE.md](docs/USAGE.md).

### 🛠 도구 켜기 (기본 꺼짐)

사이드바 "🛠 도구" 확장에서:
- **🌐 웹 검색 허용** — 최신 사실이 필요한 질문(뉴스·시세·최신 버전)에. 검색 결과는 출처 URL과 함께 기록에 남습니다. 검색 범위 기본은 "최초 답변 + 평가"(검토·반박은 기록의 검색 결과를 재사용).
- **🛠 파일·명령 허용** — `workspace\<대화>\` 폴더 안에서 파일 읽기/쓰기와 허용 목록 명령(python·pytest·pip·git·ls…)만. 단계마다 git 커밋되어 되돌릴 수 있습니다.

> **주의**: 파일·명령을 켜면 **모델이 쓴 코드가 이 PC에서 실제로 실행됩니다.** 허용 목록 안이라도 `python`은 임의 코드 실행입니다. 폴더 격리·타임아웃·중단은 실수 방지지 보안 경계가 아닙니다. 믿을 수 있는 작업에서만 켜고, 다른 사람과 공유하는 PC에서는 켜지 마세요.

## 프롬프트만 쓰고 싶다면

코드 없이 지시문만 가져다 ChatGPT·Claude 웹에서 흉내 낼 수 있습니다 → [docs/PROMPTS.md](docs/PROMPTS.md) (공통 규칙, 단계별 지시문, `[지적 N]`/`[반영 N]` 계약, 평가자 지시문, 손으로 돌리는 절차).

## 개발

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest        # 112개, 약 10초 — 실제 CLI는 절대 호출하지 않음
```

| 문서 | 내용 |
|---|---|
| [docs/GUIDE.md](docs/GUIDE.md) | **처음 받는 분용 설치 가이드** — ZIP → setup.cmd → launch_ui.cmd, 막힐 때 표 |
| [docs/USAGE.md](docs/USAGE.md) | **사용 설명서** — 사이드바 옵션마다 기본값·특징, 화면 표시 읽는 법, 상황별 추천 설정 |
| [CLAUDE.md](CLAUDE.md) | 작업 지도 — 제약·규칙·현재 상태 (Claude Code / Codex로 개발할 때 자동으로 읽힘) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 코드가 어떻게 동작하나 (CLI 호출 플래그, 단계 계획, 반영 계약, 평가자, 도구·기록, 압축, 관측) |
| [docs/DECISIONS.md](docs/DECISIONS.md) | 왜 이렇게 만들었나 — 날짜별 결정·사고·실측 데이터 |
| [docs/SETUP.md](docs/SETUP.md) | 설치, 다른 컴퓨터에서 접속, 문제 해결 표 |
| [docs/PROMPTS.md](docs/PROMPTS.md) | 프롬프트 모음 (자동 생성) |

구조: `engine/`(엔진 12모듈) + `debate.py`(콘솔 진입점) + `app.py`(Streamlit UI). 설계는 OpenAI·Anthropic이 말하는 "하네스 엔지니어링"을 참고했습니다 — 도구를 막는 대신 범위를 정하고, 모든 행동을 기록에 남기고, 사람이 방향을 정합니다.

## 알려진 제한

- Codex는 글자 단위 스트리밍이 없어 GPT 단계는 완성되면 한 번에 표시됩니다(도구 사용은 실시간 표시).
- 브라우저를 새로고침하면 진행 중인 라운드는 사라집니다(저장된 라운드는 유지).
- 1인용입니다 — 서버 PC의 구독 계정으로 CLI를 실행하므로 여러 사람이 같이 쓰는 용도가 아닙니다.
- 평가자·요약자도 모델이라 같은 오해를 공유할 수 있습니다.

## 라이선스

MIT — [LICENSE](LICENSE)
