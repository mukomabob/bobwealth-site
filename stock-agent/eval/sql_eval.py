#!/usr/bin/env python3
"""
Compare Workers AI models on the chatbot's text-to-SQL step.

Each question below has a ground-truth query, written by hand and run against
the same database, so the expected answer is recomputed every run and stays
right as new price sheets arrive. A model passes a question when the rows its
own SQL returns contain every expected value. Only the SQL step is scored: that
is where the chatbot's wrong answers came from, and the answer-wording step is
the same whichever model writes the query.

The system prompt is read from stock-agent/cloudflare/sql_prompt.js, the module
the Worker itself uses, so the comparison runs exactly what visitors get.

Usage (needs a database built by build_dataset.py + export_to_d1.py):
    CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=... \\
    python3 sql_eval.py eval.db @cf/meta/llama-3.3-70b-instruct-fp8-fast @cf/openai/gpt-oss-120b
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT_MODULE = os.path.join(HERE, "..", "cloudflare", "sql_prompt.js")


def load_system_prompt():
    out = subprocess.run(
        ["node", "--input-type=module", "-e",
         f"import {{ SQL_SYSTEM_PROMPT }} from {json.dumps(PROMPT_MODULE)}; process.stdout.write(SQL_SYSTEM_PROMPT);"],
        check=True, capture_output=True, text=True,
    )
    return out.stdout


# ─── questions + ground truth ────────────────────────────────────────────────
# Each truth query returns the values a correct answer must contain: strings are
# matched case-insensitively inside any cell, numbers within a small tolerance.
LATEST = "(SELECT MAX(date) FROM prices)"
QUESTIONS = [
    ("What was Pfuma's price 2 months ago?",
     f"""SELECT date, usd_price_ibr FROM prices WHERE counter LIKE '%Pfuma%'
         AND date <= (SELECT date(MAX(date), '-2 months') FROM prices) ORDER BY date DESC LIMIT 1"""),
    ("How did Cafca and Delta trade on 29 September 2026?",
     """SELECT change_pct FROM prices WHERE date = '2026-09-29' AND counter IN ('Cafca Limited', 'Delta')"""),
    ("How much CBZ stock changed hands on 9 September 2026, in ZiG?",
     """SELECT value_traded_zig FROM prices WHERE date = '2026-09-09' AND counter = 'CBZ'"""),
    ("Which three VFEX counters fell the most on the latest trading day?",
     f"""SELECT counter FROM prices WHERE market = 'VFEX' AND date = {LATEST}
         ORDER BY change_pct ASC LIMIT 3"""),
    ("Top 5 gainers this year",
     f"""SELECT counter FROM prices WHERE date = {LATEST} AND usd_return_pct IS NOT NULL
         ORDER BY usd_return_pct DESC LIMIT 5"""),
    ("Which REIT is up the most this year?",
     f"""SELECT counter FROM prices WHERE date = {LATEST} AND sector = 'Reit'
         ORDER BY usd_return_pct DESC LIMIT 1"""),
    ("What's on today's watchlist?",
     """SELECT counter FROM predictions WHERE watchlist_rank IS NOT NULL"""),
    ("How accurate is the prediction model?",
     """SELECT model_mae, no_change_mae FROM model_track_record"""),
    ("What is Delta's latest closing price in ZiG?",
     f"""SELECT close FROM prices WHERE counter = 'Delta' AND date = {LATEST}"""),
    ("How has Econet InfraCo done in US dollars since it listed?",
     f"""SELECT usd_return_pct FROM prices WHERE counter = 'Econet InfraCo' AND date = {LATEST}"""),
    ("Which sector has the best average US-dollar return this year?",
     f"""SELECT sector FROM prices WHERE date = {LATEST} AND usd_return_pct IS NOT NULL
         GROUP BY sector ORDER BY AVG(usd_return_pct) DESC LIMIT 1"""),
    ("What was Innscor's US dollar price a month ago, and what is it now?",
     f"""SELECT usd_price_ibr FROM prices WHERE counter LIKE 'Innscor%' AND date IN (
           {LATEST},
           (SELECT MAX(date) FROM prices WHERE date <= (SELECT date(MAX(date), '-1 month') FROM prices)))"""),
    ("How much has Edgars moved in September?",
     """SELECT ROUND((e.p / s.p - 1) * 100, 2) FROM
          (SELECT usd_price_ibr AS p FROM prices WHERE counter = 'Edgars' AND usd_price_ibr > 0
             AND date < '2026-09-01' ORDER BY date DESC LIMIT 1) s,
          (SELECT usd_price_ibr AS p FROM prices WHERE counter = 'Edgars' AND usd_price_ibr > 0
             AND date < '2026-10-01' ORDER BY date DESC LIMIT 1) e"""),
    ("How many counters rose on 30 September 2026?",
     """SELECT COUNT(*) FROM prices WHERE date = '2026-09-30' AND change_pct > 0"""),
]


def expected_values(db, truth_sql):
    return [v for row in db.execute(truth_sql).fetchall() for v in row if v is not None]


def matches(expected, cells):
    if isinstance(expected, str):
        e = expected.lower()
        return any(isinstance(c, str) and e in c.lower() for c in cells)
    for c in cells:
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            if abs(c - expected) <= max(0.01, abs(expected) * 0.005):
                return True
    return False


# ─── model calls ─────────────────────────────────────────────────────────────
CAIMEX_BASE_URL = "https://caimex.econetai.co.zw:2052/v1"


def run_model(model, system, question):
    """`caimex:<model>` goes to Econet's Caimex gateway (OpenAI-compatible);
    anything else is a Workers AI model ID."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    if model.startswith("caimex:"):
        url, key = f"{CAIMEX_BASE_URL}/chat/completions", os.environ.get("CAIMEX_API_KEY", "")
        payload = {"model": model[len("caimex:"):], "messages": messages, "max_tokens": 2048}
    else:
        url = (f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CLOUDFLARE_ACCOUNT_ID']}"
               f"/ai/run/{model}")
        key, payload = os.environ.get("CLOUDFLARE_API_TOKEN", ""), {"messages": messages, "max_tokens": 2048}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "bobwealth-sql-eval",
    })
    with urllib.request.urlopen(req, timeout=120) as res:
        body = json.load(res)
    return body["result"] if "result" in body else body


def ai_text(result):
    """Same shapes the Worker's aiText() handles."""
    if isinstance(result.get("response"), str):
        return result["response"]
    if result.get("choices"):
        c = result["choices"][0]
        return (c.get("message") or {}).get("content") or c.get("text") or ""
    for item in result.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if isinstance(c.get("text"), str):
                    return c["text"]
    return ""


def extract_sql(raw):
    """Mirror of extractSql() in sql_prompt.js."""
    sql = raw.strip()
    fenced = re.search(r"```(?:sql)?\s*([\s\S]*?)```", sql, re.IGNORECASE)
    if fenced:
        sql = fenced.group(1).strip()
    return re.sub(r";+\s*$", "", sql).strip()


def main():
    db_path, models = sys.argv[1], sys.argv[2:]
    db = sqlite3.connect(db_path)
    system = load_system_prompt()
    truths = [(q, expected_values(db, t)) for q, t in QUESTIONS]

    results = {m: [] for m in models}
    skipped = {}
    for model in models:
        print(f"\n=== {model}", flush=True)
        for (question, expected) in truths:
            if model in skipped:
                results[model].append(None)
                continue
            t0 = time.time()
            try:
                result = run_model(model, system, question)
                sql = extract_sql(ai_text(result))
                cells = [v for row in db.execute(sql).fetchall() for v in row] if sql.upper() != "NONE" else []
                missing = [e for e in expected if not matches(e, cells)]
                ok, note = not missing, (f"missing {missing}" if missing else "")
            except urllib.error.HTTPError as e:
                if e.code in (401, 402, 403, 429):
                    # Credentials refused, no balance, or a daily limit reached: the rest
                    # can't be scored, so stop this model instead of counting failures.
                    skipped[model] = f"HTTP {e.code} {e.read()[:160]!r}"
                    print(f"SKIPPED from here: provider refused the request ({skipped[model]}). "
                          "For Workers AI the API token needs the 'Workers AI - Read' permission.", flush=True)
                    results[model].append(None)
                    continue
                sql, ok, note = "", False, f"HTTP {e.code}: {e.read()[:200]!r}"
            except Exception as e:
                ok, note = False, f"{type(e).__name__}: {e}"
                sql = locals().get("sql", "")
            results[model].append(ok)
            print(f"{'PASS' if ok else 'FAIL'}  {time.time() - t0:4.1f}s  {question}", flush=True)
            if not ok:
                print(f"        expected {expected}\n        {note}\n        SQL: {' '.join(sql.split())[:300]}", flush=True)

    lines = ["| Model | Passed |", "|---|---|"] + [
        f"| `{m}` | {sum(bool(x) for x in r)}/{sum(x is not None for x in r)} scored"
        + (f" (stopped: {skipped[m]})" if m in skipped else "") + " |"
        for m, r in results.items()
    ]
    lines += ["", "| Question | " + " | ".join(m.split("/")[-1] for m in models) + " |",
              "|---|" + "---|" * len(models)]
    for i, (q, _) in enumerate(truths):
        lines.append(f"| {q} | " + " | ".join(
            "—" if results[m][i] is None else "✅" if results[m][i] else "❌" for m in models) + " |")
    summary = "\n".join(lines)
    print("\n" + summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("## Text-to-SQL model comparison\n\n" + summary + "\n")


if __name__ == "__main__":
    main()
