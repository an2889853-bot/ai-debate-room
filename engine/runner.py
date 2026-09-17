# -*- coding: utf-8 -*-
"""engine.runner — 단계 실행(execute_stage: 계약 검사·재요청·코드 검사·압축)과 콘솔용 순차 실행(run_debate)."""
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


# ----------------------------------------------------------------------------
# 단계 실행
# ----------------------------------------------------------------------------
def execute_stage(question: str, attachments: list[dict], history: list[dict], stage: dict,
                  cfg: Config, prior: list[dict] | None = None, mode: str = "general", plan_len: int = 5,
                  on_delta: DeltaCB | None = None, on_tick: TickCB | None = None,
                  cancel: threading.Event | None = None, max_retries: int = MAX_CONTRACT_RETRIES,
                  compaction: dict | None = None, on_action: Callable[[list[dict]], None] | None = None) -> dict:
    """단계 하나를 실행해 기록 항목을 반환. 실패 시 CLIError.
    반박/최종 단계는 직전 검토의 [지적 N]마다 [반영/반박 N]이 있어야 하며, 빠지면 max_retries회까지 재요청한다.
    검사 결과는 entry["contract"] = {"issues", "resolved", "missing", "retries", "source"} (검사할 지적이 없으면 키 없음)."""
    stage_name = f"{DISPLAY[stage['who']]} · {stage['label']}"
    src, issues = open_issues(history) if stage["kind"] in RESPOND_KINDS else (None, [])
    comp = compact_history(question, history, attachments, cfg, compaction)  # 기록이 길면 앞부분을 요약(라운드 캐시)
    prompt = build_prompt(question, history, stage, plan_len, prior, attachments, issues, src, comp)
    # 이 단계의 도구 범위: 웹은 web_scope에 따라(최초 답변의 검색 결과는 기록으로 남아 뒤 단계가 재사용), 평가자는 읽기 전용
    web_on = bool(cfg.web_search) and web_allowed(cfg.web_scope, stage["kind"])
    stage_cfg = dataclasses.replace(cfg, web_search=web_on, readonly=bool(cfg.readonly) or stage["kind"] == "evaluate")
    rules = system_rules(mode, cfg.tools, web_on, web_reuse=bool(cfg.web_search) and not web_on)
    t0 = time.time()
    images = image_attachments(attachments)  # 매 호출이 독립 세션이므로 이미지도 매 단계 다시 전달
    contract: dict | None = None
    retries = 0
    while True:
        if stage["who"] == "claude":
            content, meta = call_claude(stage_cfg, rules, prompt, stage_name, on_delta, on_tick, cancel, images, on_action)
        else:
            content, meta = call_codex(stage_cfg, rules + "\n" + prompt, stage_name, on_tick, cancel, images, on_action)
        if not issues:
            break
        contract = check_contract(issues, content)
        if not contract["missing"] or retries >= max_retries:
            break
        retries += 1
        prompt += retry_note(contract["missing"])  # 같은 단계를 다시: 빠진 번호를 명시해 재요청
    entry = {"who": stage["who"], "label": stage["label"], "kind": stage["kind"], "content": content,
             "elapsed": round(time.time() - t0, 1), "meta": meta, "prompt_chars": len(prompt)}
    if contract is not None:
        contract.update({"retries": retries, "source": f"{DISPLAY[src['who']]} · {src['label']}"})
        entry["contract"] = contract
    evidence = check_code_blocks(content, run=cfg.run_code)  # 코드 블록이 있으면 검사해 다음 단계의 증거로
    if evidence:
        entry["evidence"] = evidence
    if comp:
        entry["compacted"] = {"count": comp["count"], "method": comp["method"]}
    # 도구 사용 기록: 다음 단계와 저장 파일에 남긴다 (meta에서 꺼내 entry 최상위로)
    actions = meta.pop("actions", None) if isinstance(meta, dict) else None
    denials = meta.pop("denials", None) if isinstance(meta, dict) else None
    if actions:
        entry["actions"] = actions
    if denials:
        entry["denials"] = denials
    if cfg.tools and cfg.workspace:
        changed, rev = workspace_commit(Path(cfg.workspace), f"{DISPLAY[stage['who']]} · {stage['label']}")
        if changed:
            entry["workspace"] = {"changed": changed, "commit": rev}
    return entry






def interjection_entry(text: str) -> dict:
    return {"who": "user", "label": "개입", "kind": "interjection", "content": text.strip(),
            "elapsed": 0, "meta": {}, "prompt_chars": 0}


EventCB = Callable[[dict], None]


def run_debate(question: str, cfg: Config, max_stage: int | None = None,
               on_event: EventCB | None = None, prior: list[dict] | None = None,
               attachments: list[dict] | None = None, rounds: int = 1, mode: str = "general",
               early_stop: bool = True, first: str = "claude", final_who: str | None = None,
               custom: list | None = None, stage_count: int = DEFAULT_STAGE_COUNT,
               evaluate: bool = False, eval_revise: bool = True) -> dict:
    """콘솔용: 계획된 단계를 순차 실행. 각 단계 시작/완료/오류/건너뜀/재작성을 on_event로 알린다."""
    emit = on_event or (lambda e: None)
    plan = plan_stages(rounds, mode, first, final_who, custom, stage_count, evaluate)
    if max_stage is not None:
        plan = plan[:max(1, max_stage)]
    history: list[dict] = []
    compaction: dict = {}
    if cfg.tools and not cfg.workspace:  # 도구를 켰으면 라운드용 작업 폴더
        cfg = dataclasses.replace(cfg, workspace=str(new_workspace(question)))
    run = {"question": question, "attachments": attachments or [], "config": asdict(cfg),
           "workspace": cfg.workspace or None,
           "mode": mode, "rounds": rounds, "stage_count": stage_count,
           "plan": [s["label"] for s in plan], "stages": history,
           "error": None, "status": "running", "early_stopped": False, "prior_rounds": len(prior or []),
           "started": datetime.now().isoformat(timespec="seconds")}
    i = 0
    while i < len(plan):
        stage = plan[i]
        emit({"type": "start", "index": i + 1, "total": len(plan), "who": stage["who"], "label": stage["label"]})
        try:
            entry = execute_stage(question, attachments or [], history, stage, cfg, prior, mode, len(plan),
                                  compaction=compaction)
        except (CLIError, FileNotFoundError, OSError) as e:
            run["error"], run["status"] = str(e), "error"
            emit({"type": "error", "index": i + 1, "total": len(plan), "who": stage["who"],
                  "label": stage["label"], "error": str(e)})
            break
        history.append(entry)
        emit({"type": "done", "index": i + 1, "total": len(plan), **entry})
        i += 1
        plan, ev = adjust_plan_after(plan, i, entry, early_stop, eval_revise)
        if ev:
            if ev["type"] == "skip":
                run["early_stopped"] = True
            emit(ev)
            run["plan"] = [s["label"] for s in plan]
    else:
        run["status"] = "done"
    run["contract"] = contract_summary(history)
    run["evaluation"] = eval_summary(history)
    run["usage"] = usage_summary(history)
    run["compaction"] = compaction or None
    run["finished"] = datetime.now().isoformat(timespec="seconds")
    return run
