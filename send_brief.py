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


# --- IMAGE ENRICHMENT (every article gets a picture that fits its topic) ---
def _is_direct_url(url):
    """A publisher article URL we can scrape for a real image. Google News
    redirect links and arXiv abstract pages don't expose a usable og:image."""
    if not url:
        return False
    return "news.google.com" not in url and "arxiv.org" not in url


def _extract_og_image(url):
    """Best-effort Open Graph / Twitter image from a direct article URL -- the
    article's OWN photo, so it always fits the story."""
    if not _is_direct_url(url):
        return None
    try:
        resp = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0 (news-app)"},
                            allow_redirects=True)
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


_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at", "by",
    "with", "from", "as", "is", "are", "be", "new", "says", "will", "how",
    "why", "what", "this", "that", "its", "it", "after", "over", "into", "amid",
}


def _topical_query(a):
    """A few salient keywords from the story so the stock-image search returns a
    picture that actually matches the topic (not random)."""
    label = (a.get("_topic_label") or "").strip()
    if label:
        return label
    title = a.get("title") or ""
    title = re.sub(r"\s+-\s+[^-]+$", "", title)  # trim "Headline - Source"
    words = re.findall(r"[A-Za-z0-9']+", title)
    keep = [w for w in words if w.lower() not in _STOPWORDS and len(w) > 2]
    # Lead with proper nouns / capitalised entities, then top up with the other
    # content words so the query is specific even when few words are capitalised.
    proper = [w for w in keep if w[:1].isupper()]
    picked, seen = [], set()
    for w in proper + keep:
        lw = w.lower()
        if lw not in seen:
            seen.add(lw)
            picked.append(w)
        if len(picked) >= 5:
            break
    return " ".join(picked) or (title[:60])


def _fallback_image(a):
    """A TOPICAL photo when the source carries none: the story's own keywords are
    encoded into the image URL, so the recipient's mail client resolves a picture
    that actually fits the article (not a random stock photo). Deterministic
    (``lock``) so the same story always shows the same image, and resolved
    client-side -- no engine-side fetch, so nothing here can fail the send."""
    import hashlib
    q = _topical_query(a)
    tags = ",".join(re.findall(r"[A-Za-z0-9]+", q)[:3]).lower() or "news,headline"
    lock = int(hashlib.md5((a.get("url") or a.get("title") or "x").encode("utf-8")).hexdigest()[:6], 16)
    return f"https://loremflickr.com/640/360/{tags}?lock={lock}"


def enrich_images(articles):
    """Guarantee every article has an image that fits its topic:
    source image -> the article's own og:image -> a topical photo matched on the
    story's keywords. Runs only on the curated shortlist (a handful of items),
    so the og:image fetches are bounded."""
    real, og, topical = 0, 0, 0
    for a in articles:
        if a.get("urlToImage"):
            real += 1
            continue
        img = _extract_og_image(a.get("url"))
        if img:
            og += 1
        else:
            img = _fallback_image(a)
            topical += 1
        a["urlToImage"] = img
    print(f"   images: {real} from source, {og} via og:image, {topical} topical fallback")
    return articles


# --- SENT HISTORY (no-repeat news across days; topic memory for follow-ups) ---
HISTORY_DIR = "history"
HISTORY_RETENTION_DAYS = 7


def _history_path(profile_id):
    return os.path.join(HISTORY_DIR, f"{profile_id}.json")


def _load_history(profile_id):
    path = _history_path(profile_id)
    if not os.path.exists(path):
        return {"sent": [], "topics": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {"sent": data.get("sent", []), "topics": data.get("topics", [])}
    except Exception as e:
        print(f"!! {profile_id}: could not read sent-history: {e}")
        return {"sent": [], "topics": []}


def load_sent_urls(profile_id):
    """URLs already emailed to this profile within the retention window."""
    data = _load_history(profile_id)
    return {_norm_url(e.get("url")) for e in data["sent"] if e.get("url")}


def load_sent_topics(profile_id):
    """Topics already covered in the last HISTORY_RETENTION_DAYS, each with the
    most recent date it was sent. Powers the cross-day follow-up rule: a topic
    may only reappear if there is genuinely newer news on it."""
    data = _load_history(profile_id)
    latest = {}
    for t in data["topics"]:
        label = (t.get("label") or "").strip()
        date = str(t.get("date") or "")
        if not label:
            continue
        if label not in latest or date > latest[label]:
            latest[label] = date
    return [{"label": k, "date": v} for k, v in latest.items()]


def record_sent(profile_id, sent_items):
    """Append today's sent URLs and topics to the profile's history and prune
    anything older than the retention window.

    ``sent_items`` is a list of ``{"url": ..., "topic": ...}`` for the items
    that actually went into today's brief.
    """
    data = _load_history(profile_id)
    url_entries = data["sent"]
    topic_entries = data["topics"]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)).strftime("%Y-%m-%d")

    have_urls = {_norm_url(e.get("url")) for e in url_entries if e.get("url")}
    for it in sent_items:
        nu = _norm_url(it.get("url"))
        if nu and nu not in have_urls:
            url_entries.append({"url": nu, "date": today})
            have_urls.add(nu)
        topic = (it.get("topic") or "").strip()
        if topic:
            topic_entries.append({"label": topic, "date": today, "url": nu})

    url_entries = [e for e in url_entries if str(e.get("date", "9999")) >= cutoff]
    topic_entries = [e for e in topic_entries if str(e.get("date", "9999")) >= cutoff]

    os.makedirs(HISTORY_DIR, exist_ok=True)
    with open(path := _history_path(profile_id), "w", encoding="utf-8") as f:
        json.dump({"sent": url_entries, "topics": topic_entries}, f, indent=2)
    print(f"-> {profile_id}: sent-history now holds {len(url_entries)} URL(s), "
          f"{len({e['label'] for e in topic_entries})} topic(s)")


# --- ANALYSIS (OpenAI) ---
def _is_research(a):
    """Whether an item is an academic/preprint (arXiv) rather than a news story.
    Used to keep the brief from getting too research-heavy."""
    src = (a.get("source") or {}).get("name", "").lower()
    url = (a.get("url") or "").lower()
    return "arxiv" in src or "arxiv.org" in url


def _sent_topic_conflict(article, sent_topics):
    """Last-sent date of a recent topic this article clearly rehashes, else None.
    A token-overlap heuristic used by the LLM-outage fallback so it still honours
    the no-repeat rule (the LLM path does this far more precisely)."""
    ttoks = {w for w in re.findall(r"[a-z0-9]+", (article.get("title") or "").lower())
             if w not in _STOPWORDS and len(w) > 3}
    hit = None
    for t in sent_topics:
        ltoks = {w for w in re.findall(r"[a-z0-9]+", (t.get("label") or "").lower())
                 if w not in _STOPWORDS and len(w) > 3}
        if ltoks and len(ttoks & ltoks) >= 2:
            d = str(t.get("date") or "")
            if hit is None or d > hit:
                hit = d
    return hit


def _fallback_clusters(articles, budget, sent_topics=None):
    """Cluster-free safety net: dedupe by normalised title, honour the cross-day
    no-repeat rule, keep the freshest N with a research cap. Used only when the
    Curator LLM call fails."""
    sent_topics = sent_topics or []
    seen, picked = set(), []
    research_cap = max(1, budget["items"] // 3)
    research_used = 0
    for a in articles:
        key = _title_key(a)
        if key in seen:
            continue
        # Skip a story we already sent this week unless this article is newer.
        prior = _sent_topic_conflict(a, sent_topics)
        if prior and (a.get("publishedAt") or "")[:10] <= prior:
            continue
        if _is_research(a):
            if research_used >= research_cap:
                continue
            research_used += 1
        seen.add(key)
        a["_topic_label"] = re.sub(r"\s+-\s+[^-]+$", "", (a.get("title") or ""))[:80]
        picked.append(a)
        if len(picked) >= budget["items"]:
            break
    return picked


# --- CURATOR (role 4): cluster the week's news into topics, rank by profile ---
def curate(profile, articles):
    """Cluster candidates into distinct story-topics, keep ONE representative per
    topic, and rank the topics by relevance to THIS reader.

    A big story that spawns six articles becomes a single item -- the reader
    sees each topic at most once. A topic already covered in the last 7 days is
    only allowed back if there is genuinely NEWER news on it (the cross-day
    follow-up rule), not a leftover from when it first broke.

    Returns (shortlist, rationale) where shortlist is one article per kept topic
    (ranked, freshest-relevant first) and each shortlist article carries a
    ``_topic_label``. On any failure a deterministic clustered fallback is used
    -- a curator outage must not kill the send.
    """
    fmt = profile.get("format") or DEFAULT_FORMAT
    budget = FORMAT_BUDGETS.get(fmt, FORMAT_BUDGETS[DEFAULT_FORMAT])
    _stage(profile["id"], "CURATOR", f"{len(articles)} candidates -> top {budget['items']} topics ({fmt})")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    candidates = []
    for i, a in enumerate(articles[:MAX_ARTICLES_TO_MODEL]):
        candidates.append({
            "id": i,
            "title": (a.get("title") or "")[:200],
            "source": (a.get("source") or {}).get("name", ""),
            "date": (a.get("publishedAt") or "")[:10],
            "kind": "research" if _is_research(a) else "news",
            "desc": (a.get("description") or "")[:240],
        })

    sent_topics = load_sent_topics(profile["id"])
    context = {
        "role_context": profile.get("role_context", ""),
        "goals": profile.get("goals") or [],
        "info_needs": profile.get("info_needs") or [],
        # v1 profiles have no goals/info_needs; topics+watchlist still anchor scoring.
        "topics": profile.get("topics", []),
        "watchlist": profile.get("watchlist", []),
        "exclude": profile.get("exclude", []),
        # The tuning loop: recent reader notes shift what scores well.
        "recent_reader_feedback": [f.get("note", "") for f in profile.get("feedback_log", [])
                                   if isinstance(f, dict) and f.get("note")][-3:],
    }
    research_cap = max(1, budget["items"] // 3)

    try:
        out = _openai_json([
            {"role": "system", "content": (
                "You are the Curator for a personalised DAILY brief. Your job is to turn a pile of "
                "candidate articles into a RANKED list of distinct story-TOPICS for THIS reader.\n"
                f"Today is {today}.\n"
                "STEP 1 -- CLUSTER: group candidates that are about the SAME underlying story into one "
                "topic (e.g. six articles about one company's cyber-attack are ONE topic). For each "
                "topic pick a single best representative article: the most substantive one, and among "
                "similar ones the FRESHEST (use 'date').\n"
                "STEP 2 -- RANK: order the topics by how much they matter to this reader today, folding "
                "in recency -- a fresh on-topic story outranks a stale one; strongly prefer items from "
                "today or the last 24-48h. Drop anything matching their exclude list or with no bearing "
                "on their work.\n"
                "STEP 3 -- BALANCE: this is a NEWS brief, not a research digest. Keep academic/preprint "
                f"('research') topics to at most {research_cap} of the final list; news leads.\n"
                "STEP 4 -- NO REPEATS ACROSS DAYS: you are given 'already_sent_topics' (label + the date "
                "each was last emailed). If a candidate topic is the SAME ongoing story as one of those, "
                "only keep it when its representative article is NEWER than that date AND carries a "
                "genuine development (set is_followup=true and say what's new in 'reason'). If it is just "
                "the same news again with nothing new, DROP it.\n"
                'Return JSON: {"topics":[{"label":"short topic name","best_id":int,'
                '"member_ids":[int,...],"score":number,"kind":"news|research",'
                '"is_followup":true|false,"reason":"one line: why it matters to THIS reader (and, if a '
                'follow-up, what is new)"}]}. '
                "Order 'topics' best-first. 'label' must be specific enough to recognise the same story "
                "again tomorrow. Every best_id and member_id must be a candidate id."
            )},
            {"role": "user", "content":
                "READER:\n" + json.dumps(context, ensure_ascii=False) +
                "\n\nALREADY_SENT_TOPICS (last 7 days -- do not repeat unless genuinely newer):\n" +
                json.dumps(sent_topics, ensure_ascii=False) +
                "\n\nCANDIDATES (each has 'date' = publish date, 'kind' = news|research):\n" +
                json.dumps(candidates, ensure_ascii=False)},
        ], max_tokens=2500)

        topics = out.get("topics")
        if not isinstance(topics, list) or not topics:
            raise RuntimeError("contract violation: topics missing")
    except Exception as e:
        # Only a genuine LLM/parse outage falls back -- never the "nothing new"
        # case below, which is a legitimate decision, not a failure.
        print(f"!! STAGE CURATOR failed for {profile['id']}: {e}")
        print(f"   falling back to clustered top-{budget['items']}")
        shortlist = _fallback_clusters(articles, budget, sent_topics)
        return shortlist, {i: "" for i in range(len(shortlist))}

    # The Curator LLM succeeded. Enforce the rules in code (it advises; we enforce).
    sent_by_label = {t["label"].lower(): t["date"] for t in sent_topics}
    clean, used_ids = [], set()
    for t in topics:
        try:
            bid = int(t["best_id"])
            score = float(t.get("score", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= bid < len(articles)) or bid in used_ids or score <= 0:
            continue
        label = str(t.get("label", "")).strip()[:80] or (articles[bid].get("title") or "")[:80]
        kind = "research" if (t.get("kind") == "research" or _is_research(articles[bid])) else "news"
        reason = str(t.get("reason", ""))[:200]
        is_followup = bool(t.get("is_followup"))

        # Cross-day follow-up rule: if this topic was sent before, its
        # representative must be NEWER than the last send, else it is a leftover.
        prior = sent_by_label.get(label.lower())
        if prior:
            rep_date = (articles[bid].get("publishedAt") or "")[:10]
            if rep_date <= prior:
                print(f"   skip (already covered, no newer news) {label[:60]}")
                continue
            is_followup = True

        used_ids.add(bid)
        clean.append({"id": bid, "label": label, "score": score, "kind": kind,
                      "reason": reason, "is_followup": is_followup})

    if not clean:
        # The reader has genuinely already seen every relevant story this week;
        # re-sending a rehash would break the one-topic promise. Send nothing.
        print(f"-> {profile['id']}: no genuinely new topics today; nothing to send")
        return [], {}

    clean.sort(key=lambda c: c["score"], reverse=True)

    # Ranking deliberation: a second agent reviews balance / dedup / order.
    clean = critique_ranking(profile, clean, articles, budget)

    # Enforce the research cap after ranking, then trim to the format budget.
    final, research_used = [], 0
    for c in clean:
        if c["kind"] == "research":
            if research_used >= research_cap:
                continue
            research_used += 1
        final.append(c)
        if len(final) >= budget["items"]:
            break

    shortlist, rationale = [], {}
    for a in articles:
        a.pop("_topic_label", None)  # avoid stale labels leaking across profiles
    for pos, c in enumerate(final):
        art = articles[c["id"]]
        art["_topic_label"] = c["label"]
        shortlist.append(art)
        rationale[pos] = c["reason"]
        tag = " (follow-up)" if c["is_followup"] else ""
        print(f"   keep [{c['score']:.0f}/10] {c['kind']}{tag}: {c['label'][:60]} -- {art.get('title','')[:50]}")
    print(f"-> curator kept {len(shortlist)} topic(s) from {len(candidates)} candidates "
          f"({research_used} research)")
    return shortlist, rationale


# --- RANKING CRITIC (role 4b): deliberates with the Curator on priority ---
def critique_ranking(profile, ranked, articles, budget):
    """A second agent reviews the Curator's ranked topics and can reorder or drop
    them -- the two 'talk' about what deserves priority for this reader. Returns
    the (possibly) revised ranked list; on any failure the input is returned
    unchanged so a critic outage never blocks the send."""
    if len(ranked) < 2:
        return ranked
    _stage(profile["id"], "RANK-CRITIC", f"{len(ranked)} topics")

    proposal = [{
        "id": c["id"],
        "label": c["label"],
        "score": c["score"],
        "kind": c["kind"],
        "date": (articles[c["id"]].get("publishedAt") or "")[:10],
        "reason": c["reason"],
    } for c in ranked]
    context = {
        "role_context": profile.get("role_context", ""),
        "goals": profile.get("goals") or [],
        "topics": profile.get("topics", []),
        "watchlist": profile.get("watchlist", []),
        "format_items": budget["items"],
        "research_cap": max(1, budget["items"] // 3),
    }
    try:
        out = _openai_json([
            {"role": "system", "content": (
                "You are the Ranking Critic. The Curator proposed a ranked list of story-topics for "
                "this reader's daily brief. Review it as a second opinion and return the FINAL ranking.\n"
                "Judge: (1) is the most important, most decision-relevant story for THIS reader at the "
                "top? Reorder if not. (2) Is it too research-heavy? News should lead; keep 'research' "
                "topics to at most research_cap and never at the very top unless truly the day's biggest "
                "item. (3) Are any two entries actually the same story? Drop the weaker duplicate. "
                "(4) Drop anything off-profile or stale.\n"
                'Return JSON: {"ranked_ids":[int,...],"notes":"one line on what you changed and why"} '
                "-- ranked_ids is the final order, best first, a subset/reordering of the proposed ids "
                "(use the topic 'id' values). Keep the strongest items; you may shorten the list."
            )},
            {"role": "user", "content":
                "READER:\n" + json.dumps(context, ensure_ascii=False) +
                "\n\nPROPOSED RANKING (best-first as the Curator sees it):\n" +
                json.dumps(proposal, ensure_ascii=False)},
        ], max_tokens=800)

        order = out.get("ranked_ids")
        if not isinstance(order, list) or not order:
            return ranked
        by_id = {c["id"]: c for c in ranked}
        revised, seen = [], set()
        for rid in order:
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                continue
            if rid in by_id and rid not in seen:
                revised.append(by_id[rid])
                seen.add(rid)
        if not revised:
            return ranked
        notes = str(out.get("notes", ""))[:200]
        print(f"-> rank-critic: {len(ranked)} -> {len(revised)} topic(s){' -- ' + notes if notes else ''}")
        return revised
    except Exception as e:
        print(f"!! STAGE RANK-CRITIC failed for {profile['id']}: {e} -- keeping curator order")
        return ranked


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

        "Task: The items below were selected AND ranked by a curator and a ranking critic "
        "(WhyRelevant notes included where available). Each item is a DISTINCT story-topic -- they are "
        "already de-duplicated, so write exactly ONE block per item and never merge or split them, and "
        "never cover the same story twice. Keep the given order: it is the deliberated priority for THIS "
        "reader. You may drop an item only if on closer reading it is clearly irrelevant or excluded. "
        "Quality over quantity.\n\n"

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

    # The Curator clusters into topics (one item per story) and the Ranking
    # Critic deliberates on priority; rationale is already indexed by shortlist
    # position, and each shortlist article carries its `_topic_label`.
    shortlist, rationale = curate(profile, articles)
    if not shortlist:
        print(f"-> {profile['id']}: nothing genuinely new to send today.")
        return False

    # Ensure every item in the brief has a picture that fits its topic.
    _stage(profile["id"], "IMAGES")
    enrich_images(shortlist)

    html, err = analyze(profile, shortlist, rationale=rationale)
    if not html:
        print(f"!! STAGE WRITER failed for {profile['id']}:\n{err}")
        return False

    passed, notes = critique(profile, html)
    if not passed and notes:
        _stage(profile["id"], "WRITER", "regenerating once with critic notes")
        retry_html, retry_err = analyze(profile, shortlist, rationale=rationale, critic_notes=notes)
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
    # Record URLs AND topics so tomorrow's brief won't repeat either: the same
    # URL never returns, and a topic only returns if there is genuinely newer
    # news on it (the cross-day follow-up rule).
    record_sent(profile["id"], [
        {"url": a.get("url"), "topic": a.get("_topic_label", "")} for a in shortlist
    ])
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
