// Netlify Function (v2) — POST /.netlify/functions/chat
//
// Proxies the OpenAI Chat Completions API so the API key never reaches the
// browser. The key is read from the Netlify environment variable OPENAI_API_KEY.
//
// Request  body: { "messages": [{ "role": "user"|"assistant", "content": "..." }, ...] }
// Response body: { "reply": "..." }  (the assistant's next message)

const OPENAI_URL = "https://api.openai.com/v1/chat/completions";
const MODEL = "gpt-4o-mini";
const MAX_HISTORY = 24;

const SYSTEM_PROMPT = `You are an intake interviewer for a personalised daily news brief.

Your job: in AT MOST 7 questions, learn enough about ONE person to build them a
profile. Ask ONE question at a time. Acknowledge their previous answer in a few
words, then ask the next thing — each question must build on what they already
told you. Be warm, concise and specific. Never invent facts about them; if you
need their name or email address, ask for it.

Cover these areas (group related ones so you stay within 7 questions):
1. Their name, their role, and what they are accountable for over the next 12 months.
2. The recurring decisions they make where sharper information would help.
3. Their watchlist: specific competitors, partners, accounts, products or people to track.
4. The sources they read today that this brief should replace or supplement.
5. The region(s) they care about and the language they want the brief written in.
6. What time of day they want it delivered, in THEIR local time.
7. How deep it should go, anything to explicitly exclude, and how they will judge
   in four weeks whether the brief is useful.

When you have enough information (or after their 7th answer), STOP asking
questions and output ONLY a JSON object — no prose before or after it — inside a
\`\`\`json code fence, matching EXACTLY this shape:

\`\`\`json
{
  "id": "<slug of their name: lowercase, words hyphenated; if unknown, a short random token>",
  "active": true,
  "name": "<their name>",
  "email": "<their email>",
  "send_hour_utc": <integer 0-23: your best guess from the local time they gave; it will be reconfirmed>,
  "language": "<2-letter code, e.g. en, nl>",
  "role_context": "<1-2 sentences: role, tenure, what they are accountable for>",
  "regions": ["..."],
  "topics": ["..."],
  "watchlist": ["..."],
  "priority_sources": ["<bare domains like ft.com, reuters.com>"],
  "exclude": ["..."]
}
\`\`\`

JSON rules: topics = themes to track; watchlist = named entities; priority_sources
= bare domains only; exclude = things they do not want. Keep each array tight
(3-8 items). No comments, no trailing commas. Emit the JSON exactly once, at the
very end of the conversation.`;

export default async (req) => {
  if (req.method !== "POST") {
    return json({ error: "Method not allowed" }, 405);
  }

  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    return json({ error: "OPENAI_API_KEY is not configured on the server." }, 500);
  }

  let body;
  try {
    body = await req.json();
  } catch {
    return json({ error: "Invalid JSON body" }, 400);
  }

  const incoming = Array.isArray(body.messages) ? body.messages : [];
  const history = incoming
    .filter(
      (m) =>
        m &&
        typeof m.content === "string" &&
        (m.role === "user" || m.role === "assistant"),
    )
    .slice(-MAX_HISTORY);

  const payload = {
    model: MODEL,
    temperature: 0.5,
    messages: [{ role: "system", content: SYSTEM_PROMPT }, ...history],
  };

  try {
    const resp = await fetch(OPENAI_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const detail = (await resp.text()).slice(0, 500);
      return json({ error: "OpenAI request failed", detail }, 502);
    }

    const data = await resp.json();
    const reply = data?.choices?.[0]?.message?.content ?? "";
    return json({ reply });
  } catch (e) {
    return json({ error: "Upstream request failed", detail: String(e) }, 502);
  }
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
