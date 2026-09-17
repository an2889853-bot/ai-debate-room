# -*- coding: utf-8 -*-
"""engine.config — 경로·Config·모델 상수·CLIError·CLI 실행 파일 탐색·env 정리. 다른 모듈이 모두 이 위에 선다."""
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


ROOT = Path(__file__).resolve().parent.parent  # engine/ 의 상위 = 프로젝트 폴더
SANDBOX = ROOT / "sandbox"   # CLI 호출용 빈 작업 폴더 (코드 없음)
CHATS = ROOT / "chats"       # 대화 저장 폴더 (대화 1개 = json + md)
RUNS = ROOT / "runs"         # 구 버전 라운드 기록 (읽기만)

# winget이 설치한 Codex 실행 파일 (PATH의 codex.cmd 대신 exe를 직접 호출하기 위함)
CODEX_WINGET_EXE = (Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
                    / "OpenAI.Codex_Microsoft.Winget.Source_8wekyb3d8bbwe"
                    / "codex-x86_64-pc-windows-msvc.exe")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---- 도구 허용 범위 (하네스 방식: 끄는 게 아니라 범위를 정하고 기록한다) ----
WORKSPACES = ROOT / "workspace"        # 대화별 작업 폴더 (git 제외). 도구를 켠 라운드는 여기서 파일·명령을 다룬다
TOOL_ALLOW_CMDS = ["python", "python3", "python3.14", "py", "pytest", "pip", "git", "ls", "dir", "cat", "type", "echo", "mkdir"]
CLAUDE_FILE_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]
CLAUDE_WEB_TOOLS = ["WebSearch", "WebFetch"]
MAX_ACTION_OUTPUT = 1200               # 기록에 남기는 도구 결과 상한(글자)
WEB_SCOPES = {"initial_eval": "최초 답변 + 평가", "initial": "최초 답변만", "all": "전체 단계"}
# Codex 샌드박스(workspace-write)는 workspace 밖 실행 파일을 막아 venv 파이썬(uv 트램폴린 → uv 설치 폴더)이 실패했다(실기 2026-09-17).
# --add-dir 로 venv 와 기반 파이썬 폴더를 열어 주면 동작함을 확인. 쓰기 가능 폴더가 되므로 모델이 venv 를 건드릴 수 있다는 위험은 문서에 명시.
CODEX_ADD_DIRS = [d for d in dict.fromkeys([sys.prefix, sys.base_prefix]) if d and Path(d).exists()]


def web_allowed(scope: str, kind: str) -> bool:
    """이 단계에서 웹 검색을 쓰는가. 최초 답변의 검색 결과는 [프로그램 기록]으로 모든 단계에 남으므로 기본은 최초 + 평가만."""
    return {"all": True, "initial": kind == "initial", "initial_eval": kind in ("initial", "evaluate")}.get(scope, True)


# ----------------------------------------------------------------------------
# 설정 / 오류
# ----------------------------------------------------------------------------
CODEX_AUTO = "auto"   # Codex 모델 자동: 카탈로그(codex debug models) 최상위(priority 최소) 모델을 실행 시점에 고른다


@dataclass
class Config:
    claude_model: str | None = "fable"    # 별칭 'fable'은 항상 최신 Fable을 가리킨다(claude --help). None이면 settings.json 기본값
    claude_effort: str | None = "xhigh"   # low / medium / high / xhigh / max
    codex_model: str | None = CODEX_AUTO  # 'auto' | 카탈로그 slug | None(=~/.codex/config.toml 기본값)
    codex_effort: str | None = "xhigh"    # 모델이 지원하지 않으면 지원 범위 안에서 가장 가까운 아래 단계로 낮춘다
    timeout: int = 900                    # CLI 호출 1회당 최대 대기 시간(초)
    run_code: bool = False                # True면 답변의 python 코드 블록을 sandbox\_run에서 실제로 실행해 증거로 붙인다 (문법 검사는 항상)
    compact_chars: int = 60_000           # 대화 기록이 이 글자 수를 넘으면 오래된 단계를 요약해 전달 (0 = 끄기)
    web_search: bool = False              # True면 웹 검색·페이지 읽기 허용 (Claude WebSearch/WebFetch, Codex --search)
    tools: bool = False                   # True면 workspace 안에서 파일 읽기/쓰기와 허용 목록 명령 실행 허용 (행동은 기록에 남음)
    workspace: str = ""                   # tools일 때 CLI 작업 폴더 (비어 있으면 sandbox\)
    web_scope: str = "initial_eval"       # 웹 검색을 쓰는 단계: all(전체) | initial(최초 답변만) | initial_eval(최초 답변 + 평가)
    readonly: bool = False                # True면 도구는 읽기·실행·검색만 (Edit/Write 금지, Codex read-only) — 평가자 단계가 씀
    tool_budget: int = 8                  # 단계당 도구 호출 권고 상한 (지시문으로 전달 — CLI에 강제 옵션이 없음)
    eval_tool_budget: int = 5             # 평가자 단계의 권고 상한 (실측: 평가자가 12회 조회하면 2분·API 환산 $1 이상)
    claude_exe: str = ""
    codex_exe: str = ""


# Claude CLI가 받는 모델 별칭과 effort 단계 (claude --help 기준). 별칭은 각 계열의 최신 모델로 자동 해석된다.
CLAUDE_MODELS = ["fable", "opus", "sonnet"]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
EFFORT_ORDER = ["none", "low", "medium", "high", "xhigh", "max", "ultra"]

# `codex debug models` 조회에 실패했을 때 쓰는 내장 목록 (2026-09-10 카탈로그 기준)
CODEX_EFFORTS_ALL = ["low", "medium", "high", "xhigh", "max", "ultra"]
CODEX_MODELS_FALLBACK = [
    {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol", "default_effort": "low", "priority": 6,
     "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"]},
    {"slug": "gpt-5.6-terra", "display_name": "GPT-5.6-Terra", "default_effort": "medium", "priority": 7,
     "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"]},
    {"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "default_effort": "medium", "priority": 8,
     "efforts": ["low", "medium", "high", "xhigh", "max"]},
    {"slug": "gpt-5.5", "display_name": "GPT-5.5", "default_effort": "medium", "priority": 12,
     "efforts": ["low", "medium", "high", "xhigh"]},
]


class CLIError(Exception):
    """어느 CLI의 어느 단계에서 실패했는지 담는 예외."""

    def __init__(self, cli: str, stage: str, message: str, returncode: int | None = None,
                 stderr: str = "", timeout: bool = False, cancelled: bool = False):
        super().__init__(message)
        self.cli, self.stage, self.message = cli, stage, message
        self.returncode, self.stderr = returncode, stderr
        self.timeout, self.cancelled = timeout, cancelled

    def __str__(self) -> str:
        head = f"[{self.cli}] {self.stage}: {self.message}"
        if self.timeout:
            head += " (timeout)"
        if self.cancelled:
            return head
        if self.returncode is not None:
            head += f" (exit={self.returncode})"
        tail = self.stderr.strip()[-1200:]
        return head + (f"\n--- stderr (tail) ---\n{tail}" if tail else "")


def find_claude() -> str:
    p = shutil.which("claude")
    if p:
        return p
    p = Path.home() / ".local" / "bin" / "claude.exe"
    if p.exists():
        return str(p)
    raise FileNotFoundError("claude CLI를 찾을 수 없습니다. `irm https://claude.ai/install.ps1 | iex` 로 설치하세요.")


def find_codex() -> str:
    p = shutil.which("codex")
    if p and p.lower().endswith(".exe"):
        return p
    if CODEX_WINGET_EXE.exists():
        return str(CODEX_WINGET_EXE)
    if p:  # codex.cmd 래퍼라도 있으면 사용
        return p
    raise FileNotFoundError("codex CLI를 찾을 수 없습니다. `winget install --id OpenAI.Codex` 로 설치하세요.")


def clean_env(tools: bool = False) -> dict[str, str]:
    """Claude Code 세션 안에서 실행해도 중첩 세션 차단에 걸리지 않도록 관련 변수 제거.
    tools=True면 모델이 부르는 `python`이 스토어 스텁이 아니라 이 프로젝트 venv로 잡히도록 PATH 앞에 .venv/Scripts를 둔다."""
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if tools:
        venv = ROOT / ".venv" / "Scripts"
        if venv.exists():
            env["PATH"] = str(venv) + os.pathsep + env.get("PATH", "")
    return env


OTHER = {"claude": "gpt", "gpt": "claude"}

DISPLAY = {"claude": "Claude", "gpt": "GPT", "user": "사용자"}


# ---- 도구 허용 시 작업 폴더 (대화별 workspace, git으로 변경 추적) ----
def cwd_for(cfg: Config) -> Path:
    """CLI 작업 폴더: 도구가 켜져 있고 workspace가 있으면 그곳, 아니면 빈 sandbox 폴더."""
    return Path(cfg.workspace) if getattr(cfg, "tools", False) and getattr(cfg, "workspace", "") else SANDBOX


def git_exe() -> str | None:
    return shutil.which("git")


def _git(ws: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([git_exe(), "-c", "user.name=AI Debate Room", "-c", "user.email=debate@local", *args],
                          cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=CREATE_NO_WINDOW)


def new_workspace(question: str = "") -> Path:
    """대화별 작업 폴더를 만들고 git init (git이 있으면). 라운드 시작 시 한 번 — 같은 대화의 다음 라운드는 재사용."""
    slug = re.sub(r"[^\w가-힣-]+", "_", (question or "").strip()[:20]).strip("_") or "ws"
    ws = WORKSPACES / f"{datetime.now():%Y%m%d_%H%M%S}_{slug}"
    ws.mkdir(parents=True, exist_ok=True)
    if git_exe() and not (ws / ".git").exists():
        subprocess.run([git_exe(), "init", "-q"], cwd=ws, capture_output=True, creationflags=CREATE_NO_WINDOW)
    return ws


def workspace_commit(ws: Path, message: str) -> tuple[list[str], str | None]:
    """단계가 끝난 뒤 작업 폴더의 변경을 커밋해 증거로 남긴다. (바뀐 파일 목록, 짧은 해시). git이 없거나 변경이 없으면 ([], None)."""
    ws = Path(ws)
    if not git_exe() or not (ws / ".git").exists():
        return [], None
    status = _git(ws, "status", "--porcelain")
    changed = [ln[3:].strip() for ln in status.stdout.splitlines() if ln.strip()]
    if not changed:
        return [], None
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", message)
    rev = _git(ws, "rev-parse", "--short", "HEAD").stdout.strip() or None
    return changed, rev
