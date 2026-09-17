# -*- coding: utf-8 -*-
"""engine.prompt — 프롬프트 조립: 이전 라운드, 전체 대화 기록(요약·증거 블록 포함), 이번 단계 지시, 처리해야 할 지적."""
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
from .usage import *  # noqa: F401,F403


# ----------------------------------------------------------------------------
# 프롬프트 구성
# ----------------------------------------------------------------------------
def actions_summary(actions: list[dict], denials: list | None = None) -> str:
    """캡션 한 줄: '도구 3회 — Bash 2 · WebSearch 1 (실패 1) · 거부 1'."""
    if not actions and not denials:
        return "도구 사용 없음"
    counts: dict[str, int] = {}
    for a in actions or []:
        counts[a["tool"]] = counts.get(a["tool"], 0) + 1
    s = f"도구 {len(actions or [])}회 — " + " · ".join(f"{k} {v}" for k, v in counts.items()) if actions else "도구 사용 없음"
    err = sum(1 for a in actions or [] if a.get("error"))
    if err:
        s += f" (실패 {err})"
    if denials:
        s += f" · 거부 {len(denials)}"
    return s


def render_actions(h: dict) -> str:
    """대화 기록에 들어가는 도구 사용 블록 — 모델의 말과 구분되는 프로그램 머리말."""
    lines = [f"[프로그램 기록 · {DISPLAY[h['who']]} · {h['label']}의 도구 사용 — 사람이 아니라 프로그램이 기록한 실제 명령과 결과]"]
    for a in h.get("actions") or []:
        out = (a.get("output") or "").strip().replace("\n", " ⏎ ")
        line = f"- {a['tool']}: {a['input']}"
        if out:
            line += f" → {out[:300]}"
        if a.get("error"):
            line += " [실패]"
        lines.append(line)
    for d in h.get("denials") or []:
        name = d.get("tool_name") or d.get("tool") or "?"
        lines.append(f"- ⚠ 거부됨 (허용 목록 밖): {name} {json.dumps(d.get('tool_input'), ensure_ascii=False)[:200]}")
    ws = h.get("workspace") or {}
    if ws.get("changed"):
        lines.append(f"- 작업 폴더 변경: {', '.join(ws['changed'])}" + (f" (git 커밋 {ws['commit']})" if ws.get("commit") else ""))
    return "\n".join(lines)


def render_prior(prior: list[dict]) -> str:
    """이전 라운드(질문 + 최종 답변)를 참고용 블록으로 렌더링. UI에서 이어지는 질문에 사용."""
    if not prior:
        return ""
    parts = []
    for k, p in enumerate(prior, start=1):
        parts.append(f"[이전 질문 {k}]\n{p['question'].strip()}\n\n[이전 최종 답변 {k}]\n{p['final'].strip()}")
    return ("=== 이전 대화 (이미 끝난 토론의 질문과 최종 답변, 참고용) ===\n"
            + "\n\n".join(parts) + "\n\n")


def render_transcript(question: str, history: list[dict], attachments: list[dict] | None = None,
                      compaction: dict | None = None) -> str:
    """compaction이 있으면 앞 count개 항목 대신 요약 블록을 넣는다."""
    parts = [f"[사용자]\n{question.strip()}{render_attachments(attachments or [])}"]
    if compaction and compaction.get("count"):
        parts.append(render_compaction(compaction))
        history = history[compaction["count"]:]
    for h in history:
        parts.append(f"[{DISPLAY[h['who']]} · {h['label']}]\n{h['content'].strip()}")
        if h.get("evidence"):
            parts.append(render_evidence(h))
        if h.get("actions") or h.get("denials") or (h.get("workspace") or {}).get("changed"):
            parts.append(render_actions(h))
    return "\n\n".join(parts)


def build_prompt(question: str, history: list[dict], stage: dict, plan_len: int,
                 prior: list[dict] | None = None, attachments: list[dict] | None = None,
                 issues: list[tuple[int, str]] | None = None, issue_src: dict | None = None,
                 compaction: dict | None = None) -> str:
    """issues/issue_src가 있으면(직전 검토의 [지적 N]) 끝에 '처리해야 할 지적' 블록을 붙인다.
    compaction이 있으면 대화 기록의 앞부분이 요약 블록으로 대체된다."""
    who, label = stage["who"], stage["label"]
    n = sum(1 for h in history if h["who"] != "user") + 1
    return (
        render_prior(prior or []) +
        f"=== 지금까지의 전체 대화 기록 ({len(history)}개 발언) ===\n"
        f"{render_transcript(question, history, attachments, compaction)}\n\n"
        f"=== 이번 단계 ({n}/{plan_len}): {DISPLAY[who]} · {label} ===\n"
        f"당신은 {DISPLAY[who]}입니다. {stage['instruction']}\n"
        f"'[{DISPLAY[who]} · {label}]' 같은 머리말은 붙이지 말고 본문만 쓰십시오."
        + (contract_block(issue_src, issues) if issues and issue_src else "")
    )


def render_compaction(c: dict) -> str:
    how = "Claude가 요약" if c.get("method") == "claude" else "앞부분만 잘라 붙임(요약 호출 실패)"
    return (f"[요약 · 이전 단계 {c['count']}개 ({c.get('labels', '')}) — 프로그램이 길이를 줄이려고 {how}. 원문은 아래 최근 단계만]\n"
            f"{(c.get('text') or '').strip()}")
