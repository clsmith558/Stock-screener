# Dashboard formulas

Reference for metrics shown in the Signal Screener UI.  
Code sources: `scoring/quant_score.py`, `data/yf_helpers.py`, `data/wiki_bridge.py`, `data/earnings_multiples.py`, `data/commodity_producers.py`, `config/opportunity_weights.yaml`.

Weights and thresholds below match the defaults in `config/opportunity_weights.yaml` (override that file to change Score weights).

---

## Score (column: **Score**)

**Score is quantitative only.** It does **not** use Obsidian vault coverage, memo existence, or vault `qual_score`.

| Field | Meaning |
|--------|---------|
| `quant_score` | Weighted sum of four quant components (0–100) |
| `composite_score` | **Same as `quant_score`** (what the Score column displays) |
| `qual_score` / `coverage_tier` | Vault-only metadata for links / Tier chip — **not** in Score |

### Overall Score

\[
\text{Score} = w_v \cdot V + w_p \cdot P + w_i \cdot I + w_b \cdot B
\]

Default weights (\(w_v + w_p + w_i + w_b = 1\)):

| Component | Symbol | Weight |
|-----------|--------|--------|
| Valuation | \(V\) | \(w_v = 0.35\) |
| Price posture | \(P\) | \(w_p = 0.25\) |
| Insider | \(I\) | \(w_i = 0.20\) |
| Buyback at low | \(B\) | \(w_b = 0.20\) |

Each of \(V, P, I, B\) is on a **0–100** scale before weighting.

**Previous behaviour (removed):**  
`composite_score = quant_score × (qual_score / 100)` scaled Score down when the vault had thin coverage. That multiplication is **disabled**.

---

### Valuation component \(V\)

Uses peer-relative percentile ranks among stocks currently on the same list/load (`peers`).

**Percentile rank** for value \(x\) vs population \(\{v_i\}\) (positive values only):

- **Lower is better** (P/FCF, EV/EBITDA):  
  \(\text{rank} = \#\{v_i \ge x\} / N \times 100\)
- **Higher is better** (FCF yield):  
  \(\text{rank} = \#\{v_i \le x\} / N \times 100\)

If a metric is missing or ≤ 0, it is skipped. If none available, \(V = 50\).

\[
V = \text{mean of available ranks among: P/FCF, EV/EBITDA, FCF yield}
\]

| Input | Source | Direction |
|-------|--------|-----------|
| P/FCF | `p_fcf` | lower better |
| EV/EBITDA | `metrics.ev_ebitda` | lower better |
| FCF yield % | `metrics.fcf_yield` | higher better |

---

### Price posture \(P\)

Uses **gap to 52-week low** \(g = \text{pct_from_52w_low}\) and **gap to 200-week MA** \(m = \text{pct_from_200w_ma}\).

Default near-low threshold: \(g^* = 15\%\).

\[
\begin{align*}
P_0 &= 
\begin{cases}
60 + \max(0,\, 2\cdot(g^* - g)) & g < g^* \\
40 & g^* \le g < 30 \\
20 & 30 \le g < 50 \\
0 & g \ge 50
\end{cases} \\[0.5em]
P &= \min\bigl(100,\; P_0 + \mathbf{1}_{m<0}\cdot\min(25,\, |m|)\bigr)
\end{align*}
\]

(Bonus for trading below the 200-week MA is on by default: `below_200w_ma_bonus: true`.)

---

### RSI(14)

Wilder relative strength index on **daily** auto-adjusted closes, period \(n = 14\). Not part of Score.

\[
\begin{align*}
\Delta_t &= C_t - C_{t-1} \\
\text{avg gain}_t &= \bigl((n-1)\cdot\text{avg gain}_{t-1} + \max(\Delta_t,0)\bigr)/n \\
\text{avg loss}_t &= \bigl((n-1)\cdot\text{avg loss}_{t-1} + \max(-\Delta_t,0)\bigr)/n \\
\text{RSI}_t &= 100 - 100/\bigl(1 + \text{avg gain}_t/\text{avg loss}_t\bigr)
\end{align*}
\]

| Display | Meaning |
|---------|---------|
| Table **RSI** | Latest daily RSI(14) from the yfinance cache (blank until a YF rebuild or a detail open that writes it) |
| Popup chart | Same series over ~2 years, with 70 / 30 guide lines |

Convention: \(\text{RSI} \ge 70\) overbought (rose), \(\text{RSI} \le 30\) oversold (green).

---

### Insider \(I\)

\(n\) = number of open-market insider purchase events counted for the row (`insider_buys_2026`).

\[
I = \min(\text{insider\_cap},\; n \times \text{points\_per\_buy})
\]

Defaults: 25 points per buy, cap 100. If \(n = 0\), \(I = 0\).

---

### Buyback at low \(B\)

Uses buyback flags (`buyback_announced`, `annual_buyback_pct`, exchange buyback sources) and \(g = \text{pct_from_52w_low}\).  
Near-low threshold: \(g_b^* = 20\%\) (`buyback_near_low_pct`).

| Condition | \(B\) |
|-----------|--------|
| Buyback announced **and** \(g < g_b^*\) | 90 |
| Annual buyback % \(> 1\) **and** \(g < g_b^*\) | \(\min(100,\; 70 + 3\cdot\text{annual\_buyback\_pct})\) |
| HK/JP/ASX buyback source **and** \(g < g_b^*\) | 75 |
| Any buyback signal but **not** near low | 40 |
| No buyback signal | 0 |

---

### Rank

`opportunity_rank` = 1-based rank after sorting the current list by **Score** descending (higher Score = better rank number 1).

---

### Vault fields (not in Score)

Still shown as Tier / vault links / detail “Vault qual”:

| Field | Role |
|-------|------|
| `coverage_tier` | A/B/C/D from Company-Registry |
| `qual_score` | Base by tier (A≈75, B≈60, C≈35, D≈20; default 30 if unmatched), with optional leverage/regulation penalties |
| Wiki URIs | Obsidian links only |
| Opportunity memo | `wiki/investing/opportunities/{TICKER}.md` — create/open/push from detail; `/enrich` writes Score + multiples + driver/filing scalars into frontmatter |

These do **not** enter \(\text{Score}\). The memo snapshot is a research handoff, not a scoring input.

---

## Price & range metrics

### Gap to 52-week low (`pct_from_52w_low`)

After reconciling so that \(\text{low} \le \text{price} \le \text{high}\):

\[
\text{pct_from_52w_low} = \max\Bigl(0,\; \frac{\text{price} - \text{low}_{52w}}{\text{low}_{52w}} \times 100\Bigr)
\]

Floored at **0%** (cannot report below the low). Yahoo lows that lag a print are extended to include current price before this calc.

### Last 52-week low date (`last_52w_low_date`)

Most recent session when the stock printed the **trailing ~252 trading-day low** (daily `Low`, else `Close`).

- Source: yfinance daily history at YF fetch time (`data/yf_helpers.py` → `calc_last_52w_low_metrics`)
- If several days share the same low, the **latest** of those days is kept (latest re-test)
- Also stored: `days_since_52w_low` (calendar days)
- Column **Last 52w Low** in table views; filter **Recent 52w lows** (within N days, default **20**, optional still near low ≤2% gap)
- **Blank until Rebuild YF** for symbols whose cache pre-dates this field

### List breadth KPIs (dashboard strip)

Computed client-side on the **full loaded list** (`allData`, pending-YF rows excluded), not only the filtered table:

| KPI | Meaning |
|-----|---------|
| **Recent 52w lows** | Count (and %) with `days_since_52w_low ≤ N` (N = Recent 52w lows filter setting, default 20) |
| **Near low** | % with `pct_from_52w_low ≤ 2%` |
| **Median 52w gap** | Median gap of the list; mean shown in subtitle |

When few names have `last_52w_low_date`, the recent-lows card notes `withDate/total` and suggests Rebuild YF.

### Own-history cheapness (`own_hist_*`)

Compares **current** P/B, P/FCF, and FCF yield to a reconstructed path from the stock’s own ~5y weekly closes (book and FCF levels held fixed at today’s implied levels).

| Field | Meaning |
|-------|---------|
| `own_hist_*_pctile` | 100 = cheapest vs own history on that metric |
| `own_hist_cheap` | True if **any** metric is in the cheapest quintile (pctile ≥ 80) |
| `own_hist_label` | `cheap_vs_self` / `mid_vs_self` / `rich_vs_self` / `unknown` |

UI: **Self$** chip on Tier column; detail panel lines. Needs YF rebuild to populate. Not a full fundamental history model.

### Score weights UI

Dashboard **Score weights** panel edits `config/opportunity_weights.yaml` via `POST /api/score-weights`. The four component weights are re-normalized to sum to 1. After save, the current list reloads so scores recompute.

### Watchlist alerts

`GET /api/watchlist/alerts` evaluates watched names for:

- **recent_52w_low** — `days_since_52w_low ≤ N` (default 20)
- **burry_pass** — lightweight Burry-style gates (no peer EV/EBITDA median)

New fires write `logs/watchlist_alerts.log`, show an amber banner, and optionally browser `Notification`. State in `data/watchlist.db` (`watchlist_alert_state`) prevents spam (clears when condition drops; cooldown 48h).

### Gap to 200-week MA (`pct_from_200w_ma`)

\[
\text{MA}_{200w} = \text{mean of last } \min(200, N) \text{ weekly closes}
\]

\[
\text{pct_from_200w_ma} = \frac{\text{price} - \text{MA}_{200w}}{\text{MA}_{200w}} \times 100
\]

(Can be negative if price is below the MA.)

---

## Valuation multiples (data layer)

### PEG ratio

Shown in the full table after **D/E** when available; otherwise `—`.

\[
\text{PEG} = \frac{\text{P/E}}{\text{earnings growth \%}}
\]

Example: trailing P/E 15 and earnings growth 10% → PEG = 1.5.

Sources (first match that works):

1. Computed from trailing P/E and yfinance `earningsGrowth` (or quarterly growth), with growth normalized to a percent denominator  
2. Else yfinance `pegRatio` / `trailingPegRatio` when present  

Missing growth or non-positive P/E → blank (not zero).

### P/FCF

\[
\text{P/FCF} = \frac{\text{price}}{\text{FCF} / \text{shares outstanding}}
\quad\text{(only if FCF/share > 0)}
\]

FCF amounts are converted into the **quote** (market-cap) currency when `financialCurrency` differs.

### Enterprise value

\[
\text{EV} = \text{market cap} - \text{cash} + \text{debt}
\quad\text{(same currency)}
\]

### EV / EBITDA

\[
\text{EV/EBITDA} = \frac{\text{EV}}{\text{EBITDA}}
\quad\text{(EBITDA in quote currency when FX-aligned)}
\]

### FCF yield

\[
\text{FCF yield (\%)} = \frac{\text{FCF}}{\text{market cap}} \times 100
\]

### EV / FCF

\[
\text{EV/FCF} = \frac{\text{EV}}{\text{FCF}}
\quad\text{(FCF > 0)}
\]

### FCF quality flag (`metrics.fcf_quality`)

**Coarse free-data heuristic** — not owner earnings, not maintenance capex, not a full cash-flow model.

Inputs (quote-currency aligned when FX differs):

| Field | Yahoo source | Stored |
|-------|--------------|--------|
| FCF | `freeCashflow` | `fcf_m` ($m) |
| OCF | `operatingCashflow` | `ocf_m` ($m) |
| Capex spend | \(\lvert\)`capitalExpenditures`\(\rvert\) | `capex_m` ($m) |

\[
\text{FCF/OCF} = \frac{\text{FCF}}{\text{OCF}} \quad(\text{OCF} > 0)
\]

| Label | Rule (approx.) | Meaning |
|-------|----------------|---------|
| `solid` | FCF > 0 and FCF/OCF ≥ 0.70 | Most OCF becomes FCF (maintenance-ish / light growth) |
| `mixed` | FCF > 0 and 0.40 ≤ FCF/OCF < 0.70 | Meaningful reinvestment |
| `capex_heavy` | FCF > 0 and FCF/OCF < 0.40 | Growth / heavy reinvestment; FCF understates OCF power |
| `weak` | OCF > 0 but FCF ≤ 0 | Capex + WC absorb cash; “FCF” not supportive |
| `unknown` | Missing OCF/FCF (or FCF without OCF) | Needs YF rebuild or feed gap |

Also stored: `fcf_ocf_ratio`, `capex_ocf_ratio` (capex spend / OCF when both present).

Code: `data/yf_helpers.py` → `classify_fcf_quality` / `calc_fcf_metrics`. Shown in the stock detail panel. **Existing cache rows stay `unknown` until Rebuild YF.**

### Net cash / market cap

\[
\text{net cash} = \text{cash} - \text{debt}
\]

\[
\text{net cash \% mcap} = \frac{\text{net cash}}{\text{market cap}} \times 100
\]

### Debt / equity (display)

Yahoo `debtToEquity` is often expressed as percent-like; the screener stores:

\[
\text{debt_to_equity} \approx \frac{\text{Yahoo debtToEquity}}{100}
\]

when the raw field is present.

### P / 5-year avg earnings (`pe_5y_avg_earnings`)

\[
\text{EPS}_{5y\text{ avg}} = \text{mean of up to last 5 annual EPS}
\]

\[
\text{P / 5Y avg earn} = \frac{\text{price}}{\text{EPS}_{5y\text{ avg}}}
\quad\text{(if avg EPS > 0)}
\]

Peak-earnings variant: \(\text{price} / \max(\text{annual EPS})\).

---

## Commodity tab

### Stock / commodity ratio

On common trading dates:

\[
\text{ratio}_t = \frac{\text{stock close}_t}{\text{commodity last}_t}
\]

### Ratio z-score

Over the last \(L\) ratios (default \(L = 90\)):

\[
z = \frac{\text{ratio}_{\text{now}} - \mu}{\sigma}
\]

(\(\mu, \sigma\) = population mean and std of the lookback window; need ≥ 5 points.)

### Unit margin (USD)

\[
\text{unit margin} = \text{commodity price} - \text{unit cost (USD)}
\]

\[
\text{margin \%} = \frac{\text{unit margin}}{\text{commodity price}} \times 100
\]

ASX costs reported in A$ are converted to USD via FX before margin.

### Undervalued flag (default thresholds)

True when:

\[
z \le -1.0 \quad\text{and}\quad \text{margin \%} \ge 15\%
\]

(configurable: `undervalued_ratio_z_max`, `undervalued_min_margin_pct`).

### Relative return (3m)

\[
\text{relative 3m} = \text{stock return 3m} - \text{commodity return 3m}
\]

---

## Index tab (drawdowns)

Typical form (where ATH = all-time high in the series):

\[
\text{pct from ATH} = \frac{\text{last} - \text{ATH}}{\text{ATH}} \times 100
\]

(negative when below the high).

---

## Filter screens (logic, not Score)

These **filter** rows; they are not part of Score unless the same inputs appear in quant components above.

| Screen | Core idea |
|--------|-----------|
| Burry / “road-kill” | Near 52w low; cheap EV/FCF; low leverage; EV/EBITDA ≤ sector median on list |
| Templeton-style | Near 52w low; D/E ≤ 1; debt/EBITDA ≤ 3 (est.); EV/EBITDA ≤ sector; P/B ≤ 1.5 |
| Pariah | **Universe first** (seed tickers + tight Yahoo industries: drillers, coal, steel, shipping/shipbuilding, bulk/uranium miners). Then depressed price (52w gap or 200w MA) + clean BS + cheap vs book. Optional reserve life / low-cost flags. |

Majors, midstream, utilities, and generic Energy/Industrials are **not** pariahs unless listed. The company popup shows pariah gates only for universe names.

Exact thresholds and the ticker/industry lists are in `config/pariah_industries.yaml` and the filter JS in `templates/index.html`.

---

## Quick map: UI label → field / formula

| UI | Field / formula |
|----|-----------------|
| Score | `composite_score` = `quant_score` (quant only) |
| Rank | sort by Score desc |
| Tier | vault `coverage_tier` only |
| 52w Gap | \(\max(0,(P-L)/L\times 100)\) |
| 200w MA Gap | \((P-\text{MA}_{200w})/\text{MA}_{200w}\times 100\) |
| RSI | Wilder RSI(14) on daily closes |
| P/E, P/B | yfinance trailing / price-to-book |
| PEG | P/E ÷ growth % (or Yahoo pegRatio) when available |
| P/FCF | price ÷ (FCF/shares) |
| Net Cash | cash − debt (quote FX) |
| Net Cash % | net cash / mcap × 100 |
| P / 5Y avg earn | price / mean annual EPS (≤5y) |

---

## Changing Score weights

Edit [`config/opportunity_weights.yaml`](../config/opportunity_weights.yaml):

```yaml
valuation: 0.35
price_posture: 0.25
insider: 0.20
buyback_at_low: 0.20
near_52w_low_pct: 15
buyback_near_low_pct: 20
insider_points_per_buy: 25
insider_cap: 100
```

Restart the app (or reload the process) so weights are re-read.
