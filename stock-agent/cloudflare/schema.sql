-- D1 schema for the FBC stock-trend chat agent.
-- Mirrors the table build_dataset.py writes to fbc_history.db (SQLite) --
-- see stock-agent/README.md for the full column reference.

DROP TABLE IF EXISTS prices;

CREATE TABLE prices (
  "date"              TEXT,
  "counter"           TEXT,
  "isin"              TEXT,
  "market"            TEXT,
  "sector"            TEXT,
  "shares_in_issue"   REAL,
  "mkt_cap_usd_ibr"   REAL,
  "open_price_zig"    REAL,
  "close"             REAL,
  "usd_price_ibr"     REAL,
  "change_pct"        REAL,
  "volume"            REAL,
  "value_traded_zig"  REAL,
  "div_yield_fy25"    REAL,
  "div_yield_fy26"    REAL,
  "ytd_gain_loss"     REAL,
  "chg_pct_filled"    REAL,
  "roll5_chg"         REAL,
  "roll20_std_chg"    REAL,
  "traded"            INTEGER
);

CREATE INDEX idx_counter_date ON prices(counter, date);
CREATE INDEX idx_date ON prices(date);
