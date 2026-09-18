# -*- coding: utf-8 -*-
"""Claude 모델 자동 대체: 429 credits_required 면 opus → sonnet 으로 재시도, 다른 오류는 그대로 실패. _popen_stream 대역."""
from __future__ import annotations

import json

import pytest

import debate as D
from conftest import patch_all


def _result(is_error: bool = False, code: str | None = None, text: str = "OK", model: str = "claude-fable-5-1") -> str:
    o = {"type": "result", "subtype": "success", "is_error": is_error, "result": text, "usage": {}, "modelUsage": {model: {}}}
    if code:
        o["api_error_code"], o["api_error_status"] = code, 429
    return json.dumps(o)


def _fake_popen(responses: list[str], cmds: list[list[str]]):
    def fake(cmd, prompt, timeout, on_line=None, on_tick=None, cancel=None, cwd=None, env=None):
        cmds.append(list(cmd))
        line = responses.pop(0)
        if on_line:
            on_line(line)
        return 0, line + "\n", "", None
    return fake


def _model_of(cmd: list[str]) -> str:
    return cmd[cmd.index("--model") + 1]


def test_credits_required_falls_back_to_opus(monkeypatch):
    cmds: list[list[str]] = []
    patch_all(monkeypatch, "_popen_stream", _fake_popen(
        [_result(True, "credits_required", "Fable 5.1 requires usage credits. Switch to another model"), _result(model="claude-opus-5")], cmds))
    text, meta = D.call_claude(D.Config(claude_model="fable", claude_exe="x"), "r", "p", "st", on_delta=lambda t: None)
    assert text == "OK" and meta["resolve_note"] == "fable 크레딧 필요 → opus로 대체"
    assert [_model_of(c) for c in cmds] == ["fable", "opus"]


def test_falls_through_to_sonnet(monkeypatch):
    cmds: list[list[str]] = []
    patch_all(monkeypatch, "_popen_stream", _fake_popen(
        [_result(True, "credits_required"), _result(True, "credits_required"), _result(model="claude-sonnet-5")], cmds))
    _, meta = D.call_claude(D.Config(claude_model="fable", claude_exe="x"), "r", "p", "st", on_delta=lambda t: None)
    assert meta["resolve_note"] == "fable → opus 크레딧 필요 → sonnet로 대체" and [_model_of(c) for c in cmds] == ["fable", "opus", "sonnet"]


def test_all_models_need_credits_raises(monkeypatch):
    cmds: list[list[str]] = []
    patch_all(monkeypatch, "_popen_stream", _fake_popen([_result(True, "credits_required")] * 3, cmds))
    with pytest.raises(D.CLIError) as ei:
        D.call_claude(D.Config(claude_model="fable", claude_exe="x"), "r", "p", "st", on_delta=lambda t: None)
    assert "모두 크레딧 필요" in str(ei.value) and len(cmds) == 3


def test_requested_fallback_model_is_not_retried_twice(monkeypatch):
    cmds: list[list[str]] = []
    patch_all(monkeypatch, "_popen_stream", _fake_popen([_result(True, "credits_required"), _result()], cmds))
    _, meta = D.call_claude(D.Config(claude_model="opus", claude_exe="x"), "r", "p", "st", on_delta=lambda t: None)
    assert [_model_of(c) for c in cmds] == ["opus", "sonnet"] and "opus 크레딧 필요 → sonnet" in meta["resolve_note"]


def test_other_errors_do_not_fall_back(monkeypatch):
    cmds: list[list[str]] = []
    patch_all(monkeypatch, "_popen_stream", _fake_popen(
        [json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom"})], cmds))
    with pytest.raises(D.CLIError) as ei:
        D.call_claude(D.Config(claude_model="fable", claude_exe="x"), "r", "p", "st", on_delta=lambda t: None)
    assert len(cmds) == 1 and not isinstance(ei.value, D.CreditsRequired)


def test_success_has_no_note(monkeypatch):
    cmds: list[list[str]] = []
    patch_all(monkeypatch, "_popen_stream", _fake_popen([_result()], cmds))
    _, meta = D.call_claude(D.Config(claude_model="fable", claude_exe="x"), "r", "p", "st", on_delta=lambda t: None)
    assert "resolve_note" not in meta and len(cmds) == 1
