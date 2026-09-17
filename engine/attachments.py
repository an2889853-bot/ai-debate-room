# -*- coding: utf-8 -*-
"""engine.attachments — 첨부 파일: 텍스트/PDF/이미지 변환, 텍스트 없는 PDF → 쪽 이미지, 프롬프트용 렌더링."""
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
