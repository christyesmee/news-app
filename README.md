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
 │ chat.mjs         │  save-profile.mjs  │  <id>.json   │  cron gate  │ NewsAPI + Gemini │
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
   ever touching cron. Reuses the existing NewsAPI + Gemini + Gmail-SMTP pipeline.

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
regions, restricted to `priority_sources`) and its own Gemini prompt (ranked to
that person's role, honouring `exclude`).

## Secrets

| Where           | Key                            | Purpose                                             |
|-----------------|--------------------------------|-----------------------------------------------------|
| GitHub Actions  | `NEWS_API_KEY`                 | NewsAPI (see caveat)                                |
| GitHub Actions  | `GEMINI_API_KEY`               | brief generation                                    |
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
- `google.generativeai` is deprecated in favour of `google-genai`; not migrated.

## Decisions taken on the plan's open questions

- **Profile persistence:** auto-commit via a fine-grained GitHub PAT (true
  self-serve), with strict server-side field validation. This meets the
  definition of done — a completed chat lands a committed profile with no manual
  step.
- **Phase 5 (newsletter forwarding):** documented as a follow-up, not built. It
  needs a dedicated inbox (IMAP) or mail-webhook service provisioned first. It
  remains the legitimate way to fold a user's own subscriptions into their brief
  (no paywalled scraping). To add it: provision the inbox, give each profile a
  `forward_token`, and have the engine pull that user's recently forwarded mail
  and pass it to Gemini as an extra trusted-sources block before generating.

## Not in scope

Scraping or ingesting paywalled subscription content (FT, Stratechery, etc.)
using a user's logins — this breaks those services' terms and copyright. Phase 5
(newsletter forwarding) is the legitimate alternative.
