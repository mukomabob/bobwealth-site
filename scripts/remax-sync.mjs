// Pulls every Zimbabwe listing (for sale + to rent) from remax.co.za and
// writes remaxtest/listings.json in the format the demo + Worker expect.
// Run: node scripts/remax-sync.mjs   (Node 20+, no dependencies)

import { writeFile, readFile } from "node:fs/promises";

const BASE = "https://www.remax.co.za";
const SOURCES = [
  { type: "Sale", path: "/property-for-sale-zimbabwe", prefix: "S" },
  { type: "Rent", path: "/property-to-rent-zimbabwe", prefix: "R" },
];
const OUT = "remaxtest/listings.json";
const UA = "Mozilla/5.0 (compatible; bobwealth-remax-sync/1.0; +https://bobwealth.org/remaxtest/)";
const FEATURE_WORDS = [
  "borehole", "solar", "swimming pool", "pool", "garden cottage", "cottage", "staff quarters",
  "walled and gated", "electric fence", "security", "generator", "inverter", "backup power",
  "double garage", "garage", "carport", "en-suite", "ensuite", "study", "office", "gazebo",
  "veranda", "patio", "braai", "fireplace", "tennis court", "water tank", "solar geyser",
  "fitted kitchen", "pantry", "scullery", "laundry", "furnished", "air conditioning", "lift",
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function get(url) {
  for (let attempt = 1; attempt <= 3; attempt++) {
    const res = await fetch(url, { headers: { "user-agent": UA, accept: "text/html" } });
    if (res.ok) return res.text();
    if (res.status === 404) return null;
    console.warn(`GET ${url} -> ${res.status} (attempt ${attempt})`);
    await sleep(2000 * attempt);
  }
  throw new Error(`Failed to fetch ${url}`);
}

export function jsonLd(html) {
  const out = [];
  const re = /<script[^>]*type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(html))) {
    try {
      const j = JSON.parse(m[1]);
      out.push(...(j["@graph"] || [j]));
    } catch { /* skip malformed block */ }
  }
  return out;
}

const listingUrls = (graph) =>
  graph.filter((o) => o["@type"] === "RealEstateListing" && o.url).map((o) => o.url);

async function collectUrls(src) {
  const urls = new Set();
  for (let page = 1; page <= 30; page++) {
    const html = await get(`${BASE}${src.path}${page > 1 ? `?page=${page}` : ""}`);
    if (!html) break;
    const found = listingUrls(jsonLd(html)).filter((u) => !urls.has(u));
    if (!found.length) break;
    found.forEach((u) => urls.add(u));
    await sleep(800);
  }
  return [...urls];
}

const propObj = (p, name) =>
  (p?.additionalProperty || []).find((x) => String(x.name).toLowerCase() === name.toLowerCase());
const prop = (p, name) => propObj(p, name)?.value;
// Areas in m²; RE/MAX gives larger stands in hectares or acres.
function area(p, ...names) {
  for (const n of names) {
    const o = propObj(p, n);
    if (o?.value == null) continue;
    const v = Number(o.value), u = String(o.unitText || "").toLowerCase();
    if (!v) continue;
    if (/ha|hect/.test(u)) return Math.round(v * 10000);
    if (/acre|ac\b/.test(u)) return Math.round(v * 4046.86);
    return Math.round(v);
  }
  return null;
}

function features(text) {
  const t = String(text || "").toLowerCase();
  const hits = FEATURE_WORDS.filter((w) => t.includes(w));
  // drop generic words already covered by a more specific match
  return hits
    .filter((w) => !(w === "pool" && hits.includes("swimming pool")))
    .filter((w) => !(w === "garage" && hits.includes("double garage")))
    .filter((w) => !(w === "cottage" && hits.includes("garden cottage")))
    .filter((w) => !(w === "solar" && hits.includes("solar geyser") && !/solar (panel|power|system|inverter)/.test(t)))
    .filter((w) => !(w === "en-suite" && hits.includes("ensuite")))
    .map((w) => (w === "en-suite" ? "ensuite" : w))
    .map((w) => w.replace(/(^|\s)\S/g, (c) => c.toUpperCase()));
}

export function parseDetail(html, src, url) {
  const g = jsonLd(html);
  const listing = g.find((o) => o["@type"] === "RealEstateListing");
  if (!listing) return null;
  const offer = g.find((o) => o["@type"] === "Offer");
  const place = g.find((o) => o.address && o["@id"]?.endsWith("#property")) ||
    g.find((o) => o.address && o["@type"] !== "RealEstateAgent" && o["@type"] !== "PostalAddress");
  const agent = g.find((o) => o["@type"] === "Person");
  const id = (url.match(/-(\d+)\/?$/) || [])[1];
  const desc = String(listing.description || "").replace(/\s+/g, " ").trim();
  const price = Number(offer?.price ?? offer?.priceSpecification?.price ?? 0) || null;
  return {
    listing_ref: `RMX-${src.prefix}${id}`,
    listing_type: src.type,
    status: "Active",
    property_type: prop(place, "Property Type") || place?.["@type"] || null,
    title: listing.name,
    suburb: place?.address?.addressLocality || null,
    region: place?.address?.addressRegion || null,
    price_usd: price,
    price_period: src.type === "Rent" ? "per month" : null,
    bedrooms: place?.numberOfBedrooms ?? null,
    bathrooms: place?.numberOfBathroomsTotal ?? null,
    parking: prop(place, "Parking Spaces") ?? null,
    stand_size_m2: area(place, "Land Area", "Erf Size", "Stand Size"),
    floor_size_m2: area(place, "Floor Area", "Floor Size"),
    features: features(desc),
    listed_date: listing.datePosted ? String(listing.datePosted).slice(0, 10) : null,
    agent: agent ? { name: agent.name } : null,
    url,
    summary: desc.slice(0, 400),
  };
}

async function main() {
  const listings = [];
  for (const src of SOURCES) {
    const urls = await collectUrls(src);
    console.log(`${src.type}: ${urls.length} listing pages`);
    for (const url of urls) {
      const html = await get(url);
      const l = html && parseDetail(html, src, url);
      if (l && l.price_usd) listings.push(l);
      await sleep(800);
    }
  }
  if (!listings.length) throw new Error("No listings parsed — leaving listings.json untouched");

  listings.sort((a, b) => String(b.listed_date).localeCompare(String(a.listed_date)));
  const body = (generated) => JSON.stringify({
    generated,
    source: "https://www.remax.co.za (Zimbabwe listings)",
    note: "Synced daily from remax.co.za by .github/workflows/remax-sync.yml",
    listings,
  }, null, 2) + "\n";

  // Only rewrite the file (and so only commit) when the listings actually changed.
  let prev = null;
  try { prev = JSON.parse(await readFile(OUT, "utf8")); } catch {}
  if (prev && JSON.stringify(prev.listings) === JSON.stringify(listings)) {
    console.log("No changes.");
    return;
  }
  await writeFile(OUT, body(new Date().toISOString()));
  console.log(`Wrote ${listings.length} listings to ${OUT}`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((e) => { console.error(e); process.exit(1); });
}
