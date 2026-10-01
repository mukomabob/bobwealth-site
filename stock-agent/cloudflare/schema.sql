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
  "traded"            INTEGER,
  "usd_return_pct"    REAL,
  "return_since"      TEXT
);

CREATE INDEX idx_counter_date ON prices(counter, date);
CREATE INDEX idx_date ON prices(date);

-- The latest model run from predictions.json (scripts/generate_predictions.py):
-- one row per counter the model scored for price_date.
DROP TABLE IF EXISTS predictions;

CREATE TABLE predictions (
  "price_date"        TEXT,
  "counter"           TEXT,
  "market"            TEXT,
  "sector"            TEXT,
  "close"             REAL,
  "usd_price"         REAL,
  "today_chg_pct"     REAL,
  "traded_today"      INTEGER,
  "predicted_chg_pct" REAL,
  "risk_score"        REAL,
  "volatility_band"   TEXT,
  "days_traded_20"    INTEGER,
  "signal"            TEXT,
  "watchlist_rank"    INTEGER
);

-- One row: how the watchlist has done when replayed over recent days, and
-- the model's error against simply assuming no change.
DROP TABLE IF EXISTS model_track_record;

CREATE TABLE model_track_record (
  "price_date"                        TEXT,
  "training_days"                     INTEGER,
  "days"                              INTEGER,
  "beat_market_days"                  INTEGER,
  "beat_market_days_pct"              REAL,
  "picks_avg_next_day_pct"            REAL,
  "market_avg_next_day_pct"           REAL,
  "biggest_fallers_avg_next_day_pct"  REAL,
  "picks_total"                       INTEGER,
  "picks_rose_next_day_pct"           REAL,
  "picks_flat_next_day_pct"           REAL,
  "picks_fell_next_day_pct"           REAL,
  "holdout_days"                      INTEGER,
  "model_mae"                         REAL,
  "no_change_mae"                     REAL
);
