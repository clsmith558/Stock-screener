# Multi-Market Signal Screener

Lightweight Python/Flask dashboard for screening stocks across ASX, US large-cap indices, and custom business lists. Live metrics via yfinance; buyback and insider signals from exchange filings (ASX, HKEX, JPX, SEC EDGAR).

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

Never commit `.env` — it is listed in `.gitignore`.

---

## Optional: Obsidian integration

If you use an Obsidian vault with a `Company-Registry.yaml`:

```powershell
python scripts/export_registry.py --vault C:\path\to\your-vault
```

This writes `data/company_registry.json` (gitignored locally). See [`data/company_registry.example.json`](data/company_registry.example.json) for the expected shape.

---

## Project layout

```
├── app.py                          # Flask routes
├── asx_announcements.py            # ASX scraper (SQLite cache)
├── config/
│   ├── identity.py                 # SEC / HTTP user-agent config
│   └── opportunity_weights.yaml    # Quant score weights
├── data/                           # Market data layers
├── scripts/                        # Collectors & vault export
├── templates/index.html            # Dashboard UI
├── requirements.txt
└── .env.example
```

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