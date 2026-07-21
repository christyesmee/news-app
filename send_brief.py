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

PROFILES_DIR = "profiles"
TEMPLATE_FILE = "email_template.html"
# example.json is a committed reference profile; the scheduled run never emails it.
REFERENCE_PROFILE = "example.json"

# NewsAPI look-back window and article budget.
LOOKBACK_DAYS = 5
MAX_ARTICLES = 40
MAX_ARTICLES_TO_MODEL = 30


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


def fetch_news(profile):
    query = build_query(profile)
    if not query:
        print(f"!! {profile['id']}: empty query (no topics/watchlist/regions); skipping.")
        return []

    date_from = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    params = {
        "q": query,
        "from": date_from,
        "sortBy": "relevance",
        "language": profile.get("language", "en"),
        "pageSize": MAX_ARTICLES,
        "apiKey": NEWS_API_KEY,
    }
    sources = [s.strip() for s in profile.get("priority_sources", []) if s.strip()]
    if sources:
        params["domains"] = ",".join(sources)

    try:
        response = requests.get("https://newsapi.org/v2/everything", params=params, timeout=30)
        data = response.json()
    except Exception as e:
        print(f"!! Error fetching news for {profile['id']}: {e}")
        return []

    if data.get("status") != "ok":
        print(f"!! NewsAPI error for {profile['id']}: {data.get('code')} - {data.get('message')}")
        return []

    articles = data.get("articles", [])
    print(f"-> {profile['id']}: found {len(articles)} articles")
    return articles


# --- ANALYSIS (OpenAI) ---
def build_prompt(profile, raw_text):
    name = profile.get("name") or "the reader"
    role = profile.get("role_context", "")
    topics = ", ".join(profile.get("topics", [])) or "(none specified)"
    watchlist = ", ".join(profile.get("watchlist", [])) or "(none specified)"
    regions = ", ".join(profile.get("regions", [])) or "(none specified)"
    exclude = ", ".join(profile.get("exclude", [])) or "(nothing specific)"
    language = profile.get("language", "en")

    return (
        f"Role: You are a senior intelligence analyst writing today's personalised daily brief for {name}.\n"
        f"About the reader: {role}\n"
        f"Their focus topics: {topics}\n"
        f"Their watchlist (companies / products / people to track closely): {watchlist}\n"
        f"Their regions of interest: {regions}\n"
        f"Do NOT cover / explicitly exclude: {exclude}\n\n"

        "Task: From the raw articles below, select and RANK the items most relevant to THIS reader's role and "
        "focus. Drop anything on their exclude list or irrelevant to their work. Quality over quantity.\n\n"

        "### OUTPUT (an HTML fragment only -- no <html>, <head> or <body> wrapper):\n"
        "1. Open with the bottom line:\n"
        "   <p><strong>Bottom line:</strong> 2-3 sentences with the single most important takeaway for them today.</p>\n"
        "2. Then 4-6 story blocks, most important first, each formatted exactly as:\n"
        "   <div class='section'>\n"
        "     <div class='news-title'>HEADLINE</div>\n"
        "     <img src='IMG_URL' class='story-image'>   <!-- include ONLY when a real image URL is given, never for NO_IMAGE -->\n"
        "     <p><strong>What happened:</strong> the facts, 1-2 sentences. <a href='URL'>[1]</a></p>\n"
        "     <p><strong>Why it matters for you:</strong> tie it directly to their role, watchlist or region.</p>\n"
        "     <p><strong>Watch / do:</strong> one concrete thing to monitor or act on.</p>\n"
        "   </div>\n\n"

        "### RULES:\n"
        "- Cite every claim with a clickable footnote <a href='URL'>[n]</a> using the article's real URL.\n"
        "- Keep the whole brief tight: a busy person should read it in under 15 minutes.\n"
        f"- Write in the reader's language (language code: {language}).\n"
        "- Output raw HTML only. Do NOT wrap it in markdown code fences.\n\n"

        f"RAW ARTICLES:\n{raw_text}"
    )


def analyze(profile, articles):
    if not OPENAI_API_KEY:
        return None, "OPENAI_API_KEY is not set."

    print(f"-> {profile['id']}: analysing with {OPENAI_MODEL}")
    raw_text = ""
    for i, a in enumerate(articles[:MAX_ARTICLES_TO_MODEL]):
        title = (a.get("title") or "").replace('"', "'")
        source = (a.get("source") or {}).get("name", "Unknown")
        url = a.get("url", "")
        img = a.get("urlToImage") or "NO_IMAGE"
        desc = (a.get("description") or "").replace('"', "'")
        raw_text += (
            f"ID: {i + 1} | Title: {title} | Source: {source} | URL: {url} | IMG: {img} | Desc: {desc}\n"
        )

    prompt = build_prompt(profile, raw_text)
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


def send_email(profile, html_content):
    try:
        with open(TEMPLATE_FILE, encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        template = "<html><body>{{DATE}}{{CONTENT}}</body></html>"

    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    name = (profile.get("name") or "").strip()
    header = f"Daily Brief · {name}" if name else "Daily Brief"

    final_html = template.replace("⚡ Compute Market Intel", header)
    final_html = final_html.replace("{{DATE}}", date_str)
    final_html = final_html.replace("{{CONTENT}}", html_content)

    greeting = f"Good morning{', ' + name if name else ''}. Here is your daily brief for " \
               f"{datetime.now(timezone.utc).strftime('%A, %B %d')}.\n\n"
    plain_text = greeting + _clean_html(html_content)

    msg = MIMEMultipart("alternative")
    msg["From"] = EMAIL_USER
    msg["To"] = profile["email"]
    msg["Subject"] = f"Your Daily Brief · {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(final_html, "html"))

    server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
    try:
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)
    finally:
        server.quit()


# --- ORCHESTRATION ---
def process(profile):
    print(f"=== Processing {profile['id']} <{profile.get('email')}> ===")
    articles = fetch_news(profile)
    if not articles:
        print(f"-> {profile['id']}: no articles; nothing sent.")
        return False
    html, err = analyze(profile, articles)
    if not html:
        print(f"!! {profile['id']}: analysis failed:\n{err}")
        return False
    try:
        send_email(profile, html)
    except Exception as e:
        print(f"!! {profile['id']}: failed to send email: {e}")
        return False
    print(f"vv {profile['id']}: brief sent to {profile['email']}")
    return True


def dry_run(profiles, force, current_hour):
    print(f"--- DRY RUN (current UTC hour: {current_hour}) ---")
    for p in profiles:
        gated = should_send(p, force, current_hour)
        print(f"\n[{p.get('id')}] active={p.get('active')} send_hour_utc={p.get('send_hour_utc')} "
              f"email={p.get('email')} -> would_send={gated}")
        print(f"  language: {p.get('language', 'en')} | sources: {p.get('priority_sources', [])}")
        print(f"  query: {build_query(p)}")


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
