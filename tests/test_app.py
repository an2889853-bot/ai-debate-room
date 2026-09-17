# -*- coding: utf-8 -*-
"""Streamlit UI 종단 테스트 (streamlit.testing.v1.AppTest). 단계 실행은 conftest의 FakeStages가 대신하므로
실제 CLI는 호출되지 않는다. 각 at.run()이 한 단계씩 진행시킨다(가짜 스레드가 끝나면 본문이 결과를 붙임)."""
from __future__ import annotations

import time
from pathlib import Path

from streamlit.testing.v1 import AppTest

import debate as D
from conftest import ROOT, patch_all

APP = str(ROOT / "app.py")


def boot() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state["conv"] is None, "격리된 chats 폴더인데 이전 대화가 복원됨"
    return at


def radio(at, label):
    return next(r for r in at.sidebar.radio if r.label == label)


def select(at, label):
    return next(s for s in at.sidebar.selectbox if s.label == label)


def checkbox(at, prefix):
    return next(c for c in at.sidebar.checkbox if c.label.startswith(prefix))


def button(at, label):
    return next(b for b in at.button if b.label == label)


def configure(at, *, stage_count: int, first: str, pause_each: bool = False, early_stop: bool = True,
              evaluate: bool = False) -> None:
    radio(at, "대화 단계 수").set_value(stage_count)
    select(at, "먼저 답하는 AI").set_value(first)
    select(at, "최종 정리 AI").set_value("same")
    checkbox(at, "검토 AI가").set_value(early_stop)
    checkbox(at, "단계마다 멈춰서").set_value(pause_each)
    checkbox(at, "🧑‍⚖️ FINAL 뒤").set_value(evaluate)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]


def submit(at, text: str) -> None:
    """질문 제출. 가짜 단계는 즉시 끝나므로 이 한 번의 run 안에서 라운드가 통째로 끝날 수도 있다."""
    at.chat_input[0].set_value(text).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    started = at.session_state["active"] is not None or bool((at.session_state["conv"] or {}).get("rounds"))
    assert started, "질문을 보냈는데 라운드가 시작되지 않음"


def drive(at, max_runs: int = 40) -> None:
    """가짜 단계가 끝날 때마다 재실행해 라운드가 끝나거나 일시정지될 때까지 진행."""
    for _ in range(max_runs):
        active = at.session_state["active"]
        if active is None or active["status"] == "paused":
            return
        live = at.session_state["live"]
        if live is not None:
            t0 = time.time()
            while not live["done"] and time.time() - t0 < 10:
                time.sleep(0.02)
            assert live["done"], "가짜 단계가 10초 안에 끝나지 않음"
        at.run()
        assert not at.exception, [str(e.value) for e in at.exception]
    raise AssertionError("라운드가 끝나지 않음")


def last_round(at) -> dict:
    conv = at.session_state["conv"]
    assert conv is not None and conv["rounds"], "저장된 라운드가 없음"
    return conv["rounds"][-1]


# ---------------------------------------------------------------------------------
def test_sidebar_defaults_render(isolated):
    at = boot()
    r = radio(at, "대화 단계 수")
    assert r.options == ["3단계 (최초 → 검토 → 최종)", "5단계 (최초 → 검토 → 반박 → 재검사 → 최종)"]
    caps = [c.value for c in at.sidebar.caption if c.value.startswith("순서:")]
    eval_on = checkbox(at, "🧑‍⚖️ FINAL 뒤").value          # 기본 켜짐 → 순서 끝에 Eval 한 단계가 더 붙는다
    assert eval_on and len(caps) == 1 and caps[0].count("→") == r.value - 1 + 1 and caps[0].endswith("·Eval")
    compact = next(n for n in at.sidebar.number_input if n.label.startswith("긴 토론 요약 기준"))
    assert compact.value == 60, "기본 60천 자"


def test_three_stage_round_end_to_end(isolated, fake_stages):
    fake = fake_stages(review_ok=False)
    at = boot()
    configure(at, stage_count=3, first="claude")
    assert not any(n.label.startswith("검토 ↔ 반박") for n in at.sidebar.number_input), "3단계에선 라운드 수 입력이 숨어야 함"
    submit(at, "3단계 테스트 질문")
    drive(at)

    rnd = last_round(at)
    assert rnd["status"] == "done" and rnd["stage_count"] == 3 and not rnd["early_stopped"]
    assert [(e["who"], e["kind"]) for e in rnd["stages"]] == [("claude", "initial"), ("gpt", "review"), ("claude", "final")]
    assert rnd["plan_steps"] == [["claude", "initial"], ["gpt", "review"], ["claude", "final"]]
    # 매 단계에 전체 기록이 누적 전달되고, 3단계 FINAL에는 검토 반영 지시가 붙는다
    assert [c["history"] for c in fake.calls] == [0, 1, 2]
    assert "[GPT · Review]" in fake.calls[-1]["prompt"]
    assert "앞선 검토의 지적을 항목별로 판정" in fake.calls[-1]["instruction"]
    # 지적 번호별 반영 계약: FINAL 프롬프트에 처리할 지적 블록, 항목·라운드에 계약 결과, 화면에 ✅ 캡션
    assert "=== 처리해야 할 지적 (직전 [GPT · Review]) ===" in fake.calls[-1]["prompt"]
    assert rnd["stages"][-1]["contract"]["resolved"] == {1: "반영", 2: "반박"}
    assert rnd["contract"]["ok"] and rnd["contract"]["issues"] == 2
    assert any("검토 지적 2건 전부 처리됨" in c.value for c in at.caption)
    # 관측: 단계 캡션의 토큰, 라운드 합계, 저장 dict의 usage
    assert any("1.0k→200 토큰 · API 환산 $0.010" in c.value for c in at.caption)
    assert any(c.value.startswith("⏱ 합계") and "토큰 2.9k (Claude 2.4k · GPT 500)" in c.value for c in at.caption)
    assert rnd["usage"]["tokens"] == 2900 and rnd["usage"]["known"] == 3
    # 자동 저장: 격리된 chats 폴더에 json + md
    saved = list((isolated / "chats").glob("*.json"))
    assert len(saved) == 1 and saved[0].with_suffix(".md").exists()
    assert "최종 답변입니다." in saved[0].with_suffix(".md").read_text(encoding="utf-8")


def test_five_stage_full_run_gpt_first(isolated, fake_stages):
    fake = fake_stages(review_ok=False)
    at = boot()
    configure(at, stage_count=5, first="gpt")
    assert any(n.label.startswith("검토 ↔ 반박") for n in at.sidebar.number_input), "5단계에선 라운드 수 입력이 보여야 함"
    submit(at, "5단계 테스트")
    drive(at)

    rnd = last_round(at)
    assert rnd["status"] == "done" and rnd["stage_count"] == 5
    assert [e["kind"] for e in rnd["stages"]] == ["initial", "review", "rebuttal", "recheck", "final"]
    assert [e["who"] for e in rnd["stages"]] == ["gpt", "claude", "gpt", "claude", "gpt"]
    assert [c["history"] for c in fake.calls] == [0, 1, 2, 3, 4]


def test_five_stage_early_stop(isolated, fake_stages):
    fake_stages(review_ok=True)
    at = boot()
    configure(at, stage_count=5, first="claude", early_stop=True)
    submit(at, "조기 종료 테스트")
    drive(at)

    rnd = last_round(at)
    assert rnd["early_stopped"] is True
    assert [e["kind"] for e in rnd["stages"]] == ["initial", "review", "final"]


def test_pause_and_interjection(isolated, fake_stages):
    fake = fake_stages(review_ok=False)
    at = boot()
    configure(at, stage_count=3, first="claude", pause_each=True)
    submit(at, "개입 테스트")

    drive(at)  # Initial 뒤 일시정지
    active = at.session_state["active"]
    assert active["status"] == "paused" and [e["kind"] for e in active["stages"]] == ["initial"]

    at.text_area[0].set_value("비상정지는 하드와이어로 분리한다는 전제로 검토해줘")
    button(at, "▶ 계속").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    drive(at)  # Review 뒤 다시 일시정지
    assert at.session_state["active"]["status"] == "paused"
    button(at, "▶ 계속").click().run()
    drive(at)

    rnd = last_round(at)
    assert rnd["status"] == "done"
    assert [e["kind"] for e in rnd["stages"]] == ["initial", "interjection", "review", "final"]
    review_prompt = next(c for c in fake.calls if c["kind"] == "review")["prompt"]
    assert "[사용자 · 개입]\n비상정지는 하드와이어로" in review_prompt


def test_independent_evaluation_with_revision(isolated, fake_stages):
    """FINAL 뒤 상대 AI가 평가 → NEEDS_WORK → FINAL 2(평가자 지적을 계약으로 검사) → Eval 2 PASS."""
    fake = fake_stages(review_ok=False)
    at = boot()
    configure(at, stage_count=3, first="claude", evaluate=True)
    caps = [c.value for c in at.sidebar.caption if c.value.startswith("순서:")]
    assert caps[0].endswith("GPT·Eval"), "최종 정리를 쓰지 않은 쪽(GPT)이 평가자"
    submit(at, "평가 테스트")
    drive(at)
    rnd = last_round(at)
    assert [e["label"] for e in rnd["stages"]] == ["Initial", "Review", "FINAL", "Eval", "FINAL 2", "Eval 2"]
    assert rnd["evaluation"] == {"verdict": "PASS", "evals": 2, "revised": True}
    assert rnd["stages"][4]["contract"]["source"] == "GPT · Eval" and rnd["stages"][4]["contract"]["missing"] == []
    assert D.final_of(rnd) == rnd["stages"][4]["content"]
    assert "처리해야 할 지적 (직전 [GPT · Eval])" in fake.calls[4]["prompt"]
    assert any("독립 평가: PASS (FINAL 재작성 후 재평가)" in c.value for c in at.caption)
    assert any("3단계" in c.value for c in at.caption), "설정 요약의 단계 수는 평가를 세지 않는다"


def test_code_review_mode_shows_evidence(isolated, fake_stages):
    """code_review 모드에서 답변의 python 블록이 검사되고(문법 OK 1 · 실패 1) 화면에 검사 결과가 보인다."""
    fake = fake_stages(review_ok=False)
    at = boot()
    select(at, "모드").set_value("code_review")
    configure(at, stage_count=3, first="claude")
    assert not checkbox(at, "🔬 코드 블록").value, "코드 실행은 기본으로 꺼져 있어야 함"
    submit(at, "코드 검토")
    drive(at)
    rnd = last_round(at)
    ev = rnd["stages"][0]["evidence"]
    assert [(b["check"], b["ok"]) for b in ev] == [("syntax", True), ("syntax", False)]
    assert "[프로그램 검사 · Claude · Initial의 코드 블록" in fake.calls[1]["prompt"]
    assert any("코드 검사 2개 블록" in c.value for c in at.caption)


def test_tools_default_on_creates_workspace(isolated, fake_stages):
    """두 토글 모두 기본 켜짐(사용자 지정). 🛠이 켜진 라운드는 대화별 workspace가 생기고 라운드·대화에 기록된다."""
    fake_stages(review_ok=False)
    at = boot()
    assert checkbox(at, "🛠 파일·명령").value and checkbox(at, "🌐 웹 검색").value
    configure(at, stage_count=3, first="claude")
    submit(at, "도구 테스트")
    drive(at)
    rnd = last_round(at)
    assert rnd["workspace"] and Path(rnd["workspace"]).parent == isolated / "ws"
    assert rnd["config"]["tools"] is True and rnd["config"]["web_search"] is True
    assert rnd["config"]["workspace"] == rnd["workspace"]
    assert at.session_state["conv"]["workspace"] == rnd["workspace"]
    assert any("📁 작업 폴더" in c.value for c in at.caption)


def test_tools_toggle_off_means_no_workspace(isolated, fake_stages):
    fake_stages(review_ok=False)
    at = boot()
    checkbox(at, "🛠 파일·명령").set_value(False)
    checkbox(at, "🌐 웹 검색").set_value(False)
    configure(at, stage_count=3, first="claude")
    submit(at, "도구 끔")
    drive(at)
    rnd = last_round(at)
    assert rnd["workspace"] is None and rnd["config"]["tools"] is False and rnd["config"]["web_search"] is False
    assert not any("📁 작업 폴더" in c.value for c in at.caption)


def test_unresolved_issues_show_warning(isolated, fake_stages):
    """반박/최종이 [반영/반박 N]을 빼먹으면 라운드가 '미처리 지적' 경고를 단다 (default-FAIL)."""
    fake_stages(review_ok=False, resolve=False)
    at = boot()
    configure(at, stage_count=3, first="claude")
    submit(at, "계약 위반 테스트")
    drive(at)
    rnd = last_round(at)
    assert rnd["status"] == "done"
    assert rnd["stages"][-1]["contract"]["missing"] == [1, 2]
    assert rnd["contract"]["missing"] == [("Claude · FINAL", [1, 2])] and not rnd["contract"]["ok"]
    assert any("미처리 지적" in w.value for w in at.warning)


def test_stop_then_resume(isolated, fake_stages):
    fake_stages(review_ok=False)
    at = boot()
    configure(at, stage_count=3, first="claude", pause_each=True)
    submit(at, "중단·재개 테스트")
    drive(at)
    button(at, "⏹ 중단").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rnd = last_round(at)
    assert rnd["status"] == "stopped" and [e["kind"] for e in rnd["stages"]] == ["initial"]
    assert at.session_state["active"] is None

    checkbox(at, "단계마다 멈춰서").set_value(False).run()   # 재개 전에 끄지 않으면 다음 단계 뒤 또 멈춘다
    button(at, "▶ 이어서 진행 (중단된 지점부터)").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    drive(at)
    rnd = last_round(at)
    assert rnd["status"] == "done"
    assert [e["kind"] for e in rnd["stages"]] == ["initial", "review", "final"]
    assert len(at.session_state["conv"]["rounds"]) == 1, "재개는 라운드를 새로 만들지 않고 이어 붙여야 함"
