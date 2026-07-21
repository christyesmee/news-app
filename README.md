# Personalised Daily Brief

A multi-user, self-configurable daily briefing system. A prospect completes a
short conversational intake; the system builds a profile for them; a scheduled
job emails them a personalised brief every day at a time they choose — with no
code or cron edits from anyone.

## How it fits together

Two halves that meet at one file per user: `profiles/<id>.json`.

```
 intake/ (Netlify)                         repo                         GitHub Actions
 ┌──────────────────┐   commit profile   ┌──────────────┐   hourly    ┌──────────────────┐
 │ chat UI + OpenAI │ ─────────────────▶ │ profiles/    │ ──────────▶ │ send_brief.py    │
 │ chat.mjs         │  save-profile.mjs  │  <id>.json   │  cron gate  │ NewsAPI + OpenAI │
 │                  │  (GitHub API)      │              │             │ + Gmail SMTP     │
 └──────────────────┘                    └──────────────┘             └────────┬─────────┘
                                                                               │ email
                                                                               ▼
                                                                          the user
```

1. **Delivery engine** — [`send_brief.py`](send_brief.py), run hourly by
   [`.github/workflows/brief.yml`](.github/workflows/brief.yml). Each run reads
   every profile and emails only the ones whose `send_hour_utc` matches the
   current UTC hour. That gate is how users self-configure delivery time without
   ever touching cron. Runs the NewsAPI + OpenAI + Gmail-SMTP pipeline.

2. **Intake chatbot** — [`intake/`](intake/), a Netlify-hosted web app. A
   serverless function talks to the **OpenAI API**, holds a ~7-question
   conversation, builds a profile, and commits `profiles/<id>.json` to this repo.
   See [`intake/README.md`](intake/README.md) for deploy steps.

## The engine

```bash
python send_brief.py                 # scheduled: send to profiles whose send_hour_utc == current UTC hour
python send_brief.py --force         # every active profile, ignore the hour gate
python send_brief.py --only <id>     # a single profile (profiles/<id>.json)
python send_brief.py --dry-run       # print each profile's query + who would send; no API calls, no email
```

`profiles/example.json` is a committed reference schema; scheduled runs skip it.
Each profile drives its own NewsAPI query (topics + watchlist OR-joined, AND
regions, restricted to `priority_sources`) and its own OpenAI prompt (ranked to
that person's role, honouring `exclude`). Brief generation runs on a small model
(`gpt-4o-mini` by default; override with the `OPENAI_MODEL` env var).

## Secrets

| Where           | Key                            | Purpose                                             |
|-----------------|--------------------------------|-----------------------------------------------------|
| GitHub Actions  | `NEWS_API_KEY`                 | NewsAPI (see caveat)                                |
| GitHub Actions  | `OPENAI_API_KEY`               | brief generation (small GPT model)                  |
| GitHub Actions  | `OPENAI_MODEL`                 | *(optional)* override model (default `gpt-4o-mini`) |
| GitHub Actions  | `EMAIL_USER` / `EMAIL_PASSWORD`| Gmail SMTP (app password)                           |
| Netlify         | `OPENAI_API_KEY`               | intake chatbot                                      |
| Netlify         | `GITHUB_TOKEN`                 | fine-grained PAT (Contents:R/W on this repo) to commit profiles |
| Netlify         | `GITHUB_REPO` / `GITHUB_BRANCH`| target repo (`owner/news-app`) and branch (`main`)  |

`RECIPIENT_EMAIL` from the old setup is no longer used — each profile carries its
own `email`.

## Caveats (fine for a demo, address before charging money)

- **NewsAPI** free tier is delayed ~24h and licensed for development only, not
  commercial use. A paid news source is required once this is sold.
- **GitHub cron** can be delayed several minutes under load, and GitHub disables
  scheduled workflows after 60 days of repo inactivity.
- **DST:** `send_hour_utc` is a fixed UTC hour, converted from the user's local
  pick using their browser's offset at signup. Delivery drifts one hour across a
  DST change until the profile is refreshed.
- **Model:** brief generation uses OpenAI `gpt-4o-mini` (both halves now run on
  OpenAI; Gemini is no longer used). Change it via the `OPENAI_MODEL` env var.

## Agent pipeline & cost

Five staged LLM roles with strict JSON contracts (no extra infrastructure):

| When | Role | Job |
|---|---|---|
| intake (Netlify) | **Enricher** | exhaustive entity extraction from pasted material |
| intake (Netlify) | **Researcher** | consent-gated public web search (OpenAI `web_search`) |
| intake (Netlify) | **Profiler** | role narrative, trajectory, goals, info needs, ≤3 gap questions |
| intake (Netlify) | **Expander** | 8–15 topics, 20–40 watchlist, 10+ sources, query packs, arXiv cats |
| daily run (Action) | **Curator** | scores every fetched item 0–10 vs the profile, keeps top N per format |
| daily run (Action) | **Critic** | QA of the draft; on fail → one regeneration with its notes, send best-of |

Every stage validates its JSON and fails loudly with the stage name in the log —
never a silent skip. Failures degrade (curator → top-N unscored, critic → send
draft), so an OpenAI outage never blocks delivery.

**Cost (gpt-4o-mini, $0.15/M in, $0.60/M out):** intake ≈ 4–6 calls + 1 web
search ≈ **€0.02–0.05 per signup**. Daily run per user ≈ 3–4 calls (curator,
writer, critic, occasional rewrite) ≈ €0.005–0.01/day → **€0.15–0.30 per user
per month**. Web search is the priciest single call (~€0.02); it runs once per
intake, never daily.

## Decisions taken on the plan's open questions

- **Profile persistence:** auto-commit via a fine-grained GitHub PAT (true
  self-serve), with strict server-side field validation. This meets the
  definition of done — a completed chat lands a committed profile with no manual
  step.

## Not in scope

Scraping or ingesting paywalled subscription content (FT, Stratechery, etc.)
using a user's logins — this breaks those services' terms and copyright.
