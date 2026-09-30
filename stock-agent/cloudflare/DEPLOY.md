# Deploying the stock-trend chat agent (Cloudflare Workers + D1 + Workers AI)

Everything in this folder is code and config — none of it is live until you
do the steps below, once, in your own free Cloudflare account. Total cost:
$0 to start (10,000 Workers AI "Neurons" free per day, D1 free tier is
generous for a table this size).

## 1. Create a Cloudflare account + install Wrangler

1. Sign up free at [dash.cloudflare.com/sign-up](https://dash.cloudflare.com/sign-up) if you don't have one.
2. On your computer, in this folder (`stock-agent/cloudflare/`), run:
   ```
   npx wrangler login
   ```
   This opens a browser tab to authorize Wrangler (Cloudflare's CLI) against your account. No install needed beyond Node.js — `npx` fetches Wrangler on demand.

## 2. Create the D1 database

```
npx wrangler d1 create fbc-history
```

This prints something like:

```
[[d1_databases]]
binding = "DB"
database_name = "fbc-history"
database_id = "a1b2c3d4-...."
```

Copy the `database_id` value it gives you into `wrangler.toml` in this
folder, replacing `REPLACE_WITH_YOUR_DATABASE_ID`.

## 3. Load the schema and data into D1

From this folder:

```
npx wrangler d1 execute fbc-history --remote --file=schema.sql
```

Then build the data dump from the sheet archive (needs the sibling
`build_dataset.py` from `stock-agent/`) and import it:

```
cd ..
python3 build_dataset.py ../data/fbc-sheets fbc_history.db
cd cloudflare
python3 export_to_d1.py ../fbc_history.db d1_data.sql
npx wrangler d1 execute fbc-history --remote --file=d1_data.sql
```

(Adjust the `../data/fbc-sheets` path to wherever you've checked out the
`bobwealth-site` repo locally.)

## 4. Deploy the Worker

```
npx wrangler deploy
```

Wrangler prints the Worker's live URL, e.g.:

```
https://fbc-stock-agent.<your-subdomain>.workers.dev
```

That's your chat backend — test it directly before wiring up the frontend:

```
curl -X POST https://fbc-stock-agent.<your-subdomain>.workers.dev \
  -H "Content-Type: application/json" \
  -d '{"question": "What were the top 5 gainers on the most recent trading day?"}'
```

You should get back JSON with a real `answer`, the `sql` it ran, and the
`rows` it grounded the answer in.

## 5. Wire it into the site

Open `chat-widget.html` in this folder, replace:

```js
const WORKER_URL = "REPLACE_WITH_YOUR_WORKER_URL";
```

with your actual Worker URL from step 4, then paste the whole file's
contents (the `<section>`, `<style>`, and `<script>` blocks) into
`markets.html` or `index.html` wherever you want the widget to appear — it
reuses the site's existing CSS variables, so no extra styling is needed.

## 6. Keeping D1 fresh as new sheets arrive

Both workflows are already in the repo and do nothing until two GitHub
secrets exist:

- `fbc-daily-publish.yml` rebuilds D1 from `data/fbc-sheets/` whenever it
  publishes a new day's sheet, so the chatbot answers from the same date as
  the website.
- `chatbot-deploy.yml` redeploys the Worker and reloads D1 whenever
  `stock-agent/` (or the publish script it shares baselines with) changes on
  `main`, and can be run by hand from the Actions tab.

To switch them on:

1. **Create a Cloudflare API token.** Cloudflare dashboard → My Profile →
   API Tokens → Create Token → "Edit Cloudflare Workers" template, then add
   the permission **Account → D1 → Edit**. Limit it to your own account.
2. **Find your account ID.** Workers & Pages overview → right-hand sidebar.
3. **Add both as repository secrets.** GitHub → the repo → Settings →
   Secrets and variables → Actions → New repository secret:
   `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.
4. **Run it once.** Actions → "Deploy chatbot (Worker + D1 data)" → Run
   workflow. After that the daily run keeps D1 current on its own.

The export recreates the `prices` table on every import (it inlines
`schema.sql`), so adding a column to the schema reaches D1 with the next
refresh.

The same import loads the repo's `predictions.json` into the `predictions`
and `model_track_record` tables, so the chatbot can answer questions about
the watchlist and its track record. The daily run exports after
`generate_predictions.py`, so these match the Watchlist page.

`usd_return_pct` / `return_since` are computed with
`scripts/publish_fbc_prices.py`'s own baselines, so the chatbot's
"best performer" is the same one the Investment Simulator shows.

## Costs and limits to know about

- **Workers AI**: 10,000 Neurons/day free (resets 00:00 UTC), then ~$0.011
  per 1,000 Neurons on a paid Workers plan. Each chat question uses two
  model calls (text→SQL, then SQL→answer), three when the first query fails
  and is retried — a rough, non-guaranteed
  estimate is a few hundred Neurons per question, so the free daily
  allowance likely covers casual use; watch actual usage in the dashboard.
- **D1**: free tier covers far more than ~7,000 rows and the query volume
  a small site like this will see.
- **Worker requests**: 100,000 requests/day free.

If you outgrow the free tier, Cloudflare's dashboard shows exactly what's
being used before anything charges — nothing here auto-upgrades you.

## Access controls

`wrangler.toml` locks the Worker down in two ways, both applied on the next
`npx wrangler deploy`:

- **`ALLOWED_ORIGINS`** — only pages on these sites can call the Worker from a
  browser; everything else gets `403`. To test from your own machine, add e.g.
  `http://localhost:8000` to the list temporarily.
- **`RATE_LIMITER`** — 10 questions per minute per visitor IP; extra requests
  get `429`. Change `limit` / `period` in `wrangler.toml` to tune it.
