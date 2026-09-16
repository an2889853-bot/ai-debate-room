# -*- coding: utf-8 -*-
"""독립 평가자(evaluate 단계): 계획, 판정 파싱, 계획 조정(조기 종료·재작성), 콘솔 엔진 종단. CLI는 가짜."""
from __future__ import annotations

import debate as D


def test_default_plan_appends_evaluator_of_other_side():
    assert D.default_plan(1, "claude", None, 3, evaluate=True)[-1] == ("gpt", "evaluate")
    assert D.default_plan(1, "gpt", "claude", 3, evaluate=True)[-1] == ("gpt", "evaluate")   # 최종 정리 claude → 평가자 gpt
    assert D.default_plan(1, "claude", None, 5, evaluate=True)[-2:] == [("claude", "final"), ("gpt", "evaluate")]
    assert "evaluate" not in [k for _, k in D.default_plan(1, "claude", None, 3)]


def test_validate_plan_evaluate_rules():
    ok = [("claude", "initial"), ("gpt", "review"), ("claude", "final"), ("gpt", "evaluate")]
    assert D.validate_plan(ok) is None
    assert "바로 뒤" in D.validate_plan([("claude", "initial"), ("gpt", "evaluate"), ("claude", "final")])
    assert "한 번만" in D.validate_plan(ok + [("gpt", "evaluate")])
    assert "마지막 단계" in D.validate_plan([("claude", "initial"), ("gpt", "review")])


def test_plan_stages_eval_instruction_and_label():
    plan = D.plan_stages(1, "general", "claude", None, None, 3, evaluate=True)
    ev = plan[-1]
    assert ev["label"] == "Eval" and ev["who"] == "gpt" and ev["kind"] == "evaluate"
    assert "[평가: PASS]" in ev["instruction"] and "[지적 1]" in ev["instruction"] and "Claude" in ev["instruction"]
    assert "{" not in ev["instruction"]
    assert D.plan_preview(plan).endswith("GPT·Eval")


def test_eval_verdict():
    assert D.eval_verdict({"content": "좋음\n[평가: PASS]"}) == "PASS"
    assert D.eval_verdict({"content": "[평가: needs_work]"}) == "NEEDS_WORK"
    assert D.eval_verdict({"content": "[평가: PASS]\n...\n[ 평가 : NEEDS WORK ]"}) == "NEEDS_WORK"   # 마지막이 유효
    assert D.eval_verdict({"content": "판정 없음"}) is None


def test_adjust_plan_after_skip_keeps_evaluation():
    plan = D.plan_stages(1, "general", "claude", None, None, 5, evaluate=True)
    review = {"kind": "review", "who": "gpt", "content": "[지적 없음]\n" + D.VERDICT_OK}
    new, ev = D.adjust_plan_after(plan, 2, review, early_stop=True)
    assert [s["label"] for s in new] == ["Initial", "Review", "FINAL", "Eval"]
    assert ev == {"type": "skip", "skipped": ["Rebuttal", "Recheck"]}
    assert D.adjust_plan_after(plan, 2, review, early_stop=False) == (plan, None)


def test_adjust_plan_after_revises_once():
    plan = D.plan_stages(1, "general", "claude", None, None, 3, evaluate=True)
    needs = {"kind": "evaluate", "who": "gpt", "content": "[지적 1] 결론이 모호\n" + D.EVAL_NEEDS}
    new, ev = D.adjust_plan_after(plan, 4, needs)
    assert [s["label"] for s in new] == ["Initial", "Review", "FINAL", "Eval", "FINAL 2", "Eval 2"]
    assert ev == {"type": "revise", "added": ["FINAL 2", "Eval 2"]}
    assert new[4]["kind"] == "final" and new[4]["who"] == "claude" and "독립 평가자의 지적" in new[4]["instruction"]
    assert new[5]["kind"] == "evaluate" and new[5]["who"] == "gpt"
    assert D.adjust_plan_after(new, 6, needs) == (new, None)                       # 두 번째 NEEDS_WORK는 더 안 붙임
    assert D.adjust_plan_after(plan, 4, {"kind": "evaluate", "content": D.EVAL_PASS}) == (plan, None)
    assert D.adjust_plan_after(plan, 4, needs, eval_revise=False) == (plan, None)
    assert D.adjust_plan_after(plan, 3, needs) == (plan, None)                     # 평가가 마지막이 아니면(진행 중) 무시


def test_eval_summary_and_final_of():
    stages = [{"kind": "final", "label": "FINAL", "content": "v1"},
              {"kind": "evaluate", "label": "Eval", "content": "[지적 1] x\n" + D.EVAL_NEEDS},
              {"kind": "final", "label": "FINAL 2", "content": "v2"},
              {"kind": "evaluate", "label": "Eval 2", "content": D.EVAL_PASS}]
    assert D.eval_summary(stages) == {"verdict": "PASS", "evals": 2, "revised": True}
    assert D.eval_summary([]) == {"verdict": None, "evals": 0, "revised": False}
    assert D.final_of({"stages": stages}) == "v2"
    assert D.final_of({"stages": stages[:1]}) == "v1"
    assert D.final_of({"stages": [{"label": "FINAL", "content": "구 기록"}]}) == "구 기록"   # 구 기록엔 kind가 없다


def test_run_debate_with_evaluation_and_revision(cli):
    f = cli(["첫 답",                                    # claude initial
             "[지적 없음]\n" + D.VERDICT_NEED,             # gpt review — 지적은 없지만 '수정 필요'라 조기 종료 없음
             "최종 v1",                                   # claude FINAL (검사할 지적 없음)
             "[지적 1] 결론이 없음\n" + D.EVAL_NEEDS,       # gpt Eval → NEEDS_WORK
             "[반영 1] 결론 추가\n최종 v2",                 # claude FINAL 2 (평가자 지적을 계약으로 검사)
             "[지적 없음]\n" + D.EVAL_PASS])               # gpt Eval 2
    events = []
    run = D.run_debate("q", D.Config(), on_event=events.append, first="claude", stage_count=3, evaluate=True)
    assert run["status"] == "done" and len(f.calls) == 6
    assert [e["label"] for e in run["stages"]] == ["Initial", "Review", "FINAL", "Eval", "FINAL 2", "Eval 2"]
    assert run["evaluation"] == {"verdict": "PASS", "evals": 2, "revised": True}
    assert run["stages"][4]["contract"] == {"issues": [1], "resolved": {1: "반영"}, "missing": [], "retries": 0,
                                            "source": "GPT · Eval"}
    assert any(e["type"] == "revise" for e in events) and run["plan"][-2:] == ["FINAL 2", "Eval 2"]
    assert "[GPT · Eval]" in f.calls[4] and "처리해야 할 지적 (직전 [GPT · Eval])" in f.calls[4]
    assert D.final_of(run) == "[반영 1] 결론 추가\n최종 v2"


def test_run_debate_evaluate_pass_no_revision(cli):
    f = cli(["첫", "[지적 없음]\n" + D.VERDICT_NEED, "최종", "[지적 없음]\n" + D.EVAL_PASS])
    run = D.run_debate("q", D.Config(), stage_count=3, evaluate=True)
    assert len(f.calls) == 4 and run["evaluation"] == {"verdict": "PASS", "evals": 1, "revised": False}


def test_run_debate_no_evaluate_by_default(cli):
    f = cli(["첫", "[지적 없음]\n" + D.VERDICT_NEED, "최종"])
    run = D.run_debate("q", D.Config(), stage_count=3)
    assert len(f.calls) == 3 and run["evaluation"] == {"verdict": None, "evals": 0, "revised": False}
