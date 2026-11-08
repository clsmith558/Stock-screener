"""
Section 6: Natural Language Processing (NLP) on Filings & Announcements.

Implements a multi-layered Financial NLP & Text-Mining engine over ASX announcements,
SEC EDGAR filings (8-K, 10-Q, 10-K, Form 4), mining/energy filing extracts (S-K 1300
reserves, unit costs, Item 1.03 reorganization), and company business descriptions:

1. **Loughran-McDonald Financial Sentiment & Capital-Cycle Lexicon (`score_text_financial_nlp`)**:
   - Evaluates domain-specific financial polarity (`net_sentiment_score` in [-100, +100]),
     Capital-Return Conviction (`capital_return_score`), Operational/Guidance Inflection
     (`guidance_inflection_score`), Uncertainty/Hedging (`uncertainty_pct`), and
     Litigation/Distress/Dilution Risk (`distress_dilution_score`).

2. **Per-Announcement Catalyst & Red-Flag Tagging (`analyze_announcements_list`)**:
   - Tags each announcement or SEC filing with a structured Catalyst Badge
     (`Capital Return / Buyback`, `Insider Accumulation`, `Guidance / Cash-Flow Beat`,
     `Turnaround / Emergence`, `Reserves / Low-Cost Asset`, `Dilution / Equity Raise`,
     `Distress / Covenant / Legal`, `Insider Disposal`) and per-item sentiment score.

3. **TF-IDF + Non-Negative Matrix Factorization (`TfidfVectorizer` + `NMF`) Topic Modeling
   & Cosine Textual Similarity (`run_corpus_nlp_analysis`)**:
   - Vectorizes per-company filing & announcement corpora using 1–2 gram TF-IDF with
     sublinear term-frequency scaling.
   - Extracts 6 latent corporate filing themes via `NMF` and computes TF-IDF cosine
     similarity to surface "Textual / Filing Twins" for any stock.
   - Detects **Bullish Filing Divergence**: companies with high NLP Catalyst & Sentiment
     scores whose share prices remain depressed near 52-week lows.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any

os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 4))

import numpy as np
from sklearn.decomposition import NMF
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger(__name__)

# Domain-specific stop words (boilerplate legal/exchange tokens that carry no alpha)
_FINANCIAL_BOILERPLATE_STOP = frozenset({
    "ltd", "limited", "inc", "corp", "corporation", "plc", "group", "holdings",
    "company", "companies", "asx", "sec", "nyse", "nasdaq", "edgar", "form",
    "item", "items", "period", "date", "dated", "pursuant", "thereof", "herein",
    "announcement", "notice", "notification", "update", "report", "statement",
    "page", "pages", "part", "section", "table", "thousand", "million", "billion",
    "aud", "usd", "gbp", "eur", "jpy", "hkd", "cad", "new", "daily", "monthly",
    "quarterly", "annual", "year", "ended", "ending", "quarter", "months",
})
_CUSTOM_STOP_WORDS = list(ENGLISH_STOP_WORDS.union(_FINANCIAL_BOILERPLATE_STOP))

# -------------------------------------------------------------------------
# 1. LOUGHRAN-MCDONALD + CAPITAL-CYCLE FINANCIAL LEXICON
# -------------------------------------------------------------------------
LM_POSITIVE_TERMS: dict[str, float] = {
    "accretive": 2.0,
    "achieved": 1.3,
    "acquired": 1.4,
    "advancing": 1.1,
    "approved": 1.2,
    "authorization": 1.5,
    "beat": 1.8,
    "beneficial": 1.2,
    "bought": 1.5,
    "buyback": 1.8,
    "buy-back": 1.8,
    "cancellation": 1.7,
    "cancelled": 1.6,
    "cashflow": 1.5,
    "committed": 1.4,
    "completed": 1.2,
    "confirmed": 1.5,
    "confirming": 1.5,
    "deleverage": 1.8,
    "deleveraging": 1.8,
    "delivered": 1.4,
    "dividend": 1.4,
    "earnings": 1.1,
    "effective": 1.2,
    "efficiency": 1.4,
    "emerged": 2.0,
    "emergence": 2.0,
    "exceeded": 1.9,
    "expansion": 1.5,
    "favorable": 1.5,
    "free cash": 1.8,
    "growth": 1.3,
    "high-grade": 1.6,
    "improved": 1.5,
    "improvement": 1.5,
    "increased": 1.2,
    "low-cost": 1.8,
    "margin": 1.3,
    "milestone": 1.4,
    "on-market": 1.6,
    "outperform": 1.8,
    "probable": 1.3,
    "profit": 1.6,
    "profitable": 1.7,
    "profitability": 1.7,
    "proven": 1.5,
    "purchase": 1.4,
    "purchased": 1.5,
    "record": 1.8,
    "recovery": 1.5,
    "reduced debt": 2.0,
    "repurchase": 1.8,
    "repurchased": 1.9,
    "repurchases": 1.8,
    "reserves": 1.4,
    "resolved": 1.5,
    "restored": 1.6,
    "returned": 1.5,
    "robust": 1.5,
    "solid": 1.4,
    "special dividend": 2.2,
    "strengthened": 1.6,
    "strong": 1.5,
    "substantial": 1.3,
    "surplus": 1.6,
    "survivor": 1.7,
    "synergies": 1.4,
    "tender offer": 2.0,
    "turnaround": 1.8,
    "undervalued": 2.0,
    "upgraded": 2.0,
    "upside": 1.6,
}

LM_NEGATIVE_TERMS: dict[str, float] = {
    "administration": 2.2,
    "administrator": 2.2,
    "adverse": 1.6,
    "allegations": 1.8,
    "bankruptcy": 2.5,
    "breach": 2.2,
    "cease": 1.7,
    "challenges": 1.3,
    "chapter 11": 2.2,
    "chapter 7": 2.8,
    "claim": 1.2,
    "class action": 2.4,
    "closure": 1.7,
    "covenant": 1.8,
    "curtailment": 1.7,
    "decline": 1.5,
    "declined": 1.5,
    "default": 2.4,
    "deficit": 1.6,
    "delay": 1.4,
    "delayed": 1.4,
    "delisting": 2.5,
    "dilution": 2.0,
    "dilutive": 2.1,
    "discontinued": 1.6,
    "disposed": 1.6,
    "dispute": 1.6,
    "distress": 2.2,
    "downgrade": 2.0,
    "downgraded": 2.1,
    "downward": 1.6,
    "going concern": 2.8,
    "halt": 1.7,
    "headwinds": 1.5,
    "impairment": 2.1,
    "inflation": 1.3,
    "insolvency": 2.6,
    "investigation": 2.1,
    "lawsuit": 2.0,
    "liquidating": 2.6,
    "liquidation": 2.7,
    "litigation": 2.0,
    "loss": 1.6,
    "losses": 1.7,
    "material weakness": 2.5,
    "non-compliance": 2.1,
    "penalty": 1.8,
    "petition": 1.9,
    "placement": 1.4,
    "receivership": 2.6,
    "restatement": 2.5,
    "restructuring": 1.6,
    "revised downward": 2.2,
    "shortfall": 1.9,
    "sold": 1.4,
    "subpoena": 2.4,
    "suspended": 2.0,
    "suspension": 2.0,
    "terminated": 1.8,
    "termination": 1.8,
    "unfavorable": 1.7,
    "warning": 1.9,
    "winding-up": 2.6,
    "write-down": 2.2,
    "writedown": 2.2,
    "write-off": 2.2,
}

LM_UNCERTAINTY_TERMS: frozenset[str] = frozenset({
    "approximate", "approximately", "contingent", "contingency", "depend",
    "depending", "doubt", "exposure", "fluctuate", "fluctuation", "indefinite",
    "may", "might", "pending", "possible", "possibly", "preliminary", "risk",
    "risks", "speculative", "subject", "uncertain", "uncertainty", "unforeseen",
    "unpredictable", "variable", "volatile", "volatility",
})

CAPITAL_RETURN_PATTERNS = re.compile(
    r"\b(buy-?back|repurchas(?:e|ed|es|ing)|appendix\s*3[ce]|issuer\s+purchases|"
    r"cancellation\s+of\s+shares|tender\s+offer|special\s+dividend|capital\s+return|"
    r"accretive|share\s+reduction|cannibal|substantial\s+holder|appendix\s*3y|"
    r"director['’]?s?\s+interest|form\s*4|open[- ]market\s+purchase|insider\s+buy)\b",
    re.I,
)

GUIDANCE_BEAT_PATTERNS = re.compile(
    r"\b(upgrad(?:e|ed)\s+guidance|raised?\s+guidance|record\s+(?:revenue|earnings|ebitda|cash|production)|"
    r"exceed(?:ed|s)\s+guidance|strong\s+cash\s+flow|margin\s+expansion|cost\s+reduction|"
    r"low[- ]cost\s+quartile|emerged?\s+from\s+chapter\s*11|plan\s+became\s+effective|"
    r"confirm(?:ed|ing)\s+the\s+plan|proven\s+and\s+probable\s+reserves?|reserve\s+life)\b",
    re.I,
)

DILUTION_DISTRESS_PATTERNS = re.compile(
    r"\b(capital\s+raising|share\s+placement|institutional\s+placement|entitlement\s+offer|"
    r"rights\s+issue|appendix\s*3b|going\s+concern|voluntary\s+administration|"
    r"chapter\s*11\s+petitions?|chapter\s*7|receivership|liquidat(?:ion|ing)|"
    r"covenant\s+breach|material\s+weakness|subpoena|restatement|impairment\s+charge|"
    r"downgrad(?:e|ed)\s+guidance|trading\s+halt|suspension\s+from\s+official\s+quotation)\b",
    re.I,
)

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9\-']{1,24}")

_nlp_mem_cache: dict[str, Any] = {}
NLP_CACHE_TTL_SEC = 3600 * 6


def score_text_financial_nlp(text: str) -> dict[str, Any]:
    """
    Evaluate a text passage or collection of filing headlines/snippets using the
    Loughran-McDonald + Capital-Cycle Financial Lexicon.
    """
    raw = (text or "").strip()
    if not raw:
        return {
            "word_count": 0,
            "net_sentiment_score": 0.0,
            "capital_return_score": 0.0,
            "guidance_inflection_score": 0.0,
            "distress_dilution_score": 0.0,
            "uncertainty_pct": 0.0,
            "positive_terms": [],
            "negative_terms": [],
        }

    lower = raw.lower()
    words = _WORD_RE.findall(lower)
    n_words = max(1, len(words))

    pos_score = 0.0
    neg_score = 0.0
    unc_count = 0
    pos_matched: set[str] = set()
    neg_matched: set[str] = set()

    for w in words:
        if w in LM_POSITIVE_TERMS:
            pos_score += LM_POSITIVE_TERMS[w]
            pos_matched.add(w)
        if w in LM_NEGATIVE_TERMS:
            neg_score += LM_NEGATIVE_TERMS[w]
            neg_matched.add(w)
        if w in LM_UNCERTAINTY_TERMS:
            unc_count += 1

    # Check multi-word phrases
    for phrase, weight in (
        ("free cash", 2.0),
        ("special dividend", 2.4),
        ("tender offer", 2.2),
        ("reduced debt", 2.0),
        ("on-market buy-back", 2.2),
        ("share repurchase", 2.0),
        ("proven and probable", 1.8),
        ("emerged from chapter 11", 2.4),
    ):
        if phrase in lower:
            pos_score += weight
            pos_matched.add(phrase)

    for phrase, weight in (
        ("going concern", 3.0),
        ("class action", 2.5),
        ("material weakness", 2.6),
        ("chapter 11", 2.2),
        ("chapter 7", 2.8),
        ("revised downward", 2.3),
        ("write-down", 2.2),
        ("covenant breach", 2.5),
    ):
        if phrase in lower:
            neg_score += weight
            neg_matched.add(phrase)

    cap_hits = len(CAPITAL_RETURN_PATTERNS.findall(raw))
    beat_hits = len(GUIDANCE_BEAT_PATTERNS.findall(raw))
    dist_hits = len(DILUTION_DISTRESS_PATTERNS.findall(raw))

    net_sent = ((pos_score - neg_score) / (pos_score + neg_score + 2.5)) * 100.0
    net_sent = max(-100.0, min(100.0, net_sent))

    cap_ret_score = min(100.0, cap_hits * 18.0 + (20.0 if "buyback" in lower or "repurchase" in lower else 0.0))
    guid_score = max(-100.0, min(100.0, beat_hits * 22.0 - dist_hits * 20.0 + net_sent * 0.35))
    dist_score = min(100.0, dist_hits * 25.0 + neg_score * 5.5)
    unc_pct = min(100.0, (unc_count / n_words) * 100.0)

    return {
        "word_count": len(words),
        "net_sentiment_score": round(net_sent, 1),
        "capital_return_score": round(cap_ret_score, 1),
        "guidance_inflection_score": round(guid_score, 1),
        "distress_dilution_score": round(dist_score, 1),
        "uncertainty_pct": round(unc_pct, 2),
        "positive_terms": sorted(pos_matched)[:8],
        "negative_terms": sorted(neg_matched)[:8],
    }


def classify_announcement_catalyst(headline: str, ann_type: str = "") -> dict[str, Any]:
    """
    Classify a single announcement or SEC filing headline into a structured Catalyst /
    Red-Flag category with sentiment polarity.
    """
    text = f"{headline or ''} {ann_type or ''}".strip()
    low = text.lower()

    if any(k in low for k in ("buy-back", "buyback", "repurchase", "3c", "3e", "cancellation of shares", "tender offer", "special dividend")):
        return {
            "catalyst_id": "capital_return",
            "catalyst_label": "Capital Return / Buyback",
            "tone": "bullish",
            "tone_score": +75,
            "badge_cls": "bg-emerald-100 text-emerald-900 border-emerald-200",
        }
    if any(k in low for k in ("insider", "form 4", "appendix 3y", "change of director", "director's interest", "substantial holder")) and not any(s in low for s in ("sale", "sold", "disposed", "ceasing")):
        return {
            "catalyst_id": "insider_buy",
            "catalyst_label": "Insider / Substantial Accumulation",
            "tone": "bullish",
            "tone_score": +70,
            "badge_cls": "bg-teal-100 text-teal-900 border-teal-200",
        }
    if any(k in low for k in ("emerged", "emergence", "confirming plan", "confirmation order", "doca effectuated", "reorganization")):
        return {
            "catalyst_id": "turnaround_emergence",
            "catalyst_label": "Post-Reorg Emergence / Turnaround",
            "tone": "bullish",
            "tone_score": +68,
            "badge_cls": "bg-indigo-100 text-indigo-900 border-indigo-200",
        }
    if any(k in low for k in ("ore reserve", "mineral resource", "jorc", "proven and probable", "exhibit 96", "low-cost", "aisc")):
        return {
            "catalyst_id": "reserves_costs",
            "catalyst_label": "Reserves / Cost-Curve Disclosure",
            "tone": "bullish",
            "tone_score": +50,
            "badge_cls": "bg-sky-100 text-sky-900 border-sky-200",
        }
    if any(k in low for k in ("placement", "capital raising", "entitlement offer", "rights issue", "share purchase plan", "appendix 3b", "appendix 2a")):
        return {
            "catalyst_id": "dilution_raise",
            "catalyst_label": "Equity Raise / Dilution",
            "tone": "bearish",
            "tone_score": -65,
            "badge_cls": "bg-rose-100 text-rose-900 border-rose-200",
        }
    if any(k in low for k in ("administration", "receivership", "liquidation", "chapter 11", "going concern", "default", "impairment", "trading halt", "suspension", "downgrade")):
        return {
            "catalyst_id": "distress_warning",
            "catalyst_label": "Distress / Halt / Downside Warning",
            "tone": "bearish",
            "tone_score": -80,
            "badge_cls": "bg-rose-100 text-rose-900 border-rose-200",
        }
    if any(k in low for k in ("ceasing to be a substantial", "insider sale", "director sold", "disposed")):
        return {
            "catalyst_id": "insider_exit",
            "catalyst_label": "Insider / Holder Exit",
            "tone": "bearish",
            "tone_score": -55,
            "badge_cls": "bg-amber-100 text-amber-900 border-amber-200",
        }
    if any(k in low for k in ("quarterly", "half year", "full year", "annual report", "10-k", "10-q", "appendix 4e", "appendix 4d", "results", "guidance", "dividend")):
        lex = score_text_financial_nlp(text)
        ns = lex["net_sentiment_score"]
        tone = "bullish" if ns >= 15 else ("bearish" if ns <= -15 else "neutral")
        return {
            "catalyst_id": "earnings_filing",
            "catalyst_label": "Periodic Financial / Operations Filing",
            "tone": tone,
            "tone_score": round(ns),
            "badge_cls": "bg-slate-100 text-slate-800 border-slate-200",
        }

    lex = score_text_financial_nlp(text)
    ns = lex["net_sentiment_score"]
    tone = "bullish" if ns >= 20 else ("bearish" if ns <= -20 else "neutral")
    return {
        "catalyst_id": "general_disclosure",
        "catalyst_label": "Corporate Disclosure",
        "tone": tone,
        "tone_score": round(ns),
        "badge_cls": "bg-slate-100 text-slate-700 border-slate-200",
    }


def _load_company_corpus_from_db(
    db_path: str = "asx_announcements_cache.db",
) -> dict[str, dict[str, Any]]:
    """
    Assemble the multi-source text corpus per ticker from SQLite:
      - ASX `announcements` headlines & classifications
      - US `us_buyback_events` & `us_buyback_repurchases` headlines
      - US `us_insider_purchases` Form 4 summaries
      - `mining_reorg_summary`, `mining_reserves_summary`, `mining_costs_summary`, `operating_extract`
      - Stock sector/industry & cached metadata
    """
    from data.db import connect_db
    from scoring.unsupervised_ml import load_full_global_stock_universe

    stocks = load_full_global_stock_universe(db_path=db_path)
    docs: dict[str, dict[str, Any]] = {}

    for s in stocks:
        t = str(s.get("ticker") or "").strip().upper()
        if not t or (s.get("current_price") or 0) <= 0:
            continue
        m = s.get("metrics") or {}
        ind = str(m.get("industry") or s.get("sector") or "")
        docs[t] = {
            "ticker": t,
            "name": s.get("name") or t,
            "sector": s.get("sector") or "Unknown",
            "industry": ind,
            "pct_from_52w_low": float(s.get("pct_from_52w_low") or 0.0),
            "annual_buyback_pct": float(s.get("annual_buyback_pct") or m.get("annual_buyback_pct") or 0.0),
            "recent_90d_texts": [],
            "older_texts": [],
            "all_snippets": [f"{s.get('sector') or ''} {ind}"],
            "filing_count": 0,
            "price_sensitive_count": 0,
            "catalysts": set(),
        }

    cutoff_90d = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    conn = connect_db(db_path)
    try:
        # 1. ASX Announcements (8,700+ rows)
        for base_t, dt_s, headline, is_ps, cls_t in conn.execute(
            "SELECT ticker, date, headline, is_price_sensitive, classification FROM announcements WHERE headline IS NOT NULL"
        ).fetchall():
            bt = str(base_t or "").strip().upper()
            sym = f"{bt}.AX" if f"{bt}.AX" in docs else bt
            d = docs.get(sym)
            if not d:
                continue
            txt = f"{headline} {cls_t or ''}".strip()
            d["filing_count"] += 1
            if is_ps:
                d["price_sensitive_count"] += 1
            if len(d["all_snippets"]) < 35:
                d["all_snippets"].append(txt)
            if str(dt_s or "")[:10] >= cutoff_90d:
                d["recent_90d_texts"].append(txt)
            else:
                d["older_texts"].append(txt)
            if cls_t == "buyback":
                d["catalysts"].add("On-Market Buyback Notice")
            elif cls_t == "insider_purchase":
                d["catalysts"].add("Director Appendix 3Y Buy")

        # 2. US SEC 8-K / 10-Q Buyback Filings
        for yf_sym, dt_s, form, headline in conn.execute(
            "SELECT yf_symbol, date, form, headline FROM us_buyback_events WHERE headline IS NOT NULL"
        ).fetchall():
            sym = str(yf_sym or "").strip().upper()
            d = docs.get(sym)
            if not d:
                continue
            txt = f"SEC {form} share repurchase buyback program {headline}".strip()
            d["filing_count"] += 1
            d["catalysts"].add(f"SEC {form} Repurchase")
            if len(d["all_snippets"]) < 35:
                d["all_snippets"].append(txt)
            if str(dt_s or "")[:10] >= cutoff_90d:
                d["recent_90d_texts"].append(txt)
            else:
                d["older_texts"].append(txt)

        # 3. US SEC Form 4 Open-Market Insider Purchases (aggregated per ticker)
        for yf_sym, cnt, max_dt, positions in conn.execute(
            """
            SELECT yf_symbol, COUNT(*), MAX(date), GROUP_CONCAT(DISTINCT position)
            FROM us_insider_purchases
            GROUP BY yf_symbol
            """
        ).fetchall():
            sym = str(yf_sym or "").strip().upper()
            d = docs.get(sym)
            if not d:
                continue
            pos_str = str(positions or "director officer")[:120]
            txt = f"SEC Form 4 open-market purchase acquiredbought by insider {pos_str} ({cnt} purchases)"
            d["filing_count"] += int(cnt or 1)
            d["catalysts"].add("SEC Form 4 Insider Buy")
            d["all_snippets"].append(txt)
            if str(max_dt or "")[:10] >= cutoff_90d:
                d["recent_90d_texts"].append(txt)
            else:
                d["older_texts"].append(txt)

        # 4. Mining & Energy 10-K S-K 1300 Reserves, Unit Costs, Reorg Item 1.03 & Operating Extracts
        for t_raw, r_snip, p_snip, life_y in conn.execute(
            "SELECT ticker, reserve_snippet, production_snippet, reserve_life_years FROM mining_reserves_summary"
        ).fetchall():
            sym = str(t_raw or "").strip().upper()
            d = docs.get(sym) or docs.get(f"{sym}.AX")
            if not d:
                continue
            snip = f"{r_snip or ''} {p_snip or ''}".strip()
            if snip:
                d["filing_count"] += 1
                d["all_snippets"].append(snip)
                d["recent_90d_texts"].append(snip)
                if life_y and float(life_y) >= 10:
                    d["catalysts"].add(f"S-K 1300 Reserve Life {float(life_y):.0f}y")

        for t_raw, c_snip, low_q in conn.execute(
            "SELECT ticker, cost_snippet, low_cost_quartile FROM mining_costs_summary"
        ).fetchall():
            sym = str(t_raw or "").strip().upper()
            d = docs.get(sym) or docs.get(f"{sym}.AX")
            if not d or not c_snip:
                continue
            d["filing_count"] += 1
            d["all_snippets"].append(str(c_snip))
            d["recent_90d_texts"].append(str(c_snip))
            if low_q:
                d["catalysts"].add("Low-Cost Curve Q1")

        for t_raw, surv, m_note, ev_json in conn.execute(
            "SELECT ticker, survivor_candidate, manual_note, events_json FROM mining_reorg_summary"
        ).fetchall():
            sym = str(t_raw or "").strip().upper()
            d = docs.get(sym)
            if not d:
                continue
            ev_txt = ""
            if ev_json:
                try:
                    evs = json.loads(ev_json) or []
                    ev_txt = " ".join(str(e.get("snippet") or "") for e in evs[:3])
                except Exception:
                    pass
            comb = f"{m_note or ''} {ev_txt}".strip()
            if comb:
                d["filing_count"] += 1
                d["all_snippets"].append(comb)
                if surv:
                    d["catalysts"].add("Post-Ch.11 Reorg Survivor")

        for t_raw, snip, field_k in conn.execute(
            "SELECT ticker, snippet, field FROM operating_extract WHERE snippet IS NOT NULL"
        ).fetchall():
            sym = str(t_raw or "").strip().upper()
            d = docs.get(sym) or docs.get(f"{sym}.AX")
            if not d or not snip:
                continue
            d["filing_count"] += 1
            d["all_snippets"].append(f"{field_k or ''}: {snip}")
            d["recent_90d_texts"].append(str(snip))
    finally:
        conn.close()

    # Keep only companies that actually have at least 1 filing/announcement/extract
    return {t: d for t, d in docs.items() if d["filing_count"] > 0}


def run_corpus_nlp_analysis(
    *,
    stocks: list[dict[str, Any]] | None = None,
    force_rebuild: bool = False,
    db_path: str = "asx_announcements_cache.db",
) -> dict[str, Any]:
    """
    Run TF-IDF Vectorization + Non-Negative Matrix Factorization (`NMF`) Topic Discovery,
    Loughran-McDonald Financial Sentiment scoring, Tone Momentum, and Bullish Filing
    Divergence detection across the filing corpus.
    """
    t0 = time.time()
    cached = _nlp_mem_cache.get("corpus")
    if not force_rebuild and cached and (time.time() - cached.get("ts", 0)) < NLP_CACHE_TTL_SEC:
        core = cached["data"]
    else:
        docs_map = _load_company_corpus_from_db(db_path=db_path)
        tickers = sorted(docs_map.keys())
        if len(tickers) < 10:
            return {"available": False, "error": "Insufficient filing corpus in cache."}

        corpus_texts = [" ".join(docs_map[t]["all_snippets"]) for t in tickers]

        # 1. TF-IDF Vectorizer (1-2 grams, sublinear TF) + NMF Topic Discovery
        vec = TfidfVectorizer(
            stop_words=_CUSTOM_STOP_WORDS,
            ngram_range=(1, 2),
            max_features=1000,
            min_df=3,
            max_df=0.85,
            sublinear_tf=True,
        )
        X_tfidf = vec.fit_transform(corpus_texts)
        vocab = np.array(vec.get_feature_names_out())

        n_topics = min(6, max(2, X_tfidf.shape[0] // 20))
        nmf = NMF(n_components=n_topics, init="nndsvd", max_iter=200, random_state=42)
        W_topics = nmf.fit_transform(X_tfidf)  # (n_docs, n_topics)
        H_topics = nmf.components_             # (n_topics, n_terms)

        topic_Attachment_idx = np.argmax(W_topics, axis=1)

        topics_summary: list[dict[str, Any]] = []
        topic_labels: dict[int, str] = {}
        used_base_names: set[str] = set()
        for k in range(n_topics):
            top_term_idxs = np.argsort(H_topics[k])[::-1][:6]
            top_terms = [str(vocab[i]) for i in top_term_idxs if H_topics[k, i] > 0]
            joined_t = " ".join(top_terms).lower()
            if "10" in joined_t or "repurchase" in joined_t or "program" in joined_t:
                base_name = "SEC 10-Q / 8-K Share Repurchase Programs"
            elif "buy back" in joined_t or "buyback" in joined_t or "3c" in joined_t or "3e" in joined_t or "on market" in joined_t:
                base_name = "On-Market Buy-Back Execution & Share Cancellation"
            elif "chief" in joined_t or "officer" in joined_t or "president" in joined_t or "executive" in joined_t:
                base_name = "C-Suite & Officer Form 4 Open-Market Purchases"
            elif "director" in joined_t or "insider" in joined_t or "3y" in joined_t or "purchased" in joined_t:
                base_name = "Director & Substantial Holder Accumulation"
            elif "reserve" in joined_t or "coal" in joined_t or "mining" in joined_t or "probable" in joined_t:
                base_name = "Resource Reserves (S-K 1300 / JORC) & Mine Life"
            elif "cost" in joined_t or "production" in joined_t or "gold" in joined_t or "copper" in joined_t or "energy" in joined_t:
                base_name = "Commodity Production, Cash Costs & Margins"
            elif "dividend" in joined_t or "results" in joined_t or "guidance" in joined_t:
                base_name = "Earnings Releases, Dividends & Guidance"
            else:
                base_name = f"Corporate Catalyst Theme #{k + 1}"

            if base_name in used_base_names and top_terms:
                t_name = f"{base_name} ({', '.join(top_terms[:2])})"
            else:
                t_name = base_name
            used_base_names.add(base_name)

            topic_labels[k] = t_name
            member_count = int(np.sum(topic_Attachment_idx == k))
            topics_summary.append({
                "topic_id": k,
                "label": t_name,
                "member_count": member_count,
                "top_terms": top_terms,
            })

        # 2. Score each company's corpus with Loughran-McDonald + Tone Momentum + Top TF-IDF terms
        company_profiles: dict[str, dict[str, Any]] = {}
        for i, t in enumerate(tickers):
            d = docs_map[t]
            full_lex = score_text_financial_nlp(corpus_texts[i])
            rec_txt = " ".join(d["recent_90d_texts"])
            old_txt = " ".join(d["older_texts"])
            rec_lex = score_text_financial_nlp(rec_txt) if rec_txt else full_lex
            old_lex = score_text_financial_nlp(old_txt) if old_txt else full_lex
            tone_mom = round(rec_lex["net_sentiment_score"] - old_lex["net_sentiment_score"], 1)

            # Extract top 4 TF-IDF terms for this company
            row_tfidf = X_tfidf.getrow(i).toarray()[0]
            top_ti = np.argsort(row_tfidf)[::-1][:4]
            top_kw = [str(vocab[idx]) for idx in top_ti if row_tfidf[idx] > 0.05]

            # Composite NLP Alpha Score (0–100)
            nlp_alpha = (
                50.0
                + 0.25 * full_lex["net_sentiment_score"]
                + 0.22 * full_lex["capital_return_score"]
                + 0.18 * full_lex["guidance_inflection_score"]
                + 0.15 * tone_mom
                - 0.30 * full_lex["distress_dilution_score"]
            )
            nlp_alpha = round(max(2.0, min(99.0, nlp_alpha)), 1)

            gap52 = d["pct_from_52w_low"]
            is_bullish_div = bool(nlp_alpha >= 64.0 and gap52 <= 25.0 and full_lex["distress_dilution_score"] < 25.0)
            t_id = int(topic_Attachment_idx[i])

            company_profiles[t] = {
                "ticker": t,
                "name": d["name"],
                "sector": d["sector"],
                "filing_count": d["filing_count"],
                "price_sensitive_count": d["price_sensitive_count"],
                "nlp_score": nlp_alpha,
                "net_sentiment_score": full_lex["net_sentiment_score"],
                "capital_return_score": full_lex["capital_return_score"],
                "guidance_inflection_score": full_lex["guidance_inflection_score"],
                "distress_dilution_score": full_lex["distress_dilution_score"],
                "uncertainty_pct": full_lex["uncertainty_pct"],
                "tone_momentum": tone_mom,
                "pct_from_52w_low": round(gap52, 1),
                "bullish_filing_divergence": is_bullish_div,
                "topic_id": t_id,
                "topic_label": topic_labels.get(t_id, "Corporate Disclosure"),
                "top_tfidf_terms": top_kw,
                "catalysts": sorted(d["catalysts"])[:4],
                "positive_terms": full_lex["positive_terms"][:5],
                "negative_terms": full_lex["negative_terms"][:5],
            }

        core = {
            "computed_at": datetime.now().isoformat(timespec="seconds"),
            "total_companies_with_filings": len(tickers),
            "vocab_size": len(vocab),
            "topics": topics_summary,
            "profiles": company_profiles,
            "_tickers": tickers,
            "_X_tfidf": X_tfidf,
        }
        _nlp_mem_cache["corpus"] = {"ts": time.time(), "data": core}

    profiles: dict[str, dict[str, Any]] = core["profiles"]
    if stocks:
        wanted = set()
        for s in stocks:
            tk = str(s.get("ticker") or "").strip().upper()
            if tk:
                wanted.add(tk)
                if not tk.endswith(".AX"):
                    wanted.add(f"{tk}.AX")
        scoped_list = [p for t, p in profiles.items() if t in wanted]
        if len(scoped_list) < 5:
            scoped_list = list(profiles.values())
    else:
        scoped_list = list(profiles.values())

    bullish_divergences = [
        p for p in scoped_list if p["bullish_filing_divergence"]
    ]
    bullish_divergences.sort(
        key=lambda p: (p["nlp_score"], -p["pct_from_52w_low"], p["filing_count"]),
        reverse=True,
    )

    top_nlp_leaders = sorted(
        scoped_list,
        key=lambda p: (p["nlp_score"], p["capital_return_score"], p["filing_count"]),
        reverse=True,
    )[:25]

    distress_warnings = [
        p for p in scoped_list if p["distress_dilution_score"] >= 20.0 or p["net_sentiment_score"] <= -15.0
    ]
    distress_warnings.sort(
        key=lambda p: (p["distress_dilution_score"], -p["net_sentiment_score"]),
        reverse=True,
    )

    return {
        "available": True,
        "computed_at": core["computed_at"],
        "elapsed_s": round(time.time() - t0, 2),
        "total_companies_with_filings": core["total_companies_with_filings"],
        "scoped_companies_with_filings": len(scoped_list),
        "vocab_size": core["vocab_size"],
        "topics": core["topics"],
        "bullish_filing_divergences": bullish_divergences[:20],
        "top_nlp_leaders": top_nlp_leaders,
        "distress_red_flags": distress_warnings[:15],
    }


def compute_stock_nlp_profile(
    stock: dict[str, Any],
    *,
    announcements: list[dict[str, Any]] | None = None,
    db_path: str = "asx_announcements_cache.db",
) -> dict[str, Any]:
    """
    Compute the detailed per-stock NLP Filing & Announcement Profile for the Stock
    Detail Modal:
      1. Loughran-McDonald Financial Sentiment & Capital-Cycle scores across the
         stock's announcements, SEC filings, mining/operating snippets, and business summary.
      2. Per-announcement Catalyst & Tone classification (`annotated_announcements`).
      3. NMF Topic assignment & top TF-IDF n-grams.
      4. Top 5 TF-IDF Cosine Similarity "Textual / Filing Twins" across the corpus.
    """
    if not stock:
        return {"available": False}

    ticker = str(stock.get("ticker") or "").strip().upper()
    canon_t = ticker if ticker.endswith(".AX") else ticker

    # Ensure corpus model is warm
    cached = _nlp_mem_cache.get("corpus")
    if not cached or (time.time() - cached.get("ts", 0)) >= NLP_CACHE_TTL_SEC:
        run_corpus_nlp_analysis(db_path=db_path)
        cached = _nlp_mem_cache.get("corpus")

    core = cached["data"] if cached else None
    profiles = (core and core.get("profiles")) or {}
    corpus_prof = profiles.get(canon_t) or profiles.get(f"{canon_t}.AX") or profiles.get(canon_t.replace(".AX", ""))

    # Annotate individual announcements passed in or loaded from stock
    anns = announcements if announcements is not None else (stock.get("_announcements") or [])
    annotated_anns: list[dict[str, Any]] = []
    ann_texts: list[str] = []
    for a in anns[:15]:
        hl = str(a.get("headline") or a.get("desc") or "")
        tp = str(a.get("type") or "")
        cat = classify_announcement_catalyst(hl, tp)
        ann_texts.append(f"{hl} {tp}")
        annotated_anns.append({
            "date": (a.get("date") or "")[:10],
            "headline": hl,
            "url": a.get("url") or "",
            "source": a.get("source") or "",
            "price_sensitive": bool(a.get("price_sensitive")),
            "catalyst_label": cat["catalyst_label"],
            "tone": cat["tone"],
            "tone_score": cat["tone_score"],
            "badge_cls": cat["badge_cls"],
        })

    # Combine stock summary + signals + announcements + filing snippets
    sig_texts = [str(s.get("desc") or s.get("headline") or "") for s in (stock.get("signals") or [])]
    summary_txt = str(stock.get("summary") or "")
    combined_txt = " ".join([summary_txt] + ann_texts + sig_texts).strip()
    live_lex = score_text_financial_nlp(combined_txt)

    # Merge corpus profile with live summary/announcement lexicon
    if corpus_prof:
        nlp_score = corpus_prof["nlp_score"]
        net_sent = corpus_prof["net_sentiment_score"]
        cap_ret = max(corpus_prof["capital_return_score"], live_lex["capital_return_score"])
        guid_inf = corpus_prof["guidance_inflection_score"]
        dist_score = max(corpus_prof["distress_dilution_score"], live_lex["distress_dilution_score"])
        unc_pct = max(corpus_prof["uncertainty_pct"], live_lex["uncertainty_pct"])
        tone_mom = corpus_prof["tone_momentum"]
        topic_label = corpus_prof["topic_label"]
        top_terms = corpus_prof["top_tfidf_terms"]
        catalysts = corpus_prof["catalysts"]
        bull_div = corpus_prof["bullish_filing_divergence"]
        pos_terms = sorted(set(corpus_prof["positive_terms"] + live_lex["positive_terms"]))[:8]
        neg_terms = sorted(set(corpus_prof["negative_terms"] + live_lex["negative_terms"]))[:8]
        filing_cnt = max(corpus_prof["filing_count"], len(annotated_anns))
    else:
        if not combined_txt:
            return {"available": False}
        net_sent = live_lex["net_sentiment_score"]
        cap_ret = live_lex["capital_return_score"]
        guid_inf = live_lex["guidance_inflection_score"]
        dist_score = live_lex["distress_dilution_score"]
        unc_pct = live_lex["uncertainty_pct"]
        tone_mom = 0.0
        nlp_score = round(
            max(5.0, min(98.0, 50.0 + 0.25 * net_sent + 0.22 * cap_ret + 0.18 * guid_inf - 0.30 * dist_score)),
            1,
        )
        topic_label = "Business Summary & Recent Disclosures"
        top_terms = live_lex["positive_terms"][:4]
        catalysts = []
        bull_div = bool(nlp_score >= 64.0 and float(stock.get("pct_from_52w_low") or 99.0) <= 25.0)
        pos_terms = live_lex["positive_terms"]
        neg_terms = live_lex["negative_terms"]
        filing_cnt = len(annotated_anns)

    # Find Top 5 Textual / Filing Twins via TF-IDF Cosine Similarity
    textual_twins: list[dict[str, Any]] = []
    if core and corpus_prof:
        t_list: list[str] = core["_tickers"]
        X_tfidf = core["_X_tfidf"]
        match_t = corpus_prof["ticker"]
        if match_t in t_list:
            idx = t_list.index(match_t)
            sims = cosine_similarity(X_tfidf.getrow(idx), X_tfidf)[0]
            order = np.argsort(sims)[::-1]
            for j_idx in order:
                if int(j_idx) == idx:
                    continue
                sim_val = float(sims[int(j_idx)])
                if sim_val < 0.08:
                    break
                peer_t = t_list[int(j_idx)]
                peer_p = profiles.get(peer_t) or {}
                textual_twins.append({
                    "ticker": peer_t,
                    "name": peer_p.get("name") or peer_t,
                    "sector": peer_p.get("sector") or "Unknown",
                    "cosine_similarity_pct": round(sim_val * 100.0, 1),
                    "nlp_score": peer_p.get("nlp_score"),
                    "shared_topic": peer_p.get("topic_label") or "",
                    "shared_terms": [w for w in (peer_p.get("top_tfidf_terms") or []) if w in top_terms][:3]
                    or (peer_p.get("top_tfidf_terms") or [])[:2],
                })
                if len(textual_twins) >= 5:
                    break

    return {
        "available": True,
        "ticker": ticker,
        "filing_count": filing_cnt,
        "nlp_score": nlp_score,
        "net_sentiment_score": net_sent,
        "capital_return_score": cap_ret,
        "guidance_inflection_score": guid_inf,
        "distress_dilution_score": dist_score,
        "uncertainty_pct": unc_pct,
        "tone_momentum": tone_mom,
        "bullish_filing_divergence": bull_div,
        "topic_label": topic_label,
        "top_tfidf_terms": top_terms,
        "catalysts": catalysts,
        "positive_terms": pos_terms,
        "negative_terms": neg_terms,
        "textual_twins": textual_twins,
        "annotated_announcements": annotated_anns,
    }
