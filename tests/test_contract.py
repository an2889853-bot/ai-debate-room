# -*- coding: utf-8 -*-
"""지적 번호별 반영 계약 (default-FAIL): 파싱, 프롬프트 블록, execute_stage의 재요청, 라운드 합계. CLI는 가짜."""
from __future__ import annotations

import pytest

import debate as D

REVIEW = "검토 결과입니다.\n**[지적 1]** 근거가 약함\n- [지적 2]: 예외 처리 누락\n[지적 3] 용어 오류\n[지적 2] (중복)\n" + D.VERDICT_NEED
HISTORY = [
    {"who": "gpt", "label": "Initial", "kind": "initial", "content": "첫 답"},
    {"who": "claude", "label": "Review", "kind": "review", "content": REVIEW},
]
FULL = "[반영 1] 근거 보강\n[반박 2] 이미 처리됨\n[반영 3] 용어 수정\n최종 답변"


def final_stage(stage_count: int = 3) -> dict:
    return D.plan_stages(1, "general", "gpt", None, None, stage_count)[-1]


# ---- 파싱 ---------------------------------------------------------------------
def test_parse_issues_tolerates_markdown_and_dedupes():
    assert D.parse_issues(REVIEW) == [(1, "근거가 약함"), (2, "예외 처리 누락"), (3, "용어 오류")]
    assert D.parse_issues("[지적 없음]\n" + D.VERDICT_OK) == []
    assert D.parse_issues("") == []


def test_parse_resolutions_last_wins():
    assert D.parse_resolutions("[반영 1] a\n[ 반박 2 ] b\n[반영 2] c") == {1: "반영", 2: "반영"}


def test_open_issues_only_from_latest_review_like_stage():
    src, issues = D.open_issues(HISTORY)
    assert src is HISTORY[1] and [n for n, _ in issues] == [1, 2, 3]
    # 사용자 개입이 뒤에 있어도 직전 AI 단계(검토)를 찾는다
    src2, issues2 = D.open_issues(HISTORY + [D.interjection_entry("한마디")])
    assert src2 is HISTORY[1] and len(issues2) == 3
    # 직전 AI 단계가 반박이면 검사할 지적 없음
    assert D.open_issues(HISTORY + [{"who": "gpt", "label": "Rebuttal", "kind": "rebuttal", "content": FULL}]) == (None, [])
    assert D.open_issues([]) == (None, [])


def test_check_contract():
    c = D.check_contract([(1, "a"), (2, "b"), (3, "c")], "[반영 1] x\n[반박 3] y")
    assert c == {"issues": [1, 2, 3], "resolved": {1: "반영", 3: "반박"}, "missing": [2]}


def test_build_prompt_appends_contract_block():
    src, issues = D.open_issues(HISTORY)
    p = D.build_prompt("q", HISTORY, final_stage(), 3, issues=issues, issue_src=src)
    assert "=== 처리해야 할 지적 (직전 [Claude · Review]) ===" in p
    assert "[지적 2] 예외 처리 누락" in p and "(N = 1, 2, 3)" in p
    assert "처리해야 할 지적" not in D.build_prompt("q", HISTORY, final_stage(), 3)


def test_review_templates_carry_issue_format_rule():
    plan = D.plan_stages(1, "general", "claude", None, None, 5)
    for s in plan:
        if s["kind"] in D.REVIEW_KINDS:
            assert "[지적 1]" in s["instruction"] and "[지적 없음]" in s["instruction"]
        if s["kind"] == "rebuttal":
            assert "[반영 N]" in s["instruction"] and "[반박 N]" in s["instruction"]


# ---- execute_stage: 재요청 (call_claude / call_codex 대역 = conftest.cli) ---------------------
def test_complete_answer_needs_no_retry(cli):
    f = cli([FULL])
    e = D.execute_stage("q", [], HISTORY, final_stage(), D.Config(), plan_len=3)   # who=gpt → codex 경로
    assert len(f.calls) == 1
    assert e["contract"] == {"issues": [1, 2, 3], "resolved": {1: "반영", 2: "반박", 3: "반영"}, "missing": [],
                             "retries": 0, "source": "Claude · Review"}
    assert "처리해야 할 지적" in f.calls[0]


def test_missing_verdict_triggers_one_retry_then_succeeds(cli):
    f = cli(["[반영 1] 근거 보강\n최종 답변", FULL])
    stage = D.plan_stages(1, "general", "claude", None, None, 3)[-1]   # who=claude → claude 경로
    e = D.execute_stage("q", [], HISTORY, stage, D.Config(), plan_len=3)
    assert len(f.calls) == 2
    assert "=== 재요청 ===" in f.calls[1] and "지적 2, 3" in f.calls[1]
    assert "재요청" not in f.calls[0]
    assert e["contract"]["retries"] == 1 and e["contract"]["missing"] == [] and e["content"] == FULL


def test_retry_exhausted_records_missing(cli):
    f = cli(["[반영 1] 만 처리"])
    e = D.execute_stage("q", [], HISTORY, final_stage(), D.Config(), plan_len=3)
    assert len(f.calls) == 1 + D.MAX_CONTRACT_RETRIES
    assert e["contract"]["missing"] == [2, 3] and e["contract"]["retries"] == D.MAX_CONTRACT_RETRIES


def test_max_retries_zero(cli):
    f = cli(["[반영 1] 만 처리"])
    e = D.execute_stage("q", [], HISTORY, final_stage(), D.Config(), plan_len=3, max_retries=0)
    assert len(f.calls) == 1 and e["contract"]["missing"] == [2, 3] and e["contract"]["retries"] == 0


def test_no_open_issues_means_no_contract(cli):
    f = cli(["최종 답변"])
    no_issue_hist = [HISTORY[0], {"who": "claude", "label": "Review", "kind": "review", "content": "[지적 없음]\n" + D.VERDICT_OK}]
    e = D.execute_stage("q", [], no_issue_hist, final_stage(), D.Config(), plan_len=3)
    assert "contract" not in e and len(f.calls) == 1 and "처리해야 할 지적" not in f.calls[0]
    # 검토 단계 자체는 검사 대상이 아니다
    f2 = cli(["[지적 1] x\n" + D.VERDICT_NEED])
    review_stage = D.plan_stages(1, "general", "gpt", None, None, 3)[1]
    e2 = D.execute_stage("q", [], [HISTORY[0]], review_stage, D.Config(), plan_len=3)
    assert "contract" not in e2 and len(f2.calls) == 1


# ---- 라운드 합계 / 콘솔 엔진 ---------------------------------------------------------
def test_contract_summary_and_line():
    ok = {"who": "gpt", "label": "Rebuttal", "contract": {"issues": [1, 2], "resolved": {1: "반영", 2: "반박"}, "missing": [],
                                                          "retries": 0, "source": "Claude · Review"}}
    bad = {"who": "gpt", "label": "FINAL", "contract": {"issues": [1], "resolved": {}, "missing": [1],
                                                        "retries": 1, "source": "Claude · Recheck"}}
    s = D.contract_summary([{"who": "gpt", "label": "Initial"}, ok, bad])
    assert s == {"issues": 3, "accepted": 1, "rejected": 1, "retries": 1, "missing": [("GPT · FINAL", [1])], "ok": False}
    assert D.contract_summary([]) == {"issues": 0, "accepted": 0, "rejected": 0, "retries": 0, "missing": [], "ok": True}
    assert D.contract_line(ok["contract"]) == "Claude · Review의 지적 2건 전부 처리 — 반영 1 · 반박 1"
    assert D.contract_line(bad["contract"]).startswith("⚠ Claude · Recheck의 지적 1 미처리 (재요청 1회 후에도)")


def test_run_debate_three_stages_enforces_contract(cli):
    f = cli(["첫 답",                                   # gpt initial
             "[지적 1] a\n[지적 2] b\n" + D.VERDICT_NEED,  # claude review
             "[반영 1] x\n최종",                          # gpt final — 2번 빠짐 → 재요청
             "[반영 1] x\n[반박 2] y\n최종"])             # 재요청 응답
    run = D.run_debate("q", D.Config(), first="gpt", stage_count=3)
    assert run["status"] == "done" and len(f.calls) == 4
    assert run["stages"][-1]["contract"]["retries"] == 1
    assert run["contract"] == {"issues": 2, "accepted": 1, "rejected": 1, "retries": 1, "missing": [], "ok": True}
