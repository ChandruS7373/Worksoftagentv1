"""
github_sync.py — GitHub DB Sync for Flask / Render deployment
==============================================================
• Downloads worksoft_support.db from GitHub on first startup so the
  app always has the latest knowledge base (even after Render restarts).
• Background thread watches the DB every 30 s and pushes any change
  to GitHub via the REST API — no git binary required.
• push_now() triggers an immediate push after a SF sync.

Config (via .env or Render environment variables):
    GH_TOKEN        — Personal access token (Contents: read+write)
                      NOTE: use GH_TOKEN not GITHUB_TOKEN — Render reserves that name
    GITHUB_REPO     — "owner/repo-name"
    GITHUB_BRANCH   — branch name (default: main)
    GITHUB_DB_PATH  — path inside the repo  (default: worksoft_support.db)
"""

import os
import base64
import hashlib
import threading
import time
import logging

import requests as _req

log = logging.getLogger(__name__)

_DB_PATH           = None   # set by init()
_lock              = threading.Lock()
_db_downloaded     = False
_sync_started      = False
_push_queue        = threading.Event()
_last_mtime        = 0.0


# ── Config ────────────────────────────────────────────────────────────────────

def _cfg():
    from dotenv import load_dotenv
    load_dotenv()
    token  = os.environ.get("GH_TOKEN",       "")
    repo   = os.environ.get("GITHUB_REPO",    "")
    branch = os.environ.get("GITHUB_BRANCH",  "main")
    path   = os.environ.get("GITHUB_DB_PATH", "worksoft_support.db")
    return token, repo, branch, path


def is_configured():
    token, repo, *_ = _cfg()
    return bool(token and repo)


# ── GitHub REST helpers ───────────────────────────────────────────────────────

def _headers():
    token, *_ = _cfg()
    return {
        "Authorization": f"token {token}",
        "Accept":        "application/vnd.github.v3+json",
    }


def _api(repo, branch, db_path):
    return f"https://api.github.com/repos/{repo}/contents/{db_path}?ref={branch}"


def remote_info():
    """Return {'sha': ..., 'size': ...} or {} if not found."""
    token, repo, branch, db_path = _cfg()
    if not token or not repo:
        return {}
    try:
        r = _req.get(_api(repo, branch, db_path), headers=_headers(), timeout=10)
        if r.status_code == 200:
            d = r.json()
            return {"sha": d.get("sha", ""), "size": d.get("size", 0)}
        return {}
    except Exception:
        return {}


# ── Download ──────────────────────────────────────────────────────────────────

def ensure_db_downloaded():
    """
    Download DB from GitHub if the local file is missing or smaller than the
    remote copy.  Called once at app startup before init_db().
    """
    global _db_downloaded
    if _db_downloaded:
        return

    if not is_configured():
        log.info("[github_sync] Not configured — skipping download.")
        _db_downloaded = True
        return

    token, repo, branch, db_path = _cfg()
    local = _DB_PATH

    try:
        r = _req.get(_api(repo, branch, db_path), headers=_headers(), timeout=20)
        if r.status_code != 200:
            log.warning("[github_sync] Remote DB not found (HTTP %s) — using local.", r.status_code)
            _db_downloaded = True
            return

        data     = r.json()
        content  = base64.b64decode(data["content"])
        remote_size = len(content)
        local_size  = os.path.getsize(local) if os.path.exists(local) else 0

        if local_size >= remote_size:
            log.info("[github_sync] Local DB (%d B) >= remote (%d B) — no download needed.",
                     local_size, remote_size)
            _db_downloaded = True
            return

        with _lock:
            with open(local, "wb") as f:
                f.write(content)
        log.info("[github_sync] Downloaded DB from GitHub (%d B).", remote_size)

    except Exception as e:
        log.warning("[github_sync] Download error: %s", e)

    _db_downloaded = True


# ── Upload ────────────────────────────────────────────────────────────────────

def _push_db():
    """Push the local DB to GitHub via the REST API."""
    if not is_configured():
        return

    token, repo, branch, db_path = _cfg()
    local = _DB_PATH

    if not os.path.exists(local):
        return

    try:
        with _lock:
            with open(local, "rb") as f:
                content = f.read()

        encoded = base64.b64encode(content).decode()

        # Need the current SHA to update the file
        r = _req.get(_api(repo, branch, db_path), headers=_headers(), timeout=10)
        sha = r.json().get("sha", "") if r.status_code == 200 else ""

        payload = {
            "message": f"chore: sync DB [{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}]",
            "content": encoded,
            "branch":  branch,
        }
        if sha:
            payload["sha"] = sha

        url = f"https://api.github.com/repos/{repo}/contents/{db_path}"
        r2  = _req.put(url, headers=_headers(), json=payload, timeout=30)

        if r2.status_code in (200, 201):
            log.info("[github_sync] DB pushed to GitHub (%d B).", len(content))
        else:
            log.warning("[github_sync] Push failed: HTTP %s — %s",
                        r2.status_code, r2.text[:200])

    except Exception as e:
        log.warning("[github_sync] Push error: %s", e)


# ── Background sync thread ────────────────────────────────────────────────────

def _sync_loop():
    global _last_mtime
    local = _DB_PATH
    while True:
        try:
            # Wait up to 30 s, or wake immediately if push_now() called
            triggered = _push_queue.wait(timeout=30)
            _push_queue.clear()

            if not os.path.exists(local):
                continue

            mtime = os.path.getmtime(local)
            if triggered or mtime != _last_mtime:
                _push_db()
                _last_mtime = os.path.getmtime(local)

        except Exception as e:
            log.warning("[github_sync] Sync loop error: %s", e)
            time.sleep(10)


def start_sync_thread():
    """Start the background push thread (idempotent — safe to call multiple times)."""
    global _sync_started
    if _sync_started or not is_configured():
        return
    _sync_started = True
    t = threading.Thread(target=_sync_loop, daemon=True, name="github-sync")
    t.start()
    log.info("[github_sync] Background sync thread started.")


def push_now():
    """Trigger an immediate push — call this after a Salesforce sync."""
    _push_queue.set()


# ── Init ──────────────────────────────────────────────────────────────────────

def init(db_path: str):
    """
    Call this once at app startup with the absolute path to the DB file.
    Example:
        import github_sync
        github_sync.init(app_db_path)
        github_sync.ensure_db_downloaded()
        init_db()
        github_sync.start_sync_thread()
    """
    global _DB_PATH
    _DB_PATH = db_path
