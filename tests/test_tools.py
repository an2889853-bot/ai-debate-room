# -*- coding: utf-8 -*-
"""도구 허용(웹 검색 / 파일·명령)과 행동 기록: CLI 인자, 이벤트 파서, 규칙 문구, 기록 블록, workspace git 커밋. 실제 CLI 호출 없음."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import debate as D
from conftest import patch_all


# ---- CLI 인자 ------------------------------------------------------------------
def test_claude_tool_args():
    assert D.claude_tool_args(D.Config()) == ["--tools", ""]                       # 기본: 전부 비활성
    web = D.claude_tool_args(D.Config(web_search=True))
    assert web[:2] == ["--tools", "WebSearch,WebFetch"] and web[2:4] == ["--allowedTools", "WebSearch WebFetch"]
    assert "--permission-mode" not in web
    tools = D.claude_tool_args(D.Config(tools=True))
    assert "Bash" in tools[1].split(",") and "WebSearch" not in tools[1]
    allowed = tools[3].split()
    assert "Bash(python" in tools[3] and "Bash(pytest" in tools[3] and "Edit" in allowed and "Bash(rm" not in tools[3]
    assert tools[-2:] == ["--permission-mode", "acceptEdits"]
    both = D.claude_tool_args(D.Config(tools=True, web_search=True))
    assert "WebFetch" in both[1].split(",") and "WebSearch" in both[3].split()
    assert "dangerously" not in " ".join(both)


def test_readonly_tools_for_evaluator():
    """평가자: 읽기·실행·검색은 되지만 Edit/Write 없음, acceptEdits 없음, Codex는 read-only."""
    ro = D.claude_tool_args(D.Config(tools=True, web_search=True, readonly=True))
    assert "Edit" not in ro[1].split(",") and "Write" not in ro[1].split(",") and "Bash" in ro[1].split(",")
    assert "Edit" not in ro[3].split() and "Write" not in ro[3].split() and "Read" in ro[3].split() and "WebSearch" in ro[3].split()
    assert "--permission-mode" not in ro
    assert D.codex_tool_args(D.Config(tools=True, readonly=True))[:2] == ["--sandbox", "read-only"]


def test_web_allowed_scopes():
    for kind in ("initial", "review", "rebuttal", "recheck", "final", "evaluate"):
        assert D.web_allowed("all", kind)
    assert D.web_allowed("initial", "initial") and not D.web_allowed("initial", "evaluate") and not D.web_allowed("initial", "review")
    assert D.web_allowed("initial_eval", "initial") and D.web_allowed("initial_eval", "evaluate") and not D.web_allowed("initial_eval", "final")
    assert D.web_allowed("???", "review")   # 모르는 값은 전체 허용 (안전 쪽이 아니라 기능 쪽 — 설정 파일 손상 시 검색이 조용히 꺼지지 않게)
    assert list(D.WEB_SCOPES) == ["initial_eval", "initial", "all"]


def test_codex_tool_args(tmp_path):
    off = D.codex_tool_args(D.Config())
    assert off[:2] == ["--sandbox", "read-only"] and off[off.index("-C") + 1] == str(D.SANDBOX)
    assert "web_search=live" not in off and "--json" not in off and "--search" not in off
    on = D.codex_tool_args(D.Config(tools=True, web_search=True, workspace=str(tmp_path)))
    assert on[:2] == ["--sandbox", "workspace-write"] and on[on.index("-C") + 1] == str(tmp_path)
    assert on[on.index("-c") + 1] == "web_search=live" and "--json" in on and "danger" not in " ".join(on)
    assert "--search" not in on, "--search는 대화형 CLI 옵션 — codex exec는 거부한다 (실기 확인)"
    assert "--json" in D.codex_tool_args(D.Config(web_search=True))
    assert D.codex_tool_args(D.Config(tools=True))[:2] == ["--sandbox", "workspace-write"]   # workspace 없으면 sandbox\\


def test_cwd_for_and_env(tmp_path):
    assert D.cwd_for(D.Config()) == D.SANDBOX
    assert D.cwd_for(D.Config(tools=True, workspace=str(tmp_path))) == tmp_path
    assert D.cwd_for(D.Config(tools=False, workspace=str(tmp_path))) == D.SANDBOX     # tools 꺼지면 workspace 무시
    env = D.clean_env(tools=True)
    assert "CLAUDECODE" not in env
    venv = D.ROOT / ".venv" / "Scripts"
    if venv.exists():
        assert env["PATH"].startswith(str(venv))
        assert not D.clean_env()["PATH"].startswith(str(venv))


# ---- 이벤트 파서 ----------------------------------------------------------------
CLAUDE_EVENTS = [
    {"type": "system", "subtype": "init"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "확인해 보겠습니다."},
                                                   {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "python hello.py"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "hi 42\n"}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu2", "name": "WebSearch", "input": {"query": "python latest"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu2",
                                              "content": [{"type": "text", "text": "Python 3.14 - python.org"}]}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu3", "name": "Bash", "input": {"command": "rm -rf /"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu3", "content": "permission denied", "is_error": True}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "python hello.py"}}]}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}},
    {"type": "result", "subtype": "success", "result": "hi 42 입니다."},
]


def test_claude_event_parser_collects_tool_use_and_results():
    p = D.ClaudeEventParser()
    for e in CLAUDE_EVENTS:
        p.feed(e)
    assert [(a["tool"], a["input"], a["output"].strip(), a["error"]) for a in p.actions] == [
        ("Bash", "python hello.py", "hi 42", False),
        ("WebSearch", "python latest", "Python 3.14 - python.org", False),
        ("Bash", "rm -rf /", "permission denied", True)]                            # 중복 id(tu1)는 한 번만


def test_tidy_command_and_relative_paths():
    ws = r"C:\Users\LG\ai-debate-room\workspace\20260917_093828_프로브"
    # 실기 출력 그대로: Claude는 cd "<ws>" && …, Codex는 powershell.exe -Command '…'
    assert D._tidy_command(f'cd "{ws}" && python hello.py', ws) == "python hello.py"
    assert D._tidy_command("\"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe\" -Command 'python hello2.py'", ws) == "python hello2.py"
    assert D._tidy_command('"C:\\x\\powershell.exe" -Command "Get-Content -LiteralPath .\\hello2.py; py hello2.py"') == "Get-Content -LiteralPath .\\hello2.py; py hello2.py"
    assert D._tidy_command("bash -lc 'ls -la'") == "ls -la"
    assert D._tidy_command("python plain.py") == "python plain.py"
    assert D._rel(ws + "\\hello.py", ws) == "hello.py" and D._rel(r"C:\other\a.py", ws) == r"C:\other\a.py" and D._rel("rel.py", ws) == "rel.py"
    p = D.ClaudeEventParser(ws)
    p.feed({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "a", "name": "Write", "input": {"file_path": ws + "\\hello.py", "content": "..."}},
        {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": f'cd "{ws}" && python hello.py'}}]}})
    assert [(a["tool"], a["input"]) for a in p.actions] == [("Write", "hello.py"), ("Bash", "python hello.py")]


CODEX_REAL = "\n".join(json.dumps(o, ensure_ascii=False) for o in [   # 2026-09-17 실기 출력 축약
    {"type": "item.started", "item": {"id": "i1", "type": "file_change", "changes": [{"path": "C:\\ws\\hello2.py", "kind": "add"}], "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "i1", "type": "file_change", "changes": [{"path": "C:\\ws\\hello2.py", "kind": "add"}], "status": "completed"}},
    {"type": "item.started", "item": {"id": "i2", "type": "command_execution", "command": "\"C:\\W\\powershell.exe\" -Command 'python hello2.py'", "aggregated_output": "", "exit_code": None, "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "i2", "type": "command_execution", "command": "\"C:\\W\\powershell.exe\" -Command 'python hello2.py'", "aggregated_output": "did not find executable", "exit_code": "1", "status": "failed"}},
    {"type": "item.started", "item": {"id": "i3", "type": "web_search", "query": "", "action": {"type": "other"}}},
    {"type": "item.completed", "item": {"id": "i3", "type": "web_search", "query": "", "action": {"type": "search", "query": "latest python"}}},
    {"type": "item.completed", "item": {"id": "i4", "type": "command_execution", "command": "\"C:\\W\\powershell.exe\" -Command 'Get-Date'", "aggregated_output": "2026-09-17", "exit_code": 0, "status": "completed"}},
    {"type": "item.completed", "item": {"id": "i5", "type": "agent_message", "text": "끝"}},
    {"type": "turn.completed", "usage": {"input_tokens": 89043, "cached_input_tokens": 78336, "output_tokens": 868}},
])


def test_parse_codex_events_real_format():
    actions, usage, last = D.parse_codex_events(CODEX_REAL, "C:\\ws")
    assert [(a["tool"], a["input"], a["error"]) for a in actions] == [
        ("Edit", "hello2.py", False), ("Bash", "python hello2.py", True), ("WebSearch", "latest python", False), ("Bash", "Get-Date", False)]
    assert usage == {"in": 89043, "out": 868, "total": 89911} and last == "끝"


CODEX_JSONL = "\n".join(json.dumps(o, ensure_ascii=False) for o in [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "item.completed", "item": {"id": "i1", "type": "reasoning", "text": "..."}},
    {"type": "item.completed", "item": {"id": "i2", "type": "command_execution", "command": "python hello2.py",
                                        "aggregated_output": "hi 43\n", "exit_code": 0}},
    {"type": "item.completed", "item": {"id": "i3", "type": "web_search", "query": "python latest"}},
    {"type": "item.completed", "item": {"id": "i4", "type": "file_change", "changes": [{"path": "hello2.py", "kind": "add"}]}},
    {"type": "item.completed", "item": {"id": "i5", "type": "command_execution", "command": "false", "aggregated_output": "", "exit_code": 1}},
    {"type": "item.completed", "item": {"id": "i6", "type": "agent_message", "text": "hi 43 이 출력됐습니다."}},
    {"type": "turn.completed", "usage": {"input_tokens": 1200, "cached_input_tokens": 100, "output_tokens": 300}},
])


def test_parse_codex_events():
    actions, usage, last = D.parse_codex_events(CODEX_JSONL + "\nnot json\n")
    assert [(a["tool"], a["input"], a["error"]) for a in actions] == [
        ("Bash", "python hello2.py", False), ("WebSearch", "python latest", False), ("Edit", "hello2.py", False), ("Bash", "false", True)]
    assert actions[0]["output"] == "hi 43\n"
    assert usage == {"in": 1200, "out": 300, "total": 1500} and last == "hi 43 이 출력됐습니다."
    assert D.parse_codex_events("") == ([], None, "")
    e = {"who": "gpt", "meta": {"usage": usage}}
    assert D.usage_of(e) == {"in": 1200, "out": 300, "total": 1500, "cost_usd": None} and D.usage_line(e) == "1.2k→300 토큰"


# ---- 규칙 문구 -------------------------------------------------------------------
def test_system_rules_variants():
    off = D.system_rules("general")
    assert "어떤 도구도 사용하지 말고" in off and off == D.COMMON_RULES + D.MODES["general"]["rules"]
    on = D.system_rules("invest", tools=True, web=True)
    assert "어떤 도구도 사용하지 말고" not in on and "허용된 명령" in on and "출처 URL" in on and "[프로그램 기록" in on
    assert "웹 검색으로 확인하고" in on and "알 수 없으므로" not in on
    web_only = D.system_rules("general", web=True)
    assert "파일 읽기·명령 실행은 허용되지 않습니다" in web_only and "웹 검색·페이지 읽기가 허용" in web_only
    assert "웹 검색은 허용되지 않습니다" in D.system_rules("general", tools=True)
    reuse = D.system_rules("general", tools=False, web=False, web_reuse=True)
    assert "이번 단계는 웹 검색이 꺼져 있습니다" in reuse and "검색 결과(출처 URL 포함)를 근거로" in reuse and "어떤 도구도" not in reuse


def test_execute_stage_applies_web_scope_and_evaluator_readonly(cli, monkeypatch):
    seen = []
    f = cli(["답"])
    orig = f.claude

    def spy(cfg, system_prompt, prompt, stage, *a, **kw):
        seen.append((stage, cfg.web_search, cfg.readonly, cfg.tools, "웹 검색이 꺼져" in system_prompt))
        return orig(cfg, system_prompt, prompt, stage, *a, **kw)
    patch_all(monkeypatch, "call_claude", spy)
    cfg = D.Config(tools=True, web_search=True, web_scope="initial_eval")
    plan = D.plan_stages(1, "general", "claude", "claude", None, 5, evaluate=True)   # 전부 claude가 쓰도록 final_who=claude
    plan = [dict(s, who="claude") for s in plan]
    for s in plan:
        D.execute_stage("q", [], [], s, cfg, plan_len=len(plan))
    by_kind = {stage.split(" · ")[1]: (web, ro, tools, reuse) for stage, web, ro, tools, reuse in seen}
    assert by_kind["Initial"] == (True, False, True, False)
    assert by_kind["Review"] == (False, False, True, True) and by_kind["FINAL"] == (False, False, True, True)
    assert by_kind["Eval"] == (True, True, True, False)                       # 평가자: 웹은 되고 쓰기는 금지
    seen.clear()
    D.execute_stage("q", [], [], plan[0], D.Config(tools=True, web_search=False), plan_len=1)
    assert seen[0][1] is False and seen[0][4] is False                        # 웹을 아예 껐으면 '재사용' 문구도 없다
    seen.clear()
    D.execute_stage("q", [], [], plan[1], D.Config(web_search=True, web_scope="all"), plan_len=1)
    assert seen[0][1] is True


# ---- 기록 블록 -------------------------------------------------------------------
def test_render_actions_and_transcript():
    h = {"who": "claude", "label": "Initial", "kind": "initial", "content": "답",
         "actions": [{"tool": "Bash", "input": "python hello.py", "output": "hi 42\n", "error": False},
                     {"tool": "WebSearch", "input": "python latest", "output": "", "error": False}],
         "denials": [{"tool_name": "Bash", "tool_input": {"command": "dir"}}],
         "workspace": {"changed": ["hello.py"], "commit": "abc1234"}}
    text = D.render_actions(h)
    assert text.startswith("[프로그램 기록 · Claude · Initial의 도구 사용")
    assert "- Bash: python hello.py → hi 42" in text and "- WebSearch: python latest" in text
    assert "⚠ 거부됨 (허용 목록 밖): Bash" in text and "작업 폴더 변경: hello.py (git 커밋 abc1234)" in text
    assert D.actions_summary(h["actions"], h["denials"]) == "도구 2회 — Bash 1 · WebSearch 1 · 거부 1"
    assert D.actions_summary([]) == "도구 사용 없음"
    t = D.render_transcript("q", [h])
    assert "[프로그램 기록 · Claude · Initial" in t and t.index("[Claude · Initial]") < t.index("[프로그램 기록")
    assert "[프로그램 기록" not in D.render_transcript("q", [{"who": "claude", "label": "Initial", "content": "x"}])


def test_execute_stage_records_actions_and_next_stage_sees_them(cli, monkeypatch):
    f = cli(["첫 답", "[지적 없음]\n" + D.VERDICT_NEED])
    orig = f.claude

    def with_actions(cfg, system_prompt, prompt, stage, *a, **kw):
        text, meta = orig(cfg, system_prompt, prompt, stage, *a, **kw)
        return text, dict(meta, actions=[{"tool": "Bash", "input": "python x.py", "output": "ok", "error": False}], denials=[])
    patch_all(monkeypatch, "call_claude", with_actions)
    plan = D.plan_stages(1, "general", "claude", None, None, 3)
    e0 = D.execute_stage("q", [], [], plan[0], D.Config(tools=True), plan_len=3)     # workspace 없음 → 커밋 없음
    assert e0["actions"][0]["input"] == "python x.py" and "denials" not in e0 and "workspace" not in e0
    assert "actions" not in e0["meta"] and "denials" not in e0["meta"]
    e1 = D.execute_stage("q", [], [e0], plan[1], D.Config(), plan_len=3)
    assert "[프로그램 기록 · Claude · Initial의 도구 사용" in f.calls[1] and "python x.py → ok" in f.calls[1]


# ---- workspace ------------------------------------------------------------------
@pytest.mark.skipif(not D.git_exe(), reason="git 없음")
def test_workspace_create_and_commit(tmp_path, monkeypatch):
    patch_all(monkeypatch, "WORKSPACES", tmp_path / "ws")
    ws = D.new_workspace("컨베이어 기동 회로 검토")
    assert ws.parent == tmp_path / "ws" and (ws / ".git").exists() and "컨베이어" in ws.name
    assert D.workspace_commit(ws, "빈 상태") == ([], None)
    (ws / "hello.py").write_text("print(1)\n", encoding="utf-8")
    changed, rev = D.workspace_commit(ws, "Claude · Initial")
    assert changed == ["hello.py"] and rev and len(rev) >= 7
    assert D.workspace_commit(ws, "다시") == ([], None)
    assert D.workspace_commit(tmp_path / "nogit", "x") == ([], None)


@pytest.mark.skipif(not D.git_exe(), reason="git 없음")
def test_execute_stage_commits_workspace_changes(cli, tmp_path, monkeypatch):
    patch_all(monkeypatch, "WORKSPACES", tmp_path / "ws")
    ws = D.new_workspace("q")
    (ws / "a.txt").write_text("모델이 만든 파일", encoding="utf-8")   # 가짜 CLI 대신 미리 만들어 둔 변경
    cli(["답"])
    stage = D.plan_stages(1, "general", "claude", None, None, 3)[0]
    e = D.execute_stage("q", [], [], stage, D.Config(tools=True, workspace=str(ws)), plan_len=3)
    assert e["workspace"]["changed"] == ["a.txt"] and e["workspace"]["commit"]
    assert "작업 폴더 변경: a.txt" in D.render_actions(e)


def test_run_debate_creates_workspace_only_when_tools(cli, tmp_path, monkeypatch):
    patch_all(monkeypatch, "WORKSPACES", tmp_path / "ws")
    cli(["첫", "[지적 없음]\n" + D.VERDICT_NEED, "최종"])
    run = D.run_debate("질문", D.Config(tools=True), stage_count=3)
    assert run["workspace"] and Path(run["workspace"]).parent == tmp_path / "ws" and run["config"]["workspace"] == run["workspace"]
    cli(["첫", "[지적 없음]\n" + D.VERDICT_NEED, "최종"])
    assert D.run_debate("질문", D.Config(), stage_count=3)["workspace"] is None
