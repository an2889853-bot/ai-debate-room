"""AI Debate Room - 토론 엔진 (Windows)

구독 계정으로 로그인된 Claude Code CLI(`claude -p`)와 OpenAI Codex CLI(`codex exec`)를
subprocess로 번갈아 호출해 토론을 수행한다. 각 단계에는 직전 답변만이 아니라
지금까지의 **전체 대화 기록**(사용자의 중간 개입 포함)을 전달한다.

  Initial(Claude) → [Review(GPT) → Rebuttal(Claude)] × N → Recheck(GPT) → FINAL(Claude)

사용 예 (PowerShell, 프로젝트 폴더에서):
  python debate.py --check                     # 두 CLI가 응답하는지 OK 테스트만
  python debate.py "PLC 인터록 회로를 만들어줘"   # 기본: 일반 모드, 3단계(최초 → 검토 → 최종)
  python debate.py -q "질문" --mode plc --stages 5 --rounds 2 --file 회로.txt
  python debate.py --list-codex-models
"""

# 이 파일은 얇은 facade다. 구현은 engine/ 패키지에 있고, 여기서는 모든 공개 이름을 다시 내보내 `import debate as D` 호출부(app.py, tests)와
# 콘솔 진입점(`python debate.py`)을 그대로 유지한다. 테스트는 conftest.patch_all()로 facade와 engine.* 모듈의 같은 이름을 함께 바꾼다.
from __future__ import annotations

import sys

from engine.console import *  # noqa: F401,F403  (console이 앞선 모듈을 전부 star-import 하므로 공개 이름이 모두 들어온다)
from engine.cli import _popen_stream, _run_quiet, _tidy_command, _rel  # noqa: F401  (밑줄 이름은 star-import에 안 실리므로 명시)
from engine.evidence import _run_python  # noqa: F401
from engine.attachments import _find_tool, _decode_text, _pdf_pages_to_images, _pdf_to_text  # noqa: F401
from engine.store import _round_lines, _ILLEGAL  # noqa: F401
from engine.cli import _codex_models_cache, _claude_image_message  # noqa: F401

if __name__ == "__main__":
    sys.exit(main())
