import json
import os
import hmac
import hashlib
import time
import threading
import requests
from flask import Response, jsonify
from bs4 import BeautifulSoup
import re

# ---------- ENV ----------
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
ATLASSIAN_API_TOKEN = os.environ["ATLASSIAN_API_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]

JIRA_BASE = "https://svavacapital.atlassian.net"
CONFLUENCE_BASE = "https://svavacapital.atlassian.net/wiki"

SPACE_KEY = "IS"
SERVICE_DESK_ID = "4"
REQUEST_TYPE_ID = "69"
CUSTOMER_NAME_FIELD = "customfield_10978"

AUTH = (JIRA_EMAIL, ATLASSIAN_API_TOKEN)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
BOT_USER_ID = os.environ.get("SLACK_BOT_USER_ID")

# ---------- DM CONVERSATION STORE ----------
active_threads = {}
# When a user writes in the DM *main* pane (not in the thread), map channel → root thread_ts
# so `ticket`, follow-ups, etc. still hit the same session and history.
dm_channel_active_thread = {}

# ---------- KB SUMMARY CACHE ----------
SUMMARY_CACHE_TTL_SEC = int(os.environ.get("KB_SUMMARY_CACHE_TTL_SEC", "3600"))
SUMMARY_CACHE_MAX_ENTRIES = int(os.environ.get("KB_SUMMARY_CACHE_MAX_ENTRIES", "500"))
summary_cache = {}
summary_cache_lock = threading.Lock()


def _env_truthy(name, default="false"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# Optional: Gemini chooses which candidate article best matches the user query (first KB hit only).
KB_AI_ARTICLE_PICK_ENABLED = _env_truthy("KB_AI_ARTICLE_PICK", "true")
KB_AI_PICK_MAX_CANDIDATES = max(3, min(20, int(os.environ.get("KB_AI_PICK_MAX_CANDIDATES", "12"))))

# AI chat (DM / engineer) must only use Confluence excerpts when true — no generic “best guess” procedures.
AI_KB_GROUNDING_ENABLED = _env_truthy("AI_KB_GROUNDING", "true")
AI_GROUNDING_MAX_PAGES = max(1, min(12, int(os.environ.get("AI_GROUNDING_MAX_PAGES", "5"))))
AI_GROUNDING_PER_PAGE_CHARS = max(800, min(8000, int(os.environ.get("AI_GROUNDING_PER_PAGE_CHARS", "3500"))))
AI_GROUNDING_TOTAL_CHARS = max(4000, min(32000, int(os.environ.get("AI_GROUNDING_TOTAL_CHARS", "18000"))))
# If best Confluence hit scores below this, treat as "no relevant KB" → general IT answer (not KB-only). 0 = disable.
try:
    AI_GROUNDING_MIN_TOP_SCORE = float(os.environ.get("AI_GROUNDING_MIN_TOP_SCORE", "22"))
except ValueError:
    AI_GROUNDING_MIN_TOP_SCORE = 22.0


def _unbind_active_thread(thread_ts):
    data = active_threads.pop(thread_ts, None)
    if not data:
        return
    ch = data.get("dm_channel")
    if ch and dm_channel_active_thread.get(ch) == thread_ts:
        dm_channel_active_thread.pop(ch, None)


def _discard_dm_session_for_channel(dm_channel):
    """Drop any in-flight support thread for this DM (e.g. user starts a brand-new issue)."""
    ts = dm_channel_active_thread.pop(dm_channel, None)
    if ts and ts in active_threads:
        del active_threads[ts]


def _active_thread_for_dm(dm_channel, user_id):
    if not dm_channel or not user_id:
        return None
    ts = dm_channel_active_thread.get(dm_channel)
    if not ts or ts not in active_threads:
        if ts:
            dm_channel_active_thread.pop(dm_channel, None)
        return None
    if active_threads[ts].get("user_id") != user_id:
        return None
    return ts


def _bind_dm_thread(dm_channel, thread_ts, payload):
    active_threads[thread_ts] = payload
    dm_channel_active_thread[dm_channel] = thread_ts


def _ticket_summary_from_history(history):
    user_lines = [m["parts"][0]["text"].strip() for m in history if m.get("role") == "user"]
    if not user_lines:
        return "IT Help request from Slack"
    merged = " — ".join(user_lines)
    return merged[:240]


def _ticket_description_transcript(history, slack_user):
    lines = []
    for m in history:
        who = "User" if m.get("role") == "user" else "IT Help"
        lines.append(f"{who}: {m['parts'][0]['text']}")
    body = "\n\n".join(lines)
    return f"Created from Slack IT Help\n\nSlack User: {slack_user}\n\n--- Transcript ---\n{body[:7500]}"

# ---------- SLACK VERIFY ----------
def verify_slack(req):
    timestamp = req.headers.get("X-Slack-Request-Timestamp")
    signature = req.headers.get("X-Slack-Signature")

    if not timestamp or not signature:
        return False

    if abs(time.time() - int(timestamp)) > 300:
        return False

    body = req.get_data(as_text=True)
    base = f"v0:{timestamp}:{body}"

    my_sig = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        base.encode(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(my_sig, signature)

# ---------- AI KEYWORD EXTRACTION ----------
def _merge_keywords_with_bigrams(user_query, keywords):
    """Prepend 2-word phrases from the user query so search & anchors match titles, not only body."""
    bigrams = _extract_query_bigrams(user_query)
    seen = set()
    out = []
    for bg in bigrams:
        bl = bg.lower()
        if bl not in seen:
            seen.add(bl)
            out.append(bg)
    for k in keywords or []:
        lk = (k or "").strip().lower()
        if lk and lk not in seen:
            seen.add(lk)
            out.append(k.strip())
    return out[:8]


def extract_search_keywords(user_query):
    # Fast path for obvious IT troubleshooting phrases to avoid a model round-trip.
    cleaned = re.sub(r'[^a-zA-Z0-9 ]', ' ', (user_query or "").lower())
    tokens = [t for t in cleaned.split() if len(t) > 2]
    trouble_words = {
        "unable", "cannot", "cant", "can't", "connect", "connecting", "working", "broken", "error", "issue",
        "vpn", "twingate", "wifi", "wireless", "jumpcloud", "bitwarden", "outlook", "gmail", "login",
    }
    if any(t in trouble_words for t in tokens):
        uniq = []
        for t in tokens:
            if t not in uniq:
                uniq.append(t)
        # small synonym lift for common networking complaints
        if "wifi" in uniq and "wireless" not in uniq:
            uniq.append("wireless")
        if "cant" in uniq and "connect" not in uniq:
            uniq.append("connect")
        return _merge_keywords_with_bigrams(user_query, uniq)[:8]

    prompt = (
        "You are a search keyword extractor for an IT knowledge base.\n\n"
        "Given a user's IT issue described in natural language, extract 3-5 search keywords "
        "that would best match relevant knowledge base articles.\n\n"
        "RULES:\n"
        "- Fix obvious typos (e.g. 'twingat' -> 'twingate', 'outllook' -> 'outlook')\n"
        "- Include the main product/tool name (e.g. 'twingate', 'outlook', 'VPN')\n"
        "- Include the action/problem type (e.g. 'login', 'setup', 'install', 'connect')\n"
        "- Add 1-2 synonyms that KB articles might use (e.g. 'wifi' -> also 'wireless')\n"
        "- Drop filler words (my, not, working, please, help, etc.)\n"
        "- Prefer words that would plausibly appear in a KB *article title* (product, error, action).\n"
        "- Return ONLY comma-separated keywords, nothing else\n\n"
        f"User query: {user_query}\n\n"
        "Keywords:"
    )

    try:
        r = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 256}
            },
            timeout=10
        )
        r.raise_for_status()
        raw = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        keywords = [k.strip().lower() for k in raw.split(",") if k.strip() and len(k.strip()) > 1]
        if not keywords:
            keywords = [k.strip().lower() for k in raw.split() if k.strip() and len(k.strip()) > 1]
        return _merge_keywords_with_bigrams(user_query, keywords)
    except Exception:
        clean = re.sub(r'[^a-zA-Z0-9 ]', '', user_query.lower())
        fallback = [w for w in clean.split() if len(w) > 2][:5]
        return _merge_keywords_with_bigrams(user_query, fallback)

# ---------- CONFLUENCE SEARCH ----------
# Terms that match too many unrelated pages — not used alone to validate a hit
GENERIC_KB_TERMS = frozenset({
    "connect", "connection", "connecting", "unable", "issue", "issues", "help", "not", "working", "work",
    "setup", "set", "install", "login", "log", "access", "guide", "email", "mail", "google", "outlook",
    "migration", "migrating", "how", "use", "using", "request", "service", "computer", "laptop", "desktop",
    "network", "internet", "wifi", "wi", "fi", "wireless", "lan", "fix", "fixed", "problem", "problems",
    "error", "errors", "fail", "failed", "try", "click", "open", "check", "checks", "need", "please",
    "can", "you", "your", "the", "and", "for", "any", "into", "with", "from", "this", "that", "what",
    "when", "where", "user", "users", "account", "accounts", "password", "reset", "update", "updates",
    "application", "applications", "app", "apps", "system", "device", "devices", "phone", "mobile",
    "team", "teams", "company", "general", "basic", "advanced", "create", "creating", "new", "see",
})

# Short, high-signal tokens — always kept as anchors when present in the query/keywords
HIGH_SIGNAL_TERMS = frozenset({
    "twingate", "vpn", "zscaler", "forticlient", "anyconnect", "openvpn", "wireguard", "jumpcloud",
    "bitwarden", "okta", "duo", "intune", "jamf", "mdm", "citrix", "sap", "jira", "confluence",
    "trello", "github", "gitlab", "aws", "azure", "gcp", "slack", "zoom", "dialpad", "ringcentral",
})

# For “can’t connect / not working” style queries, require one of these *by name in the page title*
# so hub pages that only mention the product in passing (body) don’t win over real how-tos.
TITLE_STRICT_PRODUCTS = frozenset({
    "twingate", "netskope", "jumpcloud", "bitwarden", "okta", "zscaler", "citrix", "intune",
    "forticlient", "anyconnect", "wireguard", "openvpn", "jamf", "duo",
})

_TROUBLE_PATTERNS = re.compile(
    r"\b(unable|can\'?t|cannot|won\'?t|not\s+working|doesn\'?t\s+work|"
    r"unable\s+to\s+connect|can\'?t\s+connect|cannot\s+connect|won\'?t\s+connect|"
    r"not\s+connect|failing|failure|broken|error|issue\s+with|problem\s+with)\b",
    re.I,
)


def _troubleshooting_intent(query):
    return bool(query and _TROUBLE_PATTERNS.search(query))


def _strict_title_products_from_query(query):
    if not query or not _troubleshooting_intent(query):
        return []
    ql = query.lower()
    return sorted({t for t in TITLE_STRICT_PRODUCTS if t in ql})


def _hub_landing_title(title):
    """Broad integration/announcement pages — deprioritize for fix-it queries."""
    if not title:
        return False
    t = title.lower()
    return any(
        x in t
        for x in (
            "integration",
            "technology integration",
            "overview",
            "quick reference",
            "announcement",
            "newsletter",
            "migration timeline",
            "high-level",
            "high level",
            "guidance for staff",
            "technology changes",
            "onesyfe it",
            "key focus",
        )
    )


def _howto_signal_in_title(title):
    if not title:
        return False
    t = title.lower()
    return any(
        x in t
        for x in (
            "how to",
            "how do",
            "setup",
            "set up",
            "install",
            "login",
            "log in",
            "connect",
            "access",
            "troubleshoot",
            "fix",
            "guide",
            "using ",
            "vpn",
        )
    )


def _merge_unique_pages(first_list, second_list):
    seen = set()
    out = []
    for lst in (first_list, second_list):
        for page in lst:
            pid = page.get("id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            out.append(page)
    return out


def _strip_html_quick(html):
    if not html:
        return ""
    return re.sub(r"<[^>]+>", " ", html)


def _anchor_terms(keywords, query):
    """Specific terms the KB page should reflect; used to reject generic OR-bucket matches."""
    pool = list(keywords)
    pool.extend(re.findall(r"[a-z0-9]{2,}", query.lower()))
    seen = set()
    anchors = []
    for w in pool:
        w = w.strip().lower()
        if len(w) < 2 or w in seen:
            continue
        if w in HIGH_SIGNAL_TERMS or (len(w) >= 4 and w not in GENERIC_KB_TERMS) or (len(w) == 3 and w in {"mdm", "mfa", "sso", "ssl", "tls"}):
            seen.add(w)
            anchors.append(w)
    return anchors[:20]


def _relevance_score(anchors, title, body_blob):
    if not anchors:
        return 0
    t = (title or "").lower()
    b = (body_blob or "").lower()
    s = 0
    for a in anchors:
        if a in t:
            s += 25
        elif a in b:
            s += 5
    return s


def _title_keyword_hits(page, keywords):
    ttl = (page.get("title") or "").lower()
    return sum(1 for w in keywords if w and len(w) > 1 and w.lower() in ttl)


_STOP_TITLE_OVERLAP = frozenset({
    "with", "from", "that", "this", "have", "been", "need", "just", "want", "tell", "give",
    "help", "please", "would", "could", "should", "does", "did", "will", "into", "about", "some",
    "what", "when", "where", "which", "your", "are", "was", "how", "for", "the", "and", "you", "not",
})


def _extract_query_bigrams(user_query):
    """
    Pull 2-word phrases from the raw query for search + scoring (e.g. 'apple id', 'mac laptop').
    General — any topic; helps CQL and title-matching vs body-only hits.
    """
    q = (user_query or "").lower()
    out = []
    for m in re.finditer(r"\b([a-z]{3,})\s+([a-z]{2,})\b", q):
        out.append(f"{m.group(1)} {m.group(2)}")
    for m in re.finditer(r"\b([a-z]{2,})\s+([a-z]{3,})\b", q):
        pair = f"{m.group(1)} {m.group(2)}"
        if pair not in out:
            out.append(pair)
    seen = set()
    uniq = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:6]


def _lexical_title_alignment_boost(query, title):
    """
    General first-hit quality: prefer pages whose *title* matches the user's words and phrases.
    Penalizes hub-like hits that only match in the body (e.g. 'Mac' in Wi-Fi article body).
    """
    if not query or not title:
        return 0
    ql = query.lower()
    tt = title.lower()
    stop = GENERIC_KB_TERMS | _STOP_TITLE_OVERLAP
    words = [w for w in re.findall(r"[a-z0-9]{3,}", ql) if w not in stop]
    if not words:
        return 0
    seen = set()
    uniq = []
    for w in words:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    in_title = sum(1 for w in uniq[:16] if w in tt)
    score = min(17 * in_title, 130)
    for phrase in _extract_query_bigrams(query):
        if phrase in tt:
            score += 72
    score = min(score, 210)
    # Strong signal: user named several specifics but *none* appear in the KB title — often wrong article.
    if len(uniq) >= 3 and in_title == 0:
        score -= 135
    return score


def _run_cql(cql_query, limit=8):
    try:
        r = requests.get(
            f"{CONFLUENCE_BASE}/rest/api/content/search",
            params={"cql": cql_query, "limit": limit, "expand": "body.storage"},
            auth=AUTH,
            headers={"Accept": "application/json"},
            timeout=10
        )
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception:
        return []


def _escape_cql_term(term):
    """
    Escape user/model-provided term before interpolating into CQL quoted strings.
    """
    if term is None:
        return ""
    t = str(term).strip()
    # Keep it simple and safe for title ~ "..."
    t = t.replace("\\", "\\\\").replace('"', '\\"')
    # Remove characters that commonly break CQL parsing in this context
    t = re.sub(r"[\r\n\t]", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


def _kb_page_excerpt_for_ai_pick(page, max_chars=480):
    try:
        raw = page.get("body", {}).get("storage", {}).get("value") or ""
        soup = BeautifulSoup(raw, "html.parser")
        for img in soup.find_all(["ac:image", "img"]):
            img.decompose()
        text = soup.get_text("\n").strip()
        text = re.sub(r"\s+", " ", text)
        return text[:max_chars]
    except Exception:
        return ""


def _ai_pick_best_kb_index(user_query, ordered_pages):
    """
    Ask Gemini which candidate index best answers the question. Returns None → keep composite rank #0.
    Does not replace semantic search; only disambiguates among retrieved candidates.
    """
    if not ordered_pages or len(ordered_pages) < 2:
        return None
    n = min(len(ordered_pages), KB_AI_PICK_MAX_CANDIDATES)
    lines = []
    for i in range(n):
        p = ordered_pages[i]
        title = (p.get("title") or "")[:220]
        ex = _kb_page_excerpt_for_ai_pick(p, 420)
        lines.append(f"[{i}] TITLE: {title}\n    EXCERPT: {ex}\n")
    catalog = "\n".join(lines)
    prompt = (
        "You pick the single best internal IT knowledge article for the employee's question.\n\n"
        f"QUESTION:\n{(user_query or '').strip()[:1200]}\n\n"
        f"CANDIDATES (indices 0..{n - 1}):\n{catalog}\n"
        "Choose the ONE index whose topic and content most directly answer the question.\n"
        "Avoid picking an article that only mentions related words in passing (e.g. Wi‑Fi doc when they ask about Apple ID).\n"
        "If none fit reasonably, reply NONE.\n"
        f"Reply with ONLY an integer 0..{n - 1} or NONE. No other words."
    )
    try:
        raw = gemini_generate(
            contents=[{"parts": [{"text": prompt}]}],
            generation_config={"temperature": 0.1, "maxOutputTokens": 32},
            timeout=(8, 28),
            retries=1,
        )
        raw = (raw or "").strip()
        if re.search(r"\bNONE\b", raw, re.I):
            return None
        m = re.search(r"\b(\d{1,2})\b", raw)
        if m:
            idx = int(m.group(1))
            if 0 <= idx < n:
                return idx
    except Exception as exc:
        print(f"[DEBUG] _ai_pick_best_kb_index: {exc}")
    return None


def search_confluence(query, start_index=0):
    keywords = extract_search_keywords(query)
    print(f"[DEBUG] Extracted keywords for '{query}': {keywords}")
    if not keywords:
        return None

    safe_keywords = [_escape_cql_term(w) for w in keywords if _escape_cql_term(w)]
    if not safe_keywords:
        return None

    def _build_cql(words, operator="AND"):
        parts = [f'(title ~ "{w}" OR text ~ "{w}")' for w in words]
        return f'space.key="{SPACE_KEY}" AND ({f" {operator} ".join(parts)})'

    anchors = _anchor_terms(safe_keywords, query)
    print(f"[DEBUG] Anchor terms for relevance: {anchors}")

    strict_title_prods = _strict_title_products_from_query(query)
    title_focus_hits = []
    for term in strict_title_prods[:4]:
        safe_term = _escape_cql_term(term)
        if not safe_term:
            continue
        cql = f'space.key="{SPACE_KEY}" AND title ~ "{safe_term}"'
        title_focus_hits.extend(_run_cql(cql, limit=10))
    print(f"[DEBUG] Title-focused CQL pages: {len(title_focus_hits)} for terms {strict_title_prods}")

    results = _run_cql(_build_cql(safe_keywords, "AND"), limit=10)
    print(f"[DEBUG] Tier 1 AND results: {len(results)}")
    if not results:
        results = _run_cql(_build_cql(safe_keywords, "OR"), limit=20)
        print(f"[DEBUG] Tier 2 OR results: {len(results)}")

    merged = _merge_unique_pages(title_focus_hits, results or [])
    if not merged:
        return None

    trouble = _troubleshooting_intent(query)

    def _composite_score(page):
        title = page.get("title") or ""
        tl = title.lower()
        raw = page.get("body", {}).get("storage", {}).get("value", "") or ""
        blob = _strip_html_quick(raw)[:8000]
        rel = _relevance_score(anchors, title, blob)
        score = float(rel)
        score += _lexical_title_alignment_boost(query, title)
        score += 4 * _title_keyword_hits(page, safe_keywords)
        # Strong preference: named product appears in title on fix-it queries
        if trouble and strict_title_prods:
            for term in strict_title_prods:
                if term in tl:
                    score += 120
        elif strict_title_prods:
            for term in strict_title_prods:
                if term in tl:
                    score += 60
        # How-to style titles beat integration blurbs on trouble queries
        if trouble and _howto_signal_in_title(title):
            score += 35
        # Penalize org-wide hub / migration-announcement pages when user is trying to fix something
        if trouble and _hub_landing_title(title):
            score -= 100
        # Slight penalty if many body mentions but weak title (hub often lists every technology)
        title_anchor_hits = sum(1 for a in anchors if a and a.lower() in tl)
        if title_anchor_hits == 0 and rel > 0:
            score -= 25
        return score

    scored_pages = [( -_composite_score(p), p.get("title") or "", p) for p in merged ]
    scored_pages.sort(key=lambda x: (x[0], x[1]))
    ordered = [t[2] for t in scored_pages]

    # If the user named a specific product (Twingate, JumpCloud, etc.), the page must mention it
    mandatory = [a for a in anchors if a in HIGH_SIGNAL_TERMS and a in query.lower()]
    if mandatory:
        filtered = []
        for p in ordered:
            raw = p.get("body", {}).get("storage", {}).get("value", "") or ""
            blob_all = ((p.get("title") or "") + " " + _strip_html_quick(raw)).lower()
            if all(m in blob_all for m in mandatory):
                filtered.append(p)
        if not filtered:
            print(f"[DEBUG] No KB contains required terms {mandatory} — skipping irrelevant hits.")
            return None
        ordered = filtered

    # Fix-it question + named product (e.g. Twingate): drop pages that only mention it in the body
    if trouble and strict_title_prods:
        title_match = [
            p for p in ordered
            if any(t in (p.get("title") or "").lower() for t in strict_title_prods)
        ]
        if title_match:
            ordered = title_match
            print(f"[DEBUG] Restricted to pages with product in title: {[p.get('title') for p in ordered[:5]]}")
        else:
            no_hub = [p for p in ordered if not _hub_landing_title(p.get("title") or "")]
            if no_hub:
                ordered = no_hub
                print(f"[DEBUG] No strict-title product match; removed hub pages, candidates: {[p.get('title') for p in ordered[:5]]}")
            else:
                print(f"[DEBUG] No strict-title product match and only hub-style pages remain for {strict_title_prods}; use AI fallback.")
                return None

    if anchors:
        best_rel = _relevance_score(anchors, ordered[0].get("title") or "", _strip_html_quick(
            ordered[0].get("body", {}).get("storage", {}).get("value", "") or ""
        )[:8000])
        if best_rel == 0:
            print("[DEBUG] Top KB hit has zero anchor overlap — use AI path.")
            return None

    # Confidence gate: if a troubleshooting query still has weak KB confidence, prefer AI fallback.
    if trouble:
        top_score = _composite_score(ordered[0])
        top_title = (ordered[0].get("title") or "").lower()
        if strict_title_prods and top_score < 70:
            print(f"[DEBUG] Low KB confidence ({top_score}) for troubleshooting query; use AI fallback.")
            return None
        if _hub_landing_title(top_title):
            print("[DEBUG] Top KB appears to be a hub/overview page on troubleshooting query; use AI fallback.")
            return None

    print(f"[DEBUG] Ranked titles: {[p.get('title') for p in ordered]}")

    if start_index >= len(ordered):
        return None

    pick_idx = start_index
    if (
        start_index == 0
        and KB_AI_ARTICLE_PICK_ENABLED
        and len(ordered) > 1
    ):
        ai_ix = _ai_pick_best_kb_index(query, ordered)
        if ai_ix is not None:
            pick_idx = ai_ix
            print(
                f"[DEBUG] KB_AI_ARTICLE_PICK chose index {pick_idx}: "
                f"{ordered[pick_idx].get('title')!r}"
            )

    if pick_idx >= len(ordered):
        return None

    page = ordered[pick_idx]
    soup = BeautifulSoup(page["body"]["storage"]["value"], "html.parser")
    for img in soup.find_all(["ac:image", "img"]):
        img.decompose()
    raw_text = soup.get_text("\n").strip()

    return {
        "title": page["title"],
        "text": clean_kb_text(raw_text),
        "summary_source": kb_text_for_summary(raw_text),
        "url": f"{CONFLUENCE_BASE}{page['_links']['webui']}",
        "next_index": pick_idx + 1,
        "total": len(ordered)
    }

# ---------- KB TEXT OPTIMIZATION ----------
def clean_kb_text(raw_text):
    lines = raw_text.split("\n")
    cleaned = [line.strip() for line in lines if len(line.strip()) >= 30]
    text = "\n".join(cleaned)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:1100]


def kb_text_for_summary(raw_text, max_chars=12000):
    """
    Full enough Confluence body for summarize_kb (LLM). clean_kb_text is too short and cuts mid-article,
    which produced truncated / aadha summaries in Slack.
    """
    if not raw_text:
        return ""
    text = re.sub(r"\n{3,}", "\n\n", raw_text.strip())
    return text[:max_chars]


def _simple_grounding_score(page, query, safe_keywords, anchors):
    """Rank pages for AI grounding — wider net than strict KB first-hit (no hard reject gates)."""
    title = page.get("title") or ""
    raw = page.get("body", {}).get("storage", {}).get("value", "") or ""
    blob = _strip_html_quick(raw)[:8000]
    rel = _relevance_score(anchors, title, blob)
    s = float(rel)
    s += _lexical_title_alignment_boost(query, title)
    s += 4 * _title_keyword_hits(page, safe_keywords)
    tl = title.lower()
    ql = (query or "").lower()
    for term in TITLE_STRICT_PRODUCTS:
        if term in ql and term in tl:
            s += 40
    if _hub_landing_title(title):
        s -= 30
    return s


def _format_kb_excerpts_positive_block(body):
    if not (body or "").strip():
        return ""
    return (
        "--- INTERNAL KB EXCERPTS (only trusted source for procedures & company-specific facts; "
        "mention article title or URL when you rely on one) ---\n"
        f"{body}\n"
        "--- END INTERNAL KB EXCERPTS ---"
    )


def _retrieve_kb_grounding_payload(user_query):
    """
    Decide if Confluence has *relevant* snippets for grounding.

    Returns (use_kb_grounding: bool, excerpt_plaintext: str).
    When use_kb_grounding is False, excerpt_plaintext is always "".
    """
    if not AI_KB_GROUNDING_ENABLED:
        return False, ""
    q = (user_query or "").strip()
    if not q:
        return False, ""
    keywords = extract_search_keywords(q)
    if not keywords:
        return False, ""
    safe_keywords = [_escape_cql_term(w) for w in keywords if _escape_cql_term(w)]
    if not safe_keywords:
        return False, ""

    def _cql_or(words):
        parts = [f'(title ~ "{w}" OR text ~ "{w}")' for w in words]
        return f'space.key="{SPACE_KEY}" AND ({ " OR ".join(parts) })'

    results = _run_cql(_cql_or(safe_keywords), limit=25)
    if not results and len(safe_keywords) > 4:
        results = _run_cql(_cql_or(safe_keywords[:4]), limit=20)
    if not results:
        return False, ""

    anchors = _anchor_terms(safe_keywords, q)
    scored = [(_simple_grounding_score(p, q, safe_keywords, anchors), p) for p in results]
    scored.sort(key=lambda x: -x[0])
    top_score = float(scored[0][0]) if scored else 0.0

    if AI_GROUNDING_MIN_TOP_SCORE > 0 and top_score < AI_GROUNDING_MIN_TOP_SCORE:
        return False, ""

    pages = [p for _, p in scored[:AI_GROUNDING_MAX_PAGES]]

    chunks = []
    used = 0
    for i, page in enumerate(pages, 1):
        title = page.get("title") or "Untitled"
        url = f"{CONFLUENCE_BASE}{page['_links']['webui']}"
        raw = page.get("body", {}).get("storage", {}).get("value", "") or ""
        try:
            soup = BeautifulSoup(raw, "html.parser")
            for img in soup.find_all(["ac:image", "img"]):
                img.decompose()
            text = soup.get_text("\n").strip()
        except Exception:
            text = _strip_html_quick(raw)
        text = re.sub(r"\n{3,}", "\n\n", text)
        excerpt = text[:AI_GROUNDING_PER_PAGE_CHARS]
        block = f"=== [{i}] {title} ===\nURL: {url}\n\n{excerpt}\n\n"
        if used + len(block) > AI_GROUNDING_TOTAL_CHARS:
            break
        chunks.append(block)
        used += len(block)
    body = "".join(chunks).strip()
    if not body:
        return False, ""
    return True, body


def retrieve_kb_snippets_for_ai(user_query):
    """Plain excerpts only when grounding is considered useful; else empty string."""
    ok, body = _retrieve_kb_grounding_payload(user_query)
    return body if ok else ""


_SKIP_USER_FOR_RETRIEVAL = frozenset({"need more help", "need more help and kb exhausted"})
_NEED_MORE_USER_PREFIX = "Context for you: the employee pressed"


def _last_substantive_user_message(history):
    for m in reversed(history or []):
        if m.get("role") != "user":
            continue
        t = m["parts"][0]["text"].strip()
        if not t:
            continue
        if t.startswith(_NEED_MORE_USER_PREFIX):
            continue
        if t.lower() in _SKIP_USER_FOR_RETRIEVAL:
            continue
        return t
    return ""


def _retrieval_query_for_grounding(history, session_query=None):
    """Blend original issue (session) with latest user line for follow-up turns."""
    last = _last_substantive_user_message(history)
    sq = (session_query or "").strip()
    if sq and last:
        if last in sq or sq in last:
            return last[:2000]
        return f"{sq}\n{last}"[:2000]
    return (last or sq)[:2000]


def _normalize_query_for_cache(user_query):
    q = (user_query or "").strip().lower()
    q = re.sub(r"\s+", " ", q)
    return q[:200]


def _make_kb_summary_cache_key(title, raw_text, user_query):
    base = (
        (title or "").strip().lower()
        + "|"
        + _normalize_query_for_cache(user_query)
        + "|"
        + hashlib.sha256((raw_text or "").encode("utf-8")).hexdigest()
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _get_cached_kb_summary(cache_key):
    now = time.time()
    with summary_cache_lock:
        entry = summary_cache.get(cache_key)
        if not entry:
            return None
        expires_at, value = entry
        if expires_at <= now:
            summary_cache.pop(cache_key, None)
            return None
        return value


def _set_cached_kb_summary(cache_key, value):
    now = time.time()
    with summary_cache_lock:
        if len(summary_cache) >= SUMMARY_CACHE_MAX_ENTRIES:
            # remove expired entries first
            expired_keys = [k for k, (exp, _) in summary_cache.items() if exp <= now]
            for k in expired_keys:
                summary_cache.pop(k, None)
        if len(summary_cache) >= SUMMARY_CACHE_MAX_ENTRIES:
            # if still full, evict the earliest-expiring entry
            oldest_key = min(summary_cache, key=lambda k: summary_cache[k][0])
            summary_cache.pop(oldest_key, None)
        summary_cache[cache_key] = (now + SUMMARY_CACHE_TTL_SEC, value)

# ---------- CONVERSATION HISTORY OPTIMIZATION ----------
def trim_history(history):
    MAX_RECENT = 4
    MAX_TOTAL = 6

    if len(history) <= MAX_TOTAL:
        return history

    old_messages = history[:-MAX_RECENT]
    recent_messages = history[-MAX_RECENT:]

    old_texts = []
    for msg in old_messages:
        role = "User" if msg["role"] == "user" else "Bot"
        text = msg["parts"][0]["text"][:100]
        old_texts.append(f"{role}: {text}")

    summary = "Previous conversation summary: " + " | ".join(old_texts)
    summary = summary[:300]

    return [
        {"role": "user", "parts": [{"text": summary}]},
        {"role": "model", "parts": [{"text": "Got it, I have the context from earlier."}]}
    ] + recent_messages

# ---------- SLACK FORMATTING ----------
def slack_format(text):
    text = re.sub(r"### (.*)", r"*\1*", text)
    text = re.sub(r"## (.*)", r"*\1*", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"*\1*", text)
    return text


def gemini_generate(contents, generation_config, timeout=(10, 60), retries=2):
    """
    Wrapper for Gemini calls with retry/backoff for transient timeouts/network blips.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                GEMINI_URL,
                params={"key": GEMINI_API_KEY},
                headers={"Content-Type": "application/json"},
                json={"contents": contents, "generationConfig": generation_config},
                timeout=timeout,
            )
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            last_exc = exc
            low = str(exc).lower()
            transient = any(
                k in low
                for k in (
                    "read timed out",
                    "timed out",
                    "connection aborted",
                    "temporarily unavailable",
                    "bad gateway",
                    "service unavailable",
                    "gateway timeout",
                    "502",
                    "503",
                    "504",
                )
            )
            rate_limited = any(
                k in low
                for k in ("429", "too many requests", "resource exhausted", "resourceexhausted")
            )
            if attempt < retries and (transient or rate_limited):
                # Longer pause for quota/rate limits so a retry can succeed.
                delay = (3.5 * (attempt + 1)) if rate_limited else (1.2 * (attempt + 1))
                time.sleep(delay)
                continue
            raise
    raise last_exc

# ---------- GEMINI AI (one-shot) ----------
def ask_ai(user_input, retrieval_query=None):
    base_prompt = (
        "You are a *senior internal IT support engineer* on Slack. You sound calm, precise, and experienced — "
        "like a real L2 engineer who triages quickly and explains clearly.\n\n"
        "VOICE & QUALITY:\n"
        "- Acknowledge the issue in one short line (what you understood).\n"
        "- State assumptions briefly if needed (*e.g.* 'Assuming this is on your work laptop.*')\n"
        "- Give *numbered* troubleshooting in *strict order* — simplest checks first.\n"
        "- Be specific: name UI paths, toggles, and expected outcomes (*what they should see next*).\n"
        "- If information is missing to proceed safely, ask 1–3 *bullet* clarifying questions at the end "
        "(*OS*, *error text*, *when it started*, *Wi‑Fi vs wired*, *VPN on/off*).\n"
        "- Never guess credentials, policies, or URLs your company did not provide in the message.\n"
        "- If the issue could be account-side or infra-side, say so honestly and offer the ticket path.\n\n"
        "SECURITY RULES (strictly follow):\n"
        "- Give *all steps a normal employee can do* without admin or elevated rights.\n"
        "- If a step needs admin (registry, PowerShell/sudo/terminal, BIOS, Group Policy, security bypass, etc.), "
        "do *not* describe that procedure — use one line: *_This part must be done by IT — create a Jira ticket._*\n"
        "- NEVER suggest disabling antivirus, firewall, security agents, MDM, or company VPN enforcement.\n"
        "- Do *not* replace the whole answer with a generic escalation if other user-safe steps exist — only omit the privileged parts.\n"
        "- If *everything* in the scenario is admin-only, say briefly to open a Jira ticket.\n\n"
        "FORMAT RULES (for Slack messages):\n"
        "- Start with a one-line *summary* of the most likely cause or category.\n"
        "- Use numbered steps (1. 2. 3.) for troubleshooting.\n"
        "- Bold key actions with *bold* (single asterisk, Slack format).\n"
        "- Keep each step to 1–3 tight sentences — no filler.\n"
        "- Do NOT use markdown headings (# or ##) or **double bold**.\n"
        "- Do NOT use tables.\n"
        "- End with: 'If this doesn't resolve it, create a Jira ticket so we can pick up with full tooling and access.'\n"
        "- Keep total response under 2000 characters.\n\n"
    )
    if AI_KB_GROUNDING_ENABLED:
        rq = (retrieval_query or "").strip() or (user_input or "")[:1200]
        use_kb, kb_body = _retrieve_kb_grounding_payload(rq)
        if use_kb:
            extra = (
                CHAT_KB_GROUNDING_APPEND
                + "\n\n"
                + _format_kb_excerpts_positive_block(kb_body)
                + "\n\n"
            )
        else:
            extra = CHAT_GENERAL_NO_KB_APPEND + "\n\n"
        safe_system_prompt = base_prompt + extra + f"User issue / context:\n{user_input}"
    else:
        safe_system_prompt = base_prompt + f"User issue:\n{user_input}"

    raw_answer = gemini_generate(
        contents=[{"parts": [{"text": safe_system_prompt}]}],
        generation_config={"temperature": 0.35, "maxOutputTokens": 1800},
        timeout=(10, 60),
        retries=2,
    )
    return enforce_security_policy(raw_answer)

# Phrases that indicate a line/paragraph should not be given as end-user self-service steps
_SENSITIVE_HINTS = (
    "registry", "regedit", "powershell", "pwsh", "sudo ", " sudo", "terminal command",
    "open terminal", "command prompt", "cmd.exe", "disable antivirus", "disable firewall",
    "turn off antivirus", "turn off firewall", "group policy", "gpedit", "bios", "firmware",
    "admin rights", "administrator privileges", "elevated privileges", "run as administrator",
    "system32", "security policy", "local admin", "domain admin", "uac ", " icacls",
    "chmod ", "chown ", "/etc/", "hlm\\", "hkey_", "net user", "diskpart",
)


def _line_or_paragraph_sensitive(text_chunk):
    low = text_chunk.lower()
    return any(h in low for h in _SENSITIVE_HINTS)


def redact_sensitive_instructions(text):
    """
    Remove or replace only the lines/paragraphs that describe privileged/admin actions;
    keep the rest of the guidance visible.
    """
    if not text or not str(text).strip():
        return text

    placeholder = "_[(Needs *IT admin* — omitted here. Type `ticket`.)]_"
    footer = (
        "\n\n_Sections above that require administrator or elevated access were shortened. "
        "Create a *ticket* for anything that still needs IT._"
    )

    paragraphs = re.split(r"\n{2,}", text.strip())
    out_paragraphs = []
    any_redacted = False

    for para in paragraphs:
        if not _line_or_paragraph_sensitive(para):
            out_paragraphs.append(para)
            continue

        lines = para.split("\n")
        new_lines = []
        for line in lines:
            if _line_or_paragraph_sensitive(line):
                any_redacted = True
                new_lines.append(placeholder)
            else:
                new_lines.append(line)

        # Drop duplicate placeholders in a row
        deduped = []
        prev_was_ph = False
        for ln in new_lines:
            is_ph = ln.strip() == placeholder.strip()
            if is_ph and prev_was_ph:
                continue
            deduped.append(ln)
            prev_was_ph = is_ph

        out_paragraphs.append("\n".join(deduped))

    result = "\n\n".join(out_paragraphs)
    if any_redacted:
        result = result + footer

    # If we stripped almost everything, escalate instead of an empty-looking message
    alnum = re.sub(r"[^a-zA-Z0-9]+", "", result)
    if any_redacted and len(alnum) < 120:
        return (
            "⚠️ Most of this guidance involves *administrator-only* steps we can’t paste in chat.\n\n"
            "Create a Jira *ticket* so IT can run the privileged parts safely."
        )
    return result


def enforce_security_policy(ai_text):
    """Prefer redaction of risky lines; only hard-block when almost nothing safe remains."""
    return redact_sensitive_instructions(ai_text)

# ---------- GEMINI AI (multi-turn chat) ----------
CHAT_SYSTEM_PROMPT = (
    "You are a *senior internal IT support engineer* in a *private Slack DM* with an employee.\n\n"
    "YOUR JOB:\n"
    "- Troubleshoot methodically like on a service desk: confirm symptoms → narrow scope → next checks.\n"
    "- Be *precise*: name exact clicks/settings, expected results, and what *not* to change.\n"
    "- Keep a warm but *professional* tone — confident, not chatty; no corporate fluff.\n"
    "- If the user is stuck after steps, ask focused follow-ups (OS, exact error text, screenshots if they can).\n"
    "- If they typed `ticket` context earlier, remember you're closing the loop toward escalation when needed.\n\n"
    "CRITICAL RULES:\n"
    "1. Give *user-level* steps the employee can do alone; keep them in order.\n"
    "2. When the source mentions admin-only work (registry, PowerShell/sudo/terminal, BIOS, GPO, etc.), "
    "do *not* paste those steps — one line each: *_IT must do this step — type `ticket` if needed._*\n"
    "3. NEVER suggest disabling antivirus, firewall, MDM, VPN enforcement, or company security tooling.\n"
    "4. Do *not* delete the entire reply because one step is admin-only; keep the safe steps visible.\n"
    "5. If literally nothing is safe without admin, say to type `ticket` for escalation.\n"
    "6. Prefer short paragraphs plus numbered steps; end with what to try next *or* what info you need.\n"
    "7. If the user says the issue is resolved, confirm briefly and wish them well.\n"
    "8. Earlier messages may include a *knowledge-base summary* you (or the system) pasted — "
    "treat that as ground truth for follow-ups; don’t pretend it never happened."
)

CHAT_KB_GROUNDING_APPEND = (
    "\n\nKB-ONLY GROUNDING (mandatory when this block appears):\n"
    "- The *INTERNAL KB EXCERPTS* section in this message is the authoritative source for *specific* "
    "procedures, company tool names as documented, and policy facts.\n"
    "- Base numbered troubleshooting *only* on those excerpts. Do *not* add steps from general internet "
    "knowledge or guesswork.\n"
    "- If excerpts clearly cover the issue, synthesize and cite which article (title or URL) you used.\n"
    "- If excerpts are missing or insufficient, say so honestly — do not invent fixes; suggest `ticket` "
    "and ask neutral clarifiers.\n"
    "- You may still use the conversation transcript for *symptoms* and *what the user tried*; not for "
    "new technical facts absent from excerpts."
)

CHAT_GENERAL_NO_KB_APPEND = (
    "\n\nNO INTERNAL KB MATCH (mandatory context):\n"
    "Confluence did not return a confident internal article for this question (no results, or relevance "
    "score too low). You are *not* limited to KB excerpts.\n"
    "- Give *general* workplace IT troubleshooting using widely accepted best practices, subject to the "
    "same security rules above (user-level steps only; omit admin/registry/terminal/BIOS procedures; never "
    "disable antivirus, firewall, MDM, or VPN enforcement; use one-line `ticket` deferrals for privileged work).\n"
    "- Open with one short line such as: *_No matching internal KB article — this is general guidance; your "
    "org’s policies and tools may differ._*\n"
    "- Do *not* invent company-specific URLs, product rollouts, or policies. If the issue is org-specific, "
    "say so and suggest `ticket`.\n"
    "- You may ask neutral clarifying questions (OS, exact error, when it started) like a service desk engineer."
)

# "Need More Help" after a KB card: steer model toward discovery before more KB-style answers.
_NMH_AI_DISCOVERY_HINT = (
    "Context for you: the employee pressed *Need More Help* — the article shown was not enough or not "
    "quite right for them.\n\n"
    "Your reply should *prioritize understanding* their real problem before heavy troubleshooting: "
    "what they are trying to accomplish, exact symptoms, OS/device if relevant, precise error text, "
    "and what they expected vs what happened. Ask focused, respectful questions; avoid dumping another "
    "generic article list. Offer `ticket` if they are blocked or this needs admin access."
)

def ask_ai_with_history(history, session_query=None):
    trimmed = trim_history(history)
    if AI_KB_GROUNDING_ENABLED:
        rq = _retrieval_query_for_grounding(trimmed, session_query)
        use_kb, kb_body = _retrieve_kb_grounding_payload(rq)
        if use_kb:
            system_text = (
                CHAT_SYSTEM_PROMPT
                + CHAT_KB_GROUNDING_APPEND
                + "\n\n"
                + _format_kb_excerpts_positive_block(kb_body)
            )
        else:
            system_text = CHAT_SYSTEM_PROMPT + CHAT_GENERAL_NO_KB_APPEND
    else:
        system_text = CHAT_SYSTEM_PROMPT

    contents = [
        {"role": "user", "parts": [{"text": system_text}]},
        {"role": "model", "parts": [{"text": "Understood. I will follow all the rules."}]},
    ]
    contents.extend(trimmed)

    raw = gemini_generate(
        contents=contents,
        generation_config={"temperature": 0.35, "maxOutputTokens": 1800},
        timeout=(10, 60),
        retries=2,
    )
    return enforce_security_policy(raw)

# ---------- SLACK BOT HELPERS ----------
def slack_post_message(channel, text=None, thread_ts=None, blocks=None):
    payload = {"channel": channel}
    if text is not None:
        payload["text"] = text
    if blocks is not None:
        payload["blocks"] = blocks
    if not payload.get("text") and not payload.get("blocks"):
        payload["text"] = "IT Help"
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json=payload,
        timeout=10
    )
    resp = r.json()
    if not resp.get("ok"):
        print(f"[DEBUG] slack_post_message FAILED: {resp.get('error')}")
    return resp.get("ts")


def slack_update_message(channel, ts, text=None, blocks=None):
    payload = {"channel": channel, "ts": ts}
    if text is not None:
        payload["text"] = text
    if blocks is not None:
        payload["blocks"] = blocks
    r = requests.post(
        "https://slack.com/api/chat.update",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json=payload,
        timeout=10
    )
    data = r.json()
    if not data.get("ok"):
        print(f"[DEBUG] slack_update_message FAILED: {data.get('error')}")
    return data.get("ok")


def slack_delete_message(channel, ts):
    r = requests.post(
        "https://slack.com/api/chat.delete",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"channel": channel, "ts": ts},
        timeout=10
    )
    data = r.json()
    if not data.get("ok"):
        print(f"[DEBUG] slack_delete_message FAILED: {data.get('error')}")
    return data.get("ok")


def slack_post_ephemeral(channel, user_id, text):
    r = requests.post(
        "https://slack.com/api/chat.postEphemeral",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"channel": channel, "user": user_id, "text": text},
        timeout=10
    )
    data = r.json()
    if not data.get("ok"):
        print(f"[DEBUG] slack_post_ephemeral FAILED: {data.get('error')}")
    return data.get("ok")


def get_bot_user_id():
    """
    Resolve bot user id once (used for mention-detection fallback on message events).
    """
    global BOT_USER_ID
    if BOT_USER_ID:
        return BOT_USER_ID
    try:
        r = requests.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            BOT_USER_ID = data.get("user_id")
            return BOT_USER_ID
    except Exception as e:
        print(f"[DEBUG] get_bot_user_id ERROR: {e}")
    return None


def slack_publish_home(user_id):
    """
    Publish a simple, useful App Home so users don't see Slack's default placeholder.
    """
    home_view = {
        "type": "home",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🤖 IT Help Bot", "emoji": True},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Welcome!* I can help you troubleshoot common IT issues and guide you to the right next step quickly."
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Main Facilities*\n"
                        "• *Smart KB Search* — finds relevant Confluence articles for your issue\n"
                        "• *Need More Help* — checks additional related articles (up to 2) before AI fallback\n"
                        "• *AI Troubleshooting* — step-by-step guidance when KB is not enough\n"
                        "• *Private Support Flow* — continues sensitive troubleshooting in DM\n"
                        "• *Jira Ticket Creation* — type `ticket` or use `/it jira <issue>` for escalation"
                    ),
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*How To Use*\n"
                        "1. In any channel: `/it help <your issue>`\n"
                        "2. Or open *Messages* tab and describe your issue directly\n"
                        "3. If needed, click *Need More Help* for deeper assistance"
                    ),
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "_For security, admin-only steps are not exposed in chat. "
                            "If elevated access is required, please create a Jira ticket._"
                        ),
                    }
                ],
            },
        ],
    }

    r = requests.post(
        "https://slack.com/api/views.publish",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"user_id": user_id, "view": home_view},
        timeout=10,
    )
    data = r.json()
    if not data.get("ok"):
        print(f"[DEBUG] slack_publish_home FAILED: {data.get('error')}")
    return data.get("ok")


def post_live_wait_status(channel, thread_ts=None, phase="working"):
    phase_text = {
        "kb_lookup": ":hourglass_flowing_sand: _Scanning internal KB… pulling the best match for you._",
        "kb_next": ":hourglass_flowing_sand: _Checking another KB article… one moment._",
        "ai_think": ":hourglass_flowing_sand: _Analyzing your message… drafting precise next steps._",
        "ai_fallback": ":hourglass_flowing_sand: _No reliable KB fix found — switching to AI troubleshooting._",
        "working": ":hourglass_flowing_sand: _Working on this now…_",
    }
    return slack_post_message(channel, phase_text.get(phase, phase_text["working"]), thread_ts=thread_ts)


def start_live_spinner(channel, label, thread_ts=None, interval_s=1.0):
    """
    Show a moving status indicator by updating one Slack message repeatedly.
    Returns stop() callback that removes spinner message.
    """
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    ts = slack_post_message(channel, f"{frames[0]} _{label}_", thread_ts=thread_ts)
    stop_event = threading.Event()

    if not ts:
        def _noop():
            return
        return _noop

    def _run():
        i = 1
        while not stop_event.wait(interval_s):
            frame = frames[i % len(frames)]
            slack_update_message(channel, ts, text=f"{frame} _{label}_")
            i += 1

    threading.Thread(target=_run, daemon=True).start()

    def _stop():
        stop_event.set()
        slack_delete_message(channel, ts)

    return _stop


def open_dm(user_id):
    r = requests.post(
        "https://slack.com/api/conversations.open",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"users": user_id},
        timeout=10
    )
    data = r.json()
    if data.get("ok"):
        return data["channel"]["id"]
    print(f"[DEBUG] open_dm FAILED: {data.get('error')}")
    return None

def start_ai_dm_thread(query, user_id):
    dm_channel = open_dm(user_id)
    print(f"[DEBUG] start_ai_dm_thread: dm_channel={dm_channel}")
    if not dm_channel:
        print("[DEBUG] Failed to open DM channel")
        return

    _discard_dm_session_for_channel(dm_channel)

    thread_ts = slack_post_message(
        dm_channel,
        f"👋 *IT Support* — I’ve got your request.\n\n"
        f"*What you reported:* _{query}_\n\n"
        f"_Reviewing details now — I’ll reply in this thread with the next steps._"
    )
    if not thread_ts:
        return

    history = [{"role": "user", "parts": [{"text": query}]}]
    stop_spinner = start_live_spinner(dm_channel, "Analyzing your issue and preparing first steps…", thread_ts=thread_ts)
    try:
        ai_response = ask_ai_with_history(history, session_query=query)
        ai_slack = slack_format(ai_response)
        history.append({"role": "model", "parts": [{"text": ai_response}]})
    finally:
        stop_spinner()

    _bind_dm_thread(
        dm_channel,
        thread_ts,
        {
            "dm_channel": dm_channel,
            "user_id": user_id,
            "query": query,
            "next_index": 0,
            "history": history,
        },
    )

    slack_post_message(
        dm_channel,
        f"{ai_slack}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💬 _Reply in this thread to continue chatting._\n"
        "Type `done` to end  •  Type `ticket` to create a Jira ticket.",
        thread_ts=thread_ts
    )
    slack_post_message(
        dm_channel,
        text="Need more help options",
        blocks=_ai_controls_blocks(query=query, next_index=0),
        thread_ts=thread_ts,
    )

# ---------- KB SUMMARIZATION ----------
def summarize_kb(title, raw_text, user_query):
    cache_key = _make_kb_summary_cache_key(title, raw_text, user_query)
    cached = _get_cached_kb_summary(cache_key)
    if cached:
        print(f"[DEBUG] KB summary cache hit: title={title[:80]!r}")
        return cached

    prompt = (
        "You are a *senior IT support engineer* rewriting an internal KB article for Slack.\n\n"
        "A Confluence article matched the employee’s request. Rewrite it as a *clear, accurate runbook* — "
        "professional, precise, and easy to follow for a non‑technical reader.\n\n"
        "WRITING RULES:\n"
        "- Open with one line tying the article to *their* question (what this doc helps them do).\n"
        "- Use numbered steps (1. 2. 3.). Each step must say *where* to go, *what* they see, *what* to do.\n"
        "- Bold buttons, menu names, and field labels with *bold* (single asterisk — Slack format).\n"
        "- If Windows vs Mac differs, label sections *Windows* / *Mac*.\n"
        "- Call out common failure points (*wrong account, cached credentials, VPN state*) when the source implies it.\n"
        "- If the source is vague, say what is *known from the article* and what *needs IT* — don’t invent policy.\n"
        "- Skip TOC, metadata, author noise, and unrelated sections.\n"
        "- Do NOT use markdown headings (# or ##) or **double bold**.\n"
        "- Do NOT use tables.\n"
        "- If the article clearly does *not* address the user’s issue, say so in one honest sentence "
        "and suggest they reply with more detail or open a ticket.\n\n"
        "ADMIN / ELEVATED CONTENT (critical):\n"
        "- The Confluence source may mix *end-user* steps with *IT-only* steps (registry, PowerShell, sudo, "
        "terminal commands, CMD, BIOS/firmware, Group Policy, disabling security tools, system folders like "
        "*System32*, etc.).\n"
        "- **Include** every step a typical employee can follow *without* admin or elevated rights.\n"
        "- For any admin-only step: **do not** copy the technical procedure. Replace with a single line like: "
        "*_This step requires IT administrator access — type `ticket` if you need it._*\n"
        "- **Do not** refuse to summarize the whole article because it contains some admin sections — "
        "always pass through the safe, user-runnable parts.\n"
        "- Never tell the reader to disable antivirus, firewall, MDM, or mandatory VPN.\n"
        "- End with a complete sentence. Do *not* stop mid-step or mid-word — if space is tight, shorten earlier steps, "
        "not the last line.\n\n"
        f"User's issue: {user_query}\n\n"
        f"Article title: {title}\n\n"
        f"Article content:\n{raw_text}"
    )

    try:
        summary = gemini_generate(
            contents=[{"parts": [{"text": prompt}]}],
            generation_config={"temperature": 0.25, "maxOutputTokens": 2800},
            timeout=(10, 70),
            retries=2,
        )
        final_summary = enforce_security_policy(summary)
        _set_cached_kb_summary(cache_key, final_summary)
        return final_summary
    except Exception:
        fallback = enforce_security_policy(raw_text[:4500])
        _set_cached_kb_summary(cache_key, fallback)
        return fallback


def _summary_chunk_at_limit(text, limit):
    """Prefer paragraph/sentence boundary so Slack blocks don’t end on 'limi…'."""
    if len(text) <= limit:
        return text, ""
    chunk = text[:limit]
    for sep in ("\n\n", "\n", ". "):
        cut = chunk.rfind(sep)
        if cut >= limit // 2:
            end = cut + (2 if sep == ". " else len(sep))
            return text[:end].rstrip(), text[end:].lstrip()
    return chunk.rstrip(), text[limit:].lstrip()


def _summary_to_blocks(summary_text):
    BLOCK_LIMIT = 2900
    if len(summary_text) <= BLOCK_LIMIT:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": summary_text}}]

    blocks = []
    rest = summary_text
    while rest:
        if len(rest) <= BLOCK_LIMIT:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": rest}})
            break
        chunk, rest = _summary_chunk_at_limit(rest, BLOCK_LIMIT)
        if chunk:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
    return blocks


def _kb_blocks_with_actions(kb, kb_summary, query):
    """Block Kit for a KB card + Need More Help + Create Jira (same shape in DM and slash ephemeral)."""
    summary_blocks = _summary_to_blocks(kb_summary)
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"📘 {kb['title'][:130]}", "emoji": True}},
        *summary_blocks,
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"🔗 <{kb['url']}|Open full article with images>"}},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "_Still not the right doc? Click *Need More Help* below, or reply here with what you expected. `ticket` opens Jira._"}],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "need_more_help",
                    "text": {"type": "plain_text", "text": "Need More Help"},
                    "value": json.dumps({"query": query, "index": kb["next_index"]}),
                },
                {
                    "type": "button",
                    "action_id": "create_jira_ticket",
                    "text": {"type": "plain_text", "text": "Create Jira Ticket"},
                    "style": "primary",
                    "value": query,
                },
            ],
        },
    ]


def _ai_controls_blocks(query, next_index=0):
    return [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "_Need more depth? Use the button below. I’ll check up to 2 related KBs, then continue with AI if needed._"}],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "need_more_help",
                    "text": {"type": "plain_text", "text": "Need More Help"},
                    "value": json.dumps({"query": query, "index": next_index}),
                },
                {
                    "type": "button",
                    "action_id": "create_jira_ticket",
                    "text": {"type": "plain_text", "text": "Create Jira Ticket"},
                    "style": "primary",
                    "value": query,
                },
            ],
        },
    ]


def _interaction_is_dm_channel(payload):
    cid = (payload.get("channel") or {}).get("id") or ""
    return cid.startswith("D")


def _interaction_thread_ts(payload):
    msg = payload.get("message") or {}
    return msg.get("thread_ts") or msg.get("ts")


# ---------- SIDEBAR DM ROUTING (engineer-like UX) ----------
_IT_TOPIC = re.compile(
    r"\b(vpn|twingate|wifi|wi-?fi|wireless|lan|ethernet|email|outlook|gmail|password|passcode|mfa|2fa|otp|"
    r"laptop|macbook|windows|pc|imac|monitor|dock|usb|printer|drive|onedrive|google\s*drive|slack|teams|zoom|"
    r"jumpcloud|bitwarden|mdm|intune|citrix|sap|license|install|uninstall|update|upgrade|"
    r"crash|hang|freeze|slow|error|login|sign\s*in|cannot|can't|can\s*not|unable|broken|"
    r"not\s*working|doesn'?t\s*work|won'?t\s*connect|no\s*internet|screen|display|audio|microphone|camera|"
    r"headset|keyboard|mouse)\b",
    re.I,
)

_FEEDBACK = re.compile(
    r"\b("
    r"not\s*what|wrong\s*(article|doc|answer)|not\s*(this|that)|didn'?t\s*help|doesn'?t\s*help|"
    r"irrelevant|bad\s*answer|useless|still\s*stuck|not\s*solved|doesn'?t\s*match|nope|that'?s\s*wrong|"
    r"not\s+what\s+i\s*'?m\s+looking\s+for|not\s+what\s+i\s+need|"
    r"isn'?t\s+what\s+i\s*'?m\s+looking\s+for|isn'?t\s+the\s+right\s+(article|doc|one)|"
    r"this\s+isn'?t\s+what\s+i\s+(need|want)|this\s+isn'?t\s+it|"
    r"wrong\s+(article|doc|one)|different\s+article|not\s+the\s+right\s+(article|doc|one)"
    r")\b",
    re.I,
)

# Common typo: user omits “not” (“no, this is what i am looking for” meaning the opposite)
_FEEDBACK_TYPOS = re.compile(
    r"(\bno\s*,?\s+this\s+is\s+(what\s+)?i\s+am\s+looking\s+for\b)|"
    r"(\bno\s*,?\s+thisis\s+what\s+i\s+am\s+looking\s+for\b)",
    re.I,
)

def is_feedback_text(text):
    if not text:
        return False
    if _FEEDBACK_TYPOS.search(text):
        return True
    return bool(_FEEDBACK.search(text))


_GREETING_ONLY = re.compile(
    r"^(hi+|hello+|hey+|good\s+(morning|afternoon|evening)|hiya|yo|sup|howdy|thanks?|thank\s+you|ty|thx|"
    r"bye+|cya|later|cheers|ok+ay?)[\s,!.]*$",
    re.I,
)


def classify_sidebar_message(text):
    """Route sidebar DM without an extra model call when possible."""
    t = (text or "").strip()
    if not t:
        return "UNCLEAR"
    low = t.lower()
    # Reserved session commands — must never be treated as generic "greeting"
    if low in ("ticket", "tickets"):
        return "TICKET_CMD"
    if low in ("done", "end", "exit"):
        return "DONE_CMD"
    if is_feedback_text(t):
        return "FEEDBACK"
    if _IT_TOPIC.search(t):
        return "IT_ISSUE"
    if _GREETING_ONLY.match(t) and len(t) < 80:
        return "GREETING"
    if len(t) < 50 and not any(ch.isdigit() for ch in t):
        return "GREETING"
    return "IT_ISSUE"


def engineer_smalltalk_reply(user_text):
    prompt = (
        "You are a *senior IT support engineer* replying in Slack DM. The employee sent a short social message.\n\n"
        "Reply in 2–4 sentences: professional, human, not robotic — but not overly casual.\n"
        "- Acknowledge them briefly.\n"
        "- Invite them to describe a *work technology* issue (*VPN, email, laptop, access, software*).\n"
        "- Do not invent troubleshooting steps or tools.\n\n"
        "FORMAT: Slack mrkdwn — *bold* only for emphasis; no ## headings or **double bold**; no tables.\n\n"
        f"Employee message: {user_text}"
    )
    try:
        raw = gemini_generate(
            contents=[{"parts": [{"text": prompt}]}],
            generation_config={"temperature": 0.35, "maxOutputTokens": 512},
            timeout=(10, 45),
            retries=2,
        )
        return enforce_security_policy(raw)
    except Exception:
        return (
            "Hi — I’m here. Tell me what’s going wrong with your *work tech* "
            "(*VPN*, *email*, *laptop*, *access*, *software*), and I’ll walk you through the next checks."
        )


def engineer_feedback_reply(user_text, prior_snippets):
    ctx = "\n".join(prior_snippets[-6:]) if prior_snippets else "(no prior thread context)"
    augmented = (
        "Context — recent DM lines:\n"
        f"{ctx}\n\n"
        "The user indicates the previous answer, article, or direction wasn’t what they needed.\n\n"
        f"Their latest message: {user_text}\n\n"
        "Respond as a *senior IT engineer*:\n"
        "- Brief apology, no drama.\n"
        "- Acknowledge you may have missed their goal.\n"
        "- Ask *specific* clarifiers (what they expected, system/OS, exact error text, app name, when it started).\n"
        "- Do *not* paste a generic KB wall or repeat the last article unless they ask.\n"
        "- Mention they can use *Need More Help* on the last KB card if one was shown.\n"
        "- Offer `ticket` if they’re blocked or it needs admin access.\n\n"
        "FORMAT: Slack — numbered list optional; *bold* for emphasis; no ## or **; no tables; keep under ~1200 chars."
    )
    try:
        return enforce_security_policy(ask_ai(augmented, retrieval_query=user_text))
    except Exception:
        return (
            "Sorry that wasn’t the right fix. Tell me in one sentence *what outcome you need*, "
            "your *OS* if relevant, and any *exact error text* — I’ll narrow this down. "
            "If you’re blocked, type `ticket` to log it for IT."
        )


def _post_kb_thread_and_register(dm_channel, user_id, user_text, kb, query):
    src = kb.get("summary_source") or kb["text"]
    kb_summary = summarize_kb(kb["title"], src, query)
    blocks = _kb_blocks_with_actions(kb, kb_summary, query)
    _discard_dm_session_for_channel(dm_channel)

    thread_ts = slack_post_message(dm_channel, text=f"KB: {kb['title']}", blocks=blocks)
    if not thread_ts:
        return
    clip = kb_summary[:3500]
    _bind_dm_thread(
        dm_channel,
        thread_ts,
        {
            "dm_channel": dm_channel,
            "user_id": user_id,
            "query": query,
            "next_index": kb["next_index"],
            "history": [
                {"role": "user", "parts": [{"text": user_text}]},
                {"role": "model", "parts": [{"text": f"We walked through internal KB *{kb['title']}* (summary):\n\n{clip}"}]},
            ],
        },
    )


def _need_more_help_flow(
    dm_channel, user_id, query, start_index=0, thread_ts=None, max_extra=2, action_id="need_more_help"
):
    """
    In DM context:
    - action_id need_more_help: one click -> AI engineer chat (discovery / dig into the issue); no extra KB round.
    - action_id show_another_article: legacy — KB search then AI if fewer than max_extra articles found.
    """
    if not dm_channel:
        return

    if not thread_ts:
        thread_ts = slack_post_message(
            dm_channel,
            f"🤝 *Need More Help* received.\n\nI’ll check up to *{max_extra}* additional related KB articles, "
            "then continue with AI troubleshooting if needed.",
        )
        if not thread_ts:
            return

    # Ensure a conversation shell exists for continuity.
    if thread_ts not in active_threads:
        _bind_dm_thread(
            dm_channel,
            thread_ts,
            {
                "dm_channel": dm_channel,
                "user_id": user_id,
                "query": query,
                "next_index": start_index,
                "history": [{"role": "user", "parts": [{"text": query}]}],
            },
        )

    conv = active_threads[thread_ts]
    conv["query"] = query or conv.get("query") or ""

    if action_id == "show_another_article":
        index = int(start_index if start_index is not None else conv.get("next_index", 0))
        shown = 0
        spinner_stop = start_live_spinner(dm_channel, "Finding more related KB articles…", thread_ts=thread_ts)
        try:
            while shown < max_extra:
                kb = search_confluence(conv["query"], index)
                if not kb:
                    break
                src = kb.get("summary_source") or kb["text"]
                kb_summary = summarize_kb(kb["title"], src, conv["query"])
                blocks = _kb_blocks_with_actions(kb, kb_summary, conv["query"])
                slack_post_message(dm_channel, text=f"KB: {kb['title']}", blocks=blocks, thread_ts=thread_ts)
                clip = kb_summary[:3200]
                conv["history"].append({"role": "user", "parts": [{"text": "Need more help"}]})
                conv["history"].append(
                    {"role": "model", "parts": [{"text": f"Additional KB: *{kb['title']}* (summary):\n\n{clip}"}]}
                )
                index = kb["next_index"]
                shown += 1
        finally:
            spinner_stop()

        conv["next_index"] = index

        if shown < max_extra:
            ai_spinner_stop = start_live_spinner(
                dm_channel,
                "Not enough reliable KB matches. Switching to AI troubleshooting…",
                thread_ts=thread_ts,
            )
            try:
                conv["history"].append({"role": "user", "parts": [{"text": "Need more help and KB exhausted"}]})
                ai_response = ask_ai_with_history(conv["history"], session_query=conv.get("query"))
                conv["history"].append({"role": "model", "parts": [{"text": ai_response}]})
                slack_post_message(dm_channel, slack_format(ai_response), thread_ts=thread_ts)
            except Exception as exc:
                print(f"[DEBUG] _need_more_help_flow AI fallback ERROR: {exc}")
                slack_post_message(dm_channel, _api_error_user_message(exc), thread_ts=thread_ts)
            finally:
                ai_spinner_stop()
        return

    ai_spinner_stop = start_live_spinner(
        dm_channel,
        "Switching to IT support chat to understand your issue…",
        thread_ts=thread_ts,
    )
    try:
        conv["history"].append({"role": "user", "parts": [{"text": _NMH_AI_DISCOVERY_HINT}]})
        ai_response = ask_ai_with_history(conv["history"], session_query=conv.get("query"))
        conv["history"].append({"role": "model", "parts": [{"text": ai_response}]})
        slack_post_message(dm_channel, slack_format(ai_response), thread_ts=thread_ts)
    except Exception as exc:
        print(f"[DEBUG] _need_more_help_flow AI fallback ERROR: {exc}")
        slack_post_message(dm_channel, _api_error_user_message(exc), thread_ts=thread_ts)
    finally:
        ai_spinner_stop()


def _api_error_user_message(exc):
    s = str(exc).lower()
    if "read timed out" in s or "timed out" in s:
        return (
            "⚠️ The AI service is taking too long to respond right now. "
            "Please try again in a few seconds. If this is urgent, type `ticket` to escalate to IT."
        )
    if "429" in s or "resource exhausted" in s or "quota" in s or "rate" in s:
        return (
            "⚠️ Our AI backend is *rate-limited* right now (too many requests). "
            "Please wait a few minutes and try again, or type `ticket` / use `/it jira` if this is urgent."
        )
    return (
        "❌ I couldn’t finish that just now. Please try again in a moment, "
        "or type `ticket` so IT can pick it up with full tools and access."
    )

# ---------- JIRA ----------
def create_jira_ticket(summary, slack_user, description=None):
    payload = {
        "serviceDeskId": SERVICE_DESK_ID,
        "requestTypeId": REQUEST_TYPE_ID,
        "requestFieldValues": {
            "summary": summary,
            "description": description or f"Created from Slack IT Help\n\nSlack User: {slack_user}",
            CUSTOMER_NAME_FIELD: slack_user
        }
    }
    r = requests.post(
        f"{JIRA_BASE}/rest/servicedeskapi/request",
        json=payload, auth=AUTH,
        headers={"Accept": "application/json"},
        timeout=10
    )
    r.raise_for_status()
    return f"✅ Ticket created: {r.json()['issueKey']}"

# ---------- ASYNC HELP FLOW ----------
def async_help(query, response_url, user_id):
    try:
        kb = search_confluence(query, 0)

        if kb:
            src = kb.get("summary_source") or kb["text"]
            kb_summary = summarize_kb(kb["title"], src, query)
            payload = {
                "response_type": "ephemeral",
                "blocks": _kb_blocks_with_actions(kb, kb_summary, query),
            }
            requests.post(response_url, json=payload, timeout=5)

        else:
            requests.post(response_url, json={
                "response_type": "ephemeral",
                "text": (
                    "📚 No matching KB article found.\n"
                    "🤖 I've started a *private AI chat* with you.\n\n"
                    "👉 *Check your DMs with IT Help Bot* to continue troubleshooting."
                )
            }, timeout=10)

            start_ai_dm_thread(query, user_id)

    except Exception as e:
        requests.post(response_url, json={
            "response_type": "ephemeral",
            "text": f"❌ Error: {str(e)}"
        }, timeout=5)

# ---------- SLASH COMMAND ----------
def handler(req):
    if not verify_slack(req):
        return Response("Forbidden", status=403)

    text = req.form.get("text", "").strip()
    slack_user = req.form.get("user_name", "Unknown User")
    response_url = req.form.get("response_url")
    user_id = req.form.get("user_id")

    if text.lower().startswith("help"):
        query = text[4:].strip()
        if not query:
            return Response("Usage: `/it help <issue>`", status=200)

        threading.Thread(
            target=async_help,
            args=(query, response_url, user_id),
            daemon=True
        ).start()

        return Response(
            "🔍 Searching knowledge base…\n"
            "🤖 This may take a few seconds while I check our knowledge base.",
            status=200
        )

    if text.lower().startswith("jira"):
        query = text[4:].strip()
        if not query:
            return Response("Usage: `/it jira <issue>`", status=200)
        return Response(create_jira_ticket(query, slack_user), status=200)

    return Response("Usage:\n• `/it help <issue>`\n• `/it jira <issue>`", status=200)

# ---------- BUTTON ----------
def handle_interactive(req):
    if not verify_slack(req):
        return {"response_type": "ephemeral", "text": "Forbidden"}
    payload = json.loads(req.form["payload"])
    threading.Thread(target=process_interaction, args=(payload,), daemon=True).start()
    return {}

def process_interaction(payload):
    try:
        action = payload["actions"][0]
        action_id = action["action_id"]
        response_url = payload.get("response_url")
        in_dm = _interaction_is_dm_channel(payload)
        ch = (payload.get("channel") or {}).get("id")
        thread_ts = _interaction_thread_ts(payload)
        user_obj = payload.get("user") or {}
        user_id = user_obj.get("id")

        if action_id == "create_jira_ticket":
            query = action["value"]
            slack_user = user_obj.get("username", "Unknown User")
            result = create_jira_ticket(query, slack_user)
            if in_dm and ch and thread_ts:
                slack_post_message(ch, result, thread_ts=thread_ts)
            elif response_url:
                requests.post(response_url, json={"response_type": "ephemeral", "text": result}, timeout=5)
            return

        if action_id in ("show_another_article", "need_more_help"):
            data = json.loads(action["value"])
            query = data["query"]
            index = data["index"]

            loading = {
                "response_type": "ephemeral",
                "replace_original": True,
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "🔍 _Searching for another matching article…_"}}],
            }

            if in_dm and ch and thread_ts:
                _need_more_help_flow(
                    dm_channel=ch,
                    user_id=user_id,
                    query=query,
                    start_index=index,
                    thread_ts=thread_ts,
                    max_extra=2,
                    action_id=action_id,
                )
            else:
                # If this came from /it help in a channel, move directly to AI DM thread first.
                if response_url:
                    requests.post(
                        response_url,
                        json={
                            "response_type": "ephemeral",
                            "replace_original": False,
                            "text": "🤝 *Need More Help* selected. I’ve opened a private AI troubleshooting DM for you.",
                        },
                        timeout=5,
                    )
                if user_id:
                    start_ai_dm_thread(query, user_id)
                elif response_url:
                    requests.post(response_url, json=loading, timeout=5)

    except Exception as e:
        print("Interactive error:", e)
        msg = _api_error_user_message(e)
        try:
            ch = (payload.get("channel") or {}).get("id")
            thread_ts = _interaction_thread_ts(payload)
            response_url = payload.get("response_url")
            in_dm = _interaction_is_dm_channel(payload)
            if in_dm and ch and thread_ts:
                slack_post_message(ch, msg, thread_ts=thread_ts)
            elif response_url:
                requests.post(
                    response_url,
                    json={"response_type": "ephemeral", "replace_original": False, "text": msg},
                    timeout=5,
                )
        except Exception as notify_exc:
            print(f"[DEBUG] Interactive error user notify failed: {notify_exc}")

# ---------- EVENTS ----------
def handle_events(req):
    if not verify_slack(req):
        return Response("Forbidden", status=403)
    data = req.get_json(silent=True)
    if not data:
        return Response("", status=200)

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    if data.get("type") == "event_callback":
        event = data.get("event", {})
        print(f"[DEBUG] Event received: type={event.get('type')}, channel_type={event.get('channel_type')}, bot_id={event.get('bot_id')}, subtype={event.get('subtype')}, thread_ts={event.get('thread_ts')}, text={event.get('text', '')[:50]}")

        if event.get("type") == "app_home_opened":
            user_id = event.get("user")
            if user_id:
                threading.Thread(target=slack_publish_home, args=(user_id,), daemon=True).start()
            return Response("", status=200)

        if event.get("type") == "app_mention" and not event.get("bot_id"):
            threading.Thread(target=_handle_app_mention, args=(event,), daemon=True).start()
            return Response("", status=200)

        if event.get("type") == "message" and not event.get("bot_id") and event.get("subtype") is None:
            thread_ts_event = event.get("thread_ts")
            channel_type = event.get("channel_type")
            user_id_ev = event.get("user")
            dm_channel_ev = event.get("channel")
            text_ev = event.get("text", "") or ""

            # Fallback: if app_mention event isn't configured, still handle bot mention in channel messages.
            if channel_type != "im":
                bot_uid = get_bot_user_id()
                if bot_uid and f"<@{bot_uid}>" in text_ev:
                    threading.Thread(target=_handle_app_mention, args=(event,), daemon=True).start()
                    return Response("", status=200)

            if thread_ts_event and thread_ts_event in active_threads:
                threading.Thread(target=_handle_dm_reply, args=(event, thread_ts_event), daemon=True).start()

            elif channel_type == "im" and user_id_ev and dm_channel_ev:
                active_ts = _active_thread_for_dm(dm_channel_ev, user_id_ev)
                # User wrote in the DM *main* pane (or in some other thread): keep one coherent session
                if active_ts and (not thread_ts_event or thread_ts_event != active_ts):
                    ev = dict(event)
                    ev["thread_ts"] = active_ts
                    threading.Thread(target=_handle_dm_reply, args=(ev, active_ts), daemon=True).start()
                else:
                    threading.Thread(target=_handle_sidebar_message, args=(event,), daemon=True).start()

    return Response("", status=200)

def _handle_dm_reply(event, thread_ts):
    try:
        conversation = active_threads[thread_ts]
        dm_channel = conversation["dm_channel"]
        user_text = _strip_slack_mentions(event.get("text", "").strip())
        user_id = event.get("user")

        if user_text.lower() == "done":
            slack_post_message(dm_channel, "✅ *Chat ended.* If you need help again, use `/it help <issue>` anytime!", thread_ts=thread_ts)
            _unbind_active_thread(thread_ts)
            return

        if user_text.lower() == "ticket":
            summary = _ticket_summary_from_history(conversation["history"])
            desc = _ticket_description_transcript(conversation["history"], f"<@{user_id}>")
            result = create_jira_ticket(summary, f"<@{user_id}>", description=desc)
            slack_post_message(dm_channel, result, thread_ts=thread_ts)
            slack_post_message(dm_channel, "Chat ended. The IT team will follow up on your ticket.", thread_ts=thread_ts)
            _unbind_active_thread(thread_ts)
            return

        if is_feedback_text(user_text):
            snippets = []
            for msg in conversation["history"][-10:]:
                role = "User" if msg["role"] == "user" else "IT"
                snippets.append(f"{role}: {msg['parts'][0]['text'][:400]}")
            ai_response = engineer_feedback_reply(user_text, snippets)
            conversation["history"].append({"role": "user", "parts": [{"text": user_text}]})
            conversation["history"].append({"role": "model", "parts": [{"text": ai_response}]})
            slack_post_message(dm_channel, slack_format(ai_response), thread_ts=thread_ts)
            return

        stop_spinner = start_live_spinner(dm_channel, "Analyzing your message and preparing next checks…", thread_ts=thread_ts)
        try:
            conversation["history"].append({"role": "user", "parts": [{"text": user_text}]})
            ai_response = ask_ai_with_history(
                conversation["history"],
                session_query=conversation.get("query"),
            )
            ai_slack = slack_format(ai_response)
            conversation["history"].append({"role": "model", "parts": [{"text": ai_response}]})
        finally:
            stop_spinner()

        slack_post_message(dm_channel, ai_slack, thread_ts=thread_ts)

    except Exception as exc:
        print(f"[DEBUG] _handle_dm_reply ERROR: {exc}")
        dm_channel = active_threads.get(thread_ts, {}).get("dm_channel")
        if dm_channel:
            slack_post_message(
                dm_channel,
                _api_error_user_message(exc),
                thread_ts=thread_ts,
            )

def _strip_slack_mentions(text):
    """Remove Slack <@U…> tokens so @-invokes don’t confuse intent or search."""
    if not text:
        return ""
    return re.sub(r"<@[^>]+>\s*", "", text).strip()


def _handle_app_mention(event):
    """@IT Help in a channel → ephemeral note + same flow as sidebar DM."""
    try:
        user_id = event.get("user")
        channel = event.get("channel")
        mention_ts = event.get("ts")
        if not user_id:
            return

        cleaned = _strip_slack_mentions(event.get("text", ""))

        if channel and not str(channel).startswith("D"):
            # Public acknowledgement so the user sees immediate response in-channel.
            slack_post_message(
                channel,
                f"👋 <@{user_id}> I’m on it — moving this to DM for private troubleshooting.",
                thread_ts=mention_ts,
            )
            slack_post_ephemeral(
                channel,
                user_id,
                "👋 *IT Help* — I’ve moved this to our *private DM* so nothing sensitive sits in the channel. "
                "Check your DM with this app in a moment.",
            )
            dm_channel = open_dm(user_id)
            if not dm_channel:
                slack_post_message(
                    channel,
                    f"⚠️ <@{user_id}> I couldn’t open DM right now. Please message me directly in *Messages* tab.",
                    thread_ts=mention_ts,
                )
                return
        else:
            dm_channel = channel

        if not dm_channel:
            return

        synthetic = {
            "channel": dm_channel,
            "user": user_id,
            "text": cleaned if cleaned else "hi",
            "thread_ts": None,
        }
        _handle_sidebar_message(synthetic)
    except Exception as e:
        print(f"[DEBUG] _handle_app_mention ERROR: {e}")


# Top-level DM “memory” for short social replies (threaded KB+AI uses active_threads)
DM_CONVERSATIONS = {}

_SIDEBAR_ENGINEER_PROMPT = (
    "You are a *senior internal IT support engineer* chatting in Slack DM with an employee.\n\n"
    "STYLE:\n"
    "- Sound like a real engineer: clear, direct, respectful — not a generic chatbot.\n"
    "- Prefer *structured* answers: short opening → numbered checks → what info you need next.\n"
    "- Ask sharp clarifying questions when the report is vague (*OS*, exact symptom, error text, VPN on/off, wired vs Wi‑Fi).\n"
    "- If they only said hello/thanks, keep it brief and route them toward describing a work-tech problem.\n\n"
    "SECURITY:\n"
    "- No admin/registry/terminal/PowerShell/sudo/BIOS/policy-bypass steps.\n"
    "- Never tell them to disable antivirus, firewall, MDM, or company security tooling.\n"
    "- If it needs elevated access: tell them to type `ticket` for Jira / IT.\n\n"
    "FORMAT: Slack mrkdwn — *bold* only; numbered lists OK; no ## or **; no tables."
)


def _handle_sidebar_message(event):
    try:
        user_id = event.get("user")
        user_text = _strip_slack_mentions(event.get("text", "").strip())
        dm_channel = event.get("channel")

        if not user_text or not user_id:
            return

        intent = classify_sidebar_message(user_text)

        if intent == "TICKET_CMD":
            slack_post_message(
                dm_channel,
                "I don’t have an *open* troubleshooting thread here yet. "
                "Describe the issue in one message (or reply inside the thread under the last article), "
                "then type `ticket` again. You can also use `/it jira <short summary>` anytime.",
            )
            return

        if intent == "DONE_CMD":
            slack_post_message(
                dm_channel,
                "There’s no active session to close. Send *hi* or describe an issue whenever you need IT.",
            )
            return
        print(f"[DEBUG] Sidebar intent: {intent} text={user_text[:80]!r}")

        if intent == "GREETING":
            reply = engineer_smalltalk_reply(user_text)
            slack_post_message(dm_channel, slack_format(reply))
            return

        if intent == "FEEDBACK":
            if dm_channel not in DM_CONVERSATIONS:
                DM_CONVERSATIONS[dm_channel] = []
            snippets = []
            for m in DM_CONVERSATIONS[dm_channel][-8:]:
                role = "User" if m["role"] == "user" else "IT"
                snippets.append(f"{role}: {m['parts'][0]['text'][:400]}")
            reply = engineer_feedback_reply(user_text, snippets)
            slack_post_message(dm_channel, slack_format(reply))
            DM_CONVERSATIONS[dm_channel].append({"role": "user", "parts": [{"text": user_text}]})
            DM_CONVERSATIONS[dm_channel].append({"role": "model", "parts": [{"text": reply}]})
            return

        if intent == "IT_ISSUE":
            DM_CONVERSATIONS[dm_channel] = []
            _discard_dm_session_for_channel(dm_channel)
            stop_spinner = start_live_spinner(dm_channel, "Searching KB and preparing the best answer…")
            try:
                kb = search_confluence(user_text, 0)
                if kb:
                    _post_kb_thread_and_register(dm_channel, user_id, user_text, kb, user_text)
                else:
                    slack_post_message(
                        dm_channel,
                        "_No reliable KB match found — I’m opening a focused AI troubleshooting thread now._",
                    )
                    start_ai_dm_thread(user_text, user_id)
            finally:
                stop_spinner()
            return

        if intent == "UNCLEAR":
            slack_post_message(
                dm_channel,
                "I didn’t quite catch that — what’s going wrong with your *work tech* (one sentence is fine)?",
            )
            return

        # Fallback: engineer chat with light history
        if dm_channel not in DM_CONVERSATIONS:
            DM_CONVERSATIONS[dm_channel] = []
        history = DM_CONVERSATIONS[dm_channel]
        history.append({"role": "user", "parts": [{"text": user_text}]})

        trimmed = trim_history(history)
        if AI_KB_GROUNDING_ENABLED:
            use_kb, kb_body = _retrieve_kb_grounding_payload(user_text)
            if use_kb:
                sys_prompt = (
                    _SIDEBAR_ENGINEER_PROMPT
                    + CHAT_KB_GROUNDING_APPEND
                    + "\n\n"
                    + _format_kb_excerpts_positive_block(kb_body)
                )
            else:
                sys_prompt = _SIDEBAR_ENGINEER_PROMPT + CHAT_GENERAL_NO_KB_APPEND
        else:
            sys_prompt = _SIDEBAR_ENGINEER_PROMPT
        contents = [
            {"role": "user", "parts": [{"text": sys_prompt}]},
            {"role": "model", "parts": [{"text": "Understood — triaging like a service desk engineer."}]},
        ]
        contents.extend(trimmed)

        ai_reply = gemini_generate(
            contents=contents,
            generation_config={"temperature": 0.3, "maxOutputTokens": 1600},
            timeout=(10, 60),
            retries=2,
        )
        ai_reply = enforce_security_policy(ai_reply)
        ai_slack = slack_format(ai_reply)
        history.append({"role": "model", "parts": [{"text": ai_reply}]})
        slack_post_message(dm_channel, ai_slack)

    except Exception as exc:
        print(f"[DEBUG] _handle_sidebar_message ERROR: {exc}")
        dm_channel = event.get("channel")
        if dm_channel:
            slack_post_message(dm_channel, _api_error_user_message(exc))
