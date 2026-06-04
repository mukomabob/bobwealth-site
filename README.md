Bob Wealth Markets Dashboard
A two-page market intelligence system for Zimbabwe Stock Exchange (ZSE) and VFEX data, built on static GitHub Pages with no backend required.

Architecture
bobwealth.org/superuser   →   private dashboard (you only)
bobwealth.org/markets     →   public dashboard (anyone)
                ↕
        market-data.json  (in GitHub repo root)
The two pages share no live connection. The superuser page writes a JSON file; the public page reads it. GitHub is the data store.

Files in the repo
FilePurposesuperuser.htmlPrivate dashboard with upload, import, and publish toolsmarkets.htmlPublic read-only dashboardmarket-data.jsonPublished snapshot — updated manually by superuserPrice Sheet 06.01.26.xlsxBaseline price sheet (Jan 6 2026) — the $100 investment starting point

How the superuser page works

Baseline auto-loads on page open — fetches Price Sheet 06.01.26.xlsx from the repo via jsDelivr CDN, with allorigins proxy as fallback. No action needed.
Import a price sheet — click ↑ Import Sheet and select the day's FBC Securities .xlsx file. All dashboard views populate instantly (Overview, Movers, Trading Activity, Sectors, Screener, $100 Tracker, Investment Chart).
Publish — click ⬆ Publish Snapshot to download market-data.json. This file contains all counter data plus the pre-calculated $100 investment values for every counter.
Upload to GitHub — drag market-data.json into the repo root via github.com. The public page updates automatically within minutes (jsDelivr CDN cache).


How the public markets page works

On load, markets.html fetches market-data.json from the repo using a three-URL cascade:

jsDelivr CDN — fast, CORS-friendly mirror of the GitHub repo
allorigins proxy — public CORS proxy, fallback if jsDelivr fails
GitHub raw URL — direct fallback


The header shows a green dot if data is less than 26 hours old, gold dot if older
All six dashboard sections are read-only — no file inputs or upload buttons exist on this page


The $100 Investment Tracker

Every counter's closing USD price on Jan 6 2026 is the baseline ($100 starting value)
ZSE prices are converted to USD using the IBR rate column in each price sheet
VFEX prices are native USD
When the superuser imports a new sheet, each counter's current USD price is divided by its baseline price and multiplied by 100 to give the current value of that original $100
This calculation is baked into market-data.json at publish time, so the public page does no raw price processing


Updating the baseline year
When you want to reset the $100 starting point to a new date:

Replace Price Sheet 06.01.26.xlsx in the repo with the new baseline file (keep the same filename, or update the BASELINE_FNAME and BASELINE_URLS constants in superuser.html)
Publish a fresh snapshot — the new baseline values will flow through to the public page on next import


Data source
All price data comes from FBC Securities (Pvt) Ltd daily price sheets. The dashboard does not connect to any live data feed — it is entirely dependent on manual imports by the superuser.You said: Thanks, please fix thisThanks, please fix this Trying: 
