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


def clean_env() -> dict[str, str]:
    """Claude Code 세션 안에서 실행해도 중첩 세션 차단에 걸리지 않도록 관련 변수 제거."""
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


OTHER = {"claude": "gpt", "gpt": "claude"}

DISPLAY = {"claude": "Claude", "gpt": "GPT", "user": "사용자"}
