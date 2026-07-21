// Netlify Function (v2) — POST /.netlify/functions/save-profile
//
// Takes a profile from the intake page, VALIDATES and whitelists every field
// server-side, then commits profiles/<id>.json to the repo through the GitHub
// Contents API. The engine's next hourly run picks the new profile up.
//
// Security:
//   * The output object is rebuilt field-by-field from a fixed whitelist — no
//     field from the request is ever passed through untouched, so the endpoint
//     cannot write arbitrary keys.
//   * `id` must match ^[a-z0-9][a-z0-9-]{0,63}$ so the file path can never be
//     traversed (no "/", no "..", no "example").
//   * The GitHub token MUST be a fine-grained PAT scoped to Contents:R/W on this
//     repo only. Never use a classic all-repo token.
//
// Env:
//   GITHUB_TOKEN   fine-grained PAT (Contents: Read/Write on this repo)
//   GITHUB_REPO    "owner/repo"  (e.g. christyesmee/news-app)
//   GITHUB_BRANCH  target branch (default "main")

const GITHUB_API = "https://api.github.com";

export default async (req) => {
  if (req.method !== "POST") return json({ error: "Method not allowed" }, 405);

  const token = process.env.GITHUB_TOKEN;
  const repo = process.env.GITHUB_REPO;
  const branch = process.env.GITHUB_BRANCH || "main";
  if (!token || !repo) {
    return json({ error: "Server not configured (GITHUB_TOKEN / GITHUB_REPO)." }, 500);
  }

  let input;
  try {
    input = await req.json();
  } catch {
    return json({ error: "Invalid JSON body" }, 400);
  }

  const { profile, errors } = validateProfile(input);
  if (errors.length) {
    return json({ error: "Profile validation failed", details: errors }, 422);
  }

  const path = `profiles/${profile.id}.json`;
  const contentB64 = Buffer.from(JSON.stringify(profile, null, 2) + "\n", "utf8").toString("base64");
  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "news-app-intake",
    "Content-Type": "application/json",
  };

  try {
    // If the file already exists we need its blob sha to update it.
    let sha;
    const getResp = await fetch(
      `${GITHUB_API}/repos/${repo}/contents/${path}?ref=${encodeURIComponent(branch)}`,
      { headers },
    );
    if (getResp.ok) {
      sha = (await getResp.json()).sha;
    } else if (getResp.status !== 404) {
      const detail = (await getResp.text()).slice(0, 300);
      return json({ error: "GitHub read failed", status: getResp.status, detail }, 502);
    }

    const putResp = await fetch(`${GITHUB_API}/repos/${repo}/contents/${path}`, {
      method: "PUT",
      headers,
      body: JSON.stringify({
        message: `feat(profile): add ${profile.id}`,
        content: contentB64,
        branch,
        ...(sha ? { sha } : {}),
      }),
    });

    if (!putResp.ok) {
      const detail = (await putResp.text()).slice(0, 300);
      return json({ error: "GitHub write failed", status: putResp.status, detail }, 502);
    }

    const data = await putResp.json();
    return json({
      ok: true,
      id: profile.id,
      path,
      updated: Boolean(sha),
      commit: data?.commit?.html_url || null,
    });
  } catch (e) {
    return json({ error: "Commit failed", detail: String(e) }, 502);
  }
};

// ---------- validation ----------
function validateProfile(input) {
  const errors = [];
  const profile = {};

  if (!input || typeof input !== "object") {
    return { profile, errors: ["body must be a JSON object"] };
  }

  const id = String(input.id || "").trim().toLowerCase();
  if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(id)) {
    errors.push("id must be a slug: lowercase letters, digits and hyphens (max 64 chars)");
  } else if (id === "example") {
    errors.push("id 'example' is reserved");
  } else {
    profile.id = id;
  }

  profile.active = input.active === false ? false : true;

  profile.name = clampStr(input.name, 120);
  if (!profile.name) errors.push("name is required");

  const email = clampStr(input.email, 200);
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) errors.push("valid email is required");
  else profile.email = email;

  const hour = Number(input.send_hour_utc);
  if (!Number.isInteger(hour) || hour < 0 || hour > 23) {
    errors.push("send_hour_utc must be an integer 0-23");
  } else {
    profile.send_hour_utc = hour;
  }

  const lang = clampStr(input.language, 5).toLowerCase();
  profile.language = /^[a-z]{2}(-[a-z]{2})?$/.test(lang) ? lang : "en";

  profile.role_context = clampStr(input.role_context, 600);

  profile.regions = clampList(input.regions, 20, 80);
  profile.topics = clampList(input.topics, 30, 80);
  profile.watchlist = clampList(input.watchlist, 40, 80);
  profile.priority_sources = clampList(input.priority_sources, 30, 120)
    .map(sanitizeDomain)
    .filter(Boolean);
  profile.exclude = clampList(input.exclude, 30, 120);

  if (profile.topics.length === 0) errors.push("at least one topic is required");

  return { profile, errors };
}

function clampStr(v, max) {
  return String(v == null ? "" : v).replace(/\s+/g, " ").trim().slice(0, max);
}

function clampList(v, maxItems, maxLen) {
  const arr = Array.isArray(v) ? v : String(v || "").split(",");
  const out = [];
  const seen = new Set();
  for (const item of arr) {
    const s = clampStr(item, maxLen);
    const key = s.toLowerCase();
    if (s && !seen.has(key)) {
      seen.add(key);
      out.push(s);
      if (out.length >= maxItems) break;
    }
  }
  return out;
}

function sanitizeDomain(s) {
  // Reduce to a bare hostname: strip scheme, path, and any leading "www.".
  let d = String(s).trim().toLowerCase();
  d = d.replace(/^https?:\/\//, "").replace(/\/.*$/, "").replace(/^www\./, "");
  return /^[a-z0-9.-]+\.[a-z]{2,}$/.test(d) ? d : "";
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
