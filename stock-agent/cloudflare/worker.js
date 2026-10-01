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
 *   env.ANTHROPIC_API_KEY - optional Worker secret. When set, Claude writes
 *             the SQL (step 1); Workers AI still words the answer (step 2)
 *             and takes over step 1 if Claude is unavailable.
 */

import Anthropic from "@anthropic-ai/sdk";
import { SQL_SYSTEM_PROMPT, extractSql, validateSql } from "./sql_prompt.js";

const MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast";
// Writing the query is where the answers went wrong (wrong day, wrong counter),
// so that step goes to Claude. Haiku 4.5 is the cheapest Claude model.
const CLAUDE_SQL_MODEL = "claude-haiku-4-5";

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

const ANSWER_SYSTEM_PROMPT = `You are a stock-market assistant for the Zimbabwe Stock Exchange (ZSE) and
VFEX. You are given a user's question and the exact rows a SQL query
returned for it. Answer the question in 2-4 sentences using ONLY the numbers
in those rows -- never state a figure that isn't present in the data. If the
rows are empty, say plainly that there's no data for that question rather
than guessing. Earlier turns, if shown, only explain what the new question refers to --
take every figure from the new rows, never from an earlier answer. For a question about one counter, describe it from its row: sector and
market, latest price, the day's change, and its US-dollar return since return_since. Name a counter's sector
ONLY if the rows include its sector value -- never guess or infer one. When the answer is about a
single counter, explain its sector in one short clause for a reader who may not know the term
(in a list of several counters, just name each sector -- no explanations), using ONLY the matching
definition below (word it naturally, don't add to it):
${SECTOR_DEFINITIONS}
The table has no company descriptions -- if asked what the company does, give only its sector
and that definition.
If there ARE rows, they are the answer: the query already did
the ranking or filtering, so never say there is no data -- state what the rows
show, even if they carry only names.

Formatting rules -- follow these exactly, the raw column values are not
already in a form fit to read aloud:
- change_pct, chg_pct_filled, roll5_chg, roll20_std_chg, div_yield_fy25,
  div_yield_fy26 are already percentages (2.94 means 2.94%) -- state with a
  % sign, rounded to 1-2 decimals.
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
- End any answer about the watchlist or model with: "Algorithmic signals only, not financial advice."`;

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

async function writeSql(env, messages) {
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
      return extractSql(text);
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
  // The free Workers AI model for this step is set by SQL_MODEL in wrangler.toml.
  const resp = await env.AI.run(env.SQL_MODEL || MODEL, { messages, max_tokens: 2048 });
  return extractSql(aiText(resp));
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
