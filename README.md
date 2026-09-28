# Multi-Market Signal Screener

Lightweight Python/Flask dashboard for screening stocks across ASX, US large-cap indices, and custom business lists. Live metrics via yfinance; buyback and insider signals from exchange filings (ASX, HKEX, JPX, SEC EDGAR).

**Intended use:** personal research tool running on your machine. It is not hardened for multi-user or public internet hosting.

---

## Quick start

```powershell
# 1. From repo root
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Configure identity (required for SEC EDGAR / Wikipedia fetches)
copy .env.example .env
# Edit .env — set SEC_EDGAR_IDENTITY to your email

# 3. (Optional) Add business list CSVs — one ticker per row in a local folder:
#    mkdir "Business lists"
#    # e.g. Business lists\my_watchlist.csv
#    Builtin Lists tab also includes Capital-cycle pariah (from config/pariah_industries.yaml)

# 4. Run
python app.py

# 5. Open http://127.0.0.1:5000
```

---

## Configuration

Copy [`.env.example`](.env.example) to `.env` and set:

| Variable | Purpose |
|----------|---------|
| `SEC_EDGAR_IDENTITY` | Email for SEC EDGAR API (required for US filing scans) |
| `SCREENER_CONTACT_EMAIL` | Contact email in Wikipedia user-agent strings |
| `OBSIDIAN_VAULT_PATH` | Optional — local Obsidian vault for wiki links |
| `SCREENER_HOST` | Bind address (default `127.0.0.1`) |
| `SCREENER_PORT` | Port (default `5000`) |
| `FLASK_DEBUG` / `SCREENER_DEBUG` | Enable Flask debug only on loopback (`1` / `true`) |
| `SCREENER_API_TOKEN` | Optional secret required on POST refresh/scrape routes |
| `SCREENER_ALLOW_REMOTE` | Allow non-localhost mutating requests (default off) |

Never commit `.env` — it is listed in `.gitignore`.

### Local-only security

Defaults assume a personal machine:

- Listens on **127.0.0.1** (not all interfaces)
- **Debug mode is off** unless you set `FLASK_DEBUG=1`
- **POST** routes (refresh, scrapers, YF rebuild, vault memos, watchlist edits) refuse non-localhost clients unless `SCREENER_ALLOW_REMOTE=1`
- Every mutating request must send `X-Screener-Token`. The dashboard does this itself. With no `SCREENER_API_TOKEN`, the value is `local`. A query-string `?token=` is ignored
- Optional `SCREENER_API_TOKEN` replaces that value and is injected into the dashboard for those requests

Do not set `SCREENER_HOST=0.0.0.0` with `FLASK_DEBUG=1`. The Werkzeug debugger is not safe on a network.

---

## Commodities tab (producers vs strips)

Filter a strip, then compare **all mapped unit costs** on one chart. Click a producer for the stock + cost + margin chart, a volume chart when filing extracts exist, and adjusted R² (log returns) of price vs stock / volume / cost.

Mapped producers now cover **coal, gold, copper, aluminum, iron ore, silver, steel scrap, uranium, crude, Henry Hub gas, and lithium** (plus extra names that also produce one of the original five, e.g. AEM / RMS.AX / GFI in gold). Prices use **FRED/EIA/Yahoo** when collected (WTI, Brent, uranium, Henry Hub, iron TSI); otherwise the local Barchart CSV. The CSV `iron ore` row was Tokyo Topix-Mini (`TS*0`, ~4000) — the tab now uses SGX TSI `TIO=F` (~USD/t 62% Fe).

```powershell
# Data path default in config/commodity_producers.yaml (override with COMMODITY_DATA_CSV)
# Open dashboard → Commodities tab

# Offline research: margin / ratio vs forward stock returns
python scripts/commodity_margin_regression.py

# Multi-quarter unit costs (ASX IR PDFs + US 10-Q/8-K/6-K) for stepped cost line on chart
python scripts/collect_unit_cost_history.py
python scripts/collect_unit_cost_history.py --tickers NHC.AX,WHC.AX,NEM --status
python scripts/collect_unit_cost_history.py --tickers FMG.AX,VALE,CIA.AX,MIN.AX,AEM,GFI,PLS.AX

# Production volumes for the volume chart (seed includes commodities producers)
python scripts/collect_operating_extract.py --tickers HCC,NHC.AX,WHC.AX,YAL.AX
```

- Config: [`config/commodity_producers.yaml`](config/commodity_producers.yaml)
- Loaders: [`data/commodity_prices.py`](data/commodity_prices.py), [`data/commodity_producers.py`](data/commodity_producers.py)
- Cost history: [`data/unit_cost_history.py`](data/unit_cost_history.py) → SQLite `mining_unit_cost_history`. Coal FOB; gold **AISC** (AEM / GFI / NEM); copper unit net cash; iron **C1** (Fortescue hematite USD/wmt, Vale FOB ex-third-party, Champion loaded-vessel USD/dmt, MinRes Onslow FOB); lithium spodumene **FOB SC6** (PLS). Overlays are indicative (wmt vs dmt, FOB vs CFR, spodumene vs carbonate).
- APIs: `GET /api/commodities`, `/api/commodities/<id>/panel`, `/api/commodities/producers`, `/api/commodities/producers/<ticker>`, `/api/commodities/cost-history`
- Research output: `data/commodity_research/` (gitignored if large)

### Official supporting series (EIA / FRED / Pink Sheet / AEMO)

Phase 1 pulls **daily/weekly** catalog rows from EIA v2 `seriesid` and FRED v1 `series/observations` into SQLite. Derived `crack_321` is computed after WTI + RBOB + ULSD land.

Phase 3 adds **monthly** EIA/FRED (LNG exports, dry-gas production, Baker Hughes rigs, uranium), EIA STEO US HDD/CDD, World Bank Pink Sheet (Newcastle coal, Japan LNG), and AEMO NEM daily regional-price means (NSW/QLD/VIC). Barchart daily CSV, Ember EU ETS, worldsteel, alumina (not in the Pink Sheet), and the TBD US power hub are still catalog-only.

```powershell
python scripts/collect_supporting_series.py
python scripts/collect_supporting_series.py --status
python scripts/collect_supporting_series.py --phase 3
python scripts/collect_supporting_series.py --series henry_hub,newcastle_coal,aemo_nsw_price
python scripts/collect_supporting_series.py --full
```

- Config: [`config/supporting_series.yaml`](config/supporting_series.yaml)
- Store: `data/supporting_series.db` (gitignored)
- Keys: `EIA_API_KEY`, `FRED_API_KEY` in `.env` (Pink Sheet / AEMO need none)
- Scheduled as job `supporting_series` (`max_age_hours: 24`)
- First AEMO pull stores ~36 months of daily means; `--full` goes back to 2018
- EIA monthly gas volumes are stored as **Bcf/d** (converted from MMCF/month)
- Joined onto **energy_pilot** and **watchlist** tables (strip last / 3m / crack) and the stock detail modal (stock vs strip + 3-2-1), same ratio/relative math as the Commodities tab

### Operating extracts (filings)

Phase 2 pulls **production, realized price, hedge %, capex** from 10-Q / 10-K / 8-K Item 2.02 and ASX quarterlies for a seed list (OXY, EQT, HCC, NHC.AX, WDS.AX, CEG). Heuristic — hover the detail cards for the source snippet.

```powershell
python scripts/collect_operating_extract.py
python scripts/collect_operating_extract.py --status
python scripts/collect_operating_extract.py --tickers OXY,NHC.AX
```

- Config: [`config/operating_extract.yaml`](config/operating_extract.yaml)
- Store: `operating_extract` table in `asx_announcements_cache.db`
- Scheduled as job `operating_extract` (`max_age_hours: 168`)

---

## Background data updates (without the dashboard)

You do **not** need `python app.py` running for collectors. Jobs write SQLite/CSV caches the UI reads later.

### Can updates run while the PC is asleep?

**No.** On typical Windows sleep (S3), the CPU is frozen — Task Scheduler and Python do not run.  
What *does* work: schedule jobs while the machine is **awake**, and enable **catch-up after a missed start** so a Friday-night miss runs Saturday/Sunday after you open the lid (or leave the PC on).

### Orchestrator (recommended)

```powershell
# Status: which jobs are stale
python scripts/run_scheduled_updates.py --status

# Run only stale jobs (respects config/update_schedule.yaml max_age_hours)
python scripts/run_scheduled_updates.py

# Force all enabled jobs
python scripts/run_scheduled_updates.py --force

# One job
python scripts/run_scheduled_updates.py --only asx_announcements
```

- Config: [`config/update_schedule.yaml`](config/update_schedule.yaml) (enable jobs, ages, timeouts)
- Logs/state: `logs/scheduled_updates.log`, `logs/scheduled_updates_state.json` (gitignored)

### Windows Task Scheduler

```powershell
# Register: every 12h + daily 07:30 local; catch up if a start was missed while asleep
powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_task.ps1

# Every 6 hours only
powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_task.ps1 -IntervalHours 6 -DailyTime ""

# Remove
powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_task.ps1 -Unregister

# Run now
Start-ScheduledTask -TaskName "SignalScreener-ScheduledUpdates"
```

Task settings used: **StartWhenAvailable** (run missed ASAP after wake). Optional `-WakeToRun` asks Windows to wake the PC; many laptops ignore this on battery.

### Inactive symbol hygiene

Confirmed delisted / no-quote tickers are tracked in SQLite and skipped by YF jobs. Periodic hygiene:

```powershell
python scripts/hygiene_inactive_symbols.py --status
python scripts/hygiene_inactive_symbols.py --dry-run
python scripts/hygiene_inactive_symbols.py
```

- Syncs zero-price cache rows → inactive  
- Re-probes *tentative* (`cached_zero_price`) with live yfinance (promote or revive)  
- Purges *confirmed* inactive from `business_list_entries` and rewrites Business list CSVs  
- Prunes orphan ASX inactive rows no longer on the official universe  
- Scheduled weekly via `inactive_hygiene` in `config/update_schedule.yaml`  
- **Watchlist is never auto-purged**

### Korea / Japan / China / Hong Kong / Taiwan equity lists (Business lists)

```powershell
# Japan TSE equities — JPX Excel; default = Prime+Standard+Growth stocks (no ETF/REIT/funds)
python scripts/refresh_japan_stocks_list.py
# python scripts/refresh_japan_stocks_list.py --prime-only   # Prime board only
# python scripts/refresh_japan_stocks_list.py --all-issues # include ETFs etc.

# Korea KOSPI + KOSDAQ (Yahoo .KS / .KQ) — FinanceDataReader / GitHub KRX cache
python scripts/refresh_korea_stocks_list.py

# China CSI 300 (.SS/.SZ) + Hong Kong Hang Seng (.HK) + Taiwan 50 (.TW),
# unioned with buyback-registry names listed on those markets
python scripts/refresh_china_hk_index_lists.py
```

Writes `Business lists/japan_stocks.csv`, `korea_stocks.csv`, `China CSI 300.csv`, `Hong Kong Hang Seng.csv`, and `Taiwan 50.csv` (one ticker per line; comma-blob also supported). Rescan Lists in the UI, then rebuild YF for the new list. The old **Asia Emerging** OTC/ADR dump is not scanned. CSI 300 extras come from A-share daily repurchase prints (East Money tape of SSE/SZSE filings; CNINFO 回购股份 search as fallback). TWSE treasury-stock (庫藏股) filings live on MOPS but there is no equivalent open daily tape.

### Morningstar wide moat (public proxy)

Morningstar’s complete Wide Economic Moat screen is paywalled (about **169 US names** after the March 2026 review). The Lists dropdown **Morningstar wide moat** is a free union of:

1. **Best Companies to Own** public CSVs — Wide moat **and** Standard/Exemplary capital allocation **and** Low/Medium fair-value uncertainty ([2026 article](https://www.morningstar.com/stocks/best-companies-own-2026-edition))
2. A public compilation of **98 selected** US-listed names (August 2026; not the live constituent file)
3. **VanEck MOAT** holdings when scrapeable (Wide Moat Focus Index — cheapest ~50 wide-moat names)

```powershell
python scripts/refresh_morningstar_wide_moat.py
# python scripts/refresh_morningstar_wide_moat.py --best-companies-only
```

Writes `Business lists/Morningstar wide moat.csv`. Scheduled ~monthly as job `morningstar_wide_moat` in `config/update_schedule.yaml`. Config: [`config/morningstar_wide_moat.yaml`](config/morningstar_wide_moat.yaml).

### Currency / dual-list audit

```powershell
python scripts/audit_currency_dual_list.py
```

Writes [`docs/CURRENCY_DUAL_LIST_AUDIT.md`](docs/CURRENCY_DUAL_LIST_AUDIT.md): quote vs financial currency FX coverage, extreme net-cash % of mcap, dual listings (NEM / NEM.AX, registry multi-tickers). Metrics clamp `|net_cash % mcap| > 500%` to 0 to avoid filter pollution from bad mcaps.

### US index universes (S&P 500 / Russell 1000 / Nasdaq-100)

Builtin lists are re-fetched from Wikipedia and re-registered in SQLite:

```powershell
python scripts/refresh_us_index_lists.py
```

Scheduled **every ~6 months** (`max_age_hours: 4320`) as job `us_index_lists` in `config/update_schedule.yaml`. After a membership change, run a YF refresh for those lists if you need fresh prices on new constituents.

### yfinance background refresh

Default schedule includes:

| Job id | What | Default |
|--------|------|---------|
| `yf_watchlist` | Personal watchlist quotes/metrics | On, daily if stale |
| `yf_lists` | S&P 500 + Business list CSVs + buybacks registry | On, every ~3 days if stale |
| `yf_asx_full` | Entire ASX listed universe | **Off** (hours; enable in `config/update_schedule.yaml`) |

CLI (without the orchestrator):

```powershell
python scripts/rebuild_yf_cache.py --scope watchlist
python scripts/rebuild_yf_cache.py --scope all-light --max-cache-days 7
python scripts/rebuild_yf_cache.py --scope asx --max-cache-days 14   # slow
python scripts/rebuild_yf_cache.py --scope all-light --force         # every symbol
```

Only symbols whose cache is older than `--max-cache-days` are fetched (unless `--force`).

Heavy full-ASX rebuild: set `yf_asx_full.enabled: true` in [`config/update_schedule.yaml`](config/update_schedule.yaml), or run the `asx` scope by hand on AC power.

---

## Mining pilot (reorg + reserves)

Shared universe: [`config/us_mining_universe.yaml`](config/us_mining_universe.yaml)

### Phase-1 — bankruptcy-cleaned survivor (Item 1.03)

```powershell
python scripts/collect_item_103.py
python scripts/collect_item_103.py --tickers BTU,ARCH,HCC,AMR --force
python scripts/collect_item_103.py --status
```

- Logic: [`data/bankruptcy_reorg.py`](data/bankruptcy_reorg.py) → `mining_item103_events`, `mining_reorg_summary`
- Dashboard: **Require reorg survivor (Item 1.03)**; **R** chip
- APIs: `GET /api/pariah/reorg`, `POST /api/pariah/reorg/scan`

### Phase-2 — reserve life (10-K S-K 1300 narrative)

```powershell
python scripts/collect_mining_reserves.py
python scripts/collect_mining_reserves.py --tickers BTU,HCC,FCX,NEM,AMR --force
python scripts/collect_mining_reserves.py --status
```

- Logic: [`data/mining_reserves.py`](data/mining_reserves.py) → `mining_reserves_summary`
- Parses proven+probable reserves + annual production → `reserve_life_years` (heuristic; verify in 10-K)
- Flags Exhibit 96 technical report summaries; **does not** estimate AISC / cost-curve rank
- Dashboard: optional **Min reserve life (years)**; **L** life chip; popup reserves line
- APIs: `GET /api/pariah/reserves`, `POST /api/pariah/reserves/scan`

### Phase-3 — reported unit cost rank (pilot peers only)

```powershell
python scripts/collect_mining_costs.py
python scripts/collect_mining_costs.py --tickers BTU,HCC,AMR,FCX,NEM --force
python scripts/collect_mining_costs.py --status
python scripts/collect_mining_costs.py --rank-only
```

- Logic: [`data/mining_costs.py`](data/mining_costs.py) → `mining_costs_summary`
- Metrics (heuristic): coal cash cost/ton, gold AISC/oz, copper unit net cash/lb
- Ranks **only within pilot peers sharing the same metric** (not industry cost curve)
- Dashboard: optional **Require low-cost quartile**; **C** chip; popup cost line
- APIs: `GET /api/pariah/costs`, `POST /api/pariah/costs/scan`

### ASX mining pilot (coal + NEM dual-list)

```powershell
python scripts/collect_asx_mining.py
python scripts/collect_asx_mining.py --force
python scripts/collect_asx_mining.py --tickers YAL,WHC,NHC,NEM
python scripts/collect_asx_mining.py --status
```

- Universe: [`config/asx_mining_universe.yaml`](config/asx_mining_universe.yaml) — **YAL, WHC, NHC** (coal) + **NEM** (ASX CDIs; cross-fill from US SEC)
- Logic: [`data/asx_mining.py`](data/asx_mining.py) → `asx_mining_summary` / `asx_mining_announcements`
- Phase 1: VA / DOCA / scheme / receiver headlines (+ US Item 1.03 for NEM)
- Phase 2–3: quarterly + seed annual-report PDFs (JORC / FOB cash cost / production)
- APIs: `GET /api/pariah/asx-mining`, `POST /api/pariah/asx-mining/scan`
- Note: free ASX company API returns a short recent window; seed IR annual PDFs supply reserve depth

---

## Optional: Obsidian integration

If you use an Obsidian vault with a `Company-Registry.yaml`:

```powershell
python scripts/export_registry.py --vault C:\path\to\your-vault
```

This writes `data/company_registry.json` (gitignored locally). See [`data/company_registry.example.json`](data/company_registry.example.json) for the expected shape.

Wiki book icons appear when a registry entry has `opportunity_memo` **or** `wiki/investing/opportunities/{TICKER}.md` already exists on disk (exchange suffix must still match — `RMS.PA` ≠ `RMS.AX`).

### Watchlist → vault memo

From any stock detail modal:

- **Open memo** — `obsidian://` link when the opportunity note exists
- **Create memo** — writes `wiki/investing/opportunities/{TICKER}.md` from the vault `_template.md` and fills Score / multiples / driver / filing extract
- **Push snapshot** — updates frontmatter + Path A values + a decision-log row; does **not** rewrite the thesis

`python scripts/enrich_ticker.py HCC --export-vault` now also patches that same memo (Score, yfinance multiples, operating extract, 5y financial table when empty).

```powershell
# status / create / push (also available as POST /api/vault/memo/<ticker>)
python -c "from data.vault_memo import memo_status; print(memo_status('HCC'))"
```

---

## Project layout

```
├── app.py                          # Flask routes (local-only guards)
├── asx_announcements.py            # ASX scraper (SQLite cache)
├── config/
│   ├── identity.py                 # SEC / HTTP user-agent config
│   └── opportunity_weights.yaml    # Quant score weights
├── data/                           # Market data layers
│   └── yf_helpers.py               # Shared yfinance metric helpers
├── docs/
│   └── DASHBOARD_FORMULAS.md       # Score + metric formulas (human-readable)
├── scripts/                        # Collectors & vault export
├── templates/index.html            # Dashboard UI
├── requirements.txt
└── .env.example
```

**Score** on the table is **quantitative only** (valuation, price posture, insider, buyback@low). Obsidian vault tier/qual no longer scales it. See [`docs/DASHBOARD_FORMULAS.md`](docs/DASHBOARD_FORMULAS.md).

Local-only (not committed): `.venv/`, `*.db`, `Business lists/`, scraped download folders, `data/company_registry.json`.


---

## Data sources

- **yfinance** — prices, fundamentals, earnings history
- **ASX** — unofficial announcements API + Appendix 3Y PDF parsing
- **SEC EDGAR** — Form 4 insider buys, 8-K/10-Q buyback signals
- **HKEX / JPX** — share repurchase and insider dealing feeds
- **Wikipedia** — S&P 500 / Russell 1000 / Nasdaq-100 constituent lists

---

## Caveats

- Unofficial scrapers can break if exchange endpoints change.
- First loads are slow (yfinance + filing scans); results are cached in SQLite.
- For production use, consider licensed market data feeds.

*Multi-market signal screener (2026).*
