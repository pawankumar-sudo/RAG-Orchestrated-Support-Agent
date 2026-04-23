# IT Help — Slack Bot

A Flask-based Slack bot that acts as a senior L2 IT support engineer. When an
employee `@mentions` the bot in a channel (or replies in a live thread), it
searches the internal Confluence knowledge base, summarizes the best article
with Google Gemini, chats back-and-forth in the same thread, and — if the issue
is still unresolved — auto-creates a Jira Service Desk ticket or escalates to a
private IT channel.

---

## Features

- **@mention-triggered help in channel threads** — no DM needed; the whole
  conversation stays in a public thread.
- **Smart Confluence KB retrieval**
  - Gemini-assisted keyword extraction with typo correction
    (`twingat` → `twingate`)
  - Bigram phrase extraction for title matching (`apple id`, `mac laptop`)
  - Two-tier CQL: strict AND → OR fallback
  - `HIGH_SIGNAL_TERMS` / `GENERIC_KB_TERMS` / `TITLE_STRICT_PRODUCTS` guards
  - Composite scoring (relevance + title-alignment + how-to bonus + product
    name-in-title boost − hub/overview penalty)
  - Gemini disambiguator picks the single best article from the top candidates
- **KB summarization** — rewrites the Confluence article as a Slack-friendly
  numbered runbook.
- **Multi-turn thread chat** — follow-up replies in the thread (or the main
  channel box, when routed back) keep context.
- **AI fallback with KB grounding** — if the KB signal is weak, Gemini answers
  using any KB excerpts that clear a minimum relevance score.
- **Security policy enforcement**
  - Admin-only steps (registry edits, PowerShell, `sudo`, BIOS, GPO, disabling
    AV/firewall, etc.) are redacted and replaced with a ticket placeholder.
  - Citation tokens (`KB[1]`, `[4]`, `(source: 2)`) are stripped from model
    output.
- **Live spinner** — rotating Braille frames update a Slack message while the
  bot is working.
- **Interactive Block Kit buttons**
  - *Need More Help* — AI discovery flow
  - *Show Another Article* — next-ranked KB page
  - *Helpful* / *Not Helpful*
  - *Create Jira Ticket* / *Get Help Now (Urgent)*
- **Jira Service Desk ticket creation** with full conversation transcript as
  the description.
- **Urgent escalation** — cross-posts the thread, transcript, and ticket key to
  a private IT escalation channel.
- **Feedback detection** — phrases like "wrong article", "doesn't help",
  "isn't what I need" trigger a tailored apology + clarifier from the bot.
- **Keywords in thread** — typing `ticket` logs a Jira ticket, `done` ends the
  session cleanly.

---

## Architecture

```
User @mentions IT Help in #channel
      │
      ▼
verify_slack()  — HMAC-SHA256 on timestamp + body (reject > 5 min drift)
      │
      ▼
_handle_app_mention()  — strip <@…>, detect trivial greeting
      │
      ├─► greeting? → ask for the issue, bind thread (awaiting_query = True)
      │
      ▼
extract_search_keywords()  — Gemini @ temp 0.1
      │
      ▼
search_confluence()  — 2-tier CQL + composite scoring + AI pick
      │
      ├─► KB hit  → summarize_kb() (Gemini @ 0.25) → Block Kit card + CTA
      │                                      │
      │                                      ▼
      │                             user replies in thread
      │                                      │
      │                                      ▼
      │                             ask_ai_with_history()
      │                             (Gemini @ 0.35, KB-grounded)
      │
      └─► no KB → start_ai_thread() → ask_ai_with_history() (no-KB prompt)
                                                    │
                                                    ▼
                                   Satisfaction buttons
                                                    │
                              ┌─────────────────────┴─────────────────────┐
                              ▼                                           ▼
                         close thread                         Create Ticket / Urgent
                                                                  │
                                                                  ▼
                                                  create_jira_ticket() + escalation post
```

---

## Project layout

| File | Purpose |
|---|---|
| `local.py` | Local dev entry point. Thin Flask app with two routes: `POST /slack/interactive` and `POST /slack/events`. Runs on port `3000`. |
| `index.py` | All the logic: Slack handlers, Confluence search, Gemini prompts, Jira ticket creation, escalation, caching, security redaction. ~2,150 lines. |

---

## Tech stack

| Layer | Tech |
|---|---|
| Web framework | Flask |
| AI | Google Gemini (`gemini-flash-latest` via REST `generativelanguage.googleapis.com/v1beta`) |
| Knowledge base | Atlassian Confluence (CQL) |
| Ticketing | Atlassian Jira Service Desk |
| Messaging | Slack Web API + Block Kit + Interactive Components |
| HTML parsing | BeautifulSoup4 |
| HTTP | requests |
| Security | HMAC-SHA256 Slack signature verification |
| Concurrency | `threading` (async handlers, spinner, cache locks) |
| Caching | In-memory TTL cache for KB summaries |

---

## Gemini configuration

| Task | Temperature | Max output tokens | Retries |
|---|---|---|---|
| Keyword extraction | 0.1 | 256 | 0 |
| KB article disambiguation | 0.1 | 32 | 1 |
| KB → Slack runbook summary | 0.25 | 2800 | 2 |
| Conversational IT engineer reply | 0.35 | 1800 | 2 |

Low temperatures for deterministic retrieval, 0.35 for chat so replies feel
natural without drifting. Built-in retry/backoff handles 429, 502, 503, 504
and read timeouts.

---

## Environment variables

### Required

| Name | Description |
|---|---|
| `SLACK_SIGNING_SECRET` | For `X-Slack-Signature` HMAC verification |
| `SLACK_BOT_TOKEN` | Bot token (`xoxb-…`) |
| `JIRA_EMAIL` | Atlassian account email |
| `ATLASSIAN_API_TOKEN` | Atlassian API token |
| `GEMINI_API_KEY` | Google AI Studio key |

### Optional

| Name | Default | Description |
|---|---|---|
| `SLACK_BOT_USER_ID` | (auto-resolved via `auth.test`) | Bot user ID for mention detection |
| `URGENT_ESCALATION_CHANNEL` | `C0ARXM5MTUM` | Slack channel ID for urgent escalations |
| `KB_SUMMARY_CACHE_TTL_SEC` | `3600` | KB summary cache TTL |
| `KB_SUMMARY_CACHE_MAX_ENTRIES` | `500` | Cache size cap |
| `KB_AI_ARTICLE_PICK` | `true` | Enable Gemini disambiguation among top KB candidates |
| `KB_AI_PICK_MAX_CANDIDATES` | `12` | Max candidates sent to the picker |
| `AI_KB_GROUNDING` | `true` | Enable KB excerpts in AI chat prompts |
| `AI_GROUNDING_MAX_PAGES` | `5` | Pages included as grounding excerpts |
| `AI_GROUNDING_PER_PAGE_CHARS` | `3500` | Per-page char budget |
| `AI_GROUNDING_TOTAL_CHARS` | `18000` | Total grounding char budget |
| `AI_GROUNDING_MIN_TOP_SCORE` | `22` | Minimum top-page score before grounding kicks in |

---

## Confluence / Jira setup

The code assumes the following values (edit in `index.py` if yours differ):

```
JIRA_BASE        = https://svavacapital.atlassian.net
CONFLUENCE_BASE  = https://svavacapital.atlassian.net/wiki
SPACE_KEY        = IS
SERVICE_DESK_ID  = 4
REQUEST_TYPE_ID  = 69
CUSTOMER_NAME_FIELD = customfield_10978
```

---

## Local development

### 1. Install dependencies

```bash
cd "slack application"
python3 -m venv venv
source venv/bin/activate
pip install flask requests beautifulsoup4
```

### 2. Export env vars

```bash
export SLACK_SIGNING_SECRET=...
export SLACK_BOT_TOKEN=xoxb-...
export JIRA_EMAIL=you@company.com
export ATLASSIAN_API_TOKEN=...
export GEMINI_API_KEY=...
export URGENT_ESCALATION_CHANNEL=C0ARXM5MTUM
```

### 3. Run the Flask app

```bash
python local.py
```

The server listens on `http://localhost:3000`.

### 4. Expose to Slack

Use `ngrok` (or any HTTPS tunnel) so Slack can reach your local server:

```bash
ngrok http 3000
```

Point the Slack app Request URLs at:

- Event Subscriptions: `https://<ngrok-host>/slack/events`
- Interactivity & Shortcuts: `https://<ngrok-host>/slack/interactive`

---

## Slack app configuration

### OAuth scopes (bot)

- `app_mentions:read`
- `channels:history`
- `groups:history`
- `chat:write`
- `users:read`
- `im:history` (only if you also want DM support)

### Event subscriptions

- `app_mention`
- `message.channels`
- `message.groups`

### Interactivity

- Enable interactivity and set the request URL to `/slack/interactive`.

---

## Security notes

- All inbound Slack requests are verified with HMAC-SHA256 over
  `v0:{timestamp}:{body}`; requests older than 5 minutes are rejected.
- The model output runs through `enforce_security_policy()` before it ever
  hits Slack: admin-only procedures are redacted, citation tokens are stripped.
- The bot never suggests disabling antivirus, firewall, MDM, or VPN
  enforcement.
- Secrets are loaded from environment variables only — never committed.
  `.env` files are in `.gitignore`.

---

## Caching & performance

- KB summaries are cached by
  `sha256(title + normalized_query + sha256(body))` for 1 hour
  (configurable), with soft LRU-style eviction after 500 entries.
- Slack user names are cached in-process to avoid repeated `users.info` calls.
- Every Slack event handler spawns a daemon thread so the HTTP handler can
  return `200 OK` within Slack's 3-second ACK deadline.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Forbidden` on every request | Wrong `SLACK_SIGNING_SECRET`, or clock skew > 5 min |
| No KB match, always falls to AI | CQL returning nothing — check `SPACE_KEY` and that the Atlassian token has access |
| `429` / quota errors | Gemini rate limit — the retry backoff will handle transient bursts; upgrade the key tier for sustained load |
| Button clicks do nothing | Interactivity request URL not set in Slack app settings |
| Ticket creation 403/400 | `SERVICE_DESK_ID`, `REQUEST_TYPE_ID`, or `CUSTOMER_NAME_FIELD` doesn't match your Jira project |
