"""Profile-driven daily brief engine.

Replaces the old hard-coded agents. Reads every profile in ``profiles/`` and,
for each one whose ``send_hour_utc`` matches the current UTC hour, builds a
personalised NewsAPI query, has a small OpenAI chat model synthesise a brief
tailored to that person's role, and emails it to them via Gmail SMTP.

Users self-configure their delivery time by setting ``send_hour_utc`` in their
own profile -- the workflow runs every hour and this gate decides who gets a
brief. No cron edits are ever needed to change a delivery time.

CLI:
    python send_brief.py                 # scheduled run: send to profiles whose hour matches now
    python send_brief.py --force         # ignore the hour gate; send to every active profile
    python send_brief.py --only <id>     # process a single profile (profiles/<id>.json)
    python send_brief.py --dry-run       # build queries and report who would send, without network/email
"""

import os
import sys
import re
import glob
import json
import argparse
import smtplib
import traceback
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone

import requests

# --- CONFIGURATION ---
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# Brief generation runs on a small OpenAI chat model. Override the model without
# a code change by setting OPENAI_MODEL in the environment. Use `or` (not a
# getenv default) so an empty-string env var -- what GitHub Actions passes for an
# undefined secret -- still falls back to the default instead of sending model="".
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

# Tune-my-brief loop: when both are set, every email footer gets a signed
# "Tune my brief" link (HMAC of the profile id -- no passwords). TUNE_BASE_URL
# is the deployed intake site, e.g. https://newsappig.netlify.app
PROFILE_LINK_SECRET = os.getenv("PROFILE_LINK_SECRET")
TUNE_BASE_URL = (os.getenv("TUNE_BASE_URL") or "").rstrip("/")

PROFILES_DIR = "profiles"
TEMPLATE_FILE = "email_template.html"
# example.json is a committed reference profile; the scheduled run never emails it.
REFERENCE_PROFILE = "example.json"

# NewsAPI look-back window and article budget. RECENCY is handled by fetching
# newest-first + the Curator preferring today's items; this window is only the
# safety net for how far back we look when today is quiet (weekends, niche
# topics, NewsAPI's ~24h free-tier delay), so it stays generous to avoid an
# empty brief. Prioritising today != fetching only today.
LOOKBACK_DAYS = 7
MAX_ARTICLES = 40
MAX_ARTICLES_TO_MODEL = 30

# Per-format budgets: how many items the Curator keeps and roughly how many
# words the writer spends per item.
FORMAT_BUDGETS = {
    "punchy": {"items": 4, "words": 60},
    "standard": {"items": 6, "words": 90},
    "deep": {"items": 8, "words": 130},
}
DEFAULT_FORMAT = "standard"


def _stage(profile_id, name, detail=""):
    """Stage-boundary marker; keeps every pipeline step visible in the Action log."""
    print(f"=== STAGE: {name} ({profile_id}){' -- ' + detail if detail else ''} ===")


def _openai_json(messages, max_tokens=1200):
    """One OpenAI call that must return JSON. Raises with a clear message on
    HTTP errors or unparseable output -- callers name the stage."""
    resp = requests.post(
        OPENAI_URL,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": OPENAI_MODEL,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": messages,
        },
        timeout=90,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI {resp.status_code}: {resp.text[:300]}")
    content = resp.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise RuntimeError(f"model returned unparseable JSON: {content[:200]}")
        return json.loads(match.group(0))


# --- PROFILE LOADING ---
def load_profiles(only=None):
    """Load profile dicts from ``profiles/``.

    Normal runs skip the reference ``example.json``. ``--only <id>`` targets a
    single profile and is allowed to load the reference profile for testing.
    """
    if only:
        path = os.path.join(PROFILES_DIR, f"{only}.json")
        if not os.path.exists(path):
            print(f"!! Profile not found: {path}")
            return []
        paths = [path]
    else:
        paths = sorted(glob.glob(os.path.join(PROFILES_DIR, "*.json")))

    profiles = []
    for path in paths:
        name = os.path.basename(path)
        if not only and name == REFERENCE_PROFILE:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                profile = json.load(f)
        except Exception as e:
            print(f"!! Failed to parse {path}: {e}")
            continue
        profile.setdefault("id", os.path.splitext(name)[0])
        profiles.append(profile)
    return profiles


def should_send(profile, force, current_hour):
    """Send gate: active AND (force OR send_hour_utc == current UTC hour)."""
    if not profile.get("active", False):
        return False
    if force:
        return True
    try:
        return int(profile.get("send_hour_utc", -1)) == current_hour
    except (TypeError, ValueError):
        return False


# --- NEWS FETCH ---
def _quote_term(term):
    term = term.strip()
    if not term:
        return ""
    # Quote multi-word terms so NewsAPI treats them as a phrase.
    return f'"{term}"' if " " in term else term


def build_query(profile):
    """Build a NewsAPI query from topics + watchlist (OR) AND regions (OR)."""
    terms = list(profile.get("topics", [])) + list(profile.get("watchlist", []))
    topic_terms = [q for q in (_quote_term(t) for t in terms) if q]
    region_terms = [q for q in (_quote_term(r) for r in profile.get("regions", [])) if q]

    query = ""
    if topic_terms:
        query = "(" + " OR ".join(topic_terms) + ")"
    if region_terms:
        region_clause = "(" + " OR ".join(region_terms) + ")"
        query = f"{query} AND {region_clause}" if query else region_clause
    return query


def _fetch_newsapi_query(profile, query, page_size, use_domains=True):
    """One NewsAPI call. Returns a (possibly empty) article list."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    params = {
        "q": query,
        "from": date_from,
        # Newest-first so the freshest (today's) stories are always in the
        # candidate pool; the Curator then ranks by relevance + recency.
        "sortBy": "publishedAt",
        "language": profile.get("language", "en"),
        "pageSize": page_size,
        "apiKey": NEWS_API_KEY,
    }
    if use_domains:
        sources = [s.strip() for s in profile.get("priority_sources", []) if s.strip()]
        # arxiv.org never serves NewsAPI results; keep it for the arxiv adapter only.
        newsapi_sources = [s for s in sources if s != "arxiv.org"]
        if newsapi_sources:
            params["domains"] = ",".join(newsapi_sources)

    try:
        response = requests.get("https://newsapi.org/v2/everything", params=params, timeout=30)
        data = response.json()
    except Exception as e:
        print(f"!! NewsAPI request error for {profile['id']}: {e}")
        return []
    if data.get("status") != "ok":
        print(f"!! NewsAPI error for {profile['id']}: {data.get('code')} - {data.get('message')}")
        return []
    return data.get("articles", [])


def fetch_newsapi(profile):
    """NewsAPI adapter. Iterates the profile's themed query_packs (the NewsAPI
    `q` parameter is capped at ~500 chars, so a 20-40 entity watchlist can never
    be one query); falls back to the single legacy query when no packs exist."""
    packs = [p for p in profile.get("query_packs", [])
             if isinstance(p, dict) and p.get("q")]
    if not packs:
        query = build_query(profile)
        if not query:
            print(f"!! {profile['id']}: empty query (no packs/topics/watchlist); skipping NewsAPI.")
            return []
        packs = [{"name": "all", "q": query}]

    per_pack = max(10, MAX_ARTICLES // len(packs))
    articles = []
    for pack in packs[:8]:
        got = _fetch_newsapi_query(profile, str(pack["q"])[:450], per_pack)
        print(f"   pack '{pack.get('name', '?')}': {len(got)} articles")
        articles.extend(got)

    # An over-restrictive priority_sources list (or a quiet few days on those
    # specific sites) must not produce an empty brief. If we found nothing within
    # the preferred domains, retry across ALL sources.
    has_domains = any(s.strip() and s.strip() != "arxiv.org"
                      for s in profile.get("priority_sources", []))
    if not articles and has_domains:
        print(f"-> {profile['id']}: nothing within priority_sources; retrying across all sources")
        for pack in packs[:8]:
            got = _fetch_newsapi_query(profile, str(pack["q"])[:450], per_pack, use_domains=False)
            print(f"   pack '{pack.get('name', '?')}' (all sources): {len(got)} articles")
            articles.extend(got)
    return articles


def fetch_arxiv(profile):
    """arXiv adapter. arxiv.org content does NOT come through NewsAPI -- without
    this a research profile silently gets zero papers. Queries the arXiv Atom
    API by category + top watchlist terms, last LOOKBACK_DAYS days."""
    categories = [c.strip() for c in profile.get("arxiv_categories", []) if c.strip()]
    if not categories:
        return []

    import xml.etree.ElementTree as ET

    cat_clause = " OR ".join(f"cat:{c}" for c in categories[:8])
    terms = [t for t in profile.get("watchlist", [])[:6] if t]
    term_clause = " OR ".join(f'all:"{t}"' for t in terms)
    query = f"({cat_clause})" + (f" AND ({term_clause})" if term_clause else "")

    try:
        resp = requests.get(
            "https://export.arxiv.org/api/query",
            params={
                "search_query": query,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": 25,
            },
            timeout=30,
        )
        root = ET.fromstring(resp.content)
    except Exception as e:
        print(f"!! arXiv request error for {profile['id']}: {e}")
        return []

    ns = {"a": "http://www.w3.org/2005/Atom"}
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    articles = []
    for entry in root.findall("a:entry", ns):
        published = (entry.findtext("a:published", "", ns) or "").strip()
        try:
            pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            continue
        if pub_dt < cutoff:
            continue
        title = re.sub(r"\s+", " ", entry.findtext("a:title", "", ns) or "").strip()
        url = (entry.findtext("a:id", "", ns) or "").strip().replace("http://", "https://", 1)
        summary = re.sub(r"\s+", " ", entry.findtext("a:summary", "", ns) or "").strip()
        if title and url:
            articles.append({
                "title": title,
                "url": url,
                "urlToImage": None,
                "description": summary[:300],
                "source": {"name": "arXiv"},
                "publishedAt": published,
            })
    return articles


# Google News RSS locale by 2-letter language (hl / gl / ceid).
_GNEWS_LOCALE = {
    "en": ("en-US", "US", "US:en"), "nl": ("nl", "NL", "NL:nl"),
    "de": ("de", "DE", "DE:de"), "fr": ("fr", "FR", "FR:fr"),
    "es": ("es", "ES", "ES:es"), "it": ("it", "IT", "IT:it"),
    "pt": ("pt-PT", "PT", "PT:pt"), "sv": ("sv", "SE", "SE:sv"),
}


def fetch_googlenews(profile):
    """Google News RSS adapter -- free, no key, ~real-time. Complements NewsAPI
    with broad current coverage (snippets only; links are Google redirects). Runs
    across all sources (no domain restriction) as the broad real-time source."""
    import urllib.parse
    import email.utils
    import xml.etree.ElementTree as ET

    packs = [p for p in profile.get("query_packs", []) if isinstance(p, dict) and p.get("q")]
    if not packs:
        q = build_query(profile)
        if not q:
            return []
        packs = [{"name": "all", "q": q}]

    lang = (profile.get("language") or "en").split("-")[0].lower()
    hl, gl, ceid = _GNEWS_LOCALE.get(lang, _GNEWS_LOCALE["en"])
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    articles = []
    for pack in packs[:8]:
        # Google News search supports OR/quotes and when:Nd for recency.
        q = f"{str(pack['q'])[:250]} when:{LOOKBACK_DAYS}d"
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
               + f"&hl={hl}&gl={gl}&ceid={ceid}")
        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (news-app)"})
            root = ET.fromstring(resp.content)
        except Exception as e:
            print(f"!! Google News error for {profile['id']}: {e}")
            continue

        count = 0
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            src_el = item.find("source")
            source = ((src_el.text if src_el is not None else "") or "Google News").strip()

            iso = ""
            try:
                dt = email.utils.parsedate_to_datetime(pub)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
                iso = dt.astimezone(timezone.utc).isoformat()
            except Exception:
                pass

            # Google titles are usually "Headline - Source"; trim the source suffix.
            if source and title.endswith(f" - {source}"):
                title = title[: -(len(source) + 3)].strip()
            if title and link:
                articles.append({
                    "title": title,
                    "url": link,
                    "urlToImage": None,
                    "description": _clean_html(item.findtext("description") or "")[:300],
                    "source": {"name": source},
                    "publishedAt": iso,
                })
                count += 1
        print(f"   gnews pack '{pack.get('name', '?')}': {count} articles")
    return articles


def _norm_url(u):
    return (u or "").strip().rstrip("/")


def _title_key(a):
    """Normalised title, used to dedupe the same story across sources
    (a Google-redirect URL and a publisher URL never match on URL alone)."""
    t = re.sub(r"[^a-z0-9 ]", "", (a.get("title") or "").lower())
    return re.sub(r"\s+", " ", t).strip()[:60]


def _pub_ts(a):
    try:
        return datetime.fromisoformat((a.get("publishedAt") or "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def fetch_news(profile):
    """All source adapters, merged, deduped (URL + title) and newest-first."""
    articles = fetch_newsapi(profile)

    gnews = fetch_googlenews(profile)
    if gnews:
        print(f"   google news: {len(gnews)} items")
    articles.extend(gnews)

    arxiv_items = fetch_arxiv(profile)
    if arxiv_items:
        print(f"   arxiv: {len(arxiv_items)} papers")
    articles.extend(arxiv_items)

    seen_url, seen_title, unique = set(), set(), []
    for a in articles:
        url = _norm_url(a.get("url"))
        tkey = _title_key(a)
        if (url and url in seen_url) or (tkey and tkey in seen_title):
            continue
        if url:
            seen_url.add(url)
        if tkey:
            seen_title.add(tkey)
        unique.append(a)

    # Freshest across ALL sources first, so the Curator sees today's news up top.
    unique.sort(key=_pub_ts, reverse=True)
    print(f"-> {profile['id']}: {len(unique)} unique articles across adapters")
    return unique


# --- IMAGE ENRICHMENT (every article in the email gets a picture) ---
def _extract_og_image(url):
    """Best-effort Open Graph / Twitter image from a direct article URL. Google
    News redirect links are skipped (they don't expose a usable image)."""
    if not url or "news.google.com" in url:
        return None
    try:
        resp = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0 (news-app)"})
        html = resp.text[:150000]
    except Exception:
        return None
    for pat in (
        r'<meta[^>]+(?:property|name)=["\'](?:og:image|og:image:url|twitter:image)["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']',
    ):
        m = re.search(pat, html, re.IGNORECASE)
        if m and m.group(1).strip().startswith("http"):
            return m.group(1).strip()
    return None


def _fallback_image(a):
    """Deterministic neutral photo so a story without a source image still shows
    one (Google News / arXiv items don't carry an image)."""
    import hashlib
    seed = hashlib.md5((a.get("url") or a.get("title") or "x").encode("utf-8")).hexdigest()[:12]
    return f"https://picsum.photos/seed/{seed}/640/360"


def enrich_images(articles):
    """Guarantee every article has an image URL: source image -> og:image ->
    neutral fallback. Runs only on the curated shortlist (a handful of items),
    so the extra fetches are bounded."""
    real, og, fb = 0, 0, 0
    for a in articles:
        if a.get("urlToImage"):
            real += 1
            continue
        img = _extract_og_image(a.get("url"))
        if img:
            og += 1
        else:
            img = _fallback_image(a)
            fb += 1
        a["urlToImage"] = img
    print(f"   images: {real} from source, {og} via og:image, {fb} fallback")
    return articles


# --- SENT HISTORY (no-repeat news across days) ---
HISTORY_DIR = "history"
HISTORY_RETENTION_DAYS = 30


def _history_path(profile_id):
    return os.path.join(HISTORY_DIR, f"{profile_id}.json")


def load_sent_urls(profile_id):
    """URLs already emailed to this profile within the retention window."""
    path = _history_path(profile_id)
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {_norm_url(e.get("url")) for e in data.get("sent", []) if e.get("url")}
    except Exception as e:
        print(f"!! {profile_id}: could not read sent-history: {e}")
        return set()


def record_sent_urls(profile_id, urls):
    """Append today's sent URLs to the profile's history and prune old ones."""
    path = _history_path(profile_id)
    entries = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                entries = json.load(f).get("sent", [])
        except Exception:
            entries = []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)).strftime("%Y-%m-%d")
    have = {_norm_url(e.get("url")) for e in entries if e.get("url")}
    for u in urls:
        nu = _norm_url(u)
        if nu and nu not in have:
            entries.append({"url": nu, "date": today})
            have.add(nu)
    entries = [e for e in entries if str(e.get("date", "9999")) >= cutoff]

    os.makedirs(HISTORY_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"sent": entries}, f, indent=2)
    print(f"-> {profile_id}: sent-history now holds {len(entries)} URL(s)")


# --- ANALYSIS (OpenAI) ---
# --- CURATOR (role 4) ---
def curate(profile, articles):
    """Score every candidate 0-10 against the profile; keep top N per format.

    Returns (shortlist, rationale_by_id). On any failure the error is logged
    with the stage name and the top-N articles pass through unscored -- a
    curator outage must not kill the send.
    """
    fmt = profile.get("format") or DEFAULT_FORMAT
    budget = FORMAT_BUDGETS.get(fmt, FORMAT_BUDGETS[DEFAULT_FORMAT])
    _stage(profile["id"], "CURATOR", f"{len(articles)} candidates -> top {budget['items']} ({fmt})")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    candidates = []
    for i, a in enumerate(articles[:MAX_ARTICLES_TO_MODEL]):
        candidates.append({
            "id": i,
            "title": (a.get("title") or "")[:200],
            "source": (a.get("source") or {}).get("name", ""),
            "date": (a.get("publishedAt") or "")[:10],
            "desc": (a.get("description") or "")[:300],
        })

    goals = profile.get("goals") or []
    info_needs = profile.get("info_needs") or []
    context = {
        "role_context": profile.get("role_context", ""),
        "goals": goals,
        "info_needs": info_needs,
        # v1 profiles have no goals/info_needs; topics+watchlist still anchor scoring.
        "topics": profile.get("topics", []),
        "watchlist": profile.get("watchlist", []),
        "exclude": profile.get("exclude", []),
        # The tuning loop: recent reader notes shift what scores well.
        "recent_reader_feedback": [f.get("note", "") for f in profile.get("feedback_log", [])
                                   if isinstance(f, dict) and f.get("note")][-3:],
    }

    try:
        out = _openai_json([
            {"role": "system", "content": (
                "You are the Curator for a personalised DAILY brief. Score EVERY candidate item "
                "0-10 for how much it belongs in TODAY's brief for THIS reader.\n"
                f"Today is {today}. This is a daily news brief, so RECENCY matters as much as fit: "
                "strongly prefer items published today or in the last 24 hours. Only include an older "
                "item (2-3 days) if it is highly relevant AND still timely; never let a stale item "
                "outrank a fresh, on-topic one. Use each candidate's 'date' field.\n"
                "Score relevance against their goals, information needs, watchlist and topics. Score 0 "
                "for anything matching their exclude list, duplicated stories, or generic news with no "
                "bearing on their work.\n"
                'Return JSON: {"items":[{"id":int,"score":number,"reason":"one line"}]} '
                "covering every candidate id exactly once. Fold recency into the score."
            )},
            {"role": "user", "content":
                "READER:\n" + json.dumps(context, ensure_ascii=False) +
                "\n\nCANDIDATES (each has a 'date' = publish date):\n" +
                json.dumps(candidates, ensure_ascii=False)},
        ], max_tokens=2000)
        items = out.get("items")
        if not isinstance(items, list):
            raise RuntimeError("contract violation: items missing")
        scored = []
        for it in items:
            try:
                idx = int(it["id"])
                score = float(it.get("score", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= idx < len(articles) and score > 0:
                scored.append((score, idx, str(it.get("reason", ""))[:200]))
        scored.sort(reverse=True)
        keep = scored[: budget["items"]]
        shortlist = [articles[idx] for _, idx, _ in keep]
        rationale = {idx: reason for _, idx, reason in keep}
        for score, idx, reason in keep:
            print(f"   keep [{score:.0f}/10] {articles[idx].get('title', '')[:70]} -- {reason}")
        dropped = len(candidates) - len(keep)
        print(f"-> curator kept {len(keep)}, dropped {dropped}")
        if not shortlist:
            raise RuntimeError("curator scored everything 0 -- falling back to top-N")
        return shortlist, rationale
    except Exception as e:
        print(f"!! STAGE CURATOR failed for {profile['id']}: {e}")
        print(f"   falling back to first {budget['items']} articles unscored")
        return articles[: budget["items"]], {}


# --- CRITIC (role 5) ---
def critique(profile, html_content):
    """QA the draft brief against the profile. Returns (passed, notes)."""
    _stage(profile["id"], "CRITIC")
    checks = {
        "role_context": profile.get("role_context", ""),
        "goals": profile.get("goals", []),
        "topics": profile.get("topics", []),
        "watchlist": profile.get("watchlist", []),
        "exclude": profile.get("exclude", []),
        "format": profile.get("format") or DEFAULT_FORMAT,
        "item_budget": FORMAT_BUDGETS.get(profile.get("format") or DEFAULT_FORMAT,
                                          FORMAT_BUDGETS[DEFAULT_FORMAT])["items"],
    }
    try:
        out = _openai_json([
            {"role": "system", "content": (
                "You are the Critic, QA-ing a personalised daily brief before it is emailed.\n"
                "Checks: (1) every item is traceably relevant to this reader's profile; "
                "(2) nothing matches the exclude list; (3) every item has a well-formed http(s) "
                "link -- news.google.com/rss/... redirect links are VALID and must NOT be flagged "
                "(they resolve to the publisher); (4) the item count is within +/-2 of the format "
                "budget; (5) tone is professional and concrete, no filler.\n"
                'Return JSON: {"passed": true|false, "notes": "empty when passed; otherwise the '
                'specific problems, actionable enough to fix in one rewrite"}'
            )},
            {"role": "user", "content":
                "PROFILE:\n" + json.dumps(checks, ensure_ascii=False) +
                "\n\nDRAFT BRIEF (HTML):\n" + html_content[:12000]},
        ], max_tokens=600)
        passed = bool(out.get("passed"))
        notes = str(out.get("notes", ""))[:800]
        print(f"-> critic verdict: {'PASS' if passed else 'FAIL'}{' -- ' + notes if notes else ''}")
        return passed, notes
    except Exception as e:
        print(f"!! STAGE CRITIC failed for {profile['id']}: {e}")
        print("   treating draft as passed (critic outage must not block the send)")
        return True, ""


def build_prompt(profile, raw_text):
    name = profile.get("name") or "the reader"
    role = profile.get("role_context", "")
    goals = "; ".join(profile.get("goals", [])) or "(not specified)"
    info_needs = "; ".join(profile.get("info_needs", [])) or "(not specified)"
    topics = ", ".join(profile.get("topics", [])) or "(none specified)"
    watchlist = ", ".join(profile.get("watchlist", [])) or "(none specified)"
    exclude = ", ".join(profile.get("exclude", [])) or "(nothing specific)"
    language = profile.get("language", "en")

    fmt = profile.get("format") or DEFAULT_FORMAT
    budget = FORMAT_BUDGETS.get(fmt, FORMAT_BUDGETS[DEFAULT_FORMAT])

    # The tuning loop: the reader's last few notes steer selection and tone.
    feedback = [f.get("note", "") for f in profile.get("feedback_log", [])
                if isinstance(f, dict) and f.get("note")][-3:]
    feedback_block = ""
    if feedback:
        feedback_block = (
            "### READER FEEDBACK (recent notes -- honour these):\n- "
            + "\n- ".join(feedback) + "\n\n"
        )

    return (
        f"Role: You are a senior intelligence analyst writing today's personalised daily brief for {name}.\n"
        f"About the reader: {role}\n"
        f"Their goals (next 6-12 months): {goals}\n"
        f"Their information needs: {info_needs}\n"
        f"Their focus topics: {topics}\n"
        f"Their watchlist (companies / products / protocols / people to track closely): {watchlist}\n"
        f"Do NOT cover / explicitly exclude: {exclude}\n\n"

        "Task: The items below were pre-selected by a curator (WhyRelevant notes included where "
        "available). Rank them for THIS reader and write the brief. Drop anything that on closer "
        "reading is irrelevant or excluded. Quality over quantity.\n\n"

        "### OUTPUT (an HTML fragment only -- no <html>, <head> or <body> wrapper):\n"
        "1. Open with the bottom line:\n"
        "   <p><strong>Bottom line:</strong> 2-3 sentences with the single most important takeaway for them today.</p>\n"
        f"2. Then about {budget['items']} story blocks (never more than {budget['items'] + 1}), "
        "most important first, each formatted exactly as:\n"
        "   <div class='section'>\n"
        "     <div class='news-title'>HEADLINE</div>\n"
        "     <img src='IMG_URL' class='story-image'>   <!-- REQUIRED: every story block MUST open with its image, using the item's IMG value verbatim -->\n"
        "     <p><strong>What happened:</strong> the facts, 1-2 sentences. <a href='URL'>[1]</a></p>\n"
        "     <p><strong>Why it matters for you:</strong> tie it directly to their goals, watchlist or role.</p>\n"
        "     <p><strong>Watch / do:</strong> one concrete thing to monitor or act on.</p>\n"
        "   </div>\n\n"

        "### RULES:\n"
        "- EVERY story block MUST include its image as the first element: <img src='IMG_URL' "
        "class='story-image'> using that item's IMG value exactly. No story without a picture.\n"
        "- This is a DAILY brief: lead with the freshest news (today's, then most recent). Each item "
        "shows a Date -- order the brief newest-first and drop anything that reads as stale.\n"
        f"- Format: '{fmt}' -- keep each story block around {budget['words']} words.\n"
        "- Cite every claim with a clickable footnote <a href='URL'>[n]</a> using the article's real URL.\n"
        "- Keep the whole brief tight: a busy person should read it well inside 15 minutes.\n"
        f"- Write in the reader's language (language code: {language}).\n"
        "- Output raw HTML only. Do NOT wrap it in markdown code fences.\n\n"

        f"{feedback_block}"
        f"RAW ARTICLES:\n{raw_text}"
    )


def analyze(profile, articles, rationale=None, critic_notes=""):
    if not OPENAI_API_KEY:
        return None, "OPENAI_API_KEY is not set."

    _stage(profile["id"], "WRITER", f"{len(articles)} items, model {OPENAI_MODEL}")
    raw_text = ""
    for i, a in enumerate(articles[:MAX_ARTICLES_TO_MODEL]):
        title = (a.get("title") or "").replace('"', "'")
        source = (a.get("source") or {}).get("name", "Unknown")
        url = a.get("url", "")
        img = a.get("urlToImage") or "NO_IMAGE"
        desc = (a.get("description") or "").replace('"', "'")
        date = (a.get("publishedAt") or "")[:10]
        why = ""
        if rationale and i in rationale:
            why = f" | WhyRelevant: {rationale[i]}"
        raw_text += (
            f"ID: {i + 1} | Date: {date} | Title: {title} | Source: {source} | URL: {url} | IMG: {img} | Desc: {desc}{why}\n"
        )

    prompt = build_prompt(profile, raw_text)
    if critic_notes:
        prompt += (
            "\n\n### QA NOTES FROM THE PREVIOUS DRAFT (fix ALL of these in this rewrite):\n"
            + critic_notes
        )
    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0.4,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a senior intelligence analyst. Follow the user's instructions exactly "
                    "and output a raw HTML fragment only -- no markdown code fences, no <html> or "
                    "<body> wrapper."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(OPENAI_URL, headers=headers, json=payload, timeout=90)
        if resp.status_code != 200:
            return None, f"OpenAI error {resp.status_code}: {resp.text[:400]}"
        content = resp.json()["choices"][0]["message"]["content"]
        clean = content.replace("```html", "").replace("```", "").strip()
        return clean, None
    except Exception:
        return None, traceback.format_exc()


# --- EMAIL ---
def _clean_html(raw_html):
    text = re.sub(r"<[^>]+>", "", raw_html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _signed_link(profile, action):
    """Signed {action}-my-brief URL for this profile, or '' when not configured.
    The HMAC token authenticates that the holder controls this profile (no
    passwords); the same token serves both tune and unsubscribe."""
    if not (PROFILE_LINK_SECRET and TUNE_BASE_URL):
        return ""
    import hmac
    import hashlib
    token = hmac.new(PROFILE_LINK_SECRET.encode(), profile["id"].encode(),
                     hashlib.sha256).hexdigest()[:32]
    return f"{TUNE_BASE_URL}/?{action}={profile['id']}&token={token}"


def tune_link(profile):
    return _signed_link(profile, "tune")


def send_email(profile, html_content):
    try:
        with open(TEMPLATE_FILE, encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        template = "<html><body>{{DATE}}{{CONTENT}}</body></html>"

    link = tune_link(profile)
    unsub = _signed_link(profile, "unsubscribe")
    if link:
        html_content += (
            "<div style='text-align:center;margin:32px 0 8px'>"
            f"<a href='{link}' style='display:inline-block;background:#00b8d4;color:#04252b;"
            "font-weight:700;text-decoration:none;padding:13px 24px;border-radius:24px;font-size:15px'>"
            "✏️ Change my brief</a>"
            "<div style='font-size:12px;color:#999;margin-top:8px'>"
            "Opens a quick chat to adjust what you get.</div></div>"
        )
    if unsub:
        html_content += (
            "<div style='text-align:center;margin:14px 0 4px'>"
            f"<a href='{unsub}' style='color:#999;font-size:12px;text-decoration:underline'>"
            "Unsubscribe &amp; delete my data</a></div>"
        )

    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    name = (profile.get("name") or "").strip()
    header = f"Daily Brief · {name}" if name else "Daily Brief"

    final_html = template.replace("⚡ Compute Market Intel", header)
    final_html = final_html.replace("{{DATE}}", date_str)
    final_html = final_html.replace("{{CONTENT}}", html_content)

    greeting = f"Good morning{', ' + name if name else ''}. Here is your daily brief for " \
               f"{datetime.now(timezone.utc).strftime('%A, %B %d')}.\n\n"
    plain_text = greeting + _clean_html(html_content)
    if link:
        plain_text += f"\n\nChange my brief (opens a quick chat to adjust it): {link}"
    if unsub:
        plain_text += f"\n\nUnsubscribe & delete my data: {unsub}"

    recipient = profile["email"]
    msg = MIMEMultipart("alternative")
    msg["From"] = EMAIL_USER
    msg["To"] = recipient
    msg["Subject"] = f"Your Daily Brief · {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(final_html, "html"))

    server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
    try:
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        # send_message returns a dict of recipients the SERVER refused outright.
        refused = server.send_message(msg)
    finally:
        server.quit()

    if refused:
        # Recipient was rejected by Gmail at handoff (bad address, etc.).
        raise RuntimeError(f"SMTP refused recipients: {refused}")

    # NOTE: confirms Gmail ACCEPTED the message -- not that it reached the inbox
    # (spam/bounce can still happen downstream, out of our control).
    print(f"vv SMTP accepted brief for {recipient}")


# --- ORCHESTRATION ---
def process(profile):
    """Full pipeline for one profile: FETCH -> CURATOR -> WRITER -> CRITIC -> SEND."""
    print(f"=== Processing {profile['id']} <{profile.get('email')}> ===")

    _stage(profile["id"], "FETCH")
    articles = fetch_news(profile)
    if not articles:
        print(f"-> {profile['id']}: no articles; nothing sent.")
        return False

    # No-repeat: drop anything already emailed to this reader in the last
    # HISTORY_RETENTION_DAYS, so each brief is genuinely new.
    already_sent = load_sent_urls(profile["id"])
    if already_sent:
        fresh = [a for a in articles if _norm_url(a.get("url")) not in already_sent]
        dropped = len(articles) - len(fresh)
        if dropped:
            print(f"-> {profile['id']}: dropped {dropped} already-sent article(s)")
        articles = fresh
    if not articles:
        print(f"-> {profile['id']}: no NEW articles today (all already sent); nothing sent.")
        return False

    shortlist, rationale = curate(profile, articles)
    # rationale keys are indexes into `articles`; remap onto shortlist order.
    remapped = {}
    for new_i, art in enumerate(shortlist):
        for old_i, reason in (rationale or {}).items():
            if articles[old_i] is art:
                remapped[new_i] = reason
                break

    # Ensure every item in the brief has a picture.
    _stage(profile["id"], "IMAGES")
    enrich_images(shortlist)

    html, err = analyze(profile, shortlist, rationale=remapped)
    if not html:
        print(f"!! STAGE WRITER failed for {profile['id']}:\n{err}")
        return False

    passed, notes = critique(profile, html)
    if not passed and notes:
        _stage(profile["id"], "WRITER", "regenerating once with critic notes")
        retry_html, retry_err = analyze(profile, shortlist, rationale=remapped, critic_notes=notes)
        if retry_html:
            retry_passed, _ = critique(profile, retry_html)
            # Send the best draft we have: the rewrite if it passed (or at
            # least exists); the original otherwise.
            html = retry_html if retry_passed or retry_html else html
        else:
            print(f"!! regeneration failed, sending first draft: {retry_err}")

    _stage(profile["id"], "SEND")
    try:
        send_email(profile, html)
    except Exception as e:
        print(f"!! {profile['id']}: failed to send email: {e}")
        return False
    # Record what we just sent so tomorrow's brief won't repeat it. Record the
    # curated shortlist (what actually went into the brief).
    record_sent_urls(profile["id"], [a.get("url") for a in shortlist])
    print(f"vv {profile['id']}: brief sent to {profile['email']}")
    return True


def dry_run(profiles, force, current_hour):
    print(f"--- DRY RUN (current UTC hour: {current_hour}) ---")
    for p in profiles:
        gated = should_send(p, force, current_hour)
        print(f"\n[{p.get('id')}] active={p.get('active')} send_hour_utc={p.get('send_hour_utc')} "
              f"email={p.get('email')} -> would_send={gated}")
        print(f"  language: {p.get('language', 'en')} | format: {p.get('format', DEFAULT_FORMAT)} "
              f"| tz: {p.get('timezone', '-')} | sources: {p.get('priority_sources', [])}")
        packs = p.get("query_packs", [])
        if packs:
            for pk in packs:
                print(f"  pack '{pk.get('name', '?')}': {str(pk.get('q', ''))[:100]}")
        else:
            print(f"  query (legacy single): {build_query(p)}")
        if p.get("arxiv_categories"):
            print(f"  arxiv: {p['arxiv_categories']} + top watchlist terms")


def main():
    parser = argparse.ArgumentParser(description="Profile-driven daily brief engine")
    parser.add_argument("--force", action="store_true",
                        help="Ignore the send-hour gate; process every active profile.")
    parser.add_argument("--only", metavar="ID",
                        help="Process a single profile by id (profiles/<id>.json).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report queries and who would send, without calling any API or sending email.")
    args = parser.parse_args()

    current_hour = datetime.now(timezone.utc).hour
    profiles = load_profiles(only=args.only)
    if not profiles:
        print("No profiles to process.")
        return

    if args.dry_run:
        dry_run(profiles, args.force, current_hour)
        return

    missing = [
        k for k, v in {
            "NEWS_API_KEY": NEWS_API_KEY,
            "OPENAI_API_KEY": OPENAI_API_KEY,
            "EMAIL_USER": EMAIL_USER,
            "EMAIL_PASSWORD": EMAIL_PASSWORD,
        }.items() if not v
    ]
    if missing:
        print(f"!! Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    sent = 0
    considered = 0
    for p in profiles:
        if not p.get("email"):
            print(f"-- {p.get('id')}: no email address; skipping.")
            continue
        if not should_send(p, args.force, current_hour):
            print(f"-- {p['id']}: skipped (active={p.get('active')}, "
                  f"send_hour_utc={p.get('send_hour_utc')}, current UTC hour={current_hour}).")
            continue
        considered += 1
        if process(p):
            sent += 1

    print(f"\nDone. {sent}/{considered} brief(s) sent.")


if __name__ == "__main__":
    main()
