# -*- coding: utf-8 -*-
"""컨텍스트 압축: 필요 판정, 캐시, 요약 실패 시 기계적 요약, 프롬프트 반영, 콘솔 엔진 종단. CLI는 가짜."""
from __future__ import annotations

import debate as D
from conftest import patch_all

LONG = "가" * 5000


def entry(who: str, label: str, kind: str, content: str) -> dict:
    return {"who": who, "label": label, "kind": kind, "content": content}


HIST = [entry("claude", "Initial", "initial", "A" + LONG),
        entry("gpt", "Review", "review", "[지적 1] x\n" + LONG + D.VERDICT_NEED),
        entry("claude", "Rebuttal", "rebuttal", "[반영 1] y\n" + LONG),
        entry("gpt", "Recheck", "recheck", "[지적 1] z\n" + LONG + D.VERDICT_NEED)]


def test_compaction_needed():
    assert D.compaction_needed("q", HIST, None, 0) == 0            # 끔
    assert D.compaction_needed("q", HIST, None, 10**6) == 0        # 아직 짧음
    assert D.compaction_needed("q", HIST, None, 1000) == 2          # 4개 중 마지막 2개는 원문
    assert D.compaction_needed("q", HIST[:2], None, 1000) == 0      # keep_last 이하면 요약할 게 없다


def test_fallback_summary_truncates():
    s = D.fallback_summary(HIST[:2])
    assert s.startswith("[Claude · Initial]\nA") and "…(잘림)" in s
    assert len(s) < 2 * (D.COMPACT_FALLBACK_CHARS + 80)


def test_compact_history_caches_by_content(cli):
    f = cli(["요약본 1", "요약본 2"])
    cfg = D.Config(compact_chars=1000)
    cache: dict = {}
    c1 = D.compact_history("q", HIST, None, cfg, cache)
    assert c1 is cache and c1["count"] == 2 and c1["text"] == "요약본 1" and c1["method"] == "claude"
    assert c1["labels"] == "Claude·Initial ~ GPT·Review"
    assert len(f.calls) == 1 and "=== 요약할 단계들 ===" in f.calls[0] and "[GPT · Review]" in f.calls[0]
    assert D.compact_history("q", HIST, None, cfg, cache) is cache and len(f.calls) == 1   # 같은 대상 → 캐시
    changed = HIST[:1] + [entry("gpt", "Review", "review", "다른 내용" + LONG)] + HIST[2:]
    assert D.compact_history("q", changed, None, cfg, cache)["text"] == "요약본 2" and len(f.calls) == 2  # 내용 바뀜 → 재요약
    assert D.compact_history("q", HIST, None, cfg, None) is None                          # cache=None → 압축 안 함
    assert D.compact_history("q", HIST, None, D.Config(compact_chars=0), {}) is None


def test_summary_uses_low_effort(cli, monkeypatch):
    seen = {}
    f = cli(["요약"])
    orig = f.claude

    def spy(cfg, *a, **k):
        seen.update(effort=cfg.claude_effort, tools=cfg.tools, web=cfg.web_search)
        return orig(cfg, *a, **k)
    patch_all(monkeypatch, "call_claude", spy)
    D.summarize_entries(HIST[:2], D.Config(claude_effort="xhigh", tools=True, web_search=True))
    assert seen == {"effort": "low", "tools": False, "web": False}   # 요약자는 도구·웹 없이


def test_summarize_falls_back_when_claude_fails(monkeypatch):
    def boom(*a, **k):
        raise D.CLIError("claude", "요약", "실패")
    patch_all(monkeypatch, "call_claude", boom)
    text, method = D.summarize_entries(HIST[:2], D.Config())
    assert method == "fallback" and "…(잘림)" in text


def test_render_transcript_with_compaction():
    comp = {"count": 2, "text": "요약본", "method": "claude", "labels": "Claude·Initial ~ GPT·Review"}
    t = D.render_transcript("질문", HIST, None, comp)
    assert "[요약 · 이전 단계 2개 (Claude·Initial ~ GPT·Review) — 프로그램이 길이를 줄이려고 Claude가 요약" in t
    assert "요약본" in t and "[Claude · Rebuttal]" in t and "[GPT · Recheck]" in t
    assert "[Claude · Initial]" not in t and "[GPT · Review]" not in t
    assert "[요약 ·" not in D.render_transcript("질문", HIST, None, None)


def test_execute_stage_compacts_and_marks_entry(cli):
    f = cli(["요약본", "[반영 1] ok\n최종"])
    stage = D.plan_stages(1, "general", "claude", None, None, 5)[-1]    # claude · FINAL
    cache: dict = {}
    e = D.execute_stage("q", [], HIST, stage, D.Config(compact_chars=1000), plan_len=5, compaction=cache)
    assert len(f.calls) == 2                                            # 요약 1회 + 단계 1회
    prompt = f.calls[1]
    assert "[요약 · 이전 단계 2개" in prompt and "요약본" in prompt
    assert "[Claude · Initial]\nA" not in prompt and "[Claude · Rebuttal]" in prompt
    assert "처리해야 할 지적 (직전 [GPT · Recheck])" in prompt         # 반영 계약은 원문 기록으로 판단
    assert e["compacted"] == {"count": 2, "method": "claude"} and e["contract"]["missing"] == []
    f2 = cli(["[반영 1] ok\n최종"])                                      # 같은 캐시 → 요약 호출 없음
    e2 = D.execute_stage("q", [], HIST, stage, D.Config(compact_chars=1000), plan_len=5, compaction=cache)
    assert len(f2.calls) == 1 and e2["compacted"]["count"] == 2


def test_execute_stage_without_cache_does_not_compact(cli):
    f = cli(["[반영 1] ok\n최종"])
    stage = D.plan_stages(1, "general", "claude", None, None, 5)[-1]
    e = D.execute_stage("q", [], HIST, stage, D.Config(compact_chars=1000), plan_len=5)   # compaction=None
    assert len(f.calls) == 1 and "compacted" not in e and "[요약 ·" not in f.calls[0]


def test_run_debate_compacts_long_debate(cli):
    long = "나" * 3000
    f = cli([long,                                      # claude Initial
             "[지적 1] a\n" + long + D.VERDICT_NEED,      # gpt Review
             "[반영 1] b\n" + long,                       # claude Rebuttal
             "요약 1",                                    # Recheck 전 요약 (기록 3개 → 1개 요약)
             "[지적 1] c\n" + long + D.VERDICT_NEED,      # gpt Recheck
             "요약 2",                                    # FINAL 전 요약 (기록 4개 → 2개 요약)
             "[반영 1] d\n최종"])                          # claude FINAL
    run = D.run_debate("q", D.Config(compact_chars=5000), stage_count=5)
    assert run["status"] == "done" and len(f.calls) == 7
    assert run["compaction"]["count"] == 2 and run["compaction"]["text"] == "요약 2"
    assert run["stages"][3]["compacted"] == {"count": 1, "method": "claude"}
    assert run["stages"][4]["compacted"] == {"count": 2, "method": "claude"}
    assert "요약 1" in f.calls[4] and "요약 2" in f.calls[6]
    assert "compacted" not in run["stages"][0] and "compacted" not in run["stages"][2]
