# -*- coding: utf-8 -*-
"""engine.cli — CLI 호출: 공통 실행기 _popen_stream, call_claude/call_codex, Codex 모델 카탈로그·해석, 로그인 상태·로그아웃·LoginFlow."""
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


# ----------------------------------------------------------------------------
# 프로세스 실행 공통: stdin으로 프롬프트, stdout 라인 스트리밍, 타임아웃, 취소
# ----------------------------------------------------------------------------
TickCB = Callable[[float], None]


def _popen_stream(cmd: list[str], prompt: str, timeout: int,
                  on_line: Callable[[str], None] | None = None,
                  on_tick: TickCB | None = None,
                  cancel: threading.Event | None = None, cwd: Path | None = None,
                  env: dict | None = None) -> tuple[int, str, str, str | None]:
    """반환 (returncode, stdout, stderr, killed_reason). killed_reason: None | "timeout" | "cancelled".
    cwd/env를 주지 않으면 빈 sandbox 폴더와 clean_env()."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=cwd or SANDBOX, env=env or clean_env(), text=True, encoding="utf-8", errors="replace",
                            bufsize=1, creationflags=CREATE_NO_WINDOW)
    out_q: queue.Queue[str | None] = queue.Queue()
    out_lines: list[str] = []
    err_lines: list[str] = []

    def feed() -> None:  # 큰 프롬프트를 stdin에 쓰는 동안 stdout이 막히지 않도록 별도 스레드
        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    def pump_out() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            out_q.put(line)
        out_q.put(None)

    def pump_err() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            err_lines.append(line)

    threads = [threading.Thread(target=f, daemon=True) for f in (feed, pump_out, pump_err)]
    for t in threads:
        t.start()

    t0 = time.time()
    deadline = t0 + timeout
    killed: str | None = None
    eof = False
    try:
        while not eof:
            try:
                item = out_q.get(timeout=0.25)
                if item is None:
                    eof = True
                else:
                    out_lines.append(item)
                    if on_line:
                        on_line(item)
            except queue.Empty:
                pass
            if on_tick:
                on_tick(time.time() - t0)
            if cancel is not None and cancel.is_set():
                killed = "cancelled"
                break
            if time.time() > deadline:
                killed = "timeout"
                break
        if killed:
            proc.kill()
        rc = proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    for t in threads:
        t.join(timeout=2)
    return rc, "".join(out_lines), "".join(err_lines), killed


# ----------------------------------------------------------------------------
# CLI 호출 (stateless: 매 호출이 독립된 새 세션)
# ----------------------------------------------------------------------------
DeltaCB = Callable[[str], None]


# ---- 도구 허용 인자와 행동 기록 파서 ----
def claude_tool_args(cfg: Config) -> list[str]:
    """도구가 꺼져 있으면 --tools "" (전부 비활성). 켜져 있으면 허용 목록만: -p 모드는 승인을 물을 수 없으므로
    --allowedTools에 없는 명령은 자동 거부되고, 그 거부가 곧 경계다. --dangerously-skip-permissions는 쓰지 않는다."""
    if not (cfg.tools or cfg.web_search):
        return ["--tools", ""]
    tools: list[str] = []
    allowed: list[str] = []
    if cfg.tools:
        tools += CLAUDE_FILE_TOOLS
        allowed += [f"Bash({c} *)" for c in TOOL_ALLOW_CMDS] + ["Read", "Glob", "Grep", "Edit", "Write"]
    if cfg.web_search:
        tools += CLAUDE_WEB_TOOLS
        allowed += CLAUDE_WEB_TOOLS
    args = ["--tools", ",".join(tools), "--allowedTools", " ".join(allowed)]
    if cfg.tools:
        args += ["--permission-mode", "acceptEdits"]  # 파일 편집은 자동 허용, 그 외는 허용 목록으로
    return args


def codex_tool_args(cfg: Config) -> list[str]:
    """샌드박스는 tools면 workspace-write, 아니면 read-only (danger-full-access는 쓰지 않음).
    웹은 설정 키 `-c web_search=live` — `--search`는 대화형 CLI 옵션이라 `codex exec`가 받지 않는다(실기 확인 2026-09-17:
    "unexpected argument '--search'"). 키를 모르는 버전이면 --strict-config가 아니라서 무시되고 검색만 안 된다(실패하지 않음).
    행동을 기록하려면 이벤트가 필요하므로 도구가 켜져 있을 때만 --json."""
    args = ["--sandbox", "workspace-write" if cfg.tools else "read-only", "-C", str(cwd_for(cfg))]
    if cfg.web_search:
        args += ["-c", "web_search=live"]
    if cfg.tools or cfg.web_search:
        args.append("--json")
    return args


def _tool_input_text(name: str | None, inp) -> str:
    inp = inp if isinstance(inp, dict) else {}
    for k in ("command", "file_path", "query", "url", "pattern", "path", "prompt"):
        if inp.get(k):
            return str(inp[k])[:300]
    return json.dumps(inp, ensure_ascii=False)[:300]


def _tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content)
    return json.dumps(content, ensure_ascii=False) if content is not None else ""


class ClaudeEventParser:
    """claude -p stream-json 이벤트에서 도구 사용(tool_use)과 결과(tool_result)를 모아 actions 목록으로.
    각 항목 {"tool", "input", "output", "error"}. 같은 tool_use id가 반복돼도 한 번만 센다."""

    def __init__(self) -> None:
        self.actions: list[dict] = []
        self._pending: dict[str, dict] = {}
        self._seen: set[str] = set()

    def feed(self, obj: dict) -> None:
        t = obj.get("type")
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            return
        if t == "assistant":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tid = str(b.get("id") or "")
                    if tid and tid in self._seen:
                        continue
                    a = {"tool": str(b.get("name") or "?"), "input": _tool_input_text(b.get("name"), b.get("input")),
                         "output": "", "error": False}
                    self.actions.append(a)
                    if tid:
                        self._seen.add(tid)
                        self._pending[tid] = a
        elif t == "user":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    a = self._pending.pop(str(b.get("tool_use_id") or ""), None)
                    if a is not None:
                        a["output"] = _tool_result_text(b.get("content"))[:MAX_ACTION_OUTPUT]
                        a["error"] = bool(b.get("is_error"))


def parse_codex_events(stdout: str) -> tuple[list[dict], dict | None, str]:
    """codex exec --json JSONL에서 (actions, usage{in,out,total}|None, 마지막 agent_message 텍스트).
    item 종류 이름은 버전마다 다를 수 있어 부분 일치로 본다(command/search/file·patch)."""
    actions: list[dict] = []
    usage: dict | None = None
    last_text = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = str(obj.get("type") or "")
        it = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        k = str(it.get("type") or "")
        if t == "item.completed":
            if "command" in k:
                rc = it.get("exit_code")
                out = it.get("aggregated_output") or it.get("output") or ""
                actions.append({"tool": "Bash", "input": str(it.get("command") or "")[:300],
                                "output": str(out)[:MAX_ACTION_OUTPUT], "error": rc not in (None, 0)})
            elif "search" in k:
                actions.append({"tool": "WebSearch", "input": str(it.get("query") or "")[:300], "output": "", "error": False})
            elif "file" in k or "patch" in k:
                changes = it.get("changes") or it.get("files") or []
                names = [str(c.get("path") or c) if isinstance(c, dict) else str(c) for c in changes] if isinstance(changes, list) else [str(changes)]
                actions.append({"tool": "Edit", "input": ", ".join(names)[:300], "output": "", "error": False})
            elif k == "agent_message":
                last_text = str(it.get("text") or "")
        elif t == "turn.completed":
            u = obj.get("usage")
            if isinstance(u, dict) and (u.get("input_tokens") is not None or u.get("output_tokens") is not None):
                i, o = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
                usage = {"in": i, "out": o, "total": i + o}
    return actions, usage, last_text


def _claude_image_message(prompt: str, images: list[dict]) -> str:
    """이미지가 있을 때 --input-format stream-json 으로 넣을 사용자 메시지 한 줄(JSON)."""
    import base64
    content: list[dict] = [{"type": "text", "text": prompt}]
    for a in images:
        data = base64.b64encode(Path(a["path"]).read_bytes()).decode("ascii")
        content.append({"type": "image", "source": {"type": "base64", "media_type": a["media_type"], "data": data}})
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False) + "\n"


def call_claude(cfg: Config, system_prompt: str, prompt: str, stage: str,
                on_delta: DeltaCB | None = None, on_tick: TickCB | None = None,
                cancel: threading.Event | None = None, images: list[dict] | None = None) -> tuple[str, dict]:
    """on_delta가 있으면 stream-json으로 글자 단위 델타를 받는다(누적 텍스트를 넘김).
    images가 있으면 --input-format stream-json 으로 base64 이미지 블록을 함께 보낸다 (도구 불필요)."""
    images = image_attachments(images)
    stream = on_delta is not None or bool(images) or bool(cfg.tools or cfg.web_search)  # 도구 이벤트를 기록하려면 stream-json
    cmd = [cfg.claude_exe, "-p", "--output-format", "stream-json" if stream else "json",
           "--no-session-persistence",    # 세션 파일 저장 안 함
           "--strict-mcp-config",         # MCP 서버 로드 안 함
           "--system-prompt", system_prompt]
    cmd += claude_tool_args(cfg)          # 꺼져 있으면 --tools "" (전부 비활성), 켜져 있으면 허용 목록만
    if stream:
        cmd += ["--verbose", "--include-partial-messages"]  # stream-json은 --verbose 필수
    if images:
        cmd += ["--input-format", "stream-json"]
        prompt = _claude_image_message(prompt, images)
    if cfg.claude_model:
        cmd += ["--model", cfg.claude_model]
    if cfg.claude_effort:
        cmd += ["--effort", cfg.claude_effort]

    acc: list[str] = []
    result_obj: dict | None = None
    parser = ClaudeEventParser()  # 도구 사용 기록

    def on_line(line: str) -> None:
        nonlocal result_obj
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        t = obj.get("type")
        parser.feed(obj)
        if t == "stream_event":
            ev = obj.get("event") or {}
            delta = ev.get("delta") or {}
            if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                acc.append(delta.get("text", ""))
                if on_delta is not None:  # 이미지 때문에 stream-json을 쓰지만 델타 콜백은 없을 수 있다
                    on_delta("".join(acc))
        elif t == "result":
            result_obj = obj

    rc, out, err, killed = _popen_stream(cmd, prompt, cfg.timeout, on_line if stream else None, on_tick, cancel,
                                         cwd=cwd_for(cfg), env=clean_env(cfg.tools))
    if on_delta is None and stream and result_obj is None:
        # 이미지 때문에 stream-json을 썼지만 델타 콜백이 없는 경우: result 이벤트를 직접 찾는다
        for line in out.splitlines():
            if line.startswith("{") and '"type":"result"' in line:
                try:
                    result_obj = json.loads(line)
                except json.JSONDecodeError:
                    pass
    if killed == "cancelled":
        raise CLIError("claude", stage, "사용자가 중단함", cancelled=True)
    if killed == "timeout":
        raise CLIError("claude", stage, f"{cfg.timeout}초 안에 응답하지 않음", timeout=True, stderr=err)
    if rc != 0:
        raise CLIError("claude", stage, "비정상 종료", rc, err or out)
    if stream:
        data = result_obj
        if data is None:
            raise CLIError("claude", stage, "스트림에서 result 이벤트를 받지 못함", rc, err + "\n--- stdout ---\n" + out[-2000:])
    else:
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            raise CLIError("claude", stage, "JSON 출력 파싱 실패", rc, err + "\n--- stdout ---\n" + out[:2000]) from None
    if data.get("is_error") or data.get("subtype") != "success":
        raise CLIError("claude", stage, f"응답 오류 subtype={data.get('subtype')}", rc, str(data.get("result", ""))[:2000])
    meta = {"session_id": data.get("session_id"), "duration_ms": data.get("duration_ms"),
            "usage": data.get("usage"), "cost_usd": data.get("total_cost_usd"),   # API 환산 비용 (구독이면 실제 과금 아님)
            "models": list((data.get("modelUsage") or {}).keys()),
            "actions": parser.actions, "denials": data.get("permission_denials") or []}  # 도구 사용 기록·거부된 요청
    return str(data.get("result", "")).strip(), meta


def call_codex(cfg: Config, prompt: str, stage: str,
               on_tick: TickCB | None = None, cancel: threading.Event | None = None,
               images: list[dict] | None = None) -> tuple[str, dict]:
    """Codex exec는 완성된 메시지만 내보내므로(글자 단위 스트리밍 없음) 마지막 메시지를 -o 파일로 받는다.
    images는 `-i 경로`로 첨부한다 (codex exec --help의 -i/--image)."""
    fd, out_path = tempfile.mkstemp(prefix="codex_last_", suffix=".txt")
    os.close(fd)
    cmd = [cfg.codex_exe, "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never", "-o", out_path]
    cmd += codex_tool_args(cfg)  # 샌드박스 정책·작업 폴더·웹 검색·(도구 켜면) --json
    for a in image_attachments(images):
        cmd += ["-i", a["path"]]
    model, effort, note = resolve_codex(cfg)  # 'auto' → 카탈로그 최상위, effort → 지원 범위로 조정
    if model:
        cmd += ["-m", model]
    if effort:
        # -c 값은 TOML로 파싱되고 실패하면 문자열 리터럴로 쓰인다. 따옴표 없이 넘겨 Windows quoting 문제를 피한다.
        cmd += ["-c", f"model_reasoning_effort={effort}"]
    cmd.append("-")  # 프롬프트를 stdin에서 읽음
    try:
        rc, out, err, killed = _popen_stream(cmd, prompt, cfg.timeout, None, on_tick, cancel,
                                             cwd=cwd_for(cfg), env=clean_env(cfg.tools))
        text = ""
        if Path(out_path).exists():
            text = Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
    finally:
        Path(out_path).unlink(missing_ok=True)
    if killed == "cancelled":
        raise CLIError("codex", stage, "사용자가 중단함", cancelled=True)
    if killed == "timeout":
        raise CLIError("codex", stage, f"{cfg.timeout}초 안에 응답하지 않음", timeout=True, stderr=err)
    if rc != 0:
        msg = "비정상 종료"
        if "401 Unauthorized" in err:
            msg = "로그인 필요 (401 Unauthorized) → 터미널에서 `codex login` 실행 후 `codex login status`로 확인"
        else:
            # API 오류는 stderr에 JSON으로 찍힌다: {"error": {"message": "...", "param": "reasoning.effort"}}
            m = re.search(r'"message":\s*"((?:[^"\\]|\\.)*)"', err)
            if m:
                msg = "API 오류: " + m.group(1).replace('\\"', '"')[:400]
        raise CLIError("codex", stage, msg, rc, err or out)
    if not text and (cfg.tools or cfg.web_search):
        text = parse_codex_events(out)[2]  # --json이면 마지막 agent_message로 보완
    if not text:
        raise CLIError("codex", stage, "마지막 메시지 파일이 비어 있음", rc, err or out)
    # codex exec는 시작 시 "model: ...", "reasoning effort: ..." 헤더를 찍는다(stderr). 실제 적용값 확인용.
    header: dict[str, str] = {}
    for line in (err + "\n" + out).splitlines()[:40]:
        k, sep, v = line.partition(":")
        if sep and k.strip().lower() in ("model", "reasoning effort", "provider"):
            header.setdefault(k.strip().lower(), v.strip())
    m_tok = TOKENS_USED_RE.search(err + "\n" + out)
    meta = {"model": header.get("model"), "reasoning_effort": header.get("reasoning effort"),
            "resolve_note": note, "stdout_tail": out[-400:],
            "usage": {"total": int(m_tok.group(1).replace(",", ""))} if m_tok else None}
    if cfg.tools or cfg.web_search:
        actions, usage_json, _ = parse_codex_events(out)
        meta["actions"] = actions
        if usage_json:
            meta["usage"] = usage_json  # turn.completed의 입력/출력 토큰
    return text, meta


_codex_models_cache: dict = {"ts": 0.0, "exe": None, "models": None}


def cached_codex_models(codex_exe: str, ttl: int = 3600) -> list[dict]:
    """list_codex_models()를 프로세스 안에서 1시간 캐시 (매 단계마다 codex debug models를 부르지 않도록)."""
    c = _codex_models_cache
    if c["models"] is None or c["exe"] != codex_exe or time.time() - c["ts"] > ttl:
        c.update(models=list_codex_models(codex_exe), exe=codex_exe, ts=time.time())
    return c["models"]


def resolve_codex(cfg: Config, models: list[dict] | None = None) -> tuple[str | None, str | None, str | None]:
    """설정을 실제 CLI 인자로 해석: (모델 slug, effort, 조정 메모).
    - codex_model == 'auto' → 카탈로그 최상위(priority 최소, visibility=list) 모델
    - codex_effort가 그 모델의 지원 목록에 없으면 지원 범위 안에서 가장 높은 아래 단계로 낮춘다 (없으면 모델 기본값)"""
    if models is None:
        models = cached_codex_models(cfg.codex_exe)
    model = cfg.codex_model
    if model == CODEX_AUTO:
        model = models[0]["slug"] if models else None
    entry = next((m for m in models if m["slug"] == model), None)
    effort, note = cfg.codex_effort, None
    if effort and entry and entry.get("efforts") and effort not in entry["efforts"]:
        rank = EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else len(EFFORT_ORDER)
        lower = [e for e in entry["efforts"] if e in EFFORT_ORDER and EFFORT_ORDER.index(e) <= rank]
        new = max(lower, key=EFFORT_ORDER.index) if lower else entry.get("default_effort")
        note = f"{model}은(는) effort '{effort}' 미지원 → '{new}'로 조정"
        effort = new
    return model, effort, note


def list_codex_models(codex_exe: str, timeout: int = 60) -> list[dict]:
    """선택 가능한 Codex 모델 목록. `codex debug models` → docs/codex-models.json → 내장 목록 순으로 시도.
    visibility == "list"인 모델만, priority 순. 각 항목: slug, display_name, default_effort, efforts."""
    raw = None
    if codex_exe:
        try:
            r = subprocess.run([codex_exe, "debug", "models"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", cwd=SANDBOX, env=clean_env(), timeout=timeout,
                               creationflags=CREATE_NO_WINDOW)
            if r.returncode == 0 and r.stdout.strip():
                raw = r.stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
    if raw is None:
        cached = ROOT / "docs" / "codex-models.json"
        if cached.exists():
            raw = cached.read_text(encoding="utf-8-sig")
    if raw:
        try:
            out = []
            for m in json.loads(raw.lstrip(chr(0xFEFF))).get("models", []):  # BOM 제거
                if m.get("visibility") != "list":
                    continue
                out.append({"slug": m["slug"], "display_name": m.get("display_name") or m["slug"],
                            "default_effort": m.get("default_reasoning_level"),
                            "priority": m.get("priority", 999),
                            "efforts": [e.get("effort") for e in m.get("supported_reasoning_levels", [])
                                        if isinstance(e, dict) and e.get("effort")]})
            out.sort(key=lambda x: x["priority"])
            if out:
                return out
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
    return [dict(m) for m in CODEX_MODELS_FALLBACK]


# ----------------------------------------------------------------------------
# 계정: 상태 확인 / 로그인 / 로그아웃 (두 CLI 모두 로컬 인증 파일을 쓴다)
#   claude: ~/.claude/.credentials.json  (VS Code 확장·Claude Code와 공유)
#   codex : ~/.codex/auth.json
# ----------------------------------------------------------------------------
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _run_quiet(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=SANDBOX, env=clean_env(), timeout=timeout, creationflags=CREATE_NO_WINDOW)
        return r.returncode, ANSI_RE.sub("", (r.stdout or "") + (r.stderr or ""))
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def claude_auth_status(claude_exe: str) -> dict:
    """`claude auth status --json` → {"loggedIn", "email", "subscriptionType", "authMethod", "detail"}."""
    rc, out = _run_quiet([claude_exe, "auth", "status", "--json"])
    try:
        d = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except (ValueError, IndexError):
        return {"loggedIn": False, "detail": out.strip()[-300:] or f"exit={rc}"}
    return {"loggedIn": bool(d.get("loggedIn")), "email": d.get("email"), "subscriptionType": d.get("subscriptionType"),
            "authMethod": d.get("authMethod"), "detail": ""}


def codex_auth_status(codex_exe: str) -> dict:
    """`codex login status` → 로그인 시 exit 0 + "Logged in using ChatGPT", 아니면 exit 1 + "Not logged in"."""
    rc, out = _run_quiet([codex_exe, "login", "status"])
    line = out.strip().splitlines()[-1] if out.strip() else f"exit={rc}"
    return {"loggedIn": rc == 0 and "logged in" in out.lower() and "not logged in" not in out.lower(), "detail": line}


def claude_logout(claude_exe: str) -> tuple[int, str]:
    return _run_quiet([claude_exe, "auth", "logout"])


def codex_logout(codex_exe: str) -> tuple[int, str]:
    return _run_quiet([codex_exe, "logout"])


class LoginFlow:
    """로그인 명령을 백그라운드로 띄우고 출력에서 URL·코드를 뽑는다.
    - claude: `claude auth login` → 브라우저를 스스로 열고 URL을 찍은 뒤 'Paste code here if prompted >'로 코드 입력을 기다린다.
      (브라우저에서 로그인이 끝나면 대부분 코드 없이 'Login successful.'이 찍힌다)
    - codex : `codex login --device-auth` → URL과 일회용 코드(XXXX-XXXXX)를 찍고, 브라우저에서 코드를 넣으면 exit 0으로 끝난다.
      주의: codex는 이 명령을 시작하는 순간 기존 로그인 정보를 지운다."""

    def __init__(self, cli: str, exe: str):
        self.cli = cli
        args = [exe, "auth", "login"] if cli == "claude" else [exe, "login", "--device-auth"]
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", cwd=SANDBOX, env=clean_env(),
                                     creationflags=CREATE_NO_WINDOW)
        self.chunks: list[str] = []
        self.started = time.time()
        self.sent_code = False
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:  # 'Paste code here >' 처럼 줄바꿈 없는 프롬프트도 보이도록 글자 단위로 읽는다
        assert self.proc.stdout is not None
        while True:
            ch = self.proc.stdout.read(1)
            if not ch:
                break
            self.chunks.append(ch)

    @property
    def text(self) -> str:
        return ANSI_RE.sub("", "".join(self.chunks))

    @property
    def url(self) -> str | None:
        m = re.search(r"https://\S+", self.text)
        return m.group(0).rstrip(".,)") if m else None

    @property
    def code(self) -> str | None:
        m = re.search(r"\b[A-Z0-9]{4}-[A-Z0-9]{5}\b", self.text)
        return m.group(0) if m else None

    @property
    def wants_code(self) -> bool:
        return "Paste code" in self.text and not self.success

    def send_code(self, code: str) -> None:
        if self.proc.stdin and not self.finished():
            self.proc.stdin.write(code.strip() + "\n")
            self.proc.stdin.flush()
            self.sent_code = True

    def finished(self) -> bool:
        return self.proc.poll() is not None

    @property
    def success(self) -> bool:
        if "Login successful" in self.text:
            return True
        return self.cli == "codex" and self.finished() and self.proc.returncode == 0

    @property
    def failed(self) -> bool:
        return self.finished() and not self.success

    def cancel(self) -> None:
        if not self.finished():
            self.proc.kill()


TOKENS_USED_RE = re.compile(r"tokens?\s+used[:\s]+([\d,]+)", re.I)
