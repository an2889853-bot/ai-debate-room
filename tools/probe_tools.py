# -*- coding: utf-8 -*-
"""실기 프로브 — 도구 허용이 실제 CLI에서 어떻게 동작하는지 확인 (구독 사용량을 씀: Claude 2회, Codex 1회, 저렴한 설정).

실행:  .venv\\Scripts\\python.exe tools\\probe_tools.py [--only claude|codex]
결과:  tools\\probe_out\\ 에 원본 stdout/stderr 저장 + 화면에 actions/denials/usage 요약과 Codex 이벤트 종류 통계.
이 스크립트는 pytest가 아니다 — 자동 테스트는 실제 CLI를 절대 호출하지 않는다는 규칙 때문에 사람이 직접 돌린다.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import debate as D  # noqa: E402
import engine.cli as C  # noqa: E402

OUT = Path(__file__).resolve().parent / "probe_out"
OUT.mkdir(exist_ok=True)

# 원본 출력을 파일로도 남긴다 (이벤트 형식이 예상과 다르면 여기서 확인)
_orig = C._popen_stream


def _tee(cmd, prompt, timeout, on_line=None, on_tick=None, cancel=None, cwd=None, env=None):
    rc, out, err, killed = _orig(cmd, prompt, timeout, on_line, on_tick, cancel, cwd, env)
    tag = f"{Path(cmd[0]).stem}_{int(time.time())}"
    (OUT / f"{tag}.cmd.txt").write_text(" ".join(cmd), encoding="utf-8")
    (OUT / f"{tag}.stdout.txt").write_text(out, encoding="utf-8")
    (OUT / f"{tag}.stderr.txt").write_text(err, encoding="utf-8")
    return rc, out, err, killed


C._popen_stream = _tee


def show(name, text, meta):
    print(f"\n===== {name} =====")
    print("text:", text[:300].replace("\n", " ⏎ "))
    print("actions:")
    for a in meta.get("actions") or []:
        print(f"  - {a['tool']}: {a['input'][:100]} → {(a.get('output') or '')[:100].replace(chr(10), ' ⏎ ')}" + (" [실패]" if a.get("error") else ""))
    if meta.get("denials"):
        print("denials:", meta["denials"])
    print("usage:", meta.get("usage"), "| model:", meta.get("model") or meta.get("models"))


only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else "all"
ws = D.new_workspace("프로브")
print("workspace:", ws)
base = dict(claude_model="sonnet", claude_effort="low", codex_model="gpt-5.6-sol", codex_effort="low", timeout=240,
            claude_exe=D.find_claude(), codex_exe=D.find_codex(), workspace=str(ws))

# 1) Claude 웹 검색만
cfg = D.Config(**base, web_search=True)
if only in ("all", "claude"):
  try:
    t, m = D.call_claude(cfg, D.system_rules("general", web=True), "웹 검색으로 Python 최신 안정 버전을 확인해 한 줄로 답하고 출처 URL을 적으십시오.", "probe-web")
    show("Claude 웹 검색", t, m)
  except Exception as e:  # noqa: BLE001
    print("Claude 웹 검색 실패:", e)

# 2) Claude 파일·명령 (허용 목록)
cfg = D.Config(**base, tools=True)
if only in ("all", "claude"):
  try:
    t, m = D.call_claude(cfg, D.system_rules("general", tools=True),
                         "현재 폴더에 hello.py 파일을 만들어 print('hi 42')를 넣고 python hello.py 를 실행해 출력을 그대로 보고하십시오. "
                         "그리고 curl 명령도 한 번 시도해 보십시오(허용되지 않으면 그렇다고 말하십시오).", "probe-tools")
    show("Claude 파일·명령", t, m)
    print("workspace 커밋:", D.workspace_commit(ws, "probe claude"))
  except Exception as e:  # noqa: BLE001
    print("Claude 파일·명령 실패:", e)

# 3) Codex 파일·명령 + 웹 검색 (--json, -c web_search=live)
#    2026-09-17 실기: 샌드박스 안에서 venv 파이썬(uv 트램폴린)이 "did not find executable at <uv 경로>"로 실패.
#    --add-dir 로 venv 와 uv 설치 폴더를 열어 주면 되는지 확인한다 (기본 엔진 경로엔 아직 안 넣음).
cfg = D.Config(**base, tools=True, web_search=True)
if only in ("all", "codex"):
  _orig_args = C.codex_tool_args
  _extra = [str(Path(sys.executable).parents[1]), str(Path(sys.base_prefix))]
  C.codex_tool_args = lambda c: _orig_args(c) + [x for d in _extra for x in ("--add-dir", d)]
  print("codex --add-dir 시험:", _extra)
  try:
    t, m = D.call_codex(cfg, D.system_rules("general", tools=True, web=True) + "\n현재 폴더에 hello2.py 파일을 만들어 print('hi 43')를 넣고 "
                        "python hello2.py 를 실행해 출력을 보고하십시오. 그 다음 웹 검색으로 Python 최신 안정 버전을 출처 URL과 함께 한 줄로 적으십시오.",
                        "probe-codex")
    show("Codex 파일·명령+웹", t, m)
    print("workspace 커밋:", D.workspace_commit(ws, "probe codex"))
  except Exception as e:  # noqa: BLE001
    print("Codex 실패:", e)
  # 파서 조정용: 실제 이벤트 종류 통계와 도구 항목 원문 (가장 최근 codex stdout)
  import json
  latest = sorted(OUT.glob("codex*.stdout.txt"), key=lambda p: p.stat().st_mtime)
  if latest:
    kinds: dict[str, int] = {}
    for line in latest[-1].read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        it = o.get("item") if isinstance(o.get("item"), dict) else {}
        key = f"{o.get('type')}/{it.get('type')}" if it else str(o.get("type"))
        kinds[key] = kinds.get(key, 0) + 1
        if it and it.get("type") not in ("agent_message", "reasoning"):
            print("  raw item:", json.dumps({k: str(v)[:100] for k, v in it.items() if k != "id"}, ensure_ascii=False)[:300])
    print("codex 이벤트 종류:", kinds)

print("\nworkspace 파일:", sorted(p.name for p in ws.iterdir() if p.name != ".git"))
print("원본 출력:", OUT)
