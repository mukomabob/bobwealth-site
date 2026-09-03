#!/usr/bin/env python3
"""
The "structured data agent" query layer for fbc_history.db.

Design: rather than hand-coding a fixed function per question type (which
only ever answers the questions you thought of), this exposes one safe,
general primitive -- run_sql() -- plus the table schema and a few worked
examples. An LLM given this primitive as a *tool* (Claude API tool-use, an
MCP server, a Cowork skill, etc.) can translate an arbitrary natural-language
trend question into SQL itself and get back exact, grounded numbers -- the
same pattern "text-to-SQL" agents and products like Network Copilot's
telemetry queries use, just pointed at stock data instead of network data.

Safety: run_sql() only permits a single read-only SELECT statement against
the local file -- no writes, no multiple statements, no PRAGMA/ATTACH tricks.

SCHEMA (table: prices, one row per counter per trading day):
    date              TEXT    'YYYY-MM-DD'
    counter           TEXT    e.g. 'Pfuma REIT', 'Old Mutual'
    isin              TEXT
    market            TEXT    'ZSE' or 'VFEX'
    sector            TEXT    e.g. 'Riet', 'Financials', 'Consumer Staples'
    shares_in_issue   REAL
    mkt_cap_usd_ibr   REAL
    open_price_zig    REAL
    close             REAL    closing price, ZiG
    usd_price_ibr     REAL    USD-denominated price (use this for cross-day USD trend/return math)
    change_pct        REAL    day's % change as reported by FBC
    volume            REAL    shares traded that day
    value_traded_zig  REAL
    div_yield_fy25    REAL
    div_yield_fy26    REAL
    ytd_gain_loss     REAL
    chg_pct_filled    REAL    change_pct with NaN->0 (safe to SUM/AVG)
    roll5_chg         REAL    5-day rolling mean of chg_pct_filled, per counter
    roll20_std_chg    REAL    20-day rolling std of change_pct, per counter (volatility proxy)
    traded            INTEGER 1 if volume > 0 else 0

Usage as a library:
    from query_engine import run_sql, SCHEMA_DOC
    run_sql("SELECT counter, usd_price_ibr FROM prices WHERE date = '2026-09-02' ORDER BY usd_price_ibr DESC LIMIT 5")
"""
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "fbc_history.db"

with open(__file__) as _f:
    SCHEMA_DOC = _f.read().split('"""')[1]

_SELECT_ONLY_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_FORBIDDEN_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|PRAGMA|VACUUM|REPLACE|CREATE)\b",
    re.IGNORECASE,
)


def run_sql(query: str, max_rows: int = 200) -> str:
    """Execute a single read-only SELECT against fbc_history.db and return
    the result as a small text table. Raises ValueError for anything that
    isn't a plain SELECT, or for multiple statements."""
    query = query.strip().rstrip(";")
    if ";" in query:
        raise ValueError("Only a single statement is allowed.")
    if not _SELECT_ONLY_RE.match(query):
        raise ValueError("Only SELECT statements are allowed.")
    if _FORBIDDEN_RE.search(query):
        raise ValueError("Query contains a disallowed keyword.")

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        cur = conn.execute(query)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows)
    finally:
        conn.close()

    if not rows:
        return "(no rows)"

    widths = [max(len(str(c)), max((len(str(r[i])) for r in rows), default=0)) for i, c in enumerate(cols)]
    lines = [" | ".join(str(c).ljust(w) for c, w in zip(cols, widths))]
    lines.append("-+-".join("-" * w for w in widths))
    for r in rows:
        lines.append(" | ".join(str(v).ljust(w) for v, w in zip(r, widths)))
    suffix = "" if len(rows) < max_rows else f"\n... (truncated at {max_rows} rows)"
    return "\n".join(lines) + suffix


if __name__ == "__main__":
    import sys
    print(run_sql(sys.argv[1] if len(sys.argv) > 1 else "SELECT COUNT(*) FROM prices"))
