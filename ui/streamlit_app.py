"""Streamlit chat UI for the Veltrix PeopleOps Assistant.

Run (from the project root, with the API already running on port 8000):

    streamlit run ui/streamlit_app.py

The UI is a thin client: it sends each message to ``POST /chat`` with the
selected ``X-Employee-Id`` and renders the answer, route, citations and tool
calls. Identity, permissions, retrieval and LLM calls all stay in the API.
"""

from __future__ import annotations

import html
import json
import os
import sys
import uuid
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_client import VeltrixAPI  # noqa: E402

DEFAULT_API_URL = os.environ.get("VELTRIX_API_URL", "http://127.0.0.1:8000")

# Demo sign-in profiles (IDs and roles only; names stay in the database).
PROFILES = {
    "E2004": "Employee · India · L3",
    "E2005": "Employee · India · L4",
    "E3004": "Employee · United States · L3",
    "E4004": "Employee · Germany · L3",
    "E2002": "Manager · India (team: E2004–E2007)",
    "E3002": "Manager · United States",
    "E4002": "Manager · Germany",
    "E1001": "HR · India",
    "E1003": "HR · United States",
}
OTHER = "Other ID…"

SUGGESTIONS = {
    "employee": [
        "What is my leave balance?",
        "How many sick days do I get per year?",
        "How many annual leave days will I have by December?",
        "Is 2 October a public holiday for me?",
        "I want to apply for leave next week",
        "Show my leave requests",
    ],
    "manager": [
        "Who on my team is on leave in December?",
        "What is E2004's leave balance?",
        "What is the hybrid work policy?",
        "What is my leave balance?",
    ],
    "hr": [
        "What is E2004's leave balance?",
        "What are the salary bands for L5?",
        "How many days can employees in Germany carry forward?",
        "What is the probation period?",
    ],
}

ROUTE_STYLE = {
    "rag": ("📄", "Policy answer", "#4F8CFF"),
    "tool": ("🛠️", "Leave data", "#22C3A6"),
    "hybrid": ("🔀", "Data + policy", "#A66BFF"),
    "clarify": ("❓", "Needs detail", "#F5B841"),
    "no_answer": ("🔍", "Not in documents", "#8A94A6"),
    "refuse": ("⛔", "Refused", "#FF6B81"),
}
TOOL_STATUS_COLOR = {"success": "#22C3A6", "pending_confirmation": "#F5B841", "error": "#FF6B81"}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"], .stMarkdown, .stChatMessage { font-family: 'Inter', sans-serif; }
.block-container { padding-top: 1.6rem; max-width: 900px; }
.vx-hero {
  padding: 1.3rem 1.5rem; border-radius: 18px; margin-bottom: 1.1rem;
  background: linear-gradient(135deg, rgba(124,92,255,.28), rgba(34,195,166,.16));
  border: 1px solid rgba(255,255,255,.08); box-shadow: 0 10px 30px rgba(0,0,0,.25);
}
.vx-hero h1 { font-size: 1.55rem; font-weight: 700; margin: 0 0 .25rem 0;
  background: linear-gradient(90deg, #ffffff, #c9bcff); -webkit-background-clip: text; color: transparent; }
.vx-hero p { margin: 0; color: #b8c0d6; font-size: .93rem; }
.vx-badge { display: inline-flex; align-items: center; gap: .35rem; padding: .18rem .62rem; border-radius: 999px;
  font-size: .74rem; font-weight: 600; letter-spacing: .02em; border: 1px solid; margin-right: .4rem; }
.vx-meta { margin-top: .55rem; display: flex; flex-wrap: wrap; align-items: center; gap: .3rem; }
.vx-latency { font-size: .74rem; color: #8a94a6; }
.vx-cite { display: inline-block; padding: .2rem .55rem; margin: .18rem .3rem .18rem 0; border-radius: 8px;
  background: rgba(79,140,255,.10); border: 1px solid rgba(79,140,255,.30); font-size: .78rem; color: #cfdcff; }
.vx-cite b { color: #ffffff; font-weight: 600; }
.vx-tool { padding: .45rem .7rem; border-radius: 10px; margin: .25rem 0; font-size: .8rem;
  background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.08); }
.vx-tool code { font-size: .76rem; }
.vx-error { padding: .7rem .9rem; border-radius: 12px; background: rgba(255,107,129,.10);
  border: 1px solid rgba(255,107,129,.35); color: #ffd2da; font-size: .9rem; }
.vx-status { display: inline-flex; align-items: center; gap: .4rem; font-size: .82rem; font-weight: 600; }
.vx-dot { width: .6rem; height: .6rem; border-radius: 50%; display: inline-block; }
.vx-welcome { padding: 1rem 1.2rem; border-radius: 14px; background: rgba(255,255,255,.03);
  border: 1px dashed rgba(255,255,255,.12); color: #c3cadb; font-size: .92rem; margin-bottom: .6rem; }
[data-testid="stChatMessage"] { border-radius: 14px; padding: .35rem .6rem; transition: background .2s ease; }
[data-testid="stChatMessage"]:hover { background: rgba(255,255,255,.025); }
div.stButton > button { border-radius: 10px; transition: transform .12s ease, border-color .12s ease; }
div.stButton > button:hover { transform: translateY(-1px); border-color: #7C5CFF; }
div.stButton > button p { white-space: normal; line-height: 1.25; }
</style>
"""


def esc_md(text: str) -> str:
    """Stop '$' amounts from being rendered as LaTeX by st.markdown."""
    return text.replace("$", r"\$")


def role_of(employee_id: str) -> str:
    label = PROFILES.get(employee_id, "").lower()
    if label.startswith("manager"):
        return "manager"
    if label.startswith("hr"):
        return "hr"
    return "employee"


def init_state() -> None:
    defaults = {"messages": [], "session_id": str(uuid.uuid4()), "employee_id": "E2004",
                "queued": None, "api_url": DEFAULT_API_URL}
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def new_conversation() -> None:
    st.session_state.messages = []
    st.session_state.session_id = str(uuid.uuid4())


def queue_message(text: str) -> None:
    st.session_state.queued = text


@st.cache_data(ttl=5, show_spinner=False)
def cached_health(api_url: str) -> str:
    return VeltrixAPI(api_url).health()


def render_sidebar() -> None:
    with st.sidebar:
        st.markdown("## 💼 Veltrix PeopleOps")
        status = cached_health(st.session_state.api_url)
        colors = {"ok": ("#22C3A6", "API online"), "starting": ("#F5B841", "Starting… indexing documents"),
                  "error": ("#FF6B81", "API start-up failed"), "offline": ("#FF6B81", "API offline")}
        color, label = colors.get(status, ("#F5B841", status))
        st.markdown(f'<span class="vx-status"><span class="vx-dot" style="background:{color}"></span>{label}</span>',
                    unsafe_allow_html=True)
        if status != "ok" and st.button("↻ Re-check", key="recheck_health"):
            cached_health.clear()
            st.rerun()

        st.markdown("#### Signed in as")
        options = list(PROFILES) + [OTHER]
        current = st.session_state.employee_id
        index = options.index(current) if current in PROFILES else len(options) - 1
        choice = st.selectbox("Employee", options, index=index, key="profile_select",
                              format_func=lambda k: k if k == OTHER else f"{k} — {PROFILES[k]}",
                              label_visibility="collapsed")
        employee_id = choice
        if choice == OTHER:
            employee_id = st.text_input("Employee ID", value="" if current in PROFILES else current,
                                        key="other_employee_id", placeholder="e.g. E2007").strip().upper()
        if employee_id and employee_id != st.session_state.employee_id:
            # A session belongs to one employee (the API returns 403 otherwise).
            st.session_state.employee_id = employee_id
            new_conversation()
            st.rerun()

        st.caption("The API identifies you only by the X-Employee-Id header; "
                   "what you can see depends on that employee's role.")
        st.button("🗨️ New conversation", key="new_conversation", on_click=new_conversation,
                  width="stretch")

        with st.expander("What the badges mean"):
            for icon, label, color in ROUTE_STYLE.values():
                st.markdown(f'<span class="vx-badge" style="color:{color};border-color:{color}55">'
                            f'{icon} {label}</span>', unsafe_allow_html=True)
        with st.expander("Settings"):
            url = st.text_input("API URL", value=st.session_state.api_url, key="api_url_input")
            if url.strip() and url.strip() != st.session_state.api_url:
                st.session_state.api_url = url.strip()
                cached_health.clear()
                st.rerun()
            st.caption(f"Session: `{st.session_state.session_id[:8]}…`")


def render_meta(meta: dict) -> None:
    data = meta.get("data") or {}
    route = data.get("route", "")
    icon, label, color = ROUTE_STYLE.get(route, ("•", route or "unknown", "#8A94A6"))
    parts = [f'<span class="vx-badge" style="color:{color};border-color:{color}55;background:{color}14">'
             f'{icon} {label}</span>']
    if data.get("pending_confirmation"):
        parts.append('<span class="vx-badge" style="color:#F5B841;border-color:#F5B84155">⏳ Awaiting confirmation</span>')
    parts.append(f'<span class="vx-latency">{meta.get("latency_s", 0):.1f}s</span>')
    st.markdown(f'<div class="vx-meta">{"".join(parts)}</div>', unsafe_allow_html=True)

    citations = data.get("citations") or []
    if citations:
        chips = "".join(
            f'<span class="vx-cite">📄 <b>{html.escape(c.get("doc_id", ""))}</b> · '
            f'{html.escape(c.get("locator", ""))}</span>' for c in citations
        )
        st.markdown(f'<div style="margin-top:.35rem">{chips}</div>', unsafe_allow_html=True)

    tool_calls = data.get("tool_calls") or []
    if tool_calls:
        with st.expander(f"🛠️ Tool calls ({len(tool_calls)})"):
            for call in tool_calls:
                status = call.get("status", "")
                c = TOOL_STATUS_COLOR.get(status, "#8A94A6")
                args = html.escape(json.dumps(call.get("args", {}), ensure_ascii=False))
                st.markdown(
                    f'<div class="vx-tool"><b>{html.escape(call.get("name", ""))}</b> '
                    f'<span class="vx-badge" style="color:{c};border-color:{c}55">{html.escape(status)}</span>'
                    f'<br><code>{args}</code></div>', unsafe_allow_html=True)


def render_history() -> None:
    messages = st.session_state.messages
    for i, msg in enumerate(messages):
        avatar = "🧑‍💼" if msg["role"] == "user" else "💼"
        with st.chat_message(msg["role"], avatar=avatar):
            if msg.get("error"):
                st.markdown(f'<div class="vx-error">⚠️ {html.escape(msg["error"])}</div>', unsafe_allow_html=True)
                continue
            st.markdown(esc_md(msg["content"]))
            if msg["role"] == "assistant":
                render_meta(msg.get("meta", {}))
                is_last = i == len(messages) - 1
                if is_last and (msg.get("meta", {}).get("data") or {}).get("pending_confirmation"):
                    col1, col2, _ = st.columns([1, 1, 3])
                    col1.button("✅ Confirm", key=f"confirm_{i}", type="primary",
                                on_click=queue_message, args=("yes",), width="stretch")
                    col2.button("✖ Cancel", key=f"decline_{i}", on_click=queue_message, args=("no",),
                                width="stretch")


def render_welcome() -> None:
    employee_id = st.session_state.employee_id
    st.markdown(
        f'<div class="vx-welcome">👋 Hi <b>{html.escape(employee_id)}</b>! Ask about company policies, '
        f'public holidays, or your leave: balances, requests, applying or cancelling. '
        f'Changes to your leave always need your confirmation first.</div>', unsafe_allow_html=True)
    suggestions = SUGGESTIONS[role_of(employee_id)]
    cols = st.columns(2)
    for n, text in enumerate(suggestions):
        cols[n % 2].button(text, key=f"suggest_{n}", on_click=queue_message, args=(text,),
                           width="stretch")


def send(text: str) -> None:
    st.session_state.messages.append({"role": "user", "content": text})
    with st.chat_message("user", avatar="🧑‍💼"):
        st.markdown(esc_md(text))
    with st.chat_message("assistant", avatar="💼"):
        with st.spinner("Thinking…"):
            result = VeltrixAPI(st.session_state.api_url).chat(
                st.session_state.employee_id, st.session_state.session_id, text)
    if result.ok:
        st.session_state.messages.append({
            "role": "assistant", "content": result.data.get("answer", ""),
            "meta": {"data": result.data, "latency_s": result.latency_s},
        })
    else:
        st.session_state.messages.append({"role": "assistant", "content": "", "error": result.error})
        if result.status_code in (503, None):
            cached_health.clear()


def main() -> None:
    st.set_page_config(page_title="Veltrix PeopleOps Assistant", page_icon="💼", layout="centered",
                       initial_sidebar_state="expanded")
    st.markdown(CSS, unsafe_allow_html=True)
    init_state()
    render_sidebar()

    st.markdown(
        '<div class="vx-hero"><h1>Veltrix PeopleOps Assistant</h1>'
        '<p>HR policies, holidays and leave, with answers grounded in company documents and citations.</p></div>',
        unsafe_allow_html=True)

    if not st.session_state.messages:
        render_welcome()
    render_history()

    prompt = st.chat_input("Ask about policies, holidays or your leave…", key="chat_input")
    queued = st.session_state.queued
    if queued:
        st.session_state.queued = None
        prompt = queued
    if prompt and prompt.strip():
        send(prompt.strip())
        st.rerun()


main()
