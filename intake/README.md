# Intake — profile-first (v2)

A hosted web app that builds a rich daily-brief profile FROM the user's own
material, instead of interviewing them. The user's time budget is 5–10 minutes:
they paste raw material, answer at most 3 questions, and confirm an editable
pre-filled profile.

```
intake/
  index.html                    the whole flow (material → progress → ≤3 questions → review)
  netlify.toml                  Netlify build/redirect config
  netlify/functions/pipeline.mjs      staged agent pipeline (Enricher → Researcher →
                                      Profiler → Expander → Query-packer), OpenAI
  netlify/functions/save-profile.mjs  validates + commits profiles/<id>.json via GitHub API
```

## Flow

1. **Raw material (one screen).** Name, work email, company, one big textarea
   for LinkedIn About/Experience or CV, an optional second textarea for whatever
   defines their work right now (strategy doc, research proposal, OKRs), and a
   consent checkbox for public web lookup. LinkedIn URLs are deliberately not
   fetched — profile pages sit behind a login wall; paste is the reliable route.
2. **Agent pipeline (~30–60s, live progress log).** The browser drives one
   serverless call per stage (keeps each call inside the function timeout):
   - **Enricher** — exhaustive entity extraction from ALL pasted material. Every
     named company, product, protocol, lab, benchmark, venue and person is a
     candidate. Losing named entities was the v1 bug; completeness is the bar.
   - **Researcher** (consent-gated) — OpenAI Responses API with the built-in
     `web_search` tool: public footprint, employer news, competitors.
   - **Profiler** — role narrative, trajectory, goals, info needs + up to 3 gap
     questions it genuinely couldn't infer.
   - **Expander** — the tracking config: 8–15 topics, 20–40 watchlist entities
     (including inferred adjacents), 10+ sources, arXiv categories, exclusions.
   - **Query-packer** — themed NewsAPI query packs, each ≤450 chars.
   Every stage has a strict JSON contract and fails loudly with its stage name
   and a correlation id. Any stage failure degrades gracefully (deterministic
   fallbacks) — the flow never dies.
3. **Gap questions (hard cap 3).** Chat-style. "You decide for me →" records the
   deflection as confirmation authority: the Expander decides and the review
   screen shows every decision.
4. **Review & activate.** The full inferred profile in editable fields; delivery
   time, timezone and format (Punchy / Standard / Deep) are UI controls here,
   not chat questions. Activation commits `profiles/<id>.json` via
   `save-profile` (correlation-id diagnostics on failure).
5. **Instant first brief.** Right after activation the page renders a compact
   first brief on screen (`preview_fetch` + `preview_write`) and states the
   daily email schedule. In parallel, the profile commit push-triggers the
   GitHub Action, which emails the full first brief (curator + critic + arXiv)
   within a few minutes — nobody waits for their scheduled hour.

## Deploy on Netlify

1. New site from this Git repo, **Base directory:** `intake`.
2. Environment variables (Site configuration → Environment variables), then
   **redeploy — env changes only apply to new deploys**:
   - `OPENAI_API_KEY` — pipeline (intake-time agents + web search).
   - `NEWS_API_KEY` — the instant on-screen first brief (`preview_fetch`).
   - `GITHUB_TOKEN` — fine-grained PAT, **Contents: Read & Write** on this repo only.
   - `GITHUB_REPO` — e.g. `christyesmee/news-app`.
   - `GITHUB_BRANCH` — `main`.
   - `OPENAI_MODEL` — optional, default `gpt-4o-mini`.

## Notes

- Serverless timeout: each stage is one call and fits the default 10s window on
  gpt-4o-mini. If a stage times out on your plan, the UI logs it as a warning
  and falls back — nothing blocks activation.
- Cost per intake ≈ 4–6 gpt-4o-mini calls + up to 1 web search ≈ **€0.02–0.05**.
