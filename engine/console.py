# -*- coding: utf-8 -*-
"""engine.console — 콘솔 출력·CLI 점검·argparse main."""
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
import argparse
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
from .store import *  # noqa: F401,F403


# ----------------------------------------------------------------------------
# 콘솔 UI
# ----------------------------------------------------------------------------
def console_event(e: dict) -> None:
    if e["type"] == "skip":
        print(f"\n⏩ 검토 판정 '추가 수정 불필요' → 건너뜀: {', '.join(e['skipped'])}", flush=True)
        return
    if e["type"] == "revise":
        print(f"\n🔁 독립 평가 NEEDS_WORK → 추가: {', '.join(e['added'])}", flush=True)
        return
    name = f"{DISPLAY[e['who']]} · {e['label']}"
    if e["type"] == "start":
        print(f"\n▶ [{e['index']}/{e['total']}] {name} 응답 생성 중...", flush=True)
    elif e["type"] == "done":
        bar = "=" * 72
        ul = usage_line(e)
        print(f"{bar}\n[{name}]  ({e['elapsed']}s, 입력 {e['prompt_chars']}자{', ' + ul if ul else ''})\n{bar}\n{e['content']}\n", flush=True)
        if e.get("contract"):
            print(f"🧾 {contract_line(e['contract'])}\n", flush=True)
        if e.get("evidence"):
            print(f"🔬 {evidence_summary(e['evidence'])}\n{render_evidence(e)}\n", flush=True)
        if e.get("kind") == "evaluate":
            print(f"🧑‍⚖️ 독립 평가: {eval_verdict(e) or '(판정 형식 없음)'}\n", flush=True)
        if e.get("compacted"):
            print(f"🗜 이전 단계 {e['compacted']['count']}개를 요약해 전달 ({e['compacted']['method']})\n", flush=True)
        if e.get("actions") or e.get("denials") or (e.get("workspace") or {}).get("changed"):
            print(f"🛠 {actions_summary(e.get('actions') or [], e.get('denials'))}\n{render_actions(e)}\n", flush=True)
    elif e["type"] == "error":
        cli = "claude" if e["who"] == "claude" else "codex"
        print(f"\n❌ 오류 — 단계 {e['index']}/{e['total']} {name} ({cli} CLI)\n{e['error']}\n", flush=True)


def check_clis(cfg: Config) -> bool:
    ok = True
    for cli, fn in (("claude", lambda: call_claude(cfg, "간단히 답하십시오.", "Reply with exactly the word OK.", "check")),
                    ("codex", lambda: call_codex(cfg, "Reply with exactly the word OK and nothing else.", "check"))):
        exe = cfg.claude_exe if cli == "claude" else cfg.codex_exe
        t0 = time.time()
        try:
            text, meta = fn()
            used = ""
            if cli == "codex" and meta.get("model"):
                used = f"  [model={meta['model']}, effort={meta.get('reasoning_effort')}]"
                if meta.get("resolve_note"):
                    used += f"  ({meta['resolve_note']})"
            elif cli == "claude" and meta.get("models"):
                used = f"  [models={','.join(meta['models'])}]"
            print(f"✅ {cli}: {text!r}  ({time.time() - t0:.1f}s){used}  <- {exe}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"❌ {cli}: {e}\n   <- {exe}")
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AI Debate Room (Claude CLI + Codex CLI)")
    ap.add_argument("question", nargs="?", help="사용자 질문 (생략하면 입력받음)")
    ap.add_argument("-q", "--question", dest="q", help="사용자 질문")
    ap.add_argument("--file", action="append", default=[], help="첨부 파일 경로 (여러 번 지정 가능)")
    ap.add_argument("--mode", choices=list(MODES), default="general", help="모드 프리셋")
    ap.add_argument("--stages", dest="stage_count", type=int, choices=STAGE_COUNTS, default=DEFAULT_STAGE_COUNT,
                    help="토론 단계 수: 3=최초·검토·최종(기본), 5=최초·검토·반박·재검사·최종")
    ap.add_argument("--rounds", type=int, default=1, help="검토↔반박 라운드 수 (5단계일 때만, 기본 1)")
    ap.add_argument("--first", choices=["claude", "gpt"], default="claude", help="먼저 답하는 AI (기본 claude)")
    ap.add_argument("--final-who", choices=["claude", "gpt"], default=None, help="최종 정리 AI (기본: 먼저 답한 AI)")
    ap.add_argument("--plan", default=None,
                    help="순서 직접 지정, 예: claude:initial,gpt:review,claude:rebuttal,gpt:recheck,claude:final")
    ap.add_argument("--no-early-stop", action="store_true", help="검토 AI가 '추가 수정 불필요'여도 끝까지 진행")
    ap.add_argument("--no-evaluate", action="store_true", help="FINAL 뒤 독립 평가(상대 AI의 PASS/NEEDS_WORK 채점)를 생략")
    ap.add_argument("--no-eval-revise", action="store_true", help="평가가 NEEDS_WORK여도 FINAL을 다시 쓰지 않음")
    ap.add_argument("--max-stage", type=int, default=None, help="앞의 N단계만 실행 (테스트용)")
    ap.add_argument("--claude-model", default=Config.claude_model, help="fable/opus/sonnet 별칭 또는 전체 모델명 (기본 fable=최신 Fable)")
    ap.add_argument("--claude-effort", default=Config.claude_effort, help="low/medium/high/xhigh/max (기본 xhigh)")
    ap.add_argument("--codex-model", default=Config.codex_model, help="auto(카탈로그 최상위, 기본) 또는 slug")
    ap.add_argument("--codex-effort", default=Config.codex_effort, help="low/medium/high/xhigh/max/ultra (기본 xhigh, 모델 지원 범위로 자동 조정)")
    ap.add_argument("--timeout", type=int, default=Config.timeout, help="CLI 호출 1회 타임아웃(초)")
    ap.add_argument("--run-code", action="store_true", help="답변의 python 코드 블록을 sandbox\\_run에서 실제로 실행해 증거로 붙임 (문법 검사는 항상)")
    ap.add_argument("--compact-chars", type=int, default=Config.compact_chars,
                    help="대화 기록이 이 글자 수를 넘으면 오래된 단계를 요약해 전달 (기본 60000, 0=끄기)")
    ap.add_argument("--no-web", action="store_true", help="웹 검색·페이지 읽기 끄기 (기본 켜짐: Claude WebSearch/WebFetch, Codex --search)")
    ap.add_argument("--no-tools", action="store_true",
                    help="파일·명령 도구 끄기 (기본 켜짐: 라운드용 workspace\\ 안에서 읽기/쓰기와 허용 목록 명령만, 행동은 기록에 남고 단계마다 git 커밋)")
    ap.add_argument("--check", action="store_true", help="두 CLI가 응답하는지만 확인")
    ap.add_argument("--list-codex-models", action="store_true", help="선택 가능한 Codex 모델과 effort 출력")
    ap.add_argument("--stats", action="store_true", help="chats\\ 저장 대화 전체 통계 (단계 시간, 토큰, 반영 계약, 평가)")
    ap.add_argument("--no-save", action="store_true", help="chats\\ 에 저장하지 않음")
    a = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 리다이렉트 시 한글 깨짐 방지
    except Exception:  # noqa: BLE001
        pass
    SANDBOX.mkdir(exist_ok=True)

    cfg = Config(claude_model=a.claude_model or None, claude_effort=a.claude_effort or None,
                 codex_model=a.codex_model or None, codex_effort=a.codex_effort or None, timeout=a.timeout,
                 run_code=a.run_code, compact_chars=a.compact_chars, web_search=not a.no_web, tools=not a.no_tools)
    try:
        cfg.claude_exe = find_claude()
        cfg.codex_exe = find_codex()
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return 2

    if a.stats:
        convs = []
        for c in list_conversations():
            try:
                convs.append(load_conversation(c["path"]))
            except (OSError, ValueError):
                pass
        print("\n".join(stats_lines(stats(convs))))
        return 0
    if a.list_codex_models:
        for m in list_codex_models(cfg.codex_exe):
            print(f"{m['slug']:16} {m['display_name']:16} 기본 effort={m['default_effort']:8} 지원={', '.join(m['efforts'])}")
        return 0
    if a.check:
        return 0 if check_clis(cfg) else 1

    question = (a.q or a.question or "").strip()
    if not question:
        try:
            question = input("질문을 입력하세요: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
        if not question:
            print("질문이 비어 있습니다.")
            return 1

    attachments = []
    for f in a.file:
        p = Path(f)
        if not p.is_file():
            print(f"❌ 첨부 파일을 찾을 수 없음: {f}")
            return 2
        attachments.extend(load_attachments(p.name, p.read_bytes()))

    print(f"\n[사용자]\n{question}\n")
    for at in attachments:
        print(f"📎 {at['name']} ({at['chars']:,}자)" + (f" — ⚠ {at['warning']}" if at["warning"] else ""))
    custom = None
    if a.plan:
        try:
            custom = [tuple(p.strip().lower().split(":")) for p in a.plan.split(",") if p.strip()]
            plan_stages(1, a.mode, custom=custom)
        except (ValueError, TypeError) as e:
            print(f"❌ --plan 형식 오류: {e}")
            return 2
    cm, ce, cnote = resolve_codex(cfg)
    print(f"(모드 {MODES[a.mode]['name']}, 순서 {plan_preview(plan_stages(a.rounds, a.mode, a.first, a.final_who, custom, a.stage_count, not a.no_evaluate))}\n"
          f" claude: {cfg.claude_model or '기본'}/{cfg.claude_effort or '기본 effort'}, "
          f"codex: {cm or '기본'}/{ce or '기본 effort'}{' [' + cnote + ']' if cnote else ''}, timeout {cfg.timeout}s)")
    run = run_debate(question, cfg, max_stage=a.max_stage, on_event=console_event, attachments=attachments,
                     rounds=a.rounds, stage_count=a.stage_count, mode=a.mode, early_stop=not a.no_early_stop,
                     evaluate=not a.no_evaluate, eval_revise=not a.no_eval_revise,
                     first=a.first, final_who=a.final_who, custom=custom)
    if not a.no_save:
        conv = new_conversation(question)
        conv["rounds"].append(run)
        _, mpath = save_conversation(conv)
        print(f"저장: {mpath}")
    return 1 if run["error"] else 0
