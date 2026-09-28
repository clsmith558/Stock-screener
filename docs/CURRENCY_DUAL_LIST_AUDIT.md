# Currency / dual-list consistency audit

_Generated 2026-07-29 23:39 by `scripts/audit_currency_dual_list.py`._

## Executive summary

| Check | Count |
|-------|------:|
| Business YF cache rows | 12781 |
| Quote ≠ financial currency | 2355 |
| Mismatch **without** FX conversion | 0 |
| Suffix vs quote currency mismatches | 1 |
| \|net_cash % mcap\| > 500% (suspect units) | 656 |
| Dual bases in cache (same root, >1 symbol) | 465 |
| Registry multi-ticker companies | 9 |

## How the screener is designed

1. **Quote currency** (`info.currency` / exchange suffix): trading price and `marketCap`.
2. **Financial currency** (`info.financialCurrency`): cash, debt, FCF, equity totals when Yahoo reports them separately.
3. **`financial_to_quote_fx`** multiplies BS/CF absolute amounts into quote units before net cash, EV, P/FCF.
4. **Display** uses quote currency labels so net cash % of mcap is meaningful **when FX succeeds**.
5. **Dual listings** are separate Yahoo symbols (e.g. `NEM` vs `NEM.AX`); mining pilot uses explicit `us_crossfill`.
6. **Vault registry** must list **both** exchange tickers for duals; bare-base matching was removed (RMS.AX ≠ RMS.PA).

## Findings

### 1. FX conversion coverage

- **Good:** All 2355 quote≠financial cases in cache have `currency_converted=true` (FX path ran).

Top FX pairs (quote → financial):

| Quote | Financial | N |
|-------|-----------|--:|
| USD | CNY | 543 |
| HKD | CNY | 433 |
| USD | CAD | 371 |
| USD | EUR | 149 |
| USD | AUD | 123 |
| USD | GBP | 96 |
| USD | JPY | 96 |
| USD | HKD | 95 |
| USD | THB | 74 |
| USD | IDR | 67 |
| USD | PHP | 57 |
| USD | MYR | 38 |
| HKD | USD | 35 |
| USD | SEK | 23 |
| USD | ZAR | 20 |

### 2. Extreme net cash % of market cap

Values with \|net cash / mcap\| > 500% almost always indicate **tiny/wrong `marketCap`** or residual unit issues — not a healthy balance sheet. UI should treat these as unreliable.

Count: **656**

| Symbol | net_cash% | net_cash_m | marketCap | quote | fin | FX? |
|--------|----------:|-----------:|----------:|-------|-----|-----|
| `MYMX` | -189798059.5 | -85 | 45 | USD | EUR | True |
| `KGJI` | -178023019.2 | -2001 | 1124 | USD | USD | False |
| `QKLS` | -35172327.6 | -53 | 152 | USD | USD | False |
| `HGYMF` | 28719635.1 | 37 | 130 | USD | JPY | True |
| `TRITF` | -21625736.9 | -18 | 84 | USD | USD | False |
| `AMCF` | -15413349.9 | -158 | 1025 | USD | USD | False |
| `BWOWF` | -13761785.7 | -5 | 35 | USD | USD | False |
| `CALIQ` | -12350752.1 | -60 | 484 | USD | USD | False |
| `CIWT` | -11101419.6 | -17 | 153 | USD | USD | False |
| `GCFB` | -9477681.0 | -136 | 1436 | USD | USD | False |
| `CHNGQ` | -9382063.4 | -40 | 429 | USD | USD | False |
| `BIQIF` | 7387978.1 | 59 | 803 | USD | USD | False |
| `ZTNO` | 6107153.3 | 11 | 180 | USD | USD | False |
| `GBGD` | -6003758.9 | -10 | 163 | USD | USD | False |
| `USDP` | -5500236.7 | -186 | 3377 | USD | USD | False |
| `SEDN` | -4821511.6 | -25 | 516 | USD | USD | False |
| `ZSTN` | 4763465.4 | 56 | 1171 | USD | USD | False |
| `EBODF` | 4343526.1 | 10 | 224 | USD | CNY | True |
| `ENGY` | -3907179.5 | -8 | 195 | USD | USD | False |
| `VALV` | 3855237.7 | 66 | 1719 | USD | USD | False |

### 3. Exchange suffix vs quote currency

Strict mismatches: **1** (local suffix e.g. `.AX`/`.T` should usually match AUD/JPY).

- `0770.HK`: expected HKD, quote USD

### 4. Known dual listings

| Name | Tickers | In cache | Notes |
|------|---------|----------|-------|
| Newmont | `NEM`, `NEM.AX` | present: `NEM`, `NEM.AX`; missing: — | US primary + ASX CDI; mining pilot us_crossfill NEM.AX <- NEM |
| Alibaba | `BABA`, `9988.HK` | present: `BABA`, `9988.HK`; missing: — | US ADR + HK dual; registry includes both |
| Tencent | `TCEHY`, `0700.HK` | present: `TCEHY`, `0700.HK`; missing: — | OTC ADR + HK primary |
| Hermes | `RMS.PA`, `HESAY` | present: —; missing: `RMS.PA`, `HESAY` | Euronext + US OTC; exchange-aware wiki match required |
| LVMH | `MC.PA`, `LVMUY` | present: —; missing: `MC.PA`, `LVMUY` | Euronext + US OTC |
| Constellation Software | `CSU.TO`, `CNSWF` | present: —; missing: `CSU.TO`, `CNSWF` | TSX + OTC |

Detail for present legs:

**Newmont**
- `NEM`: quote=USD fin=USD FX=False mcap=96433913856 net_cash%=3.54
- `NEM.AX`: quote=AUD fin= FX=False mcap=142443560960 net_cash%=2.28

**Alibaba**
- `BABA`: quote=USD fin=CNY FX=True mcap=268785238016 net_cash%=1.93
- `9988.HK`: quote=HKD fin=CNY FX=True mcap=2109247586304 net_cash%=1.93

**Tencent**
- `TCEHY`: quote=USD fin=CNY FX=True mcap=500310540288 net_cash%=1.6
- `0700.HK`: quote=HKD fin=CNY FX=True mcap=3912107819008 net_cash%=1.6

### 5. Vault registry multi-ticker rows

Multi-ticker companies: **9**

- `BABA`: `BABA`, `9988.HK`, `BABA-W`, `9988` — Alibaba
- `GOOG`: `GOOG`, `GOOGL` — Alphabet
- `TCEHY`: `TCEHY`, `0700.HK`, `700.HK`, `448` — Tencent
- `SQ`: `SQ`, `XYZ` — Block
- `BRK-B`: `BRK-B`, `BRK.A` — Berkshire Hathaway
- `RYAAY`: `RYAAY`, `RYA.IR` — Ryanair
- `LVMH`: `MC.PA`, `LVMUY` — LVMH
- `HESAY`: `RMS.PA`, `HESAY` — Hermès
- `CSU`: `CSU.TO`, `CNSWF` — Constellation Software

### 6. Mining / commodity dual config

```json
[
  {
    "asx": "NEM.AX",
    "us_crossfill": "NEM",
    "notes": "Dual-listed with US NEM \u2014 compare ASX parse vs SEC phase-2/3 cache"
  },
  {
    "commodity_producers_nem": [
      "NEM",
      "NEM.AX"
    ]
  }
]
```

## Recommendations

1. **Keep** quote/financial FX path as source of truth; unconverted count should stay ~0.
2. **Clamp or null** `net_cash_pct_mcap` when abs > 500% (or mcap below a floor) so dashboard filters are not polluted by penny quotes.
3. **Dual listings:** treat each Yahoo symbol separately for price/mcap; use explicit cross-fill (NEM) for SEC/ASX mining fields — do not merge currencies blindly.
4. **Registry:** always list full exchange symbols for duals; never strip `.AX`/`.PA` on lookup.
5. **Re-run this audit** after large YF rebuilds (`python scripts/audit_currency_dual_list.py`).
6. Optional: surface `currency_converted` badge in detail modal when quote ≠ financial.

## Per-market expectations

| Suffix | Typical quote ccy | Notes |
|--------|-------------------|-------|
| (none) US | USD | ADRs may still have foreign financialCurrency |
| `.AX` | AUD | ASX |
| `.T` | JPY | TSE |
| `.KS` / `.KQ` | KRW | KRX |
| `.HK` | HKD | |
| `.L` | GBP | GBp/GBX normalized to GBP for FX |
| `.PA` / `.DE` | EUR | |
| `.TO` | CAD | |

