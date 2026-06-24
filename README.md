# Worksoft AI Support Assistant

A Flask web application that integrates an AI-powered support chat with Salesforce case management.

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env with your API keys
```

**Minimum required:** At least one AI key (`GROQ_API_KEY` recommended — free at [console.groq.com](https://console.groq.com)).

All other keys (Salesforce, email) are optional — the app works without them.

### 3. Run
```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

---

## Features

| Feature | Requires |
|---------|----------|
| AI chat with Worksoft expertise | GROQ_API_KEY (or ANTHROPIC/OPENAI) |
| Image/file analysis | GROQ_API_KEY |
| Salesforce case knowledge sync | SF_* credentials |
| L2 escalation (Salesforce case creation) | SF_* credentials |
| Email notifications on escalation | OUTLOOK_EMAIL + OUTLOOK_PASSWORD |
| Conversation history (SQLite) | *(built-in, no config needed)* |

---

## File Structure

```
worksoft_support/
├── app.py              ← Flask backend (AI, Salesforce, email, DB)
├── requirements.txt
├── .env.example        ← Copy to .env and fill in your keys
├── support.db          ← SQLite DB (auto-created on first run)
└── static/
    ├── index.html      ← Onboarding page
    ├── chat.html       ← Chat interface
    ├── chat.js         ← Chat logic (streaming, modals, file upload)
    └── style.css       ← All styles
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Onboarding page |
| GET | `/chat` | Chat page |
| POST | `/api/session` | Start session (onboarding submit) |
| POST | `/api/chat` | Send message, get streaming AI reply |
| POST | `/api/escalate` | Create SF case + send email |
| POST | `/api/resolve` | Mark session resolved at L1 |
| POST | `/api/sync` | Sync Salesforce knowledge base |
| GET | `/api/status` | Health check |

---

## Chat Flow

1. User fills onboarding form → `/api/session` creates session
2. Welcome message displayed, user chats freely
3. Every message streams through Groq/Claude/OpenAI with Salesforce case context
4. After 3+ exchanges, AI checks if issue is resolved or conversation is stuck
5. If resolved → L1 resolution popup → `✅ Resolved` or `🔺 Forward to L2`
6. If stuck → L2 popup with same options
7. L2 escalation → form → Salesforce case created → email to IT Admin

---

## Salesforce Knowledge Sync

Click **🔄 Sync Salesforce Knowledge Base** on the onboarding page to pull all Salesforce cases into the local SQLite database. The AI uses these as its primary knowledge source.

You can also hit `POST /api/sync` programmatically or schedule it as a cron job.
