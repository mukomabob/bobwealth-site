#!/usr/bin/env python3
"""
Builds a single clean, queryable SQLite database from the full archive of
FBC Securities daily price sheets (data/fbc-sheets/*.xlsx in the
bobwealth-site repo).

This is the "structured data agent" foundation: instead of chunking the
sheets into text and embedding them for vector search (which is a poor fit
for numeric trend questions -- "who gained most this week", "what's Pfuma
REIT's price over the last month"), every sheet is parsed into one tidy
table so an agent can answer those questions with exact SQL/pandas
aggregation instead of approximate text retrieval.

Reuses the same load_sheets() / clean_rows() / standardize() logic already
validated (in generate_predictions.py) against all 111 archived sheets:
0 fatal rows dropped, 64 unique counters, 111 unique trading days.

Usage:
    python3 build_dataset.py /path/to/fbc-sheets-dir /path/to/output.db
"""
import glob
import os
import re
import sqlite3
import sys

import pandas as pd

VFEX_COUNTERS = {
    "BINDURA", "PADENGA", "CALEDONIA", "SEEDCO INTL",
    "AFRICAN SUN", "AXIA", "INNSCOR", "NATFOODS",
    "SIMBISA", "FIDELITY", "GETBUCKS", "NMBZ",
    "OLD MUTUAL", "ZIMRE HOLD", "ARISTON", "MASH HOLDINGS",
    "TSL", "WILLDALE",
}

ROWS_TO_DROP = {
    "VFEX ETF (USD$)", "VFEX BONDS (USD$)", "VFEX PRICE SHEET (USD$)", "VFEX REITS (USD$)",
    "ZSE ETF PRICES (ZiG)", "ZSE REIT PRICES (ZiG)", "ZSE Top Gainers",
    "This Price Sheet is based on PROVISIONAL information obtained during trading. "
    "No liability is accepted by FBC Securities (Private) Limited for any errors in this report.",
}

# See scripts/generate_predictions.py in bobwealth-site for the full story on
# why this hard stop exists: the "Top Gainers/Losers" leaderboard block at
# the end of each sheet packs four unrelated mini-tables side by side per
# row, so it can't be read with the normal single-counter column layout.
HARD_STOP_RE = re.compile(r"ZSE TOP|VFEX TOP|THIS PRICE SHEET", re.IGNORECASE)

COLUMN_MAP = {
    "COUNTER": "counter",
    "Isin": "isin",
    "# of Shares": "shares_in_issue",
    "US$ Market Cap (IBR)": "mkt_cap_usd_ibr",
    "US$ Market Cap (PMR)": "mkt_cap_usd_pmr",
    "Sector": "sector",
    "ZIG Prices 31 Dec 2024": "zig_prices_dec_24",
    "Opening Price (ZiG)": "open_price_zig",
    "Closing Price (ZiG)": "close",
    "USD Prices ($) @IBR": "usd_price_ibr",
    "USD Prices ($) @PMR": "usd_price_pmr",
    "Price p": "price_p",
    "Price % p": "change_pct",
    "Total Volume Traded": "volume",
    "Total Value Traded (ZiG$)": "value_traded_zig",
    "Div Yield-FY25": "div_yield_fy25",
    "Div Yield-FY26": "div_yield_fy26",
    "YTD Gain/Loss": "ytd_gain_loss",
    "Div": "div",
    "Date": "date",
}


def log(msg):
    print(msg, flush=True)


def load_sheets(sheets_dir):
    files = sorted(glob.glob(os.path.join(sheets_dir, "*.xlsx")))
    if not files:
        raise ValueError(f"No .xlsx files found in {sheets_dir}")

    all_dfs = []
    for file_path in files:
        file_name = os.path.basename(file_path)
        try:
            temp_df = pd.read_excel(file_path, header=None, nrows=10)
            header_row_index = temp_df[
                temp_df.astype(str)
                .apply(lambda x: x.str.contains("COUNTER", case=False, na=False))
                .any(axis=1)
            ].index
            if header_row_index.empty:
                log(f"WARNING: 'COUNTER' header not found in {file_name} -- skipping.")
                continue

            df = pd.read_excel(file_path, header=header_row_index[0])
            df.dropna(how="all", inplace=True)
            df.reset_index(drop=True, inplace=True)
            df.columns = df.columns.astype(str).str.replace("\n", " ", regex=False).str.strip()

            hard_stop_rows = df.index[
                df["COUNTER"].astype(str).str.contains(HARD_STOP_RE, na=False)
            ]
            if len(hard_stop_rows):
                df = df.iloc[: hard_stop_rows[0]]

            date_match = re.search(r"(\d{2}[._]\d{2}[._]\d{2})\.xlsx", file_name)
            if date_match:
                date_str = date_match.group(1).replace("_", ".")
                df["Date"] = pd.to_datetime(date_str, format="%d.%m.%y", errors="coerce")
            else:
                log(f"WARNING: could not extract a date from {file_name} -- rows will have no date.")
                df["Date"] = pd.NaT

            all_dfs.append(df)
        except Exception as e:
            log(f"WARNING: error loading {file_name}: {e}")

    if not all_dfs:
        raise ValueError("No sheets loaded successfully.")

    combined = pd.concat(all_dfs, ignore_index=True)
    log(f"Loaded {len(combined):,} rows from {len(all_dfs)} sheet(s) ({len(files)} file(s) found).")
    return combined


def clean_rows(combined_df):
    combined_df = combined_df.dropna(subset=["COUNTER"]).copy()

    unnamed_cols = [c for c in combined_df.columns if "Unnamed:" in c]
    if unnamed_cols:
        mask = (
            combined_df[unnamed_cols]
            .astype(str)
            .apply(lambda x: x.str.contains("Counter", case=False, na=False))
            .any(axis=1)
        )
        combined_df = combined_df[~mask]
    combined_df.reset_index(drop=True, inplace=True)

    if "EPS" in combined_df.columns:
        combined_df = combined_df.drop(columns=["EPS"])

    combined_df = combined_df[~combined_df["COUNTER"].isin(ROWS_TO_DROP)]

    unnamed_to_drop = [c for c in combined_df.columns if "Unnamed:" in c]
    if unnamed_to_drop:
        combined_df = combined_df.drop(columns=unnamed_to_drop)

    cols = ["Date"] + [c for c in combined_df.columns if c != "Date"]
    combined_df = combined_df[cols].reset_index(drop=True)
    return combined_df


def standardize(combined_df):
    df = combined_df.rename(
        columns={k: v for k, v in COLUMN_MAP.items() if k in combined_df.columns}
    ).copy()

    df["counter"] = df["counter"].astype(str).str.strip()
    df["date"] = pd.to_datetime(df["date"])

    df["sector_is_str"] = df["sector"].apply(lambda x: isinstance(x, str))
    df["sector"] = df["sector"].astype(str)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

    df["market"] = df["counter"].apply(lambda x: "VFEX" if x.upper() in VFEX_COUNTERS else "ZSE")
    df = df.sort_values(["date", "counter"]).reset_index(drop=True)

    # FBC's own sheets spell the REIT sector two different ways depending on
    # the day ("Reit" vs "Riet") -- same sector, just an upstream typo that
    # varies by sheet. Normalize so sector-level aggregates (e.g. "how did
    # REITs do this year") aren't silently split across two labels. Distinct
    # from "Real Estate", which is FBC's separate, legitimate category.
    df["sector"] = df["sector"].replace({"Riet": "Reit"})

    return df


def sanity_check(df):
    fatal_mask = pd.Series(False, index=df.index)
    dropped = []

    for idx, row in df.iterrows():
        row_reasons = []
        if pd.isna(row["counter"]) or not isinstance(row["counter"], str):
            row_reasons.append("BAD_COUNTER")
        if row["market"] not in {"ZSE", "VFEX"}:
            row_reasons.append("BAD_MARKET")
        if not row["sector_is_str"]:
            row_reasons.append("BAD_SECTOR")
        if not pd.isna(row["close"]):
            try:
                float(row["close"])
            except (TypeError, ValueError):
                row_reasons.append("BAD_CLOSE")
        if not pd.isna(row["volume"]):
            try:
                float(row["volume"])
            except (TypeError, ValueError):
                row_reasons.append("BAD_VOLUME")

        if row_reasons:
            fatal_mask.at[idx] = True
            dropped.append((idx, row.get("counter"), row_reasons))

    for idx, counter, row_reasons in dropped:
        log(f"WARNING: dropping row {idx} (counter={counter!r}): {', '.join(row_reasons)}")

    df = df[~fatal_mask].reset_index(drop=True).drop(columns=["sector_is_str"])
    log(f"Sanity check: {fatal_mask.sum()} fatal row(s) dropped, {len(df):,} row(s) remain.")
    return df


def engineer_derived_fields(df):
    """A handful of fields that make trend questions cheap to answer without
    the agent having to hand-roll window functions every time."""
    df = df.sort_values(["counter", "date"]).reset_index(drop=True)

    # When a row has no closing price (the counter didn't trade that day /
    # was suspended), the source sheet's own "Price % p" and "YTD Gain/Loss"
    # cells are still populated -- but with meaningless placeholder/error
    # values (observed: flat -100%, -1, or +100) rather than being blank.
    # Left in place, these get picked up by MAX()/MIN() trend queries (e.g.
    # "best performer this year") as if they were real price moves. Null
    # them out here so only rows backed by an actual close price can ever
    # look like a real gain or loss.
    no_price = df["close"].isna()
    n_scrubbed = int(no_price.sum())
    if n_scrubbed:
        log(f"Scrubbing change_pct/ytd_gain_loss on {n_scrubbed} row(s) with no close price "
            f"(counters: {sorted(df.loc[no_price, 'counter'].unique())})")
    # Use float NaN (not pd.NA) so change_pct/ytd_gain_loss stay numeric
    # dtype -- pd.NA would upcast the column to object and break the
    # rolling().std() call below.
    df["change_pct"] = df["change_pct"].astype(float)
    df["ytd_gain_loss"] = df["ytd_gain_loss"].astype(float)
    df.loc[no_price, ["change_pct", "ytd_gain_loss"]] = float("nan")

    grp = df.groupby("counter")

    df["chg_pct_filled"] = df["change_pct"].fillna(0)
    df["roll5_chg"] = grp["chg_pct_filled"].transform(lambda x: x.rolling(5, min_periods=1).mean())
    df["roll20_std_chg"] = grp["change_pct"].transform(lambda x: x.rolling(20, min_periods=1).std())
    df["traded"] = (df["volume"].fillna(0) > 0).astype(int)

    return df


def build(sheets_dir, db_path):
    combined = load_sheets(sheets_dir)
    combined = clean_rows(combined)
    df = standardize(combined)
    df = sanity_check(df)
    df = engineer_derived_fields(df)

    keep_cols = [
        "date", "counter", "isin", "market", "sector",
        "shares_in_issue", "mkt_cap_usd_ibr", "open_price_zig", "close",
        "usd_price_ibr", "change_pct", "volume", "value_traded_zig",
        "div_yield_fy25", "div_yield_fy26", "ytd_gain_loss",
        "chg_pct_filled", "roll5_chg", "roll20_std_chg", "traded",
    ]
    df = df[[c for c in keep_cols if c in df.columns]].copy()
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")

    # Several of these columns come back as pandas "object" dtype (a mix of
    # floats/None from the source sheets), which pandas.to_sql then writes
    # into SQLite as TEXT -- silently breaking numeric ORDER BY/aggregate
    # queries (SQLite sorts TEXT lexicographically: "9.5" > "14.8"). Force
    # every non-identifier column to a real numeric dtype before it touches
    # SQLite.
    text_cols = {"date", "counter", "isin", "market", "sector"}
    for col in df.columns:
        if col not in text_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    df.to_sql("prices", conn, index=False)
    conn.execute("CREATE INDEX idx_counter_date ON prices(counter, date)")
    conn.execute("CREATE INDEX idx_date ON prices(date)")
    conn.commit()

    n_counters = df["counter"].nunique()
    n_days = df["date"].nunique()
    date_min, date_max = df["date"].min(), df["date"].max()
    log(
        f"Wrote {db_path} -- {len(df):,} rows, {n_counters} counters, "
        f"{n_days} trading days ({date_min} to {date_max})."
    )
    conn.close()
    return db_path


if __name__ == "__main__":
    sheets_dir = sys.argv[1] if len(sys.argv) > 1 else "data/fbc-sheets"
    db_path = sys.argv[2] if len(sys.argv) > 2 else "fbc_history.db"
    build(sheets_dir, db_path)
