/**
 * i5-cloud — Cloudflare Worker + D1 endpoint for Color i5 reading uploads,
 * with per-user API keys ("license keys").
 *
 * Auth model:
 *   - The ADMIN key is the API_TOKEN Worker secret (`wrangler secret put API_TOKEN`).
 *     It can do everything, including issuing/revoking user keys via /api/keys.
 *   - USER keys live in the D1 `api_keys` table (SHA-256 hashes only). Each key
 *     is tied to an owner, can be read-only, has a daily upload quota, and can
 *     be revoked with one flag. Every uploaded reading is tagged with its
 *     uploader, so a bad actor's rows can be purged by owner.
 *
 * Routes (all require `Authorization: Bearer <key>`):
 *   GET  /api/health            any key    → { ok, readings }
 *   POST /api/readings          write keys → { ok, inserted, skipped, total }
 *   GET  /api/readings?…        any key    → { ok, readings: [...] }
 *   POST /api/keys              admin      → { ok, key, owner }   (key shown ONCE)
 *   GET  /api/keys              admin      → { ok, keys: [...] }  (hashes, not keys)
 *   POST /api/keys/revoke       admin      → { ok, owner, revoked }
 *   POST /api/keys/purge        admin      → { ok, owner, deleted } (their readings)
 *
 * Setup: see README.md (d1 create → schema.sql → secret put API_TOKEN → deploy).
 */

const JSON_HEADERS = { "Content-Type": "application/json" };

function reply(status, body) {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

async function sha256hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Constant-time equality (hash both sides to equalize length). */
async function safeEqual(a, b) {
  const enc = new TextEncoder();
  const [da, db] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  return crypto.subtle.timingSafeEqual(da, db);
}

/**
 * Resolve the bearer token to an identity:
 *   { owner:"admin", admin:true, canWrite:true } |
 *   { owner, admin:false, canWrite, dailyQuota } |
 *   null
 */
async function identify(request, env) {
  if (!env.API_TOKEN) return null; // refuse to run open — set the secret
  const token = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  if (!token) return null;
  if (await safeEqual(token, env.API_TOKEN)) {
    return { owner: "admin", admin: true, canWrite: true };
  }
  const row = await env.DB.prepare(
    "SELECT owner, can_write, daily_quota FROM api_keys WHERE key_hash = ? AND revoked = 0",
  ).bind(await sha256hex(token)).first();
  if (!row) return null;
  return { owner: row.owner, admin: false, canWrite: !!row.can_write, dailyQuota: row.daily_quota };
}

const NUM = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

// ── readings ──────────────────────────────────────────────────────────────────

async function insertReadings(env, who, payload) {
  const device = String(payload.device || "color-i5").slice(0, 64);
  const readings = Array.isArray(payload.readings) ? payload.readings : [];
  if (!readings.length) return { inserted: 0, skipped: 0 };
  if (readings.length > 500) throw new Error("max 500 readings per upload");

  if (!who.admin) {
    const used = await env.DB.prepare(
      "SELECT COUNT(*) AS n FROM readings WHERE uploaded_by = ? AND uploaded_at >= date('now')",
    ).bind(who.owner).first();
    if (used.n + readings.length > who.dailyQuota) {
      throw new Error(`daily quota exceeded (${used.n}/${who.dailyQuota} used today)`);
    }
  }

  const stmt = env.DB.prepare(
    `INSERT OR IGNORE INTO readings
       (uploaded_by, device, ts, label, mode, L, a, b, C, h, agtron,
        roast_class, roast_shade, de76, crc_ok, datasum_ok, flashes, status, spectrum)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
  );
  const batch = readings.map((r) => {
    const spectrum = Array.isArray(r.reflectance) ? r.reflectance : [];
    if (spectrum.length !== 40) throw new Error(`reading "${r.label}" has ${spectrum.length} spectral values, expected 40`);
    return stmt.bind(
      who.owner, device, String(r.timestamp || ""), String(r.label || ""), String(r.mode || "sci"),
      NUM(r.L), NUM(r.a), NUM(r.b), NUM(r.C), NUM(r.h), NUM(r.agtron),
      r.roast_class ?? null, r.roast_shade ?? null, NUM(r.dE76),
      r.crc_ok ? 1 : 0, r.datasum_ok ? 1 : 0,
      String(r.flashes ?? ""), String(r.status ?? ""), JSON.stringify(spectrum),
    );
  });
  const results = await env.DB.batch(batch);
  const inserted = results.reduce((n, r) => n + (r.meta?.changes || 0), 0);
  return { inserted, skipped: readings.length - inserted };
}

async function listReadings(env, url) {
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "100", 10) || 100, 1000);
  const filters = [];
  const binds = [];
  for (const key of ["label", "device", "mode", "uploaded_by"]) {
    const v = url.searchParams.get(key);
    if (v) { filters.push(`${key} = ?`); binds.push(v); }
  }
  const where = filters.length ? `WHERE ${filters.join(" AND ")}` : "";
  const { results } = await env.DB.prepare(
    `SELECT * FROM readings ${where} ORDER BY ts DESC LIMIT ?`,
  ).bind(...binds, limit).all();
  for (const row of results) row.spectrum = JSON.parse(row.spectrum);
  return results;
}

// ── key management (admin only) ──────────────────────────────────────────────

function newKey() {
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  const b64 = btoa(String.fromCharCode(...bytes))
    .replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
  return `i5k_${b64}`;
}

async function createKey(env, body) {
  const owner = String(body.owner || "").trim().toLowerCase();
  if (!owner || owner === "admin") throw new Error("owner is required (and can't be 'admin')");
  const key = newKey();
  await env.DB.prepare(
    `INSERT INTO api_keys (key_hash, owner, can_write, daily_quota, note)
     VALUES (?,?,?,?,?)
     ON CONFLICT(owner) DO UPDATE SET
       key_hash = excluded.key_hash, can_write = excluded.can_write,
       daily_quota = excluded.daily_quota, note = excluded.note, revoked = 0`,
  ).bind(
    await sha256hex(key), owner,
    body.can_write === false ? 0 : 1,
    Number.isInteger(body.daily_quota) ? body.daily_quota : 5000,
    String(body.note || ""),
  ).run();
  // The key is returned exactly once; only its hash is stored.
  return { key, owner };
}

async function listKeys(env) {
  const { results } = await env.DB.prepare(
    `SELECT k.owner, k.can_write, k.revoked, k.daily_quota, k.note, k.created_at,
            (SELECT COUNT(*) FROM readings r WHERE r.uploaded_by = k.owner) AS readings
     FROM api_keys k ORDER BY k.created_at`,
  ).all();
  return results;
}

async function revokeKey(env, body) {
  const owner = String(body.owner || "").trim().toLowerCase();
  if (!owner) throw new Error("owner is required");
  const r = await env.DB.prepare(
    "UPDATE api_keys SET revoked = 1 WHERE owner = ?",
  ).bind(owner).run();
  if (!r.meta.changes) throw new Error(`no key found for owner "${owner}"`);
  return { owner, revoked: true };
}

async function purgeOwner(env, body) {
  const owner = String(body.owner || "").trim().toLowerCase();
  if (!owner || owner === "admin") throw new Error("owner is required (and can't be 'admin')");
  const r = await env.DB.prepare(
    "DELETE FROM readings WHERE uploaded_by = ?",
  ).bind(owner).run();
  return { owner, deleted: r.meta.changes };
}

// ── router ────────────────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const route = `${request.method} ${url.pathname}`;
    try {
      const who = await identify(request, env);
      if (!who) {
        return reply(401, { ok: false, error: "missing, bad, or revoked API key" });
      }
      if (url.pathname.startsWith("/api/keys") && !who.admin) {
        return reply(403, { ok: false, error: "admin key required" });
      }

      switch (route) {
        case "GET /api/health": {
          const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM readings").first();
          return reply(200, { ok: true, readings: row.n });
        }
        case "POST /api/readings": {
          if (!who.canWrite) return reply(403, { ok: false, error: "this key is read-only" });
          const { inserted, skipped } = await insertReadings(env, who, await request.json());
          const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM readings").first();
          return reply(200, { ok: true, inserted, skipped, total: row.n });
        }
        case "GET /api/readings":
          return reply(200, { ok: true, readings: await listReadings(env, url) });
        case "POST /api/keys":
          return reply(200, { ok: true, ...(await createKey(env, await request.json())) });
        case "GET /api/keys":
          return reply(200, { ok: true, keys: await listKeys(env) });
        case "POST /api/keys/revoke":
          return reply(200, { ok: true, ...(await revokeKey(env, await request.json())) });
        case "POST /api/keys/purge":
          return reply(200, { ok: true, ...(await purgeOwner(env, await request.json())) });
        default:
          return reply(404, { ok: false, error: "not found" });
      }
    } catch (err) {
      const message = String(err.message || err);
      console.log(JSON.stringify({ level: "error", route, message }));
      const status = /quota|read-only|required|expected 40|max 500|no key found/.test(message) ? 400 : 500;
      return reply(status, { ok: false, error: message });
    }
  },
};
