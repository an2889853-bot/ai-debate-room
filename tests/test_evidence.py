# -*- coding: utf-8 -*-
"""코드 블록 검사(외부 증거): 펜스 추출, 문법/파싱 검사, 옵션 실행, 다음 단계 프롬프트 전달. CLI는 가짜, 파이썬 실행은 진짜."""
from __future__ import annotations

import debate as D

ANSWER = (
    "설명입니다.\n"
    "```python\nprint('hi')\nx = 1 + 1\n```\n"
    "중간 글.\n"
    "```json\n{\"a\": 1}\n```\n"
    "```py\ndef f(:\n    pass\n```\n"
    "```text\n|--[ X0 ]--( Y0 )--|\n```\n"
    "```toml\nname = \"x\"\n```\n"
    "```json\n{bad}\n```\n"
)


def test_extract_code_blocks_keeps_order_and_lang():
    blocks = D.extract_code_blocks(ANSWER)
    assert [b["lang"] for b in blocks] == ["python", "json", "py", "text", "toml", "json"]
    assert blocks[0]["code"] == "print('hi')\nx = 1 + 1\n"
    assert D.extract_code_blocks("펜스 없음") == []


def test_syntax_and_parse_checks_without_running():
    ev = D.check_code_blocks(ANSWER, run=False)
    assert [(b["index"], b["lang"], b["check"], b["ok"]) for b in ev] == [
        (1, "python", "syntax", True), (2, "json", "parse", True), (3, "python", "syntax", False),
        (5, "toml", "parse", True), (6, "json", "parse", False)]      # text 블록(4)은 검사 대상 아님
    assert "SyntaxError" in ev[2]["detail"] and "줄 1" in ev[2]["detail"]
    assert ev[0]["lines"] == 2
    assert not (D.RUN_DIR.exists() and any(D.RUN_DIR.iterdir())), "실행을 안 켰는데 실행 폴더가 생김"


def test_run_python_blocks_when_enabled():
    ev = D.check_code_blocks("```python\nprint('결과 42')\n```\n```python\nimport sys\nsys.exit(3)\n```", run=True)
    assert ev[0]["check"] == "run" and ev[0]["ok"] and "exit 0" in ev[0]["detail"] and "결과 42" in ev[0]["detail"]
    assert ev[1]["check"] == "run" and not ev[1]["ok"] and "exit 3" in ev[1]["detail"]
    assert not (D.RUN_DIR.exists() and any(D.RUN_DIR.iterdir())), "실행 폴더가 정리되지 않음"


def test_run_timeout_and_syntax_error_not_run():
    ev = D.check_code_blocks("```python\nwhile True:\n    pass\n```\n```python\ndef f(:\n```", run=True, timeout=1)
    assert ev[0]["check"] == "run" and not ev[0]["ok"] and "초과" in ev[0]["detail"]
    assert ev[1]["check"] == "syntax" and not ev[1]["ok"]   # 문법 오류면 실행하지 않는다


def test_run_reads_no_stdin():
    ev = D.check_code_blocks("```python\ninput('x')\n```", run=True, timeout=5)
    assert not ev[0]["ok"] and "EOFError" in ev[0]["detail"]


def test_evidence_summary_and_render():
    h = {"who": "claude", "label": "Initial", "evidence": D.check_code_blocks(ANSWER)}
    assert D.evidence_summary(h["evidence"]) == "코드 검사 5개 블록 — OK 3 · 실패 2 (문법·파싱만)"
    text = D.render_evidence(h)
    assert text.startswith("[프로그램 검사 · Claude · Initial의 코드 블록")
    assert "- 블록 3 (python, 2줄) syntax: 실패 — SyntaxError" in text


def test_execute_stage_attaches_evidence_and_next_stage_sees_it(cli):
    f = cli(["```python\nprint(1\n```\n첫 답", "[지적 1] 문법 오류\n" + D.VERDICT_NEED])
    plan = D.plan_stages(1, "code_review", "claude", None, None, 3)
    e0 = D.execute_stage("q", [], [], plan[0], D.Config(), mode="code_review", plan_len=3)
    assert e0["evidence"][0]["ok"] is False and e0["evidence"][0]["check"] == "syntax"
    e1 = D.execute_stage("q", [], [e0], plan[1], D.Config(), mode="code_review", plan_len=3)
    assert "[프로그램 검사 · Claude · Initial의 코드 블록" in f.calls[1]
    assert "SyntaxError" in f.calls[1]
    assert "evidence" not in e1   # 검토 답변엔 코드 블록이 없다


def test_execute_stage_runs_only_when_config_enabled(cli, monkeypatch):
    calls = []
    monkeypatch.setattr(D, "_run_python", lambda code, timeout: (calls.append(code) or (True, "exit 0")))
    cli(["```python\nprint(1)\n```"])
    stage = D.plan_stages(1, "general", "claude", None, None, 3)[0]
    D.execute_stage("q", [], [], stage, D.Config(run_code=False), plan_len=3)
    assert calls == []
    e = D.execute_stage("q", [], [], stage, D.Config(run_code=True), plan_len=3)
    assert calls == ["print(1)\n"] and e["evidence"][0]["check"] == "run"


def test_common_rules_mention_program_check():
    assert "[프로그램 검사" in D.system_rules("general")
