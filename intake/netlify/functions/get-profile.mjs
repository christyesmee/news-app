// Netlify Function (v2) — GET /.netlify/functions/get-profile?id=<id>&token=<hmac>
//
// Returns profiles/<id>.json for the tune-my-brief page. Access is gated by a
// signed token: HMAC-SHA256(PROFILE_LINK_SECRET, id), first 32 hex chars — the
// same token the engine embeds in every email's "Tune my brief" link. No
// passwords, no accounts; possession of the emailed link IS the authentication.
//
// Env: PROFILE_LINK_SECRET, GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH (default main)

import { createHmac, timingSafeEqual } from "node:crypto";

const GITHUB_API = "https://api.github.com";

export function signId(secret, id) {
  return createHmac("sha256", secret).update(id).digest("hex").slice(0, 32);
}

export default async (req) => {
  const cid = (globalThis.crypto?.randomUUID?.() || String(Math.random()).slice(2)).slice(0, 8);
  const fail = (status, payload) => {
    console.error(`[get-profile ${cid}]`, JSON.stringify(payload));
    return json({ ...payload, cid }, status);
  };

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

  const url = new URL(req.url);
  const id = String(url.searchParams.get("id") || "").toLowerCase();
  const supplied = String(url.searchParams.get("token") || "");
  if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(id)) return fail(400, { error: "Invalid id" });

  const expected = signId(secret, id);
  const a = Buffer.from(supplied);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !timingSafeEqual(a, b)) {
    return fail(403, { error: "Invalid or expired link" });
  }

  try {
    const resp = await fetch(
      `${GITHUB_API}/repos/${repo}/contents/profiles/${id}.json?ref=${encodeURIComponent(branch)}`,
      {
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "news-app-intake",
        },
      },
    );
    if (resp.status === 404) return fail(404, { error: "Profile not found" });
    if (!resp.ok) {
      return fail(502, { error: "GitHub read failed", status: resp.status, detail: (await resp.text()).slice(0, 200) });
    }
    const data = await resp.json();
    const profile = JSON.parse(Buffer.from(data.content, "base64").toString("utf8"));
    console.log(`[get-profile ${cid}] served ${id}`);
    return json({ ok: true, profile, cid });
  } catch (e) {
    return fail(502, { error: "Read failed", detail: String(e) });
  }
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
