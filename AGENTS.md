# AGENTS.md

이 저장소의 에이전트 지침은 **[CLAUDE.md](CLAUDE.md)** 한 곳에 있다 (Claude Code·Codex 공용). 먼저 그 파일을 읽고, 세부는
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)(현재 동작) · [docs/DECISIONS.md](docs/DECISIONS.md)(결정·사고 기록) · [docs/SETUP.md](docs/SETUP.md)(설치·접속)을 본다.

지켜야 할 것 (CLAUDE.md "작업 규칙"과 같음): 실제 claude/codex 호출로 테스트하지 않기, `debate.py` 수정 후 8501 서버 재시작, `.cmd`는 ASCII만, 수정 → `pytest` → 커밋, 결정은 DECISIONS.md 맨 아래에.

참고: 토론 실행 중 Codex는 `sandbox\`를 작업 폴더로 호출되므로 이 파일을 읽지 않는다. 이 파일은 이 저장소를 Codex CLI로 **개발**할 때를 위한 것이다.
