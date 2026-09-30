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
 * Bindings expected (see wrangler.toml):
 *   env.DB  - D1 database bound as "DB", containing the `prices`,
 *             `predictions` and `model_track_record` tables (schema.sql /
 *             build_dataset.py / export_to_d1.py build these)
 *   env.AI  - Workers AI binding, bound as "AI"
 */

const MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast";

const SCHEMA_DOC = `
Table: prices (one row per counter per trading day, ZSE/VFEX stock exchange data for Zimbabwe)
  date              TEXT    'YYYY-MM-DD'
  counter           TEXT    company/counter name, e.g. 'Pfuma REIT', 'Old Mutual'
  isin              TEXT
  market            TEXT    'ZSE' or 'VFEX'
  sector            TEXT    e.g. 'Reit', 'Financials', 'Consumer Staples'
  shares_in_issue   REAL
  mkt_cap_usd_ibr   REAL
  open_price_zig    REAL
  close             REAL    closing price, ZiG
  usd_price_ibr     REAL    USD-denominated price -- use this for cross-day USD trend/return math
  change_pct        REAL    that day's % change as reported by FBC
  volume            REAL    shares traded that day
  value_traded_zig  REAL
  div_yield_fy25    REAL
  div_yield_fy26    REAL
  ytd_gain_loss     REAL    FRACTION of price change vs the 31-Dec-2024 baseline,
                            in ZiG (local-currency) terms -- e.g. 0.05 means +5%,
                            6.54 means +654%. NOT already a percentage, and NOT
                            the same as a USD-converted return over the same period.
  chg_pct_filled    REAL    change_pct with NULL -> 0 (safe to SUM/AVG)
  roll5_chg         REAL    5-day rolling mean of chg_pct_filled, per counter
  roll20_std_chg    REAL    20-day rolling std of change_pct, per counter (volatility proxy)
  traded            INTEGER 1 if volume > 0 else 0
  usd_return_pct    REAL    % change in US DOLLARS since return_since -- the same figure the
                            site's Investment Simulator charts ($100 -> 100 + usd_return_pct).
                            Already a percentage. NULL before a counter's start date.
  return_since      TEXT    'YYYY-MM-DD' start of usd_return_pct: 2026-01-06, or the listing /
                            first-price date for counters listed later

Table: predictions (the site's ML watchlist: one row per counter from the latest model run only)
  price_date        TEXT    'YYYY-MM-DD' price sheet the model scored
  counter           TEXT    same names as prices.counter
  market            TEXT
  sector            TEXT
  close             REAL    closing price, ZiG
  usd_price         REAL    USD price
  today_chg_pct     REAL    that day's % change, already a percentage
  traded_today      INTEGER 1 if it traded on price_date
  predicted_chg_pct REAL    model's predicted next-day % change -- use ONLY to rank/order
  risk_score        REAL    20-day volatility of daily % changes (higher = jumpier)
  signal            TEXT    'bullish', 'bearish' or 'neutral'
  watchlist_rank    INTEGER 1-5 for the counters on today's watchlist, NULL otherwise

Table: model_track_record (exactly one row: how the watchlist has done, replayed day by day)
  price_date                        TEXT
  training_days                     INTEGER trading days the model learned from
  days                              INTEGER days replayed
  beat_market_days                  INTEGER days the 5 picks' average beat the market average
  beat_market_days_pct              REAL    same, as a percentage
  picks_avg_next_day_pct            REAL    picks' average next-day % change
  market_avg_next_day_pct           REAL    all counters' average next-day % change
  biggest_fallers_avg_next_day_pct  REAL    no-model rule (buy the day's biggest fallers), for comparison
  picks_total                       INTEGER individual picks replayed
  picks_rose_next_day_pct           REAL    % of individual picks that rose the next day
  picks_flat_next_day_pct           REAL    % unchanged
  picks_fell_next_day_pct           REAL    % that fell
  holdout_days                      INTEGER recent days used to test prediction error
  model_mae                         REAL    model's average miss, in percentage points
  no_change_mae                     REAL    average miss from simply predicting no change
`.trim();

const SQL_SYSTEM_PROMPT = `You are a SQLite query writer for a stock-market database. Given a user's
question, write exactly ONE read-only SQL query (SELECT, or WITH ... SELECT)
that answers it, using only the tables below. Output ONLY the SQL query --
no explanation, no markdown code fences, no trailing semicolon commentary.
If the question has nothing to do with the stocks, prices, volumes, sectors or the site's
watchlist/model in these tables, output exactly: NONE

${SCHEMA_DOC}

Rules:
- Only SELECT or WITH ... SELECT. Never INSERT/UPDATE/DELETE/DROP/etc.
- For performance questions -- best/worst performer, gainers/losers, returns, "this year",
  "since January" -- use usd_return_pct on the most recent date. Use ytd_gain_loss ONLY when
  the user explicitly asks for ZiG / local-currency terms.
- Use usd_price_ibr for price questions unless the user asks about ZiG specifically.
- A question naming a counter -- "what's ART?", "tell me about Delta", "how is Econet doing?" --
  with no past date or period IS answerable: return that counter's most recent row with date, market, sector,
  usd_price_ibr, close, change_pct, volume, usd_return_pct and return_since. Match the name
  case-insensitively and loosely, e.g. WHERE UPPER(counter) LIKE UPPER('%ART%'), and prefer an
  exact match when several counters contain the text.
- Always SELECT the column you ranked, filtered or aggregated by, next to counter -- never
  the counter name alone. A row with only a name gives the answer nothing to cite.
- When querying the most recent date, also SELECT date (and return_since for usd_return_pct)
  so the answer can say what the figures are as of.
- "today" / "most recent" means the MAX(date) in the table, not a real-world date.
- Relative times -- "2 months ago", "last week", "a month back" -- count back from MAX(date),
  and markets are shut some days, so take the last trading date on or before that point,
  e.g. "Pfuma price 2 months ago":
    SELECT date, counter, usd_price_ibr, close FROM prices
    WHERE UPPER(counter) LIKE UPPER('%Pfuma%')
      AND date <= (SELECT date(MAX(date), '-2 months') FROM prices)
    ORDER BY date DESC LIMIT 1
- Questions about the watchlist, picks, signals, what the model/AI thinks, "bullish", "risk"
  use the predictions table: the watchlist is WHERE watchlist_rank IS NOT NULL ORDER BY
  watchlist_rank. Select price_date, signal, risk_score and watchlist_rank; you may ORDER BY
  predicted_chg_pct but do not SELECT it.
- Questions about how accurate the model is, its track record, or whether the picks work
  use model_track_record: SELECT * FROM model_track_record.
- If earlier turns are given, use them only to work out what "it", "that one", "and Delta?",
  "the same for VFEX" etc. refer to. Write SQL for the NEW question only.
- Always LIMIT results to at most 20 rows unless the question clearly needs a single aggregate.`;

const ANSWER_SYSTEM_PROMPT = `You are a stock-market assistant for the Zimbabwe Stock Exchange (ZSE) and
VFEX. You are given a user's question and the exact rows a SQL query
returned for it. Answer the question in 2-4 sentences using ONLY the numbers
in those rows -- never state a figure that isn't present in the data. If the
rows are empty, say plainly that there's no data for that question rather
than guessing. Earlier turns, if shown, only explain what the new question refers to --
take every figure from the new rows, never from an earlier answer. For a question about one counter, describe it from its row: sector and
market, latest price, the day's change, and its US-dollar return since return_since. The
table has no company descriptions -- if asked what the company does, say only its sector.
If there ARE rows, they are the answer: the query already did
the ranking or filtering, so never say there is no data -- state what the rows
show, even if they carry only names.

Formatting rules -- follow these exactly, the raw column values are not
already in a form fit to read aloud:
- change_pct, chg_pct_filled, roll5_chg, roll20_std_chg, div_yield_fy25,
  div_yield_fy26 are already percentages (2.94 means 2.94%) -- state with a
  % sign, rounded to 1-2 decimals.
- usd_return_pct is already a percentage in US dollars since return_since. State it as
  "+X% in US dollars since <return_since>", and you may add what $100 would now be worth
  (100 + usd_return_pct). This is the same measure as the site's Investment Simulator.
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
  by its rank, signal and risk_score (20-day volatility: higher means jumpier).
- For the track record, give the group result (beat the market on beat_market_days of days;
  picks averaged picks_avg_next_day_pct vs market_avg_next_day_pct) AND the weaker per-pick
  result (picks_rose_next_day_pct% of individual picks rose the next day).
- For accuracy, compare model_mae with no_change_mae honestly: if model_mae is larger, say
  the model's predicted percentages have missed by more than simply assuming no change.
- End any answer about the watchlist or model with: "Algorithmic signals only, not financial advice."`;

const SELECT_ONLY_RE = /^\s*(SELECT|WITH)\b/i;
const FORBIDDEN_RE = /\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|PRAGMA|VACUUM|REPLACE|CREATE)\b/i;

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

function extractSql(raw) {
  let sql = raw.trim();
  // Strip markdown code fences if the model added them anyway.
  const fenced = sql.match(/```(?:sql)?\s*([\s\S]*?)```/i);
  if (fenced) sql = fenced[1].trim();
  sql = sql.replace(/;+\s*$/, "").trim();
  return sql;
}

function validateSql(sql) {
  if (!sql || sql.toUpperCase() === "NONE") {
    return { ok: false, reason: "not_answerable" };
  }
  if (sql.includes(";")) {
    return { ok: false, reason: "multiple_statements" };
  }
  if (!SELECT_ONLY_RE.test(sql)) {
    return { ok: false, reason: "not_a_select" };
  }
  if (FORBIDDEN_RE.test(sql)) {
    return { ok: false, reason: "forbidden_keyword" };
  }
  return { ok: true };
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

async function writeSql(env, messages) {
  const resp = await env.AI.run(MODEL, { messages });
  return extractSql(resp.response || "");
}

async function handleAsk(request, env, origin) {
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
  if (!question) {
    return new Response(JSON.stringify({ error: "Missing 'question'." }), {
      status: 400,
      headers: { "Content-Type": "application/json", ...corsHeaders(origin) },
    });
  }
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
  let sql = await writeSql(env, sqlMessages);
  const check = validateSql(sql);

  if (!check.ok) {
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
    const retrySql = await writeSql(env, [
      ...sqlMessages,
      { role: "assistant", content: sql },
      {
        role: "user",
        content: `That query failed with: ${String(e && e.message ? e.message : e).slice(0, 300)}\n` +
          "Write a corrected query. Output ONLY the SQL.",
      },
    ]);
    if (validateSql(retrySql).ok) {
      try {
        rows = (await env.DB.prepare(retrySql).all()).results || [];
        sql = retrySql;
      } catch (e2) {
        console.error("retry query failed:", e2);
      }
    }
  }
  if (!rows) {
    return new Response(
      JSON.stringify({
        answer: "That query didn't run cleanly against the database -- try asking a simpler or more specific version of the question.",
        sql,
        rows: [],
      }),
      { status: 200, headers: { "Content-Type": "application/json", ...corsHeaders(origin) } }
    );
  }

  // Step 3: SQL results -> natural-language answer
  const answerResp = await env.AI.run(MODEL, {
    messages: [
      { role: "system", content: ANSWER_SYSTEM_PROMPT },
      {
        role: "user",
        content: `${prompt}\n\nQuery results (JSON):\n${JSON.stringify(rows).slice(0, 4000)}`,
      },
    ],
  });

  return new Response(
    JSON.stringify({ answer: (answerResp.response || "").trim(), sql, rows: rows.slice(0, 20) }),
    { status: 200, headers: { "Content-Type": "application/json", ...corsHeaders(origin) } }
  );
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";

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
        return new Response(
          JSON.stringify({ error: "Too many questions in a short time -- please wait a minute and try again." }),
          { status: 429, headers: { "Content-Type": "application/json", ...corsHeaders(origin) } }
        );
      }
    }

    try {
      return await handleAsk(request, env, origin);
    } catch (e) {
      console.error("handleAsk failed:", e);
      return new Response(JSON.stringify({ error: "Something went wrong answering that -- please try again." }), {
        status: 500,
        headers: { "Content-Type": "application/json", ...corsHeaders(origin) },
      });
    }
  },
};
