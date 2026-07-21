# Intake chatbot

A small hosted web app that interviews a prospect (via the OpenAI API) and builds
a `profiles/<id>.json` for the [daily brief engine](../send_brief.py).

```
intake/
  index.html                    chat UI (mobile-first) + review/confirm step + offline fallback
  netlify.toml                  Netlify build/redirect config
  netlify/functions/chat.mjs    proxies OpenAI Chat Completions (key stays server-side)
  netlify/functions/save-profile.mjs   commits the confirmed profile to the repo (Phase 4)
```

## Deploy on Netlify

1. New site from this Git repo.
2. **Site configuration → Build & deploy → Base directory:** `intake`
   (everything in `netlify.toml` is relative to that base).
3. **Environment variables:**
   - `OPENAI_API_KEY` — used by `chat.mjs`.
   - `GITHUB_TOKEN` — a **fine-grained** PAT scoped to *Contents: Read/Write* on
     this repo only, used by `save-profile.mjs` (Phase 4).
   - `GITHUB_REPO` — `owner/news-app` (defaults can be set in the function).
4. Deploy. The chat is served at `/` and the functions at `/api/chat` and
   `/api/save-profile` (see the redirects in `netlify.toml`).

## Flow

1. The chat asks up to 7 questions (one at a time) and returns a strict JSON
   profile matching the [Phase 1 schema](../profiles/example.json).
2. The page shows the profile back, lets the user confirm email + a delivery
   time (picked in their local timezone, converted to `send_hour_utc`), and pick
   a format.
3. On confirm, the profile is committed to `profiles/<id>.json`; the next hourly
   workflow run emails that user.

If the OpenAI proxy is unreachable, the page falls back to a baked-in scripted
questionnaire that builds the same profile locally, so the flow never dies.
