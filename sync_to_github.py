"""
sync_to_github.py
─────────────────
Pulls resolved cases from Salesforce into worksoft_support.db,
then commits and pushes the updated database to GitHub.

Usage (local):
    python sync_to_github.py

Automated (GitHub Actions):
    See .github/workflows/sync_db.yml — runs every 6 hours automatically.
"""

import os, sys, subprocess, logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── Load .env (local dev) ──────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ── Import SF sync logic from app.py ──────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import sync_sf_knowledge, DB_PATH

# ── Git helper ─────────────────────────────────────────────────────────────────
def _git(cmd, check=True):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and result.returncode != 0:
        log.error("git command failed: %s\n%s", cmd, result.stderr.strip())
        sys.exit(1)
    return result.stdout.strip(), result.returncode

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    log.info("═══ Worksoft SF → GitHub sync started ═══")

    # Step 1: Sync Salesforce cases into the SQLite DB
    log.info("Connecting to Salesforce and pulling cases…")
    ok, msg = sync_sf_knowledge()

    if ok:
        log.info("Salesforce sync: %s", msg)
    else:
        log.error("Salesforce sync failed: %s", msg)
        sys.exit(1)

    # Step 2: Stage the updated database file
    db_file = DB_PATH or "worksoft_support.db"
    if not os.path.exists(db_file):
        log.error("Database file not found: %s", db_file)
        sys.exit(1)

    _git(f'git add "{db_file}"')

    # Step 3: Check if anything actually changed
    staged, _ = _git("git diff --cached --name-only", check=False)
    if not staged.strip():
        log.info("No changes detected in database — nothing to push.")
        return

    # Step 4: Commit
    timestamp  = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    commit_msg = f"chore: sync SF knowledge base [{timestamp}]"
    _git(f'git commit -m "{commit_msg}"')
    log.info("Committed: %s", commit_msg)

    # Step 5: Push
    _git("git push")
    log.info("Database pushed to GitHub successfully.")
    log.info("═══ Sync complete ═══")


if __name__ == "__main__":
    main()
