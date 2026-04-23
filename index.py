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
URGENT_ESCALATION_CHANNEL = os.environ.get("URGENT_ESCALATION_CHANNEL", "C0ARXM5MTUM")  # Private channel for urgent IT escalation

# ---------- CONVERSATION STORE ----------
active_threads = {}  # thread_ts → {channel, user_id, query, next_index, history}
# (channel, user_id) → thread_ts, to route channel-main replies back to their active thread
user_active_thread = {}

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

# AI chat (DM / engineer) must only use Confluence excerpts when true — no generic "best guess" procedures.
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
    if data:
        key = (data.get("channel"), data.get("user_id"))
        if user_active_thread.get(key) == thread_ts:
            user_active_thread.pop(key, None)


def _bind_thread(thread_ts, payload):
    active_threads[thread_ts] = payload
    ch = payload.get("channel")
    uid = payload.get("user_id")
    if ch and uid:
        user_active_thread[(ch, uid)] = thread_ts


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

# For "can't connect / not working" style queries, require one of these *by name in the page title*
# so hub pages that only mention the product in passing (body) don't win over real how-tos.
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
        "--- INTERNAL KB EXCERPTS (authoritative source for procedures & company-specific facts). "
        "IMPORTANT: the [1], [2], [3] indices below are for YOUR internal reference only — "
        "NEVER copy them into your reply. Do NOT write 'KB[1]', '[4]', '(source 2)', or any citation "
        "token in the user-facing response. Refer to articles by title in plain prose if needed. ---\n"
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
        "- End with ONE *specific* clarifying question that narrows down the fix "
        "(e.g., 'What OS are you on — Mac or Windows?', 'Does this happen on Wi-Fi or wired?', "
        "'Which browser — Chrome, Safari, or Edge?', 'What's the exact error text?'). "
        "Never generic like 'any other info?' — must be specific to their issue.\n"
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
            "⚠️ Most of this guidance involves *administrator-only* steps we can't paste in chat.\n\n"
            "Create a Jira *ticket* so IT can run the privileged parts safely."
        )
    return result


_CITATION_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"KB\s*\[\d+\]|"                    # KB[4], KB [5]
    r"\[\s*KB\s*\d+\s*\]|"              # [KB 4], [KB4]
    r"\[\s*\d{1,2}\s*\]|"               # [4], [5] (standalone index)
    r"\(\s*KB\s*\d+\s*\)|"              # (KB 1), (KB1)
    r"\(\s*source\s*[:：]?\s*\d+\s*\)|" # (source: 1)
    r"\bsource\s*\[\d+\]|"              # source[1]
    r"\bref\s*\[\d+\]"                  # ref[1]
    r")",
    re.IGNORECASE,
)


def strip_citation_tokens(text):
    """Remove AI-generated citation markers like KB[1], [4], (source: 2), etc."""
    if not text:
        return text
    cleaned = _CITATION_TOKEN_RE.sub("", text)
    # Collapse double spaces left behind
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    return cleaned


def enforce_security_policy(ai_text):
    """Prefer redaction of risky lines; also strip citation tokens. Only hard-block when almost nothing safe remains."""
    return strip_citation_tokens(redact_sensitive_instructions(ai_text))

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
    "treat that as ground truth for follow-ups; don't pretend it never happened.\n"
    "9. ALWAYS end your reply with ONE *specific* clarifying question that helps narrow down the fix "
    "(e.g., 'What OS are you on — Mac or Windows?', 'Does this happen on Wi-Fi, wired, or both?', "
    "'Which browser — Chrome, Safari, or Edge?', 'What's the exact error text you see?'). "
    "The question must be *specific* to their issue — never generic like 'any other info?'. "
    "Skip this ONLY if the user has confirmed the issue is resolved."
)

CHAT_KB_GROUNDING_APPEND = (
    "\n\nKB-ONLY GROUNDING (mandatory when this block appears):\n"
    "- The *INTERNAL KB EXCERPTS* section in this message is the authoritative source for *specific* "
    "procedures, company tool names as documented, and policy facts.\n"
    "- Base numbered troubleshooting *only* on those excerpts. Do *not* add steps from general internet "
    "knowledge or guesswork.\n"
    "- If excerpts clearly cover the issue, synthesize the guidance *naturally* without citation markers.\n"
    "- *NEVER* write citation tokens like `KB[1]`, `KB[4]`, `[1]`, `[2]`, `[3]`, `[source:...]`, `(1)`, etc. "
    "in your reply. Write prose only — no reference/citation brackets of any kind.\n"
    "- If you need to refer to the source, mention the article *title* in plain prose (e.g. _'per the VPN setup guide'_) — "
    "do *not* use numbered references.\n"
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
    "org's policies and tools may differ._*\n"
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
def slack_post_message(channel, text=None, thread_ts=None, blocks=None, attachments=None):
    payload = {"channel": channel}
    if text is not None:
        payload["text"] = text
    if blocks is not None:
        payload["blocks"] = blocks
    if attachments is not None:
        payload["attachments"] = attachments
    if thread_ts is not None:
        payload["thread_ts"] = thread_ts
    if not payload.get("text") and not payload.get("blocks") and not payload.get("attachments"):
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


# Cache Slack user_id → display name to avoid hitting users.info repeatedly
_slack_user_name_cache = {}


def get_slack_user_name(user_id):
    """
    Resolve Slack user_id to a readable display name (real name or display name).
    Falls back to the raw user_id if lookup fails.
    """
    if not user_id:
        return "Unknown User"
    if user_id in _slack_user_name_cache:
        return _slack_user_name_cache[user_id]
    try:
        r = requests.get(
            "https://slack.com/api/users.info",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params={"user": user_id},
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            user = data.get("user", {}) or {}
            profile = user.get("profile", {}) or {}
            name = (
                profile.get("real_name_normalized")
                or profile.get("real_name")
                or profile.get("display_name_normalized")
                or profile.get("display_name")
                or user.get("real_name")
                or user.get("name")
                or user_id
            )
            _slack_user_name_cache[user_id] = name
            return name
        print(f"[DEBUG] get_slack_user_name FAILED: {data.get('error')}")
    except Exception as e:
        print(f"[DEBUG] get_slack_user_name ERROR: {e}")
    return user_id


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


def start_ai_thread(channel, user_id, query, thread_ts):
    """Start AI troubleshooting in an existing channel thread (no DM)."""
    history = [{"role": "user", "parts": [{"text": query}]}]
    ai_spinner = start_live_spinner(channel, "Analyzing your issue and preparing first steps…", thread_ts=thread_ts)
    try:
        ai_response = ask_ai_with_history(history, session_query=query)
        ai_slack = slack_format(ai_response)
        history.append({"role": "model", "parts": [{"text": ai_response}]})
    finally:
        ai_spinner()

    _bind_thread(thread_ts, {
        "channel": channel,
        "user_id": user_id,
        "query": query,
        "next_index": 0,
        "history": history,
    })

    slack_post_message(channel, ai_slack, thread_ts=thread_ts)
    _post_ai_followup(channel, thread_ts, query, history)

# ---------- KB SUMMARIZATION ----------
def summarize_kb(title, raw_text, user_query):
    cache_key = _make_kb_summary_cache_key(title, raw_text, user_query)
    cached = _get_cached_kb_summary(cache_key)
    if cached:
        print(f"[DEBUG] KB summary cache hit: title={title[:80]!r}")
        return cached

    prompt = (
        "You are a *senior IT support engineer* rewriting an internal KB article for Slack.\n\n"
        "A Confluence article matched the employee's request. Rewrite it as a *clear, accurate runbook* — "
        "professional, precise, and easy to follow for a non‑technical reader.\n\n"
        "WRITING RULES:\n"
        "- Open with one line tying the article to *their* question (what this doc helps them do).\n"
        "- Use numbered steps (1. 2. 3.). Each step must say *where* to go, *what* they see, *what* to do.\n"
        "- Bold buttons, menu names, and field labels with *bold* (single asterisk — Slack format).\n"
        "- If Windows vs Mac differs, label sections *Windows* / *Mac*.\n"
        "- Call out common failure points (*wrong account, cached credentials, VPN state*) when the source implies it.\n"
        "- If the source is vague, say what is *known from the article* and what *needs IT* — don't invent policy.\n"
        "- Skip TOC, metadata, author noise, and unrelated sections.\n"
        "- Do NOT use markdown headings (# or ##) or **double bold**.\n"
        "- Do NOT use tables.\n"
        "- If the article clearly does *not* address the user's issue, say so in one honest sentence "
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
    """Prefer paragraph/sentence boundary so Slack blocks don't end on 'limi…'."""
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
    """Block Kit for a KB card (article content only — follow-up CTA posted separately with color)."""
    summary_blocks = _summary_to_blocks(kb_summary)
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"📘 {kb['title'][:130]}", "emoji": True}},
        *summary_blocks,
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"🔗 <{kb['url']}|Open full article with images>"}},
    ]


def _kb_followup_attachment(query, next_index):
    """Colored attachment that grabs attention after a KB card — nudges user to reply + Need More Help button."""
    return [{
        "color": "#FFB347",  # warm orange accent
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "🔔  *Didn't fully solve your issue?*\n"
                        "👉 *Reply right here* with more detail — *exact error, what you tried, OS/device, when it started* — "
                        "and I'll tailor the fix to *your* situation.\n\n"
                        "_Or tap *Need More Help* below for general AI troubleshooting._"
                    ),
                },
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
                ],
            },
        ],
    }]


def _post_ai_followup(channel, thread_ts, query, history):
    """After every AI reply, show satisfaction buttons (with a 'before you click' nudge) in a colored attachment."""
    slack_post_message(
        channel,
        text="Was this helpful?",
        attachments=_satisfaction_attachment(query=query),
        thread_ts=thread_ts,
    )


def _satisfaction_attachment(query):
    """Colored attachment grouping the 'before you click' nudge + Satisfied/Not Satisfied buttons."""
    return [{
        "color": "#F2C744",  # bright yellow — attention-grabbing
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "⚡ *Before you decide…*\n"
                        "Have you actually *tried the steps above*? If you hit any confusion or see a "
                        "new error, *just reply here* and I'll adjust the fix to your exact situation."
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "satisfied",
                        "text": {"type": "plain_text", "text": "✅ Helpful (end the chat)"},
                        "style": "primary",
                        "value": json.dumps({"query": query}),
                    },
                    {
                        "type": "button",
                        "action_id": "not_satisfied",
                        "text": {"type": "plain_text", "text": "❌ Not Helpful (For Ticket and IT Engineer Support)"},
                        "style": "danger",
                        "value": json.dumps({"query": query}),
                    },
                ],
            },
        ],
    }]


def _not_satisfied_blocks(query):
    """Create Jira Ticket + Get Help Now (Urgent) buttons shown after Not Satisfied."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "Sorry this didn't resolve your issue. Choose an option below:",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "create_jira_ticket",
                    "text": {"type": "plain_text", "text": "📝 Create Jira Ticket"},
                    "style": "primary",
                    "value": query,
                },
                {
                    "type": "button",
                    "action_id": "get_help_urgent",
                    "text": {"type": "plain_text", "text": "🚨 Get Help Now (Urgent)"},
                    "style": "danger",
                    "value": json.dumps({"query": query}),
                },
            ],
        },
    ]


def _handle_urgent_escalation(channel, thread_ts, user_id, query):
    """Cross-post the thread to the urgent escalation channel, create a Jira ticket, and notify the user."""
    if not URGENT_ESCALATION_CHANNEL:
        slack_post_message(
            channel,
            "⚠️ Urgent escalation channel is not configured. Please contact IT directly.",
            thread_ts=thread_ts,
        )
        return

    # Create Jira ticket with conversation history
    conv = active_threads.get(thread_ts)
    user_name = get_slack_user_name(user_id)
    if conv and conv.get("history"):
        summary = _ticket_summary_from_history(conv["history"])
        desc = _ticket_description_transcript(conv["history"], user_name)
    else:
        summary = (query or "Urgent IT Help request from Slack")[:240]
        desc = f"Created from Slack IT Help (Urgent)\n\nSlack User: {user_name}\n\nIssue: {query}"
    try:
        ticket_result = create_jira_ticket(summary, user_name, description=desc)
    except Exception as exc:
        print(f"[DEBUG] Urgent escalation Jira ticket failed: {exc}")
        ticket_result = "⚠️ Could not create Jira ticket automatically — please create one manually."

    # Build thread link
    thread_link = f"https://slack.com/archives/{channel}/p{thread_ts.replace('.', '')}"

    # Build history summary for escalation message
    history_summary = ""
    if conv and conv.get("history"):
        lines = []
        for m in conv["history"][-6:]:
            role = "Employee" if m.get("role") == "user" else "IT Bot"
            lines.append(f"*{role}:* {m['parts'][0]['text'][:300]}")
        history_summary = "\n".join(lines)

    escalation_text = (
        f"🚨 *URGENT IT Help Request* from *{user_name}* (<@{user_id}>)\n\n"
        f"*Issue:* {query[:500]}\n\n"
    )
    if history_summary:
        escalation_text += f"*Thread Summary:*\n{history_summary}\n\n"
    escalation_text += f"{ticket_result}\n\n"
    escalation_text += f"🔗 <{thread_link}|View full thread>"

    slack_post_message(URGENT_ESCALATION_CHANNEL, escalation_text)

    slack_post_message(
        channel,
        f"🚨 *Urgent help requested!* Your issue has been escalated to the IT team.\n"
        f"{ticket_result}\n"
        "An engineer will respond shortly.",
        thread_ts=thread_ts,
    )
    _unbind_active_thread(thread_ts)


def _interaction_thread_ts(payload):
    msg = payload.get("message") or {}
    return msg.get("thread_ts") or msg.get("ts")


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

# Common typo: user omits "not" ("no, this is what i am looking for" meaning the opposite)
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


def engineer_feedback_reply(user_text, prior_snippets):
    ctx = "\n".join(prior_snippets[-6:]) if prior_snippets else "(no prior thread context)"
    augmented = (
        "Context — recent DM lines:\n"
        f"{ctx}\n\n"
        "The user indicates the previous answer, article, or direction wasn't what they needed.\n\n"
        f"Their latest message: {user_text}\n\n"
        "Respond as a *senior IT engineer*:\n"
        "- Brief apology, no drama.\n"
        "- Acknowledge you may have missed their goal.\n"
        "- Ask *specific* clarifiers (what they expected, system/OS, exact error text, app name, when it started).\n"
        "- Do *not* paste a generic KB wall or repeat the last article unless they ask.\n"
        "- Mention they can use *Need More Help* on the last KB card if one was shown.\n"
        "- Offer `ticket` if they're blocked or it needs admin access.\n\n"
        "FORMAT: Slack — numbered list optional; *bold* for emphasis; no ## or **; no tables; keep under ~1200 chars."
    )
    try:
        return enforce_security_policy(ask_ai(augmented, retrieval_query=user_text))
    except Exception:
        return (
            "Sorry that wasn't the right fix. Tell me in one sentence *what outcome you need*, "
            "your *OS* if relevant, and any *exact error text* — I'll narrow this down. "
            "If you're blocked, type `ticket` to log it for IT."
        )


def _post_kb_in_thread(channel, user_id, query, kb, thread_ts):
    """Post KB article in the existing channel thread, then a colored follow-up CTA."""
    src = kb.get("summary_source") or kb["text"]
    kb_summary = summarize_kb(kb["title"], src, query)
    blocks = _kb_blocks_with_actions(kb, kb_summary, query)

    slack_post_message(channel, text=f"KB: {kb['title']}", blocks=blocks, thread_ts=thread_ts)
    # Highlighted attention-grabbing follow-up CTA
    slack_post_message(
        channel,
        text="Reply for a tailored fix",
        attachments=_kb_followup_attachment(query, kb["next_index"]),
        thread_ts=thread_ts,
    )
    clip = kb_summary[:3500]
    _bind_thread(thread_ts, {
        "channel": channel,
        "user_id": user_id,
        "query": query,
        "next_index": kb["next_index"],
        "history": [
            {"role": "user", "parts": [{"text": query}]},
            {"role": "model", "parts": [{"text": f"We walked through internal KB *{kb['title']}* (summary):\n\n{clip}"}]},
        ],
    })


def _need_more_help_flow(channel, user_id, query, start_index=0, thread_ts=None, action_id="need_more_help"):
    """
    Need More Help in channel thread:
    - need_more_help: AI engineer chat (discovery / dig into the issue) + satisfaction buttons.
    - show_another_article: KB search then AI if no more articles found.
    """
    if not channel or not thread_ts:
        return

    # Ensure a conversation shell exists for continuity.
    if thread_ts not in active_threads:
        _bind_thread(thread_ts, {
            "channel": channel,
            "user_id": user_id,
            "query": query,
            "next_index": start_index,
            "history": [{"role": "user", "parts": [{"text": query}]}],
        })

    conv = active_threads[thread_ts]
    conv["query"] = query or conv.get("query") or ""

    if action_id == "show_another_article":
        index = int(start_index if start_index is not None else conv.get("next_index", 0))
        shown = 0
        max_extra = 2
        spinner_stop = start_live_spinner(channel, "Finding more related KB articles…", thread_ts=thread_ts)
        try:
            while shown < max_extra:
                kb = search_confluence(conv["query"], index)
                if not kb:
                    break
                src = kb.get("summary_source") or kb["text"]
                kb_summary = summarize_kb(kb["title"], src, conv["query"])
                blocks = _kb_blocks_with_actions(kb, kb_summary, conv["query"])
                slack_post_message(channel, text=f"KB: {kb['title']}", blocks=blocks, thread_ts=thread_ts)
                slack_post_message(
                    channel,
                    text="Reply for a tailored fix",
                    attachments=_kb_followup_attachment(conv["query"], kb["next_index"]),
                    thread_ts=thread_ts,
                )
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
            _ai_fallback_in_thread(channel, conv, thread_ts)
        return

    # need_more_help → AI discovery
    _ai_fallback_in_thread(channel, conv, thread_ts)


def _ai_fallback_in_thread(channel, conv, thread_ts):
    """Run AI troubleshooting in a channel thread and post satisfaction buttons."""
    ai_spinner_stop = start_live_spinner(
        channel,
        "Switching to AI troubleshooting…",
        thread_ts=thread_ts,
    )
    try:
        conv["history"].append({"role": "user", "parts": [{"text": _NMH_AI_DISCOVERY_HINT}]})
        ai_response = ask_ai_with_history(conv["history"], session_query=conv.get("query"))
        conv["history"].append({"role": "model", "parts": [{"text": ai_response}]})
        slack_post_message(channel, slack_format(ai_response), thread_ts=thread_ts)
        _post_ai_followup(channel, thread_ts, conv.get("query", ""), conv["history"])
    except Exception as exc:
        print(f"[DEBUG] _ai_fallback_in_thread ERROR: {exc}")
        slack_post_message(channel, _api_error_user_message(exc), thread_ts=thread_ts)
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
            "Please wait a few minutes and try again, or type `ticket` if this is urgent."
        )
    return (
        "❌ I couldn't finish that just now. Please try again in a moment, "
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
        ch = (payload.get("channel") or {}).get("id")
        thread_ts = _interaction_thread_ts(payload)
        user_obj = payload.get("user") or {}
        user_id = user_obj.get("id")

        if action_id == "create_jira_ticket":
            query = action["value"]
            user_name = get_slack_user_name(user_id) if user_id else user_obj.get("name") or user_obj.get("username") or "Unknown User"
            # Use conversation history for description if available
            conv = active_threads.get(thread_ts)
            if conv and conv.get("history"):
                summary = _ticket_summary_from_history(conv["history"])
                desc = _ticket_description_transcript(conv["history"], user_name)
                result = create_jira_ticket(summary, user_name, description=desc)
            else:
                result = create_jira_ticket(query, user_name)
            if ch and thread_ts:
                slack_post_message(ch, result, thread_ts=thread_ts)
                slack_post_message(ch, "The IT team will follow up on your ticket.", thread_ts=thread_ts)
                _unbind_active_thread(thread_ts)
            elif response_url:
                requests.post(response_url, json={"response_type": "ephemeral", "text": result}, timeout=5)
            return

        if action_id in ("show_another_article", "need_more_help"):
            data = json.loads(action["value"])
            query = data["query"]
            index = data.get("index", 0)
            if ch and thread_ts:
                _need_more_help_flow(
                    channel=ch,
                    user_id=user_id,
                    query=query,
                    start_index=index,
                    thread_ts=thread_ts,
                    action_id=action_id,
                )
            return

        if action_id == "satisfied":
            if ch and thread_ts:
                slack_post_message(
                    ch,
                    "✅ Glad that helped! If you need anything else, @mention IT Help anytime.",
                    thread_ts=thread_ts,
                )
                _unbind_active_thread(thread_ts)
            return

        if action_id == "not_satisfied":
            data = json.loads(action["value"])
            query = data.get("query", "")
            if ch and thread_ts:
                slack_post_message(
                    ch,
                    text="Escalation options",
                    blocks=_not_satisfied_blocks(query),
                    thread_ts=thread_ts,
                )
            return

        if action_id == "get_help_urgent":
            data = json.loads(action["value"])
            query = data.get("query", "")
            if ch and thread_ts:
                _handle_urgent_escalation(ch, thread_ts, user_id, query)
            return

    except Exception as e:
        print("Interactive error:", e)
        msg = _api_error_user_message(e)
        try:
            ch = (payload.get("channel") or {}).get("id")
            thread_ts = _interaction_thread_ts(payload)
            response_url = payload.get("response_url")
            if ch and thread_ts:
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

        if event.get("type") == "app_mention" and not event.get("bot_id"):
            threading.Thread(target=_handle_app_mention, args=(event,), daemon=True).start()
            return Response("", status=200)

        if event.get("type") == "message" and not event.get("bot_id") and event.get("subtype") is None:
            thread_ts_event = event.get("thread_ts")
            text_ev = event.get("text", "") or ""
            channel_ev = event.get("channel")
            user_ev = event.get("user")

            # Priority 1: replies in an active IT Help thread
            if thread_ts_event and thread_ts_event in active_threads:
                threading.Thread(target=_handle_thread_reply, args=(event, thread_ts_event), daemon=True).start()
                return Response("", status=200)

            # Priority 2: bot @mention in a channel (fallback if app_mention event not configured)
            bot_uid = get_bot_user_id()
            if bot_uid and f"<@{bot_uid}>" in text_ev:
                threading.Thread(target=_handle_app_mention, args=(event,), daemon=True).start()
                return Response("", status=200)

            # Priority 3: user typed in main channel box but has an active IT Help thread → route to it
            if not thread_ts_event and channel_ev and user_ev:
                active_ts = user_active_thread.get((channel_ev, user_ev))
                if active_ts and active_ts in active_threads:
                    ev = dict(event)
                    ev["thread_ts"] = active_ts
                    threading.Thread(target=_handle_thread_reply, args=(ev, active_ts), daemon=True).start()
                    return Response("", status=200)

    return Response("", status=200)

def _handle_thread_reply(event, thread_ts):
    """Handle user replies in a channel thread with an active IT Help session."""
    try:
        conversation = active_threads[thread_ts]
        channel = conversation["channel"]
        user_text = _strip_slack_mentions(event.get("text", "").strip())
        user_id = event.get("user")

        if not user_text:
            return

        # If we were waiting for the initial query (empty @mention)
        if conversation.get("awaiting_query"):
            if _is_trivial_greeting(user_text):
                slack_post_message(
                    channel,
                    "👋 Hi! I still need to know what's going wrong. Please describe your *work tech* issue "
                    "(e.g. _'VPN not connecting on Mac'_ or _'Can't log into Gmail'_) and I'll help.",
                    thread_ts=thread_ts,
                )
                return
            conversation["awaiting_query"] = False
            conversation["query"] = user_text
            stop_spinner = start_live_spinner(channel, "Searching KB and preparing the best answer…", thread_ts=thread_ts)
            try:
                kb = search_confluence(user_text, 0)
                if kb:
                    _post_kb_in_thread(channel, user_id, user_text, kb, thread_ts)
                else:
                    start_ai_thread(channel, user_id, user_text, thread_ts)
            finally:
                stop_spinner()
            return

        if user_text.lower() == "done":
            slack_post_message(channel, "✅ *Chat ended.* If you need help again, @mention IT Help in any channel!", thread_ts=thread_ts)
            _unbind_active_thread(thread_ts)
            return

        if user_text.lower() == "ticket":
            user_name = get_slack_user_name(user_id)
            summary = _ticket_summary_from_history(conversation["history"])
            desc = _ticket_description_transcript(conversation["history"], user_name)
            result = create_jira_ticket(summary, user_name, description=desc)
            slack_post_message(channel, result, thread_ts=thread_ts)
            slack_post_message(channel, "Chat ended. The IT team will follow up on your ticket.", thread_ts=thread_ts)
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
            slack_post_message(channel, slack_format(ai_response), thread_ts=thread_ts)
            _post_ai_followup(channel, thread_ts, conversation.get("query", ""), conversation["history"])
            return

        stop_spinner = start_live_spinner(channel, "Analyzing your message and preparing next checks…", thread_ts=thread_ts)
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

        slack_post_message(channel, ai_slack, thread_ts=thread_ts)
        _post_ai_followup(channel, thread_ts, conversation.get("query", ""), conversation["history"])

    except Exception as exc:
        print(f"[DEBUG] _handle_thread_reply ERROR: {exc}")
        ch = active_threads.get(thread_ts, {}).get("channel")
        if ch:
            slack_post_message(ch, _api_error_user_message(exc), thread_ts=thread_ts)

def _strip_slack_mentions(text):
    """Remove Slack <@U…> tokens so @-invokes don't confuse intent or search."""
    if not text:
        return ""
    return re.sub(r"<@[^>]+>\s*", "", text).strip()


_GREETING_PATTERN = re.compile(
    r"^(hi+|hello+|hey+|yo|sup|howdy|hiya|"
    r"good\s+(morning|afternoon|evening)|"
    r"thanks?|thank\s+you|ty|thx|"
    r"bye+|cya|later|cheers|"
    r"ok+ay?|okay|okey|cool|nice|"
    r"test|testing|ping|"
    r"help|help\s+me|need\s+help|need\s+some\s+help)"
    r"[\s,!.?\-]*$",
    re.I,
)


def _is_trivial_greeting(text):
    """Detect short greetings / filler where we should ask for the actual issue."""
    if not text:
        return True
    t = text.strip()
    if len(t) < 3:
        return True
    if _GREETING_PATTERN.match(t):
        return True
    return False


def _handle_app_mention(event):
    """@IT Help in a channel → reply in thread → KB search or AI chat in the same thread."""
    try:
        user_id = event.get("user")
        channel = event.get("channel")
        mention_ts = event.get("ts")
        if not user_id or not channel:
            return

        cleaned = _strip_slack_mentions(event.get("text", ""))
        query = cleaned.strip() if cleaned else ""

        if _is_trivial_greeting(query):
            slack_post_message(
                channel,
                f"👋 <@{user_id}> Hi! What's going wrong with your *work tech* (VPN, email, laptop, access, software)?\n\n"
                "🧵 *Please reply in this thread* (click *Reply* below or open the thread) with your issue and I'll help.",
                thread_ts=mention_ts,
            )
            # Register thread so replies are caught
            _bind_thread(mention_ts, {
                "channel": channel,
                "user_id": user_id,
                "query": "",
                "next_index": 0,
                "history": [],
                "awaiting_query": True,
            })
            return

        stop_spinner = start_live_spinner(channel, "Searching KB and preparing the best answer…", thread_ts=mention_ts)
        try:
            kb = search_confluence(query, 0)
            if kb:
                _post_kb_in_thread(channel, user_id, query, kb, mention_ts)
            else:
                start_ai_thread(channel, user_id, query, mention_ts)
        finally:
            stop_spinner()
    except Exception as e:
        print(f"[DEBUG] _handle_app_mention ERROR: {e}")


