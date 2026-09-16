# -*- coding: utf-8 -*-
"""토론 엔진(debate.py)의 계획·프롬프트·판정·저장 이름 로직. CLI 호출 없음."""
from __future__ import annotations

import pytest

import debate as D

CLAUDE_FIRST_5 = [("claude", "initial"), ("gpt", "review"), ("claude", "rebuttal"), ("gpt", "recheck"), ("claude", "final")]


# ---- default_plan / plan_stages -------------------------------------------------
def test_default_is_three_stages():
    assert D.DEFAULT_STAGE_COUNT == 3
    assert D.default_plan() == [("claude", "initial"), ("gpt", "review"), ("claude", "final")]


def test_three_stages_respects_first_and_final_who():
    assert D.default_plan(1, "gpt", None, 3) == [("gpt", "initial"), ("claude", "review"), ("gpt", "final")]
    assert D.default_plan(1, "gpt", "claude", 3) == [("gpt", "initial"), ("claude", "review"), ("claude", "final")]


def test_five_stages_and_rounds():
    assert D.default_plan(1, "claude", None, 5) == CLAUDE_FIRST_5
    seven = D.default_plan(2, "claude", None, 5)
    assert [k for _, k in seven] == ["initial", "review", "rebuttal", "review", "rebuttal", "recheck", "final"]


def test_rounds_ignored_in_three_stages():
    assert len(D.default_plan(3, "claude", None, 3)) == 3


@pytest.mark.parametrize("mode", list(D.MODES))
@pytest.mark.parametrize("stage_count", D.STAGE_COUNTS)
def test_plan_stages_formats_every_mode(mode, stage_count):
    plan = D.plan_stages(1, mode, "claude", None, None, stage_count)
    assert len(plan) == stage_count
    for s in plan:
        assert "{" not in s["instruction"], f"채워지지 않은 자리표시자: {s['instruction']}"


def test_final_gets_review_note_only_without_rebuttal():
    three = D.plan_stages(1, "general", "gpt", None, None, 3)
    five = D.plan_stages(1, "general", "gpt", None, None, 5)
    note = "앞선 검토의 지적을 항목별로 판정"
    assert note in three[-1]["instruction"]
    assert note not in five[-1]["instruction"]


def test_other_placeholder_follows_order():
    plan = D.plan_stages(1, "general", "gpt", None, None, 3)
    assert "Claude" in plan[0]["instruction"]      # 최초 답변(GPT)은 다음 검토자(Claude)를 가리킴
    assert "GPT" in plan[1]["instruction"]         # 검토(Claude)는 직전 작성자(GPT)를 가리킴
    assert "최초 답변" in plan[1]["instruction"]


def test_custom_plan_wins_over_stage_count():
    plan = D.plan_stages(1, "general", custom=[("claude", "initial"), ("gpt", "final")], stage_count=5)
    assert [(s["who"], s["kind"]) for s in plan] == [("claude", "initial"), ("gpt", "final")]


@pytest.mark.parametrize("steps, msg", [
    ([("gpt", "review"), ("claude", "final")], "첫 단계"),
    ([("claude", "initial"), ("gpt", "review")], "마지막 단계"),
    ([("claude", "initial"), ("gpt", "initial"), ("claude", "final")], "한 번만"),
    ([("claude", "initial"), ("bob", "review"), ("claude", "final")], "알 수 없는"),
])
def test_validate_plan_rejects(steps, msg):
    err = D.validate_plan(steps)
    assert err and msg in err
    with pytest.raises(ValueError):
        D.plan_stages(1, "general", custom=steps)


def test_empty_custom_means_default_plan():
    assert "하나도" in D.validate_plan([])
    # plan_stages는 빈 custom을 '직접 편집 없음'으로 보고 기본 순서를 쓴다
    assert len(D.plan_stages(1, "general", custom=[], stage_count=3)) == 3


# ---- 판정 / 프롬프트 --------------------------------------------------------------
def test_reviewer_says_ok():
    ok = {"who": "gpt", "content": f"괜찮습니다.\n{D.VERDICT_OK}"}
    need = {"who": "gpt", "content": f"문제 있음.\n{D.VERDICT_NEED}"}
    spaced = {"who": "claude", "content": "[ 판정 : 추가 수정 불필요 ]"}
    assert D.reviewer_says_ok(ok) and D.reviewer_says_ok(spaced)
    assert not D.reviewer_says_ok(need)
    assert not D.reviewer_says_ok({"who": "user", "content": D.VERDICT_OK})  # 사용자 발언은 판정이 아님


def test_build_prompt_contains_full_transcript_and_interjection():
    plan = D.plan_stages(1, "general", "claude", None, None, 3)
    history = [
        {"who": "claude", "label": "Initial", "content": "첫 답"},
        D.interjection_entry("비상정지는 하드와이어 전제"),
    ]
    prompt = D.build_prompt("질문입니다", history, plan[1], len(plan))
    assert "[사용자]\n질문입니다" in prompt
    assert "[Claude · Initial]\n첫 답" in prompt
    assert "[사용자 · 개입]\n비상정지는 하드와이어 전제" in prompt
    assert "이번 단계 (2/3): GPT · Review" in prompt


def test_build_prompt_includes_prior_rounds():
    plan = D.plan_stages(1, "general", "claude", None, None, 3)
    prior = [{"question": "이전 질문", "final": "이전 결론"}]
    prompt = D.build_prompt("새 질문", [], plan[0], 3, prior=prior)
    assert "[이전 질문 1]\n이전 질문" in prompt and "[이전 최종 답변 1]\n이전 결론" in prompt


# ---- 콘솔 엔진 run_debate (execute_stage 대역) ----------------------------------------
def _fake_execute(review_ok: bool):
    verdict = D.VERDICT_OK if review_ok else D.VERDICT_NEED

    def run(question, attachments, history, stage, cfg, prior=None, mode="general", plan_len=5, **kw):
        body = {"review": f"지적.\n{verdict}", "recheck": f"재검사.\n{verdict}"}.get(stage["kind"], stage["label"])
        return {"who": stage["who"], "label": stage["label"], "kind": stage["kind"], "content": body,
                "elapsed": 0, "meta": {}, "prompt_chars": 0}
    return run


def test_run_debate_three_stages(monkeypatch):
    monkeypatch.setattr(D, "execute_stage", _fake_execute(review_ok=False))
    events = []
    run = D.run_debate("q", D.Config(), on_event=events.append, first="gpt", stage_count=3)
    assert run["status"] == "done" and run["stage_count"] == 3
    assert [e["kind"] for e in run["stages"]] == ["initial", "review", "final"]
    assert [e["who"] for e in run["stages"]] == ["gpt", "claude", "gpt"]
    assert not run["early_stopped"] and not any(e["type"] == "skip" for e in events)


def test_run_debate_five_stages_early_stop(monkeypatch):
    monkeypatch.setattr(D, "execute_stage", _fake_execute(review_ok=True))
    events = []
    run = D.run_debate("q", D.Config(), on_event=events.append, stage_count=5)
    assert run["early_stopped"] and [e["kind"] for e in run["stages"]] == ["initial", "review", "final"]
    skip = next(e for e in events if e["type"] == "skip")
    assert skip["skipped"] == ["Rebuttal", "Recheck"]


def test_run_debate_five_stages_no_early_stop(monkeypatch):
    monkeypatch.setattr(D, "execute_stage", _fake_execute(review_ok=True))
    run = D.run_debate("q", D.Config(), early_stop=False, stage_count=5)
    assert [e["kind"] for e in run["stages"]] == ["initial", "review", "rebuttal", "recheck", "final"]


# ---- 저장 이름 -------------------------------------------------------------------
def test_make_title_and_slugify():
    assert D.make_title("# **PLC** 인터록 회로를 만들어줘\n둘째 줄") == "PLC 인터록 회로를 만들어줘"
    assert D.make_title("x" * 40).endswith("…")
    assert D.slugify('a/b:c*d?  e  ') == "abcd_e"
    assert D.slugify("...") == "chat"
