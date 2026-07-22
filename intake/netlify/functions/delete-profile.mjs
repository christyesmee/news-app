// Netlify Function (v2) — POST /.netlify/functions/delete-profile
//
// Unsubscribe + erase: deletes profiles/<id>.json (and history/<id>.json) from
// the repo so the user's account and data are removed and the engine stops
// emailing them. Gated by the same signed token as the tune link
// (HMAC-SHA256(PROFILE_LINK_SECRET, id), first 32 hex) — possession of the
// emailed link is the authorisation.
//
// POST-only on purpose: email clients / link scanners issue GETs, and we must
// never delete someone's account from a prefetch. The intake page shows a
// confirmation and only POSTs here on an explicit click.
//
// Env: PROFILE_LINK_SECRET, GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH (default main)

import { createHmac, timingSafeEqual } from "node:crypto";

const GITHUB_API = "https://api.github.com";

function signId(secret, id) {
  return createHmac("sha256", secret).update(id).digest("hex").slice(0, 32);
}

export default async (req) => {
  const cid = (globalThis.crypto?.randomUUID?.() || String(Math.random()).slice(2)).slice(0, 8);
  const fail = (status, payload) => {
    console.error(`[delete-profile ${cid}]`, JSON.stringify(payload));
    return json({ ...payload, cid }, status);
  };

  if (req.method !== "POST") return fail(405, { error: "Method not allowed" });

  const secret = process.env.PROFILE_LINK_SECRET;
  const token = process.env.GITHUB_TOKEN;
  const repo = process.env.GITHUB_REPO;
  const branch = process.env.GITHUB_BRANCH || "main";
  if (!secret || !token || !repo) {
    return fail(500, {
      error: "Server not configured",
      detail: `missing env: ${[!secret && "PROFILE_LINK_SECRET", !token && "GITHUB_TOKEN", !repo && "GITHUB_REPO"]
        .filter(Boolean).join(", ")}`,
    });
  }

  let body;
  try {
    body = await req.json();
  } catch {
    return fail(400, { error: "Invalid JSON body" });
  }
  const id = String(body.id || "").toLowerCase();
  const supplied = String(body.token || "");
  if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(id) || id === "example") {
    return fail(400, { error: "Invalid id" });
  }

  const expected = signId(secret, id);
  const a = Buffer.from(supplied);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !timingSafeEqual(a, b)) {
    return fail(403, { error: "Invalid or expired link" });
  }

  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "news-app-intake",
    "Content-Type": "application/json",
  };

  async function remove(path) {
    const get = await fetch(
      `${GITHUB_API}/repos/${repo}/contents/${path}?ref=${encodeURIComponent(branch)}`,
      { headers },
    );
    if (get.status === 404) return "absent";
    if (!get.ok) throw new Error(`read ${path}: ${get.status}`);
    const sha = (await get.json()).sha;
    const del = await fetch(`${GITHUB_API}/repos/${repo}/contents/${path}`, {
      method: "DELETE",
      headers,
      body: JSON.stringify({ message: `chore(profile): unsubscribe ${id}`, sha, branch }),
    });
    if (!del.ok) throw new Error(`delete ${path}: ${del.status} ${(await del.text()).slice(0, 200)}`);
    return "deleted";
  }

  try {
    const profile = await remove(`profiles/${id}.json`);
    let history = "absent";
    try {
      history = await remove(`history/${id}.json`);
    } catch (e) {
      // history is best-effort; the profile removal is what matters
      console.error(`[delete-profile ${cid}] history cleanup: ${e}`);
    }
    console.log(`[delete-profile ${cid}] ${id}: profile=${profile} history=${history}`);
    return json({ ok: true, id, profile, history, cid });
  } catch (e) {
    return fail(502, { error: "Delete failed", detail: String(e) });
  }
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
