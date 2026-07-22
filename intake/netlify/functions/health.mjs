// Netlify Function (v2) — GET /.netlify/functions/health
//
// A read-only self-diagnosis for the intake's GitHub wiring. Open it in a
// browser (/api/health) to see, without doing a full signup or reading Netlify
// logs, whether `save-profile` can actually commit a profile — the single thing
// a new signup depends on to trigger the welcome email.
//
// It reports which env vars are present (booleans only — never their values),
// whether the GITHUB_TOKEN can READ the repo, whether it has WRITE (push)
// access, and whether the target branch exists. The #1 real-world failure is an
// expired fine-grained PAT: everything looks configured, but every new signup's
// commit 401s. This endpoint makes that obvious at a glance.
//
// It NEVER writes anything and NEVER returns secret values.
//
// Env: GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH (default main); PROFILE_LINK_SECRET
//      and OPENAI_API_KEY are reported as present/absent only.

const GITHUB_API = "https://api.github.com";

export default async (req) => {
  const cid = (globalThis.crypto?.randomUUID?.() || String(Math.random()).slice(2)).slice(0, 8);
  if (req.method !== "GET" && req.method !== "HEAD") {
    return json({ ok: false, error: "Method not allowed", cid }, 405);
  }

  const token = process.env.GITHUB_TOKEN;
  const repo = process.env.GITHUB_REPO;
  const branch = process.env.GITHUB_BRANCH || "main";

  const env = {
    GITHUB_TOKEN: Boolean(token),
    GITHUB_REPO: Boolean(repo),
    GITHUB_BRANCH: branch,
    PROFILE_LINK_SECRET: Boolean(process.env.PROFILE_LINK_SECRET),
    OPENAI_API_KEY: Boolean(process.env.OPENAI_API_KEY),
    NEWS_API_KEY: Boolean(process.env.NEWS_API_KEY),
  };

  const checks = [];
  const github = { reachable: null, tokenValid: null, canRead: null, canWrite: null, branchExists: null };

  if (!token || !repo) {
    checks.push(
      `Missing env: ${[!token && "GITHUB_TOKEN", !repo && "GITHUB_REPO"].filter(Boolean).join(", ")}. ` +
      "Set them in Netlify → Environment variables and REDEPLOY (env changes only apply to new deploys).",
    );
    return json({ ok: false, summary: "Not configured — save-profile cannot commit.", env, github, checks, cid }, 200);
  }

  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "news-app-health",
  };

  // 1) Can the token READ the repo, and does it carry push (write) access?
  try {
    const r = await fetch(`${GITHUB_API}/repos/${repo}`, { headers });
    github.reachable = true;
    if (r.ok) {
      const data = await r.json();
      github.tokenValid = true;
      github.canRead = true;
      github.canWrite = Boolean(data?.permissions?.push);
      if (!github.canWrite) {
        checks.push("Token can read but NOT write. A fine-grained PAT needs Contents: Read & Write on this repo.");
      }
    } else if (r.status === 401) {
      github.tokenValid = false;
      checks.push("GITHUB_TOKEN rejected (401) — the token is invalid or EXPIRED. Regenerate the fine-grained PAT and update it in Netlify, then redeploy.");
    } else if (r.status === 403) {
      github.tokenValid = true;
      github.canWrite = false;
      checks.push("GITHUB_TOKEN lacks access (403) — the fine-grained PAT must grant Contents: Read & Write on this repo.");
    } else if (r.status === 404) {
      checks.push(`Repo not found (404) — is GITHUB_REPO '${repo}' correct, and can the token see it? (404 also means no access.)`);
    } else {
      checks.push(`Unexpected GitHub status ${r.status} reading the repo.`);
    }
  } catch (e) {
    github.reachable = false;
    checks.push(`Could not reach GitHub: ${String(e).slice(0, 160)}`);
  }

  // 2) Does the target branch exist? (save-profile commits to it.)
  if (github.canRead) {
    try {
      const b = await fetch(`${GITHUB_API}/repos/${repo}/branches/${encodeURIComponent(branch)}`, { headers });
      github.branchExists = b.ok;
      if (!b.ok) {
        checks.push(`Target branch '${branch}' not found (${b.status}). Set GITHUB_BRANCH to an existing branch (usually 'main').`);
      }
    } catch {
      /* non-fatal for the diagnosis */
    }
  }

  const ok = github.tokenValid === true && github.canRead === true &&
             github.canWrite === true && github.branchExists !== false;
  const summary = ok
    ? "Healthy — save-profile can commit; new signups will trigger their welcome email."
    : "Problem detected — new signups cannot be saved (see checks).";

  return json({ ok, summary, env, github, checks, cid }, 200);
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj, null, 2), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}
