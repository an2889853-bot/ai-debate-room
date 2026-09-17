# -*- coding: utf-8 -*-
"""engine.usage — 관측: 단계별 토큰·비용 해석, 라운드 합계, 저장된 대화 전체 통계."""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable
from .config import *  # noqa: F401,F403
from .attachments import *  # noqa: F401,F403
from .cli import *  # noqa: F401,F403
from .contract import *  # noqa: F401,F403
from .plan import *  # noqa: F401,F403
from .evidence import *  # noqa: F401,F403


# ---- 관측: 토큰·비용·시간 ----
# Claude는 결과 JSON의 usage(입력=input+cache_creation+cache_read, 출력=output)와 total_cost_usd(API 환산, 구독이면 실제 과금 아님).
# Codex는 CLI가 "tokens used: N" 줄을 찍을 때만 총 토큰을 안다(입력/출력 구분 없음). 모르면 None으로 두고 '?'로 표시한다.


def usage_of(entry: dict) -> dict:
    """단계 항목의 토큰·비용: {"in", "out", "total", "cost_usd"} (모르면 None)."""
    meta = entry.get("meta") or {}
    u = meta.get("usage") or {}
    if entry.get("who") == "claude" and u:
        inp = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        out = int(u.get("output_tokens") or 0)
        return {"in": inp, "out": out, "total": inp + out, "cost_usd": meta.get("cost_usd")}
    if u.get("total") is not None:
        return {"in": u.get("in"), "out": u.get("out"), "total": int(u["total"]), "cost_usd": None}
    return {"in": None, "out": None, "total": None, "cost_usd": None}


def fmt_tokens(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def usage_line(entry: dict) -> str:
    """캡션용 한 줄: '12.3k→0.8k 토큰 · API 환산 $0.04' / '총 1.2k 토큰' / '' (모르면)."""
    u = usage_of(entry)
    if u["total"] is None:
        return ""
    s = f"{fmt_tokens(u['in'])}→{fmt_tokens(u['out'])} 토큰" if u["in"] is not None else f"총 {fmt_tokens(u['total'])} 토큰"
    if u["cost_usd"]:
        s += f" · API 환산 ${u['cost_usd']:.3f}"
    return s


def usage_summary(stages: list[dict]) -> dict:
    """라운드 합계: {"stages": AI 단계 수, "elapsed": 초, "tokens": 합계(아는 것만), "known": 토큰을 아는 단계 수,
    "cost_usd": 합계(아는 것만), "by_who": {"claude": {"stages", "elapsed", "tokens"}, "gpt": {...}}}"""
    out = {"stages": 0, "elapsed": 0.0, "tokens": 0, "known": 0, "cost_usd": 0.0,
           "by_who": {"claude": {"stages": 0, "elapsed": 0.0, "tokens": 0}, "gpt": {"stages": 0, "elapsed": 0.0, "tokens": 0}}}
    for h in stages:
        who = h.get("who")
        if who not in out["by_who"]:
            continue
        u = usage_of(h)
        el = float(h.get("elapsed") or 0)
        out["stages"] += 1
        out["elapsed"] += el
        out["by_who"][who]["stages"] += 1
        out["by_who"][who]["elapsed"] += el
        if u["total"] is not None:
            out["tokens"] += u["total"]
            out["known"] += 1
            out["by_who"][who]["tokens"] += u["total"]
        if u["cost_usd"]:
            out["cost_usd"] += float(u["cost_usd"])
    out["elapsed"] = round(out["elapsed"], 1)
    out["cost_usd"] = round(out["cost_usd"], 4)
    return out


def usage_summary_line(stages: list[dict]) -> str:
    s = usage_summary(stages)
    if not s["stages"]:
        return ""
    bw = s["by_who"]
    tok = (f"토큰 {fmt_tokens(s['tokens'])} (Claude {fmt_tokens(bw['claude']['tokens'])} · GPT {fmt_tokens(bw['gpt']['tokens']) if bw['gpt']['tokens'] else '?'})"
           if s["known"] else "토큰 ?")
    line = f"⏱ 합계 {s['elapsed']:.0f}s ({s['stages']}단계) · {tok}"
    if s["cost_usd"]:
        line += f" · API 환산 ${s['cost_usd']:.2f}"
    if s["known"] and s["known"] < s["stages"]:
        line += f" · 토큰 미확인 {s['stages'] - s['known']}단계"
    return line


def stats(conversations: list[dict]) -> dict:
    """저장된 대화 전체 집계 (하네스 관측용). 라운드 수, AI별 단계 수·평균 시간, 조기 종료율, 반영 계약(지적·미처리·재요청),
    독립 평가(PASS율·재작성), 토큰·비용 합계."""
    st = {"conversations": len(conversations), "rounds": 0, "done": 0, "early_stopped": 0,
          "by_who": {"claude": {"stages": 0, "elapsed": 0.0, "tokens": 0}, "gpt": {"stages": 0, "elapsed": 0.0, "tokens": 0}},
          "contract": {"rounds": 0, "issues": 0, "accepted": 0, "rejected": 0, "retries": 0, "rounds_missing": 0},
          "eval": {"rounds": 0, "pass": 0, "needs_work": 0, "revised": 0},
          "tokens": 0, "cost_usd": 0.0, "elapsed": 0.0}
    for conv in conversations:
        for r in conv.get("rounds", []):
            st["rounds"] += 1
            st["done"] += r.get("status") == "done"
            st["early_stopped"] += bool(r.get("early_stopped"))
            u = usage_summary(r.get("stages", []))
            st["tokens"] += u["tokens"]
            st["cost_usd"] += u["cost_usd"]
            st["elapsed"] += u["elapsed"]
            for who in ("claude", "gpt"):
                for k in ("stages", "elapsed", "tokens"):
                    st["by_who"][who][k] += u["by_who"][who][k]
            c = r.get("contract") or contract_summary(r.get("stages", []))
            if c.get("issues"):
                st["contract"]["rounds"] += 1
                for k in ("issues", "accepted", "rejected", "retries"):
                    st["contract"][k] += int(c.get(k) or 0)
                st["contract"]["rounds_missing"] += bool(c.get("missing"))
            ev = r.get("evaluation") or eval_summary(r.get("stages", []))
            if ev.get("evals"):
                st["eval"]["rounds"] += 1
                st["eval"]["pass"] += ev.get("verdict") == "PASS"
                st["eval"]["needs_work"] += ev.get("verdict") == "NEEDS_WORK"
                st["eval"]["revised"] += bool(ev.get("revised"))
    for who in ("claude", "gpt"):
        b = st["by_who"][who]
        b["avg_elapsed"] = round(b["elapsed"] / b["stages"], 1) if b["stages"] else None
    st["cost_usd"] = round(st["cost_usd"], 2)
    st["elapsed"] = round(st["elapsed"], 1)
    return st


def stats_lines(st: dict) -> list[str]:
    """통계를 사람이 읽는 줄 목록으로 (콘솔·사이드바 공용)."""
    bw, c, e = st["by_who"], st["contract"], st["eval"]
    lines = [f"대화 {st['conversations']}개 · 라운드 {st['rounds']}개 (완료 {st['done']}, 조기 종료 {st['early_stopped']})",
             f"Claude {bw['claude']['stages']}단계 평균 {bw['claude']['avg_elapsed'] or '?'}s · GPT {bw['gpt']['stages']}단계 평균 {bw['gpt']['avg_elapsed'] or '?'}s",
             f"토큰 {fmt_tokens(st['tokens'])} (Claude {fmt_tokens(bw['claude']['tokens'])} · GPT {fmt_tokens(bw['gpt']['tokens']) if bw['gpt']['tokens'] else '?'}) · API 환산 ${st['cost_usd']:.2f} · 총 {st['elapsed']:.0f}s"]
    if c["rounds"]:
        lines.append(f"반영 계약 {c['rounds']}라운드: 지적 {c['issues']} → 반영 {c['accepted']} · 반박 {c['rejected']} · 재요청 {c['retries']}회 · 미처리 라운드 {c['rounds_missing']}")
    if e["rounds"]:
        lines.append(f"독립 평가 {e['rounds']}라운드: PASS {e['pass']} · NEEDS_WORK {e['needs_work']} · 재작성 {e['revised']}")
    return lines
