"""
streamlit_app.py — Streamlit UI for Worksoft AI Support Assistant

Run locally:
    streamlit run streamlit_app.py

Deploy on Streamlit Cloud:
    1. Push repo to GitHub (exclude .env and *.db files)
    2. Add secrets in Streamlit Cloud dashboard (see .streamlit/secrets.toml.example)
    3. Connect repo and deploy
"""

import streamlit as st
import os, sys, re, json, base64, hashlib, time
from datetime import datetime

# ── Page config — must be the very first Streamlit call ───────────────────────
st.set_page_config(
    page_title="Worksoft AI Support",
    page_icon="🤖",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ── Load Streamlit secrets into os.environ BEFORE importing app.py ────────────
# This ensures app.py reads the correct values when its module-level constants
# (GROQ_API_KEY, SF_USERNAME, etc.) are evaluated on first import.
def _bootstrap_secrets():
    try:
        for k, v in st.secrets.items():
            if v is not None and str(v).strip() and k not in os.environ:
                os.environ[k] = str(v)
    except Exception:
        pass  # Local dev: .env file handles secrets via load_dotenv() in app.py

_bootstrap_secrets()

# ── Import backend logic from app.py ──────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import (
    create_session, save_message, update_session_status, save_ticket,
    process_chat_stream, sync_sf_knowledge,
    sf_create_case, send_escalation_email, _pdf_text,
    IT_ADMIN_EMAIL, SF_CLIENT_ID, SF_USERNAME, SF_PASSWORD,
    OUTLOOK_EMAIL, AI_PROVIDER,
    GROQ_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY,
)

GEMINI_API_KEY_SET = bool(os.environ.get("GEMINI_API_KEY"))

# ── Session state initialisation ──────────────────────────────────────────────
_DEFAULTS = {
    "page":             "onboarding",
    "name":             "",
    "email":            "",
    "company":          "",
    "sid":              None,
    "messages":         [],
    "sf_resolution":    "",
    "show_escalation":  False,
    "escalation_done":  False,
    "ticket_info":      None,
    "email_ok":         False,
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ── File processing (Streamlit UploadedFile → file_data dict) ─────────────────
def _process_file(uploaded):
    if uploaded is None:
        return None
    name = uploaded.name
    mime = uploaded.type or ""
    raw  = uploaded.getvalue()
    if mime.startswith("image/") or name.lower().endswith((".png",".jpg",".jpeg",".gif",".webp",".bmp")):
        if not mime.startswith("image/"):
            mime = "image/png"
        return {"type":"image","name":name,"mime":mime,"base64":base64.b64encode(raw).decode()}
    if mime == "application/pdf" or name.lower().endswith(".pdf"):
        return {"type":"text","name":name,"content":_pdf_text(raw)}
    try:    text = raw.decode("utf-8", errors="replace")
    except: text = str(raw[:3000])
    return {"type":"text","name":name,"content":text[:4000]}

# ── Streaming helper ───────────────────────────────────────────────────────────
def _make_stream(text, history, sf_res, file_data=None):
    """
    Returns a (generator, meta_container) pair.
    The generator yields display-only text (META chunks are filtered out).
    meta_container is populated with the metadata as the generator is consumed.
    """
    meta_container = {}

    def _gen():
        for chunk in process_chat_stream(text, history, sf_res, file_data):
            if "__META__" in chunk:
                m = re.search(r"__META__(.+?)__END_META__", chunk, re.DOTALL)
                if m:
                    try:
                        meta_container.update(json.loads(m.group(1)))
                    except Exception:
                        pass
                pre = chunk.split("__META__")[0]
                if pre.strip():
                    yield pre
            else:
                yield chunk

    return _gen(), meta_container

# ── Escalation helper ──────────────────────────────────────────────────────────
def _do_escalate(priority, extra):
    name  = st.session_state.name
    email = st.session_state.email
    sid   = st.session_state.sid
    msgs  = st.session_state.messages

    issue_text = next((m["content"] for m in msgs if m.get("role") == "user"), "Worksoft issue")
    convo = "\n\n".join(
        f"{'User' if m['role']=='user' else 'Agent'}: {m['content']}"
        for m in msgs if m.get("content", "").strip()
    )
    subj = f"Worksoft Issue – {issue_text[:80]}"
    desc = (
        f"Reported by: {name} ({email})\n\nIssue:\n{issue_text}"
        + (f"\n\nAdditional Details:\n{extra}" if extra else "")
        + (f"\n\nAI Chat Transcript:\n{convo}" if convo else "")
    )

    ticket = None; sf_error = ""
    if all([SF_CLIENT_ID, SF_USERNAME, SF_PASSWORD]):
        try:
            ticket = sf_create_case(subj, desc, name, email, priority)
        except Exception as exc:
            sf_error = str(exc)

    if not ticket:
        ref = "QWS-" + hashlib.md5(f"{email}{time.time()}".encode()).hexdigest()[:6].upper()
        ticket = {"id": ref, "case_number": ref, "url": "#"}

    ticket.update({
        "priority":   priority,
        "created_at": datetime.now().strftime("%d %b %Y, %H:%M"),
    })

    email_sent = False
    if OUTLOOK_EMAIL:
        try:
            email_sent, _ = send_escalation_email(ticket, name, email, issue_text, convo)
        except Exception:
            pass

    if sid:
        try:
            save_ticket(sid, name, email, issue_text, priority,
                        ticket.get("case_number",""), ticket.get("url",""), sf_error, email_sent)
            update_session_status(sid, "escalated")
        except Exception:
            pass

    st.session_state.ticket_info      = ticket
    st.session_state.email_ok         = email_sent
    st.session_state.escalation_done  = True
    st.session_state.show_escalation  = False

# ── Escalation dialog ──────────────────────────────────────────────────────────
@st.dialog("Escalate to IT Support", width="large")
def escalation_dialog():
    st.markdown(f"**Escalating for:** {st.session_state.name} ({st.session_state.email})")
    st.markdown("This will create a Salesforce case and email your IT team.")
    st.divider()

    priority = st.selectbox("Priority", ["High", "Medium", "Low"])
    extra    = st.text_area(
        "Additional details (optional)", height=100,
        placeholder="Any extra context, steps already tried, or attachments to mention…"
    )

    c1, c2 = st.columns(2)
    with c1:
        if st.button("🎫 Create Case & Notify IT", type="primary", use_container_width=True):
            with st.spinner("Creating Salesforce case and sending email…"):
                _do_escalate(priority, extra)
            st.rerun()
    with c2:
        if st.button("Cancel", use_container_width=True):
            st.session_state.show_escalation = False
            st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# PAGE: ONBOARDING
# ══════════════════════════════════════════════════════════════════════════════
def page_onboarding():
    st.markdown("## 🤖 Worksoft AI Support Assistant")
    st.markdown("Enter your details below to start a support session.")
    st.divider()

    with st.form("onboard_form", clear_on_submit=False):
        col1, col2 = st.columns(2)
        name    = col1.text_input("Name *",            placeholder="Alex Johnson")
        email   = col2.text_input("Corporate Email *", placeholder="alex@acme.com")
        company = st.text_input("Company Name *",      placeholder="Acme Corp")
        go      = st.form_submit_button("Continue →", use_container_width=True, type="primary")

        if go:
            errors = []
            if not name.strip():
                errors.append("Name is required.")
            if not email.strip() or "@" not in email:
                errors.append("A valid corporate email is required.")
            if not company.strip():
                errors.append("Company name is required.")

            if errors:
                for err in errors:
                    st.error(err)
            else:
                with st.spinner("Starting your session…"):
                    sid   = create_session(name.strip(), email.strip())
                    first = name.strip().split()[0]
                    welcome = (
                        f"Hello {first}! I'm your AI Support Assistant for Worksoft. "
                        f"How can I help you today? You can describe any issue with CTM, Certify, "
                        f"Portal, or Capture — or ask anything about your Worksoft environment."
                    )
                    save_message(sid, "assistant", welcome)

                st.session_state.update({
                    "page":          "chat",
                    "name":          name.strip(),
                    "email":         email.strip(),
                    "company":       company.strip(),
                    "sid":           sid,
                    "messages":      [{"role": "assistant", "content": welcome}],
                    "sf_resolution": "",
                })
                st.rerun()

    st.divider()

    # Salesforce sync
    st.markdown("##### 🔄 Salesforce Knowledge Base")
    st.caption("Sync resolved cases from Salesforce to improve AI answers.")
    if st.button("Sync Now", use_container_width=False):
        with st.spinner("Syncing Salesforce cases…"):
            ok, msg = sync_sf_knowledge()
        if ok:
            st.success(f"✅ {msg}")
        else:
            st.error(f"❌ {msg}")

    # System status
    with st.expander("ℹ️ System Status"):
        st.write(f"**AI Provider:** `{AI_PROVIDER}`")
        cols = st.columns(3)
        cols[0].metric("Groq",       "✅ Ready" if GROQ_API_KEY       else "❌ Not set")
        cols[1].metric("Claude",     "✅ Ready" if ANTHROPIC_API_KEY  else "❌ Not set")
        cols[2].metric("OpenAI",     "✅ Ready" if OPENAI_API_KEY     else "❌ Not set")
        cols2 = st.columns(3)
        cols2[0].metric("Gemini",    "✅ Ready" if GEMINI_API_KEY_SET else "❌ Not set")
        cols2[1].metric("Salesforce","✅ Ready" if (SF_CLIENT_ID and SF_USERNAME) else "❌ Not set")
        cols2[2].metric("Email",     "✅ Ready" if OUTLOOK_EMAIL      else "❌ Not set")

# ══════════════════════════════════════════════════════════════════════════════
# PAGE: CHAT
# ══════════════════════════════════════════════════════════════════════════════
def page_chat():
    # Top bar
    hdr, btn_resolve, btn_escalate = st.columns([3, 1.2, 1.2])
    hdr.markdown(f"### 🤖 Worksoft AI Support")
    hdr.caption(f"Session: {st.session_state.name} · {st.session_state.company}")

    with btn_resolve:
        if st.button("✅ Resolved", use_container_width=True):
            if st.session_state.sid:
                update_session_status(st.session_state.sid, "resolved")
            st.success("Great to hear! Session marked as resolved. 🎉")

    with btn_escalate:
        if st.button("🚨 Escalate", use_container_width=True, type="secondary"):
            st.session_state.show_escalation = True
            st.rerun()

    st.divider()

    # Ticket confirmation banner
    if st.session_state.escalation_done and st.session_state.ticket_info:
        t = st.session_state.ticket_info
        badge = "✅ Email sent to IT" if st.session_state.email_ok else "⚠️ Email not sent"
        st.success(
            f"**Case #{t.get('case_number')} created** · "
            f"Priority: {t.get('priority')} · {badge}"
        )
        if t.get("url") and t["url"] not in ("#", "#sf-not-configured"):
            st.markdown(f"[View in Salesforce ↗]({t['url']})")

    # Render conversation history
    for msg in st.session_state.messages:
        avatar = "🤖" if msg["role"] == "assistant" else "👤"
        with st.chat_message(msg["role"], avatar=avatar):
            st.markdown(msg["content"])

    # Optional file attachment
    uploaded = st.file_uploader(
        "Attach a screenshot or file (optional)",
        type=["png","jpg","jpeg","gif","webp","bmp","pdf","txt","log"],
        label_visibility="collapsed",
        key="file_uploader",
    )

    # Chat input
    if prompt := st.chat_input("Describe your Worksoft issue…"):
        file_data    = _process_file(uploaded)
        display_text = prompt
        if file_data:
            display_text += f"\n\n📎 *Attached: {file_data['name']}*"

        # Append and display user message
        st.session_state.messages.append({"role": "user", "content": display_text})
        save_message(st.session_state.sid, "user", display_text)
        with st.chat_message("user", avatar="👤"):
            st.markdown(display_text)

        # Prepare history (exclude the message we just added)
        history = [
            m for m in st.session_state.messages[:-1]
            if m["role"] in ("user", "assistant")
        ]

        # Stream the AI response
        gen, meta_container = _make_stream(
            prompt, history, st.session_state.sf_resolution, file_data
        )
        with st.chat_message("assistant", avatar="🤖"):
            full_reply = st.write_stream(gen)

        # Persist the reply
        st.session_state.messages.append({"role": "assistant", "content": full_reply})
        save_message(st.session_state.sid, "assistant", full_reply)

        # Update state from metadata
        if meta_container.get("sf_resolution"):
            st.session_state.sf_resolution = meta_container["sf_resolution"]
        if meta_container.get("resolved") or meta_container.get("stuck"):
            st.toast("💡 Tip: use Escalate if you need IT to take over.", icon="🚨")

        st.rerun()

    # Open escalation dialog if requested
    if st.session_state.show_escalation:
        escalation_dialog()

# ══════════════════════════════════════════════════════════════════════════════
# ROUTER
# ══════════════════════════════════════════════════════════════════════════════
if st.session_state.page == "chat":
    page_chat()
else:
    page_onboarding()
