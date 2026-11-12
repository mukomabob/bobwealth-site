/**
 * FBC stock-trend chat agent -- Cloudflare Worker.
 *
 * Two-step "structured data agent" loop, same pattern as query_engine.py's
 * run_sql() but with an LLM in front of it instead of a human writing SQL:
 *
 *   1. text -> SQL:   Workers AI reads the question + schema, writes ONE
 *                      read-only SQL query.
 *   2. SQL -> answer:  that query runs against D1 (real numbers, not
 *                      hallucinated), then Workers AI reads the question +
 *                      the actual result rows and composes a short answer.
 *
 * Every number in the final answer traces back to a real D1 query result --
 * the LLM never gets to just state a figure from "memory".
 *
 * If D1 rejects the query, the model sees the error and gets one retry.
 * The client may send the last few turns as `history` so follow-ups like
 * "and Delta?" can be resolved; they are context only, never trusted as SQL.
 *
 * Every question is also written to the ask_log table (see logAsk) for the
 * daily monitor in stock-agent/monitor/.
 *
 * Bindings expected (see wrangler.toml):
 *   env.DB  - D1 database bound as "DB", containing the `prices`,
 *             `predictions` and `model_track_record` tables (schema.sql /
 *             build_dataset.py / export_to_d1.py build these)
 *   env.AI  - Workers AI binding, bound as "AI"
 *   env.ANTHROPIC_API_KEY - optional Worker secret. When set, Claude writes
 *             the SQL (step 1); Workers AI still words the answer (step 2)
 *             and takes over step 1 if Claude is unavailable.
 *   Big moves and "why" questions also get context the price table can't give:
 *   the counter's corporate_actions rows (splits, dividends) from D1, and -- when
 *   ANTHROPIC_API_KEY is set -- a short Claude web search for announcements or
 *   news. Both go to the answer step, clearly marked, with sources returned.
 *   env.CAIMEX_API_KEY - optional Worker secret for Econet's Caimex gateway
 *             (OpenAI-compatible). Writes the SQL when Claude isn't set
 *             (CAIMEX_SQL_MODEL) and words the answer (CAIMEX_ANSWER_MODEL);
 *             Workers AI takes over either step if Caimex fails.
 */

import Anthropic from "@anthropic-ai/sdk";
import { SQL_SYSTEM_PROMPT, extractSql, validateSql } from "./sql_prompt.js";

const MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast";
// Writing the query is where the answers went wrong (wrong day, wrong counter),
// so that step goes to Claude. Haiku 4.5 is the cheapest Claude model.
const CLAUDE_SQL_MODEL = "claude-haiku-4-5";
// Claude model for the web-search step (override with SEARCH_MODEL in wrangler.toml).
const CLAUDE_SEARCH_MODEL = "claude-haiku-4-5";
// A row moving this much (%) in a day, or this much over a period, counts as a
// big move worth explaining.
const BIG_DAY_MOVE_PCT = 20;
const BIG_PERIOD_MOVE_PCT = 40;
const WHY_RE = /\b(why|reason|cause|caused|what happened|news|announce\w*|split|dividend)\b/i;

// Plain-language sector definitions, keyed by the sector values in the data.
// The answer step may only use these, so it never improvises one.
const SECTOR_DEFINITIONS = `
- Reit: a real estate investment trust -- a company that owns income-producing property
  (offices, shops, housing) and pays most of the rental income out to its investors.
- Real Estate: companies that develop, own or manage property.
- Financials: banks, insurers, asset managers and other financial-services firms.
- Consumer Staples: everyday essentials people buy regardless of the economy -- food,
  beverages, household goods.
- Consumer Discretionary: non-essential goods and services people buy more of when money
  is easier -- hotels, retail, leisure.
- Industrials: firms that make equipment and materials for other businesses, or provide
  engineering, construction and transport services.
- Materials: producers of raw materials -- mining, metals, chemicals, building materials.
- IT, Communication: technology and telecommunications companies.
- Derivative: an exchange-traded fund (ETF) or depository receipt (ZDR) -- a listed instrument
  whose value tracks a basket of shares, or an asset such as gold or a share listed elsewhere.
- Fixed Term  Bond: a bond that pays a set interest rate until it matures.`.trim();

// Sector definitions go to the model only when the rows describe one counter:
// given them alongside a list, it attached a definition to every name.
const SECTOR_GUIDANCE_ONE = `Explain the counter's sector in one short clause for a reader who may not
know the term, using ONLY the matching definition below (word it naturally, don't add to it):
${SECTOR_DEFINITIONS}
The table has no company descriptions -- if asked what the company does, give only its sector
and that definition.`;
const SECTOR_GUIDANCE_MANY = `When listing several counters, just name each one's sector if you mention it -- do
not explain what the sectors mean.`;

const answerSystemPrompt = (rowCount) =>
  ANSWER_SYSTEM_PROMPT.replace("${SECTOR_GUIDANCE}", rowCount === 1 ? SECTOR_GUIDANCE_ONE : SECTOR_GUIDANCE_MANY);

const ANSWER_SYSTEM_PROMPT = `You are a stock-market assistant for the Zimbabwe Stock Exchange (ZSE) and
VFEX. You are given a user's question and the exact rows a SQL query
returned for it. Answer the question in at most 8 short sentences using ONLY the numbers
in those rows and in the context blocks below them -- never state a figure that isn't
present in what you were given. If the
rows are empty, say plainly that there's no data for that question rather
than guessing. Earlier turns, if shown, only explain what the new question refers to --
take every figure from the new rows, never from an earlier answer. For a question about one counter, describe it from its row: sector and
market, latest price, the day's change, and its US-dollar return since return_since. Name a counter's sector
ONLY if the rows include its sector value -- never guess or infer one.
\${SECTOR_GUIDANCE}
If there ARE rows, they are the answer: the query already did
the ranking or filtering, so never say there is no data -- state what the rows
show, even if they carry only names.

Formatting rules -- follow these exactly, the raw column values are not
already in a form fit to read aloud:
- change_pct, chg_pct_filled, roll5_chg, roll20_std_chg, div_yield_fy25,
  div_yield_fy26 are already percentages (2.94 means 2.94%) -- state with a
  % sign, rounded to 1-2 decimals.
- pct_change_usd is already a percentage, in US dollars, from start_date to end_date: say e.g.
  "+3.33% in US dollars over September, from $0.0240 on 31 Aug to $0.0248 on 28 Sep". If
  end_date is before the month ended, say that was its last price in the month.
- usd_return_pct is already a percentage in US dollars since return_since. State it as
  "+X% in US dollars since <return_since>", and you may add what $1 invested then would now be
  worth: 1 + usd_return_pct / 100, to 2 decimal places (e.g. +64% -> $1.64). This is the same
  measure as the site's Investment Simulator.
- If the rows include a date, say the figures are "as of <date>". If that date is not
  exactly the one asked for, give the figure anyway and name the date it is from.
- ytd_gain_loss is a FRACTION of price change vs the 31-Dec-2024 baseline,
  in ZiG terms -- NOT already a percentage. Multiply by 100 before stating
  it as a %, e.g. a value of 6.54 means "+654%", never "+6.54%". Say this
  is a ZiG/local-currency figure if the question could be read as asking
  about USD returns.
- Never repeat a raw float verbatim -- round every number you state to at
  most 2 decimal places (write "654.10%", not "6.5409836065573765").
- usd_price_ibr, usd_price and close are prices -- state with a currency figure to
  4 decimal places, and say which currency (USD vs ZiG).

Watchlist and model rows (predictions / model_track_record):
- Never state a predicted_chg_pct value or promise a price move. Describe a watchlist counter
  by its rank, signal and volatility_band, with risk_score in brackets (20-day volatility).
- A volatility band describes how far the price has swung, in both directions -- never call a
  counter "safe", and never present "Volatile" as a sign it will rise. "Too little trading" means
  it traded on too few days to judge.
- For the track record, give the group result (beat the market on beat_market_days of days;
  picks averaged picks_avg_next_day_pct vs market_avg_next_day_pct) AND the weaker per-pick
  result (picks_rose_next_day_pct% of individual picks rose the next day).
- For accuracy, compare model_mae with no_change_mae honestly: if model_mae is larger, say
  the model's predicted percentages have missed by more than simply assuming no change.
- End any answer about the watchlist or model with: "Algorithmic signals only, not financial advice."

Advice and periods:
- If the user asks whether to buy, sell or hold, or what to invest in, start with this sentence:
  "I cannot tell you to buy or sell a share." Then give the facts from the rows, and end with:
  "Algorithmic signals only, not financial advice."
- Every percentage change must state its period or start date, for example "on 10 November
  2026", "in the last 5 trading days" or "since 6 January 2026". Use the date column and
  return_since. If the rows do not show the period, do not state the percentage.

Corporate actions and web findings (only when given, after the rows):
- adj_change_pct / adj_usd_return_pct / adj_usd_price have splits taken out. When they differ from
  the raw figures, lead with the adjusted figure and say the raw price fell (or rose) because of the
  split, e.g. "Most of this decrease is because of a 33-for-1 share split. When you adjust for
  the split, the price decreased by 20% on that day."
- A corporate action with source "auto-detected" was inferred from the price sheets: say it
  "appears to be" a split and that the sheets show the share count change.
- Web findings come from news and announcements, not the price data: say "reported" or name the
  source (e.g. "according to Africanfinancials"). Never present them as certain.
- If neither explains a move, say the data shows the move but not its cause. Never guess a cause.

Writing style -- ASD-STE100 Simplified Technical English. Follow these rules in every answer:
- Write sentences of 20 words or fewer. Put one idea in each sentence.
- Use the active voice: "The price decreased", not "A decrease was recorded".
- Use simple, common words with one meaning. For price changes use "increase" and "decrease",
  not "rally", "surge", "jump", "slide", "tank", "fall", "drop" or "collapse".
- Do not use idioms, slang, metaphors or phrasal verbs such as "pick up" or "drive up".
- Keep finance terms (share, split, dividend, return, sector, volume, REIT, ETF) as technical
  names. If a reader may not know a term, explain it in one short sentence.
- Write dates as "28 October 2026". Keep every number exactly as the formatting rules above say.
- If the answer has more than 4 sentences, put it in short paragraphs separated by a blank line.`;

// Only these sites may call the Worker from a browser. Override with the
// ALLOWED_ORIGINS var in wrangler.toml (comma-separated) without a code change.
const DEFAULT_ALLOWED_ORIGINS = ["https://bobwealth.org", "https://www.bobwealth.org"];
const MAX_QUESTION_CHARS = 500;
// Follow-up context the client sends back: the last few turns, each trimmed.
const MAX_HISTORY_TURNS = 3;
const MAX_HISTORY_ANSWER_CHARS = 600;
const MAX_HISTORY_SQL_CHARS = 1000;

function allowedOrigins(env) {
  const raw = (env && env.ALLOWED_ORIGINS) || "";
  const list = raw.split(",").map((o) => o.trim()).filter(Boolean);
  return list.length ? list : DEFAULT_ALLOWED_ORIGINS;
}

function corsHeaders(origin) {
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    Vary: "Origin",
  };
}


// The client's record of earlier turns, reduced to plain strings of bounded
// length. Anything malformed is dropped rather than rejected: history only
// helps resolve a follow-up, it is never required.
function cleanHistory(raw) {
  if (!Array.isArray(raw)) return [];
  const str = (v, max) => (typeof v === "string" ? v.trim().slice(0, max) : "");
  return raw
    .map((t) => ({
      question: str(t && t.question, MAX_QUESTION_CHARS),
      answer: str(t && t.answer, MAX_HISTORY_ANSWER_CHARS),
      sql: str(t && t.sql, MAX_HISTORY_SQL_CHARS),
    }))
    .filter((t) => t.question)
    .slice(-MAX_HISTORY_TURNS);
}

function withHistory(question, history) {
  if (!history.length) return question;
  const turns = history
    .map((t) => `Q: ${t.question}` + (t.sql ? `\nSQL: ${t.sql}` : "") + (t.answer ? `\nA: ${t.answer}` : ""))
    .join("\n\n");
  return `Earlier in this conversation:\n${turns}\n\nNew question: ${question}`;
}

async function writeSql(env, messages, meta = {}) {
  if (env.ANTHROPIC_API_KEY) {
    const [system, ...turns] = messages;
    try {
      const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY, maxRetries: 1, timeout: 20_000 });
      const resp = await client.messages.create({
        model: CLAUDE_SQL_MODEL,
        max_tokens: 1024,
        system: system.content,
        messages: turns,
      });
      const text = resp.content.filter((b) => b.type === "text").map((b) => b.text).join("");
      return { sql: extractSql(text), by: CLAUDE_SQL_MODEL };
    } catch (e) {
      // Fall through to Workers AI so a Claude outage, rate limit or spending
      // cap never takes the chatbot down.
      if (e instanceof Anthropic.AuthenticationError) {
        console.error("ANTHROPIC_API_KEY was rejected; using Workers AI for SQL.");
      } else if (e instanceof Anthropic.RateLimitError) {
        console.error("Claude rate-limited; using Workers AI for SQL.");
      } else if (e instanceof Anthropic.APIError) {
        console.error(`Claude API error ${e.status}; using Workers AI for SQL:`, e.message);
      } else {
        console.error("Claude request failed; using Workers AI for SQL:", e);
      }
    }
  }
  if (env.CAIMEX_API_KEY && env.CAIMEX_SQL_MODEL) {
    const text = await caimexChat(env, env.CAIMEX_SQL_MODEL, messages, 3000, meta, "sql");
    if (text) return { sql: extractSql(text), by: env.CAIMEX_SQL_MODEL };
  }
  // The free Workers AI model for this step is set by SQL_MODEL in wrangler.toml.
  const model = env.SQL_MODEL || MODEL;
  const resp = await env.AI.run(model, { messages, max_tokens: 2048 });
  return { sql: extractSql(aiText(resp)), by: model };
}



// One chat request to Econet's Caimex gateway. Returns the reply text, or null
// when it is out of balance, over a limit, down or unreachable -- the caller
// then falls back to the free Workers AI model. The reason for a failure goes
// into meta.caimexErrors, so the question log and the monitor can show it.
// DeepSeek V4 "thinks" before it replies and the thinking counts against
// max_tokens: too low a limit gives an empty reply (finish_reason "length").
async function caimexChat(env, model, messages, maxTokens, meta = {}, step = "") {
  try {
    const res = await fetch(`${env.CAIMEX_BASE_URL}/chat/completions`, {
      method: "POST",
      headers: { Authorization: `Bearer ${env.CAIMEX_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model, messages, max_tokens: maxTokens }),
      signal: AbortSignal.timeout(25_000),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
    const json = await res.json();
    const text = aiText(json);
    if (!text) throw new Error(`empty reply (finish_reason: ${json?.choices?.[0]?.finish_reason || "unknown"})`);
    return text;
  } catch (e) {
    const reason = e?.name === "TimeoutError" ? "timeout after 25 s" : String(e?.message || e).slice(0, 160);
    (meta.caimexErrors ||= []).push(`${step || "call"}: ${reason}`);
    console.error(`Caimex (${model}) failed; falling back to Workers AI:`, reason);
    return null;
  }
}

// Workers AI models answer in different shapes: { response } (Llama),
// OpenAI-style { choices }, or Responses-style { output }.
function aiText(resp) {
  if (typeof resp?.response === "string") return resp.response;
  const choice = resp?.choices?.[0];
  if (choice) return choice.message?.content ?? choice.text ?? "";
  for (const item of resp?.output || []) {
    if (item.type !== "message") continue;
    for (const c of item.content || []) if (typeof c.text === "string") return c.text;
  }
  return "";
}


// ---------- context for big moves: corporate actions + web search ----------

// Counters in the rows that moved a lot, or every counter when the question
// asks why. At most 2, so a long list never triggers a burst of searches.
function countersToExplain(question, rows) {
  const why = WHY_RE.test(question);
  const big = (r) =>
    ["change_pct", "adj_change_pct", "chg_pct_filled"].some((k) => Math.abs(Number(r[k])) >= BIG_DAY_MOVE_PCT) ||
    ["pct_change_usd", "usd_return_pct", "adj_usd_return_pct"].some((k) => Math.abs(Number(r[k])) >= BIG_PERIOD_MOVE_PCT);
  const bigNames = [], otherNames = [];
  for (const r of rows) {
    const c = typeof r.counter === "string" ? r.counter.trim() : "";
    if (!c) continue;
    if (big(r)) { if (!bigNames.includes(c)) bigNames.push(c); }
    else if (why && !otherNames.includes(c)) otherNames.push(c);
  }
  const names = [...bigNames, ...otherNames.filter((c) => !bigNames.includes(c))];
  // A "why" question about one counter's rows: explain that counter.
  return { counters: names.slice(0, 2), why };
}

async function corporateActionsFor(env, counters) {
  if (!counters.length) return [];
  try {
    const marks = counters.map(() => "?").join(",");
    const res = await env.DB.prepare(
      `SELECT ex_date, counter, action, ratio, amount_usd, note, source FROM corporate_actions
       WHERE counter IN (${marks}) ORDER BY ex_date DESC LIMIT 10`
    ).bind(...counters).all();
    return res.results || [];
  } catch (e) {
    // Table missing before the first rebuild -- carry on without it.
    console.error("corporate_actions lookup failed:", e?.message || e);
    return [];
  }
}

// The date range a move happened in, from the rows, for the search query.
function moveDates(rows, counter) {
  const dates = rows
    .filter((r) => r.counter === counter)
    .flatMap((r) => [r.date, r.start_date, r.end_date])
    .filter((d) => typeof d === "string" && /^\d{4}-\d{2}-\d{2}$/.test(d))
    .sort();
  return dates.length ? `${dates[0]} to ${dates[dates.length - 1]}` : "recent months";
}

// One Claude call with the web search tool. Returns { text, sources } or null.
async function searchNews(env, counters, rows, actions) {
  if (!env.ANTHROPIC_API_KEY || !counters.length) return null;
  const targets = counters
    .map((c) => {
      const row = rows.find((r) => r.counter === c) || {};
      return `- ${c} (${row.market || "ZSE/VFEX"}, Zimbabwe), move between ${moveDates(rows, c)}`;
    })
    .join("\n");
  const known = actions.length ? `\nAlready on record: ${JSON.stringify(actions).slice(0, 1500)}` : "";
  try {
    const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY, maxRetries: 1, timeout: 45_000 });
    const resp = await client.messages.create({
      model: env.SEARCH_MODEL || CLAUDE_SEARCH_MODEL,
      max_tokens: 700,
      tools: [{ type: "web_search_20250305", name: "web_search", max_uses: 3 }],
      system:
        "You research listed Zimbabwean companies. Search for company announcements, results, " +
        "corporate actions (splits, consolidations, rights issues, dividends), suspensions or news " +
        "that explain a share price move. Prefer the ZSE/VFEX, the company's site, africanfinancials.com " +
        "and Zimbabwean business press. Reply in at most 4 short factual sentences: what happened, when, " +
        "and the source. If nothing explains the move, say so in one sentence. Do not speculate.",
      messages: [{ role: "user", content: `Why did these share prices move?\n${targets}${known}` }],
    });
    const text = resp.content.filter((b) => b.type === "text").map((b) => b.text).join("").trim();
    const sources = [];
    const add = (url, title) => {
      if (url && /^https?:\/\//.test(url) && !sources.some((s) => s.url === url)) sources.push({ url, title: title || url });
    };
    for (const b of resp.content) {
      for (const c of b.citations || []) add(c.url, c.title);
    }
    return text ? { text, sources: sources.slice(0, 4) } : null;
  } catch (e) {
    console.error("web search failed:", e?.message || e);
    return null;
  }
}

// ---------- question log (anonymous, 30 days) ----------
// One row per question in D1. No IP address and no user agent are kept: only
// the country Cloudflare reports and "mobile"/"desktop". The session id is a
// random value the page makes on load and drops on Clear. This table is not in
// schema.sql, so the daily data rebuild never deletes it.
const LOG_RETENTION_DAYS = 30;
const LOG_DDL = `CREATE TABLE IF NOT EXISTS ask_log (
  ts TEXT, session TEXT, question TEXT, counters TEXT, outcome TEXT, row_count INTEGER,
  sql_by TEXT, answer_by TEXT, searched INTEGER, sources INTEGER, said_no_data INTEGER,
  latency_ms INTEGER, country TEXT, device TEXT, caimex_error TEXT)`;
const LOG_COLUMNS = "ts, session, question, counters, outcome, row_count, sql_by, answer_by, searched, " +
  "sources, said_no_data, latency_ms, country, device, caimex_error";
// Tables made before caimex_error existed get the column once per Worker instance.
let logSchemaChecked = false;
const NO_DATA_RE = /\b(no data|don't have (?:any )?data|do not have (?:any )?data|not available|outside what I can)\b/i;

async function logAsk(env, m) {
  if (!env.DB || !m.question) return;
  const v = (x) => (x === undefined ? null : x);
  const cutoff = new Date(Date.now() - LOG_RETENTION_DAYS * 86400000).toISOString();
  if (!logSchemaChecked) {
    logSchemaChecked = true;
    try {
      await env.DB.prepare(LOG_DDL).run();
      await env.DB.prepare("ALTER TABLE ask_log ADD COLUMN caimex_error TEXT").run();
    } catch (e) {
      // "duplicate column name" -- the column is already there.
    }
  }
  try {
    await env.DB.batch([
      env.DB.prepare(LOG_DDL),
      env.DB.prepare("CREATE INDEX IF NOT EXISTS idx_ask_log_ts ON ask_log(ts)"),
      env.DB.prepare(`INSERT INTO ask_log (${LOG_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).bind(
        new Date(m.t0).toISOString(), v(m.session), m.question.slice(0, MAX_QUESTION_CHARS),
        JSON.stringify(m.counters || []), v(m.outcome), v(m.rowCount), v(m.sqlBy), v(m.answerBy),
        m.searched ? 1 : 0, v(m.sources), m.saidNoData ? 1 : 0, Date.now() - m.t0,
        v(m.country), v(m.device), m.caimexErrors ? m.caimexErrors.join("; ").slice(0, 400) : null
      ),
      env.DB.prepare("DELETE FROM ask_log WHERE ts < ?").bind(cutoff),
    ]);
  } catch (e) {
    console.error("ask_log write failed:", e?.message || e);
  }
}

async function handleAsk(request, env, origin, meta = {}) {
  let body;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: "Expected JSON body { question: string }" }), {
      status: 400,
      headers: { "Content-Type": "application/json", ...corsHeaders(origin) },
    });
  }

  const question = String(body.question || "").trim();
  meta.session = /^[a-z0-9-]{8,40}$/i.test(String(body.session || "")) ? String(body.session) : null;
  meta.outcome = "bad_request";
  if (!question) {
    return new Response(JSON.stringify({ error: "Missing 'question'." }), {
      status: 400,
      headers: { "Content-Type": "application/json", ...corsHeaders(origin) },
    });
  }
  meta.question = question;
  if (question.length > MAX_QUESTION_CHARS) {
    return new Response(
      JSON.stringify({ error: `Please keep questions under ${MAX_QUESTION_CHARS} characters.` }),
      { status: 400, headers: { "Content-Type": "application/json", ...corsHeaders(origin) } }
    );
  }

  const history = cleanHistory(body.history);
  const prompt = withHistory(question, history);

  // Step 1: text -> SQL
  const sqlMessages = [
    { role: "system", content: SQL_SYSTEM_PROMPT },
    { role: "user", content: prompt },
  ];
  let { sql, by: sqlBy } = await writeSql(env, sqlMessages, meta);
  const check = validateSql(sql);
  meta.sqlBy = sqlBy;

  if (!check.ok) {
    meta.outcome = check.reason === "not_answerable" ? "not_answerable" : "bad_sql";
    const answer =
      check.reason === "not_answerable"
        ? "I can only answer questions about ZSE/VFEX prices, volumes, sectors and the site's watchlist -- that one's outside what I can query."
        : "I couldn't turn that into a safe query -- try rephrasing it as a more specific question about a counter, sector, or date range.";
    return new Response(JSON.stringify({ answer, sql: null, rows: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json", ...corsHeaders(origin) },
    });
  }

  // Step 2: run the query against D1. If it fails, show the model its query
  // and the error once and let it write a corrected one.
  let rows;
  try {
    rows = (await env.DB.prepare(sql).all()).results || [];
  } catch (e) {
    const retry = await writeSql(env, [
      ...sqlMessages,
      { role: "assistant", content: sql },
      {
        role: "user",
        content: `That query failed with: ${String(e && e.message ? e.message : e).slice(0, 300)}\n` +
          "Write a corrected query. Output ONLY the SQL.",
      },
    ], meta);
    if (validateSql(retry.sql).ok) {
      try {
        rows = (await env.DB.prepare(retry.sql).all()).results || [];
        sql = retry.sql;
        sqlBy = retry.by;
      } catch (e2) {
        console.error("retry query failed:", e2);
      }
    }
  }
  meta.sqlBy = sqlBy;
  if (!rows) {
    meta.outcome = "query_failed";
    return new Response(
      JSON.stringify({
        answer: "That query didn't run cleanly against the database -- try asking a simpler or more specific version of the question.",
        sql,
        rows: [],
      }),
      { status: 200, headers: { "Content-Type": "application/json", ...corsHeaders(origin) } }
    );
  }

  // Step 3: SQL results -> natural-language answer. Caimex first when
  // configured (the free model ignored instructions, e.g. said "no data" for a
  // price from the nearest trading day); Workers AI otherwise.
  const { counters } = countersToExplain(question, rows);
  const actions = await corporateActionsFor(env, counters);
  const news = await searchNews(env, counters, rows, actions);
  let context = "";
  if (actions.length) context += `\n\nCorporate actions on record (JSON):\n${JSON.stringify(actions).slice(0, 2000)}`;
  if (news) {
    context += `\n\nWeb findings (news/announcements, NOT from the price data):\n${news.text}` +
      (news.sources.length ? `\nSources: ${news.sources.map((s) => s.title).join("; ")}` : "");
  }
  const answerMessages = [
    { role: "system", content: answerSystemPrompt(rows.length) },
    {
      role: "user",
      content: `${prompt}\n\nQuery results (JSON):\n${JSON.stringify(rows).slice(0, 4000)}${context}`,
    },
  ];
  let answer = null, answerBy = MODEL;
  if (env.CAIMEX_API_KEY && env.CAIMEX_ANSWER_MODEL) {
    answer = await caimexChat(env, env.CAIMEX_ANSWER_MODEL, answerMessages, 3000, meta, "answer");
    if (answer) answerBy = env.CAIMEX_ANSWER_MODEL;
  }
  if (!answer) answer = aiText(await env.AI.run(MODEL, { messages: answerMessages }));

  Object.assign(meta, {
    outcome: rows.length ? "ok" : "empty",
    rowCount: rows.length,
    counters: [...new Set(rows.map((r) => r.counter).filter((c) => typeof c === "string"))].slice(0, 10),
    answerBy,
    searched: !!news,
    sources: news ? news.sources.length : 0,
    saidNoData: NO_DATA_RE.test(answer || ""),
  });

  return new Response(
    JSON.stringify({
      answer: (answer || "").trim(), sql, sqlBy, answerBy, rows: rows.slice(0, 20),
      actions, sources: news ? news.sources : [], searched: !!news,
    }),
    { status: 200, headers: { "Content-Type": "application/json", ...corsHeaders(origin) } }
  );
}

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get("Origin") || "";
    const meta = {
      t0: Date.now(),
      outcome: "error",
      country: request.cf?.country || null,
      device: /Mobi|Android|iPhone|iPad/i.test(request.headers.get("User-Agent") || "") ? "mobile" : "desktop",
    };
    const log = () => ctx?.waitUntil?.(logAsk(env, meta));

    // Browsers always send Origin on a cross-site POST, so a missing or
    // unknown one is either another website or a script -- refuse both.
    if (!allowedOrigins(env).includes(origin)) {
      return new Response("Forbidden", { status: 403 });
    }

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
    }
    if (request.method !== "POST") {
      return new Response("POST { question: string, history?: [{ question, answer, sql }] } to this endpoint.", {
        status: 405,
        headers: corsHeaders(origin),
      });
    }
    // Origin can be faked outside a browser, so also cap requests per client IP.
    // The binding is optional: without it (e.g. local dev) this check is skipped.
    if (env.RATE_LIMITER) {
      const ip = request.headers.get("CF-Connecting-IP") || "unknown";
      const { success } = await env.RATE_LIMITER.limit({ key: ip });
      if (!success) {
        meta.outcome = "rate_limited";
        meta.question = "(rate limited)";
        log();
        return new Response(
          JSON.stringify({ error: "Too many questions in a short time -- please wait a minute and try again." }),
          { status: 429, headers: { "Content-Type": "application/json", ...corsHeaders(origin) } }
        );
      }
    }

    try {
      const res = await handleAsk(request, env, origin, meta);
      log();
      return res;
    } catch (e) {
      console.error("handleAsk failed:", e);
      meta.outcome = "error";
      log();
      return new Response(JSON.stringify({ error: "Something went wrong answering that -- please try again." }), {
        status: 500,
        headers: { "Content-Type": "application/json", ...corsHeaders(origin) },
      });
    }
  },
};
