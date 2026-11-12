#!/usr/bin/env python3
"""
Daily monitor for the "Ask the data" chatbot.

Reads the anonymous question log (D1 table ask_log, written by the Worker),
checks the chatbot and its data pipeline, asks Caimex for at most 3 fixes, and
writes two files for the report page at superuser/chatbot.html:

    superuser/chatbot-report.json   today's report
    superuser/chatbot-history.json  one summary row per day (last 90 days)

It changes nothing else: no code, no data, no settings.

PRIVACY: bobwealth.org is public, so the report never contains the text that
visitors typed. Failed questions go to Caimex only, which summarises them as
topics ("3 questions about market cap"). The log keeps no IP address, and this
script deletes log rows older than 30 days.

Environment:
    CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID   D1 access (required)
    CAIMEX_API_KEY                                fixes and topics (optional)
    GITHUB_TOKEN, GITHUB_REPOSITORY               workflow status (optional)
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WRANGLER = os.path.join(ROOT, "stock-agent", "cloudflare", "wrangler.toml")
OUT_DIR = os.path.join(ROOT, "superuser")
REPORT_PATH = os.path.join(OUT_DIR, "chatbot-report.json")
HISTORY_PATH = os.path.join(OUT_DIR, "chatbot-history.json")

HARARE = timezone(timedelta(hours=2))
RETENTION_DAYS = 30
HISTORY_DAYS = 90
# Approximate cost of one Caimex answer, from the Caimex usage page (Nov 2026).
CAIMEX_COST_PER_ANSWER = 0.0013
FALLBACK_ALERT_SHARE = 0.2
SLOW_P95_MS = 20_000
BIG_MOVE_PCT = 30
WORKFLOWS = {"chatbot-deploy.yml": "Deploy chatbot", "fbc-daily-publish.yml": "Daily FBC prices"}


def toml_value(key):
    with open(WRANGLER) as f:
        m = re.search(rf'^\s*{key}\s*=\s*"([^"]*)"', f.read(), re.M)
    return m.group(1) if m else None


def http_json(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class D1:
    def __init__(self):
        self.url = (f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CLOUDFLARE_ACCOUNT_ID']}"
                    f"/d1/database/{toml_value('database_id')}/query")
        self.headers = {"Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"}

    def __call__(self, sql, params=()):
        res = http_json(self.url, {"sql": sql, "params": list(params)}, self.headers)
        if not res.get("success"):
            raise RuntimeError(f"D1 error: {res.get('errors')}")
        return res["result"][0].get("results", [])


def n_of(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def pct(n, d):
    return round(100 * n / d, 1) if d else None


def percentile(values, p):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def model_group(model):
    if not model:
        return "none"
    if model.startswith("@cf/"):
        return "workers_ai"
    if model.lower().startswith("claude"):
        return "claude"
    return "caimex"


def summarise(rows):
    """Numbers for one window of log rows (already filtered by time)."""
    questions = [r for r in rows if r["outcome"] != "rate_limited"]
    answered = [r for r in questions if r["outcome"] in ("ok", "empty")]
    outcomes, answer_models, sql_models, countries, devices, counters = {}, {}, {}, {}, {}, {}
    hours = [0] * 24
    for r in questions:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
        countries[r["country"] or "unknown"] = countries.get(r["country"] or "unknown", 0) + 1
        devices[r["device"] or "unknown"] = devices.get(r["device"] or "unknown", 0) + 1
        hours[datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).astimezone(HARARE).hour] += 1
        for c in json.loads(r["counters"] or "[]"):
            counters[c] = counters.get(c, 0) + 1
        if r["sql_by"]:
            g = model_group(r["sql_by"])
            sql_models[g] = sql_models.get(g, 0) + 1
    for r in answered:
        g = model_group(r["answer_by"])
        answer_models[g] = answer_models.get(g, 0) + 1
    caimex_errors = {}
    for r in questions:
        for part in (r.get("caimex_error") or "").split("; "):
            if part:
                caimex_errors[part] = caimex_errors.get(part, 0) + 1
    latency = [r["latency_ms"] for r in answered if r["latency_ms"] is not None]
    weak = [r for r in questions if r["outcome"] != "ok" or r["said_no_data"]]
    top = lambda d, n: [{"name": k, "count": v} for k, v in sorted(d.items(), key=lambda x: -x[1])[:n]]
    return {
        "questions": len(questions),
        "sessions": len({r["session"] for r in questions if r["session"]}),
        "rate_limited": sum(1 for r in rows if r["outcome"] == "rate_limited"),
        "answered": len(answered),
        "answered_ok_pct": pct(sum(1 for r in answered if not r["said_no_data"]), len(questions)),
        "weak_answers": len(weak),
        "outcomes": outcomes,
        "answer_models": answer_models,
        "sql_models": sql_models,
        "web_searches": sum(1 for r in questions if r["searched"]),
        "latency_p50_ms": percentile(latency, 50),
        "latency_p95_ms": percentile(latency, 95),
        "top_counters": top(counters, 8),
        "countries": top(countries, 6),
        "devices": devices,
        "hours_harare": hours,
        "est_caimex_spend_usd": round(answer_models.get("caimex", 0) * CAIMEX_COST_PER_ANSWER, 4),
        "caimex_errors": top(caimex_errors, 5),
    }


def workflow_status():
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return []
    out = []
    for file, label in WORKFLOWS.items():
        try:
            runs = http_json(f"https://api.github.com/repos/{repo}/actions/workflows/{file}/runs?per_page=1",
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            run = (runs.get("workflow_runs") or [None])[0]
            out.append({"name": label, "conclusion": run and (run["conclusion"] or run["status"]),
                        "at": run and run["updated_at"], "url": run and run["html_url"]})
        except (urllib.error.URLError, KeyError) as e:
            out.append({"name": label, "conclusion": "unknown", "at": None, "error": str(e)[:120]})
    return out


def data_checks(d1):
    latest = d1("SELECT MAX(date) AS d FROM prices")[0]["d"]
    try:
        moves = d1(f"""
            SELECT p.date, p.counter, ROUND(COALESCE(p.adj_change_pct, p.change_pct), 1) AS chg
            FROM prices p
            WHERE p.date >= (SELECT date(MAX(date), '-7 days') FROM prices) AND p.traded = 1
              AND ABS(COALESCE(p.adj_change_pct, p.change_pct)) >= {BIG_MOVE_PCT}
              AND NOT EXISTS (SELECT 1 FROM corporate_actions a WHERE a.counter = p.counter
                              AND ABS(julianday(a.ex_date) - julianday(p.date)) <= 7)
            ORDER BY ABS(chg) DESC LIMIT 5""")
    except RuntimeError:
        moves = []  # columns or table missing before the first rebuild
    return latest, moves


def ask_caimex(problems, weak_questions):
    key = os.environ.get("CAIMEX_API_KEY")
    model = toml_value("CAIMEX_ANSWER_MODEL")
    base = toml_value("CAIMEX_BASE_URL")
    if not (key and model and base) or not (problems or weak_questions):
        return None
    system = (
        "You review a stock-market chatbot for the Zimbabwe Stock Exchange and VFEX. It turns a question "
        "into one SQL query on tables prices, corporate_actions, predictions and model_track_record, then "
        "words an answer. You get today's problems and the questions it answered badly. Reply with JSON only: "
        '{"topics": [{"topic": "...", "count": n}], "fixes": ["...", "..."]}. '
        "topics: group the weak questions by subject. NEVER quote a question, and never include names of "
        "people, amounts a person holds, or contact details -- the topics are published on a public page. "
        "fixes: at most 3 concrete changes (to the SQL prompt rules, the answer rules, corporate_actions.csv, "
        "or settings), most useful first. Write in ASD-STE100 Simplified Technical English: sentences of "
        "20 words or fewer, active voice, simple words.")
    user = json.dumps({"problems": problems, "weak_questions": [q[:200] for q in weak_questions[:25]]})
    try:
        res = http_json(f"{base}/chat/completions",
                        {"model": model, "max_tokens": 900,
                         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                        {"Authorization": f"Bearer {key}"}, timeout=90)
        text = res["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", text, re.S)
        out = json.loads(m.group(0)) if m else {}
        topics = [{"topic": str(t.get("topic", ""))[:120], "count": int(t.get("count", 0))}
                  for t in out.get("topics", [])[:8] if isinstance(t, dict)]
        fixes = [str(f)[:400] for f in out.get("fixes", [])[:3]]
        return {"topics": topics, "fixes": fixes, "model": model}
    except Exception as e:  # the report still publishes without fixes
        print(f"Caimex review failed: {e}", file=sys.stderr)
        return {"topics": [], "fixes": [], "model": model, "error": "Caimex did not reply."}


def find_problems(day, week, workflows, latest_date, moves, now):
    p = []
    answered = day["answered"]
    fallback = day["answer_models"].get("workers_ai", 0)
    if answered and fallback / answered > FALLBACK_ALERT_SHARE:
        p.append(f"The free fallback model wrote {fallback} of {answered} answers. Caimex failed for these. "
                 "Check the Caimex balance and status.")
    errs = day.get("caimex_errors") or []
    if errs:
        total = sum(e["count"] for e in errs)
        p.append(f"Caimex failed {n_of(total, 'time')}. The most common reason: {errs[0]['name']}.")
    if week["questions"] and not week["sql_models"].get("claude"):
        p.append("Claude wrote no SQL in the last 7 days. ANTHROPIC_API_KEY is probably not set on the Worker. "
                 "Web search does not work without it.")
    failed = sum(day["outcomes"].get(k, 0) for k in ("error", "query_failed", "bad_sql"))
    if failed:
        p.append(f"{n_of(failed, 'question')} failed with an error or a bad query.")
    unanswerable = day["outcomes"].get("not_answerable", 0)
    if unanswerable:
        p.append(f"The chatbot refused {n_of(unanswerable, 'question')} as outside its data.")
    if day["latency_p95_ms"] and day["latency_p95_ms"] > SLOW_P95_MS:
        p.append(f"Answers are slow. 95% of answers took up to {day['latency_p95_ms'] / 1000:.1f} seconds.")
    if day["rate_limited"]:
        p.append(f"The rate limit stopped {n_of(day['rate_limited'], 'request')}.")
    for w in workflows:
        if w.get("conclusion") not in ("success", "in_progress", "queued", None):
            p.append(f"The last '{w['name']}' workflow run did not succeed ({w.get('conclusion')}).")
    if latest_date:
        age = (now.date() - datetime.strptime(latest_date, "%Y-%m-%d").date()).days
        if age > 4:
            p.append(f"The newest price data is from {latest_date}, {age} days ago. Check the daily FBC workflow.")
    for m in moves:
        p.append(f"{m['counter']} changed {m['chg']}% on {m['date']}, and no corporate action explains it. "
                 "Check for a split or an announcement.")
    return p


def main():
    now = datetime.now(timezone.utc)
    d1 = D1()
    tables = {r["name"] for r in d1("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = []
    if "ask_log" in tables:
        d1("DELETE FROM ask_log WHERE ts < ?", [(now - timedelta(days=RETENTION_DAYS)).isoformat()])
        rows = d1("SELECT * FROM ask_log WHERE ts >= ? ORDER BY ts",
                  [(now - timedelta(days=7)).isoformat()])
    since_day = (now - timedelta(days=1)).isoformat()
    day_rows = [r for r in rows if r["ts"] >= since_day]
    day, week = summarise(day_rows), summarise(rows)

    workflows = workflow_status()
    latest_date, moves = data_checks(d1)
    problems = find_problems(day, week, workflows, latest_date, moves, now)
    weak = [r["question"] for r in day_rows
            if r["outcome"] not in ("ok", "rate_limited") or r["said_no_data"]]
    review = ask_caimex(problems, weak)

    report = {
        "generated_at": now.isoformat(timespec="seconds"),
        "status": "attention" if problems else "ok",
        "log_available": "ask_log" in tables,
        "last_24h": day,
        "last_7d": week,
        "problems": problems,
        "weak_topics": (review or {}).get("topics", []),
        "fixes": (review or {}).get("fixes", []),
        "review_model": (review or {}).get("model"),
        "review_error": (review or {}).get("error"),
        "workflows": workflows,
        "latest_price_date": latest_date,
        "notes": "Anonymous data only. Questions are kept 30 days in D1 and are never published here.",
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=1)

    history = []
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH) as f:
            history = json.load(f)
    today = now.astimezone(HARARE).strftime("%Y-%m-%d")
    history = [h for h in history if h["date"] != today] + [{
        "date": today, "questions": day["questions"], "sessions": day["sessions"],
        "weak": day["weak_answers"], "fallback": day["answer_models"].get("workers_ai", 0),
        "searches": day["web_searches"], "p95_ms": day["latency_p95_ms"], "problems": len(problems)}]
    with open(HISTORY_PATH, "w") as f:
        json.dump(history[-HISTORY_DAYS:], f, indent=1)

    print(f"Report: {report['status']} -- {day['questions']} questions in 24h, {len(problems)} problem(s).")


if __name__ == "__main__":
    main()
