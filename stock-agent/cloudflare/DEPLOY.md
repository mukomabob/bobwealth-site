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

The existing GitHub Actions workflow (`fbc-daily-publish-v2.yml`) already
archives each new day's sheet into `data/fbc-sheets/` and regenerates
`market-data.json`/`predictions.json`. To keep D1 in sync too, add a step
to that workflow (or a separate scheduled one) that runs, after the sheet
archive step:

```yaml
- name: Rebuild D1 stock-agent database
  run: |
    pip install pandas openpyxl
    python3 stock-agent/build_dataset.py data/fbc-sheets stock-agent/fbc_history.db
    python3 stock-agent/cloudflare/export_to_d1.py stock-agent/fbc_history.db stock-agent/cloudflare/d1_data.sql
    npx wrangler d1 execute fbc-history --remote --file=stock-agent/cloudflare/d1_data.sql
  env:
    CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
    CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
```

You'd create a Cloudflare API token (My Profile → API Tokens → "Edit
Cloudflare Workers" template, scoped to D1 edit) and add it plus your
account ID as GitHub Actions secrets (Settings → Secrets and variables →
Actions) for this to authenticate. This step isn't included in the repo's
actual workflow file yet — add it when you're ready to automate the daily
refresh; until then, re-run step 3 by hand whenever you want D1 updated.

## Costs and limits to know about

- **Workers AI**: 10,000 Neurons/day free (resets 00:00 UTC), then ~$0.011
  per 1,000 Neurons on a paid Workers plan. Each chat question uses two
  model calls (text→SQL, then SQL→answer) — a rough, non-guaranteed
  estimate is a few hundred Neurons per question, so the free daily
  allowance likely covers casual use; watch actual usage in the dashboard.
- **D1**: free tier covers far more than ~7,000 rows and the query volume
  a small site like this will see.
- **Worker requests**: 100,000 requests/day free.

If you outgrow the free tier, Cloudflare's dashboard shows exactly what's
being used before anything charges — nothing here auto-upgrades you.
