"""
app.py — Flask backend integrating Worksoft AI support logic
with the existing frontend (index.html / chat.html / style.css / chat.js).

IMPROVEMENTS APPLIED:
  1. _retrieve_best_cases: Stage-1 LLM call removed — pure FTS only (saves 1-2s per message)
  2. Resolution/stuck checks: run every 3rd turn only, not every message (saves 2 LLM calls)
  3. System prompt: _WORKSOFT_DOMAIN only injected when relevant (not on greetings)
  4. Junk case filtering: skip generator cases + auto-generated escalation cases during sync
  5. WAL checkpoint: runs on startup for faster DB reads
  6. Auto daily sync: background thread syncs Salesforce every 24 hours
  7. _ERROR_KB expanded: 6 new CTM entries from real cases (00001076-00001086)
  8. Auto-save resolved chats: when user resolves at L1, saves fix to SF knowledge base

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
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR  = os.path.join(BASE_DIR, "static")
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
SF_DOMAIN         = _secret("SF_DOMAIN", "login").strip().strip('"').strip("'")
SF_INSTANCE_URL   = _secret("SF_INSTANCE_URL", "")
IT_ADMIN_EMAIL    = _secret("IT_ADMIN_EMAIL", "it-admin@qualesce.com")
OUTLOOK_EMAIL     = _secret("OUTLOOK_EMAIL")
OUTLOOK_PASSWORD  = _secret("OUTLOOK_PASSWORD")
PORTAL_URL        = SF_INSTANCE_URL or f"https://{SF_DOMAIN}.salesforce.com"

# ── Slack ─────────────────────────────────────────────────────────────────────
SLACK_WEBHOOK_URL    = _secret("SLACK_WEBHOOK_URL")
SLACK_BOT_TOKEN      = _secret("SLACK_BOT_TOKEN")
SLACK_CHANNEL        = _secret("SLACK_CHANNEL", "C0B9SK7EW64")
SLACK_SIGNING_SECRET = _secret("SLACK_SIGNING_SECRET")

# ── Confluence ────────────────────────────────────────────────────────────────
CONFLUENCE_URL       = _secret("CONFLUENCE_URL",       "https://qualesce-team-kr41ncbl.atlassian.net")
CONFLUENCE_EMAIL     = _secret("CONFLUENCE_EMAIL",     "aravind.r@qualesce.com")
CONFLUENCE_API_TOKEN = _secret("CONFLUENCE_API_TOKEN")
CONFLUENCE_SPACE_KEY = _secret("CONFLUENCE_SPACE_KEY", "IS")

# ── AI model names — all overridable from .env ──────────────────────────────
_GROQ_MODEL        = _secret("GROQ_MODEL",        "llama-3.1-8b-instant")
_GROQ_FAST_MODEL   = _secret("GROQ_FAST_MODEL",   "llama-3.1-8b-instant")
_GROQ_VISION_MODEL = _secret("GROQ_VISION_MODEL", "llama-3.2-11b-vision-preview")
_OPENAI_MODEL      = _secret("OPENAI_MODEL",      "gpt-4o")
_OPENAI_FAST_MODEL = _secret("OPENAI_FAST_MODEL", "gpt-4o-mini")
_CLAUDE_MODEL      = _secret("CLAUDE_MODEL",      "claude-sonnet-4-6")
_CLAUDE_FAST_MODEL = _secret("CLAUDE_FAST_MODEL", "claude-haiku-4-5-20251001")
_GEMINI_MODEL      = _secret("GEMINI_MODEL",      "gemini-2.0-flash")
_GEMINI_FAST_MODEL = _secret("GEMINI_FAST_MODEL", "gemini-2.0-flash")

# ── AI provider priority (from .env, default: groq) ─────────────────────────
AI_PROVIDER = _secret("AI_PROVIDER", "groq").lower()

# ═══════════════════════════════════════════════════════════
# DATABASE  (SQLite)
# ═══════════════════════════════════════════════════════════
import sqlite3, threading

DB_PATH  = _secret("DB_PATH", os.path.join(BASE_DIR, "worksoft_support.db"))
_db_lock = threading.Lock()

def _get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    with _get_db() as conn:
        # FIX 5: Checkpoint WAL on startup for faster reads
        try:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception:
            pass

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
        CREATE TABLE IF NOT EXISTS confluence_pages (
            page_id    TEXT PRIMARY KEY,
            title      TEXT,
            content    TEXT,
            space_key  TEXT,
            page_url   TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS confluence_pages_fts
            USING fts5(page_id UNINDEXED, title, content);
        CREATE TABLE IF NOT EXISTS confluence_sync_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            synced_at   TEXT DEFAULT (datetime('now')),
            pages_count INTEGER DEFAULT 0
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

def get_chat_history(session_id, limit=30):
    """Load chat messages from DB for AI context — avoids 4KB cookie limit."""
    if not session_id:
        return []
    try:
        with _get_db() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages "
                "WHERE session_id=? ORDER BY created_at ASC LIMIT ?",
                (session_id, limit)
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        log.warning(f"get_chat_history failed: {e}")
        return []

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

# ═══════════════════════════════════════════════════════════
# CONFLUENCE — local cache + FTS search
# ═══════════════════════════════════════════════════════════
def search_confluence(query, top_n=3):
    """FTS search over locally-cached Confluence pages."""
    try:
        words = [w for w in re.split(r'\W+', query.lower()) if len(w) > 3]
        fts_q = " OR ".join(words[:6]) if words else query
        with _get_db() as conn:
            rows = conn.execute(
                "SELECT p.page_id, p.title, p.content, p.page_url "
                "FROM confluence_pages p "
                "WHERE p.page_id IN ("
                "  SELECT page_id FROM confluence_pages_fts WHERE confluence_pages_fts MATCH ?"
                ") LIMIT ?",
                (fts_q, top_n)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []

def upsert_confluence_page(page_id, title, content, space_key, page_url):
    with _get_db() as conn:
        conn.execute("""
        INSERT INTO confluence_pages (page_id, title, content, space_key, page_url)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(page_id) DO UPDATE SET
            title=excluded.title, content=excluded.content,
            space_key=excluded.space_key, page_url=excluded.page_url,
            updated_at=datetime('now')
        """, (page_id, title, content, space_key, page_url))
        conn.execute("DELETE FROM confluence_pages_fts WHERE page_id=?", (page_id,))
        conn.execute(
            "INSERT INTO confluence_pages_fts (page_id, title, content) VALUES (?, ?, ?)",
            (page_id, title or "", content or "")
        )

def sync_confluence_knowledge():
    """Fetch pages from Confluence REST API and store locally for FTS search."""
    if not all([CONFLUENCE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN]):
        return False, "Confluence credentials not configured (set CONFLUENCE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN)"

    import requests as _req
    base = CONFLUENCE_URL.rstrip("/")
    auth = (CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN)
    params = {"type": "page", "limit": 50, "expand": "body.storage,space"}
    if CONFLUENCE_SPACE_KEY:
        params["spaceKey"] = CONFLUENCE_SPACE_KEY

    pages = []
    url = f"{base}/rest/api/content"
    start = 0
    while True:
        params["start"] = start
        try:
            r = _req.get(url, auth=auth, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            return False, f"Confluence API error: {exc}"

        results = data.get("results", [])
        pages.extend(results)
        if not data.get("_links", {}).get("next") or start >= 500:
            break
        start += len(results)

    if not pages:
        return False, "No pages found in Confluence (check CONFLUENCE_SPACE_KEY)"

    synced = 0
    for page in pages:
        try:
            pid      = page["id"]
            title    = page.get("title", "")
            storage  = page.get("body", {}).get("storage", {}).get("value", "")
            content  = _strip_html(storage)[:3000]
            sk       = page.get("space", {}).get("key", CONFLUENCE_SPACE_KEY or "")
            page_url = f"{base}/wiki{page.get('_links', {}).get('webui', '')}"
            upsert_confluence_page(pid, title, content, sk, page_url)
            synced += 1
        except Exception:
            continue

    with _get_db() as conn:
        conn.execute("INSERT INTO confluence_sync_log (pages_count) VALUES (?)", (synced,))

    return True, f"Synced {synced} Confluence pages from {base}"

# ── GitHub sync: download DB before init, push after SF sync ──
import github_sync as _gh
_gh.init(DB_PATH)
_gh.ensure_db_downloaded()   # pulls latest DB from GitHub before creating tables

# Init DB on startup
init_db()

# Start background push thread (watches DB for changes every 30 s)
_gh.start_sync_thread()

# ═══════════════════════════════════════════════════════════
# FIX 6: AUTO DAILY SYNC — background thread
# ═══════════════════════════════════════════════════════════
def _auto_sync_loop():
    """Syncs Salesforce knowledge base once every 24 hours, then pushes DB to GitHub."""
    time.sleep(60)  # wait 60s after startup before first sync
    while True:
        if all([SF_CLIENT_ID, SF_CLIENT_SECRET, SF_USERNAME, SF_PASSWORD]):
            log.info("Auto-sync: starting daily Salesforce knowledge sync...")
            try:
                ok, msg = sync_sf_knowledge()
                log.info(f"Auto-sync result: {msg}")
                if ok:
                    _gh.push_now()   # push updated DB to GitHub immediately
            except Exception as e:
                log.warning(f"Auto-sync failed: {e}")
        else:
            log.info("Auto-sync: skipped (Salesforce credentials not configured)")
        time.sleep(86400)  # 24 hours

_sync_thread = threading.Thread(target=_auto_sync_loop, daemon=True)
_sync_thread.start()

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
# SALESFORCE — direct REST client (no simple-salesforce dep)
# ═══════════════════════════════════════════════════════════
class _SFClient:
    """Minimal Salesforce REST client using only `requests`."""
    _API = "v58.0"

    def __init__(self, access_token, instance_url):
        self._token    = access_token
        self._inst     = instance_url.rstrip("/")
        self.base_url  = f"{self._inst}/services/data/{self._API}/"

    def _hdr(self):
        return {"Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json"}

    def query_all(self, soql):
        import requests as _req
        url  = f"{self._inst}/services/data/{self._API}/query/"
        resp = _req.get(url, headers=self._hdr(), params={"q": soql}, timeout=30)
        resp.raise_for_status()
        data    = resp.json()
        records = list(data.get("records", []))
        while not data.get("done", True) and data.get("nextRecordsUrl"):
            resp = _req.get(self._inst + data["nextRecordsUrl"],
                            headers=self._hdr(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
            records.extend(data.get("records", []))
        return {"records": records}

    class _SObj:
        def __init__(self, client, sobject):
            self._c = client
            self._s = sobject

        def create(self, payload):
            import requests as _req
            url  = f"{self._c._inst}/services/data/{self._c._API}/sobjects/{self._s}/"
            resp = _req.post(url, headers=self._c._hdr(), json=payload, timeout=15)
            resp.raise_for_status()
            return resp.json()

        def get(self, record_id):
            import requests as _req
            url  = f"{self._c._inst}/services/data/{self._c._API}/sobjects/{self._s}/{record_id}"
            resp = _req.get(url, headers=self._c._hdr(), timeout=15)
            resp.raise_for_status()
            return resp.json()

    def __getattr__(self, name):
        return _SFClient._SObj(self, name)


def _sf_client():
    import requests as _req
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
    return _SFClient(t["access_token"], t["instance_url"])

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
# FIX 4: Filter junk cases — skip generator cases and
#         auto-generated escalation cases ("Worksoft Issue –")
# ═══════════════════════════════════════════════════════════

# Keywords that identify irrelevant / junk cases to skip
_JUNK_SUBJECTS = [
    "generator", "motor", "rotor", "structural failure", "power generation",
    "electrical circuit", "portable generator", "spare parts", "installation process",
]

def _is_junk_case(subject):
    """Return True if this case should be excluded from the knowledge base."""
    if not subject:
        return True  # skip cases with no subject
    subj_lower = subject.lower().strip()
    # Skip auto-generated escalation cases created by this app
    if subj_lower.startswith("worksoft issue –") or subj_lower.startswith("worksoft issue -"):
        return True
    # Skip off-topic generator/motor cases
    if any(kw in subj_lower for kw in _JUNK_SUBJECTS):
        return True
    return False

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

        synced = 0; skipped = 0; empty_content = 0; errors = []; active_ids = []
        for case in cases:
            cid         = case["Id"]
            case_number = case.get("CaseNumber", "")
            subject     = case.get("Subject",     "") or ""
            description = case.get("Description", "") or ""
            status      = case.get("Status",      "") or ""
            resolution  = str(case.get("Resolution__c") or case.get("Comments") or "")

            # FIX 4: Skip junk/irrelevant cases
            if _is_junk_case(subject):
                log.debug(f"Skipping junk case [{case_number}]: {subject}")
                skipped += 1
                continue

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

        msg = f"Synced {synced} useful cases from Salesforce."
        if skipped:
            msg += f" Skipped {skipped} junk/irrelevant case(s)."
        if empty_content:
            msg += f" {empty_content} case(s) have no content yet."
        if deleted:
            msg += f" Removed {deleted} stale case(s)."
        return True, msg
    except Exception as exc:
        return False, f"Sync failed: {exc}"

# ═══════════════════════════════════════════════════════════
# FIX 8: AUTO-SAVE RESOLVED CHATS AS KNOWLEDGE ENTRIES
# ═══════════════════════════════════════════════════════════
def _extract_resolution_summary(history):
    """Use fast AI call to extract the fix that worked from a resolved conversation."""
    if not history or len(history) < 4:
        return ""
    convo = "\n".join(
        f"{'User' if m.get('role')=='user' else 'Agent'}: {m.get('content','')[:400]}"
        for m in history[-12:]
        if m.get("content", "").strip()
    )
    summary = _ask_ai(
        system_prompt=(
            "You are summarizing a resolved Worksoft IT support conversation. "
            "Extract: (1) the exact issue/error, (2) the fix steps that worked. "
            "Be specific — include service names, file paths, commands. "
            "If unclear, return empty string. "
            "Format:\nIssue: ...\nFix: Step 1. ... Step 2. ..."
        ),
        user_prompt=f"Conversation:\n{convo}",
        max_tokens=300, fast=True,
    )
    return str(summary).strip()

def _save_resolved_chat_to_sf(session_data, history):
    """Save a resolved L1 chat as a Salesforce knowledge entry for future reference."""
    if not all([SF_CLIENT_ID, SF_CLIENT_SECRET, SF_USERNAME, SF_PASSWORD]):
        return  # SF not configured, skip silently
    try:
        resolution_text = _extract_resolution_summary(history)
        if not resolution_text or len(resolution_text) < 20:
            return  # not enough content to save

        user_name  = session_data.get("name", "Unknown")
        user_email = session_data.get("email", "")

        # Build a clean subject from first user message
        first_issue = next(
            (m["content"].strip()[:80] for m in history if m.get("role") == "user"),
            "Worksoft issue"
        )
        subject = f"[L1 Resolved] {first_issue}"

        description = (
            f"Auto-saved from L1 resolved chat.\n"
            f"Reported by: {user_name}"
            + (f" ({user_email})" if user_email else "")
            + f"\n\nResolution summary:\n{resolution_text}"
        )

        sf = _sf_client()
        sf.Case.create({
            "Subject": subject,
            "Description": description,
            "Status": "Closed",
            "Priority": "Low",
            "Origin": "Web",
        })
        log.info(f"Auto-saved resolved chat to Salesforce: {subject[:60]}")
    except Exception as e:
        log.warning(f"Could not auto-save resolved chat to SF: {e}")

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
            with smtplib.SMTP(host, port, timeout=15) as srv:
                srv.ehlo(); srv.starttls(); srv.login(sender, password)
                srv.sendmail(sender, recipients, msg.as_string())
            return True, ""
        except Exception as exc:
            last_err = str(exc)
    return False, last_err

def send_escalation_email(ticket, user_name, user_email, issue, conversation, slack_url=""):
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
        f'border-radius:10px;margin-top:4px;margin-right:10px;">View Case in Salesforce</a>'
        if case_url != "#sf-not-configured" else
        '<p style="color:#94a3b8;font-size:12px;">Salesforce link unavailable.</p>'
    )

    slack_btn = (
        f'<a href="{slack_url}" style="display:inline-block;background:linear-gradient(135deg,#4a154b,#7c3aed);'
        f'color:#fff;text-decoration:none;font-weight:700;font-size:13px;padding:10px 24px;'
        f'border-radius:10px;margin-top:4px;">View in Slack</a>'
        if slack_url else ""
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
  {slack_btn}
  <p style="color:#475569;font-size:11px;border-top:1px solid rgba(255,255,255,.08);
     padding-top:14px;margin:20px 0 0;">
    Automated escalation · Qualesce Worksoft Support Portal
  </p>
</div>"""

    return _send_smtp(sender, password, IT_ADMIN_EMAIL,
                      f"[Worksoft Support] New Escalation – Case {case_num}",
                      html, cc=user_email)

# ═══════════════════════════════════════════════════════════
# SLACK NOTIFICATIONS
# ═══════════════════════════════════════════════════════════
def send_slack_notification(ticket, user_name, user_email, issue_text, priority):
    """Send an escalation alert to Slack via Incoming Webhook or Bot API."""
    import requests as _req

    case_num = ticket.get("case_number", ticket.get("id", "N/A"))
    case_url = ticket.get("url", "#")
    priority_emoji = {
        "critical": ":red_circle:", "high": ":large_orange_circle:",
        "medium": ":large_yellow_circle:", "low": ":large_green_circle:",
    }.get(priority.lower(), ":large_orange_circle:")

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": ":rotating_light: New L2 Support Escalation"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Case:*\n`#{case_num}`"},
                {"type": "mrkdwn", "text": f"*Priority:*\n{priority_emoji} {priority}"},
                {"type": "mrkdwn", "text": f"*User:*\n{user_name}"},
                {"type": "mrkdwn", "text": f"*Email:*\n{user_email}"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Issue:*\n{issue_text[:500]}"}},
        {"type": "divider"},
    ]

    if case_url and case_url not in ("#", "#sf-not-configured"):
        blocks.append({
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "View in Salesforce"},
                "url": case_url,
                "style": "primary",
            }],
        })

    payload = {
        "text": f":rotating_light: New Worksoft escalation from {user_name} — Case #{case_num}",
        "blocks": blocks,
    }

    if SLACK_WEBHOOK_URL:
        try:
            r = _req.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
            if r.status_code == 200:
                return True, "Slack notified via webhook", ""
        except Exception as exc:
            log.warning(f"Slack webhook failed: {exc}")

    if SLACK_BOT_TOKEN and SLACK_CHANNEL:
        try:
            payload["channel"] = SLACK_CHANNEL
            r = _req.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                json=payload, timeout=10,
            )
            data = r.json()
            if data.get("ok"):
                ch  = data.get("channel", SLACK_CHANNEL)
                ts  = data.get("ts", "")
                # Fetch permalink; fall back to channel deep-link so popup always has a URL
                permalink = f"https://slack.com/app_redirect?channel={ch}"
                try:
                    pl = _req.get(
                        "https://slack.com/api/chat.getPermalink",
                        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                        params={"channel": ch, "message_ts": ts},
                        timeout=10,
                    )
                    permalink = pl.json().get("permalink", "") or permalink
                except Exception:
                    pass
                return True, f"Slack notified in {SLACK_CHANNEL}", permalink
            return False, data.get("error", "Unknown Slack API error"), \
                   f"https://slack.com/app_redirect?channel={SLACK_CHANNEL}"
        except Exception as exc:
            return False, str(exc), ""

    # No token — return a channel deep-link so the popup button still works
    ch = SLACK_CHANNEL or ""
    fallback = f"https://slack.com/app_redirect?channel={ch}" if ch else ""
    return False, "Slack bot token not configured", fallback

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
_EXPERT_PERSONA = """You are a Worksoft support specialist at Qualesce with deep expertise in CTM, Certify, Portal, Capture, and the underlying Windows/IIS infrastructure.

Your job: diagnose and resolve the user's issue as efficiently as possible.

HOW TO RESPOND:
- If the issue is unclear, ask the most important missing question before attempting a fix.
- If you have enough context, give a direct, specific answer based on the Salesforce case data above (if present) or your expertise.
- Always use **bold** for UI paths and service names, `code` for commands/file paths, and numbered steps for multi-step fixes.
- Keep responses focused — no filler, no generic disclaimers.
- End with a brief follow-up so the user knows what to do next or what to report back.
- When referencing a resolved case from the knowledge base, always cite the case number at the start of your answer, e.g. "Based on Case #00001234, ..." or "This matches Case #00001234 — here are the steps:"."""


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

_last_ai_error = [""]  # mutable so inner functions can write to it

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
        return _ask_groq(system_prompt, user_prompt, max_tokens, history, fast, stream, temperature)

# ── Groq ────────────────────────────────────────────────────
def _ask_groq(system_prompt, user_prompt, max_tokens=800, history=None,
              fast=False, stream=False, temperature=0.3, model_override=None):
    """Call Groq via direct HTTP (no SDK) to avoid pydantic/Python-version issues."""
    if not GROQ_API_KEY:
        return _ask_claude(system_prompt, user_prompt, max_tokens, history, fast, stream, temperature)
    effective_temp = 0.0 if fast else temperature
    model = model_override or (_GROQ_FAST_MODEL if fast else _GROQ_MODEL)

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        hist = history[-6:]  # keep last 3 turns (6 messages)
        for i, msg in enumerate(hist):
            role    = msg.get("role", "")
            content = msg.get("content", "")
            if i == len(hist) - 1 and role == "user" and content.strip() == user_prompt.strip():
                continue
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": str(content)[:500]})
    messages.append({"role": "user", "content": user_prompt})

    try:
        import requests as _r
        resp = _r.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       model,
                "messages":    messages,
                "max_tokens":  max_tokens,
                "temperature": effective_temp,
            },
            timeout=60,
        )
        if resp.status_code != 200:
            err_body = ""
            try: err_body = resp.json().get("error", {}).get("message", resp.text[:200])
            except Exception: err_body = resp.text[:200]
            log.error(f"Groq HTTP {resp.status_code}: {err_body}")
            _last_ai_error[0] = f"Groq {resp.status_code}: {err_body}"
            return _ask_claude(system_prompt, user_prompt, max_tokens, history, fast, stream, temperature)
        raw = resp.json()["choices"][0]["message"]["content"] or ""
        raw = raw.strip()
        if "<think>" in raw and "</think>" in raw:
            raw = raw.split("</think>", 1)[-1].strip()
        _last_ai_error[0] = ""
        return raw
    except Exception as e:
        log.error(f"Groq HTTP error: {e}")
        _last_ai_error[0] = str(e)
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
    if AI_PROVIDER == "gemini":
        return _ask_gemini(system_prompt, user_prompt, max_tokens, history, fast, stream, temperature)
    elif AI_PROVIDER == "claude":
        return _ask_claude(system_prompt, user_prompt, max_tokens, history, fast, stream, temperature)
    elif AI_PROVIDER == "openai":
        return _ask_openai(system_prompt, user_prompt, max_tokens, history, fast)
    else:
        # Default: groq
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
    if GROQ_API_KEY:
        try:
            import requests as _r
            resp = _r.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": _GROQ_VISION_MODEL, "messages": [{"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{file_data['mime']};base64,{file_data['base64']}"}},
                    {"type": "text", "text": (
                        "Describe the error or issue visible in this Worksoft screenshot in 2 short sentences. "
                        "Focus on error messages, codes, UI state, or warnings. "
                        f"User also says: {user_text or '(no description provided)'}"
                    )},
                ]}], "max_tokens": 200},
                timeout=30,
            )
            resp.raise_for_status()
            desc = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            if desc:
                return f"{user_text} — Screenshot shows: {desc}".strip()
        except Exception as e:
            log.warning(f"Groq vision failed, trying Gemini: {e}")

    if GEMINI_API_KEY:
        try:
            import google.generativeai as genai
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
# RETRIEVAL — FTS + AI re-ranking (ported from Streamlit app)
# ═══════════════════════════════════════════════════════════
def _retrieve_best_cases(query, top_n=2):
    """FTS to get candidates, then AI picks the best ones by actual content."""
    fts_query_variants = [query]
    words = [w for w in re.split(r'\W+', query.lower()) if len(w) > 3]
    if words:
        fts_query_variants.append(" OR ".join(words[:6]))

    candidate_ids = []
    for fts_q in fts_query_variants:
        try:
            hits = search_knowledge(fts_q, top_n=12)
            for m in hits:
                cid = m["sf_case_id"]
                if cid not in candidate_ids:
                    candidate_ids.append(cid)
        except Exception:
            pass

    if not candidate_ids:
        try:
            with _get_db() as conn:
                rows = conn.execute(
                    "SELECT sf_case_id FROM sf_cases "
                    "WHERE length(comments) > 0 OR length(description) > 0 "
                    "ORDER BY rowid DESC LIMIT 10"
                ).fetchall()
            candidate_ids = [r["sf_case_id"] for r in rows]
        except Exception:
            return []

    if not candidate_ids:
        return []

    candidates = get_cases_by_ids(candidate_ids[:15])
    if not candidates:
        return []
    if len(candidates) == 1:
        return candidates

    # AI re-ranking: judge by actual content, not just subject
    content_blocks = []
    for i, c in enumerate(candidates):
        subject  = (c.get("subject",     "") or "").strip()
        case_num = (c.get("case_number", "") or "").strip()
        comments = (c.get("comments",    "") or "").strip()[:800]
        desc     = (c.get("description", "") or "").strip()[:300]
        body     = comments or desc or "(no content)"
        content_blocks.append(f"[{i+1}] #{case_num} — {subject}\n{body}")

    s2_resp = _ask_ai(
        system_prompt=(
            "You are a Worksoft support case matcher. "
            f"Pick the best 1-{top_n} cases whose content genuinely answers the user's question. "
            "Prefer cases with the most detailed resolution steps. "
            "Return ONLY position numbers e.g. '2' or '1, 3'. If none match: NONE"
        ),
        user_prompt=(
            f"User question: {query}\n\nCases:\n\n"
            + "\n\n---\n\n".join(content_blocks)
        ),
        max_tokens=30,
        fast=False,
        temperature=0.0,
    )

    if s2_resp and re.sub(r"[^a-zA-Z]", "", s2_resp).upper() != "NONE":
        final_ids = []
        for token in re.findall(r"\d+", s2_resp):
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
    """Rich case content — fuller text since AI re-ranking ensures we only get the best cases."""
    blocks = []; all_subjects = []
    for i, case in enumerate(matches, 1):
        subject    = (case.get("subject","")     or "").strip()
        case_num   = (case.get("case_number","") or "").strip()
        desc       = (case.get("description","") or "").strip()
        resolution = (case.get("resolution","")  or "").strip()
        comments   = (case.get("comments","")    or "").strip()
        if subject: all_subjects.append(subject)
        parts = []
        if case_num: parts.append(f"Case Number: #{case_num}")
        if subject:    parts.append(f"Issue type: {subject}")
        if desc:       parts.append(f"Problem description:\n{desc[:1500]}")
        if resolution: parts.append(f"Resolution summary:\n{resolution[:1500]}")
        if comments:   parts.append(f"Detailed steps/comments:\n{comments[:3500]}")
        if len(parts) > 1:
            header = f"── Case #{case_num} ──" if case_num else f"── Knowledge Entry {i} ──"
            blocks.append(header + "\n" + "\n\n".join(parts))

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
# CHAT ENGINE — 3-phase system (ported from Streamlit app)
# Phase 1: gather context  Phase 2: begin solving  Phase 3: deep troubleshoot
# ═══════════════════════════════════════════════════════════
def process_chat_stream(text, history, sf_resolution="", file_data=None):

    # ── File/image input ───────────────────────────────────────
    if file_data and file_data["type"] == "image":
        query = _describe_image(file_data, text)
    elif file_data and file_data["type"] == "text":
        query = f"{text}\n\nAttached file content:\n{file_data['content'][:1200]}"
    else:
        query = text

    # ── Rich query: combine recent user messages for better retrieval ──
    prior_user_msgs = [m["content"] for m in history if m.get("role") == "user"]
    if len(prior_user_msgs) >= 2:
        _combined = " | ".join(prior_user_msgs[-4:])
        if text.strip().lower() not in _combined.lower():
            _combined = text + " | " + _combined
        rich_query = _combined[:800]
    else:
        rich_query = query

    # ── Salesforce retrieval ────────────────────────────────────
    try:
        matches = _retrieve_best_cases(rich_query, top_n=2) if rich_query.strip() else []
    except Exception as _e:
        log.warning(f"SF retrieval failed: {_e}")
        matches = []
    knowledge_text, has_content, ctx_summary = (
        _build_case_knowledge(matches, query) if matches else ("", False, "")
    )

    # Store SF data in session so it persists across turns
    if has_content:
        session["sf_resolution"] = knowledge_text[:4000]
    sf_res = session.get("sf_resolution", "") or sf_resolution

    # ── Confluence retrieval ────────────────────────────────────
    confluence_section = ""
    try:
        conf_hits = search_confluence(rich_query, top_n=3) if rich_query.strip() else []
        if conf_hits:
            conf_blocks = []
            for hit in conf_hits:
                title   = (hit.get("title", "") or "").strip()
                content = (hit.get("content", "") or "").strip()[:1200]
                url     = hit.get("page_url", "")
                if title or content:
                    entry = f"Title: {title}"
                    if url:
                        entry += f"\nURL: {url}"
                    if content:
                        entry += f"\nContent:\n{content}"
                    conf_blocks.append(entry)
            if conf_blocks:
                confluence_section = (
                    "\n\n=== CONFLUENCE KNOWLEDGE BASE ===\n"
                    "Use these documentation pages as supplementary context.\n\n"
                    + "\n\n---\n\n".join(conf_blocks)
                    + "\n=== END ===\n"
                )
    except Exception as _ce:
        log.warning(f"Confluence retrieval failed: {_ce}")

    # ── Build SF section ────────────────────────────────────────
    sf_section = ""
    if has_content:
        sf_section = (
            "\n\n=== RESOLVED CASES FROM KNOWLEDGE BASE ===\n"
            "Use the resolution steps below as your primary answer source.\n\n"
            + knowledge_text[:1500]
            + "\n=== END ===\n"
        )
    elif sf_res:
        sf_section = (
            "\n\n=== RESOLVED CASES FROM KNOWLEDGE BASE ===\n"
            "Apply these steps if still relevant to the current question.\n\n"
            + sf_res[:1000]
            + "\n=== END ===\n"
        )

    # ── 3-phase conversation style (matches Streamlit app) ──────
    user_turn = sum(1 for m in history if m.get("role") == "user")

    system_prompt = _EXPERT_PERSONA + sf_section + confluence_section

    # ── Call AI ─────────────────────────────────────────────────
    no_keys = not any([GROQ_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY])
    if no_keys:
        yield "No AI API key configured. Add GROQ_API_KEY to your Render environment."
        return

    try:
        full = _ask_ai(
            system_prompt=system_prompt,
            user_prompt=query,
            history=history,
            max_tokens=600,
            stream=False,
            temperature=0.2,
        )
        if not isinstance(full, str):
            full = "".join(full)
    except Exception as exc:
        import traceback
        log.error(f"AI call failed: {exc}\n{traceback.format_exc()}")
        full = ""

    if not full:
        err = _last_ai_error[0]
        if err and ("401" in err or "invalid_api_key" in err.lower()):
            hint = "Your `GROQ_API_KEY` is invalid or expired. Update it in **Render → Environment tab**."
        elif err and ("429" in err or "rate_limit" in err.lower()):
            hint = "Rate limit reached. Wait a moment and try again."
        elif err and ("timeout" in err.lower() or "connection" in err.lower()):
            hint = "Network timeout. Try again in a few seconds."
        elif err:
            hint = f"Error: `{err}`"
        else:
            hint = "Check that `GROQ_API_KEY` is set in **Render → Environment tab**."
        yield f"⚠️ **AI service unavailable.**\n\n{hint}"
        return

    yield full

    # ── Resolution / stuck detection ────────────────────────────
    updated_history = history + [{"role":"user","content":text}, {"role":"assistant","content":full}]
    user_msg_count  = sum(1 for m in updated_history if m.get("role") == "user")

    resolved = False
    stuck    = False
    if user_msg_count >= 2:
        resolved = _ai_detects_resolution(updated_history)
        if not resolved and user_msg_count >= 4:
            stuck = _ai_feels_stuck(updated_history)

    meta = {"_meta": True, "resolved": resolved, "stuck": stuck,
            "sf_resolution": knowledge_text[:4000] if has_content else sf_resolution}
    yield f"\n\n__META__{json.dumps(meta)}__END_META__"

# ═══════════════════════════════════════════════════════════
# FLASK ROUTES — static files
# ═══════════════════════════════════════════════════════════
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")

@app.route("/chat")
def chat_page():
    return send_from_directory(STATIC_DIR, "chat.html")

@app.route("/style.css")
def css():
    return send_from_directory(STATIC_DIR, "style.css")

@app.route("/chat.js")
def chat_js():
    return send_from_directory(STATIC_DIR, "chat.js")

# ═══════════════════════════════════════════════════════════
# DEBUG ROUTE  — remove after fixing
# ═══════════════════════════════════════════════════════════
@app.route("/api/test-ai")
def test_ai():
    key = GROQ_API_KEY
    if not key:
        return jsonify({"status": "FAIL", "error": "GROQ_API_KEY is empty or not set"})
    try:
        import requests as _r
        resp = _r.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant",
                  "messages": [{"role": "user", "content": "Say hello in one word."}],
                  "max_tokens": 10},
            timeout=30,
        )
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"] or ""
        return jsonify({"status": "OK", "reply": reply, "key_prefix": key[:8] + "..."})
    except Exception as e:
        return jsonify({"status": "FAIL", "error": str(e), "key_prefix": key[:8] + "..."})

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
    session["turn_count"]   = 0
    session["show_popup"]   = False
    session["popup_reason"] = "resolved"

    first   = name.split()[0]
    welcome = f"Hello {first}! I'm your Worksoft Support Assistant. How can I help you today?"
    save_message(sid, "assistant", welcome)

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
    history = get_chat_history(sid)
    sf_res  = ""

    if sid:
        save_message(sid, "user", text or f"[Uploaded file: {(file_data or {}).get('name','')}]")

    history_for_ai = [m for m in history if m.get("role") in ("user","assistant")]

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
        else:
            full_reply += chunk

    if not full_reply:
        full_reply = "⚠️ No response from AI. Please check your GROQ_API_KEY in Render's Environment tab."

    if sid:
        save_message(sid, "assistant", full_reply)

    session["turn_count"] = session.get("turn_count", 0) + 1

    if popup_trigger:
        session["show_popup"]   = True
        session["popup_reason"] = popup_trigger

    return jsonify({
        "reply":        full_reply,
        "show_popup":   bool(popup_trigger),
        "popup_reason": popup_trigger or "",
    })

@app.route("/api/escalate", methods=["POST"])
def api_escalate():
    """Create a Salesforce case and send escalation email. Always returns JSON."""
    try:
        data       = request.get_json(force=True, silent=True) or {}
        user_name  = (data.get("name","")  or session.get("name","") or "Guest").strip()
        user_email = (data.get("email","") or session.get("email","") or "").strip()
        priority   = (data.get("priority","") or "High").strip()
        extra      = (data.get("extra","")    or "").strip()

        history    = get_chat_history(session.get("sid"))
        issue_text = ""
        for m in history:
            if m.get("role") == "user" and m.get("content","").strip():
                issue_text = m["content"].strip()
                break
        if not issue_text:
            issue_text = "Worksoft issue (see conversation)"

        convo = "\n\n".join(
            f"{'User' if m.get('role')=='user' else 'Agent'}: {m.get('content','')}"
            for m in history if m.get("content","").strip()
        )

        desc = (
            f"Reported by: {user_name}"
            + (f" ({user_email})" if user_email else "")
            + f"\n\nIssue:\n{issue_text}"
            + (f"\n\nAdditional Details:\n{extra}" if extra else "")
            + (f"\n\nAI Chat Transcript:\n{convo}" if convo else "")
        )
        subj = f"Worksoft Issue – {issue_text[:80]}{'…' if len(issue_text) > 80 else ''}"

        ticket   = None
        sf_error = ""
        sf_configured = all([SF_CLIENT_ID, SF_CLIENT_SECRET, SF_USERNAME, SF_PASSWORD])

        if sf_configured:
            try:
                ticket = sf_create_case(subj, desc, user_name, user_email, priority)
                log.info(f"SF case created: {ticket.get('case_number')}")
            except Exception as exc:
                sf_error = str(exc)
                log.error(f"SF case creation failed: {sf_error}")

        if not ticket:
            ref    = "QWS-" + hashlib.md5(f"{user_email}{time.time()}".encode()).hexdigest()[:6].upper()
            ticket = {"id": ref, "case_number": ref, "url": "#sf-not-configured"}
            if not sf_configured:
                sf_error = "Salesforce credentials not configured in .env"

        ticket.update({
            "sf_error":   sf_error,
            "email_ok":   True,   # notifications sent async
            "email_err":  "",
            "priority":   priority,
            "created_at": datetime.now().strftime("%d %b %Y, %H:%M"),
        })

        sid = session.get("sid")
        if sid:
            try:
                save_ticket(
                    session_id=sid, user_name=user_name, user_email=user_email,
                    issue_text=issue_text, priority=priority,
                    sf_case_number=ticket.get("case_number", ""),
                    sf_case_url=ticket.get("url", ""),
                    sf_error=sf_error, email_sent=0,
                )
                update_session_status(sid, "escalated")
            except Exception as exc:
                log.error(f"DB save failed: {exc}")

        # ── Slack: sync (fast ~1s) so we get permalink for the popup ──
        slack_url = ""
        try:
            s_ok, s_msg, slack_url = send_slack_notification(
                ticket, user_name, user_email, issue_text, priority
            )
            log.info(f"Slack: {'sent' if s_ok else 'failed'} — {slack_url or s_msg}")
        except Exception as exc:
            log.error(f"Slack exception: {exc}")

        # ── Email: async so response is instant ──
        _ticket_snap = dict(ticket)
        _slack_snap  = slack_url
        _sid_snap    = sid
        def _email_async():
            try:
                email_ok, email_err = send_escalation_email(
                    _ticket_snap, user_name, user_email, issue_text, convo, _slack_snap
                )
                log.info(f"Async email: {'sent' if email_ok else 'failed — ' + email_err}")
                if email_ok and _sid_snap:
                    with _get_db() as conn:
                        conn.execute(
                            "UPDATE tickets SET email_sent=1 WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
                            (_sid_snap,)
                        )
            except Exception as exc:
                log.error(f"Async email exception: {exc}")

        threading.Thread(target=_email_async, daemon=True).start()

        return jsonify({
            "ticket":    ticket,
            "email_ok":  True,
            "email_err": "",
            "it_admin":  IT_ADMIN_EMAIL,
            "slack_ok":  bool(slack_url),
            "slack_err": "",
            "slack_url": slack_url,
        })

    except Exception as exc:
        import traceback
        log.error(f"api_escalate unhandled error: {exc}\n{traceback.format_exc()}")
        ref = "QWS-" + hashlib.md5(str(time.time()).encode()).hexdigest()[:6].upper()
        return jsonify({
            "ticket":    {"id": ref, "case_number": ref, "url": "#sf-not-configured",
                          "priority": "High", "created_at": datetime.now().strftime("%d %b %Y, %H:%M"),
                          "sf_error": str(exc), "email_ok": False, "email_err": ""},
            "email_ok":  False,
            "email_err": str(exc),
            "it_admin":  IT_ADMIN_EMAIL,
            "error":     str(exc),
        }), 200

@app.route("/api/resolve", methods=["POST"])
def api_resolve():
    sid = session.get("sid")
    if sid:
        update_session_status(sid, "resolved")
        # FIX 8: Auto-save resolved chats as SF knowledge entries
        history = get_chat_history(sid)
        session_data = {
            "name":  session.get("name", ""),
            "email": session.get("email", ""),
        }
        # Run in a background thread so it doesn't delay the response
        t = threading.Thread(
            target=_save_resolved_chat_to_sf,
            args=(session_data, history),
            daemon=True
        )
        t.start()
    return jsonify({"ok": True})

@app.route("/api/sync", methods=["POST"])
def api_sync():
    ok, msg = sync_sf_knowledge()
    if ok:
        _gh.push_now()   # push updated DB to GitHub after manual sync
    return jsonify({"ok": ok, "message": msg})

@app.route("/api/sync-confluence", methods=["POST"])
def api_sync_confluence():
    """Sync Confluence pages into the local FTS knowledge base."""
    try:
        ok, msg = sync_confluence_knowledge()
        if ok:
            _gh.push_now()
        return jsonify({"ok": ok, "message": msg})
    except Exception as exc:
        log.error(f"Confluence sync error: {exc}")
        return jsonify({"ok": False, "message": str(exc)}), 500

@app.route("/api/status", methods=["GET"])
def api_status():
    try:
        conf_count = _get_db().execute("SELECT COUNT(*) FROM confluence_pages").fetchone()[0]
    except Exception:
        conf_count = 0
    return jsonify({
        "ai_provider":       AI_PROVIDER,
        "groq":              bool(GROQ_API_KEY),
        "gemini":            bool(GEMINI_API_KEY),
        "claude":            bool(ANTHROPIC_API_KEY),
        "openai":            bool(OPENAI_API_KEY),
        "sf":                bool(SF_CLIENT_ID and SF_USERNAME),
        "email":             bool(OUTLOOK_EMAIL),
        "slack":             bool(SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN),
        "confluence":        bool(CONFLUENCE_URL and CONFLUENCE_API_TOKEN),
        "db_cases":          _get_db().execute("SELECT COUNT(*) FROM sf_cases").fetchone()[0],
        "confluence_pages":  conf_count,
    })

# ═══════════════════════════════════════════════════════════
# GLOBAL ERROR HANDLERS — /api/ routes always return JSON
# ═══════════════════════════════════════════════════════════
@app.errorhandler(400)
def err_400(e):
    if request.path.startswith('/api/'):
        return jsonify({"error": "Bad request", "detail": str(e)}), 400
    return str(e), 400

@app.errorhandler(404)
def err_404(e):
    if request.path.startswith('/api/'):
        return jsonify({"error": "Not found"}), 404
    return str(e), 404

@app.errorhandler(500)
def err_500(e):
    if request.path.startswith('/api/'):
        return jsonify({"error": "Server error", "detail": str(e)}), 500
    return str(e), 500

@app.errorhandler(Exception)
def err_unhandled(e):
    import traceback
    log.error(f"Unhandled: {e}\n{traceback.format_exc()}")
    if request.path.startswith('/api/'):
        return jsonify({"error": str(e)}), 500
    return str(e), 500

# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG","0") == "1"
    log.info(f"Starting Worksoft Support on http://localhost:{port}  [AI provider: {AI_PROVIDER}]")
    app.run(host="0.0.0.0", port=port, debug=debug)