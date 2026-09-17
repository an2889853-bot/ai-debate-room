# -*- coding: utf-8 -*-
"""engine.evidence — 코드 블록 검사(외부 증거): python 문법·json/toml 파싱은 항상, 실행은 옵션."""
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


# ---- 코드 블록 검사 (외부 증거) ----
# 답변 속 ```python 블록은 항상 문법 검사, json/toml은 파싱 검사. cfg.run_code가 켜져 있으면 python 블록을 sandbox\_run에서
# 실제로 실행해 exit 코드·출력을 잡는다. 결과는 entry["evidence"]에 남고 다음 단계 프롬프트에 '[프로그램 검사 ...]' 블록으로 들어간다
# — 모델이 "동작한다"고 말하는 것과 별개로 프로그램이 확인한 사실을 토론에 넣기 위해서다.
CODE_FENCE_RE = re.compile(r"```([\w+.#-]*)[^\n]*\n(.*?)```", re.S)
CODE_CHECK_LANGS = {"python": "python", "py": "python", "python3": "python", "json": "json", "toml": "toml"}
RUN_DIR = SANDBOX / "_run"        # 실행용 임시 폴더 (git 제외, 실행 후 삭제)
CODE_RUN_TIMEOUT = 30             # 블록 1개 실행 제한(초)
MAX_EVIDENCE_OUTPUT = 1500        # 프롬프트에 넣는 실행 출력 상한(글자)


def extract_code_blocks(text: str) -> list[dict]:
    """``` 펜스 블록을 [{"lang", "code"}] 로 (등장 순서, 언어 태그는 소문자)."""
    return [{"lang": (m.group(1) or "").strip().lower(), "code": m.group(2)} for m in CODE_FENCE_RE.finditer(text or "")]


def _run_python(code: str, timeout: int) -> tuple[bool, str]:
    """python 블록을 격리 폴더에서 실행. (성공 여부, 'exit N' + 출력 꼬리)."""
    run_dir = RUN_DIR / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    script = run_dir / "block.py"
    script.write_text(code, encoding="utf-8")
    try:
        r = subprocess.run([sys.executable, "-I", "-X", "utf8", str(script)], cwd=run_dir, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env=clean_env(), creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, f"실행 {timeout}초 초과 (무한 루프나 입력 대기?)"
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    out = r.stdout.strip()
    if r.stderr.strip():
        out = (out + "\n[stderr]\n" + r.stderr.strip()).strip()
    return r.returncode == 0, f"exit {r.returncode}" + (f"\n{out[-MAX_EVIDENCE_OUTPUT:]}" if out else " (출력 없음)")


def check_code_blocks(text: str, run: bool = False, timeout: int = CODE_RUN_TIMEOUT) -> list[dict]:
    """검사 가능한 블록만 [{"index", "lang", "lines", "check": syntax|run|parse, "ok", "detail"}]. index는 전체 펜스 순번."""
    out: list[dict] = []
    for i, b in enumerate(extract_code_blocks(text), start=1):
        kind = CODE_CHECK_LANGS.get(b["lang"])
        if not kind:
            continue
        code = b["code"]
        item = {"index": i, "lang": kind, "lines": len(code.strip("\n").splitlines()), "check": "parse", "ok": True, "detail": ""}
        try:
            if kind == "python":
                ast.parse(code)
                item.update(check="syntax", detail="문법 OK")
                if run:
                    ok, detail = _run_python(code, timeout)
                    item.update(check="run", ok=ok, detail=detail)
            elif kind == "json":
                json.loads(code)
                item["detail"] = "JSON 파싱 OK"
            else:
                tomllib.loads(code)
                item["detail"] = "TOML 파싱 OK"
        except SyntaxError as e:
            item.update(check="syntax", ok=False, detail=f"SyntaxError: {e.msg} (줄 {e.lineno})")
        except (ValueError, tomllib.TOMLDecodeError) as e:
            item.update(ok=False, detail=f"{type(e).__name__}: {str(e)[:300]}")
        out.append(item)
    return out


def render_evidence(h: dict) -> str:
    """대화 기록에 들어가는 검사 결과 블록 (모델의 말과 구분되도록 별도 머리말)."""
    lines = [f"[프로그램 검사 · {DISPLAY[h['who']]} · {h['label']}의 코드 블록 — 사람이 아니라 프로그램이 실제로 검사한 결과]"]
    for b in h.get("evidence", []):
        lines.append(f"- 블록 {b['index']} ({b['lang']}, {b['lines']}줄) {b['check']}: {'OK' if b['ok'] else '실패'} — {b['detail']}")
    return "\n".join(lines)


def evidence_summary(ev: list[dict]) -> str:
    ok = sum(1 for b in ev if b["ok"])
    ran = sum(1 for b in ev if b["check"] == "run")
    return f"코드 검사 {len(ev)}개 블록 — OK {ok} · 실패 {len(ev) - ok}" + (f" (실행 {ran}개)" if ran else " (문법·파싱만)")
