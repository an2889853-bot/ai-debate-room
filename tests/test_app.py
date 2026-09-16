# -*- coding: utf-8 -*-
"""Streamlit UI 종단 테스트 (streamlit.testing.v1.AppTest). 단계 실행은 conftest의 FakeStages가 대신하므로
실제 CLI는 호출되지 않는다. 각 at.run()이 한 단계씩 진행시킨다(가짜 스레드가 끝나면 본문이 결과를 붙임)."""
from __future__ import annotations

import time

from streamlit.testing.v1 import AppTest

from conftest import ROOT

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


def configure(at, *, stage_count: int, first: str, pause_each: bool = False, early_stop: bool = True) -> None:
    radio(at, "대화 단계 수").set_value(stage_count)
    select(at, "먼저 답하는 AI").set_value(first)
    select(at, "최종 정리 AI").set_value("same")
    checkbox(at, "검토 AI가").set_value(early_stop)
    checkbox(at, "단계마다 멈춰서").set_value(pause_each)
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
    assert len(caps) == 1 and caps[0].count("→") == r.value - 1


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
