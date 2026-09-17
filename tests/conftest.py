# -*- coding: utf-8 -*-
"""공통 픽스처.

원칙: 테스트는 실제 claude/codex CLI를 절대 호출하지 않는다 — 구독 사용량을 쓰지 않고,
로그인 상태(특히 `codex login --device-auth`는 시작만 해도 기존 로그인을 지운다)를 건드리지 않기 위해.
CLI에 닿는 함수는 전부 가짜로 바꾸고, 개인 파일(chats\\, runs\\, ui_settings.json)은 격리·보존한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import debate as D  # noqa: E402

SETTINGS_PATH = ROOT / "ui_settings.json"


def patch_all(monkeypatch, name: str, value) -> None:
    """facade(debate)와 engine.* 모듈 중 같은 이름을 가진 곳을 전부 바꾼다.
    구현이 engine/ 패키지로 나뉘어 같은 함수의 바인딩이 여러 모듈에 있으므로, 호출부가 어디서 찾든 가짜가 보이게."""
    monkeypatch.setattr(D, name, value)
    for mod_name, mod in list(sys.modules.items()):
        if mod_name.startswith("engine.") and mod is not None and hasattr(mod, name):
            monkeypatch.setattr(mod, name, value)


@pytest.fixture
def keep_settings():
    """사이드바를 조작하면 ui_settings.json이 저장되므로 테스트 전후로 원본을 보존한다."""
    backup = SETTINGS_PATH.read_bytes() if SETTINGS_PATH.exists() else None
    yield
    if backup is None:
        SETTINGS_PATH.unlink(missing_ok=True)
    else:
        SETTINGS_PATH.write_bytes(backup)


@pytest.fixture
def isolated(tmp_path, monkeypatch, keep_settings):
    """chats\\·runs\\ 를 임시 폴더로 돌리고, CLI 탐색·로그인 상태·모델 목록·알림음을 가짜로 바꾼다."""
    patch_all(monkeypatch, "CHATS", tmp_path / "chats")
    patch_all(monkeypatch, "IMAGE_DIR", tmp_path / "chats" / "_img")
    patch_all(monkeypatch, "RUNS", tmp_path / "runs")
    # 존재하지 않는 실행 파일명 → 혹시 가짜를 우회해 실제 호출로 가더라도 즉시 실패해 사용량을 쓰지 않는다
    patch_all(monkeypatch, "find_claude", lambda: "claude-fake.exe")
    patch_all(monkeypatch, "find_codex", lambda: "codex-fake.exe")
    patch_all(monkeypatch, "claude_auth_status",
              lambda exe: {"loggedIn": True, "email": "test@example.com", "subscriptionType": "test",
                           "authMethod": "fake", "detail": ""})
    patch_all(monkeypatch, "codex_auth_status", lambda exe: {"loggedIn": True, "detail": "Logged in (fake)"})
    patch_all(monkeypatch, "list_codex_models", lambda exe, timeout=60: [dict(m) for m in D.CODEX_MODELS_FALLBACK])
    try:
        import winsound
        monkeypatch.setattr(winsound, "MessageBeep", lambda *a, **k: None)
    except ImportError:
        pass
    return tmp_path


class FakeStages:
    """`execute_stage` 대역. 단계 종류별 고정 답변을 돌려주고 호출 내역(순서·전달된 기록 길이·프롬프트)을 남긴다.
    실제 프롬프트 조립(`build_prompt`)까지는 그대로 태워서 CLI 직전 경로를 검증한다."""

    def __init__(self, review_ok: bool = False, resolve: bool = True):
        self.review_ok = review_ok   # True면 검토/재검사가 '[지적 없음]' + '추가 수정 불필요' 판정
        self.resolve = resolve       # False면 반박/최종이 [반영/반박 N] 줄을 빼먹는다 (계약 위반 시나리오)
        self.calls: list[dict] = []

    def __call__(self, question, attachments, history, stage, cfg, prior=None, mode="general", plan_len=5,
                 on_delta=None, on_tick=None, cancel=None, max_retries=D.MAX_CONTRACT_RETRIES, compaction=None):
        src, issues = D.open_issues(history) if stage["kind"] in D.RESPOND_KINDS else (None, [])
        prompt = D.build_prompt(question, history, stage, plan_len, prior, attachments, issues, src)
        self.calls.append({"kind": stage["kind"], "who": stage["who"], "history": len(history),
                           "instruction": stage["instruction"], "prompt": prompt})
        verdict = D.VERDICT_OK if self.review_ok else D.VERDICT_NEED
        tags = "[반영 1] 근거를 보강했습니다.\n[반박 2] 이미 처리된 항목입니다.\n" if self.resolve else ""
        code = "\n```python\nprint('hi')\n```\n```python\ndef f(:\n    pass\n```\n" if mode == "code_review" else ""
        n_eval = sum(1 for c in self.calls if c["kind"] == "evaluate")   # 이번 호출 포함
        content = {
            "initial": f"최초 답변입니다.{code}",
            "review": (f"[지적 없음]\n{verdict}" if self.review_ok
                       else f"[지적 1] 근거가 약합니다.\n[지적 2] 예외 처리가 빠졌습니다.\n{verdict}"),
            "rebuttal": f"{tags}수정된 답변입니다.",
            "recheck": (f"[지적 없음]\n{verdict}" if self.review_ok else f"[지적 1] 표현이 아직 모호합니다.\n{verdict}"),
            "final": f"{tags}최종 답변입니다.",
            # 첫 평가는 NEEDS_WORK(재작성 유도), 두 번째부터 PASS
            "evaluate": (f"[지적 1] 결론이 모호합니다.\n{D.EVAL_NEEDS}" if n_eval == 1 else f"[지적 없음]\n{D.EVAL_PASS}"),
        }[stage["kind"]]
        if on_delta:
            on_delta(content)
        if on_tick:
            on_tick(0.1)
        # 실제 CLI meta와 같은 모양의 토큰 정보 (Claude: usage dict + 비용, GPT: 총 토큰만)
        meta = ({"models": ["claude-fake"], "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 0,
                                                     "cache_read_input_tokens": 0, "output_tokens": 200}, "cost_usd": 0.01}
                if stage["who"] == "claude" else {"model": "gpt-fake", "usage": {"total": 500}})
        entry = {"who": stage["who"], "label": stage["label"], "kind": stage["kind"], "content": content,
                 "elapsed": 0.1, "meta": meta, "prompt_chars": len(prompt)}
        if issues:  # execute_stage와 같은 형태로 계약 결과를 붙인다 (재요청은 흉내 내지 않음)
            c = D.check_contract(issues, content)
            c.update({"retries": 0, "source": f"{D.DISPLAY[src['who']]} · {src['label']}"})
            entry["contract"] = c
        evidence = D.check_code_blocks(content, run=False)  # 코드 검사도 실제 경로 (실행은 안 함)
        if evidence:
            entry["evidence"] = evidence
        return entry


class FakeCLI:
    """call_claude / call_codex 대역: 호출 순서대로 responses를 돌려준다 (마지막 응답은 반복). 프롬프트를 기록."""

    def __init__(self, responses: list[str]):
        self.responses, self.calls = responses, []

    def claude(self, cfg, system_prompt, prompt, stage, on_delta=None, on_tick=None, cancel=None, images=None):
        return self._answer(prompt, on_delta)

    def codex(self, cfg, prompt, stage, on_tick=None, cancel=None, images=None):
        return self._answer(prompt, None)

    def _answer(self, prompt, on_delta):
        self.calls.append(prompt)
        text = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if on_delta:
            on_delta(text)
        return text, {"model": "fake"}


@pytest.fixture
def cli(monkeypatch):
    """`f = cli([응답1, 응답2, ...])` 로 두 CLI 호출을 대역으로 바꾼다 (execute_stage의 실제 경로를 태울 때)."""
    def make(responses: list[str]) -> FakeCLI:
        f = FakeCLI(responses)
        patch_all(monkeypatch, "call_claude", f.claude)
        patch_all(monkeypatch, "call_codex", f.codex)
        return f
    return make


@pytest.fixture
def fake_stages(monkeypatch):
    """`fake = fake_stages(review_ok=..., resolve=...)` 로 execute_stage를 바꿔 끼운다."""
    def make(review_ok: bool = False, resolve: bool = True) -> FakeStages:
        fake = FakeStages(review_ok, resolve)
        patch_all(monkeypatch, "execute_stage", fake)
        return fake
    return make
