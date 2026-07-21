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
  // Correlation id: shown in the UI on failure and logged with every server-side
  // error line, so a user report ("cid abc123") maps straight to the function log.
  const cid = (globalThis.crypto?.randomUUID?.() || String(Math.random()).slice(2)).slice(0, 8);
  const fail = (status, payload) => {
    console.error(`[save-profile ${cid}]`, JSON.stringify(payload));
    return json({ ...payload, cid }, status);
  };

  if (req.method !== "POST") return fail(405, { error: "Method not allowed" });

  const token = process.env.GITHUB_TOKEN;
  const repo = process.env.GITHUB_REPO;
  const branch = process.env.GITHUB_BRANCH || "main";
  if (!token || !repo) {
    return fail(500, {
      error: "Server not configured",
      detail: `missing env: ${[!token && "GITHUB_TOKEN", !repo && "GITHUB_REPO"].filter(Boolean).join(", ")}. ` +
        "Set them in Netlify site env vars and REDEPLOY (env changes only apply to new deploys).",
    });
  }

  let input;
  try {
    input = await req.json();
  } catch {
    return fail(400, { error: "Invalid JSON body" });
  }

  const { profile, errors } = validateProfile(input);
  if (errors.length) {
    return fail(422, { error: "Profile validation failed", details: errors });
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
      return fail(502, { error: "GitHub read failed", status: getResp.status, detail });
    }

    let putResp = await fetch(`${GITHUB_API}/repos/${repo}/contents/${path}`, {
      method: "PUT",
      headers,
      body: JSON.stringify({
        message: `feat(profile): add ${profile.id}`,
        content: contentB64,
        branch,
        ...(sha ? { sha } : {}),
      }),
    });

    // 409 = the file changed (or appeared) between our read and write; refetch
    // the sha once and retry so a stale sha doesn't fail the whole intake.
    if (putResp.status === 409) {
      const re = await fetch(
        `${GITHUB_API}/repos/${repo}/contents/${path}?ref=${encodeURIComponent(branch)}`,
        { headers },
      );
      const freshSha = re.ok ? (await re.json()).sha : undefined;
      putResp = await fetch(`${GITHUB_API}/repos/${repo}/contents/${path}`, {
        method: "PUT",
        headers,
        body: JSON.stringify({
          message: `feat(profile): add ${profile.id}`,
          content: contentB64,
          branch,
          ...(freshSha ? { sha: freshSha } : {}),
        }),
      });
    }

    if (!putResp.ok) {
      const detail = (await putResp.text()).slice(0, 300);
      const hint =
        putResp.status === 401 ? "Token rejected — is GITHUB_TOKEN valid and not expired?" :
        putResp.status === 403 ? "Token lacks access — fine-grained PAT needs Contents: Read & Write on THIS repo." :
        putResp.status === 404 ? `Repo or branch not found — is GITHUB_REPO '${repo}' and branch '${branch}' correct? (404 also means the token cannot see the repo.)` :
        undefined;
      return fail(502, { error: "GitHub write failed", status: putResp.status, detail, hint });
    }

    const data = await putResp.json();
    console.log(`[save-profile ${cid}] committed ${path} (${sha ? "update" : "create"})`);
    return json({
      ok: true,
      id: profile.id,
      path,
      updated: Boolean(sha),
      commit: data?.commit?.html_url || null,
      cid,
    });
  } catch (e) {
    return fail(502, { error: "Commit failed", detail: String(e) });
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
