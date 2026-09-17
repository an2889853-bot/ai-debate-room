# -*- coding: utf-8 -*-
"""사이클 3: 실시간 도구 파서(Codex/Claude on_action), 요약에 도구 기록 보존, workspace 정리·대화 삭제 연동, 점검 호출 도구 끔."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import debate as D
from conftest import patch_all


def _line(o: dict) -> str:
    return json.dumps(o, ensure_ascii=False)


def test_codex_live_parser_updates_running_to_done():
    seen = []
    p = D.CodexLiveParser("C:\\ws", on_action=lambda acts: seen.append([dict(a) for a in acts]))
    p.feed("not json")
    p.feed(_line({"type": "item.started", "item": {"id": "c1", "type": "command_execution",
                                                    "command": "\"C:\\W\\powershell.exe\" -Command 'python x.py'", "status": "in_progress"}}))
    assert p.actions == [{"tool": "Bash", "input": "python x.py", "output": "", "error": False, "running": True}]
    p.feed(_line({"type": "item.completed", "item": {"id": "c1", "type": "command_execution", "command": "python x.py",
                                                      "aggregated_output": "ok\n", "exit_code": 0, "status": "completed"}}))
    assert p.actions[0]["running"] is False and p.actions[0]["output"] == "ok\n" and len(p.actions) == 1
    p.feed(_line({"type": "item.completed", "item": {"id": "s1", "type": "web_search", "query": "", "action": {"type": "search", "query": "q"}}}))
    p.feed(_line({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "무시"}}))
    assert [a["tool"] for a in p.actions] == ["Bash", "WebSearch"] and len(seen) == 3


def test_claude_parser_on_action_callback_via_call_path(monkeypatch):
    """call_claude의 on_line 경로: assistant/user 이벤트마다 on_action에 누적 목록을 넘긴다 (프로세스 대신 on_line을 직접 구동)."""
    captured = {}

    def fake_popen(cmd, prompt, timeout, on_line=None, on_tick=None, cancel=None, cwd=None, env=None):
        captured["on_line"] = on_line
        for o in [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
                  {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py"}]}},
                  {"type": "result", "subtype": "success", "result": "끝", "usage": {}, "modelUsage": {}}]:
            on_line(_line(o))
        return 0, "", "", None
    patch_all(monkeypatch, "_popen_stream", fake_popen)
    seen = []
    text, meta = D.call_claude(D.Config(tools=True, claude_exe="x"), "rules", "prompt", "st", on_action=lambda a: seen.append([dict(x) for x in a]))
    assert text == "끝" and meta["actions"] == [{"tool": "Bash", "input": "ls", "output": "a.py", "error": False}]
    assert len(seen) == 2 and seen[-1][0]["output"] == "a.py"
    assert captured["on_line"] is not None


def test_execute_stage_passes_on_action(cli, monkeypatch):
    got = {}
    f = cli(["답"])
    orig = f.claude

    def spy(cfg, system_prompt, prompt, stage, on_delta=None, on_tick=None, cancel=None, images=None, on_action=None):
        got["on_action"] = on_action
        return orig(cfg, system_prompt, prompt, stage)
    patch_all(monkeypatch, "call_claude", spy)
    stage = D.plan_stages(1, "general", "claude", None, None, 3)[0]
    cb = lambda a: None  # noqa: E731
    D.execute_stage("q", [], [], stage, D.Config(), plan_len=3, on_action=cb)
    assert got["on_action"] is cb


def test_summary_input_keeps_tool_records(cli, monkeypatch):
    f = cli(["요약"])
    entries = [{"who": "claude", "label": "Initial", "kind": "initial", "content": "답",
                "actions": [{"tool": "WebSearch", "input": "latest python", "output": "3.14.7 python.org", "error": False}]},
               {"who": "gpt", "label": "Review", "kind": "review", "content": "검토"}]
    D.summarize_entries(entries, D.Config())
    assert "[프로그램 기록 · Claude · Initial의 도구 사용" in f.calls[0] and "latest python" in f.calls[0]
    assert "[프로그램 기록" in D.SUMMARY_RULES


@pytest.mark.skipif(not D.git_exe(), reason="git 없음")
def test_workspace_listing_and_deletion(isolated, monkeypatch):
    ws_root = isolated / "ws"
    linked = D.new_workspace("연결됨")
    orphan = D.new_workspace("고아")
    (orphan / "junk.txt").write_text("x", encoding="utf-8")
    conv = D.new_conversation("연결된 대화")
    conv["workspace"] = str(linked)
    conv["rounds"].append({"question": "q", "stages": [], "status": "done", "workspace": str(linked)})
    jp, _ = D.save_conversation(conv)
    wss = {w["name"]: w for w in D.list_workspaces()}
    assert wss[linked.name]["linked"] and not wss[orphan.name]["linked"] and wss[orphan.name]["size"] >= 1
    # 밖의 폴더는 거부
    outside = isolated / "outside"
    outside.mkdir()
    assert D.delete_workspace(outside) is False and outside.exists()
    assert D.delete_workspace(orphan) is True and not orphan.exists()
    # 대화 삭제 → 연결된 작업 폴더도 삭제
    D.delete_conversation(jp)
    assert not Path(jp).exists() and not linked.exists()
    assert D.list_workspaces() == []


def test_check_uses_no_tools_in_console(monkeypatch):
    seen = {}
    patch_all(monkeypatch, "check_clis", lambda cfg: seen.update(tools=cfg.tools, web=cfg.web_search) or True)
    patch_all(monkeypatch, "find_claude", lambda: "c")
    patch_all(monkeypatch, "find_codex", lambda: "x")
    assert D.main(["--check"]) == 0
    assert seen == {"tools": False, "web": False}
