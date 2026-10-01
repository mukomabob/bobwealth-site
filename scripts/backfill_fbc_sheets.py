#!/usr/bin/env python3
"""
One-time (but safely re-runnable) backfill: pulls every historical FBC
price-sheet attachment out of Gmail and archives it into data/fbc-sheets/,
so generate_predictions.py has the FULL trading history to train on --
not just whichever days happened to get saved to Drive by hand.

Triggered manually via workflow_dispatch (see backfill-fbc-sheets.yml),
NOT on a daily schedule -- this is a backfill, run once (or again later
if you ever suspect a gap), not part of the recurring pipeline. Safe to
re-run any time: a date already present in data/fbc-sheets/ is left
untouched, so re-running only picks up genuinely new gaps.

Uses the SAME Gmail secrets the daily workflow already has configured --
no new credentials needed:
  GMAIL_ADDRESS
  GMAIL_APP_PASSWORD

Exit codes:
  0 - success (including "nothing new found")
  1 - a real failure (bad credentials, IMAP error, etc.)
"""
import email
import imaplib
import os
import re
import sys
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHEETS_DIR = os.path.join(REPO_ROOT, "data", "fbc-sheets")
FBC_SENDER = "FBCSECURITIESRESEARCH@fbc.co.zw"
HARARE = ZoneInfo("Africa/Harare")

# Matches DD.MM.YY, DD-MM-YY, or DD_MM_YY anywhere in the filename --
# real attachment names look like "FBC Securities Price Sheet 09.06.26.xlsx".
DATE_RE = re.compile(r"(\d{2})[.\-_](\d{2})[.\-_](\d{2,4})")


def log(msg):
    print(msg, flush=True)


def decode_str(s):
    parts = decode_header(s or "")
    out = []
    for text, enc in parts:
        out.append(text.decode(enc or "utf-8", errors="replace") if isinstance(text, bytes) else text)
    return "".join(out)


def date_from_filename(fname):
    """Returns a normalized 'DD.MM.YY.xlsx' name, or None if no date found."""
    m = DATE_RE.search(fname)
    if not m:
        return None
    dd, mm, yy = m.group(1), m.group(2), m.group(3)
    yy2 = yy[-2:]
    return f"{dd}.{mm}.{yy2}.xlsx"


def sent_date(msg):
    """The email's send date in Harare, or None if the header is unusable."""
    try:
        return parsedate_to_datetime(msg["Date"]).astimezone(HARARE).date()
    except (TypeError, ValueError):
        return None


def corrected_name(out_name, sent):
    """FBC occasionally mistypes the month in a sheet's filename (the sheet
    sent on 15 May 2026 is named "15.04.26"), which would file it under a
    date it doesn't belong to. The sheets carry no date of their own, so the
    email's send date is the only cross-check. Correct the name only for that
    typo's shape -- more than 5 days off, same day of the month -- and leave
    any other mismatch alone (a genuinely old sheet resent later)."""
    if not sent:
        return out_name, None
    dd, mm, yy = out_name[:2], out_name[3:5], out_name[6:8]
    try:
        named = datetime.strptime(f"{dd}.{mm}.{yy}", "%d.%m.%y").date()
    except ValueError:
        return out_name, None
    if abs(named - sent) <= timedelta(days=5):
        return out_name, None
    if named.day == sent.day:
        return sent.strftime("%d.%m.%y") + ".xlsx", f"named {out_name} but sent {sent}"
    return out_name, f"named {out_name} but sent {sent} -- left as named"


def iter_xlsx_attachments(msg):
    """Yields (original_filename, payload_bytes) for every xlsx/xls attachment in msg."""
    for part in msg.walk():
        fname = part.get_filename()
        if not fname:
            continue
        fname = decode_str(fname)
        if not fname.lower().endswith((".xlsx", ".xls")):
            continue
        blob = part.get_payload(decode=True)
        if blob:
            yield fname, blob


def main():
    os.makedirs(SHEETS_DIR, exist_ok=True)
    existing = {f for f in os.listdir(SHEETS_DIR) if f.lower().endswith((".xlsx", ".xls"))}
    log(f"{len(existing)} sheet(s) already archived in {SHEETS_DIR}.")

    addr = os.environ["GMAIL_ADDRESS"]
    app_pw = os.environ["GMAIL_APP_PASSWORD"]

    saved = skipped_existing = skipped_no_date = skipped_no_attachment = 0

    conn = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        conn.login(addr, app_pw)
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, f'(FROM "{FBC_SENDER}")')
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        ids = data[0].split()
        log(f"Found {len(ids)} message(s) from {FBC_SENDER}.")

        for msg_id in ids:
            typ, msg_data = conn.fetch(msg_id, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])

            attachments = list(iter_xlsx_attachments(msg))
            if not attachments:
                skipped_no_attachment += 1
                continue

            # A message can carry more than one attachment; keep every one
            # that has a usable date rather than assuming just the first.
            for fname, blob in attachments:
                out_name = date_from_filename(fname)
                if not out_name:
                    log(f"  SKIP (no date found in filename): {fname!r}")
                    skipped_no_date += 1
                    continue
                out_name, note = corrected_name(out_name, sent_date(msg))
                if note:
                    log(f"  DATE CHECK {fname!r}: {note}")
                if out_name in existing:
                    skipped_existing += 1
                    continue
                with open(os.path.join(SHEETS_DIR, out_name), "wb") as f:
                    f.write(blob)
                existing.add(out_name)
                saved += 1
                log(f"  Saved {out_name}  (from {fname!r})")
    finally:
        conn.logout()

    log(
        f"\nDone. Saved {saved} new sheet(s). "
        f"Skipped: {skipped_existing} already archived, {skipped_no_date} with no parseable date, "
        f"{skipped_no_attachment} message(s) with no xlsx attachment."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
