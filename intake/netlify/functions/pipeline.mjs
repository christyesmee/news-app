// Netlify Function (v2) — POST /.netlify/functions/pipeline
//
// The intake-time agent pipeline: staged OpenAI calls with strict JSON
// contracts. The browser orchestrates the stages (one HTTP call each, which
// keeps every call inside the serverless timeout and gives the user a live
// progress log):
//
//   stage=enrich    material -> { entity_pool: [{name, type, evidence}] }
//   stage=research  name/company (+consent) -> { findings, extra_entities }   [web search]
//   stage=profile   material+pool+findings  -> { role_context, trajectory,
//                                                goals, info_needs, gap_questions }
//   stage=expand    everything + answers    -> { topics, watchlist,
//                                                priority_sources, arxiv_categories,
//                                                exclude, language }
//   stage=queries   topics+watchlist        -> { query_packs: [{name, q}] }
//
// Post-activation (the instant first brief shown on screen):
//   stage=preview_fetch  query_packs/language/sources -> { articles: [...] }   [NewsAPI]
//   stage=preview_write  articles + profile           -> { html }
//
// Every stage validates the model's JSON against its contract and fails
// LOUDLY with the stage name and a correlation id — never a silent skip.
//
// Env: OPENAI_API_KEY (required), OPENAI_MODEL (optional, default gpt-4o-mini),
//      NEWS_API_KEY (required for preview_fetch only)

const CHAT_URL = "https://api.openai.com/v1/chat/completions";
const RESPONSES_URL = "https://api.openai.com/v1/responses";
const MODEL = process.env.OPENAI_MODEL || "gpt-4o-mini";

const MAX_MATERIAL = 28000; // chars of pasted material forwarded to the model

export default async (req) => {
  const cid = (globalThis.crypto?.randomUUID?.() || String(Math.random()).slice(2)).slice(0, 8);

  if (req.method !== "POST") return json({ error: "Method not allowed", cid }, 405);
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) return json({ error: "OPENAI_API_KEY is not configured on the server.", cid }, 500);

  let body;
  try {
    body = await req.json();
  } catch {
    return json({ error: "Invalid JSON body", cid }, 400);
  }

  const stage = String(body.stage || "");
  const impl = STAGES[stage];
  if (!impl) return json({ error: `Unknown stage '${stage}'`, cid }, 400);

  try {
    const data = await impl(body, apiKey);
    console.log(`[pipeline ${cid}] stage=${stage} ok`);
    return json({ ok: true, stage, data, cid });
  } catch (e) {
    const msg = String(e?.message || e).slice(0, 500);
    console.error(`[pipeline ${cid}] stage=${stage} FAILED: ${msg}`);
    return json({ error: `Stage '${stage}' failed`, detail: msg, stage, cid }, 502);
  }
};

// ---------------------------------------------------------------- stages

const STAGES = {
  // 1. ENRICHER — exhaustive entity extraction. Completeness over precision:
  //    losing named entities from pasted material was the v1 bug.
  async enrich(body, apiKey) {
    const material = clamp(body.material, MAX_MATERIAL);
    if (material.length < 40) return { entity_pool: [] };

    const out = await chatJSON(apiKey, [
      sys(
        "You are the Enricher, an analyst extracting entities from a person's pasted material " +
        "(bio, CV, strategy doc, research proposal).\n" +
        "Extract EVERY named entity. Do not summarise, do not select, do not cap the count — " +
        "completeness over precision; a later stage filters.\n" +
        "Candidates: companies, products, tools, protocols, standards, benchmarks, datasets, " +
        "models, papers, publication venues, conferences, labs, organisations, foundations, " +
        "people, and recurring themes/topics.\n" +
        'Return JSON: {"entity_pool":[{"name":str,"type":"company|product|protocol|person|org|venue|topic","evidence":str}]}\n' +
        "evidence = at most 8 words saying where/why it appears. Use type 'product' for tools/" +
        "models/benchmarks/datasets, 'protocol' for protocols and standards, 'venue' for journals/" +
        "conferences/publication venues, 'topic' for themes."
      ),
      usr("MATERIAL:\n" + material),
    ], 3500);

    const pool = asList(out.entity_pool)
      .map((e) => ({
        name: clamp(e?.name, 80),
        type: TYPES.has(String(e?.type)) ? String(e.type) : "topic",
        evidence: clamp(e?.evidence, 80),
      }))
      .filter((e) => e.name);
    if (!Array.isArray(out.entity_pool)) throw new Error("contract violation: entity_pool missing");
    return { entity_pool: dedupeBy(pool, (e) => e.name.toLowerCase()).slice(0, 120) };
  },

  // 2. RESEARCHER — consent-gated public web search to sharpen the profile.
  async research(body, apiKey) {
    const name = clamp(body.name, 120);
    const company = clamp(body.company, 160);
    const roleHint = clamp(body.role_hint, 240);
    if (!name) throw new Error("research requires a name");

    const resp = await fetch(RESPONSES_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify({
        model: MODEL,
        tools: [{ type: "web_search" }],
        input:
          `Research the public professional footprint of ${name}` +
          (company ? ` (${company})` : "") + (roleHint ? `, ${roleHint}` : "") + ". " +
          "Find: their published work or talks, their employer's recent news, and the " +
          "competitors/peers/adjacent organisations around them. Only public sources.\n" +
          "Then output ONLY a JSON object (no prose before or after):\n" +
          '{"findings": "<=250 word briefing of what you found",' +
          ' "extra_entities":[{"name":str,"type":"company|product|protocol|person|org|venue|topic","evidence":str}]}',
      }),
    });
    if (!resp.ok) throw new Error(`OpenAI responses ${resp.status}: ${(await resp.text()).slice(0, 300)}`);
    const data = await resp.json();

    // The Responses API returns output items; the message item carries the text.
    let text = data.output_text;
    if (!text && Array.isArray(data.output)) {
      const msg = data.output.find((o) => o.type === "message");
      text = msg?.content?.map((c) => c.text || "").join("\n") || "";
    }
    const out = extractJSON(text);
    if (!out || typeof out.findings !== "string") throw new Error("contract violation: findings missing");
    return {
      findings: clamp(out.findings, 2000),
      extra_entities: asList(out.extra_entities)
        .map((e) => ({
          name: clamp(e?.name, 80),
          type: TYPES.has(String(e?.type)) ? String(e.type) : "topic",
          evidence: clamp(e?.evidence, 80),
        }))
        .filter((e) => e.name)
        .slice(0, 40),
    };
  },

  // 3. PROFILER — the deep-understanding layer: who is this person, where are
  //    they heading, what information moves them forward. Also decides the
  //    (max 3) gap questions worth the user's time.
  async profile(body, apiKey) {
    const material = clamp(body.material, MAX_MATERIAL / 2);
    const pool = asList(body.entity_pool).slice(0, 120);
    const findings = clamp(body.research_findings, 2000);

    const out = await chatJSON(apiKey, [
      sys(
        "You are the Profiler, a sharp career coach building a working profile of a professional " +
        "from their own material, an extracted entity list, and optional web research.\n" +
        "Understand: who they are, their trajectory, what they are trying to achieve in the next " +
        "6-12 months, and what INFORMATION would move them forward day to day.\n" +
        'Return JSON: {"role_context": "2-3 sentences: role, seniority, what they are accountable for",' +
        ' "trajectory": "1-2 sentences: where their career/work is heading",' +
        ' "goals": [3-6 concrete goals], "info_needs": [4-8 kinds of information that serve those goals],' +
        ' "gap_questions": [0-3 questions]}\n' +
        "gap_questions: ONLY what you genuinely cannot infer, ordered by information value. " +
        "Good candidates: the decision/goal that matters most in the next 6-12 months; hard " +
        "exclusions; how they'd judge the brief useful after 4 weeks. NEVER ask the user to pick " +
        "topics, sources, or watchlist entries — that is your job, not theirs. If the material " +
        "already answers everything, return an empty gap_questions list."
      ),
      usr(
        "MATERIAL:\n" + material +
        "\n\nENTITY POOL:\n" + JSON.stringify(pool) +
        (findings ? "\n\nWEB RESEARCH FINDINGS:\n" + findings : "")
      ),
    ], 1400);

    if (typeof out.role_context !== "string" || !Array.isArray(out.goals)) {
      throw new Error("contract violation: role_context/goals missing");
    }
    return {
      role_context: clamp(out.role_context, 600),
      trajectory: clamp(out.trajectory, 400),
      goals: strList(out.goals, 6, 160),
      info_needs: strList(out.info_needs, 8, 160),
      gap_questions: strList(out.gap_questions, 3, 200),
    };
  },

  // 4. EXPANDER — turns understanding into the tracking config. Rich, not
  //    minimal: 8-15 topics, 20-40 watchlist entities incl. inferred adjacents.
  async expand(body, apiKey) {
    const pool = asList(body.entity_pool).slice(0, 120);
    const profiler = body.profiler || {};
    const answers = asList(body.answers).slice(0, 3);
    const findings = clamp(body.research_findings, 1500);

    const out = await chatJSON(apiKey, [
      sys(
        "You are the Expander. Input: an entity pool extracted from a person's material, a " +
        "profile of who they are, web research, and their answers to up to 3 questions. " +
        "Output their daily-brief tracking configuration.\n" +
        'Return JSON: {"topics":[...], "watchlist":[...], "priority_sources":[...],' +
        ' "arxiv_categories":[...], "exclude":[...], "language":"2-letter code"}\n' +
        "Rules:\n" +
        "- topics: 8-15 themes to track, phrased as news-searchable phrases.\n" +
        "- watchlist: 20-40 NAMED entities. Start from the entity pool (companies, products, " +
        "protocols, orgs, people, benchmarks) and ADD inferred adjacents: competitors, partners, " +
        "upstream/downstream players, standards bodies the person did not name but should track. " +
        "Do not drop pool entities that plausibly matter — losing named entities is the failure " +
        "mode you exist to prevent.\n" +
        "- priority_sources: 10+ bare domains (e.g. techcrunch.com, reuters.com). Include " +
        "arxiv.org and the domains of any venues in the pool when the person is research-adjacent.\n" +
        "- arxiv_categories: arXiv category codes (e.g. cs.MA, cs.CL, cs.DC) when research-" +
        "relevant, else [].\n" +
        "- exclude: things they do not want, from their answers/material.\n" +
        "- If an answer was a deflection ('you choose'), DECIDE yourself — that is confirmation " +
        "authority, not a skip."
      ),
      usr(
        "ENTITY POOL:\n" + JSON.stringify(pool) +
        "\n\nPROFILE:\n" + JSON.stringify(profiler) +
        (findings ? "\n\nWEB RESEARCH:\n" + findings : "") +
        (answers.length ? "\n\nQ&A:\n" + JSON.stringify(answers) : "")
      ),
    ], 2200);

    if (!Array.isArray(out.topics) || !Array.isArray(out.watchlist)) {
      throw new Error("contract violation: topics/watchlist missing");
    }
    return {
      topics: strList(out.topics, 15, 80),
      watchlist: strList(out.watchlist, 40, 80),
      priority_sources: strList(out.priority_sources, 30, 120),
      arxiv_categories: strList(out.arxiv_categories, 8, 20),
      exclude: strList(out.exclude, 30, 120),
      language: /^[a-z]{2}(-[a-z]{2})?$/.test(String(out.language || "")) ? out.language : "en",
    };
  },

  // 5. QUERY-PACKER — NewsAPI q is capped (~500 chars); a 20-40 entity
  //    watchlist cannot be one query. Build themed packs, each within budget.
  async queries(body, apiKey) {
    const topics = strList(body.topics, 15, 80);
    const watchlist = strList(body.watchlist, 40, 80);

    const out = await chatJSON(apiKey, [
      sys(
        "You group a person's tracked topics and watchlist entities into 3-6 THEMED NewsAPI " +
        "queries (query packs), so related terms are searched together and results stay relevant.\n" +
        'Return JSON: {"query_packs":[{"name":"short theme name","q":"NewsAPI query"}]}\n' +
        "Query syntax rules: quote multi-word terms, join with OR inside a pack, optionally AND " +
        "a short context group. Each q MUST be under 450 characters. Every topic and watchlist " +
        "entity must appear in exactly one pack."
      ),
      usr("TOPICS:\n" + JSON.stringify(topics) + "\n\nWATCHLIST:\n" + JSON.stringify(watchlist)),
    ], 1200);

    const packs = asList(out.query_packs)
      .map((p) => ({ name: clamp(p?.name, 60) || "pack", q: clamp(p?.q, 450) }))
      .filter((p) => p.q)
      .slice(0, 8);
    if (!packs.length) throw new Error("contract violation: query_packs empty");
    return { query_packs: packs };
  },

  // 6. PREVIEW FETCH — NewsAPI pull for the instant on-screen first brief.
  //    The full engine (curator/critic/arXiv) still emails the real one.
  async preview_fetch(body) {
    const newsKey = process.env.NEWS_API_KEY;
    if (!newsKey) throw new Error("NEWS_API_KEY is not configured on the server");

    const packs = asList(body.query_packs)
      .map((p) => clamp(p?.q, 450))
      .filter(Boolean)
      .slice(0, 2);
    if (!packs.length) throw new Error("preview_fetch requires query_packs");

    const language = /^[a-z]{2}$/.test(String(body.language || "")) ? body.language : "en";
    const from = new Date(Date.now() - 5 * 24 * 3600 * 1000).toISOString().slice(0, 10);
    const domains = strList(body.priority_sources, 20, 120)
      .filter((d) => d !== "arxiv.org")
      .join(",");

    const seen = new Set();
    const articles = [];
    for (const q of packs) {
      const params = new URLSearchParams({
        q,
        from,
        sortBy: "relevance",
        language,
        pageSize: "12",
        apiKey: newsKey,
      });
      if (domains) params.set("domains", domains);
      const resp = await fetch("https://newsapi.org/v2/everything?" + params);
      if (!resp.ok) throw new Error(`NewsAPI ${resp.status}: ${(await resp.text()).slice(0, 200)}`);
      const data = await resp.json();
      if (data.status !== "ok") throw new Error(`NewsAPI: ${data.code} ${data.message}`.slice(0, 200));
      for (const a of data.articles || []) {
        const url = String(a.url || "").replace(/\/+$/, "");
        if (!url || seen.has(url)) continue;
        seen.add(url);
        articles.push({
          title: clamp(a.title, 200),
          source: clamp(a.source?.name, 60),
          url,
          desc: clamp(a.description, 300),
        });
      }
    }
    return { articles: articles.slice(0, 18) };
  },

  // 7. PREVIEW WRITE — compact brief rendered on screen right after activation.
  async preview_write(body, apiKey) {
    const articles = asList(body.articles).slice(0, 18);
    if (!articles.length) throw new Error("preview_write requires articles");
    const p = body.profile || {};

    const resp = await fetch(CHAT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify({
        model: MODEL,
        temperature: 0.4,
        max_tokens: 1200,
        messages: [
          sys(
            "You write a compact personalised news brief as an HTML FRAGMENT (no <html>/<head>/" +
            "<body>, no markdown fences). Structure: <p><strong>Bottom line:</strong> 2 sentences" +
            "</p> then 3-4 story blocks: <div class='section'><div class='news-title'>HEADLINE" +
            "</div><p><strong>What happened:</strong> ... <a href='URL'>[1]</a></p><p><strong>Why " +
            "it matters for you:</strong> ...</p></div>. Pick ONLY items relevant to the reader; " +
            "honour their exclusions; cite every claim with the article's real URL. Write in the " +
            "reader's language."
          ),
          usr(
            "READER:\n" + JSON.stringify({
              name: clamp(p.name, 120),
              role_context: clamp(p.role_context, 600),
              goals: strList(p.goals, 6, 160),
              topics: strList(p.topics, 15, 80),
              watchlist: strList(p.watchlist, 40, 80),
              exclude: strList(p.exclude, 30, 120),
              language: /^[a-z]{2}(-[a-z]{2})?$/.test(String(p.language || "")) ? p.language : "en",
            }) +
            "\n\nARTICLES:\n" + JSON.stringify(articles)
          ),
        ],
      }),
    });
    if (!resp.ok) throw new Error(`OpenAI ${resp.status}: ${(await resp.text()).slice(0, 300)}`);
    const data = await resp.json();
    let html = data?.choices?.[0]?.message?.content ?? "";
    html = html.replace(/```html/gi, "").replace(/```/g, "").trim();
    // Defence in depth before the client injects it into the page.
    html = html.replace(/<script[\s\S]*?<\/script>/gi, "").replace(/\son\w+\s*=\s*(['"]).*?\1/gi, "");
    if (html.length < 40) throw new Error("contract violation: empty preview html");
    return { html };
  },
};

// ---------------------------------------------------------------- helpers

const TYPES = new Set(["company", "product", "protocol", "person", "org", "venue", "topic"]);

const sys = (content) => ({ role: "system", content });
const usr = (content) => ({ role: "user", content });

async function chatJSON(apiKey, messages, maxTokens) {
  const resp = await fetch(CHAT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({
      model: MODEL,
      temperature: 0.3,
      max_tokens: maxTokens,
      response_format: { type: "json_object" },
      messages,
    }),
  });
  if (!resp.ok) throw new Error(`OpenAI ${resp.status}: ${(await resp.text()).slice(0, 300)}`);
  const data = await resp.json();
  const text = data?.choices?.[0]?.message?.content ?? "";
  const out = extractJSON(text);
  if (!out) throw new Error("model returned unparseable JSON");
  return out;
}

function extractJSON(text) {
  if (!text) return null;
  let candidate = text;
  const fence = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fence) candidate = fence[1];
  const start = candidate.indexOf("{");
  const end = candidate.lastIndexOf("}");
  if (start === -1 || end <= start) return null;
  try {
    return JSON.parse(candidate.slice(start, end + 1));
  } catch {
    return null;
  }
}

function clamp(v, max) {
  return String(v == null ? "" : v).trim().slice(0, max);
}
function asList(v) {
  return Array.isArray(v) ? v : [];
}
function strList(v, maxItems, maxLen) {
  return dedupeBy(
    asList(v).map((x) => clamp(x, maxLen)).filter(Boolean),
    (s) => s.toLowerCase(),
  ).slice(0, maxItems);
}
function dedupeBy(arr, key) {
  const seen = new Set();
  return arr.filter((x) => {
    const k = key(x);
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
