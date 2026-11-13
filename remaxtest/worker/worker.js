// RE/MAX "Ask the listings" — Cloudflare Worker (Workers AI version, no API key)
// Settings needed (Worker > Settings):
//   Binding:  Workers AI, variable name AI
//   Variable: LISTINGS_URL  e.g. https://bobwealth.org/remaxtest/listings.json
//   Variable: MODEL (optional) defaults to @cf/meta/llama-3.3-70b-instruct-fp8-fast

const ALLOWED_ORIGINS = ["https://bobwealth.org", "https://www.bobwealth.org"];
const DEFAULT_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast";

// ---------- data ----------
let cache = { at: 0, data: null };
async function loadListings(env) {
  if (cache.data && Date.now() - cache.at < 5 * 60 * 1000) return cache.data;
  const res = await fetch(env.LISTINGS_URL, { cf: { cacheTtl: 300 } });
  if (!res.ok) throw new Error(`Could not load listings (${res.status})`);
  const json = await res.json();
  cache = { at: Date.now(), data: json.listings || json };
  return cache.data;
}

const median = (a) => {
  if (!a.length) return null;
  const s = [...a].sort((x, y) => x - y), m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : Math.round((s[m - 1] + s[m]) / 2);
};

const usd = (n) => "$" + Number(n).toLocaleString("en-US");

// One compact line per listing, so all of them fit in the prompt.
function line(l) {
  const parts = [
    `[${l.listing_ref}]`,
    l.listing_type, l.status, l.property_type,
    `${l.suburb} (${l.region})`,
    `${usd(l.price_usd)}${l.price_period ? " " + l.price_period : ""}`,
    l.bedrooms ? `${l.bedrooms} bed` : null,
    l.bathrooms ? `${l.bathrooms} bath` : null,
    l.stand_size_m2 ? `stand ${l.stand_size_m2}m2` : null,
    l.floor_size_m2 ? `floor ${l.floor_size_m2}m2` : null,
    l.title_type,
    l.features && l.features.length ? `features: ${l.features.join(", ")}` : null,
    `listed ${l.listed_date}`,
    l.parking ? `parking ${l.parking}` : null,
    l.agent && l.agent.name ? `agent ${l.agent.name}` : null,
    `"${l.title}"`,
    l.summary ? `about: ${String(l.summary).slice(0, 220)}` : null,
  ];
  return parts.filter(Boolean).join(" | ");
}

// Pre-computed stats so the model never has to do arithmetic.
function stats(all) {
  const groups = new Map();
  for (const l of all) {
    if (!["Active", "Under Offer"].includes(l.status)) continue;
    const key = `${l.suburb} | ${l.listing_type} | ${l.property_type}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(l.price_usd);
  }
  // Sorted by median, highest first, so "highest/lowest" can be read off directly.
  return [...groups.entries()]
    .sort(([, a], [, b]) => median(b) - median(a))
    .map(([k, p]) => `${k}: count ${p.length}, median ${usd(median(p))}, min ${usd(Math.min(...p))}, max ${usd(Math.max(...p))}`)
    .join("\n");
}

function systemPrompt(all, today) {
  return `You are the assistant for RE/MAX in Zimbabwe. Today is ${today}. These are the current RE/MAX Zimbabwe listings from remax.co.za.
Answer ONLY from the listings and stats below. Never invent listings, prices or numbers. If nothing matches, say so and suggest the closest alternative from the list (another suburb, price or bedroom count).
Prices are USD; rentals are per month. Unless asked, only suggest listings whose status is Active or Under Offer.
For medians, highest and lowest, use the STATS section, which is already sorted from highest to lowest median; do not calculate or compare numbers yourself.

How to write each listing you mention, as one bullet in plain words, ending with its ref in square brackets:
- **$520,000**: 6-bed house in Glen Lorne, 3 baths, 3,942 m² stand, borehole and solar geyser [RMX-S71228481]
Never copy the raw data lines (no "|" characters, no "listed", "agent" or "about" labels). Write money with commas, e.g. $1,500,000 or $2,200/month.

Keep answers short: one plain sentence first, then up to 5 bullets. No tables. If sale vs rent is unclear, show both or assume sale and say so.
Reply in Shona if the user writes in Shona.

LISTINGS:
${all.map(line).join("\n")}

STATS (Active + Under Offer, grouped by suburb | sale/rent | property type):
${stats(all)}`;
}

// Pull hard limits (price, bedrooms, sale/rent) out of the question so the
// filtering is done by code, not guessed by the model.
function parseLimits(q) {
  const t = String(q || "").toLowerCase().replace(/,/g, "");
  const money = (n, unit) => Number(n) * (/^m/.test(unit || "") ? 1e6 : /^k/.test(unit || "") ? 1e3 : 1);
  const num = "\\$?\\s*(\\d+(?:\\.\\d+)?)\\s*(k|m|million|thousand)?";
  const max = t.match(new RegExp(`(?:under|below|less than|up to|max(?:imum)?|cheaper than|within|no more than)\\s*(?:usd)?\\s*${num}`));
  const min = t.match(new RegExp(`(?:over|above|more than|at least|from|min(?:imum)?)\\s*(?:usd)?\\s*${num}`));
  const between = t.match(new RegExp(`between\\s*${num}\\s*(?:and|-|to)\\s*${num}`));
  const beds = t.match(/(\d+)\s*\+?\s*-?\s*(?:bed|bedroom|br\b)/);
  const L = {};
  if (between) { L.min = money(between[1], between[2]); L.max = money(between[3], between[4]); }
  else { if (max) L.max = money(max[1], max[2]); if (min) L.min = money(min[1], min[2]); }
  if (beds) L.beds = Number(beds[1]);
  if (/\b(rent|rental|rentals|to let|lease|per month|a month|monthly)\b/.test(t)) L.type = "Rent";
  else if (/\b(for sale|buy|buying|purchase)\b/.test(t)) L.type = "Sale";
  return L;
}
const hasLimits = (L) => L.min != null || L.max != null || L.beds != null;
function applyLimits(all, L) {
  return all.filter((l) =>
    (!L.type || l.listing_type === L.type) &&
    (L.max == null || l.price_usd <= L.max) &&
    (L.min == null || l.price_usd >= L.min) &&
    (L.beds == null || (l.bedrooms || 0) >= L.beds));
}

async function chat(env, history) {
  const all = await loadListings(env);
  const today = new Date().toISOString().slice(0, 10);
  const lastUser = [...history].reverse().find((m) => m.role !== "assistant");
  const limits = parseLimits(lastUser && lastUser.content);
  const matches = hasLimits(limits) ? applyLimits(all, limits) : null;
  let system = systemPrompt(all, today);
  if (matches) {
    system += `\n\nFILTERED BY CODE for the latest question (${JSON.stringify(limits)}): ` +
      (matches.length
        ? `only these listings meet the price/bedroom limits: ${matches.map((l) => l.listing_ref).join(", ")}. Mention ONLY these; do not list any other listing as a match.`
        : `no listing meets these limits. Say so plainly, then suggest the 1-2 closest listings and say clearly they are outside the limit.`);
  }
  const messages = [
    { role: "system", content: system },
    ...history.slice(-8).map((m) => ({
      role: m.role === "assistant" ? "assistant" : "user",
      content: String(m.content).slice(0, 2000),
    })),
  ];
  const r = await env.AI.run(env.MODEL || DEFAULT_MODEL, { messages, max_tokens: 700, temperature: 0.2 });
  const reply = String((r && r.response) || "").trim() || "Sorry, I couldn't come up with an answer. Try rephrasing.";
  const refs = [...reply.matchAll(/\[(RMX-[A-Z]\d+)\]/g)].map((m) => m[1]);
  let cards = [...new Set(refs)].map((ref) => all.find((l) => l.listing_ref === ref)).filter(Boolean);
  // If the model still slipped in a listing outside the limits, keep it out of the cards.
  if (matches && matches.length) cards = cards.filter((l) => matches.includes(l));
  return { reply, listings: cards };
}

// ---------- leads (enquiries from the Next steps form) ----------
// Stored in the D1 database bound as DB (remax-leads). View them in the
// Cloudflare dashboard: D1 > remax-leads > Explore Data > leads.
const LEAD_FIELDS = ["name", "phone", "email", "subject", "budget", "area", "listings", "message", "questions", "channel"];
const STATUSES = ["New", "Contacted", "Viewing booked", "Offer made", "Closed", "Not interested"];
async function ensureTable(env) {
  if (!env.DB) throw new Error("Lead storage not configured");
  await env.DB.prepare(`CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    name TEXT, phone TEXT, email TEXT, subject TEXT, budget TEXT, area TEXT,
    listings TEXT, message TEXT, questions TEXT, channel TEXT,
    status TEXT DEFAULT 'New', notes TEXT DEFAULT '', updated_at TEXT)`).run();
}
async function saveLead(env, lead) {
  const clean = {};
  for (const f of LEAD_FIELDS) clean[f] = String(lead[f] ?? "").trim().slice(0, f === "message" || f === "questions" ? 2000 : 200);
  if (!clean.name || (!clean.phone && !clean.email)) throw new Error("Name and a phone or email are required");
  await ensureTable(env);
  const created = new Date().toISOString().replace("T", " ").slice(0, 19);
  await env.DB.prepare(
    `INSERT INTO leads (created_at, ${LEAD_FIELDS.join(", ")}) VALUES (?, ${LEAD_FIELDS.map(() => "?").join(", ")})`
  ).bind(created, ...LEAD_FIELDS.map((f) => clean[f])).run();
  return { ok: true };
}

// Admin (the leads app): needs the ADMIN_KEY secret set on the Worker.
function isAdmin(request, env) {
  const key = request.headers.get("x-admin-key") || "";
  return Boolean(env.ADMIN_KEY) && key.length >= 4 && key === env.ADMIN_KEY;
}
async function listLeads(env) {
  await ensureTable(env);
  const { results } = await env.DB.prepare("SELECT * FROM leads ORDER BY id DESC LIMIT 500").all();
  return { leads: results, statuses: STATUSES };
}
async function updateLead(env, u) {
  await ensureTable(env);
  const id = Number(u.id);
  if (!id) throw new Error("Missing lead id");
  const status = STATUSES.includes(u.status) ? u.status : null;
  const notes = u.notes == null ? null : String(u.notes).slice(0, 4000);
  const now = new Date().toISOString().replace("T", " ").slice(0, 19);
  await env.DB.prepare(
    "UPDATE leads SET status = COALESCE(?, status), notes = COALESCE(?, notes), updated_at = ? WHERE id = ?"
  ).bind(status, notes, now, id).run();
  return { ok: true };
}

// ---------- HTTP ----------
function corsHeaders(origin) {
  const ok = ALLOWED_ORIGINS.includes(origin);
  return {
    "Access-Control-Allow-Origin": ok ? origin : ALLOWED_ORIGINS[0],
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type, x-admin-key",
    "Vary": "Origin",
  };
}
const json = (body, status, headers) =>
  new Response(JSON.stringify(body), { status, headers: { ...headers, "content-type": "application/json" } });

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const cors = corsHeaders(origin);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    const path = new URL(request.url).pathname;

    // Leads app: GET /leads lists customers, POST /leads updates status/notes.
    if (path === "/leads") {
      if (!env.ADMIN_KEY) return json({ error: "Set the ADMIN_KEY secret on the Worker first" }, 503, cors);
      if (!isAdmin(request, env)) return json({ error: "Wrong PIN" }, 401, cors);
      try {
        if (request.method === "GET") return json(await listLeads(env), 200, cors);
        if (request.method === "POST") return json(await updateLead(env, await request.json()), 200, cors);
        return json({ error: "Method not allowed" }, 405, cors);
      } catch (e) {
        return json({ error: e.message }, 500, cors);
      }
    }

    if (request.method !== "POST") return json({ error: "Use POST" }, 405, cors);
    if (!ALLOWED_ORIGINS.includes(origin)) return json({ error: "Origin not allowed" }, 403, cors);

    try {
      const body = await request.json();
      if (body && body.lead) return json(await saveLead(env, body.lead), 200, cors);
      const { messages } = body;
      if (!Array.isArray(messages) || !messages.length) return json({ error: "No messages" }, 400, cors);
      return json(await chat(env, messages), 200, cors);
    } catch (e) {
      return json({ error: e.message }, 500, cors);
    }
  },
};
