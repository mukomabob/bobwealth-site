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
import csv
import glob
import math
import os
import re
import sqlite3
import sys

import pandas as pd

ZSE_SECTION_RE = re.compile(r"ZSE (ETF|REIT)", re.IGNORECASE)

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

            # Market comes from the section each row sits in, as in
            # publish_fbc_prices.py: rows after "VFEX PRICE SHEET" are VFEX
            # until the ZiG-priced "ZSE ETF"/"ZSE REIT" sections switch back.
            # (A hard-coded list of VFEX names went stale: it missed Pfuma
            # REIT and Eagle REIT.)
            # Blank cells stay NaN under pandas 3's astype(str), so convert
            # each value by hand.
            market, mkt = [], "ZSE"
            for v in df["COUNTER"]:
                first = "" if pd.isna(v) else str(v).strip()
                if "VFEX PRICE SHEET" in first.upper():
                    mkt = "VFEX"
                elif ZSE_SECTION_RE.match(first):
                    mkt = "ZSE"
                market.append(mkt)
            df["_section_market"] = market

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
    # read_excel types these as object (the leaderboard block below the data
    # puts text in them, and cutting those rows off does not re-type the
    # column), so make them numeric; any stray non-number becomes missing.
    df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce")
    df["ytd_gain_loss"] = pd.to_numeric(df["ytd_gain_loss"], errors="coerce")

    df["market"] = df["_section_market"]
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
    # The USD column comes through as 0.0 on those rows (Edgars from 29 Sep 2026),
    # which a query reads as a 100% fall. No close means no price.
    df["usd_price_ibr"] = pd.to_numeric(df["usd_price_ibr"], errors="coerce")
    df.loc[no_price | (df["usd_price_ibr"] <= 0), "usd_price_ibr"] = float("nan")

    grp = df.groupby("counter")

    df["chg_pct_filled"] = df["change_pct"].fillna(0)
    df["roll5_chg"] = grp["chg_pct_filled"].transform(lambda x: x.rolling(5, min_periods=1).mean())
    df["roll20_std_chg"] = grp["change_pct"].transform(lambda x: x.rolling(20, min_periods=1).std())
    df["traded"] = (df["volume"].fillna(0) > 0).astype(int)

    return df


def add_usd_returns(df):
    """USD return from the same starting point the site's Investment Simulator uses.

    ytd_gain_loss is FBC's ZiG figure against its own base date, so on its own
    the chat agent named a different "best performer" than the chart beside it.
    The starting prices come from scripts/publish_fbc_prices.py itself -- the
    6 Jan 2026 sheet, else a published listing price, else the counter's first
    archived price -- so the two cannot drift apart.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import publish_fbc_prices as pub

    base, base_date = pub.load_baseline()
    starts = {k: {"price": v["price"], "since": base_date} for k, v in base.items()}
    starts.update(pub.load_late_listing_baselines(set(base), "9999-12-31"))

    key = df["counter"].str.strip().str.upper()
    start_price = key.map(lambda k: starts.get(k, {}).get("price"))
    start_date = key.map(lambda k: starts.get(k, {}).get("since"))
    usd = pd.to_numeric(df["usd_price_ibr"], errors="coerce")
    usable = start_price.notna() & (usd > 0) & (df["date"].dt.strftime("%Y-%m-%d") >= start_date.fillna("9999"))
    df["usd_return_pct"] = ((usd / start_price - 1) * 100).where(usable)
    df["return_since"] = start_date.where(usable)
    log(f"USD returns: {int(usable.sum()):,} row(s) across {df.loc[usable, 'counter'].nunique()} counters "
        f"measured from {base_date} or their listing.")
    return df


# ---------------------------------------------------------------------------
# Corporate actions: splits, consolidations, bonus issues, dividends.
#
# A split makes the raw price fall (or a consolidation makes it jump) with no
# change in what a holder owns. Without an adjustment the chatbot reads that as
# a crash -- West Prop on 28 Oct 2026 showed as -97.6% when its shares in issue
# went from 30 million to 1 billion. Two sources feed the corporate_actions table:
#   1. stock-agent/corporate_actions.csv -- entered by hand from announcements.
#   2. Auto-detection -- a price move of 2x or more that the sheets' own
#      shares-in-issue column matches (within 1.5x) in the opposite direction.
# Price-affecting actions (split / consolidation / bonus) drive the adj_* columns.
# ---------------------------------------------------------------------------
ACTIONS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corporate_actions.csv")
PRICE_ACTIONS = {"split", "consolidation", "bonus"}
ACTION_COLUMNS = ["ex_date", "counter", "action", "ratio", "amount_usd", "note", "source"]


def load_corporate_actions(path=ACTIONS_CSV):
    """Hand-entered actions. Lines starting with # are comments."""
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    out = []
    for r in csv.DictReader(lines):
        action = (r.get("action") or "").strip().lower()
        if not r.get("ex_date") or not r.get("counter") or not action:
            continue
        ratio = float(r["ratio"]) if (r.get("ratio") or "").strip() else None
        if action in PRICE_ACTIONS and not ratio:
            log(f"WARNING: corporate_actions.csv {action} for {r['counter']} has no ratio -- ignored.")
            continue
        out.append({
            "ex_date": r["ex_date"].strip(), "counter": r["counter"].strip(), "action": action,
            "ratio": ratio,
            "amount_usd": float(r["amount_usd"]) if (r.get("amount_usd") or "").strip() else None,
            "note": (r.get("note") or "").strip() or None,
            "source": (r.get("source") or "").strip() or "corporate_actions.csv",
        })
    return out


def detect_splits(df, manual):
    """Price jumps of 2x+ that the shares-in-issue column explains."""
    manual_dates = {(a["counter"].upper(), a["ex_date"]) for a in manual if a["action"] in PRICE_ACTIONS}
    found = []
    for counter, g in df.sort_values("date").groupby("counter"):
        g = g.reset_index(drop=True)
        dates = g["date"].dt.strftime("%Y-%m-%d").tolist()
        price = pd.to_numeric(g["usd_price_ibr"], errors="coerce").tolist()
        shares = pd.to_numeric(g["shares_in_issue"], errors="coerce").tolist()
        last = None  # (index, price) of the last row with a price
        for i, p in enumerate(price):
            if not (p == p and p > 0):
                continue
            if last is not None:
                move = p / last[1]
                if move <= 0.5 or move >= 2:
                    best = None
                    # FBC updates shares in issue a day or so after the price.
                    for j in range(max(1, i - 3), min(len(g), i + 4)):
                        s0, s1 = shares[j - 1], shares[j]
                        if s0 == s0 and s1 == s1 and s0 > 0 and s1 > 0 and abs(math.log(s1 / s0)) > math.log(1.5):
                            err = abs(math.log(move * s1 / s0))
                            if best is None or err < best[0]:
                                best = (err, s1 / s0, s0, s1)
                    if best and best[0] < math.log(1.5):
                        ex = dates[i]
                        near = any(k[0] == counter.upper() and abs((pd.Timestamp(k[1]) - pd.Timestamp(ex)).days) <= 7
                                   for k in manual_dates)
                        if not near:
                            ratio = round(best[1], 6)
                            found.append({
                                "ex_date": ex, "counter": counter,
                                "action": "split" if ratio > 1 else "consolidation", "ratio": ratio,
                                "amount_usd": None,
                                "note": (f"Auto-detected from the price sheets: shares in issue went from "
                                         f"{best[2]:,.0f} to {best[3]:,.0f} while the USD price went from "
                                         f"{last[1]:g} to {p:g}. Treated as a {ratio:g}-for-1 "
                                         f"{'split' if ratio > 1 else 'consolidation'}; confirm against the "
                                         f"company announcement."),
                                "source": "auto-detected",
                            })
            last = (i, p)
    for a in found:
        log(f"Corporate action detected: {a['counter']} {a['action']} x{a['ratio']:g} on {a['ex_date']}")
    return found


def add_adjusted_prices(df, actions):
    """adj_* columns: prices and returns with splits/consolidations/bonus issues
    taken out. Prices are restated in today's share units; returns and daily
    changes become what a holder actually saw."""
    df["adj_usd_price"] = pd.to_numeric(df["usd_price_ibr"], errors="coerce")
    df["adj_change_pct"] = df["change_pct"]
    df["adj_usd_return_pct"] = df["usd_return_pct"]
    by_counter = {}
    for a in actions:
        if a["action"] in PRICE_ACTIONS and a["ratio"]:
            by_counter.setdefault(a["counter"].strip().upper(), []).append((a["ex_date"], a["ratio"]))
    for key, acts in by_counter.items():
        mask = df["counter"].str.strip().str.upper() == key
        if not mask.any():
            log(f"WARNING: corporate action for unknown counter {key!r} -- no adjustment made.")
            continue
        d = df.loc[mask, "date"].dt.strftime("%Y-%m-%d")

        def cum(day):  # product of ratios with ex_date on or before day
            return math.prod(r for ex, r in acts if ex <= day)

        total = math.prod(r for _, r in acts)
        cum_d = d.map(cum)
        df.loc[mask, "adj_usd_price"] = df.loc[mask, "adj_usd_price"] * cum_d / total
        on_ex = d.map(lambda day: math.prod(r for ex, r in acts if ex == day))
        df.loc[mask, "adj_change_pct"] = ((1 + df.loc[mask, "change_pct"] / 100) * on_ex - 1) * 100
        since = df.loc[mask, "return_since"]
        cum_since = since.map(lambda day: cum(day) if isinstance(day, str) else float("nan"))
        df.loc[mask, "adj_usd_return_pct"] = ((1 + df.loc[mask, "usd_return_pct"] / 100) * cum_d / cum_since - 1) * 100
    return df


def build(sheets_dir, db_path):
    combined = load_sheets(sheets_dir)
    combined = clean_rows(combined)
    df = standardize(combined)
    df = sanity_check(df)
    df = engineer_derived_fields(df)
    df = add_usd_returns(df)
    manual = load_corporate_actions()
    actions = manual + detect_splits(df, manual)
    df = add_adjusted_prices(df, actions)

    keep_cols = [
        "date", "counter", "isin", "market", "sector",
        "shares_in_issue", "mkt_cap_usd_ibr", "open_price_zig", "close",
        "usd_price_ibr", "change_pct", "volume", "value_traded_zig",
        "div_yield_fy25", "div_yield_fy26", "ytd_gain_loss",
        "chg_pct_filled", "roll5_chg", "roll20_std_chg", "traded",
        "usd_return_pct", "return_since",
        "adj_usd_price", "adj_change_pct", "adj_usd_return_pct",
    ]
    df = df[[c for c in keep_cols if c in df.columns]].copy()
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")

    # Several of these columns come back as pandas "object" dtype (a mix of
    # floats/None from the source sheets), which pandas.to_sql then writes
    # into SQLite as TEXT -- silently breaking numeric ORDER BY/aggregate
    # queries (SQLite sorts TEXT lexicographically: "9.5" > "14.8"). Force
    # every non-identifier column to a real numeric dtype before it touches
    # SQLite.
    text_cols = {"date", "counter", "isin", "market", "sector", "return_since"}
    for col in df.columns:
        if col not in text_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    df.to_sql("prices", conn, index=False)
    conn.execute("CREATE INDEX idx_counter_date ON prices(counter, date)")
    conn.execute("CREATE INDEX idx_date ON prices(date)")
    pd.DataFrame(actions, columns=ACTION_COLUMNS).sort_values(["counter", "ex_date"]).to_sql(
        "corporate_actions", conn, index=False)
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
