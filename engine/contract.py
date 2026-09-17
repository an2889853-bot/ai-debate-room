# -*- coding: utf-8 -*-
"""engine.contract — 규칙 문구, 판정 계약(검토 [판정]), 지적 번호별 반영 계약(default-FAIL), 독립 평가 판정·계획 조정."""
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


# ----------------------------------------------------------------------------
# 모드 프리셋 / 토론 단계 정의
# ----------------------------------------------------------------------------
COMMON_RULES = (
    "당신은 'AI Debate Room'의 참가자입니다. Claude와 GPT가 번갈아 검토·반박하며 사용자 질문에 대한 "
    "하나의 최선의 답을 만드는 것이 목표입니다.\n"
    "규칙:\n"
    "- 파일 읽기, 명령 실행, 웹 검색 등 어떤 도구도 사용하지 말고 오직 텍스트로만 답하십시오.\n"
    "- 답변 언어는 사용자 질문의 언어를 따르십시오 (한국어 질문이면 한국어).\n"
    "- 근거 없는 단정을 피하고, 확실하지 않은 부분은 그렇다고 밝히십시오.\n"
    "- 마크다운을 사용해도 되지만 불필요하게 길게 쓰지 마십시오.\n"
    "- 사용자 발언에 [첨부 파일] 블록이 있으면 그 내용을 근거로 활용하십시오.\n"
    "- 대화 기록에 '[사용자 · 개입]'이 있으면 사용자가 토론 중간에 끼어든 것입니다. 이후 단계는 그 요청을 최우선으로 반영하십시오.\n"
    "- 대화 기록의 '[프로그램 검사 · ...]' 블록은 사람이 아니라 프로그램이 답변 속 코드 블록을 실제로 검사(문법·파싱·실행)한 결과입니다. "
    "실패가 있으면 반드시 다루십시오. 통과했다고 해서 논리가 맞다는 뜻은 아닙니다.\n"
)

VERDICT_OK = "[판정: 추가 수정 불필요]"
VERDICT_NEED = "[판정: 수정 필요]"
VERDICT_OK_RE = re.compile(r"\[\s*판정\s*:\s*추가\s*수정\s*불필요\s*\]")
VERDICT_RULE = (f"\n답변의 **맨 마지막 줄**에 반드시 `{VERDICT_NEED}` 또는 `{VERDICT_OK}` 중 하나만 적으십시오. "
                "사소한 표현 차이만 남았으면 '추가 수정 불필요'로 판정하십시오.")


# ---- 지적 번호별 반영 계약 (default-FAIL) ----
# 검토/재검사는 지적을 `[지적 N] ...` 줄로 내고, 그에 답하는 반박/최종은 번호마다 `[반영 N]` 또는 `[반박 N]` 줄을 써야 한다.
# execute_stage()가 빠진 번호를 찾으면 같은 단계를 재요청(최대 MAX_CONTRACT_RETRIES회)하고, 그래도 빠지면 entry["contract"]["missing"]에
# 남겨 라운드가 "미처리 지적 있음"으로 표시된다 — 모델이 "반영했다"고 말하는 것이 아니라 번호별 기록이 있어야 처리된 것으로 본다.
ISSUE_RE = re.compile(r"\[\s*지적\s*(\d+)\s*\]")
NO_ISSUE_RE = re.compile(r"\[\s*지적\s*없음\s*\]")
RESOLVE_RE = re.compile(r"\[\s*(반영|반박)\s*(\d+)\s*\]")
REVIEW_KINDS = ("review", "recheck", "evaluate")   # 지적을 내는 단계 (평가자의 지적은 재작성 FINAL이 답한다)
RESPOND_KINDS = ("rebuttal", "final")    # 직전 지적에 답해야 하는 단계
MAX_CONTRACT_RETRIES = 1
ISSUE_FORMAT_RULE = (
    "\n지적은 한 줄에 하나씩, 중요도 순으로 `[지적 1] ...`, `[지적 2] ...` 형식으로 번호를 매겨 쓰십시오(번호는 1부터). "
    "지적할 것이 없으면 `[지적 없음]` 한 줄을 쓰십시오. 다음 단계가 이 번호로 항목별 반영/반박을 기록하고 프로그램이 검사합니다.")


def parse_issues(text: str) -> list[tuple[int, str]]:
    """검토 답변에서 `[지적 N] ...` 줄을 [(N, 본문)] 목록으로. 같은 번호는 첫 줄만 (굵게·목록 기호가 붙어도 인식)."""
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for line in (text or "").splitlines():
        m = ISSUE_RE.search(line)
        if m:
            n = int(m.group(1))
            if n not in seen:
                seen.add(n)
                out.append((n, line[m.end():].strip(" :*_-–—\t")))
    return out


def parse_resolutions(text: str) -> dict[int, str]:
    """응답에서 `[반영 N]` / `[반박 N]` → {N: "반영"|"반박"} (같은 번호가 여러 번이면 마지막)."""
    return {int(n): kind for kind, n in RESOLVE_RE.findall(text or "")}


def open_issues(history: list[dict]) -> tuple[dict | None, list[tuple[int, str]]]:
    """직전 AI 단계가 검토/재검사면 (그 항목, 지적 목록). 사용자 개입은 건너뛴다. 아니면 (None, [])."""
    for h in reversed(history):
        if h.get("who") == "user":
            continue
        if h.get("kind") in REVIEW_KINDS:
            return h, parse_issues(h.get("content", ""))
        return None, []
    return None, []


def check_contract(issues: list[tuple[int, str]], content: str) -> dict:
    """지적 번호마다 [반영/반박 N]이 있는지. {"issues": [N...], "resolved": {N: 종류}, "missing": [N...]}"""
    resolved = parse_resolutions(content)
    nums = [n for n, _ in issues]
    return {"issues": nums, "resolved": {n: resolved[n] for n in nums if n in resolved},
            "missing": [n for n in nums if n not in resolved]}


def contract_block(src: dict, issues: list[tuple[int, str]]) -> str:
    """응답 단계 프롬프트 끝에 붙이는 '처리해야 할 지적' 블록."""
    nums = ", ".join(str(n) for n, _ in issues)
    lines = "\n".join(f"[지적 {n}] {body}" for n, body in issues)
    return (f"\n\n=== 처리해야 할 지적 (직전 [{DISPLAY[src['who']]} · {src['label']}]) ===\n{lines}\n"
            f"위 지적 각각에 대해 답변 안에 `[반영 N] 무엇을 어떻게 고쳤는지 한 줄` 또는 `[반박 N] 근거` 줄을 반드시 넣으십시오 (N = {nums}). "
            "하나라도 빠지면 같은 요청을 다시 받게 됩니다.")


def retry_note(missing: list[int]) -> str:
    nums = ", ".join(map(str, missing))
    return (f"\n\n=== 재요청 ===\n직전 답변에서 지적 {nums}에 대한 [반영 N]/[반박 N] 판정이 빠졌습니다. "
            f"답변을 다시 쓰되 이번에는 지적 {nums} 각각에 `[반영 N] ...` 또는 `[반박 N] ...` 줄을 반드시 포함하십시오.")


def contract_line(c: dict) -> str:
    """단계 항목의 계약 결과 한 줄 (UI 캡션·콘솔·md 공용)."""
    res = c.get("resolved", {}) or {}
    acc = sum(1 for k in res.values() if k == "반영")
    rej = sum(1 for k in res.values() if k == "반박")
    src = c.get("source", "직전 검토")
    r = int(c.get("retries", 0) or 0)
    if c.get("missing"):
        return (f"⚠ {src}의 지적 {', '.join(map(str, c['missing']))} 미처리"
                + (f" (재요청 {r}회 후에도)" if r else "") + f" — 처리됨: 반영 {acc} · 반박 {rej}")
    return (f"{src}의 지적 {len(c.get('issues', []))}건 전부 처리 — 반영 {acc} · 반박 {rej}"
            + (f" (재요청 {r}회)" if r else ""))


def contract_summary(stages: list[dict]) -> dict:
    """라운드 전체 합계: {"issues", "accepted", "rejected", "retries", "missing": [(단계명, [N...])], "ok"}. 검사한 단계가 없으면 issues=0."""
    total = acc = rej = retries = 0
    missing: list[tuple[str, list[int]]] = []
    for h in stages:
        c = h.get("contract")
        if not c:
            continue
        res = c.get("resolved", {}) or {}
        total += len(c.get("issues", []))
        acc += sum(1 for k in res.values() if k == "반영")
        rej += sum(1 for k in res.values() if k == "반박")
        retries += int(c.get("retries", 0) or 0)
        if c.get("missing"):
            missing.append((f"{DISPLAY[h['who']]} · {h['label']}", list(c["missing"])))
    return {"issues": total, "accepted": acc, "rejected": rej, "retries": retries, "missing": missing, "ok": not missing}


# ---- 독립 평가자 (evaluate 단계) ----
# FINAL 뒤에 상대 AI가 새 호출로 최종 답변만 채점한다: 마지막 줄 [평가: PASS] / [평가: NEEDS_WORK], 문제는 [지적 N]으로.
# NEEDS_WORK면(옵션) FINAL을 한 번 다시 쓰고(평가자의 지적은 반영 계약으로 검사됨) 다시 평가한다 — 최대 1회.
EVAL_PASS = "[평가: PASS]"
EVAL_NEEDS = "[평가: NEEDS_WORK]"
EVAL_RE = re.compile(r"\[\s*평가\s*:\s*(PASS|NEEDS[_ ]?WORK)\s*\]", re.I)
EVAL_RULE = (f"\n답변의 **맨 마지막 줄**에 반드시 `{EVAL_PASS}` 또는 `{EVAL_NEEDS}` 중 하나만 적으십시오. "
             "사소한 표현 차이나 취향 문제는 PASS입니다. 사용자에게 해가 될 오류·누락·반영되지 않은 지적이 있을 때만 NEEDS_WORK입니다.")


def eval_verdict(entry: dict) -> str | None:
    """평가 단계 답변의 판정: "PASS" | "NEEDS_WORK" | None(형식 없음). 마지막 것이 유효."""
    found = EVAL_RE.findall(entry.get("content", "") or "")
    if not found:
        return None
    return "PASS" if found[-1].upper() == "PASS" else "NEEDS_WORK"


def eval_summary(stages: list[dict]) -> dict:
    """라운드의 평가 결과: {"verdict": 마지막 평가 판정|None, "evals": 평가 횟수, "revised": FINAL 재작성 여부}."""
    evals = [h for h in stages if h.get("kind") == "evaluate"]
    return {"verdict": eval_verdict(evals[-1]) if evals else None, "evals": len(evals),
            "revised": any(h.get("label", "").startswith("FINAL ") for h in stages)}


def adjust_plan_after(plan: list[dict], done: int, entry: dict, early_stop: bool = True,
                      eval_revise: bool = True) -> tuple[list[dict], dict | None]:
    """단계 하나가 끝난 뒤 남은 계획을 조정한다 (UI·콘솔 공용). done = 방금 끝난 단계까지 완료된 개수.
    - 검토가 '추가 수정 불필요'면(early_stop) 남은 검토/반박/재검사를 건너뛰고 FINAL(과 그 뒤 평가)로 → {"type": "skip", "skipped": [...]}
    - 평가가 NEEDS_WORK면(eval_revise, 아직 재작성 전) FINAL 2 + Eval 2를 뒤에 붙인다 → {"type": "revise", "added": [...]}
    아니면 (plan, None)."""
    if early_stop and entry.get("kind") == "review" and reviewer_says_ok(entry):
        final_idx = next((k for k, s in enumerate(plan) if s["kind"] == "final"), None)
        if final_idx is not None and final_idx > done:
            skipped = [s["label"] for s in plan[done:final_idx]]
            return plan[:done] + plan[final_idx:], {"type": "skip", "skipped": skipped}
    if eval_revise and entry.get("kind") == "evaluate" and eval_verdict(entry) == "NEEDS_WORK" and done >= len(plan):
        finals = [s for s in plan if s["kind"] == "final"]
        if len(finals) == 1:  # 아직 재작성 전
            final2 = dict(finals[0], label="FINAL 2",
                          instruction=finals[0]["instruction"] + " 이번에는 독립 평가자의 지적을 항목별로 판정·반영해 최종 답변을 다시 쓰십시오.")
            eval2 = dict(plan[-1], label="Eval 2")
            return plan + [final2, eval2], {"type": "revise", "added": ["FINAL 2", "Eval 2"]}
    return plan, None




def reviewer_says_ok(entry: dict) -> bool:
    """검토 단계 답변이 '[판정: 추가 수정 불필요]'로 끝났는지 (검토자가 Claude든 GPT든)."""
    return entry.get("who") != "user" and bool(VERDICT_OK_RE.search(entry.get("content", "")))


gpt_says_ok = reviewer_says_ok  # 하위 호환
