"""AI Debate Room - 토론 엔진 (Windows)

구독 계정으로 로그인된 Claude Code CLI(`claude -p`)와 OpenAI Codex CLI(`codex exec`)를
subprocess로 번갈아 호출해 토론을 수행한다. 각 단계에는 직전 답변만이 아니라
지금까지의 **전체 대화 기록**(사용자의 중간 개입 포함)을 전달한다.

  Initial(Claude) → [Review(GPT) → Rebuttal(Claude)] × N → Recheck(GPT) → FINAL(Claude)

사용 예 (PowerShell, 프로젝트 폴더에서):
  python debate.py --check                     # 두 CLI가 응답하는지 OK 테스트만
  python debate.py "PLC 인터록 회로를 만들어줘"   # 기본: 일반 모드, 3단계(최초 → 검토 → 최종)
  python debate.py -q "질문" --mode plc --stages 5 --rounds 2 --file 회로.txt
  python debate.py --list-codex-models
"""
from __future__ import annotations

import argparse
import ast
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

ROOT = Path(__file__).resolve().parent
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


# ----------------------------------------------------------------------------
# 첨부 파일 → 텍스트 (질문과 함께 프롬프트에 포함)
# ----------------------------------------------------------------------------
MAX_FILE_CHARS = 60_000     # 파일 1개당 프롬프트에 넣는 최대 글자 수
MAX_TOTAL_CHARS = 150_000   # 첨부 전체 합계 상한
TEXT_UPLOAD_TYPES = ["txt", "md", "csv", "json", "pdf", "log", "xml", "yaml", "yml", "toml", "ini", "cfg",
                     "sql", "py", "js", "ts", "html", "css", "c", "h", "cpp", "java", "cs",
                     "st", "il", "awl", "scl", "ps1", "bat", "cmd"]
IMAGE_UPLOAD_TYPES = ["png", "jpg", "jpeg", "gif", "webp", "bmp"]
UPLOAD_TYPES = TEXT_UPLOAD_TYPES + IMAGE_UPLOAD_TYPES
IMAGE_MAX_SIDE = 1568       # 이미지 긴 변 상한(px). 더 크면 축소해 토큰과 전송량을 줄인다
IMAGE_DIR = CHATS / "_img"  # 첨부 이미지 저장 위치 (매 단계 CLI에 파일/base64로 다시 전달해야 하므로 디스크에 둔다)


def load_image_attachment(name: str, data: bytes, image_dir: Path | None = None) -> dict:
    """이미지 첨부: 축소·재인코딩 후 파일로 저장. Claude에는 base64 블록, Codex에는 `-i 경로`로 전달된다."""
    from PIL import Image  # streamlit 의존성으로 이미 설치됨
    import io
    warning = None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        width, height = im.size
        scale = min(1.0, IMAGE_MAX_SIDE / max(width, height))
        if scale < 1.0:
            im = im.resize((max(1, int(width * scale)), max(1, int(height * scale))))
            warning = f"{width}x{height} → {im.size[0]}x{im.size[1]}로 축소해 전달"
        keep_png = (im.format or "").upper() in ("PNG", "GIF", "BMP", "WEBP") or im.mode in ("RGBA", "LA", "P")
        if keep_png:
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA")
            ext, media, save_kw = ".png", "image/png", {"format": "PNG", "optimize": True}
        else:
            if im.mode != "RGB":
                im = im.convert("RGB")
            ext, media, save_kw = ".jpg", "image/jpeg", {"format": "JPEG", "quality": 85}
        buf = io.BytesIO()
        im.save(buf, **save_kw)
        out = buf.getvalue()
        if keep_png and len(out) > 2_500_000:  # PNG가 너무 크면 JPEG로
            im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
            out, ext, media = buf.getvalue(), ".jpg", "image/jpeg"
        size = im.size
    except Exception as e:  # noqa: BLE001
        return {"name": name, "kind": "image", "path": None, "media_type": None, "width": 0, "height": 0,
                "bytes": len(data), "chars": 0, "truncated": False, "warning": f"이미지를 열 수 없음: {e}"}
    image_dir = image_dir or (IMAGE_DIR / datetime.now().strftime("%Y%m%d_%H%M%S"))
    image_dir.mkdir(parents=True, exist_ok=True)
    stem = slugify(Path(name).stem) or "image"
    path = image_dir / f"{stem}{ext}"
    k = 1
    while path.exists():
        k += 1
        path = image_dir / f"{stem}_{k}{ext}"
    path.write_bytes(out)
    return {"name": name, "kind": "image", "path": str(path), "media_type": media, "width": size[0], "height": size[1],
            "bytes": len(out), "chars": 0, "truncated": False, "warning": warning}


def image_attachments(attachments: list[dict] | None) -> list[dict]:
    """경로가 살아 있는 이미지 첨부만."""
    return [a for a in (attachments or []) if a.get("kind") == "image" and a.get("path") and Path(a["path"]).exists()]


def load_attachments(name: str, data: bytes, image_dir: Path | None = None) -> list[dict]:
    """업로드 파일 하나 → 첨부 항목 목록. 보통 1개지만, 텍스트 레이어가 없는 PDF(스캔본·'인쇄→PDF' 출력물)는
    쪽마다 이미지 항목으로 바뀐다 (두 AI 모두 이미지를 읽을 수 있으므로)."""
    a = load_attachment(name, data, image_dir)
    if Path(name).suffix.lower() != ".pdf" or len((a.get("text") or "").strip()) >= 20:
        return [a]
    pages, total, warn = _pdf_pages_to_images(data)
    if not pages:
        a["warning"] = (a.get("warning") or "텍스트 레이어 없음") + f"; 이미지 변환 실패: {warn}"
        return [a]
    image_dir = image_dir or (IMAGE_DIR / datetime.now().strftime("%Y%m%d_%H%M%S"))
    out = []
    for i, png in enumerate(pages, start=1):
        item = load_image_attachment(f"{Path(name).stem}_p{i}.png", png, image_dir)
        item["name"] = f"{name} ({i}/{total}쪽)"
        item["from_pdf"] = name
        out.append(item)
    note = f"텍스트 레이어가 없는 PDF(스캔본/인쇄 출력물)라 {len(pages)}쪽을 이미지로 전달"
    if total > len(pages):
        note += f" (전체 {total}쪽 중 앞 {len(pages)}쪽만)"
    out[0]["warning"] = note
    return out


def _decode_text(data: bytes) -> str | None:
    """텍스트 파일 디코딩. 한국어 Windows 파일(cp949)도 처리. 바이너리면 None."""
    if b"\x00" in data[:4096]:
        return None
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _find_tool(name: str) -> str | None:
    """PATH에서 찾고, 없으면 winget이 설치한 poppler 폴더에서 찾는다 (서버 프로세스의 PATH가 사용자 PATH와 다를 수 있음)."""
    p = shutil.which(name)
    if p:
        return p
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if base.exists():
        for cand in base.glob(f"*Poppler*/**/bin/{name}.exe"):
            return str(cand)
    return None


MAX_PDF_PAGES_AS_IMAGES = 12   # 텍스트 없는 PDF를 이미지로 넘길 때 최대 쪽수
PDF_RENDER_DPI = 130           # A4 기준 약 1075x1520px → 모델이 한글을 읽기에 충분


def _pdf_pages_to_images(data: bytes, max_pages: int = MAX_PDF_PAGES_AS_IMAGES) -> tuple[list[bytes], int, str | None]:
    """텍스트 레이어가 없는 PDF의 쪽을 PNG 바이트로. poppler pdftoppm → pypdf(쪽에 박힌 이미지 추출) 순.
    반환 (이미지 목록, 전체 쪽수, 경고)."""
    total = 0
    try:
        import io
        from pypdf import PdfReader  # type: ignore
        total = len(PdfReader(io.BytesIO(data)).pages)
    except Exception:  # noqa: BLE001
        pass
    exe = _find_tool("pdftoppm")
    if exe:
        tmpdir = Path(tempfile.mkdtemp(prefix="pdfpages_"))
        try:
            src = tmpdir / "in.pdf"
            src.write_bytes(data)
            r = subprocess.run([exe, "-png", "-r", str(PDF_RENDER_DPI), "-f", "1", "-l", str(max_pages), str(src), str(tmpdir / "p")],
                               capture_output=True, timeout=300, creationflags=CREATE_NO_WINDOW)
            pages = sorted(tmpdir.glob("p-*.png"), key=lambda q: int(q.stem.split("-")[-1]))
            if r.returncode == 0 and pages:
                return [q.read_bytes() for q in pages], total or len(pages), None
            warn = f"pdftoppm 실패 (exit={r.returncode})"
        except (OSError, subprocess.TimeoutExpired) as e:
            warn = f"pdftoppm 실행 오류: {e}"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        warn = "pdftoppm(poppler) 없음"
    try:  # 폴백: 쪽마다 박힌 이미지(스캔본/인쇄본)를 그대로 꺼낸다
        import io
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(io.BytesIO(data))
        out = []
        for page in reader.pages[:max_pages]:
            imgs = list(page.images)
            if imgs:
                out.append(max(imgs, key=lambda im: len(im.data)).data)
        if out:
            return out, total or len(reader.pages), None
        return [], total, warn + "; 쪽에서 이미지도 찾지 못함"
    except Exception as e:  # noqa: BLE001
        return [], total, f"{warn}; pypdf 오류: {e}"


def _pdf_to_text(data: bytes) -> tuple[str | None, str | None]:
    """poppler pdftotext → pypdf 순으로 시도. 반환 (텍스트, 경고)."""
    warn = "pdftotext(poppler) 없음"
    exe = _find_tool("pdftotext")
    if exe:
        fd, tmp = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            Path(tmp).write_bytes(data)
            r = subprocess.run([exe, "-layout", "-enc", "UTF-8", tmp, "-"], capture_output=True, timeout=120,
                               creationflags=CREATE_NO_WINDOW)
            if r.returncode == 0:
                return r.stdout.decode("utf-8", errors="replace"), None
            warn = f"pdftotext 실패 (exit={r.returncode})"
        except (OSError, subprocess.TimeoutExpired) as e:
            warn = f"pdftotext 실행 오류: {e}"
        finally:
            Path(tmp).unlink(missing_ok=True)
    try:
        import io
        from pypdf import PdfReader  # type: ignore  # 선택 의존성
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages), None
    except ImportError:
        return None, warn + "; pypdf 미설치 (pip install pypdf)"
    except Exception as e:  # noqa: BLE001
        return None, f"{warn}; pypdf 오류: {e}"


def load_attachment(name: str, data: bytes, image_dir: Path | None = None) -> dict:
    """업로드 파일 하나를 {"name", "text", "chars", "truncated", "warning"} 로 변환. 이미지는 load_image_attachment로."""
    warning = None
    ext = Path(name).suffix.lower().lstrip(".")
    if ext in IMAGE_UPLOAD_TYPES:
        return load_image_attachment(name, data, image_dir)
    if ext == "pdf":
        text, warning = _pdf_to_text(data)
    else:
        text = _decode_text(data)
        if text is None:
            warning = "텍스트로 읽을 수 없는 형식(바이너리/이미지)이라 내용은 전달되지 않음"
    text = text or ""
    total = len(text)
    truncated = total > MAX_FILE_CHARS
    if truncated:
        text = text[:MAX_FILE_CHARS] + f"\n... (이하 생략: 전체 {total:,}자 중 {MAX_FILE_CHARS:,}자만 전달)"
    return {"name": name, "text": text, "chars": total, "truncated": truncated, "warning": warning}


def render_attachments(attachments: list[dict]) -> str:
    if not attachments:
        return ""
    parts, budget = [], MAX_TOTAL_CHARS
    for i, a in enumerate(attachments, start=1):
        if a.get("kind") == "image":
            if a.get("path") and Path(a["path"]).exists():
                parts.append(f"--- 첨부 {i}: {a['name']} (이미지 {a.get('width')}x{a.get('height')}, 이미지 자체가 이 메시지에 함께 첨부됨) ---")
            else:
                parts.append(f"--- 첨부 {i}: {a['name']} (이미지, 전달 실패: {a.get('warning') or '파일 없음'}) ---")
            continue
        body = a.get("text") or ""
        if not body and a.get("warning"):
            body = f"(내용 없음: {a['warning']})"
        if len(body) > budget:
            body = body[:max(budget, 0)] + "\n... (첨부 전체 상한 초과로 생략)"
        budget -= len(body)
        parts.append(f"--- 첨부 {i}: {a['name']} ({a.get('chars', 0):,}자) ---\n{body}")
    return "\n\n[첨부 파일]\n" + "\n\n".join(parts)


# ----------------------------------------------------------------------------
# 프로세스 실행 공통: stdin으로 프롬프트, stdout 라인 스트리밍, 타임아웃, 취소
# ----------------------------------------------------------------------------
TickCB = Callable[[float], None]


def _popen_stream(cmd: list[str], prompt: str, timeout: int,
                  on_line: Callable[[str], None] | None = None,
                  on_tick: TickCB | None = None,
                  cancel: threading.Event | None = None) -> tuple[int, str, str, str | None]:
    """반환 (returncode, stdout, stderr, killed_reason). killed_reason: None | "timeout" | "cancelled"."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=SANDBOX, env=clean_env(), text=True, encoding="utf-8", errors="replace",
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
    stream = on_delta is not None or bool(images)
    cmd = [cfg.claude_exe, "-p", "--output-format", "stream-json" if stream else "json",
           "--tools", "",                 # 도구 전부 비활성화 (텍스트 토론만)
           "--no-session-persistence",    # 세션 파일 저장 안 함
           "--strict-mcp-config",         # MCP 서버 로드 안 함
           "--system-prompt", system_prompt]
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
        if t == "stream_event":
            ev = obj.get("event") or {}
            delta = ev.get("delta") or {}
            if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                acc.append(delta.get("text", ""))
                if on_delta is not None:  # 이미지 때문에 stream-json을 쓰지만 델타 콜백은 없을 수 있다
                    on_delta("".join(acc))
        elif t == "result":
            result_obj = obj

    rc, out, err, killed = _popen_stream(cmd, prompt, cfg.timeout, on_line if stream else None, on_tick, cancel)
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
            "models": list((data.get("modelUsage") or {}).keys())}
    return str(data.get("result", "")).strip(), meta


def call_codex(cfg: Config, prompt: str, stage: str,
               on_tick: TickCB | None = None, cancel: threading.Event | None = None,
               images: list[dict] | None = None) -> tuple[str, dict]:
    """Codex exec는 완성된 메시지만 내보내므로(글자 단위 스트리밍 없음) 마지막 메시지를 -o 파일로 받는다.
    images는 `-i 경로`로 첨부한다 (codex exec --help의 -i/--image)."""
    fd, out_path = tempfile.mkstemp(prefix="codex_last_", suffix=".txt")
    os.close(fd)
    cmd = [cfg.codex_exe, "exec",
           "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral",
           "--color", "never", "-C", str(SANDBOX), "-o", out_path]
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
        rc, out, err, killed = _popen_stream(cmd, prompt, cfg.timeout, None, on_tick, cancel)
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


# ---- 관측: 토큰·비용·시간 ----
# Claude는 결과 JSON의 usage(입력=input+cache_creation+cache_read, 출력=output)와 total_cost_usd(API 환산, 구독이면 실제 과금 아님).
# Codex는 CLI가 "tokens used: N" 줄을 찍을 때만 총 토큰을 안다(입력/출력 구분 없음). 모르면 None으로 두고 '?'로 표시한다.
TOKENS_USED_RE = re.compile(r"tokens?\s+used[:\s]+([\d,]+)", re.I)


def usage_of(entry: dict) -> dict:
    """단계 항목의 토큰·비용: {"in", "out", "total", "cost_usd"} (모르면 None)."""
    meta = entry.get("meta") or {}
    u = meta.get("usage") or {}
    if entry.get("who") == "claude" and u:
        inp = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        out = int(u.get("output_tokens") or 0)
        return {"in": inp, "out": out, "total": inp + out, "cost_usd": meta.get("cost_usd")}
    if u.get("total") is not None:
        return {"in": None, "out": None, "total": int(u["total"]), "cost_usd": None}
    return {"in": None, "out": None, "total": None, "cost_usd": None}


def fmt_tokens(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def usage_line(entry: dict) -> str:
    """캡션용 한 줄: '12.3k→0.8k 토큰 · API 환산 $0.04' / '총 1.2k 토큰' / '' (모르면)."""
    u = usage_of(entry)
    if u["total"] is None:
        return ""
    s = f"{fmt_tokens(u['in'])}→{fmt_tokens(u['out'])} 토큰" if u["in"] is not None else f"총 {fmt_tokens(u['total'])} 토큰"
    if u["cost_usd"]:
        s += f" · API 환산 ${u['cost_usd']:.3f}"
    return s


def usage_summary(stages: list[dict]) -> dict:
    """라운드 합계: {"stages": AI 단계 수, "elapsed": 초, "tokens": 합계(아는 것만), "known": 토큰을 아는 단계 수,
    "cost_usd": 합계(아는 것만), "by_who": {"claude": {"stages", "elapsed", "tokens"}, "gpt": {...}}}"""
    out = {"stages": 0, "elapsed": 0.0, "tokens": 0, "known": 0, "cost_usd": 0.0,
           "by_who": {"claude": {"stages": 0, "elapsed": 0.0, "tokens": 0}, "gpt": {"stages": 0, "elapsed": 0.0, "tokens": 0}}}
    for h in stages:
        who = h.get("who")
        if who not in out["by_who"]:
            continue
        u = usage_of(h)
        el = float(h.get("elapsed") or 0)
        out["stages"] += 1
        out["elapsed"] += el
        out["by_who"][who]["stages"] += 1
        out["by_who"][who]["elapsed"] += el
        if u["total"] is not None:
            out["tokens"] += u["total"]
            out["known"] += 1
            out["by_who"][who]["tokens"] += u["total"]
        if u["cost_usd"]:
            out["cost_usd"] += float(u["cost_usd"])
    out["elapsed"] = round(out["elapsed"], 1)
    out["cost_usd"] = round(out["cost_usd"], 4)
    return out


def usage_summary_line(stages: list[dict]) -> str:
    s = usage_summary(stages)
    if not s["stages"]:
        return ""
    bw = s["by_who"]
    tok = (f"토큰 {fmt_tokens(s['tokens'])} (Claude {fmt_tokens(bw['claude']['tokens'])} · GPT {fmt_tokens(bw['gpt']['tokens']) if bw['gpt']['tokens'] else '?'})"
           if s["known"] else "토큰 ?")
    line = f"⏱ 합계 {s['elapsed']:.0f}s ({s['stages']}단계) · {tok}"
    if s["cost_usd"]:
        line += f" · API 환산 ${s['cost_usd']:.2f}"
    if s["known"] and s["known"] < s["stages"]:
        line += f" · 토큰 미확인 {s['stages'] - s['known']}단계"
    return line


def stats(conversations: list[dict]) -> dict:
    """저장된 대화 전체 집계 (하네스 관측용). 라운드 수, AI별 단계 수·평균 시간, 조기 종료율, 반영 계약(지적·미처리·재요청),
    독립 평가(PASS율·재작성), 토큰·비용 합계."""
    st = {"conversations": len(conversations), "rounds": 0, "done": 0, "early_stopped": 0,
          "by_who": {"claude": {"stages": 0, "elapsed": 0.0, "tokens": 0}, "gpt": {"stages": 0, "elapsed": 0.0, "tokens": 0}},
          "contract": {"rounds": 0, "issues": 0, "accepted": 0, "rejected": 0, "retries": 0, "rounds_missing": 0},
          "eval": {"rounds": 0, "pass": 0, "needs_work": 0, "revised": 0},
          "tokens": 0, "cost_usd": 0.0, "elapsed": 0.0}
    for conv in conversations:
        for r in conv.get("rounds", []):
            st["rounds"] += 1
            st["done"] += r.get("status") == "done"
            st["early_stopped"] += bool(r.get("early_stopped"))
            u = usage_summary(r.get("stages", []))
            st["tokens"] += u["tokens"]
            st["cost_usd"] += u["cost_usd"]
            st["elapsed"] += u["elapsed"]
            for who in ("claude", "gpt"):
                for k in ("stages", "elapsed", "tokens"):
                    st["by_who"][who][k] += u["by_who"][who][k]
            c = r.get("contract") or contract_summary(r.get("stages", []))
            if c.get("issues"):
                st["contract"]["rounds"] += 1
                for k in ("issues", "accepted", "rejected", "retries"):
                    st["contract"][k] += int(c.get(k) or 0)
                st["contract"]["rounds_missing"] += bool(c.get("missing"))
            ev = r.get("evaluation") or eval_summary(r.get("stages", []))
            if ev.get("evals"):
                st["eval"]["rounds"] += 1
                st["eval"]["pass"] += ev.get("verdict") == "PASS"
                st["eval"]["needs_work"] += ev.get("verdict") == "NEEDS_WORK"
                st["eval"]["revised"] += bool(ev.get("revised"))
    for who in ("claude", "gpt"):
        b = st["by_who"][who]
        b["avg_elapsed"] = round(b["elapsed"] / b["stages"], 1) if b["stages"] else None
    st["cost_usd"] = round(st["cost_usd"], 2)
    st["elapsed"] = round(st["elapsed"], 1)
    return st


def stats_lines(st: dict) -> list[str]:
    """통계를 사람이 읽는 줄 목록으로 (콘솔·사이드바 공용)."""
    bw, c, e = st["by_who"], st["contract"], st["eval"]
    lines = [f"대화 {st['conversations']}개 · 라운드 {st['rounds']}개 (완료 {st['done']}, 조기 종료 {st['early_stopped']})",
             f"Claude {bw['claude']['stages']}단계 평균 {bw['claude']['avg_elapsed'] or '?'}s · GPT {bw['gpt']['stages']}단계 평균 {bw['gpt']['avg_elapsed'] or '?'}s",
             f"토큰 {fmt_tokens(st['tokens'])} (Claude {fmt_tokens(bw['claude']['tokens'])} · GPT {fmt_tokens(bw['gpt']['tokens']) if bw['gpt']['tokens'] else '?'}) · API 환산 ${st['cost_usd']:.2f} · 총 {st['elapsed']:.0f}s"]
    if c["rounds"]:
        lines.append(f"반영 계약 {c['rounds']}라운드: 지적 {c['issues']} → 반영 {c['accepted']} · 반박 {c['rejected']} · 재요청 {c['retries']}회 · 미처리 라운드 {c['rounds_missing']}")
    if e["rounds"]:
        lines.append(f"독립 평가 {e['rounds']}라운드: PASS {e['pass']} · NEEDS_WORK {e['needs_work']} · 재작성 {e['revised']}")
    return lines


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


# 단계 지시문. {other}는 상대 AI 이름(직전 단계 작성자 또는 다음 검토자)으로 실행 시점에 채워진다 → 순서를 바꿔도 지시문이 맞는다.
STAGE_TEMPLATES: dict[str, str] = {
    "initial": (
        "사용자의 질문을 처음 분석하고 해결책/주장/설계안을 제시하십시오. 핵심 근거와 가정을 명확히 적으십시오. "
        "이 답변은 이후 {other}가 검토하므로, 검증 가능한 형태로 구체적으로 쓰십시오."),
    "review": (
        "사용자 질문과 {other}의 {target}을 검토하십시오. 살펴볼 관점: "
        "틀린 부분 · 논리적 허점 · 빠진 부분 · 개선 가능한 부분 · 더 좋은 대안.\n"
        "각 지적에는 왜 그런지 근거를 붙이십시오. 문제가 없는 부분은 짧게 인정하십시오. "
        "답변 전체를 새로 다시 쓰지는 말고 검토에 집중하십시오.{round_note}" + ISSUE_FORMAT_RULE + VERDICT_RULE),
    "rebuttal": (
        "{other}의 검토를 항목별로 판정하십시오: 타당한 지적은 `[반영 N] 무엇을 어떻게 고쳤는지 한 줄`, "
        "틀렸다고 판단되는 지적은 `[반박 N] 근거`로, 검토의 [지적 N] 번호마다 빠짐없이 한 줄씩 쓰십시오. "
        "그 다음 '수정된 답변'을 완성된 형태로 다시 작성하십시오 (판정 목록 → 수정된 답변 순서)."),
    "recheck": (
        "지금까지의 전체 토론을 보고 {other}의 최신 수정본을 재검사하십시오: "
        "(a) 여전히 남아 있는 오류나 문제점, (b) 이전 검토의 지적이 제대로 반영되었는지, (c) {other}의 반박이 타당한지. "
        "수정이 더 필요한 부분은 새 번호로 나열하고(이전 검토의 번호를 언급할 때는 '1차 검토의 지적 2'처럼 구분), "
        "없다면 '추가 수정 불필요'라고 명시하십시오." + ISSUE_FORMAT_RULE + VERDICT_RULE),
    "final": (
        "사용자 질문부터 지금까지의 Claude/GPT 전체 토론을 종합하십시오.{fb_note} 양쪽 의견 중 남아 있는 충돌이 있으면 판단해 결론을 내리십시오. "
        "그리고 사용자에게 보여줄 **하나의 완결된 최종 답변**만 작성하십시오. 토론 과정을 길게 재서술하지 말고, "
        "맨 끝에 '토론을 통해 달라진 점'을 3줄 이내로만 덧붙이십시오."),
    "evaluate": (
        "당신은 이 토론의 **독립 평가자**입니다. 답을 새로 쓰지 말고 {other}의 최종 답변(FINAL)만 채점하십시오. 기준: "
        "(1) 사용자 질문에 실제로 답했는가, (2) 검토에서 나온 지적이 최종 답변에 반영되었거나 근거 있게 반박되었는가, "
        "(3) 근거 없는 단정·내부 모순·사실 오류, (4) 대화 기록의 '[프로그램 검사 ...]' 결과와 어긋나는 주장. "
        "문제가 있으면 무엇을 어떻게 고쳐야 하는지 구체적으로 적으십시오." + ISSUE_FORMAT_RULE + EVAL_RULE),
}
KIND_LABEL = {"initial": "Initial", "review": "Review", "rebuttal": "Rebuttal", "recheck": "Recheck", "final": "FINAL",
              "evaluate": "Eval"}
KIND_KO = {"initial": "최초 답변", "review": "검토", "rebuttal": "반박·수정", "recheck": "재검사", "final": "최종 정리",
           "evaluate": "평가"}
OTHER = {"claude": "gpt", "gpt": "claude"}

MODES: dict[str, dict] = {
    "general": {
        "name": "일반 토론",
        "description": "어떤 주제든. 사실 확인·논리·대안 중심으로 검토합니다.",
        "rules": "",
        "hints": {},
    },
    "code_review": {
        "name": "코드 리뷰",
        "description": "첨부한 코드의 버그·보안·성능·가독성을 검토하고 수정 코드를 냅니다.",
        "rules": ("이번 토론은 코드 리뷰입니다. 정확성(버그·경계 조건·예외 처리) → 보안 → 성능 → 가독성·유지보수성 순으로 검토하고, "
                  "지적할 때는 해당 코드 위치(함수명·줄)를 인용하십시오. 수정안은 실제 코드로 제시하십시오.\n"),
        "hints": {
            "initial": " 코드가 첨부되었으면 먼저 무엇을 하는 코드인지 요약한 뒤, 문제점과 수정 코드를 제시하십시오.",
            "review": " 각 지적에 심각도(치명/높음/중간/낮음)를 붙이십시오.",
            "final": " 최종 수정 코드 전체와 변경 요약을 제시하십시오.",
        },
    },
    "plc": {
        "name": "PLC 검증",
        "description": "GX Works2/Q 시리즈 래더 기준으로 인터록·자기유지·타이머·안전 회로를 검증합니다.",
        "rules": ("이번 토론은 PLC 프로그램/회로 검증입니다. 기본 전제: 미쓰비시 GX Works2, Q 시리즈 CPU, 래더(LD). 확인 항목: "
                  "(1) 인터록·상호 배타 조건, (2) 자기유지와 해제 조건, (3) 타이머/카운터 설정값과 리셋, "
                  "(4) 디바이스 할당(X/Y/M/T/C/D)과 a/b접점 일관성, (5) 비상정지·안전 회로를 하드와이어로 분리해야 하는지, "
                  "(6) 전원 투입·초기화 시 상태, (7) 이중 코일(같은 출력에 OUT 2회). "
                  "래더를 텍스트로 쓸 때는 '|--[ X0 ]--[/ X1 ]--( Y0 )--|' 처럼 접점(a: [ ], b: [/ ])과 코일( )을 명확히 표기하십시오.\n"),
        "hints": {
            "initial": " 래더를 랭(rung) 단위로 제시하고 각 랭의 목적을 한 줄로 설명하십시오.",
            "review": " 각 랭을 스캔 순서대로 따라가며 오동작이 나는 입력 조합(시나리오)을 구체적으로 제시하십시오.",
            "final": " 최종 래더 전체를 다시 쓰고, 디바이스 할당표와 시험 절차(어떤 입력을 켜면 어떤 출력이 나와야 하는지)를 붙이십시오.",
        },
    },
    "invest": {
        "name": "투자 분석",
        "description": "가정·시나리오·리스크를 분리해 분석합니다. 투자 권유가 아닌 정보 제공용.",
        "rules": ("이번 토론은 투자 분석이며 정보 제공용입니다(투자 권유 아님). 모든 주장에 근거를 붙이고, 확실하지 않은 수치는 추정임을 밝히십시오. "
                  "최소 두 가지 반대 시나리오와 리스크를 명시하십시오. 최신 시세·뉴스는 알 수 없으므로 '확인 필요' 항목으로 분리하십시오.\n"),
        "hints": {
            "initial": " 가정 → 핵심 논리 → 상승/하락 시나리오 → 리스크 → 확인 필요 항목 순으로 구성하십시오.",
            "review": " 근거가 약한 주장, 누락된 리스크, 확증 편향을 중점적으로 지적하십시오.",
            "final": " 결론은 '어떤 조건이면 어떤 판단'의 조건부로 제시하고, 사용자가 직접 확인할 체크리스트를 붙이십시오.",
        },
    },
}

DISPLAY = {"claude": "Claude", "gpt": "GPT", "user": "사용자"}


STAGE_COUNTS = [3, 5]        # 고를 수 있는 토론 단계 수
DEFAULT_STAGE_COUNT = 3      # 기본 3단계 (최초 → 검토 → 최종)


def default_plan(rounds: int = 1, first: str = "claude", final_who: str | None = None,
                 stage_count: int = DEFAULT_STAGE_COUNT, evaluate: bool = False) -> list[tuple[str, str]]:
    """기본 순서 (A=first, B=상대 AI).
    stage_count=3: A 최초 → B 검토 → (final_who 또는 A) 최종. 검토 지적은 최종 단계가 직접 판정·반영한다.
    stage_count=5: A 최초 → (B 검토 → A 반박)×rounds → B 재검사 → (final_who 또는 A) 최종.
    rounds는 5단계에서만 쓰인다 (2 이상이면 검토↔반박이 반복돼 단계가 7, 9개로 늘어난다)."""
    a = first if first in OTHER else "claude"
    b = OTHER[a]
    last = final_who if final_who in OTHER else a
    if int(stage_count) <= 3:
        steps = [(a, "initial"), (b, "review"), (last, "final")]
    else:
        steps = [(a, "initial")]
        for _ in range(max(1, int(rounds))):
            steps += [(b, "review"), (a, "rebuttal")]
        steps += [(b, "recheck"), (last, "final")]
    if evaluate:  # 최종 정리를 쓰지 않은 쪽이 독립 평가자
        steps.append((OTHER[last], "evaluate"))
    return steps


def validate_plan(steps: list[tuple[str, str]]) -> str | None:
    """사용자 정의 순서 검사. 문제 없으면 None, 있으면 한국어 설명."""
    if not steps:
        return "단계가 하나도 없습니다."
    for w, k in steps:
        if w not in OTHER or k not in KIND_LABEL:
            return f"알 수 없는 값: {w} / {k}"
    kinds = [k for _, k in steps]
    if kinds[0] != "initial":
        return "첫 단계는 '최초 답변'이어야 합니다."
    if "evaluate" in kinds:
        if kinds.count("evaluate") > 1:
            return "'평가'는 한 번만 넣을 수 있습니다."
        if kinds[-1] != "evaluate" or len(kinds) < 2 or kinds[-2] != "final":
            return "'평가'는 '최종 정리' 바로 뒤, 맨 마지막에만 둘 수 있습니다."
    elif kinds[-1] != "final":
        return "마지막 단계는 '최종 정리'(또는 그 뒤의 '평가')여야 합니다."
    if kinds.count("initial") != 1 or kinds.count("final") != 1:
        return "'최초 답변'과 '최종 정리'는 각각 한 번만 넣을 수 있습니다."
    return None


def plan_stages(rounds: int = 1, mode: str = "general", first: str = "claude",
                final_who: str | None = None, custom: list | None = None,
                stage_count: int = DEFAULT_STAGE_COUNT, evaluate: bool = False) -> list[dict]:
    """토론 단계 계획. 각 항목: {"who", "label", "kind", "instruction"}.
    custom이 있으면 [(who, kind), ...] 그대로, 없으면 default_plan(rounds, first, final_who, stage_count, evaluate)."""
    steps = ([(str(w), str(k)) for w, k in custom] if custom
             else default_plan(rounds, first, final_who, stage_count, evaluate))
    err = validate_plan(steps)
    if err:
        raise ValueError(err)
    hints = MODES.get(mode, MODES["general"])["hints"]
    plan: list[dict] = []
    counts: dict[str, int] = {}
    for i, (who, kind) in enumerate(steps):
        counts[kind] = counts.get(kind, 0) + 1
        n = counts[kind]
        label = KIND_LABEL[kind] + ("" if n == 1 or kind in ("initial", "final", "evaluate") else f" {n}")
        # {other}: 최초 답변이면 다음 단계(검토자), 그 외엔 직전 단계 작성자
        ref = steps[i + 1][0] if kind == "initial" and i + 1 < len(steps) else (steps[i - 1][0] if i > 0 else OTHER[who])
        fmt = {"other": DISPLAY[ref] if ref != who else "이전 단계"}
        if kind == "review":
            fmt["target"] = "최초 답변" if n == 1 else "최신 수정본"
            fmt["round_note"] = "" if n == 1 else " 이미 해결된 지적은 반복하지 말고, 새로 생긴 문제나 아직 남은 문제에 집중하십시오."
        if kind == "final":
            # 반박 단계가 없는 짧은 순서(3단계)에서는 검토 지적의 판정·반영을 최종 단계가 직접 맡는다
            fmt["fb_note"] = ("" if any(k2 == "rebuttal" for _, k2 in steps[:i]) else
                              " 앞선 검토의 지적을 항목별로 판정해 — 타당한 지적은 `[반영 N] 한 줄`로 답변에 반영하고, "
                              "틀렸다고 판단되는 지적은 `[반박 N] 근거`로 — 판정 목록을 먼저 적은 뒤 그 결과를 최종 답변에 녹이십시오.")
        text = STAGE_TEMPLATES[kind].format(**fmt) + hints.get(kind, "")
        plan.append({"who": who, "label": label, "kind": kind, "instruction": text})
    return plan


def plan_preview(plan: list[dict]) -> str:
    return " → ".join(f"{DISPLAY[s['who']]}·{s['label']}" for s in plan)


def system_rules(mode: str) -> str:
    return COMMON_RULES + MODES.get(mode, MODES["general"])["rules"]


# ----------------------------------------------------------------------------
# 프롬프트 구성
# ----------------------------------------------------------------------------
def render_prior(prior: list[dict]) -> str:
    """이전 라운드(질문 + 최종 답변)를 참고용 블록으로 렌더링. UI에서 이어지는 질문에 사용."""
    if not prior:
        return ""
    parts = []
    for k, p in enumerate(prior, start=1):
        parts.append(f"[이전 질문 {k}]\n{p['question'].strip()}\n\n[이전 최종 답변 {k}]\n{p['final'].strip()}")
    return ("=== 이전 대화 (이미 끝난 토론의 질문과 최종 답변, 참고용) ===\n"
            + "\n\n".join(parts) + "\n\n")


def render_transcript(question: str, history: list[dict], attachments: list[dict] | None = None) -> str:
    parts = [f"[사용자]\n{question.strip()}{render_attachments(attachments or [])}"]
    for h in history:
        parts.append(f"[{DISPLAY[h['who']]} · {h['label']}]\n{h['content'].strip()}")
        if h.get("evidence"):
            parts.append(render_evidence(h))
    return "\n\n".join(parts)


def build_prompt(question: str, history: list[dict], stage: dict, plan_len: int,
                 prior: list[dict] | None = None, attachments: list[dict] | None = None,
                 issues: list[tuple[int, str]] | None = None, issue_src: dict | None = None) -> str:
    """issues/issue_src가 있으면(직전 검토의 [지적 N]) 끝에 '처리해야 할 지적' 블록을 붙인다."""
    who, label = stage["who"], stage["label"]
    n = sum(1 for h in history if h["who"] != "user") + 1
    return (
        render_prior(prior or []) +
        f"=== 지금까지의 전체 대화 기록 ({len(history)}개 발언) ===\n"
        f"{render_transcript(question, history, attachments)}\n\n"
        f"=== 이번 단계 ({n}/{plan_len}): {DISPLAY[who]} · {label} ===\n"
        f"당신은 {DISPLAY[who]}입니다. {stage['instruction']}\n"
        f"'[{DISPLAY[who]} · {label}]' 같은 머리말은 붙이지 말고 본문만 쓰십시오."
        + (contract_block(issue_src, issues) if issues and issue_src else "")
    )


# ----------------------------------------------------------------------------
# 단계 실행
# ----------------------------------------------------------------------------
def execute_stage(question: str, attachments: list[dict], history: list[dict], stage: dict,
                  cfg: Config, prior: list[dict] | None = None, mode: str = "general", plan_len: int = 5,
                  on_delta: DeltaCB | None = None, on_tick: TickCB | None = None,
                  cancel: threading.Event | None = None, max_retries: int = MAX_CONTRACT_RETRIES) -> dict:
    """단계 하나를 실행해 기록 항목을 반환. 실패 시 CLIError.
    반박/최종 단계는 직전 검토의 [지적 N]마다 [반영/반박 N]이 있어야 하며, 빠지면 max_retries회까지 재요청한다.
    검사 결과는 entry["contract"] = {"issues", "resolved", "missing", "retries", "source"} (검사할 지적이 없으면 키 없음)."""
    stage_name = f"{DISPLAY[stage['who']]} · {stage['label']}"
    src, issues = open_issues(history) if stage["kind"] in RESPOND_KINDS else (None, [])
    prompt = build_prompt(question, history, stage, plan_len, prior, attachments, issues, src)
    rules = system_rules(mode)
    t0 = time.time()
    images = image_attachments(attachments)  # 매 호출이 독립 세션이므로 이미지도 매 단계 다시 전달
    contract: dict | None = None
    retries = 0
    while True:
        if stage["who"] == "claude":
            content, meta = call_claude(cfg, rules, prompt, stage_name, on_delta, on_tick, cancel, images)
        else:
            content, meta = call_codex(cfg, rules + "\n" + prompt, stage_name, on_tick, cancel, images)
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
    return entry


def reviewer_says_ok(entry: dict) -> bool:
    """검토 단계 답변이 '[판정: 추가 수정 불필요]'로 끝났는지 (검토자가 Claude든 GPT든)."""
    return entry.get("who") != "user" and bool(VERDICT_OK_RE.search(entry.get("content", "")))


gpt_says_ok = reviewer_says_ok  # 하위 호환


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
    run = {"question": question, "attachments": attachments or [], "config": asdict(cfg),
           "mode": mode, "rounds": rounds, "stage_count": stage_count,
           "plan": [s["label"] for s in plan], "stages": history,
           "error": None, "status": "running", "early_stopped": False, "prior_rounds": len(prior or []),
           "started": datetime.now().isoformat(timespec="seconds")}
    i = 0
    while i < len(plan):
        stage = plan[i]
        emit({"type": "start", "index": i + 1, "total": len(plan), "who": stage["who"], "label": stage["label"]})
        try:
            entry = execute_stage(question, attachments or [], history, stage, cfg, prior, mode, len(plan))
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
    run["finished"] = datetime.now().isoformat(timespec="seconds")
    return run


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
                 run_code=a.run_code)
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


if __name__ == "__main__":
    sys.exit(main())
