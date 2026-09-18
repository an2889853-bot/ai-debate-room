# -*- coding: utf-8 -*-
"""engine/ 의 지시문 상수를 docs/PROMPTS.md 로 내보낸다 (코드 없이 프롬프트만 쓰려는 사람용).
실행: .venv\\Scripts\\python.exe tools\\export_prompts.py   — 지시문을 고쳤으면 다시 실행해 문서를 맞춘다."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import debate as D  # noqa: E402

OUT = ROOT / "docs" / "PROMPTS.md"
PLACEHOLDERS = {
    "{other}": "상대 AI 이름 (최초 답변에선 다음 검토자, 그 외엔 직전 단계 작성자 — 예: Claude, GPT)",
    "{target}": "첫 검토면 '최초 답변', 두 번째부터는 '최신 수정본'",
    "{round_note}": "두 번째 검토부터 붙는 문장: ' 이미 해결된 지적은 반복하지 말고, 새로 생긴 문제나 아직 남은 문제에 집중하십시오.' (첫 검토는 빈 문자열)",
    "{fb_note}": "3단계(반박 없음)에서만 붙는 문장: 검토 지적을 [반영 N]/[반박 N]으로 판정하고 최종 답변에 녹이라는 지시 (5단계는 빈 문자열)",
}


def code(text: str) -> str:
    return "```text\n" + text.strip("\n") + "\n```\n"


def main() -> None:
    parts = [
        "# 프롬프트 모음 (PROMPTS.md)\n",
        "> `tools/export_prompts.py`가 `engine/`의 상수에서 **자동 생성**한 파일입니다. 손으로 고치지 말고 스크립트를 다시 실행하세요.\n",
        "코드 없이 ChatGPT·Claude 웹 같은 곳에서 이 토론 방식을 흉내 내려는 분을 위한 원문입니다. 앱은 매 단계를 **독립된 호출**로 돌리며, "
        "호출마다 (1) 아래 공통 규칙을 system prompt로, (2) 지금까지의 전체 대화 기록을, (3) 이번 단계 지시문을 넣습니다.\n",
        "## 1. 공통 규칙 (system prompt, 도구 꺼진 기본)\n", code(D.COMMON_RULES),
        "도구를 켰을 때는 세 번째 줄('어떤 도구도 사용하지 말고')이 아래로 바뀝니다 (파일·명령 + 웹 + 호출 상한 8):\n",
        code(D.tool_rules(tools=True, web=True, budget=8)),
        "## 2. 단계별 지시문\n",
        "자리표시자: " + " · ".join(f"`{k}` = {v}" for k, v in PLACEHOLDERS.items()) + "\n",
    ]
    for kind, text in D.STAGE_TEMPLATES.items():
        parts.append(f"### {D.KIND_LABEL[kind]} — {D.KIND_KO[kind]}\n")
        parts.append(code(text))
    parts += [
        "## 3. 프로그램이 검사하는 계약 문구\n",
        "검토·재검사·평가 지시문 끝에 붙는 문장들입니다. 앱은 이 형식을 정규식으로 읽어 조기 종료·재요청·재작성을 결정합니다.\n",
        "**검토 판정 (조기 종료용)**\n", code(D.VERDICT_RULE),
        "**지적 번호 형식**\n", code(D.ISSUE_FORMAT_RULE),
        "**평가 판정**\n", code(D.EVAL_RULE),
        "**반박/최종 단계에 붙는 '처리해야 할 지적' 블록 (예시)**\n",
        code(D.contract_block({"who": "gpt", "label": "Review"}, [(1, "근거가 약함"), (2, "예외 처리 누락")])),
        "빠진 번호가 있으면 앱이 같은 단계를 한 번 더 요청하며 이 문장을 덧붙입니다:\n", code(D.retry_note([2])),
        "## 4. 모드 프리셋\n",
    ]
    for key, m in D.MODES.items():
        parts.append(f"### {m['name']} (`{key}`) — {m['description']}\n")
        parts.append("**시스템 규칙에 덧붙임**\n" + (code(m["rules"]) if m["rules"] else "(없음)\n"))
        if m["hints"]:
            parts.append("**단계별 힌트**\n")
            for k, h in m["hints"].items():
                parts.append(f"- {D.KIND_LABEL[k]}: {h.strip()}\n")
    parts += [
        "\n## 5. 긴 토론 요약자 규칙\n", code(D.SUMMARY_RULES),
        "## 6. 손으로 돌리는 순서 (3단계 기준)\n",
        "1. **최초 답변** — 모델 A에게: 공통 규칙 + 사용자 질문 + Initial 지시문(`{other}`를 모델 B 이름으로).\n"
        "2. **검토** — 모델 B에게(새 대화): 공통 규칙 + `[사용자] 질문` + `[A · Initial] 답변` + Review 지시문. 답은 `[지적 N]` 목록과 마지막 줄 `[판정: ...]`.\n"
        "3. **최종** — 모델 A에게(새 대화): 공통 규칙 + 전체 기록 + FINAL 지시문 + 위 3절의 '처리해야 할 지적' 블록. `[반영 N]`/`[반박 N]`이 번호마다 있는지 직접 확인하고, 빠졌으면 재요청 문장으로 다시.\n"
        "4. (선택) **평가** — 모델 B에게(새 대화): 전체 기록 + Eval 지시문. NEEDS_WORK면 A에게 FINAL 2를 요청하고 다시 평가.\n",
        "\n5단계는 검토 뒤에 `Rebuttal`(A) → `Recheck`(B)을 끼웁니다. 매 단계 **전체 기록**을 다시 주는 것이 핵심입니다 — 모델에 기억은 없습니다.\n",
    ]
    OUT.write_text("".join(parts), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
