# -*- coding: utf-8 -*-
"""engine.plan — 단계 지시문·종류·모드 프리셋, 계획 생성/검증(3단계·5단계·직접 편집·평가)."""
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
from .cli import *  # noqa: F401,F403
from .contract import *  # noqa: F401,F403


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


def system_rules(mode: str, tools: bool = False, web: bool = False, web_reuse: bool = False) -> str:
    """공통 규칙(도구 허용 여부 반영) + 모드 규칙. 웹이 켜지면 투자 모드의 '최신 시세는 알 수 없다'도 '검색으로 확인하라'로."""
    rules = MODES.get(mode, MODES["general"])["rules"]
    if web:
        rules = rules.replace("최신 시세·뉴스는 알 수 없으므로 '확인 필요' 항목으로 분리하십시오.",
                              "최신 시세·뉴스는 웹 검색으로 확인하고 출처 URL·확인 시각을 적으십시오.")
    return COMMON_RULES_HEAD + tool_rules(tools, web, web_reuse) + COMMON_RULES_TAIL + rules
