# -*- coding: utf-8 -*-
"""engine.compact — 컨텍스트 압축: 긴 기록의 앞부분을 Claude가 요약(라운드 캐시), 실패 시 기계적 요약."""
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
from .prompt import *  # noqa: F401,F403


# ---- 컨텍스트 압축 (긴 토론 요약) ----
# 전체 대화 기록이 cfg.compact_chars를 넘으면 마지막 COMPACT_KEEP_LAST개 항목만 원문으로 두고 그 앞은 Claude에게 요약시켜
# '[요약 · 이전 단계 n개]' 블록으로 대신 넣는다. 요약은 라운드 안에서 캐시(compaction dict, 요약 대상 내용의 해시로 키)돼
# 단계마다 다시 만들지 않는다. 요약 호출이 실패하면 각 항목 앞부분을 잘라 붙이는 기계적 요약으로 대체해 토론이 멈추지 않게 한다.
COMPACT_KEEP_LAST = 2
COMPACT_FALLBACK_CHARS = 800      # 기계적 요약: 항목당 앞부분 글자 수
SUMMARY_RULES = (
    "당신은 AI 토론 기록의 요약자입니다. 도구를 쓰지 말고 텍스트로만 답하십시오. 아래 단계들을 다음 단계 참가자가 맥락을 잃지 않을 만큼 "
    "요약하십시오: 각 단계마다 '[Claude · Review]' 같은 머리말을 유지하고, 핵심 주장·수정 내용과 [지적 N]/[반영 N]/[반박 N]/[판정: ...]/"
    "[평가: ...] 줄, '[프로그램 검사 ...]' 결과는 번호와 결론을 그대로 남기고, '[프로그램 기록 ...]'의 명령·결과·검색 결과(출처 URL)도 "
    "핵심만 남기십시오. 전체 3,000자 이내. 새로운 의견을 덧붙이지 마십시오.")


def compaction_needed(question: str, history: list[dict], attachments: list[dict] | None, limit: int) -> int:
    """요약할 앞쪽 항목 수 (0이면 불필요). limit<=0이면 끔. 마지막 COMPACT_KEEP_LAST개는 항상 원문."""
    if limit <= 0 or len(history) <= COMPACT_KEEP_LAST:
        return 0
    if len(render_transcript(question, history, attachments)) <= limit:
        return 0
    return len(history) - COMPACT_KEEP_LAST


def fallback_summary(entries: list[dict]) -> str:
    """요약 호출이 실패했을 때: 각 항목 앞부분만 잘라 붙인다."""
    parts = []
    for h in entries:
        body = (h.get("content") or "").strip()
        if len(body) > COMPACT_FALLBACK_CHARS:
            body = body[:COMPACT_FALLBACK_CHARS].rstrip() + " …(잘림)"
        parts.append(f"[{DISPLAY[h['who']]} · {h['label']}]\n{body}")
    return "\n\n".join(parts)


def summarize_entries(entries: list[dict], cfg: Config) -> tuple[str, str]:
    """(요약문, 방식 'claude'|'fallback'). Claude 호출이 실패하면 기계적 요약."""
    text = "\n\n".join(f"[{DISPLAY[h['who']]} · {h['label']}]\n{(h.get('content') or '').strip()}"
                        + ("\n" + render_evidence(h) if h.get("evidence") else "")
                        + ("\n" + render_actions(h) if (h.get("actions") or h.get("denials") or (h.get("workspace") or {}).get("changed")) else "")
                        for h in entries)
    try:
        # 요약엔 깊은 추론이 필요 없다 → effort를 낮춰 빠르고 싸게 (모델은 설정 그대로)
        # 요약자는 도구·웹 없이 (요약만 하면 되고, 도구가 열리면 요약 중에 명령을 돌릴 수 있다)
        summary, _ = call_claude(dataclasses.replace(cfg, claude_effort="low", tools=False, web_search=False), SUMMARY_RULES,
                                 "=== 요약할 단계들 ===\n" + text, "요약")
        if summary.strip():
            return summary.strip(), "claude"
    except (CLIError, FileNotFoundError, OSError):
        pass
    return fallback_summary(entries), "fallback"


def compact_history(question: str, history: list[dict], attachments: list[dict] | None, cfg: Config,
                    cache: dict | None) -> dict | None:
    """필요하면 요약을 만들거나 캐시에서 꺼낸다. cache=None이면 압축 안 함(테스트·구 호출 호환).
    반환 {"count", "key", "text", "method", "labels"} 또는 None."""
    if cache is None:
        return None
    n = compaction_needed(question, history, attachments, int(getattr(cfg, "compact_chars", 0) or 0))
    if n <= 0:
        return None
    key = f"{n}:{hash(tuple((h.get('who'), h.get('label'), h.get('content', '')) for h in history[:n]))}"
    if cache.get("key") == key and cache.get("text"):
        return cache
    entries = history[:n]
    text, method = summarize_entries(entries, cfg)
    cache.clear()
    cache.update({"count": n, "key": key, "text": text, "method": method,
                  "labels": f"{DISPLAY[entries[0]['who']]}·{entries[0]['label']} ~ {DISPLAY[entries[-1]['who']]}·{entries[-1]['label']}"})
    return cache
