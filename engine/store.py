# -*- coding: utf-8 -*-
"""engine.store — 대화 저장/목록/불러오기/삭제, 제목·파일명, 마크다운 내보내기, final_of."""
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
from .compact import *  # noqa: F401,F403
from .runner import *  # noqa: F401,F403


# ----------------------------------------------------------------------------
# 대화 저장 (대화 1개 = 여러 라운드) — chats\<날짜_시각_주제>.json / .md
# ----------------------------------------------------------------------------
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def make_title(question: str, limit: int = 28) -> str:
    """질문 첫 줄에서 대화 제목을 만든다 (마크다운 기호 제거, 길이 제한)."""
    line = next((ln.strip() for ln in question.splitlines() if ln.strip()), "새 대화")
    line = re.sub(r"[#*`>\[\]_~]", "", line).strip() or "새 대화"
    return line if len(line) <= limit else line[:limit].rstrip() + "…"


def slugify(title: str, limit: int = 30) -> str:
    """파일명에 쓸 주제 슬러그 (한글 유지, Windows 금지 문자 제거)."""
    s = _ILLEGAL.sub("", title.replace("…", ""))
    s = re.sub(r"\s+", "_", s.strip())
    s = re.sub(r"_+", "_", s).strip("._")
    return (s[:limit].rstrip("._") or "chat")


def new_conversation(question: str) -> dict:
    now = datetime.now()
    title = make_title(question)
    return {"id": f"{now.strftime('%Y%m%d_%H%M')}_{slugify(title)}", "title": title,
            "created": now.isoformat(timespec="seconds"), "updated": now.isoformat(timespec="seconds"),
            "rounds": []}


def _round_lines(run: dict) -> list[str]:
    lines = ["## [사용자]", run["question"], ""]
    for a in run.get("attachments", []):
        lines.append(f"- 📎 {a['name']} ({a.get('chars', 0):,}자)" + (f" — {a['warning']}" if a.get("warning") else ""))
    if run.get("attachments"):
        lines.append("")
    for h in run.get("stages", []):
        head = f"## [{DISPLAY[h['who']]} · {h['label']}]"
        if h["who"] != "user":
            head += f"  ({h.get('elapsed', 0)}s)"
        lines += [head, h["content"], ""]
        if h.get("contract"):
            lines += [f"> 🧾 {contract_line(h['contract'])}", ""]
        if h.get("evidence"):
            lines += [f"> 🔬 {evidence_summary(h['evidence'])}"] + [f"> {ln}" for ln in render_evidence(h).splitlines()[1:]] + [""]
        if h.get("kind") == "evaluate":
            lines += [f"> 🧑‍⚖️ 독립 평가: {eval_verdict(h) or '(판정 형식 없음)'}", ""]
        if h.get("compacted"):
            lines += [f"> 🗜 이전 단계 {h['compacted']['count']}개를 요약해 전달 ({h['compacted']['method']})", ""]
    if usage_summary_line(run.get("stages", [])):
        lines += [f"> {usage_summary_line(run.get('stages', []))}", ""]
    if run.get("early_stopped"):
        lines += ["> ⏩ 검토 AI가 '추가 수정 불필요'로 판정해 남은 검토 단계를 건너뜀", ""]
    if run.get("status") == "stopped":
        lines += ["> ⏹ 사용자가 중단함", ""]
    if run.get("error"):
        lines += ["## ❌ 오류", "```", run["error"], "```", ""]
    return lines


def conversation_markdown(conv: dict) -> str:
    lines = [f"# {conv['title']}", f"_AI Debate Room · {conv['created']} ~ {conv['updated']}_", ""]
    for k, r in enumerate(conv["rounds"], start=1):
        lines += [f"# 라운드 {k}", ""] + _round_lines(r)
    return "\n".join(lines)


def save_conversation(conv: dict) -> tuple[Path, Path]:
    CHATS.mkdir(exist_ok=True)
    conv["updated"] = datetime.now().isoformat(timespec="seconds")
    jpath = CHATS / f"{conv['id']}.json"
    mpath = CHATS / f"{conv['id']}.md"
    jpath.write_text(json.dumps(conv, ensure_ascii=False, indent=2), encoding="utf-8")
    mpath.write_text(conversation_markdown(conv), encoding="utf-8")
    return jpath, mpath


def list_conversations() -> list[dict]:
    """저장된 대화 목록(최신순). 구 runs\\ 기록도 1라운드 대화로 포함(legacy)."""
    items = []
    if CHATS.exists():
        for p in CHATS.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                items.append({"id": d.get("id", p.stem), "title": d.get("title", p.stem), "updated": d.get("updated", ""),
                              "rounds": len(d.get("rounds", [])), "path": str(p), "legacy": False})
            except (ValueError, OSError):
                continue
    if RUNS.exists():
        for p in RUNS.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                items.append({"id": p.stem, "title": make_title(d.get("question", p.stem)), "updated": d.get("started", ""),
                              "rounds": 1, "path": str(p), "legacy": True})
            except (ValueError, OSError):
                continue
    items.sort(key=lambda x: x["updated"], reverse=True)
    return items


def load_conversation(path: str | Path) -> dict:
    p = Path(path)
    d = json.loads(p.read_text(encoding="utf-8"))
    if "rounds" in d:
        return d
    # 구 runs\ 형식(라운드 1개) → 대화로 감싼다
    d.setdefault("status", "error" if d.get("error") else "done")
    return {"id": p.stem, "title": make_title(d.get("question", p.stem)), "created": d.get("started", ""),
            "updated": d.get("finished") or d.get("started", ""), "rounds": [d], "legacy": True}


def delete_conversation(path: str | Path) -> None:
    p = Path(path)
    p.unlink(missing_ok=True)
    p.with_suffix(".md").unlink(missing_ok=True)


def final_of(run: dict) -> str | None:
    for s in reversed(run.get("stages", [])):
        if s.get("kind") == "final" or str(s.get("label", "")).startswith("FINAL"):
            return s["content"]
    return None
