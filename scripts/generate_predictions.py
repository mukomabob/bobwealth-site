#!/usr/bin/env python3
"""
Generates and publishes predictions.json for bobwealth.org.

Ports Stages 1B-6B of zse_tensorflow_learner_v9.ipynb (the notebook Bob runs
manually in Colab) into a plain script GitHub Actions can run unattended --
reading the accumulated FBC price-sheet history from data/fbc-sheets/
instead of a Google Drive folder, so no Drive mount / Colab session is
needed. Retrains a fresh RandomForestRegressor on the full history every
run, exactly like re-running the notebook top to bottom would.

Two deliberate differences from the notebook (confirmed with Bob):

  1. FIXED SECTOR CHECK -- the notebook casts the sector column to string
     *before* checking whether it's a string, which defeats the sanity
     check meant to catch corrupted rows (a stray number landing in the
     sector position). This script captures each row's sector type BEFORE
     casting, so a corrupted row is actually caught as fatal.

  2. FATAL ROWS ARE DROPPED, NOT ABORTED -- in Colab a fatal row just stops
     the notebook and Bob goes and looks. In an unattended daily run, a
     hard stop means predictions.json silently goes stale with nobody
     watching the logs -- so a fatal row is logged as a warning and
     dropped, and every other counter still gets published that day.

This script only WRITES predictions.json to the working tree -- the
GitHub Actions workflow that calls it is responsible for git add/commit/
push (so the repo's own GITHUB_TOKEN handles auth, no PAT needed).

Exit codes:
  0 - success (including the no-op "already published today" case)
  1 - a real failure (no sheets found, nothing left after sanity check, etc.)
"""
import glob
import json
import os
import re
import sys
import warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import TimeSeriesSplit, train_test_split
from sklearn.preprocessing import StandardScaler

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHEETS_DIR = os.path.join(REPO_ROOT, "data", "fbc-sheets")
PREDICTIONS_PATH = os.path.join(REPO_ROOT, "predictions.json")

# ─── column / value tables, ported verbatim from the notebook ──────────────
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

ROWS_TO_DROP = {
    "VFEX ETF (USD$)", "VFEX BONDS (USD$)", "VFEX PRICE SHEET (USD$)", "VFEX REITS (USD$)",
    "ZSE ETF PRICES (ZiG)", "ZSE REIT PRICES (ZiG)", "ZSE Top Gainers",
    "This Price Sheet is based on PROVISIONAL information obtained during trading. "
    "No liability is accepted by FBC Securities (Private) Limited for any errors in this report.",
}

VFEX_COUNTERS = {
    "BINDURA", "PADENGA", "CALEDONIA", "SEEDCO INTL",
    "AFRICAN SUN", "AXIA", "INNSCOR", "NATFOODS",
    "SIMBISA", "FIDELITY", "GETBUCKS", "NMBZ",
    "OLD MUTUAL", "ZIMRE HOLD", "ARISTON", "MASH HOLDINGS",
    "TSL", "WILLDALE",
}

VALID_SECTORS = {
    "Consumer Staples", "Consumer Discretionary", "Financials",
    "Industrials", "Materials", "Real Estate",
    "IT, Communication", "Energy", "Exchange Traded Fund",
}

FEATURE_COLS = [
    "chg_pct_filled", "roll5_chg", "roll10_chg", "roll5_vol",
    "traded_today", "days_since_trade", "sector_code", "market_code",
    "roll20_std_chg",
]
TARGET_COL = "next_chg_pct"


def log(msg):
    print(msg, flush=True)


# ─── Stage 1B: load every sheet, find its header row, extract its date ──────
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


# ─── Stage 1C/1D: strip non-data rows and junk columns ──────────────────────
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


# ─── Stage 1F/1G: rename, derive market -- capture sector's real type FIRST ─
def standardize(combined_df):
    df = combined_df.rename(
        columns={k: v for k, v in COLUMN_MAP.items() if k in combined_df.columns}
    ).copy()

    df["counter"] = df["counter"].astype(str).str.strip()
    df["date"] = pd.to_datetime(df["date"])

    # FIX (vs. the notebook): record whether sector was really a string
    # BEFORE casting it to one, so a corrupted row can still be caught.
    df["sector_is_str"] = df["sector"].apply(lambda x: isinstance(x, str))
    df["sector"] = df["sector"].astype(str)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

    df["market"] = df["counter"].apply(lambda x: "VFEX" if x.upper() in VFEX_COUNTERS else "ZSE")
    df = df.sort_values(["date", "counter"]).reset_index(drop=True)
    return df


# ─── Stage 2: sanity check -- drop fatal rows and continue, don't abort ────
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

    unknown = df[~df["sector"].isin(VALID_SECTORS) & (df["sector"] != "nan")]
    if len(unknown):
        log(
            f"NOTE: {len(unknown)} row(s) have a sector outside the known list (non-fatal): "
            f"{sorted(unknown['sector'].unique())}"
        )

    log(f"Sanity check: {fatal_mask.sum()} fatal row(s) dropped, {len(df):,} row(s) remain.")
    return df


# ─── Stage 4: feature engineering, ported verbatim ─────────────────────────
def engineer_features(df):
    df = df.sort_values(["counter", "date"]).reset_index(drop=True)
    grp = df.groupby("counter")

    df["chg_pct_filled"] = df["change_pct"].fillna(0)
    df["roll5_chg"] = grp["chg_pct_filled"].transform(lambda x: x.rolling(5, min_periods=1).mean())
    df["roll10_chg"] = grp["chg_pct_filled"].transform(lambda x: x.rolling(10, min_periods=1).mean())
    df["roll5_vol"] = grp["volume"].transform(lambda x: x.rolling(5, min_periods=1).mean())

    df["traded_today"] = (df["volume"] > 0).astype(int)

    def days_since_last_trade(grp_df):
        result = []
        last = None
        for _, row in grp_df.iterrows():
            if row["volume"] > 0:
                last = row["date"]
            result.append((row["date"] - last).days if last is not None else 999)
        return pd.Series(result, index=grp_df.index)

    df["days_since_trade"] = df.groupby("counter", group_keys=False).apply(days_since_last_trade)

    grp = df.groupby("counter")  # re-group: df was not re-sorted, but grp's cache may be stale
    df["roll20_std_chg"] = grp["change_pct"].transform(lambda x: x.rolling(20, min_periods=1).std())

    df["sector_code"] = df["sector"].astype("category").cat.codes
    df["market_code"] = (df["market"] == "VFEX").astype(int)
    df["next_chg_pct"] = df.groupby("counter")["change_pct"].shift(-1)

    return df


# ─── Stage 5: train a fresh RandomForestRegressor on the full history ─────
def train_model(df):
    model_df = df[FEATURE_COLS + [TARGET_COL]].dropna()
    model_df = model_df[model_df[TARGET_COL].abs() <= 50]

    if len(model_df) < 50:
        raise ValueError(f"Only {len(model_df)} trainable row(s) -- too little history to train on yet.")

    X = model_df[FEATURE_COLS].values
    y = model_df[TARGET_COL].values

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, shuffle=False)

    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train)
    X_test = scaler.transform(X_test)

    model = RandomForestRegressor(n_estimators=100, random_state=42, n_jobs=-1, max_depth=10, min_samples_leaf=5)

    # Cross-validate on the training split (time-ordered folds), same as the notebook.
    n_splits = min(5, max(2, len(X_train) // 50))
    mae_scores = []
    try:
        tscv = TimeSeriesSplit(n_splits=n_splits, gap=0)
        for train_idx, val_idx in tscv.split(X_train, y_train):
            model.fit(X_train[train_idx], y_train[train_idx])
            mae_scores.append(mean_absolute_error(y_train[val_idx], model.predict(X_train[val_idx])))
    except ValueError:
        pass  # not enough rows yet for the requested number of splits

    model.fit(X_train, y_train)  # final fit on the full training set

    if mae_scores:
        model_mae = round(float(np.mean(mae_scores)), 4)
    elif len(y_test):
        model_mae = round(float(mean_absolute_error(y_test, model.predict(X_test))), 4)
    else:
        model_mae = None

    log(f"Trained on {len(X_train):,} row(s), held out {len(X_test):,} for test. model_mae={model_mae}")
    return model, scaler, model_mae


# ─── Stage 6: predict on the latest trading day + export predictions.json ─
def safe_float(v, dp=4):
    try:
        f = float(v)
        return None if np.isnan(f) else round(f, dp)
    except (TypeError, ValueError):
        return None


def to_dict(row):
    return {
        "counter": str(row["counter"]),
        "market": str(row["market"]),
        "sector": str(row["sector"]),
        "close": safe_float(row["close"], 4),
        "usd_price": safe_float(row.get("usd_price_ibr"), 6),
        "today_chg_pct": safe_float(row.get("change_pct"), 2),
        "predicted_chg_pct": safe_float(row["predicted_chg_pct"], 2),
        "confidence_score": safe_float(row["confidence_score"], 3),
        "risk_score": safe_float(row["risk_score"], 2),
        "signal": str(row["signal"]),
    }


def build_predictions(df, model, scaler, model_mae):
    latest_date = df["date"].max()
    today_rows = df[df["date"] == latest_date].copy()

    all_pred_cols = FEATURE_COLS + ["counter", "market", "sector", "close", "usd_price_ibr", "change_pct"]
    pred_df = today_rows[all_pred_cols].dropna(subset=FEATURE_COLS).copy()

    if pred_df.empty:
        raise ValueError(
            f"No counters with complete features on {latest_date.date()} -- refusing to publish an empty snapshot."
        )

    X_pred = scaler.transform(pred_df[FEATURE_COLS].values)
    tree_preds = np.array([t.predict(X_pred) for t in model.estimators_])
    preds = model.predict(X_pred).flatten()

    pred_df["predicted_chg_pct"] = preds.round(2)
    pred_std = preds.std()
    if pred_std > 0:
        pred_df["confidence_score"] = (1 - (tree_preds.std(axis=0) / pred_std)).clip(0, 1).round(3)
    else:
        pred_df["confidence_score"] = 1.0
    pred_df["risk_score"] = pred_df["roll20_std_chg"].round(2)
    pred_df["signal"] = pred_df["predicted_chg_pct"].apply(
        lambda x: "bullish" if x > 0 else ("bearish" if x < 0 else "neutral")
    )
    pred_df = pred_df.sort_values("predicted_chg_pct", ascending=False).reset_index(drop=True)

    snapshot = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "price_date": str(latest_date.date()),
        "training_days": int(df["date"].nunique()),
        "model_mae": model_mae,
        "disclaimer": "Algorithmic signals only. Not financial advice.",
        "top_bullish": [to_dict(r) for _, r in pred_df.head(5).iterrows()],
        "top_bearish": [to_dict(r) for _, r in pred_df.tail(5).iterrows()],
        "all_predictions": [to_dict(r) for _, r in pred_df.iterrows()],
    }
    return snapshot


def main():
    warnings.filterwarnings("ignore")

    existing = None
    if os.path.exists(PREDICTIONS_PATH):
        with open(PREDICTIONS_PATH) as f:
            existing = json.load(f)

    combined = load_sheets(SHEETS_DIR)
    combined = clean_rows(combined)
    df = standardize(combined)
    df = sanity_check(df)

    if df.empty:
        print("Every row was dropped by the sanity check -- refusing to publish.", file=sys.stderr)
        return 1

    df = engineer_features(df)
    model, scaler, model_mae = train_model(df)
    snapshot = build_predictions(df, model, scaler, model_mae)

    if existing and existing.get("price_date") == snapshot["price_date"]:
        print(f"predictions.json already covers {snapshot['price_date']} -- nothing to do.")
        return 0

    with open(PREDICTIONS_PATH, "w") as f:
        json.dump(snapshot, f, indent=2)
        f.write("\n")

    log(
        f"Wrote predictions.json -- price_date={snapshot['price_date']}, "
        f"{len(snapshot['all_predictions'])} counters, model_mae={model_mae}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
