# FBC stock-trend agent — structured data prototype

Live at: https://fbc-stock-agent.robertzata23.workers.dev/ (deployed via Cloudflare Workers Builds from this folder).

Answers natural-language questions about ZSE/VFEX trends by querying a clean
SQL table built from all 111 archived FBC price sheets, instead of doing
vector-embedding retrieval over the sheets as text.

## Why not classic RAG

Questions like "who gained most today" or "what's Pfuma REIT's return since
March" need exact aggregation across thousands of rows (MAX, GROUP BY,
window functions). Vector search retrieves the *most similar-looking* text
chunks — it doesn't compute anything, so it's a poor fit and tends to
either miss rows or hallucinate numbers. A structured agent (load once,
query with SQL) gives exact, checkable answers instead.

## Files

- `build_dataset.py` — parses all 111 sheets in `data/fbc-sheets/` into one
  tidy table and writes `fbc_history.db` (SQLite). Reuses the exact
  load/clean/standardize/sanity-check pipeline already validated in
  `scripts/generate_predictions.py` (0 fatal rows dropped, 64 counters, 111
  trading days). Adds a couple of derived columns (5-day rolling change,
  20-day rolling volatility) so trend questions don't need window-function
  gymnastics every time, and normalizes the "Reit"/"Riet" sector-spelling
  inconsistency found in FBC's own sheets.

  Run it whenever the sheet archive grows (e.g. from a GitHub Action, right
  after `publish_fbc_prices.py` archives each new day's sheet):
  ```
  python3 build_dataset.py data/fbc-sheets fbc_history.db
  ```

- `query_engine.py` — one safe primitive, `run_sql(query)`, that runs a
  single read-only SELECT (or WITH ... SELECT) against `fbc_history.db` and
  returns a small text table. Blocks writes, multiple statements, and
  PRAGMA/ATTACH tricks. The module docstring is the full schema reference.

## How this becomes a conversational agent

This prototype's `run_sql` is the *tool* — the same shape as Aviz Networks'
Network Copilot or HPE Aruba Copilot's underlying pattern, just pointed at
stock data instead of network telemetry. To wire up an actual chat
interface:

1. Register `run_sql` as a tool with an LLM that supports tool-calling
   (Claude API tool use, an MCP server exposing this as a resource, or a
   Cowork skill). Give the LLM the schema doc from `query_engine.py`'s
   module docstring as context.
2. User asks a question in plain language ("how did REITs do this
   quarter?"). The LLM writes the SQL, calls `run_sql`, gets real numbers
   back, and composes the answer — it never invents a figure, because
   every number in its answer came from an actual query result.
3. Guardrail: `run_sql` already refuses anything but a SELECT, so there's
   no path from a chat question to a database write — worth keeping
   whatever front-end you build in front of it.

## Validated example questions (real output, see session transcript)

- Top 5 gainers on the most recent trading day
- Price/return trend for a specific counter (e.g. Pfuma REIT) over its
  recent history
- Most volatile sector by 20-day rolling std of daily % change
- Best/worst cumulative USD return across the full 111-day archive

All four ran correctly against the real dataset before this was handed
over — including catching and fixing two real bugs along the way: numeric
columns silently landing as SQLite TEXT (breaking ORDER BY), and the
Reit/Riet sector-spelling split.
