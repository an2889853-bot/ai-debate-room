# -*- coding: utf-8 -*-
"""관측: 단계별 토큰·비용 해석, 라운드 합계, 저장된 대화 전체 통계. CLI 없음."""
from __future__ import annotations

import debate as D

CLAUDE_ENTRY = {"who": "claude", "label": "Review", "kind": "review", "elapsed": 97.8,
                "meta": {"usage": {"input_tokens": 2, "cache_creation_input_tokens": 11986, "cache_read_input_tokens": 0,
                                   "output_tokens": 6752}, "cost_usd": 0.1234}}
GPT_ENTRY = {"who": "gpt", "label": "Initial", "kind": "initial", "elapsed": 19.0,
             "meta": {"model": "gpt-5.6-sol", "usage": {"total": 4321}}}
GPT_UNKNOWN = {"who": "gpt", "label": "FINAL", "kind": "final", "elapsed": 10.0, "meta": {"model": "gpt-5.6-sol"}}


def test_usage_of_claude_gpt_and_unknown():
    assert D.usage_of(CLAUDE_ENTRY) == {"in": 11988, "out": 6752, "total": 18740, "cost_usd": 0.1234}
    assert D.usage_of(GPT_ENTRY) == {"in": None, "out": None, "total": 4321, "cost_usd": None}
    assert D.usage_of(GPT_UNKNOWN) == {"in": None, "out": None, "total": None, "cost_usd": None}
    assert D.usage_of({"who": "user", "content": "x"})["total"] is None      # 사용자 개입엔 meta가 없다


def test_fmt_tokens_and_usage_line():
    assert D.fmt_tokens(None) == "?" and D.fmt_tokens(850) == "850" and D.fmt_tokens(18740) == "18.7k"
    assert D.usage_line(CLAUDE_ENTRY) == "12.0k→6.8k 토큰 · API 환산 $0.123"
    assert D.usage_line(GPT_ENTRY) == "총 4.3k 토큰"
    assert D.usage_line(GPT_UNKNOWN) == ""


def test_codex_tokens_used_regex():
    m = D.TOKENS_USED_RE.search("model: gpt-5.6-sol\n...\ntokens used: 12,345\n")
    assert m and int(m.group(1).replace(",", "")) == 12345
    assert D.TOKENS_USED_RE.search("Tokens Used 99") is not None
    assert D.TOKENS_USED_RE.search("no usage here") is None


def test_usage_summary_and_line():
    stages = [GPT_ENTRY, CLAUDE_ENTRY, D.interjection_entry("한마디"), GPT_UNKNOWN]
    s = D.usage_summary(stages)
    assert s["stages"] == 3 and s["elapsed"] == 126.8 and s["tokens"] == 23061 and s["known"] == 2
    assert s["cost_usd"] == 0.1234
    assert s["by_who"]["claude"] == {"stages": 1, "elapsed": 97.8, "tokens": 18740}
    assert s["by_who"]["gpt"] == {"stages": 2, "elapsed": 29.0, "tokens": 4321}
    line = D.usage_summary_line(stages)
    assert line.startswith("⏱ 합계 127s (3단계) · 토큰 23.1k (Claude 18.7k · GPT 4.3k) · API 환산 $0.12")
    assert "토큰 미확인 1단계" in line
    assert D.usage_summary_line([]) == "" and D.usage_summary([])["stages"] == 0


def test_stats_over_conversations():
    r1 = {"status": "done", "early_stopped": True, "stages": [GPT_ENTRY, CLAUDE_ENTRY, GPT_UNKNOWN],
          "contract": {"issues": 2, "accepted": 1, "rejected": 1, "retries": 1, "missing": [], "ok": True},
          "evaluation": {"verdict": "PASS", "evals": 2, "revised": True}}
    r2 = {"status": "stopped", "early_stopped": False, "stages": [CLAUDE_ENTRY]}   # contract/evaluation 키 없음 → 단계에서 계산
    st = D.stats([{"rounds": [r1, r2]}, {"rounds": []}])
    assert st["conversations"] == 2 and st["rounds"] == 2 and st["done"] == 1 and st["early_stopped"] == 1
    assert st["by_who"]["claude"]["stages"] == 2 and st["by_who"]["claude"]["avg_elapsed"] == 97.8
    assert st["by_who"]["gpt"]["stages"] == 2 and st["by_who"]["gpt"]["avg_elapsed"] == 14.5
    assert st["contract"] == {"rounds": 1, "issues": 2, "accepted": 1, "rejected": 1, "retries": 1, "rounds_missing": 0}
    assert st["eval"] == {"rounds": 1, "pass": 1, "needs_work": 0, "revised": 1}
    assert st["tokens"] == 23061 + 18740 and st["cost_usd"] == 0.25
    lines = D.stats_lines(st)
    assert lines[0] == "대화 2개 · 라운드 2개 (완료 1, 조기 종료 1)"
    assert any(l.startswith("반영 계약 1라운드: 지적 2 → 반영 1 · 반박 1 · 재요청 1회") for l in lines)
    assert any(l.startswith("독립 평가 1라운드: PASS 1") for l in lines)


def test_stats_empty():
    st = D.stats([])
    assert st["rounds"] == 0 and st["by_who"]["claude"]["avg_elapsed"] is None
    assert len(D.stats_lines(st)) == 3


def test_run_debate_records_usage(cli):
    cli(["첫", "[지적 없음]\n" + D.VERDICT_NEED, "최종"])
    run = D.run_debate("q", D.Config(), stage_count=3)
    assert run["usage"]["stages"] == 3 and run["usage"]["known"] == 0      # 가짜 CLI는 토큰을 안 준다
