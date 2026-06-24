"""
app.py — Flask backend integrating Worksoft AI support logic
with the existing frontend (index.html / chat.html / style.css / chat.js).

Run:
    python app.py

Requires a .env file (or environment variables) — see .env.example
"""

import os, re, base64, io, json, uuid, hashlib, smtplib, logging, time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser

from flask import (
    Flask, request, jsonify, session,
    send_from_directory, Response, stream_with_context
)
from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Flask app ───────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-in-prod")

# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════
def _secret(key, default=""):
    return os.environ.get(key, default)

GROQ_API_KEY      = _secret("GROQ_API_KEY")
ANTHROPIC_API_KEY = _secret("ANTHROPIC_API_KEY")
OPENAI_API_KEY    = _secret("OPENAI_API_KEY")
GEMINI_API_KEY    = _secret("GEMINI_API_KEY")

SF_USERNAME       = _secret("SF_USERNAME")
SF_PASSWORD       = _secret("SF_PASSWORD")
SF_SECURITY_TOKEN = _secret("SF_SECURITY_TOKEN")
SF_CLIENT_ID      = _secret("SF_CLIENT_ID")
SF_CLIENT_SECRET  = _secret("SF_CLIENT_SECRET")
SF_DOMAIN         = _secret("SF_DOMAIN", "login")
SF_INSTANCE_URL   = _secret("SF_INSTANCE_URL", "")
IT_ADMIN_EMAIL    = _secret("IT_ADMIN_EMAIL", "it-admin@qualesce.com")
OUTLOOK_EMAIL     = _secret("OUTLOOK_EMAIL")
OUTLOOK_PASSWORD  = _secret("OUTLOOK_PASSWORD")
PORTAL_URL        = SF_INSTANCE_URL or f"https://{SF_DOMAIN}.salesforce.com"

# ── AI model names — all overridable from .env ──────────────────────────────
_GROQ_MODEL        = _secret("GROQ_MODEL",        "llama-3.3-70b-versatile")
_GROQ_FAST_MODEL   = _secret("GROQ_FAST_MODEL",   "llama-3.1-8b-instant")
_GROQ_VISION_MODEL = _secret("GROQ_VISION_MODEL", "llama-3.2-11b-vision-preview")
_OPENAI_MODEL      = _secret("OPENAI_MODEL",      "gpt-4o")
_OPENAI_FAST_MODEL = _secret("OPENAI_FAST_MODEL", "gpt-4o-mini")
_CLAUDE_MODEL      = _secret("CLAUDE_MODEL",      "claude-sonnet-4-6")
_CLAUDE_FAST_MODEL = _secret("CLAUDE_FAST_MODEL", "claude-haiku-4-5-20251001")
_GEMINI_MODEL      = _secret("GEMINI_MODEL",      "gemini-2.0-flash")
_GEMINI_FAST_MODEL = _secret("GEMINI_FAST_MODEL", "gemini-2.0-flash")

# ── AI provider priority (from .env, default: groq) ─────────────────────────
# Set AI_PROVIDER=gemini in .env to use Gemini as primary
AI_PROVIDER = _secret("AI_PROVIDER", "groq").lower()

# ═══════════════════════════════════════════════════════════
# DATABASE  (SQLite)
# ═══════════════════════════════════════════════════════════
import sqlite3, threading

DB_PATH  = _secret("DB_PATH", "support.db")
_db_lock = threading.Lock()

def _get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sf_cases (
            sf_case_id   TEXT PRIMARY KEY,
            case_number  TEXT,
            subject      TEXT,
            description  TEXT,
            status       TEXT,
            resolution   TEXT,
            comments     TEXT,
            snippet      TEXT,
            updated_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS sf_cases_fts
            USING fts5(sf_case_id UNINDEXED, subject, description, resolution, comments);
        CREATE TABLE IF NOT EXISTS sessions (
            id           TEXT PRIMARY KEY,
            user_name    TEXT,
            user_email   TEXT,
            status       TEXT DEFAULT 'active',
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT,
            role         TEXT,
            content      TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS tickets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT,
            user_name       TEXT,
            user_email      TEXT,
            issue_text      TEXT,
            priority        TEXT,
            sf_case_number  TEXT,
            sf_case_url     TEXT,
            sf_error        TEXT,
            email_sent      INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            synced_at   TEXT DEFAULT (datetime('now')),
            cases_count INTEGER DEFAULT 0
        );
        """)

def create_session(name, email):
    sid = str(uuid.uuid4())
    with _get_db() as conn:
        conn.execute("INSERT INTO sessions (id,user_name,user_email) VALUES (?,?,?)",
                     (sid, name, email))
    return sid

def save_message(sid, role, content):
    with _get_db() as conn:
        conn.execute("INSERT INTO messages (session_id,role,content) VALUES (?,?,?)",
                     (sid, role, content))

def update_session_status(sid, status):
    with _get_db() as conn:
        conn.execute("UPDATE sessions SET status=? WHERE id=?", (status, sid))

def save_ticket(session_id, user_name, user_email, issue_text, priority,
                sf_case_number, sf_case_url, sf_error, email_sent):
    with _get_db() as conn:
        conn.execute("""INSERT INTO tickets
            (session_id,user_name,user_email,issue_text,priority,
             sf_case_number,sf_case_url,sf_error,email_sent)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (session_id, user_name, user_email, issue_text, priority,
             sf_case_number, sf_case_url, sf_error, int(email_sent)))

def upsert_sf_case(sf_case_id, case_number, subject, description, status,
                   resolution, comments):
    snippet = (comments or description or "")[:200]
    with _get_db() as conn:
        conn.execute("""
        INSERT INTO sf_cases (sf_case_id,case_number,subject,description,status,resolution,comments,snippet)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(sf_case_id) DO UPDATE SET
            case_number=excluded.case_number,
            subject=excluded.subject,
            description=excluded.description,
            status=excluded.status,
            resolution=excluded.resolution,
            comments=excluded.comments,
            snippet=excluded.snippet,
            updated_at=datetime('now')
        """, (sf_case_id, case_number, subject, description, status,
              resolution, comments, snippet))
        conn.execute("DELETE FROM sf_cases_fts WHERE sf_case_id=?", (sf_case_id,))
        conn.execute("INSERT INTO sf_cases_fts (sf_case_id,subject,description,resolution,comments) VALUES (?,?,?,?,?)",
                     (sf_case_id, subject or "", description or "", resolution or "", comments or ""))

def delete_removed_cases(active_ids):
    with _get_db() as conn:
        existing = [r["sf_case_id"] for r in conn.execute("SELECT sf_case_id FROM sf_cases").fetchall()]
        to_delete = [i for i in existing if i not in active_ids]
        for cid in to_delete:
            conn.execute("DELETE FROM sf_cases WHERE sf_case_id=?", (cid,))
            conn.execute("DELETE FROM sf_cases_fts WHERE sf_case_id=?", (cid,))
        return len(to_delete)

def update_sync_log(count):
    with _get_db() as conn:
        conn.execute("INSERT INTO sync_log (cases_count) VALUES (?)", (count,))

def get_all_case_subjects():
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT sf_case_id,case_number,subject,snippet FROM sf_cases ORDER BY rowid DESC LIMIT 300"
        ).fetchall()
    return [dict(r) for r in rows]

def get_cases_by_ids(ids):
    if not ids: return []
    ph = ",".join("?" * len(ids))
    with _get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM sf_cases WHERE sf_case_id IN ({ph})", ids
        ).fetchall()
    return [dict(r) for r in rows]

def search_knowledge(query, top_n=12):
    try:
        with _get_db() as conn:
            rows = conn.execute(
                "SELECT sf_case_id FROM sf_cases_fts WHERE sf_cases_fts MATCH ? LIMIT ?",
                (query, top_n)
            ).fetchall()
        return [{"sf_case_id": r["sf_case_id"]} for r in rows]
    except Exception:
        return []

# Init DB on startup
init_db()

# ═══════════════════════════════════════════════════════════
# HTML HELPERS
# ═══════════════════════════════════════════════════════════
def _strip_html(html_text):
    class _S(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
        def handle_data(self, d):
            self.parts.append(d)
    p = _S()
    p.feed(html_text or "")
    return re.sub(r"\s+", " ", "".join(p.parts)).strip()

# ═══════════════════════════════════════════════════════════
# SALESFORCE
# ═══════════════════════════════════════════════════════════
def _sf_client():
    import requests as _req
    from simple_salesforce import Salesforce
    resp = _req.post(
        f"https://{SF_DOMAIN}.salesforce.com/services/oauth2/token",
        data={
            "grant_type": "password",
            "client_id": SF_CLIENT_ID,
            "client_secret": SF_CLIENT_SECRET,
            "username": SF_USERNAME,
            "password": SF_PASSWORD + SF_SECURITY_TOKEN,
        }, timeout=15)
    resp.raise_for_status()
    t = resp.json()
    return Salesforce(session_id=t["access_token"], instance_url=t["instance_url"])

def sf_create_case(subject, description, user_name, user_email, priority="High"):
    sf  = _sf_client()
    res = sf.Case.create({
        "Subject": subject,
        "Description": f"Reported By: {user_name}\nEmail: {user_email}\n\n{description}",
        "Status": "New", "Priority": priority, "Origin": "Web",
    })
    cid  = res["id"]
    data = sf.Case.get(cid)
    url  = sf.base_url.split("/services")[0] + f"/lightning/r/Case/{cid}/view"
    return {"id": cid, "case_number": data.get("CaseNumber", cid), "url": url}

# ═══════════════════════════════════════════════════════════
# SF KNOWLEDGE SYNC
# ═══════════════════════════════════════════════════════════
def sync_sf_knowledge():
    try:
        sf = _sf_client()
        cases = None
        use_cc_subquery = False
        _EXTRA = "Type,Reason,Origin,Priority"
        _QUERIES = [
            (True,  f"SELECT Id,CaseNumber,Subject,Description,Status,Resolution__c,Comments,{_EXTRA},"
                    "(SELECT CommentBody,CreatedDate FROM CaseComments ORDER BY CreatedDate ASC) "
                    "FROM Case ORDER BY LastModifiedDate DESC LIMIT 1000"),
            (True,  f"SELECT Id,CaseNumber,Subject,Description,Status,{_EXTRA},"
                    "(SELECT CommentBody,CreatedDate FROM CaseComments ORDER BY CreatedDate ASC) "
                    "FROM Case ORDER BY LastModifiedDate DESC LIMIT 1000"),
            (False, "SELECT Id,CaseNumber,Subject,Description,Status,Type,Reason,Origin,Priority "
                    "FROM Case ORDER BY LastModifiedDate DESC LIMIT 1000"),
            (False, "SELECT Id,CaseNumber,Subject,Description,Status "
                    "FROM Case ORDER BY LastModifiedDate DESC LIMIT 1000"),
        ]
        for _has_sub, _q in _QUERIES:
            try:
                _r = sf.query_all(_q)
                cases = _r.get("records", [])
                use_cc_subquery = _has_sub
                break
            except Exception:
                continue

        if cases is None:
            return False, "All Salesforce queries failed. Check API credentials and field permissions."
        if not cases:
            return False, "No cases found in Salesforce."

        synced = 0; empty_content = 0; errors = []; active_ids = []
        for case in cases:
            cid         = case["Id"]
            case_number = case.get("CaseNumber", "")
            subject     = case.get("Subject",     "") or ""
            description = case.get("Description", "") or ""
            status      = case.get("Status",      "") or ""
            resolution  = str(case.get("Resolution__c") or case.get("Comments") or "")
            active_ids.append(cid)

            comments_list = []
            if use_cc_subquery:
                cc = case.get("CaseComments")
                if cc and isinstance(cc, dict):
                    for c in cc.get("records", []):
                        body = (c.get("CommentBody") or "").strip()
                        if body: comments_list.append(body)
            else:
                try:
                    res = sf.query_all(
                        f"SELECT CommentBody FROM CaseComment WHERE ParentId='{cid}' ORDER BY CreatedDate ASC")
                    for c in res.get("records", []):
                        body = (c.get("CommentBody") or "").strip()
                        if body: comments_list.append(body)
                except Exception as e:
                    errors.append(f"CaseComment [{case_number}]: {e}")

            try:
                res = sf.query_all(
                    f"SELECT Body,"
                    f"(SELECT CommentBody,CreatedDate FROM FeedComments ORDER BY CreatedDate ASC) "
                    f"FROM FeedItem WHERE ParentId='{cid}' ORDER BY CreatedDate ASC LIMIT 30")
                for fi in res.get("records", []):
                    body = _strip_html(fi.get("Body") or "").strip()
                    if body: comments_list.append(body)
                    fc_result = fi.get("FeedComments")
                    if fc_result and isinstance(fc_result, dict):
                        for fc in fc_result.get("records", []):
                            fc_body = _strip_html(fc.get("CommentBody") or "").strip()
                            if fc_body: comments_list.append(fc_body)
            except Exception as e:
                errors.append(f"FeedItem [{case_number}]: {e}")

            try:
                res = sf.query_all(
                    f"SELECT TextBody FROM EmailMessage WHERE ParentId='{cid}' ORDER BY MessageDate ASC LIMIT 20")
                for em in res.get("records", []):
                    body = (em.get("TextBody") or "").strip()
                    if body: comments_list.append(body)
            except Exception as e:
                errors.append(f"EmailMessage [{case_number}]: {e}")

            comments_text = "\n\n".join(comments_list)
            upsert_sf_case(
                sf_case_id=cid, case_number=case_number, subject=subject,
                description=description, status=status,
                resolution=resolution, comments=comments_text,
            )
            if not (comments_text or description or resolution):
                empty_content += 1
            synced += 1

        deleted = delete_removed_cases(active_ids)
        update_sync_log(synced)

        msg = f"Synced {synced} cases from Salesforce."
        if empty_content:
            msg += f" {empty_content} case(s) have no content."
        if deleted:
            msg += f" Removed {deleted} stale case(s)."
        return True, msg
    except Exception as exc:
        return False, f"Sync failed: {exc}"

# ═══════════════════════════════════════════════════════════
# EMAIL
# ═══════════════════════════════════════════════════════════
def _send_smtp(sender, password, to, subject, body, cc=""):
    msg = MIMEMultipart("alternative")
    msg["From"] = sender; msg["To"] = to; msg["Subject"] = subject
    if cc: msg["CC"] = cc
    msg.attach(MIMEText(body, "html"))
    recipients = [to] + ([cc] if cc else [])
    last_err = ""
    for host, port in [("smtp.office365.com", 587), ("smtp.gmail.com", 587)]:
        try:
            with smtplib.SMTP(host, port) as srv:
                srv.ehlo(); srv.starttls(); srv.login(sender, password)
                srv.sendmail(sender, recipients, msg.as_string())
            return True, ""
        except Exception as exc:
            last_err = str(exc)
    return False, last_err

def send_escalation_email(ticket, user_name, user_email, issue, conversation):
    sender, password = OUTLOOK_EMAIL, OUTLOOK_PASSWORD
    if not sender or not password:
        return False, "SMTP credentials not configured"

    case_num = ticket.get("case_number", ticket.get("id", "N/A"))
    case_url = ticket.get("url", "#")
    created  = datetime.now().strftime("%d %b %Y, %H:%M")
    priority = ticket.get("priority", "High")

    sf_btn = (
        f'<a href="{case_url}" style="display:inline-block;background:linear-gradient(135deg,#1d4ed8,#3b82f6);'
        f'color:#fff;text-decoration:none;font-weight:700;font-size:13px;padding:10px 24px;'
        f'border-radius:10px;margin-top:4px;">View Case in Salesforce</a>'
        if case_url != "#sf-not-configured" else
        '<p style="color:#94a3b8;font-size:12px;">Salesforce link unavailable.</p>'
    )

    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     max-width:600px;margin:0 auto;padding:32px;background:#0f172a;border-radius:16px;color:#e2e8f0;">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:24px;">
    <div style="font-size:26px;font-weight:900;color:#3b82f6;">Q</div>
    <div>
      <div style="font-size:16px;font-weight:800;color:#f1f5f9;">Worksoft Support Escalation</div>
      <div style="font-size:12px;color:#64748b;">Qualesce AI Support Agent</div>
    </div>
  </div>
  <div style="background:rgba(59,130,246,.15);border-left:4px solid #3b82f6;border-radius:0 10px 10px 0;
       padding:14px 18px;margin-bottom:20px;">
    <div style="font-size:10px;font-weight:700;color:#93c5fd;text-transform:uppercase;margin-bottom:4px;">New Case</div>
    <div style="font-size:22px;font-weight:900;color:#f1f5f9;">#{case_num}</div>
    <div style="font-size:12px;color:#94a3b8;margin-top:4px;">Created: {created}</div>
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:13px;">
    <tr><td style="padding:4px 0;color:#94a3b8;width:35%;">Reported By</td>
        <td style="font-weight:600;color:#f1f5f9;">{user_name}</td></tr>
    <tr><td style="color:#94a3b8;">Email</td>
        <td style="font-weight:600;color:#f1f5f9;">{user_email}</td></tr>
    <tr><td style="color:#94a3b8;">Priority</td>
        <td><span style="background:rgba(220,38,38,.2);color:#f87171;font-size:10px;
              font-weight:700;padding:2px 10px;border-radius:99px;">{priority.upper()}</span></td></tr>
  </table>
  <div style="background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:10px;
       padding:16px 20px;margin:14px 0;">
    <div style="font-size:12px;font-weight:700;color:#93c5fd;margin-bottom:8px;">ISSUE</div>
    <div style="font-size:13px;color:#cbd5e1;line-height:1.6;">{issue}</div>
  </div>
  <div style="background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:10px;
       padding:16px 20px;margin-bottom:20px;">
    <div style="font-size:12px;font-weight:700;color:#93c5fd;margin-bottom:8px;">AI CHAT CONVERSATION</div>
    <div style="font-size:12px;color:#94a3b8;line-height:1.7;white-space:pre-wrap;
         background:rgba(0,0,0,.2);border-radius:6px;padding:12px;">{conversation}</div>
  </div>
  {sf_btn}
  <p style="color:#475569;font-size:11px;border-top:1px solid rgba(255,255,255,.08);
     padding-top:14px;margin:20px 0 0;">
    Automated escalation · Qualesce Worksoft Support Portal
  </p>
</div>"""

    return _send_smtp(sender, password, IT_ADMIN_EMAIL,
                      f"[Worksoft Support] New Escalation – Case {case_num}",
                      html, cc=user_email)

# ═══════════════════════════════════════════════════════════
# FILE PROCESSING
# ═══════════════════════════════════════════════════════════
def _process_upload(file_storage):
    if file_storage is None: return None
    name = file_storage.filename
    mime = file_storage.content_type or ""
    raw  = file_storage.read()
    if mime.startswith("image/") or name.lower().endswith((".png",".jpg",".jpeg",".gif",".webp",".bmp")):
        if not mime.startswith("image/"): mime = "image/png"
        return {"type":"image","name":name,"mime":mime,"base64":base64.b64encode(raw).decode()}
    if mime == "application/pdf" or name.lower().endswith(".pdf"):
        return {"type":"text","name":name,"content":_pdf_text(raw)}
    try:    text = raw.decode("utf-8", errors="replace")
    except: text = str(raw[:3000])
    return {"type":"text","name":name,"content":text[:4000]}

def _pdf_text(raw):
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)[:4000]
    except Exception: pass
    try:
        import pypdf
        r = pypdf.PdfReader(io.BytesIO(raw))
        return "\n".join(p.extract_text() or "" for p in r.pages)[:4000]
    except Exception:
        return "[PDF could not be extracted — please describe the issue in text]"

# ═══════════════════════════════════════════════════════════
# AI DOMAIN KNOWLEDGE
# ═══════════════════════════════════════════════════════════
_EXPERT_PERSONA = (
    "You are a warm, friendly, and highly experienced Worksoft support specialist at Qualesce. "
    "Think of yourself as a patient and caring colleague sitting right next to the user — someone who genuinely "
    "wants to understand their problem before jumping to solutions. "
    "\n\nYOUR PERSONALITY:\n"
    "- Empathetic: always acknowledge how the user feels before diving into details. "
    "- Curious: ask thoughtful follow-up questions to understand the full picture. Never assume — always verify. "
    "- Encouraging: celebrate small wins, reassure when things are unclear. "
    "- Natural: use contractions, vary your openers, never sound scripted. "
    "- Concise but human: no filler phrases, no walls of text — conversational paragraphs. "
    "\n\nYOUR APPROACH:\n"
    "- First understand, then solve. Never rush to give steps before you know what's really happening. "
    "- When you do give steps, walk the user through them like a friend would — explain each step in plain English. "
    "- After every response that includes steps, invite the user to tell you what happened. "
    "- If the user seems confused or frustrated, slow down, simplify, and show extra care. "
    "\n\nYou have deep expertise in Worksoft CTM, Certify, Portal, Capture, agent machines, IIS, and appsettings. "
    "When Salesforce case data is available, use it as your ground truth. "
    "When it's not, troubleshoot confidently from your domain knowledge."
)

_WORKSOFT_DOMAIN = """
=== WORKSOFT PRODUCT KNOWLEDGE ===

WORKSOFT CTM (Continuous Testing Manager)
- Orchestrates test execution across agent machines via a web-based dashboard
- Test suites, jobs, and schedules are dispatched to Windows agent VMs
- Key config: appsettings.config (CTM URL, timeouts, DB connection string)
- Key paths: C:\\inetpub\\wwwroot\\CTM\\, IIS Application Pool "CTMAppPool"
- Common issues & fixes:
    • Agent offline / disconnected → restart "Worksoft CTM Agent" Windows service on the agent machine; verify CTM_URL in agent config matches the CTM server URL
    • Suite stuck in "Abort Pending" → use CTM Force Abort; if unresponsive, restart agent service + iisreset on CTM server
    • Execution timeout → increase ExecutionTimeout in appsettings.config
    • CTM UI not loading → iisreset; check IIS app pool is Started; check event viewer for 500 errors
    • Jobs not dispatching → verify agent machine heartbeat in CTM > Agents tab; check firewall rules on port 8080/443

WORKSOFT CERTIFY
- Windows desktop automation tool; records and plays back UI interactions
- Common issues & fixes:
    • Object not found / recognition failure → update object snapshot; add a Sync or Wait step before the action
    • Playback too fast → add Delay or Sync steps; lower playback speed in settings
    • License checkout failure → check license server connectivity; release checked-out licenses from admin console
    • Variable / data table errors → verify variable binding in test, check CSV/Excel data file path
    • .NET errors on launch → run as Administrator; repair Certify install; clear %APPDATA%\\Worksoft

WORKSOFT PORTAL
- Web portal for test assets, user management, and reporting (IIS-hosted)
- Common issues & fixes:
    • Login / SSO failure → check AD group membership; verify SSO config in Portal web.config; clear browser cookies
    • 403 / 401 errors → check IIS authentication settings; verify user has correct Portal role
    • Slow load / timeout → recycle IIS app pool; check SQL Server connection; review IIS logs at C:\\inetpub\\logs
    • Page not loading after update → iisreset; clear browser cache; check Portal app pool identity

WORKSOFT CAPTURE
- Browser extension + server for recording business process documentation
- Common issues: plugin not loading → reinstall browser extension; check Capture server URL in plugin settings

AGENT MACHINES (Windows VMs running Certify playback)
- Service name: "Worksoft CTM Agent" (in services.msc)
- Config file: CTMAgent.exe.config (CTM server URL, heartbeat interval)
- Fix pattern: Stop service → verify config → Start service → confirm green heartbeat in CTM dashboard
- Firewall: agent must reach CTM server on configured port (default 443 or 8080)

UNIVERSAL WORKSOFT FIX TOOLKIT
1. iisreset (run as admin) — recycles all IIS app pools; fixes most web/portal issues
2. Restart Worksoft Windows services — open services.msc, restart anything prefixed "Worksoft"
3. Check appsettings.config — wrong URLs, connection strings, or timeouts cause ~40% of issues
4. Windows Event Viewer → Application/System logs — reveals the real error behind a vague UI message
5. IIS logs at C:\\inetpub\\logs\\LogFiles\\ — shows exact HTTP errors for web components
6. SQL Server connectivity — test with SSMS if DB-related errors; check connection string in config
7. Run as Administrator — many Worksoft components require elevated privileges
=== END WORKSOFT KNOWLEDGE ===
"""

_GREET_WORDS = {"hi","hello","hey","hii","helo","heya","howdy","greetings",
                "morning","afternoon","evening","sup","yo","namaste","hai","hola"}
_GREETINGS   = _GREET_WORDS | {
    "good morning","good afternoon","good evening","good day",
    "what's up","whats up","hi there","hey there","hello there",
}

def _is_greeting(text):
    clean = re.sub(r"[^a-z\s']", "", text.lower()).strip()
    first = clean.split()[0] if clean.split() else ""
    return (
        clean in _GREETINGS
        or all(w in _GREET_WORDS for w in clean.split() if w)
        or (first in _GREET_WORDS and len(clean.split()) <= 4 and not any(c.isdigit() for c in clean))
    )

# ═══════════════════════════════════════════════════════════
# AI ENGINE — Gemini / Groq / Claude / OpenAI
# Priority order set by AI_PROVIDER in .env
# ═══════════════════════════════════════════════════════════

# ── Gemini ──────────────────────────────────────────────────
def _ask_gemini(system_prompt, user_prompt, max_tokens=800, history=None,
                fast=False, stream=False, temperature=0.3):
    if not GEMINI_API_KEY:
        return _ask_groq(system_prompt, user_prompt, max_tokens, history, fast, stream, temperature)
    effective_temp = 0.0 if fast else temperature
    model_name = _GEMINI_FAST_MODEL if fast else _GEMINI_MODEL
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=system_prompt,
        )
        # Build Gemini chat history
        chat_history = []
        if history:
            for m in history[-10:]:
                role = "user" if m.get("role") == "user" else "model"
                if m.get("content"):
                    chat_history.append({"role": role, "parts": [str(m["content"])[:2000]]})
        chat = model.start_chat(history=chat_history)

        gen_cfg = genai.types.GenerationConfig(
            max_output_tokens=max_tokens,
            temperature=effective_temp,
        )

        if stream:
            def _gen():
                response = chat.send_message(user_prompt, generation_config=gen_cfg, stream=True)
                for chunk in response:
                    if chunk.text:
                        yield chunk.text
            return _gen()

        response = chat.send_message(user_prompt, generation_config=gen_cfg)
        return response.text or ""
    except Exception as e:
        log.error(f"Gemini error: {e}")
        # Fallback to Groq on error
        return _ask_groq(system_prompt, user_prompt, max_tokens, history, fast, stream, temperature)

# ── Groq ────────────────────────────────────────────────────
def _ask_groq(system_prompt, user_prompt, max_tokens=800, history=None,
              fast=False, stream=False, temperature=0.3, model_override=None):
    if not GROQ_API_KEY:
        return _ask_claude(system_prompt, user_prompt, max_tokens, history, fast, stream, temperature)
    effective_temp = 0.0 if fast else temperature
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        model  = model_override or (_GROQ_FAST_MODEL if fast else _GROQ_MODEL)

        messages = [{"role": "system", "content": system_prompt}]
        if history:
            hist = history[-20:]
            for i, msg in enumerate(hist):
                role    = msg.get("role", "")
                content = msg.get("content", "")
                if i == len(hist)-1 and role == "user" and content.strip() == user_prompt.strip():
                    continue
                if role in ("user","assistant") and content:
                    messages.append({"role": role, "content": str(content)[:2000]})
        messages.append({"role": "user", "content": user_prompt})

        if stream:
            def _gen():
                full = ""; think_buf = ""; think_closed = False
                completion = client.chat.completions.create(
                    model=model, messages=messages,
                    max_tokens=max_tokens, temperature=effective_temp, stream=True,
                )
                for chunk in completion:
                    delta = chunk.choices[0].delta.content or ""
                    if not think_closed:
                        think_buf += delta
                        if "</think>" in think_buf:
                            after = think_buf.split("</think>", 1)[-1].lstrip("\n")
                            full = after; think_closed = True
                            if full: yield full
                        elif "<think>" not in think_buf and len(think_buf) > 60:
                            full = think_buf; think_closed = True
                            if full: yield full
                    else:
                        full += delta
                        yield delta
            return _gen()

        completion = client.chat.completions.create(
            model=model, messages=messages,
            max_tokens=max_tokens, temperature=effective_temp,
        )
        raw = (completion.choices[0].message.content or "").strip()
        if "<think>" in raw and "</think>" in raw:
            raw = raw.split("</think>", 1)[-1].strip()
        return raw
    except Exception as e:
        log.error(f"Groq error: {e}")
        return _ask_claude(system_prompt, user_prompt, max_tokens, history, fast, stream, temperature)

# ── Claude ──────────────────────────────────────────────────
def _ask_claude(system_prompt, user_prompt, max_tokens, history, fast, stream=False, temperature=0.3):
    if not ANTHROPIC_API_KEY:
        return _ask_openai(system_prompt, user_prompt, max_tokens, history, fast)
    effective_temp = 0.0 if fast else temperature
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        model  = _CLAUDE_FAST_MODEL if fast else _CLAUDE_MODEL
        msgs   = []
        if history:
            for m in history[-8:]:
                if m.get("role") in ("user","assistant") and m.get("content"):
                    msgs.append({"role": m["role"], "content": str(m["content"])[:600]})
        msgs.append({"role": "user", "content": user_prompt})

        if stream:
            def _gen():
                with client.messages.stream(model=model, system=system_prompt,
                                            messages=msgs, max_tokens=max_tokens) as s:
                    for txt in s.text_stream:
                        yield txt
            return _gen()

        r = client.messages.create(model=model, system=system_prompt,
                                   messages=msgs, max_tokens=max_tokens, temperature=effective_temp)
        return (r.content[0].text or "") if r.content else ""
    except Exception as e:
        log.error(f"Claude error: {e}")
        return _ask_openai(system_prompt, user_prompt, max_tokens, history, fast)

# ── OpenAI ──────────────────────────────────────────────────
def _ask_openai(system_prompt, user_prompt, max_tokens, history, fast):
    if not OPENAI_API_KEY:
        return ""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        model  = _OPENAI_FAST_MODEL if fast else _OPENAI_MODEL
        msgs   = [{"role": "system", "content": system_prompt}]
        if history:
            for m in history[-8:]:
                if m.get("role") in ("user","assistant") and m.get("content"):
                    msgs.append({"role": m["role"], "content": str(m["content"])[:600]})
        msgs.append({"role": "user", "content": user_prompt})
        r = client.chat.completions.create(model=model, messages=msgs,
                                           max_tokens=max_tokens, temperature=0.45)
        return (r.choices[0].message.content or "").strip()
    except Exception as e:
        log.error(f"OpenAI error: {e}")
        return ""

# ── Single entry-point — routes based on AI_PROVIDER in .env ─
def _ask_ai(system_prompt, user_prompt, max_tokens=800, history=None,
            fast=False, stream=False, temperature=0.3, model_override=None):
    """
    Routes to the correct AI provider based on AI_PROVIDER in .env.
    Default: groq
    Set AI_PROVIDER=gemini to use Gemini as primary.
    Fallback chain: gemini → groq → claude → openai
    """
    if AI_PROVIDER == "gemini":
        return _ask_gemini(system_prompt, user_prompt, max_tokens, history, fast, stream, temperature)
    elif AI_PROVIDER == "claude":
        return _ask_claude(system_prompt, user_prompt, max_tokens, history, fast, stream, temperature)
    elif AI_PROVIDER == "openai":
        return _ask_openai(system_prompt, user_prompt, max_tokens, history, fast)
    else:
        # Default: groq (with gemini as fallback if groq hits rate limit)
        if GROQ_API_KEY:
            return _ask_groq(system_prompt, user_prompt, max_tokens, history,
                             fast, stream, temperature, model_override=model_override)
        elif GEMINI_API_KEY:
            return _ask_gemini(system_prompt, user_prompt, max_tokens, history, fast, stream, temperature)
        elif ANTHROPIC_API_KEY:
            return _ask_claude(system_prompt, user_prompt, max_tokens, history, fast, stream, temperature)
        else:
            return _ask_openai(system_prompt, user_prompt, max_tokens, history, fast)

# ── Image description (Groq vision, falls back to Gemini) ───
def _describe_image(file_data, user_text):
    # Try Groq vision first
    if GROQ_API_KEY:
        try:
            from groq import Groq
            client = Groq(api_key=GROQ_API_KEY)
            r = client.chat.completions.create(
                model=_GROQ_VISION_MODEL,
                messages=[{"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{file_data['mime']};base64,{file_data['base64']}"}},
                    {"type": "text",
                     "text": (
                         "Describe the error or issue visible in this Worksoft screenshot in 2 short sentences. "
                         "Focus on error messages, codes, UI state, or warnings. "
                         f"User also says: {user_text or '(no description provided)'}"
                     )},
                ]}],
                max_tokens=200,
            )
            desc = (r.choices[0].message.content or "").strip()
            if desc:
                return f"{user_text} — Screenshot shows: {desc}".strip()
        except Exception as e:
            log.warning(f"Groq vision failed, trying Gemini: {e}")

    # Try Gemini vision as fallback
    if GEMINI_API_KEY:
        try:
            import google.generativeai as genai
            from google.generativeai.types import HarmCategory, HarmBlockThreshold
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel(_GEMINI_MODEL)
            import PIL.Image
            img = PIL.Image.open(io.BytesIO(base64.b64decode(file_data["base64"])))
            response = model.generate_content([
                img,
                f"Describe the error or issue visible in this Worksoft screenshot in 2 short sentences. "
                f"Focus on error messages, codes, UI state, or warnings. "
                f"User also says: {user_text or '(no description provided)'}"
            ])
            desc = (response.text or "").strip()
            if desc:
                return f"{user_text} — Screenshot shows: {desc}".strip()
        except Exception as e:
            log.warning(f"Gemini vision failed: {e}")

    return user_text

# ═══════════════════════════════════════════════════════════
# RETRIEVAL
# ═══════════════════════════════════════════════════════════
def _retrieve_best_cases(query, top_n=1):
    all_subs = get_all_case_subjects()
    if not all_subs: return []

    sub_lines = []
    for i, c in enumerate(all_subs[:300]):
        subj    = (c.get("subject","") or "").strip()
        snippet = (c.get("snippet","") or "").strip()[:150]
        cnum    = (c.get("case_number","") or "").strip()
        if not subj: continue
        line = f"{i+1}. [{cnum}] {subj}"
        if snippet: line += f" | {snippet}"
        sub_lines.append(line)

    if not sub_lines: return []

    s1_resp = _ask_ai(
        system_prompt=(
            "You are a Worksoft support case matcher. "
            "Each entry: index. [CaseNum] Subject | content snippet. "
            "Pick every case whose subject OR content MIGHT be related to the user's question. "
            "Be INCLUSIVE. Return ONLY position numbers e.g. '3, 7, 12'. "
            "If nothing is related: NONE"
        ),
        user_prompt=f"User question: {query}\n\nCases:\n" + "\n".join(sub_lines),
        max_tokens=80, stream=False, temperature=0.0,
    )

    candidate_ids = []
    if s1_resp and re.sub(r"[^a-zA-Z]","",str(s1_resp)).upper() != "NONE":
        for token in re.findall(r"\d+", str(s1_resp)):
            idx = int(token) - 1
            if 0 <= idx < len(all_subs):
                cid = all_subs[idx]["sf_case_id"]
                if cid not in candidate_ids:
                    candidate_ids.append(cid)

    kw_hits = search_knowledge(query, top_n=12)
    for m in kw_hits:
        cid = m["sf_case_id"]
        if cid not in candidate_ids:
            candidate_ids.append(cid)

    if not candidate_ids: return []

    candidates = get_cases_by_ids(candidate_ids[:20])
    if not candidates: return []
    if len(candidates) == 1: return candidates

    content_blocks = []
    for i, c in enumerate(candidates):
        case_num = (c.get("case_number","") or "").strip()
        subject  = (c.get("subject","")     or "").strip()
        comments = (c.get("comments","")    or "").strip()[:2500]
        desc     = (c.get("description","") or "").strip()[:800]
        body     = comments or desc or "(no content)"
        content_blocks.append(f"[{i+1}] Case #{case_num} — {subject}\n{body}")

    s2_resp = _ask_ai(
        system_prompt=(
            "You are a Worksoft support specialist. "
            f"Pick the best 1-{top_n} cases whose content genuinely answers the user's question. "
            "Return ONLY position numbers e.g. '2' or '1, 3'. "
            "If no case content actually addresses the issue: NONE"
        ),
        user_prompt=f"User question: {query}\n\n" + "\n\n---\n\n".join(content_blocks),
        max_tokens=30, stream=False, temperature=0.0,
    )

    if s2_resp and re.sub(r"[^a-zA-Z]","",str(s2_resp)).upper() != "NONE":
        final_ids = []
        for token in re.findall(r"\d+", str(s2_resp)):
            idx = int(token) - 1
            if 0 <= idx < len(candidates):
                cid = candidates[idx]["sf_case_id"]
                if cid not in final_ids:
                    final_ids.append(cid)
        if final_ids:
            return [c for c in candidates if c["sf_case_id"] in final_ids][:top_n]

    with_content = [c for c in candidates if (c.get("comments") or c.get("description") or "").strip()]
    return (with_content or candidates)[:top_n]

def _build_case_knowledge(matches, query=""):
    blocks = []; all_subjects = []
    for i, case in enumerate(matches, 1):
        subject    = (case.get("subject","")     or "").strip()
        desc       = (case.get("description","") or "").strip()
        resolution = (case.get("resolution","")  or "").strip()
        comments   = (case.get("comments","")    or "").strip()
        if subject: all_subjects.append(subject)
        parts = []
        if subject:    parts.append(f"Issue type: {subject}")
        if desc:       parts.append(f"Problem description:\n{desc[:1500]}")
        if resolution: parts.append(f"Resolution summary:\n{resolution[:1500]}")
        if comments:   parts.append(f"Detailed steps/comments:\n{comments[:3500]}")
        if len(parts) > 1:
            blocks.append(f"── Knowledge Entry {i} ──\n" + "\n\n".join(parts))

    knowledge_text  = "\n\n".join(blocks) if blocks else ""
    has_content     = bool(blocks)
    context_summary = "; ".join(all_subjects[:3]) if all_subjects else query
    return knowledge_text, has_content, context_summary

def _ai_detects_resolution(history):
    last_user = next((m["content"] for m in reversed(history) if m.get("role") == "user"), "")
    if not last_user or len(last_user.strip()) < 3: return False
    verdict = _ask_ai(
        system_prompt=(
            "You are reviewing a single support chat message. "
            "Answer with exactly one word — YES or NO:\n"
            "YES — the user is saying their issue is now fixed / resolved / working / sorted\n"
            "NO  — they are still having the problem, asking a question, or describing a failure\n"
            "One word only."
        ),
        user_prompt=f"User message: {last_user[:300]}",
        max_tokens=3, fast=True,
    )
    return str(verdict).strip().upper().startswith("YES")

def _ai_feels_stuck(history):
    recent = [m for m in history[-8:] if m.get("role") in ("user","assistant")]
    if not recent: return False
    convo_text = "\n".join(f"{m['role'].title()}: {m['content'][:300]}" for m in recent)
    verdict = _ask_ai(
        system_prompt=(
            "You are reviewing a Worksoft IT support chat. "
            "Read the conversation and respond with exactly one word:\n"
            "STUCK — the user's issue is not being resolved\n"
            "PROGRESSING — things are moving forward and a fix seems likely\n"
            "One word only. No explanation."
        ),
        user_prompt=f"Conversation so far:\n{convo_text}",
        max_tokens=5, fast=True,
    )
    return str(verdict).strip().upper().startswith("STUCK")

# ═══════════════════════════════════════════════════════════
# CHAT ENGINE
# ═══════════════════════════════════════════════════════════
def process_chat_stream(text, history, sf_resolution="", file_data=None):
    """Core AI processing. Returns a generator that yields text chunks."""

    if file_data and file_data["type"] == "image":
        query = _describe_image(file_data, text)
    elif file_data and file_data["type"] == "text":
        query = f"{text}\n\nAttached file content:\n{file_data['content'][:1200]}"
    else:
        query = text

    prior_user_msgs = [m["content"] for m in history if m.get("role") == "user"]
    if len(prior_user_msgs) >= 2:
        combined = " | ".join(prior_user_msgs[-4:])
        if text.strip().lower() not in combined.lower():
            combined = text + " | " + combined
        rich_query = combined[:800]
    else:
        rich_query = query

    matches = _retrieve_best_cases(rich_query, top_n=5) if (rich_query or query).strip() else []
    knowledge_text, has_content, ctx_summary = (
        _build_case_knowledge(matches, query) if matches else ("", "", "")
    )

    sf_section = ""
    if has_content:
        sf_section = (
            "\n\n=== SALESFORCE RESOLVED CASES — YOUR PRIMARY ANSWER SOURCE ===\n"
            "These are real tickets resolved by Qualesce Worksoft support engineers.\n"
            "RULES FOR USING THIS DATA:\n"
            "  • If resolution steps exist here → reproduce them exactly as the basis of your answer.\n"
            "  • Do NOT skip, reorder, or contradict any step that appears in these cases.\n"
            "  • You may add a one-line 'why' to help the user understand a step, but keep the steps intact.\n\n"
            + knowledge_text
            + "\n=== END OF SALESFORCE DATA ===\n"
        )
    elif sf_resolution:
        sf_section = (
            "\n\n=== SALESFORCE RESOLVED CASES (retrieved earlier in this conversation) ===\n"
            "Apply the resolution steps below if they remain relevant to the current message.\n\n"
            + sf_resolution[:4000]
            + "\n=== END OF SALESFORCE DATA ===\n"
        )

    user_turn = sum(1 for m in history if m.get("role") == "user")

    if user_turn <= 1:
        conversation_style = """
=== PHASE 1 — UNDERSTAND THE PROBLEM ===
This is the user's FIRST message. Your ONLY job right now is to understand their situation.

YOUR RESPONSE MUST:
1. Open with a warm, empathetic 1-sentence acknowledgment of what they described.
2. Then ask 2-3 specific, focused questions as a short bullet list. Choose from:
   • Which exact Worksoft product? (CTM / Certify / Portal / Capture) — SKIP if already stated.
   • What's the exact error message or what unexpected thing is happening on screen?
   • When did this start — was anything changed or updated recently?
   • What have you already tried to fix it?
   • Which environment — Production, UAT, or Dev?
   • Are other users affected or just you?
3. End with something like "Once I know these details, I can help you sort this out fast!"
DO NOT give any troubleshooting steps yet.
"""
    elif user_turn == 2:
        conversation_style = """
=== PHASE 2 — BEGIN SOLVING ===
You now know more about the user's problem. Start solving.

YOUR RESPONSE:
1. Briefly acknowledge their additional details (1 sentence).
2. Name the most likely root cause in plain English.
3. Give 3-5 clear numbered steps with short explanations.
4. Ask the user to try them and report back.

Keep it warm, like a knowledgeable colleague.
"""
    else:
        conversation_style = """
=== PHASE 3 — DEEP TROUBLESHOOTING ===
Use ALL conversation history to understand the full context.

HOW TO CONSTRUCT YOUR ANSWER:
1. SALESFORCE DATA FIRST — If the Salesforce case data above matches the user's issue,
   build your entire answer around those exact resolution steps. Quote them faithfully.
2. FILL GAPS — If the case is partially relevant, use your Worksoft expertise to fill what's missing.
3. NO CASE MATCH — Answer confidently from your deep Worksoft domain knowledge.

ANSWER FORMAT:
- Acknowledge what the user just said in 1 sentence.
- 1 sentence naming the likely root cause.
- Numbered steps: "1. **Do X** — (plain-English reason)"
- Close with something warm.

HARD RULES:
- NEVER mention Salesforce, case IDs, or any internal system name.
- NEVER give a cold, robotic or generic answer — always sound like a caring colleague.
"""

    system_prompt = (
        f"{_EXPERT_PERSONA}\n\n"
        f"{_WORKSOFT_DOMAIN}\n"
        + sf_section
        + "\n"
        + conversation_style
    )

    no_keys = not any([GROQ_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY])
    if no_keys:
        yield (
            "No AI API key is configured. Please add at least one of the following to your .env file:\n\n"
            "GROQ_API_KEY    — Free at console.groq.com\n"
            "GEMINI_API_KEY  — Free at aistudio.google.com\n"
            "ANTHROPIC_API_KEY or OPENAI_API_KEY"
        )
        return

    gen = _ask_ai(
        system_prompt=system_prompt,
        user_prompt=query,
        history=history,
        max_tokens=1400,
        stream=True,
        temperature=0.15,
    )

    if isinstance(gen, str):
        yield gen
        return

    full = ""
    for chunk in gen:
        full += chunk
        yield chunk

    updated_history = history + [{"role":"user","content":text}, {"role":"assistant","content":full}]
    resolved = _ai_detects_resolution(updated_history)
    user_msg_count = sum(1 for m in updated_history if m.get("role") == "user")
    stuck = False
    if not resolved and user_msg_count >= 4:
        stuck = _ai_feels_stuck(updated_history)

    meta = {"_meta": True, "resolved": resolved, "stuck": stuck,
            "sf_resolution": knowledge_text[:6000] if has_content else sf_resolution}
    yield f"\n\n__META__{json.dumps(meta)}__END_META__"

# ═══════════════════════════════════════════════════════════
# FLASK ROUTES — static files
# ═══════════════════════════════════════════════════════════
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/chat")
def chat_page():
    return send_from_directory("static", "chat.html")

@app.route("/style.css")
def css():
    return send_from_directory("static", "style.css")

@app.route("/chat.js")
def chat_js():
    return send_from_directory("static", "chat.js")

# ═══════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════
@app.route("/api/session", methods=["POST"])
def api_session():
    data    = request.json or {}
    name    = (data.get("name","")    or "").strip()
    email   = (data.get("email","")   or "").strip()
    company = (data.get("company","") or "").strip()

    if not name or not email or not company:
        return jsonify({"error": "Missing fields"}), 400

    sid = create_session(name, email)
    session["sid"]          = sid
    session["name"]         = name
    session["email"]        = email
    session["company"]      = company
    session["history"]      = []
    session["sf_resolution"]= ""
    session["turn_count"]   = 0
    session["show_popup"]   = False
    session["popup_reason"] = "resolved"

    first   = name.split()[0]
    welcome = (
        f"Hello {first}! I'm your AI Support Assistant for Worksoft. "
        f"How can I help you today? You can describe any issue with CTM, Certify, Portal, or Capture — "
        f"or ask anything else about your Worksoft environment."
    )
    save_message(sid, "assistant", welcome)
    session["history"] = [{"role":"assistant","content":welcome}]

    return jsonify({"session_id": sid, "welcome": welcome})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    text      = ""
    file_data = None

    if request.content_type and "multipart" in request.content_type:
        text = (request.form.get("message","") or "").strip()
        f    = request.files.get("file")
        if f and f.filename:
            file_data = _process_upload(f)
    else:
        data = request.json or {}
        text = (data.get("message","") or "").strip()

    if not text and not file_data:
        return jsonify({"error":"Empty message"}), 400

    sid     = session.get("sid")
    history = session.get("history", [])
    sf_res  = session.get("sf_resolution","")

    if sid:
        save_message(sid, "user", text or f"[Uploaded file: {(file_data or {}).get('name','')}]")

    history_for_ai = [m for m in history if m.get("role") in ("user","assistant")]

    def generate():
        full_reply        = ""
        sf_resolution_new = sf_res
        popup_trigger     = None

        for chunk in process_chat_stream(text, history_for_ai, sf_res, file_data):
            if "__META__" in chunk:
                meta_raw = re.search(r"__META__(.+?)__END_META__", chunk, re.DOTALL)
                if meta_raw:
                    try:
                        meta = json.loads(meta_raw.group(1))
                        sf_resolution_new = meta.get("sf_resolution", sf_res)
                        if meta.get("resolved"):
                            popup_trigger = "resolved"
                        elif meta.get("stuck"):
                            popup_trigger = "stuck"
                    except Exception:
                        pass
                pre = chunk.split("__META__")[0]
                if pre.strip():
                    full_reply += pre
                    yield f"data: {json.dumps({'text': pre})}\n\n"
            else:
                full_reply += chunk
                yield f"data: {json.dumps({'text': chunk})}\n\n"

        if full_reply and sid:
            save_message(sid, "assistant", full_reply)

        session["history"] = history_for_ai + [
            {"role":"user",      "content": text},
            {"role":"assistant", "content": full_reply},
        ]
        session["sf_resolution"] = sf_resolution_new
        session["turn_count"]    = session.get("turn_count", 0) + 1

        if popup_trigger:
            session["show_popup"]   = True
            session["popup_reason"] = popup_trigger

        yield f"data: {json.dumps({'done': True, 'show_popup': bool(popup_trigger), 'popup_reason': popup_trigger or ''})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.route("/api/escalate", methods=["POST"])
def api_escalate():
    data       = request.json or {}
    user_name  = (data.get("name","")  or session.get("name","Guest")).strip()
    user_email = (data.get("email","") or session.get("email","")).strip()
    priority   = data.get("priority","High")
    extra      = (data.get("extra","") or "").strip()

    history    = session.get("history",[])
    issue_text = next((m["content"] for m in history if m.get("role") == "user"), "")
    convo      = "\n\n".join(
        f"{'User' if m['role']=='user' else 'Agent'}: {m['content']}" for m in history
    )
    desc = (
        f"Reported by: {user_name}" + (f" ({user_email})" if user_email else "")
        + f"\n\nIssue:\n{issue_text}"
        + (f"\n\nAdditional Details:\n{extra}" if extra else "")
        + f"\n\nAI Chat:\n{convo}"
    )
    subj = f"Worksoft Issue – {issue_text[:80]}{'…' if len(issue_text)>80 else ''}"

    ticket = None; sf_error = ""; email_ok = False

    if all([SF_CLIENT_ID, SF_CLIENT_SECRET, SF_USERNAME, SF_PASSWORD]):
        try:
            ticket = sf_create_case(subj, desc, user_name, user_email, priority)
        except Exception as exc:
            sf_error = str(exc)
            ref = "QWS-" + hashlib.md5(f"{user_email}{time.time()}".encode()).hexdigest()[:6].upper()
            ticket = {"id": ref, "case_number": ref, "url": "#sf-not-configured"}
    else:
        ref = "QWS-" + hashlib.md5(f"{user_email}{time.time()}".encode()).hexdigest()[:6].upper()
        ticket = {"id": ref, "case_number": ref, "url": "#sf-not-configured"}
        sf_error = "Salesforce not configured"

    email_ok, email_err = send_escalation_email(ticket, user_name, user_email, issue_text, convo)
    ticket.update({
        "sf_error": sf_error, "email_ok": email_ok, "email_err": email_err,
        "priority": priority, "created_at": datetime.now().strftime("%d %b %Y, %H:%M")
    })

    sid = session.get("sid")
    if sid:
        save_ticket(
            session_id=sid, user_name=user_name, user_email=user_email,
            issue_text=issue_text, priority=priority,
            sf_case_number=ticket.get("case_number",""),
            sf_case_url=ticket.get("url",""),
            sf_error=sf_error, email_sent=email_ok,
        )
        update_session_status(sid, "escalated")

    return jsonify({
        "ticket": ticket, "email_ok": email_ok,
        "email_err": email_err if not email_ok else "",
        "it_admin": IT_ADMIN_EMAIL,
    })

@app.route("/api/resolve", methods=["POST"])
def api_resolve():
    sid = session.get("sid")
    if sid:
        update_session_status(sid, "resolved")
    return jsonify({"ok": True})

@app.route("/api/sync", methods=["POST"])
def api_sync():
    ok, msg = sync_sf_knowledge()
    return jsonify({"ok": ok, "message": msg})

@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({
        "ai_provider": AI_PROVIDER,
        "groq":        bool(GROQ_API_KEY),
        "gemini":      bool(GEMINI_API_KEY),
        "claude":      bool(ANTHROPIC_API_KEY),
        "openai":      bool(OPENAI_API_KEY),
        "sf":          bool(SF_CLIENT_ID and SF_USERNAME),
        "email":       bool(OUTLOOK_EMAIL),
        "db_cases":    _get_db().execute("SELECT COUNT(*) FROM sf_cases").fetchone()[0],
    })

# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG","0") == "1"
    log.info(f"Starting Worksoft Support on http://localhost:{port}  [AI provider: {AI_PROVIDER}]")
    app.run(host="0.0.0.0", port=port, debug=debug)