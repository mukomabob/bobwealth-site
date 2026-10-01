#!/usr/bin/env python3
"""
Automates the daily FBC Securities price-sheet import for bobwealth.org.

Replicates, field-for-field, the parseSheet() / handleCurrentSheet() /
publishToGitHub() logic that already lives in markets.html's admin panel,
so market-data.json comes out identical in shape to a manual PIN-panel
upload. This script only WRITES market-data.json to the working tree —
the GitHub Actions workflow that calls it is responsible for git add/
commit/push (so the repo's own GITHUB_TOKEN handles auth, no PAT needed).

Env vars required:
  GMAIL_ADDRESS       - the Gmail account the FBC sheet arrives at
  GMAIL_APP_PASSWORD  - a Gmail "app password" for IMAP access

Exit codes:
  0  - success (including the no-op "nothing to do yet" case)
  1  - a real failure (bad credentials, unparseable sheet, etc.)
"""
import email
import imaplib
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKET_DATA_PATH = os.path.join(REPO_ROOT, "market-data.json")
BASELINE_PATH = os.path.join(REPO_ROOT, "Price Sheet 06.01.26.xlsx")
BASELINE_FNAME = "Price_Sheet_06_01_26.xlsx"  # underscored on purpose — see sheet_date()
SHEETS_DIR = os.path.join(REPO_ROOT, "data", "fbc-sheets")  # archive for the predictions pipeline

FBC_SENDER = "FBCSECURITIESRESEARCH@fbc.co.zw"
HARARE = ZoneInfo("Africa/Harare")

EXCLUDED_SECTORS = {"Fixed Term  Bond", "Derivative"}

# Rows whose first cell just introduces a sub-section (e.g. "VFEX REITS (USD$)")
# -- skip that one row, but the real counter rows right after it use the same
# column layout as the main sheet, so keep reading normally.
EXCLUDED_ROW_RE = re.compile(
    r"VFEX BONDS|VFEX REITS|VFEX ETF|ZSE ETF|ZSE REIT",
    re.IGNORECASE,
)

# Rows that mark the start of the "Top Gainers/Losers" leaderboard block (and
# the trailing disclaimer after it). That block packs four unrelated
# mini-tables side by side per row (name, price, blank, %change, repeated),
# so it can't be read with the normal single-counter column layout -- and
# because its first cell is just a normal-looking company name (whoever is
# topping the leaderboard that day), earlier code had no way to tell those
# rows apart from real ones. Once we see this, there's nothing usable left
# in the file, so stop reading entirely instead of skipping row-by-row.
HARD_STOP_RE = re.compile(r"ZSE TOP|VFEX TOP|THIS PRICE SHEET", re.IGNORECASE)
ZSE_SECTION_RE = re.compile(r"ZSE (ETF|REIT)", re.IGNORECASE)


# ─── numeric helpers (mirror JS parseFloat(...)||0 / ||null quirks) ──────────
def parse_float(v):
    if v is None:
        return float("nan")
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    m = re.match(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", s)
    if not m or m.group(0) == "":
        return float("nan")
    try:
        return float(m.group(0))
    except ValueError:
        return float("nan")


def pf_or_zero(v):
    f = parse_float(v)
    return 0 if math.isnan(f) else f


def pf_or_null(v):
    f = parse_float(v)
    if math.isnan(f) or f == 0:
        return None
    return f


# ─── date extraction (mirrors the JS regex + fallback exactly) ──────────────
def sheet_date(fname):
    m = re.search(r"(\d{2})[_-](\d{2})[_-](\d{2,4})", fname)
    if m:
        dd, mm, yy = m.group(1), m.group(2), m.group(3)
        yy2 = yy[-2:]
        return f"20{yy2}-{mm}-{dd}"
    return re.sub(r"\.(xlsx?|XLSX?)$", "", fname)


# ─── the actual sheet parser — a line-for-line port of parseSheet() ────────
def parse_sheet(raw, fname):
    hdr = -1
    for i, row in enumerate(raw):
        if row and any(str(c or "").strip().upper() == "COUNTER" for c in row):
            hdr = i
            break
    if hdr < 0:
        raise ValueError("No COUNTER header row found.")

    date = sheet_date(fname)
    mkt = "ZSE"
    rows = []

    for i in range(hdr + 1, len(raw)):
        r = raw[i]
        if not r or r[0] is None or r[0] == "":
            continue
        first = str(r[0]).strip()
        if not first:
            continue
        if HARD_STOP_RE.search(first):
            break
        if "VFEX PRICE SHEET" in first.upper():
            mkt = "VFEX"
            continue
        # The ZiG-priced ZSE ETF/REIT sections come after the VFEX ones, so the
        # market has to switch back here; otherwise their ZiG closes (e.g.
        # Revitus at 2.00) are read as VFEX US-dollar prices.
        if ZSE_SECTION_RE.match(first):
            mkt = "ZSE"
            continue
        if EXCLUDED_ROW_RE.search(first):
            continue

        def cell(idx):
            return r[idx] if idx < len(r) else None

        c1 = cell(1)
        if math.isnan(parse_float(c1)) and not isinstance(c1, str):
            continue

        sect = str(cell(5) or "").strip()
        if sect in EXCLUDED_SECTORS:
            continue

        close_px = parse_float(cell(8))
        raw13 = cell(13)
        is_susp = "SUSPEND" in str(raw13 or "").upper() or "NOT FOUND" in str(raw13 or "").upper()
        usd_price = (
            (None if math.isnan(close_px) else close_px)
            if mkt == "VFEX"
            else pf_or_null(cell(9))
        )

        rows.append({
            "counter": first,
            "isin": str(c1 or "").strip(),
            "shares": pf_or_zero(cell(2)),
            "mktCapIBR": pf_or_zero(cell(3)),
            "sector": sect or "Other",
            "open": pf_or_null(cell(7)),
            "close": None if math.isnan(close_px) else close_px,
            "usdIBR": pf_or_null(cell(9)),
            "chg": pf_or_zero(cell(11)),
            "chgPct": pf_or_zero(cell(12)),
            "vol": 0 if is_susp else pf_or_zero(cell(13)),
            "val": 0 if is_susp else pf_or_zero(cell(14)),
            "divYield": pf_or_zero(cell(15)),
            "ytd": pf_or_zero(cell(16)),
            "market": mkt,
            "suspended": is_susp,
            "usdPrice": usd_price,
        })

    return rows, date


def sheet_to_raw(xlsx_bytes):
    wb = load_workbook(BytesIO(xlsx_bytes), data_only=True)
    ws = wb.worksheets[0]
    return [list(row) for row in ws.iter_rows(values_only=True)]


# ─── baseline (the fixed Jan-6-2026 sheet already committed to the repo) ────
def load_baseline():
    with open(BASELINE_PATH, "rb") as f:
        raw = sheet_to_raw(f.read())
    rows, date = parse_sheet(raw, BASELINE_FNAME)
    base = {}
    for r in rows:
        if r["usdPrice"] and r["usdPrice"] > 0:
            base[r["counter"].strip().upper()] = {
                "price": r["usdPrice"], "market": r["market"], "sector": r["sector"],
            }
    return base, date


# ─── Gmail (IMAP) ───────────────────────────────────────────────────────────────
# ─── late listings: counters with no Jan-6 price ─────────────────────────────
# A counter listed after the baseline is measured from its published listing
# price when we know it (the price investors actually paid), and otherwise from
# its first price in data/fbc-sheets/. The archive only starts on 16 Mar 2026,
# so anything that listed before then needs an entry here. Checked against the
# archive: Econet InfraCo (first trade 31 Mar) and Old Mutual Limited (first
# trade 12 Aug, closed 78.17c) are already exact there and need no override.
LISTING_PRICES = {
    # 471.3m units placed at US$0.10, listed on VFEX 6 Feb 2026.
    # https://www.newzimbabwe.com/pfuma-reit-lists-on-vfex-after-us25-million-private-placement/
    "PFUMA REIT": {"price": 0.10, "since": "2026-02-06"},
}

ARCHIVE_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{2})\.xlsx$", re.IGNORECASE)


def archive_iso_date(fname):
    m = ARCHIVE_DATE_RE.match(fname)
    return f"20{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def load_late_listing_baselines(known, before_iso):
    """Earliest archived USD price for every counter the Jan-6 baseline lacks.

    Only sheets dated strictly before `before_iso` count, so a counter that
    first appears today starts tomorrow rather than showing a meaningless $100.
    Returns {COUNTER_UPPER: {"price": float, "since": "YYYY-MM-DD"}}.
    """
    if not os.path.isdir(SHEETS_DIR):
        return {}
    dated = sorted(
        (iso, name) for name in os.listdir(SHEETS_DIR)
        if (iso := archive_iso_date(name)) and iso < before_iso
    )
    late = {k: dict(v) for k, v in LISTING_PRICES.items() if k not in known and v["since"] < before_iso}
    for iso, name in dated:
        try:
            wb = load_workbook(os.path.join(SHEETS_DIR, name), read_only=True, data_only=True)
            raw = [list(row) for row in wb.worksheets[0].iter_rows(values_only=True)]
            wb.close()
            rows, _ = parse_sheet(raw, name)
        except Exception as e:  # one unreadable archive file must not block publishing
            print(f"Skipping archived sheet {name}: {e}", file=sys.stderr)
            continue
        for r in rows:
            key = r["counter"].strip().upper()
            if key in known or key in late:
                continue
            if r["usdPrice"] and r["usdPrice"] > 0:
                late[key] = {"price": r["usdPrice"], "since": iso}
    return late


def decode_str(s):
    parts = decode_header(s or "")
    out = []
    for text, enc in parts:
        out.append(text.decode(enc or "utf-8", errors="replace") if isinstance(text, bytes) else text)
    return "".join(out)


def fetch_recent_attachments():
    """Returns [(filename, bytes, sent_date), ...] for every FBC price-sheet
    attachment found in the last few days, oldest email first.

    Deliberately does NOT restrict the IMAP search to "today" only: FBC's send
    time has drifted later and later (observed as late as ~14:52 UTC / 16:52
    Harare in practice, well past a same-day polling window that stops
    earlier), so a same-day-only search can permanently miss a day's sheet
    the moment it arrives after the last scheduled tick -- the next day's
    "since today" search would then no longer include it.

    Every sheet in the window is returned, not just the newest: when a run
    fails or GitHub skips scheduled runs, the next good run still archives
    the days in between (30.09.26 was nearly lost this way). The actual
    price date is read from each attachment's own filename, not the email's
    arrival date.
    """
    since_date = (datetime.now(HARARE) - timedelta(days=5)).date()
    imap_date = since_date.strftime("%d-%b-%Y")  # IMAP SINCE wants e.g. 22-Aug-2026

    addr = os.environ["GMAIL_ADDRESS"]
    app_pw = os.environ["GMAIL_APP_PASSWORD"]

    conn = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        conn.login(addr, app_pw)
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, f'(FROM "{FBC_SENDER}" SINCE {imap_date})')
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        found = []
        for msg_id in data[0].split():
            typ, msg_data = conn.fetch(msg_id, "(RFC822)")
            if typ != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            try:
                sent = parsedate_to_datetime(msg["Date"]).astimezone(HARARE).date()
            except (TypeError, ValueError):
                sent = None
            for part in msg.walk():
                fname = part.get_filename()
                if not fname:
                    continue
                fname = decode_str(fname)
                if fname.lower().endswith((".xlsx", ".xls")):
                    found.append((fname, part.get_payload(decode=True), sent))
        return found
    finally:
        conn.logout()


# ─── main ────────────────────────────────────────────────────────────────────
def archive_name(fname, sent=None):
    """'DD.MM.YY.xlsx' from the attachment's filename. FBC occasionally
    mistypes the month (the sheet sent on 15 May 2026 is named "15.04.26"),
    and the sheets carry no date of their own, so the email's send date is
    the cross-check: a name more than 5 days off with the same day of the
    month takes the send date. Any other mismatch is left as named (a
    genuinely old sheet resent later)."""
    date_match = re.search(r"\d{2}\.\d{2}\.\d{2}", fname)
    if not date_match:
        return fname
    name = f"{date_match.group(0)}.xlsx"
    try:
        named = datetime.strptime(date_match.group(0), "%d.%m.%y").date()
    except ValueError:
        return name
    if sent and abs(named - sent) > timedelta(days=5) and named.day == sent.day:
        fixed = sent.strftime("%d.%m.%y") + ".xlsx"
        print(f"Sheet {fname!r} was sent on {sent}; archiving it as {fixed}.")
        return fixed
    return name


def main():
    found = [(fname, blob, sent) for fname, blob, sent in fetch_recent_attachments() if blob]
    if not found:
        print("No FBC price-sheet email found in the last 5 days — nothing to do.")
        return 0

    # One sheet per price date; if FBC sent a date twice, the later email wins.
    by_name = {}
    for fname, blob, sent in found:
        by_name[archive_name(fname, sent)] = (fname, blob)

    # Archive the raw sheets for the predictions pipeline (generate_predictions.py
    # reads every file here to rebuild the full training history). Days missing
    # from the archive are filled in; the newest is always rewritten so a
    # same-day resend replaces it (identical bytes are a no-op for git).
    newest = max(by_name, key=lambda n: archive_iso_date(n) or "")
    os.makedirs(SHEETS_DIR, exist_ok=True)
    for name, (_, blob) in sorted(by_name.items()):
        path = os.path.join(SHEETS_DIR, name)
        if name == newest or not os.path.exists(path):
            if name != newest:
                print(f"Archiving missed sheet {name}.")
            with open(path, "wb") as f:
                f.write(blob)

    fname, blob = by_name[newest]
    parse_fname = newest

    # idempotency: skip if market-data.json already has this date or a later one
    if os.path.exists(MARKET_DATA_PATH):
        with open(MARKET_DATA_PATH) as f:
            existing = json.load(f)
        expected_date = sheet_date(parse_fname)
        if existing.get("priceDate") == expected_date:
            print(f"market-data.json already has priceDate {expected_date} — nothing to do.")
            return 0
        existing_iso = archive_iso_date(f"{existing.get('priceDate')}.xlsx")
        newest_iso = archive_iso_date(parse_fname)
        if existing_iso and newest_iso and newest_iso < existing_iso:
            print(f"market-data.json already has a later priceDate ({existing.get('priceDate')}) — nothing to do.")
            return 0

    raw = sheet_to_raw(blob)
    rows, price_date = parse_sheet(raw, parse_fname)
    if not rows:
        print("Parsed the sheet but found zero counters — refusing to publish an empty snapshot.", file=sys.stderr)
        return 1

    base, base_date = load_baseline()
    today_iso = archive_iso_date(parse_fname) or datetime.now(HARARE).strftime("%Y-%m-%d")
    late = load_late_listing_baselines(set(base), today_iso)

    def start_point(r):
        key = r["counter"].strip().upper()
        if key in base:
            return base[key]["price"], None
        if key in late:
            return late[key]["price"], late[key]["since"]
        return None, None

    def inv100(r):
        price, _ = start_point(r)
        if price and price > 0 and r["usdPrice"] and r["usdPrice"] > 0:
            return (r["usdPrice"] / price) * 100
        return None

    now = datetime.now(timezone.utc)
    generated = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    snapshot = {
        "generated": generated,
        "priceDate": price_date,
        "baseDate": base_date,
        "counters": [
            {
                "counter": r["counter"].strip(),
                "isin": r["isin"],
                "market": r["market"],
                "sector": r["sector"],
                "shares": r["shares"],
                "mktCapIBR": r["mktCapIBR"],
                "open": r["open"],
                "close": r["close"],
                "usdIBR": r["usdIBR"],
                "chg": r["chg"],
                "chgPct": r["chgPct"],
                "vol": r["vol"],
                "val": r["val"],
                "divYield": r["divYield"],
                "ytd": r["ytd"],
                "suspended": r["suspended"],
                "usdPrice": r["usdPrice"],
                "inv100": inv100(r),
                "basePrice": start_point(r)[0],
                # Set only for counters missing from the Jan-6 baseline: the
                # date of the first archived price their $100 is measured from.
                "baseSince": start_point(r)[1],
            }
            for r in rows
        ],
    }

    with open(MARKET_DATA_PATH, "w") as f:
        json.dump(snapshot, f, indent=2)
        f.write("\n")

    print(f"Wrote market-data.json — priceDate={price_date}, {len(rows)} counters, source file '{fname}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

