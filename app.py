"""AI Debate Room - Streamlit 채팅 UI

실행 (프로젝트 폴더에서):
  start_ui.cmd  또는  .venv\\Scripts\\python.exe -m streamlit run app.py

구조:
- 사이드바: 대화 목록/새 대화/삭제/내려받기, 모드, 라운드 수·조기 종료·단계마다 멈춤, 모델·effort, 공통 설정, CLI 점검
- 본문: 저장된 라운드들 → 진행 중인 라운드(스트리밍) → 맨 아래 채팅 입력창(📎 첨부)
- 단계 실행은 백그라운드 스레드에서 돌고, 화면은 0.5초마다 부분 갱신(st.fragment)된다.
  그래서 사이드바를 만져도 진행 중인 단계가 끊기지 않는다. 중단 버튼은 CLI 프로세스를 죽인다.
- 설정은 ui_settings.json 에, 대화는 chats\\<날짜_시각_주제>.json/.md 에 저장(자동 저장 끌 수 있음).
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import debate as D

st.set_page_config(page_title="AI Debate Room", page_icon="🗣️", layout="wide")

NAME = {"claude": "Claude", "gpt": "GPT", "user": "사용자"}
AVATAR = {"user": "🧑", "claude": "🟠", "gpt": "🟢"}
CLI_OF = {"claude": "claude", "gpt": "codex"}
SETTINGS_PATH = D.ROOT / "ui_settings.json"
DEFAULT_SETTINGS = {
    "claude_model": "fable", "claude_effort": "xhigh",      # fable 별칭 = 최신 Fable 자동 추적
    "codex_model": D.CODEX_AUTO, "codex_effort": "xhigh",   # auto = 카탈로그 최상위 모델 자동 선택
    "timeout": 900, "mode": "general", "stage_count": D.DEFAULT_STAGE_COUNT, "rounds": 1, "early_stop": True,
    "pause_each": False, "autosave": True, "beep": True, "run_code": False, "evaluate": True, "eval_revise": True,
    "tool_budget": 8,
    "compact_chars": 60000, "web_search": False, "tools": False,  # 공유판 기본 꺼짐(2026-09-18). 켜면 ui_settings.json에 저장돼 유지됨
    "web_scope": "initial_eval",
    # 순서: first=먼저 답하는 AI, final_who=최종 정리 AI("same"=먼저 답한 AI), use_custom=표로 직접 편집
    "first": "claude", "final_who": "same", "use_custom": False,
    "custom_plan": [["claude", "initial"], ["gpt", "review"], ["claude", "rebuttal"], ["gpt", "recheck"], ["claude", "final"]],
}
KO2WHO = {"Claude": "claude", "GPT": "gpt"}
STAGE_COUNT_LABEL = {3: "3단계 (최초 → 검토 → 최종)", 5: "5단계 (최초 → 검토 → 반박 → 재검사 → 최종)"}
KO2KIND = {v: k for k, v in D.KIND_KO.items()}

st.markdown(
    """
    <style>
      .final-title { font-weight: 700; font-size: 1.05rem; color: #d98a00; margin-bottom: 6px; }
      .stage-meta { color: #888; font-size: 0.85rem; font-weight: 400; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------------------
# 설정 파일 / 세션 상태
# ----------------------------------------------------------------------------
def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update({k: v for k, v in json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).items() if k in s})
    except (OSError, ValueError):
        pass
    return s


def save_settings(s: dict) -> None:
    try:
        SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


ss = st.session_state
if "settings" not in ss:
    ss.settings = load_settings()
if "conv" not in ss:
    ss.conv = None
    if ss.settings["autosave"]:  # 새로고침/재시작 후 마지막 대화 자동 복원
        recent = [c for c in D.list_conversations() if not c["legacy"]]
        if recent:
            try:
                ss.conv = D.load_conversation(recent[0]["path"])
            except (OSError, ValueError):
                ss.conv = None
if "active" not in ss:
    ss.active = None     # 진행 중인 라운드
if "live" not in ss:
    ss.live = None       # 진행 중인 단계의 실시간 버퍼(백그라운드 스레드와 공유)
if "pending_delete" not in ss:
    ss.pending_delete = None
for _k in ("auth", "login_flow", "pending_logout", "exes"):
    if _k not in ss:
        ss[_k] = None


def get_exes() -> dict:
    if ss.exes is None:
        try:
            ss.exes = {"claude": D.find_claude(), "codex": D.find_codex()}
        except FileNotFoundError as e:
            ss.exes = {"error": str(e)}
    return ss.exes


AUTH_TTL = 120  # 초. 로그인 상태 캐시 유효 시간 — 터미널에서 로그인해도 이 시간 안에 화면에 반영됨


def get_auth(force: bool = False) -> dict:
    """두 CLI의 로그인 상태(세션 캐시, AUTH_TTL 초마다 자동 재조회). force=True면 즉시 다시 조회."""
    stale = ss.auth is None or force or (time.time() - ss.auth.get("_ts", 0) > AUTH_TTL)
    if stale:
        ex = get_exes()
        missing = {"loggedIn": False, "detail": ex.get("error", "")}
        ss.auth = {"claude": D.claude_auth_status(ex["claude"]) if "claude" in ex else missing,
                   "codex": D.codex_auth_status(ex["codex"]) if "codex" in ex else missing,
                   "_ts": time.time()}
    return ss.auth


@st.cache_data(ttl=600, show_spinner=False)
def load_stats(keys: tuple) -> dict:
    """저장된 대화 전체 통계. keys=(경로, 수정 시각) 튜플 — 파일이 바뀌면 다시 계산."""
    convs = []
    for path, _ in keys:
        try:
            convs.append(D.load_conversation(path))
        except (OSError, ValueError):
            pass
    return D.stats(convs)


@st.cache_data(ttl=3600, show_spinner="Codex 모델 목록 조회 중...")
def load_codex_models() -> list[dict]:
    try:
        exe = D.find_codex()
    except FileNotFoundError:
        exe = ""
    return D.list_codex_models(exe)


def beep() -> None:
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------------
# 렌더링 헬퍼
# ----------------------------------------------------------------------------
def used_models(e: dict) -> str:
    meta = e.get("meta") or {}
    if e["who"] == "gpt":
        return f"model: {meta['model']} · effort: {meta.get('reasoning_effort') or '?'}" if meta.get("model") else ""
    models = [m for m in (meta.get("models") or []) if "haiku" not in m]  # haiku는 내부 보조용
    return f"model: {', '.join(models)}" if models else ""


def config_line(cfg: dict, mode: str, stage_count: int) -> str:
    return (f"{D.MODES.get(mode, D.MODES['general'])['name']} · {stage_count}단계 · "
            f"Claude {cfg.get('claude_model') or '기본'}/{cfg.get('claude_effort') or '기본 effort'} · "
            f"GPT {cfg.get('codex_model') or '기본'}/{cfg.get('codex_effort') or '기본 effort'}")


def count_debate_stages(kinds) -> int:
    """설정상 토론 단계 수: 평가 단계와 재작성된 FINAL(FINAL 2)은 세지 않는다."""
    kinds = list(kinds)
    return sum(1 for k in kinds if k != "evaluate") - max(0, kinds.count("final") - 1)


def round_stage_count(run: dict) -> int:
    """저장된 라운드의 단계 수. 계획이 저장돼 있으면 거기서 세고, 아니면(구 기록) rounds로 역산."""
    steps = run.get("plan_steps")
    if steps:
        return count_debate_stages(k for _, k in steps)
    return int(run.get("stage_count") or int(run.get("rounds", 1)) * 2 + 3)


def render_user(question: str, attachments: list[dict], cfg: dict, mode: str, stage_count: int) -> None:
    with st.chat_message("user", avatar=AVATAR["user"]):
        st.markdown(question)
        images = [a for a in (attachments or []) if a.get("kind") == "image"]
        for a in attachments or []:
            if a.get("kind") == "image":
                continue
            extra = ", 일부만 전달" if a.get("truncated") else ""
            st.caption(f"📎 **{a['name']}** ({a.get('chars', 0):,}자{extra})")
            if a.get("warning"):
                st.warning(f"{a['name']}: {a['warning']}")
        for a in images:
            if a.get("warning"):
                st.caption(f"ℹ️ {a['warning']}")
        if images:  # 이미지는 한 줄에 4장씩 작게
            for row in range(0, len(images), 4):
                cols = st.columns(4)
                for col, a in zip(cols, images[row:row + 4]):
                    with col:
                        if a.get("path") and Path(a["path"]).exists():
                            st.image(a["path"], width="stretch", caption=f"📎 {a['name']}")
                        else:
                            st.caption(f"📎 {a['name']} (이미지 파일 없음)")
        st.caption(config_line(cfg, mode, stage_count))


def render_login_flow(flow: "D.LoginFlow") -> None:
    label = "Claude" if flow.cli == "claude" else "Codex"
    if flow.success or flow.finished():
        # 종료 코드와 무관하게 실제 상태를 다시 조회해 판단한다 (device-auth가 0이 아닌 코드로 끝나도 로그인은 됐을 수 있음)
        auth_now = get_auth(force=True)
        if flow.success or auth_now[flow.cli]["loggedIn"]:
            flow.cancel()  # 성공 메시지 뒤에도 프로세스가 남아 있을 수 있음
            ss.login_flow = None
            st.toast(f"✅ {label} 로그인 완료")
            st.rerun()
    if flow.failed:
        st.error(f"{label} 로그인 실패 또는 취소됨\n\n```\n{flow.text[-400:]}\n```")
        if st.button("닫기", key=f"close_login_{flow.cli}"):
            ss.login_flow = None
            get_auth(force=True)
            st.rerun()
        return
    st.info(f"⏳ {label} 로그인 진행 중 ({time.time() - flow.started:.0f}s)")
    if flow.url:
        st.link_button("🌐 로그인 페이지 열기", flow.url, width="stretch")
    else:
        st.caption("로그인 URL 대기 중...")
    if flow.cli == "codex":
        if flow.code:
            st.markdown("브라우저에 이 일회용 코드를 입력하세요 (15분 내):")
            st.code(flow.code, language=None)
        st.caption("코드 입력이 끝나면 자동으로 감지됩니다.")
    else:
        st.caption("브라우저가 자동으로 열립니다. 로그인하면 보통 자동으로 완료되고, 코드가 표시되면 아래에 붙여넣으세요.")
        code = st.text_input("코드 붙여넣기 (표시된 경우만)", key="claude_code_input")
        if st.button("코드 보내기", key="send_code", disabled=not code.strip()):
            flow.send_code(code)
            st.rerun()
    if st.button("취소", key=f"cancel_login_{flow.cli}"):
        flow.cancel()
        ss.login_flow = None
        get_auth(force=True)
        st.rerun()


def render_evidence_ui(e: dict) -> None:
    """코드 블록 검사 결과 (문법/파싱/실행). 실패가 하나라도 있으면 상세를 펼쳐 보인다."""
    ev = e.get("evidence")
    if not ev:
        return
    bad = [b for b in ev if not b["ok"]]
    st.caption(("❌ " if bad else "🔬 ") + D.evidence_summary(ev))
    if bad:
        st.code("\n".join(D.render_evidence(e).splitlines()[1:]), language="text")


def render_actions_ui(e: dict) -> None:
    """도구 사용 기록 (명령·결과·검색·파일 변경·거부). 실패나 거부가 있으면 상세를 펼쳐 보인다."""
    acts, den = e.get("actions") or [], e.get("denials") or []
    if not acts and not den and not (e.get("workspace") or {}).get("changed"):
        return
    bad = any(a.get("error") for a in acts) or bool(den)
    st.caption(("⚠ " if bad else "🛠 ") + D.actions_summary(acts, den)
               + (f" · 파일 변경 {len(e['workspace']['changed'])}" if (e.get("workspace") or {}).get("changed") else ""))
    st.code("\n".join(D.render_actions(e).splitlines()[1:]), language="text")


def render_entry(e: dict, expanded: bool = False) -> None:
    who, label = e["who"], e["label"]
    if who == "user":
        with st.chat_message("user", avatar=AVATAR["user"]):
            st.markdown(f"**💬 중간 개입**  \n{e['content']}")
        return
    name = f"{NAME[who]} · {label}"
    used = " · ".join(x for x in (used_models(e), D.usage_line(e)) if x)
    if e.get("kind") == "evaluate":
        v = D.eval_verdict(e)
        name += "  —  " + ("✅ PASS" if v == "PASS" else "⚠ NEEDS_WORK" if v == "NEEDS_WORK" else "❔ 판정 형식 없음")
    with st.chat_message(NAME[who], avatar=AVATAR[who]):
        if label.startswith("FINAL"):
            st.markdown(f'<div class="final-title">🏁 {name}  '
                        f'<span class="stage-meta">({e.get("elapsed", 0)}s{" · " + used if used else ""})</span></div>',
                        unsafe_allow_html=True)
            with st.container(border=True):
                st.markdown(e["content"])
                if e.get("contract"):
                    st.caption("🧾 " + D.contract_line(e["contract"]))
                render_evidence_ui(e)
                render_actions_ui(e)
                if e.get("compacted"):
                    st.caption(f"🗜 이전 단계 {e['compacted']['count']}개를 요약해 전달 "
                               + ("(Claude 요약)" if e["compacted"].get("method") == "claude" else "(요약 호출 실패 → 앞부분만 잘라 붙임)"))
        else:
            with st.expander(f"**{name}**  ·  {e.get('elapsed', 0)}s", expanded=expanded):
                if used:
                    st.caption(used)
                st.markdown(e["content"])
                if e.get("contract"):
                    st.caption("🧾 " + D.contract_line(e["contract"]))
                render_evidence_ui(e)
                render_actions_ui(e)
                if e.get("compacted"):
                    st.caption(f"🗜 이전 단계 {e['compacted']['count']}개를 요약해 전달 "
                               + ("(Claude 요약)" if e["compacted"].get("method") == "claude" else "(요약 호출 실패 → 앞부분만 잘라 붙임)"))


def render_contract_summary(stages: list[dict]) -> None:
    """라운드 전체의 지적 처리 결과. 미처리가 있으면 경고 — FINAL을 그대로 믿지 말라는 뜻 (default-FAIL)."""
    s = D.contract_summary(stages)
    if s["issues"] == 0:
        return
    if s["missing"]:
        st.warning("⚠ 미처리 지적: " + "; ".join(f"{lab} → {', '.join(map(str, ns))}" for lab, ns in s["missing"])
                   + ". 재요청 후에도 판정이 빠진 항목이라 FINAL을 그대로 믿기 전에 직접 확인하세요.")
    else:
        st.caption(f"✅ 검토 지적 {s['issues']}건 전부 처리됨 (반영 {s['accepted']} · 반박 {s['rejected']}"
                   + (f" · 재요청 {s['retries']}회" if s["retries"] else "") + ")")


def render_eval_summary(stages: list[dict]) -> None:
    """독립 평가 결과. NEEDS_WORK로 끝났으면 경고."""
    s = D.eval_summary(stages)
    if not s["evals"]:
        return
    tail = " (FINAL 재작성 후 재평가)" if s["revised"] else ""
    if s["verdict"] == "PASS":
        st.caption(f"🧑‍⚖️ 독립 평가: PASS{tail}")
    elif s["verdict"] == "NEEDS_WORK":
        st.warning(f"🧑‍⚖️ 독립 평가: NEEDS_WORK{tail} — 평가자의 지적을 확인한 뒤 FINAL을 쓰세요.")
    else:
        st.caption("🧑‍⚖️ 독립 평가: 판정 형식이 없어 해석 불가")


def render_round(run: dict) -> None:
    render_user(run["question"], run.get("attachments", []), run.get("config") or {},
                run.get("mode", "general"), round_stage_count(run))
    for e in run.get("stages", []):
        render_entry(e)
    render_contract_summary(run.get("stages", []))
    render_eval_summary(run.get("stages", []))
    if run.get("workspace"):
        st.caption(f"📁 작업 폴더: {run['workspace']} (단계마다 git 커밋 — 되돌리려면 그 폴더에서 git log)")
    if D.usage_summary_line(run.get("stages", [])):
        st.caption(D.usage_summary_line(run.get("stages", [])))
    if run.get("early_stopped"):
        st.caption("⏩ 검토 AI가 '추가 수정 불필요'로 판정해 남은 검토 단계를 건너뛰었습니다.")
    if run.get("status") == "stopped":
        st.caption("⏹ 사용자가 중단한 라운드입니다.")
    if run.get("error"):
        st.error(f"❌ 오류\n\n```\n{run['error']}\n```")


def resume_round(idx: int) -> None:
    """중단/오류로 끝난 라운드를 마지막으로 끝난 단계 다음부터 다시 진행한다."""
    conv = ss.conv
    run = conv["rounds"].pop(idx)
    steps = run.get("plan_steps") or [[w, k] for w, k in D.default_plan(run.get("rounds", 1), run.get("first", "claude"),
                                                                         stage_count=run.get("stage_count", 5))]
    try:
        plan = D.plan_stages(run.get("rounds", 1), run.get("mode", "general"), custom=steps)
    except ValueError:
        plan = D.plan_stages(run.get("rounds", 1), run.get("mode", "general"))
    cfg = D.Config(**{k: v for k, v in (run.get("config") or {}).items() if k in D.Config.__dataclass_fields__})
    cfg.claude_exe, cfg.codex_exe = D.find_claude(), D.find_codex()
    prior = [{"question": r["question"], "final": D.final_of(r)} for r in conv["rounds"][:idx] if D.final_of(r)]
    ss.active = {
        "question": run["question"], "attachments": run.get("attachments", []), "cfg": cfg,
        "mode": run.get("mode", "general"), "rounds": run.get("rounds", 1),
        "stage_count": run.get("stage_count", len(plan)), "early_stop": True, "eval_revise": True,
        "compaction": dict(run.get("compaction") or {}), "workspace": run.get("workspace"),
        "plan": plan, "stages": list(run.get("stages", [])), "prior": prior, "prior_rounds": len(prior),
        "status": "running", "early_stopped": bool(run.get("early_stopped")), "started": run.get("started"),
        "first": run.get("first", "claude"), "final_who": run.get("final_who"),
    }
    ss.live = None


# ----------------------------------------------------------------------------
# 사이드바
# ----------------------------------------------------------------------------
s = ss.settings
busy = ss.active is not None

with st.sidebar:
    st.title("🗣️ AI Debate Room")
    st.caption("Claude Code CLI + Codex CLI · 구독 계정 · 로컬 실행")

    # ---- 계정 ----
    auth = get_auth()
    all_ok = auth["claude"]["loggedIn"] and auth["codex"]["loggedIn"]
    with st.expander("🔐 계정 (로그인 / 로그아웃)", expanded=(not all_ok) or ss.login_flow is not None):
        for cli, label in (("claude", "Claude"), ("codex", "GPT · Codex")):
            info = auth[cli]
            if info["loggedIn"]:
                who = info.get("email") or ""
                sub = f" · {info['subscriptionType']}" if info.get("subscriptionType") else ""
                st.markdown(f"✅ **{label}** {who}{sub}")
            else:
                st.markdown(f"❌ **{label}** 로그인 안 됨")
                if info.get("detail"):
                    st.caption(info["detail"][:160])
            flow = ss.login_flow
            if flow is not None and flow.cli == cli:
                render_login_flow(flow)
            else:
                c1, c2 = st.columns(2)
                if c1.button("🔑 로그인" if not info["loggedIn"] else "🔁 계정 변경", key=f"login_{cli}",
                             width="stretch", disabled=busy or ss.login_flow is not None or "error" in get_exes()):
                    ss.login_flow = D.LoginFlow(cli, get_exes()[cli])
                    ss.pending_logout = None
                    st.rerun()
                if info["loggedIn"] and c2.button("🚪 로그아웃", key=f"logout_{cli}", width="stretch", disabled=busy):
                    ss.pending_logout = cli
                if ss.pending_logout == cli:
                    if cli == "claude":
                        st.warning("Claude 로그아웃은 VS Code의 Claude Code와 같은 로그인 정보를 지웁니다. 그쪽도 함께 로그아웃됩니다.")
                    else:
                        st.warning("Codex 로그아웃은 이 PC의 codex CLI 로그인 정보를 지웁니다.")
                    d1, d2 = st.columns(2)
                    if d1.button("예, 로그아웃", key=f"do_logout_{cli}", width="stretch"):
                        ex = get_exes()
                        rc, out = D.claude_logout(ex["claude"]) if cli == "claude" else D.codex_logout(ex["codex"])
                        ss.pending_logout = None
                        get_auth(force=True)
                        st.toast(f"{label} 로그아웃 (exit={rc})")
                        st.rerun()
                    if d2.button("취소", key=f"no_logout_{cli}", width="stretch"):
                        ss.pending_logout = None
                        st.rerun()
        st.caption("계정을 바꾸려면 먼저 브라우저에서 claude.ai / chatgpt.com 을 로그아웃한 뒤 '계정 변경'을 누르세요. "
                   f"상태는 {AUTH_TTL // 60}분마다 자동 재확인됩니다 (마지막 확인 "
                   f"{datetime.fromtimestamp(auth.get('_ts', time.time())).strftime('%H:%M:%S')}).")
        if st.button("🔄 상태 새로고침", key="auth_refresh", width="stretch"):
            get_auth(force=True)
            st.rerun()

    # ---- 대화 ----
    st.subheader("💬 대화")
    if st.button("🆕 새 대화", width="stretch", disabled=busy):
        ss.conv, ss.active, ss.live = None, None, None
        st.rerun()
    convs = D.list_conversations()
    if convs:
        labels = [f"{c['title']} · {c['updated'][5:16].replace('T', ' ')}" + ("  (구 기록)" if c["legacy"] else "")
                  for c in convs]
        pick = st.selectbox("저장된 대화", ["(선택)"] + labels, disabled=busy)
        if pick != "(선택)":
            c = convs[labels.index(pick)]
            col1, col2 = st.columns(2)
            if col1.button("📂 열기", width="stretch", disabled=busy):
                ss.conv, ss.active, ss.live = D.load_conversation(c["path"]), None, None
                st.rerun()
            if col2.button("🗑 삭제", width="stretch", disabled=busy):
                ss.pending_delete = c["path"]
            if ss.pending_delete == c["path"]:
                st.warning(f"'{c['title']}' 기록 파일을 삭제할까요?")
                d1, d2 = st.columns(2)
                if d1.button("예, 삭제", width="stretch"):
                    D.delete_conversation(c["path"])
                    if ss.conv and ss.conv.get("id") == c["id"]:
                        ss.conv = None
                    ss.pending_delete = None
                    st.rerun()
                if d2.button("취소", width="stretch"):
                    ss.pending_delete = None
                    st.rerun()
    else:
        st.caption("저장된 대화가 없습니다.")
    with st.expander("📊 통계 (저장된 대화 전체)"):
        try:
            for line in D.stats_lines(load_stats(tuple(sorted((c["path"], c.get("updated", "")) for c in convs)))):
                st.caption(line)
        except Exception as e:  # noqa: BLE001
            st.caption(f"통계 계산 실패: {e}")
    with st.expander("📁 작업 폴더 (도구를 켠 라운드)"):
        wss = D.list_workspaces()
        orphans = [w for w in wss if not w["linked"]]
        st.caption(f"{len(wss)}개 · {sum(w['size'] for w in wss) / 1024:.0f} KB · 저장된 대화와 연결되지 않은 폴더 {len(orphans)}개 "
                   "(프로브·삭제된 대화·자동 저장 끈 라운드). 대화를 삭제하면 그 작업 폴더도 같이 지워집니다.")
        if orphans and st.button(f"🧹 연결 안 된 작업 폴더 {len(orphans)}개 삭제", disabled=busy, width="stretch"):
            for w in orphans:
                D.delete_workspace(w["path"])
            st.rerun()
    if ss.conv:
        st.download_button("⬇ 이 대화 .md 내려받기", data=D.conversation_markdown(ss.conv),
                           file_name=f"{ss.conv['id']}.md", mime="text/markdown", width="stretch")
        if not s["autosave"] and st.button("💾 이 대화 지금 저장", width="stretch"):
            _, mp = D.save_conversation(ss.conv)
            st.toast(f"저장: {mp.name}")

    st.divider()
    # ---- 토론 설정 ----
    st.subheader("⚙️ 토론 설정")
    mode_keys = list(D.MODES)
    mode = st.selectbox("모드", mode_keys, index=mode_keys.index(s["mode"]) if s["mode"] in mode_keys else 0,
                        format_func=lambda k: D.MODES[k]["name"], disabled=busy)
    st.caption(D.MODES[mode]["description"])
    with st.expander("🛠 도구 — 웹 검색 · 파일·명령 · 코드 실행", expanded=False):
        run_code = st.checkbox("🔬 코드 블록 실제 실행 (python · sandbox\\_run)", value=bool(s.get("run_code", False)), disabled=busy,
                               help="답변 속 ```python 블록을 이 PC의 venv 파이썬으로 실행해 exit 코드·출력을 다음 단계에 증거로 붙입니다. "
                                    "문법 검사(json/toml은 파싱)는 항상 하고, 실행은 켰을 때만. 모델이 쓴 코드가 그대로 실행되니 믿을 수 있는 주제에서만 켜세요.")
        web_search = st.checkbox("🌐 웹 검색 허용 (실시간 확인)", value=bool(s.get("web_search", False)), disabled=busy,
                                 help="Claude는 WebSearch/WebFetch, Codex는 web_search=live. 검색한 사실엔 출처 URL을 적게 하고 검색 기록은 대화에 남습니다. "
                                      "단계마다 검색이 반복될 수 있어 느려지고 사용량이 늡니다.")
        scope_keys = list(D.WEB_SCOPES)
        web_scope = st.selectbox("검색 범위", scope_keys,
                                 index=scope_keys.index(s.get("web_scope")) if s.get("web_scope") in scope_keys else 0,
                                 format_func=lambda k: D.WEB_SCOPES[k], disabled=busy or not web_search,
                                 help="최초 답변이 검색한 결과는 [프로그램 기록]으로 모든 단계에 남습니다. 검토·반박은 그걸 재사용하고 평가자만 다시 확인하면 "
                                      "검색 횟수와 토큰이 절반 이하로 줍니다. '전체 단계'는 단계마다 검색 (오늘 실측: 4단계 11분, 토큰 100만+).")
        tools = st.checkbox("🛠 파일·명령 허용 (대화별 workspace)", value=bool(s.get("tools", False)), disabled=busy,
                            help="workspace\\<대화>\\ 안에서 파일 읽기/쓰기와 허용 목록 명령(python·pytest·pip·git·ls 등)만 허용. 허용 목록 밖은 거부. "
                                 "실행한 명령·결과·파일 변경은 대화 기록에 남고 단계마다 git 커밋됩니다. 모델이 쓴 코드가 이 PC에서 그대로 도니 믿을 수 있는 작업에서만.")
        tool_budget = int(st.number_input("단계당 도구 호출 권고 상한 (평가자는 최대 5)", min_value=1, max_value=30,
                                          value=int(s.get("tool_budget", 8)), disabled=busy,
                                          help="지시문으로 주는 권고치입니다 (이 CLI 버전엔 강제 상한 옵션이 없음). 실측: 평가자가 12회 조회하면 2분·API 환산 $1 이상."))

    order_mode = st.radio("순서", ["기본", "직접 편집"], index=1 if s["use_custom"] else 0, horizontal=True, disabled=busy,
                          help="기본: 먼저 답하는 AI와 최종 정리 AI만 고르면 나머지 역할이 자동으로 정해짐. 직접 편집: 표에서 단계를 하나씩 구성")
    use_custom = order_mode == "직접 편집"
    custom_steps = None
    first_val = s["first"] if s["first"] in ("claude", "gpt") else "claude"
    final_sel = s["final_who"] if s["final_who"] in ("same", "claude", "gpt") else "same"
    rounds = int(s["rounds"])
    stage_count = int(s.get("stage_count", D.DEFAULT_STAGE_COUNT))
    if stage_count not in D.STAGE_COUNTS:
        stage_count = D.DEFAULT_STAGE_COUNT
    if not use_custom:
        stage_count = st.radio("대화 단계 수", D.STAGE_COUNTS, index=D.STAGE_COUNTS.index(stage_count),
                               format_func=lambda n: STAGE_COUNT_LABEL[n], disabled=busy,
                               help="3단계: 최초 답변 → 상대 AI 검토 → 검토를 반영한 최종 답변. "
                                    "5단계: 검토 뒤에 반박·재검사를 한 번 더 거친 다음 최종 답변")
        first_val = st.selectbox("먼저 답하는 AI", ["claude", "gpt"], index=0 if first_val == "claude" else 1,
                                 format_func=lambda k: NAME[k], disabled=busy,
                                 help="다른 쪽 AI가 검토(5단계면 재검사도)를 맡음")
        fw_opts = ["same", "claude", "gpt"]
        final_sel = st.selectbox("최종 정리 AI", fw_opts, index=fw_opts.index(final_sel),
                                 format_func=lambda k: "먼저 답한 AI와 같게" if k == "same" else NAME[k], disabled=busy)
        if stage_count >= 5:
            rounds = int(st.number_input("검토 ↔ 반박 라운드 수", min_value=1, max_value=3, value=int(s["rounds"]),
                                         disabled=busy, help="2 이상이면 검토↔반박이 반복돼 단계가 7, 9개로 늘어남"))
        else:
            rounds = 1
    else:
        st.caption("행을 추가/삭제하고 각 행의 AI와 역할을 고르세요. 첫 행은 '최초 답변', 마지막 행은 '최종 정리'여야 합니다.")
        if "plan_base" not in ss:
            ss.plan_base = [tuple(x) for x in s["custom_plan"]]
            ss.plan_nonce = 0
        df = pd.DataFrame([{"AI": NAME[w], "역할": D.KIND_KO[k]} for w, k in ss.plan_base if w in NAME and k in D.KIND_KO])
        edited = st.data_editor(
            df, num_rows="dynamic", hide_index=True, width="stretch", disabled=busy,
            key=f"plan_editor_{ss.plan_nonce}",
            column_config={"AI": st.column_config.SelectboxColumn("AI", options=["Claude", "GPT"], required=True),
                           "역할": st.column_config.SelectboxColumn("역할", options=list(D.KIND_KO.values()), required=True)})
        custom_steps = [(KO2WHO.get(str(r["AI"])), KO2KIND.get(str(r["역할"]))) for _, r in edited.iterrows()]
        custom_steps = [(w, k) for w, k in custom_steps if w and k]
        if st.button("기본 순서로 되돌리기", disabled=busy, width="stretch"):
            ss.plan_base = D.default_plan(1, first_val, None, stage_count, evaluate)
            ss.plan_nonce += 1
            st.rerun()
    final_val = None if final_sel == "same" else final_sel
    early_stop = st.checkbox("검토 AI가 '추가 수정 불필요'라 하면 조기 종료", value=bool(s["early_stop"]), disabled=busy)
    pause_each = st.checkbox("단계마다 멈춰서 내가 끼어들기", value=bool(s["pause_each"]),
                             help="끄면 자동으로 끝까지 진행. 진행 중에도 '다음 단계 전에 멈춤' 버튼으로 언제든 멈출 수 있음")
    evaluate = st.checkbox("🧑‍⚖️ FINAL 뒤 독립 평가", value=bool(s.get("evaluate", True)), disabled=busy,
                           help="최종 정리를 쓰지 않은 쪽 AI가 새 호출로 FINAL만 채점해 [평가: PASS] 또는 [평가: NEEDS_WORK]와 [지적 N]을 냅니다 (호출 1회 추가)")
    eval_revise = st.checkbox("NEEDS_WORK면 FINAL 1회 재작성 후 재평가", value=bool(s.get("eval_revise", True)),
                              disabled=busy or not evaluate,
                              help="평가자의 [지적 N]을 반영 계약으로 검사받으며 FINAL 2를 쓰고 Eval 2로 다시 채점 (최대 호출 2회 추가)")
    try:
        PLAN = D.plan_stages(rounds, mode, first_val, final_val, custom_steps if use_custom else None, stage_count,
                             evaluate and not use_custom)  # 직접 편집이면 표에 '평가' 행을 넣는다
        plan_ok = True
        st.caption("순서: " + D.plan_preview(PLAN))
    except ValueError as e:
        PLAN, plan_ok = [], False
        st.error(f"순서 오류: {e}")

    st.divider()
    # ---- 모델 ----
    with st.expander("🧠 모델 · 실행 설정", expanded=False):
        st.markdown("**Claude** (claude CLI)")
        cm_opts = D.CLAUDE_MODELS + ["(settings 기본값)"]
        claude_model = st.selectbox("Claude 모델", cm_opts, index=cm_opts.index(s["claude_model"]) if s["claude_model"] in cm_opts else 0,
                                    format_func=lambda k: {"fable": "fable (최신 Fable, 자동 추적)", "opus": "opus (최신 Opus)",
                                                           "sonnet": "sonnet (최신 Sonnet, 가장 빠름)"}.get(k, k),
                                    help="별칭은 항상 그 계열의 최신 모델을 가리킴. 새 버전이 나오면 자동으로 그 모델을 씀. "
                                         "(settings 기본값)은 ~/.claude/settings.json 의 모델")
        ce_opts = ["(기본)"] + D.CLAUDE_EFFORTS
        claude_effort = st.selectbox("Claude reasoning effort", ce_opts,
                                     index=ce_opts.index(s["claude_effort"]) if s["claude_effort"] in ce_opts else 0)

        st.markdown("**GPT** (codex CLI)")
        codex_models = load_codex_models()
        top_slug = codex_models[0]["slug"] if codex_models else "?"
        AUTO_LABEL = f"(자동: 카탈로그 최상위 → {top_slug})"
        model_labels = [AUTO_LABEL] + [f"{m['display_name']}  ({m['slug']})" for m in codex_models] + ["(직접 입력)"]
        slugs = [m["slug"] for m in codex_models]
        cx_idx = slugs.index(s["codex_model"]) + 1 if s["codex_model"] in slugs else 0
        codex_sel = st.selectbox("Codex 모델", model_labels, index=cx_idx,
                                 help="자동 = `codex debug models` 카탈로그에서 우선순위가 가장 높은 모델. 새 모델이 카탈로그 맨 위로 오면 자동으로 바뀜")
        if codex_sel == AUTO_LABEL:
            codex_model = D.CODEX_AUTO
            m = codex_models[0] if codex_models else None
            codex_efforts, codex_default_effort = (m["efforts"], m["default_effort"]) if m else (D.CODEX_EFFORTS_ALL, None)
        elif codex_sel == "(직접 입력)":
            codex_model = st.text_input("모델 slug 직접 입력", value="", placeholder="예: gpt-5.6-terra").strip() or None
            codex_efforts, codex_default_effort = D.CODEX_EFFORTS_ALL, None
        else:
            m = codex_models[model_labels.index(codex_sel) - 1]
            codex_model, codex_efforts, codex_default_effort = m["slug"], m["efforts"], m["default_effort"]
        xe_opts = [f"(모델 기본: {codex_default_effort or '?'})"] + codex_efforts
        codex_effort_sel = st.selectbox("Codex reasoning effort", xe_opts,
                                        index=codex_efforts.index(s["codex_effort"]) + 1 if s["codex_effort"] in codex_efforts else 0,
                                        help="모델마다 지원 범위가 다름. 높을수록 느리고 사용량을 더 씀. 자동 모델이 바뀌어 미지원이면 지원 범위로 자동 조정")
        codex_effort = None if codex_effort_sel.startswith("(") else codex_effort_sel

        st.divider()
        # ---- 공통 ----
        st.subheader("🔧 공통")
        timeout = int(st.number_input("CLI 타임아웃 (초, 호출 1회당)", min_value=60, max_value=3600, value=int(s["timeout"]), step=60))
        compact_k = int(st.number_input("긴 토론 요약 기준 (천 자, 0=끄기)", min_value=0, max_value=500,
                                        value=int(s.get("compact_chars", 60000)) // 1000, step=10, disabled=busy,
                                        help="전체 대화 기록이 이 길이를 넘으면 마지막 2단계만 원문으로 두고 그 앞은 Claude가 요약해 전달 (라운드당 1~2회 추가 호출)"))
        compact_chars = compact_k * 1000
        autosave = st.checkbox("대화 자동 저장 (chats\\ 폴더)", value=bool(s["autosave"]),
                               help="끄면 파일을 만들지 않음. 대신 새로고침/재시작하면 대화가 사라지고, 목록에도 남지 않음")
        do_beep = st.checkbox("토론 완료 시 소리", value=bool(s["beep"]))

        CFG = D.Config(
            claude_model=None if claude_model.startswith("(") else claude_model,
            claude_effort=None if claude_effort.startswith("(") else claude_effort,
            codex_model=codex_model, codex_effort=codex_effort, timeout=timeout, run_code=run_code,
            compact_chars=compact_chars, web_search=web_search, tools=tools, web_scope=web_scope,
            tool_budget=tool_budget, eval_tool_budget=min(tool_budget, D.Config.eval_tool_budget),
        )
        rx_model, rx_effort, rx_note = D.resolve_codex(CFG, codex_models)
        st.caption("현재 설정 → " + (
            f"claude `-p --model {CFG.claude_model or '(기본)'}" + (f" --effort {CFG.claude_effort}" if CFG.claude_effort else "") + "`  \n"
            f"codex `exec" + (f" -m {rx_model}" if rx_model else "")
            + (f" -c model_reasoning_effort={rx_effort}" if rx_effort else "") + "`"
            + (f"  \n⚠ {rx_note}" if rx_note else "")))

    new_settings = {"claude_model": claude_model, "claude_effort": claude_effort,
                    "codex_model": codex_model or s["codex_model"], "codex_effort": codex_effort or "(기본)",
                    "timeout": timeout, "mode": mode, "stage_count": stage_count, "rounds": rounds, "early_stop": early_stop,
                    "pause_each": pause_each, "autosave": autosave, "beep": do_beep, "run_code": run_code,
                    "evaluate": evaluate, "eval_revise": eval_revise, "compact_chars": compact_chars,
                    "web_search": web_search, "tools": tools, "web_scope": web_scope, "tool_budget": tool_budget,
                    "first": first_val, "final_who": final_sel, "use_custom": use_custom,
                    "custom_plan": [list(x) for x in custom_steps] if (use_custom and custom_steps) else s["custom_plan"]}
    if new_settings != ss.settings:
        ss.settings = new_settings
        save_settings(new_settings)

    st.divider()
    if st.button("🔍 현재 설정으로 CLI 점검", width="stretch", disabled=busy,
                 help="위에서 고른 모델/effort로 두 CLI를 한 번씩 호출해 실제 적용값을 보여줌"):
        cfg0 = D.Config(**{**D.asdict(CFG), "tools": False, "web_search": False})  # 점검은 도구 없이 최소 호출
        try:
            cfg0.claude_exe, cfg0.codex_exe = D.find_claude(), D.find_codex()
        except FileNotFoundError as e:
            st.error(str(e))
        else:
            t0 = time.time()
            with st.spinner("claude 호출 중..."):
                try:
                    text, meta = D.call_claude(cfg0, "간단히 답하십시오.", "Reply with exactly the word OK.", "check")
                    models = [m for m in meta.get("models", []) if "haiku" not in m]
                    st.success(f"**claude** {text!r} ({time.time() - t0:.1f}s)  \n"
                               f"요청: model={cfg0.claude_model or '기본'}, effort={cfg0.claude_effort or '기본'}  \n"
                               f"실제 사용 모델(응답 JSON): {', '.join(models) or '?'}")
                except Exception as e:  # noqa: BLE001
                    st.error(f"claude 실패: {str(e)[:600]}")
            t0 = time.time()
            with st.spinner("codex 호출 중..."):
                try:
                    text, meta = D.call_codex(cfg0, "Reply with exactly the word OK and nothing else.", "check")
                    st.success(f"**codex** {text!r} ({time.time() - t0:.1f}s)  \n"
                               f"요청: model={cfg0.codex_model or '기본'} → {rx_model}, effort={cfg0.codex_effort or '기본'} → {rx_effort}  \n"
                               f"실제 적용(codex 시작 헤더): model={meta.get('model')}, effort={meta.get('reasoning_effort')}")
                except Exception as e:  # noqa: BLE001
                    st.error(f"codex 실패: {str(e)[:600]}")


# ----------------------------------------------------------------------------
# 진행 중인 라운드: 백그라운드 스레드 + 실시간 갱신
# ----------------------------------------------------------------------------
def stages_done(active: dict) -> int:
    return sum(1 for e in active["stages"] if e["who"] != "user")


def start_stage_worker(active: dict, stage: dict) -> dict:
    live = {"text": "", "elapsed": 0.0, "done": False, "entry": None, "error": None, "cancelled": False,
            "cancel": threading.Event(), "stage": stage, "t0": time.time(), "actions": []}
    history = list(active["stages"])  # 스냅샷
    plan_len = len(active["plan"])
    compaction = active.setdefault("compaction", {})  # 요약 캐시 (라운드 단위)

    def work() -> None:
        try:
            live["entry"] = D.execute_stage(
                active["question"], active["attachments"], history, stage, active["cfg"],
                active["prior"], active["mode"], plan_len,
                on_delta=lambda t: live.__setitem__("text", t),
                on_tick=lambda sec: live.__setitem__("elapsed", sec),
                cancel=live["cancel"], compaction=compaction,
                on_action=lambda acts: live.__setitem__("actions", [dict(a) for a in acts]))  # 도구 사용 실시간 표시
        except D.CLIError as e:
            live["error"], live["cancelled"] = str(e), e.cancelled
        except Exception as e:  # noqa: BLE001
            live["error"] = f"{type(e).__name__}: {e}"
        live["done"] = True

    threading.Thread(target=work, daemon=True).start()
    return live


def finalize_active(status: str, error: str | None = None) -> None:
    active, live = ss.active, ss.live
    if live is not None:
        live["cancel"].set()
    now = datetime.now().isoformat(timespec="seconds")
    round_ = {k: active.get(k) for k in ("question", "attachments", "mode", "rounds", "stage_count", "stages",
                                         "early_stopped", "started", "prior_rounds", "first", "final_who", "compaction",
                                         "workspace")}
    round_.update({"plan": [st_["label"] for st_ in active["plan"]],
                   "plan_steps": [[st_["who"], st_["kind"]] for st_ in active["plan"]],  # 재개용
                   "config": D.asdict(active["cfg"]), "contract": D.contract_summary(active["stages"]),
                   "evaluation": D.eval_summary(active["stages"]), "usage": D.usage_summary(active["stages"]),
                   "status": status, "error": error, "finished": now})
    if ss.conv is None:
        ss.conv = D.new_conversation(active["question"])
    ss.conv["rounds"].append(round_)
    ss.conv["updated"] = now
    if active.get("workspace"):
        ss.conv["workspace"] = active["workspace"]  # 다음 라운드가 같은 작업 폴더를 이어 쓴다
    if ss.settings["autosave"]:
        D.save_conversation(ss.conv)
    ss.active, ss.live = None, None
    if status == "done":
        st.toast("✅ 토론 완료")
        if ss.settings["beep"]:
            beep()
    elif status == "stopped":
        st.toast("⏹ 중단했습니다")
    else:
        st.toast("❌ 오류로 멈췄습니다")


@st.fragment(run_every=0.5)
def live_view() -> None:
    live = ss.live
    if live is None:
        return
    if live["done"]:
        st.rerun()  # 본문 스크립트가 결과를 처리하도록 전체 재실행
    stage = live["stage"]
    who = stage["who"]
    with st.chat_message(NAME[who], avatar=AVATAR[who]):
        st.markdown(f"**{NAME[who]} · {stage['label']}**  ·  ⏳ {live['elapsed']:.0f}s")
        if who == "claude":
            st.markdown((live["text"] or "_생각 중..._") + " ▌")
        else:
            st.caption("GPT(codex CLI)는 완성된 답만 내보내므로 글자 단위로는 보이지 않습니다. 도구 사용은 아래에 실시간으로 표시됩니다.")
        acts = live.get("actions") or []
        if acts:
            st.caption("🛠 " + "  ·  ".join(f"{a['tool']}: {a['input'][:60]}" + (" …" if a.get("running") else "") for a in acts[-4:])
                       + (f"  (총 {len(acts)}회)" if len(acts) > 4 else ""))


def run_active_stage() -> None:
    active = ss.active
    plan = active["plan"]
    done = stages_done(active)
    if done >= len(plan):
        finalize_active("done")
        st.rerun()
    stage = plan[done]
    c1, c2, c3 = st.columns([4, 1.4, 1])
    c1.progress(done / len(plan), text=f"[{done + 1}/{len(plan)}] {NAME[stage['who']]} · {stage['label']} 진행 중 (다른 AI는 대기)")
    pause_label = "⏸ 멈춤 예약됨" if active.get("pause_next") else "⏸ 다음 단계 전에 멈춤"
    if c2.button(pause_label, width="stretch", key="pause_btn", disabled=bool(active.get("pause_next")),
                 help="지금 단계는 끝까지 받고, 그 다음 단계로 넘어가기 전에 멈춤"):
        active["pause_next"] = True
        st.rerun()
    if c3.button("⏹ 중단", width="stretch", key="stop_btn", help="지금 즉시 중단 (나중에 '이어서 진행' 가능)"):
        finalize_active("stopped")
        st.rerun()
    live = ss.live
    if live is None:
        live = start_stage_worker(active, stage)
        ss.live = live
    if not live["done"]:
        live_view()
        return
    ss.live = None
    if live["error"]:
        finalize_active("stopped" if live["cancelled"] else "error", None if live["cancelled"] else live["error"])
        st.rerun()
    entry = live["entry"]
    active["stages"].append(entry)
    done += 1
    active["plan"], ev = D.adjust_plan_after(plan, done, entry, active["early_stop"], active.get("eval_revise", True))
    if ev and ev["type"] == "skip":
        active["early_stopped"] = True
    if done >= len(active["plan"]):
        finalize_active("done")
    elif pause_each or active.get("pause_next"):
        active["status"] = "paused"
        active["pause_next"] = False
    st.rerun()


def paused_controls() -> None:
    active = ss.active
    plan = active["plan"]
    done = stages_done(active)
    nxt = plan[done]
    st.info(f"⏸ 일시정지 — 다음 단계: **{NAME[nxt['who']]} · {nxt['label']}**")
    text = st.text_area("여기서 한마디 (비워 두면 그냥 계속)", key=f"interject_{done}_{len(active['stages'])}",
                        placeholder="예: 비상정지는 하드와이어로 분리한다는 전제로 다시 검토해줘")
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("▶ 계속", width="stretch"):
        if text.strip():
            active["stages"].append(D.interjection_entry(text))
        active["status"] = "running"
        st.rerun()
    last_ai = next((i for i in range(len(active["stages"]) - 1, -1, -1) if active["stages"][i]["who"] != "user"), None)
    if c2.button("🔁 직전 단계 다시 생성", width="stretch", disabled=last_ai is None,
                 help="방금 끝난 AI 단계를 지우고 같은 단계를 다시 받음 (한마디를 적었으면 그것도 반영)"):
        del active["stages"][last_ai:]  # 직전 AI 단계(와 그 뒤 항목) 삭제 → 같은 단계를 다시 실행
        if text.strip():
            active["stages"].append(D.interjection_entry(text))
        active["status"] = "running"
        st.rerun()
    if c3.button("⏭ 바로 FINAL로", width="stretch", disabled=nxt["kind"] in ("final", "evaluate")):
        if text.strip():
            active["stages"].append(D.interjection_entry(text))
        final_idx = next(i for i, s_ in enumerate(plan) if s_["kind"] == "final")
        active["plan"] = plan[:done] + plan[final_idx:]
        active["status"] = "running"
        st.rerun()
    if c4.button("⏹ 중단", width="stretch"):
        finalize_active("stopped")
        st.rerun()


# ----------------------------------------------------------------------------
# 본문
# ----------------------------------------------------------------------------
st.title("AI Debate Room")
if ss.conv:
    st.caption(f"💬 **{ss.conv['title']}** · 라운드 {len(ss.conv['rounds'])}개 · "
               + ("자동 저장 켜짐" if s["autosave"] else "자동 저장 꺼짐 (사이드바에서 수동 저장 가능)"))
    for r in ss.conv["rounds"]:
        render_round(r)
    last = ss.conv["rounds"][-1] if ss.conv["rounds"] else None
    if last and not busy and last.get("status") in ("stopped", "error"):
        if st.button("▶ 이어서 진행 (중단된 지점부터)", key="resume_last", help="끝난 단계는 그대로 두고 다음 단계부터 다시 받습니다"):
            try:
                resume_round(len(ss.conv["rounds"]) - 1)
            except FileNotFoundError as e:
                st.error(str(e))
                st.stop()
            st.rerun()
else:
    st.caption("아래 입력창에 질문을 쓰면 Claude와 GPT가 토론을 시작합니다. 📎로 파일도 함께 올릴 수 있습니다.")

if ss.active:
    active = ss.active
    render_user(active["question"], active["attachments"], D.asdict(active["cfg"]), active["mode"],
                count_debate_stages(s_["kind"] for s_ in active["plan"]))
    for i, e in enumerate(active["stages"]):
        render_entry(e, expanded=(i == len(active["stages"]) - 1))
    if active.get("early_stopped"):
        st.caption("⏩ 검토 AI가 '추가 수정 불필요'로 판정해 남은 검토 단계를 건너뜁니다.")
    if active["status"] == "running":
        run_active_stage()
    elif active["status"] == "paused":
        paused_controls()

submitted = st.chat_input("질문을 입력하세요. 📎 버튼으로 파일 첨부 (텍스트 · pdf · 코드 · 이미지 png/jpg)",
                          accept_file="multiple", file_type=D.UPLOAD_TYPES, disabled=busy or not plan_ok)

if submitted is not None and ((submitted.text or "").strip() or submitted.files):
    question = (submitted.text or "").strip() or "첨부한 파일을 분석해줘."
    # 이미지는 매 단계 CLI에 다시 전달해야 하므로 파일로 둔다. 자동 저장이 꺼져 있으면 임시 폴더(재시작 시 사라짐).
    image_dir = (D.IMAGE_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")) if s["autosave"] \
        else Path(tempfile.mkdtemp(prefix="debate_img_"))
    attachments = []
    for f in (submitted.files or []):
        attachments.extend(D.load_attachments(f.name, f.getvalue(), image_dir))  # 텍스트 없는 PDF는 쪽 이미지로
    cfg = D.Config(**D.asdict(CFG))
    try:
        cfg.claude_exe, cfg.codex_exe = D.find_claude(), D.find_codex()
    except FileNotFoundError as e:
        st.error(str(e))
        st.stop()
    workspace = None
    if cfg.tools:  # 대화별 작업 폴더 — 같은 대화면 재사용, 없으면 새로
        workspace = (ss.conv or {}).get("workspace")
        if not workspace or not Path(workspace).exists():
            workspace = str(D.new_workspace(question))
        cfg.workspace = workspace
    prior_rounds = ss.conv["rounds"] if ss.conv else []
    prior = [{"question": r["question"], "final": D.final_of(r)} for r in prior_rounds if D.final_of(r)]
    ss.active = {
        "question": question, "attachments": attachments, "cfg": cfg, "mode": mode, "rounds": rounds,
        "stage_count": stage_count, "early_stop": early_stop, "eval_revise": eval_revise, "plan": list(PLAN), "stages": [], "prior": prior,
        "prior_rounds": len(prior), "status": "running", "early_stopped": False, "pause_next": False,
        "first": first_val, "final_who": final_val, "workspace": workspace,
        "started": datetime.now().isoformat(timespec="seconds"),
    }
    ss.live = None
    st.rerun()

# 로그인 진행 중이면 2초마다 화면을 갱신해 URL/코드/완료를 반영한다
if ss.login_flow is not None and not ss.login_flow.finished() and not ss.login_flow.success:
    time.sleep(2)
    st.rerun()
