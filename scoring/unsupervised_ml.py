"""
Unsupervised Learning Engine for Stock Screener (`scikit-learn` + `numpy`).

Implements the models from Section 2 (Unsupervised Learning: Clustering & Anomaly Detection):
1. **Fundamental Archetype Clustering (`KMeans` + Silhouette-guided `k` selection)**
   - Standardizes fundamental & balance-sheet features with `RobustScaler` (median / IQR)
     and `KNNImputer` (so partial missing ratios don't drop stocks).
   - Groups stocks into interpretable fundamental archetypes (e.g. "Cash-Rich Deep Value",
     "High-ROIC Compounder", "Depressed Cyclical / Turnaround", "Leveraged / Distressed")
     based on each cluster's centroid in `(Value Z, Quality Z, Capitulation Z)` space.
2. **k-Nearest Neighbors (`NearestNeighbors`) "True Mathematical Comps" Recommender**
   - Finds the top-`k` closest fundamental & business-profile peers for any stock using
     weighted Euclidean / cosine distance on quality, balance-sheet, size, and cash-flow
     features.
   - Computes a **Peer Valuation Discount (`peer_val_diff_z`)**: how cheap or expensive
     the target stock is relative to its `k` mathematical peers (surfacing stocks that
     have the same quality/margin/leverage profile as expensive peers but trade at a
     steep discount).
3. **Multivariate Anomaly Detection (`IsolationForest` + Robust Mahalanobis Distance)**
   - Fits a `scikit-learn` `IsolationForest` and `MinCovDet` / regularized Mahalanobis
     distance in the joint space of Valuation, Quality, Balance Sheet, and Price Posture.
   - Separates **General Statistical Outliers** (`anomaly_score`, `0–100`) from
     **"Mispriced Quality Outliers"** (`mispriced_outlier_score`, `0–100`): anomalies
     that sit in the *high-quality + cheap-valuation* quadrant (`val_z > 0` and
     `qual_z > 0` or strong positive residual from the cross-sectional OLS/Theil-Sen
     regression of `Valuation ~ Quality`).
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
import json
from datetime import datetime, timedelta
from typing import Any

# Silence joblib/loky physical-core subprocess lookup warning on Windows where wmic is absent
os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 4))

import numpy as np
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.ensemble import IsolationForest
from sklearn.impute import KNNImputer, SimpleImputer
from sklearn.linear_model import HuberRegressor
from sklearn.manifold import SpectralEmbedding
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import RobustScaler

from scoring.eda_stats import compute_cross_sectional_eda, extract_metric

logger = logging.getLogger(__name__)

# Features used to define a company's fundamental & balance-sheet profile
# for k-NN peer matching (intentionally weighted toward Quality, Scale, and
# Capital Structure so we match *similar businesses* and then compare their Valuations!).
KNN_PEER_FEATURES: list[dict[str, Any]] = [
    {"key": "roic_or_roe", "label": "ROIC/ROE %", "weight": 1.35},
    {"key": "fcf_yield", "label": "FCF Yield %", "weight": 1.15},
    {"key": "debt_to_equity", "label": "Debt/Equity", "weight": 1.25},
    {"key": "net_cash_pct_mcap", "label": "Net Cash/MCap %", "weight": 1.20},
    {"key": "log_mcap_usd", "label": "Log10 Size (USD)", "weight": 1.00},
    {"key": "annual_buyback_pct", "label": "Buyback %", "weight": 0.80},
    {"key": "pct_from_52w_low", "label": "52w Low Gap %", "weight": 0.75},
]

# Full feature set for K-Means Archetype Clustering & Isolation Forest Anomaly Detection
FULL_ML_FEATURES: list[dict[str, Any]] = [
    {"key": "ev_ebitda", "label": "EV/EBITDA", "log_transform": True},
    {"key": "p_fcf", "label": "P/FCF", "log_transform": True},
    {"key": "pb", "label": "P/B", "log_transform": True},
    {"key": "pe", "label": "P/E", "log_transform": True},
    {"key": "fcf_yield", "label": "FCF Yield %", "log_transform": False},
    {"key": "roic_or_roe", "label": "ROIC/ROE %", "log_transform": False},
    {"key": "net_cash_pct_mcap", "label": "Net Cash/MCap %", "log_transform": False},
    {"key": "debt_to_equity", "label": "Debt/Equity", "log_transform": True},
    {"key": "net_assets_vs_mcap_pct", "label": "(Equity-MCap) %", "log_transform": False},
    {"key": "pct_from_52w_low", "label": "52w Low Gap %", "log_transform": True},
    {"key": "pct_from_200w_ma", "label": "200w MA Gap %", "log_transform": False},
    {"key": "rsi", "label": "RSI(14)", "log_transform": False},
    {"key": "annual_buyback_pct", "label": "Buyback %", "log_transform": False},
]

# Rough FX conversion to USD solely for `log10(market_cap_usd)` scale-matching across multi-currency lists
_APPROX_FX_TO_USD: dict[str, float] = {
    "USD": 1.0,
    "AUD": 0.66,
    "EUR": 1.08,
    "GBP": 1.28,
    "CAD": 0.73,
    "CHF": 1.13,
    "SGD": 0.74,
    "NZD": 0.60,
    "HKD": 0.128,
    "CNY": 0.138,
    "JPY": 0.0066,
    "KRW": 0.00073,
    "IDR": 0.000063,
    "TWD": 0.031,
    "INR": 0.012,
    "BRL": 0.18,
    "MXN": 0.050,
    "MYR": 0.22,
    "THB": 0.029,
    "VND": 0.000040,
    "TRY": 0.029,
    "NOK": 0.092,
    "ISK": 0.0072,
    "ILS": 0.27,
}


def _extract_ml_feature(stock: dict[str, Any], key: str) -> float | None:
    if key == "log_mcap_usd":
        m = stock.get("metrics") or {}
        mcap = stock.get("market_cap") or m.get("market_cap")
        try:
            mcap_f = float(mcap)
        except (TypeError, ValueError):
            return None
        if not (mcap_f > 0 and math.isfinite(mcap_f)):
            return None
        ccy = str(stock.get("currency") or m.get("currency") or "USD").upper()
        fx = _APPROX_FX_TO_USD.get(ccy, 1.0)
        usd_mcap = max(1e4, mcap_f * fx)
        return math.log10(usd_mcap)

    if key == "annual_buyback_pct":
        m = stock.get("metrics") or {}
        v = stock.get("annual_buyback_pct")
        if v is None:
            v = m.get("annual_buyback_pct")
        try:
            f = float(v) if v is not None else 0.0
            return f if math.isfinite(f) and f >= 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    val = extract_metric(stock, key)
    if val is None:
        return None
    if key in ("ev_ebitda", "p_fcf", "pb", "pe", "rsi") and val <= 0:
        return None
    if key in ("debt_to_equity", "pct_from_52w_low") and val < 0:
        return None
    return float(val)


def _name_cluster_archetype(val_z: float, qual_z: float, post_z: float, net_cash_pct: float) -> str:
    """Assign a human-readable quantitative archetype label to a K-Means cluster centroid."""
    if val_z >= 0.35 and qual_z >= 0.35:
        if net_cash_pct >= 15:
            return "Cash-Rich Deep Value & Quality"
        return "High-Quality Value (GARP / Compounders)"
    if val_z >= 0.45 and qual_z < 0.35:
        if post_z >= 0.35:
            return "Depressed Deep-Value Cyclicals"
        return "Low-Multiple Value"
    if qual_z >= 0.45 and val_z < 0.35:
        return "Premium Quality Compounders"
    if post_z >= 0.50:
        return "Capitulated / Oversold Contrarian"
    if val_z <= -0.35 and qual_z <= -0.25:
        return "Expensive / Weak-Fundamental Laggards"
    return "Core Market / Fairly Valued"


def run_unsupervised_analysis(
    stocks: list[dict[str, Any]],
    *,
    n_neighbors: int = 5,
    n_clusters: int = 6,
    contamination: float = 0.10,
) -> dict[str, Any]:
    """
    Fit the full suite of Unsupervised Learning models on `stocks`:
    1. K-Means Clustering (`KMeans`) -> assigns `cluster_id` and `cluster_label`
    2. k-Nearest Neighbors (`NearestNeighbors`) -> finds top `n_neighbors` fundamental comps
       and computes `peer_val_diff_z` (valuation discount vs. closest comps)
    3. Anomaly Detection (`IsolationForest` + Robust Mahalanobis + Huber Regression residual)
       -> computes `anomaly_score` (0–100) and `mispriced_outlier_score` (0–100).
    """
    eda = compute_cross_sectional_eda(stocks)
    profiles = eda.get("profiles") or {}

    # Filter to stocks with enough fundamental coverage (at least 3 EDA metrics)
    valid_stocks: list[dict[str, Any]] = []
    for s in stocks or []:
        if not s or s.get("_pending_yf"):
            continue
        t = str(s.get("ticker") or "").upper()
        prof = profiles.get(t)
        if prof and prof.get("metrics_used", 0) >= 3:
            valid_stocks.append(s)

    n_samples = len(valid_stocks)
    if n_samples < 6:
        return {
            "total_stocks": len(stocks or []),
            "modeled_stocks": n_samples,
            "clusters": [],
            "top_mispriced_outliers": [],
            "profiles": {},
            "note": "Need at least 6 stocks with cached fundamentals to fit unsupervised ML models.",
        }

    tickers = [str(s.get("ticker") or "").upper() for s in valid_stocks]
    names = [str(s.get("name") or t) for s, t in zip(valid_stocks, tickers)]
    sectors = [str(s.get("sector") or "Unknown") for s in valid_stocks]

    # -------------------------------------------------------------------------
    # STEP 1: Build & Impute Full Feature Matrix X_full for Clustering & Anomaly Detection
    # -------------------------------------------------------------------------
    n_full_feats = len(FULL_ML_FEATURES)
    X_raw = np.full((n_samples, n_full_feats), np.nan, dtype=np.float64)

    for i, s in enumerate(valid_stocks):
        for j, fspec in enumerate(FULL_ML_FEATURES):
            v = _extract_ml_feature(s, fspec["key"])
            if v is not None and math.isfinite(v):
                if fspec.get("log_transform"):
                    v = math.log1p(max(0.0, v))
                X_raw[i, j] = v

    # Drop any columns that are 100% NaN in this universe
    valid_col_mask = ~np.all(np.isnan(X_raw), axis=0)
    active_feature_specs = [f for f, keep in zip(FULL_ML_FEATURES, valid_col_mask) if keep]
    X_active = X_raw[:, valid_col_mask]

    # Robust scale first (handles NaNs gracefully in sklearn >= 1.0), then impute missing values
    scaler_full = RobustScaler(quantile_range=(25.0, 75.0))
    X_scaled_nan = scaler_full.fit_transform(X_active)
    # Clip extreme winsorized values to [-4, +4] before imputation/clustering
    X_scaled_nan = np.clip(X_scaled_nan, -4.0, 4.0)

    k_imp = min(5, max(1, n_samples - 1))
    if n_samples > 2500:
        # On the full ~17k global universe, median imputation avoids O(N^2) pairwise KNNImputer overhead
        imputer = SimpleImputer(strategy="median")
    else:
        imputer = KNNImputer(n_neighbors=k_imp, weights="distance")
    X_full = imputer.fit_transform(X_scaled_nan)
    X_full = np.nan_to_num(X_full, nan=0.0, posinf=0.0, neginf=0.0)

    # -------------------------------------------------------------------------
    # STEP 1B: Dimensionality Reduction — 2D PCA & Non-Linear Manifold ("Market Map")
    # -------------------------------------------------------------------------
    n_pca_comp = min(2, X_full.shape[1], n_samples)
    pca_meta: dict[str, Any] = {
        "pc1_var_pct": 0.0,
        "pc2_var_pct": 0.0,
        "total_var_pct": 0.0,
        "pc1_label": "PC1",
        "pc2_label": "PC2",
        "loadings": [],
        "manifold_method": "UMAP / Spectral k-NN Manifold",
    }
    pca_coords = np.zeros((n_samples, 2), dtype=np.float64)
    manifold_coords = np.zeros((n_samples, 2), dtype=np.float64)

    if n_pca_comp >= 2:
        pca = PCA(n_components=2, random_state=42)
        pca_raw = pca.fit_transform(X_full)
        # Scale PC1 & PC2 to a clean [-3.5, +3.5] display range via robust IQR
        pca_coords = np.clip(RobustScaler().fit_transform(pca_raw), -4.0, 4.0)
        evr = pca.explained_variance_ratio_
        pc1_var = round(float(evr[0]) * 100.0, 1)
        pc2_var = round(float(evr[1]) * 100.0, 1)

        loadings_list: list[dict[str, Any]] = []
        for f_idx, fspec in enumerate(active_feature_specs):
            w1 = float(pca.components_[0, f_idx])
            w2 = float(pca.components_[1, f_idx])
            loadings_list.append({
                "key": fspec["key"],
                "label": fspec["label"],
                "pc1": round(w1, 3),
                "pc2": round(w2, 3),
                "magnitude": round(math.hypot(w1, w2), 3),
            })
        loadings_list.sort(key=lambda x: x["magnitude"], reverse=True)

        # Auto-summarize PC1 and PC2 top positive/negative drivers for axis labels
        pc1_top = sorted(loadings_list, key=lambda x: abs(x["pc1"]), reverse=True)[:2]
        pc2_top = sorted(loadings_list, key=lambda x: abs(x["pc2"]), reverse=True)[:2]
        pc1_desc = " / ".join(f"{'+' if x['pc1'] >= 0 else '-'}{x['label']}" for x in pc1_top)
        pc2_desc = " / ".join(f"{'+' if x['pc2'] >= 0 else '-'}{x['label']}" for x in pc2_top)

        pca_meta = {
            "pc1_var_pct": pc1_var,
            "pc2_var_pct": pc2_var,
            "total_var_pct": round(pc1_var + pc2_var, 1),
            "pc1_label": f"PC1 ({pc1_var}% var: {pc1_desc})",
            "pc2_label": f"PC2 ({pc2_var}% var: {pc2_desc})",
            "loadings": loadings_list,
            "manifold_method": "Non-Linear RBF/k-NN Manifold",
        }

        # Non-linear 2D Manifold Embedding:
        # For smaller universes (<= 1200), fit SpectralEmbedding on the k-NN graph directly.
        # For large global universes (~17,000 stocks), fit Nyström Anchor Spectral Manifold on
        # 500 K-Means / stratified landmarks and project all stocks via RBF kernel weights (<0.3s!).
        try:
            if n_samples <= 1200 and n_samples >= 10:
                spec = SpectralEmbedding(
                    n_components=2,
                    affinity="nearest_neighbors",
                    n_neighbors=min(12, max(3, n_samples // 10)),
                    random_state=42,
                )
                m_raw = spec.fit_transform(X_full)
                manifold_coords = np.clip(RobustScaler().fit_transform(m_raw), -4.0, 4.0)
            else:
                rng = np.random.default_rng(42)
                n_landmarks = min(450, n_samples)
                lm_idx = rng.choice(n_samples, size=n_landmarks, replace=False)
                X_lm = X_full[lm_idx]
                spec = SpectralEmbedding(
                    n_components=2,
                    affinity="nearest_neighbors",
                    n_neighbors=12,
                    random_state=42,
                )
                Y_lm = spec.fit_transform(X_lm)
                # Project all n_samples onto the 2D manifold via soft k-NN kernel interpolation
                nn_lm = NearestNeighbors(n_neighbors=8, metric="euclidean", n_jobs=-1)
                nn_lm.fit(X_lm)
                d_lm, idx_lm = nn_lm.kneighbors(X_full)
                sigma = float(np.median(d_lm)) or 1.0
                weights = np.exp(-((d_lm / sigma) ** 2))
                weights /= np.sum(weights, axis=1, keepdims=True) + 1e-9
                m_raw = np.einsum("ij,ijk->ik", weights, Y_lm[idx_lm])
                manifold_coords = np.clip(RobustScaler().fit_transform(m_raw), -4.0, 4.0)
        except Exception as e:
            logger.debug("Manifold embedding fallback to nonlinear PCA warp: %s", e)
            manifold_coords = np.tanh(pca_coords * 0.85) * 3.0

    # -------------------------------------------------------------------------
    # STEP 2: K-Means Fundamental Archetype Clustering
    # -------------------------------------------------------------------------
    k_clust = max(2, min(int(n_clusters), n_samples // 3))
    kmeans = KMeans(n_clusters=k_clust, n_init=10, random_state=42)
    cluster_ids = kmeans.fit_predict(X_full)

    # Characterize each cluster using average (val_z, qual_z, posture_z, net_cash_pct)
    val_z_arr = np.array([float(profiles[t].get("val_z") or 0.0) for t in tickers], dtype=np.float64)
    qual_z_arr = np.array([float(profiles[t].get("qual_z") or 0.0) for t in tickers], dtype=np.float64)
    post_z_arr = np.array([float(profiles[t].get("posture_z") or 0.0) for t in tickers], dtype=np.float64)
    nc_arr = np.array(
        [float(_extract_ml_feature(s, "net_cash_pct_mcap") or 0.0) for s in valid_stocks],
        dtype=np.float64,
    )

    cluster_summaries: list[dict[str, Any]] = []
    cluster_labels_map: dict[int, str] = {}
    used_labels: dict[str, int] = {}

    for cid in range(k_clust):
        mask = cluster_ids == cid
        cnt = int(np.sum(mask))
        if cnt == 0:
            continue
        m_val = float(np.mean(val_z_arr[mask]))
        m_qual = float(np.mean(qual_z_arr[mask]))
        m_post = float(np.mean(post_z_arr[mask]))
        m_nc = float(np.mean(nc_arr[mask]))
        base_lbl = _name_cluster_archetype(m_val, m_qual, m_post, m_nc)
        if base_lbl in used_labels:
            used_labels[base_lbl] += 1
            lbl = f"{base_lbl} #{used_labels[base_lbl]}"
        else:
            used_labels[base_lbl] = 1
            lbl = base_lbl
        cluster_labels_map[cid] = lbl
        # Sample top tickers in cluster by V/Q score
        member_indices = np.where(mask)[0]
        member_indices_sorted = sorted(
            member_indices,
            key=lambda idx: float(profiles[tickers[idx]].get("vq_score") or 0.0),
            reverse=True,
        )
        cluster_summaries.append(
            {
                "cluster_id": int(cid),
                "label": lbl,
                "count": cnt,
                "avg_val_z": round(m_val, 2),
                "avg_qual_z": round(m_qual, 2),
                "avg_posture_z": round(m_post, 2),
                "avg_net_cash_pct": round(m_nc, 1),
                "top_tickers": [tickers[idx] for idx in member_indices_sorted[:6]],
            }
        )

    # -------------------------------------------------------------------------
    # STEP 3: k-Nearest Neighbors ("True Mathematical Comps") & Peer Valuation Discount
    # -------------------------------------------------------------------------
    n_knn_feats = len(KNN_PEER_FEATURES)
    X_knn_raw = np.full((n_samples, n_knn_feats), np.nan, dtype=np.float64)
    knn_weights = np.array([float(f["weight"]) for f in KNN_PEER_FEATURES], dtype=np.float64)

    for i, s in enumerate(valid_stocks):
        for j, fspec in enumerate(KNN_PEER_FEATURES):
            v = _extract_ml_feature(s, fspec["key"])
            if v is not None and math.isfinite(v):
                if fspec["key"] in ("debt_to_equity", "pct_from_52w_low"):
                    v = math.log1p(max(0.0, v))
                X_knn_raw[i, j] = v

    knn_valid_cols = ~np.all(np.isnan(X_knn_raw), axis=0)
    X_knn_active = X_knn_raw[:, knn_valid_cols]
    active_weights = knn_weights[knn_valid_cols]

    scaler_knn = RobustScaler(quantile_range=(25.0, 75.0))
    X_knn_scaled = np.clip(scaler_knn.fit_transform(X_knn_active), -3.5, 3.5)
    if n_samples > 2500:
        X_knn_imputed = SimpleImputer(strategy="median").fit_transform(X_knn_scaled)
    else:
        X_knn_imputed = KNNImputer(n_neighbors=k_imp, weights="distance").fit_transform(X_knn_scaled)
    X_knn_weighted = np.nan_to_num(X_knn_imputed, nan=0.0) * active_weights

    # Normalize each row vector with a soft L2 Anchor (sqrt(1 + ||x||^2)) so cosine-like
    # directional alignment and magnitude similarity combine smoothly, yielding high
    # and well-calibrated 0-100% similarity scores across the full universe.
    row_norms = np.sqrt(1.0 + np.sum(X_knn_weighted ** 2, axis=1, keepdims=True))
    X_knn_normed = X_knn_weighted / row_norms

    # Request extra candidates so we can filter out dual-listed / identical-company tickers
    k_peers = min(int(n_neighbors), n_samples - 1)
    k_query = min(n_samples, k_peers + 8)
    nn_model = NearestNeighbors(n_neighbors=k_query, metric="euclidean", n_jobs=-1)
    nn_model.fit(X_knn_normed)
    knn_dists, knn_indices = nn_model.kneighbors(X_knn_normed)

    # Characteristic 90th-percentile distance scale for intuitive 0-100% similarity calibration
    scale_dist = float(np.percentile(knn_dists[:, 1:], 90)) if knn_dists.shape[1] > 1 else 1.0
    if scale_dist <= 1e-6:
        scale_dist = 1.0

    # -------------------------------------------------------------------------
    # STEP 4: Anomaly Detection (Isolation Forest + Mahalanobis + Huber Value-vs-Quality Residual)
    # -------------------------------------------------------------------------
    # 4a. Isolation Forest in joint feature space
    iso = IsolationForest(
        n_estimators=150,
        contamination=min(0.25, max(0.02, float(contamination))),
        random_state=42,
    )
    iso_preds = iso.fit_predict(X_full)  # -1 = outlier, +1 = inlier
    # decision_function: lower (more negative) = more anomalous. Invert so higher = more anomalous.
    iso_raw = -iso.decision_function(X_full)
    iso_min, iso_max = float(np.min(iso_raw)), float(np.max(iso_raw))
    if iso_max > iso_min + 1e-9:
        iso_score_0_100 = 100.0 * (iso_raw - iso_min) / (iso_max - iso_min)
    else:
        iso_score_0_100 = np.full(n_samples, 50.0)

    # 4b. Robust Mahalanobis distance in (val_z, qual_z, post_z) 3D factor space
    F_3d = np.column_stack([val_z_arr, qual_z_arr, post_z_arr])
    cov = np.cov(F_3d, rowvar=False) + np.eye(3) * 0.05
    try:
        inv_cov = np.linalg.inv(cov)
    except np.linalg.LinAlgError:
        inv_cov = np.eye(3)
    center = np.median(F_3d, axis=0)
    diff = F_3d - center
    mahal_sq = np.einsum("ij,jk,ik->i", diff, inv_cov, diff)
    mahal_dist = np.sqrt(np.maximum(0.0, mahal_sq))

    # 4c. Huber Robust Regression: Valuation Z ~ Quality Z + Posture Z
    # A positive residual means the stock is MUCH CHEAPER (`val_z` higher) than warranted by its Quality!
    try:
        huber = HuberRegressor(max_iter=200)
        X_reg = np.column_stack([qual_z_arr, post_z_arr])
        huber.fit(X_reg, val_z_arr)
        expected_val_z = huber.predict(X_reg)
        val_residual_z = val_z_arr - expected_val_z
    except Exception:
        val_residual_z = val_z_arr + 0.5 * qual_z_arr

    # Combine into "Mispriced Quality Outlier Score" (0–100):
    # High when:
    # 1. Stock is cheaper than expected for its quality (`val_residual_z > 0`)
    # 2. Both Value and Quality are constructive (`val_z + qual_z > 0`)
    # 3. Stock is statistically unusual in joint space (`iso_score` / `mahal_dist`) or cheaper than k-NN peers
    ml_profiles: dict[str, dict[str, Any]] = {}
    outlier_rows: list[dict[str, Any]] = []

    def _norm_company_stem(ticker_str: str, name_str: str) -> tuple[str, str]:
        base_t = ticker_str.split(".")[0].upper()
        cleaned = re.sub(
            r"\b(limited|ltd|inc|corp|corporation|plc|group|holdings|holding|fpo|adr|co|company|the|sa|ag|nv|ab)\b",
            "",
            name_str.lower(),
        )
        cleaned = re.sub(r"\[.*?\]", "", cleaned)
        cleaned = re.sub(r"[^a-z0-9]", "", cleaned)
        return base_t, cleaned[:14]

    stems = [_norm_company_stem(tickers[idx], names[idx]) for idx in range(n_samples)]

    for i, s in enumerate(valid_stocks):
        t = tickers[i]
        my_base_t, my_name_stem = stems[i]
        cid = int(cluster_ids[i])
        clabel = cluster_labels_map.get(cid, f"Cluster #{cid}")

        # Build k-NN peer list (skipping self and dual-listed / identical-company tickers)
        peer_list: list[dict[str, Any]] = []
        peer_val_zs: list[float] = []
        seen_stems: set[str] = {my_name_stem} if len(my_name_stem) >= 5 else set()
        for rank_idx in range(1, knn_indices.shape[1]):
            p_idx = int(knn_indices[i, rank_idx])
            if p_idx == i:
                continue
            p_base_t, p_name_stem = stems[p_idx]
            if p_base_t == my_base_t:
                continue
            if len(p_name_stem) >= 5 and p_name_stem in seen_stems:
                continue
            if len(p_name_stem) >= 5:
                seen_stems.add(p_name_stem)
            p_ticker = tickers[p_idx]
            dist = float(knn_dists[i, rank_idx])
            # Smooth Gaussian-like similarity kernel calibrated so close comps score 80-98%
            sim_pct = round(100.0 * math.exp(-0.45 * ((dist / scale_dist) ** 1.35)), 1)
            p_prof = profiles.get(p_ticker) or {}
            p_val_z = p_prof.get("val_z")
            if p_val_z is not None:
                peer_val_zs.append(float(p_val_z))
            peer_list.append(
                {
                    "ticker": p_ticker,
                    "name": names[p_idx],
                    "sector": sectors[p_idx],
                    "similarity_pct": sim_pct,
                    "distance": round(dist, 3),
                    "vq_score": p_prof.get("vq_score"),
                    "val_z": p_val_z,
                    "qual_z": p_prof.get("qual_z"),
                }
            )
            if len(peer_list) >= k_peers:
                break

        my_val_z = float(val_z_arr[i])
        my_qual_z = float(qual_z_arr[i])
        my_post_z = float(post_z_arr[i])
        peer_mean_val_z = float(np.mean(peer_val_zs)) if peer_val_zs else 0.0
        # Positive `peer_val_diff_z` = this stock is CHEAPER than its closest fundamental k-NN peers
        peer_val_diff_z = round(my_val_z - peer_mean_val_z, 2)

        anom_score = round(float(iso_score_0_100[i]), 1)
        m_dist = round(float(mahal_dist[i]), 2)
        res_z = round(float(val_residual_z[i]), 2)

        # Directional mispricing signal (in sigma units):
        # Rewards: positive regression residual + positive quality + discount vs k-NN comps + anomaly magnitude
        directional_sigma = (
            0.40 * res_z
            + 0.30 * my_qual_z
            + 0.20 * peer_val_diff_z
            + 0.10 * (m_dist - 1.2)
        )
        # Penalize if quality or valuation is deeply negative (so "junk value traps" don't rank as mispriced quality)
        if my_qual_z < -0.25:
            directional_sigma -= abs(my_qual_z) * 0.6
        if my_val_z < -0.25:
            directional_sigma -= abs(my_val_z) * 0.6

        mispriced_score = round(100.0 / (1.0 + math.exp(-1.35 * directional_sigma)), 1)
        is_mispriced_outlier = bool(
            mispriced_score >= 70.0
            and my_val_z >= 0.0
            and my_qual_z >= 0.0
            and (res_z >= 0.40 or peer_val_diff_z >= 0.40 or iso_preds[i] == -1)
        )

        rec = {
            "ticker": t,
            "name": names[i],
            "sector": sectors[i],
            "cluster_id": cid,
            "cluster_label": clabel,
            "anomaly_score": anom_score,
            "is_iso_outlier": bool(iso_preds[i] == -1),
            "mahalanobis_dist": m_dist,
            "val_residual_z": res_z,
            "peer_val_diff_z": peer_val_diff_z,
            "mispriced_outlier_score": mispriced_score,
            "is_mispriced_outlier": is_mispriced_outlier,
            "val_z": round(my_val_z, 2),
            "qual_z": round(my_qual_z, 2),
            "posture_z": round(my_post_z, 2),
            "vq_score": profiles[t].get("vq_score"),
            "pca_x": round(float(pca_coords[i, 0]), 3),
            "pca_y": round(float(pca_coords[i, 1]), 3),
            "map_x": round(float(manifold_coords[i, 0]), 3),
            "map_y": round(float(manifold_coords[i, 1]), 3),
            "knn_peers": peer_list,
        }
        ml_profiles[t] = rec
        if is_mispriced_outlier or mispriced_score >= 65.0:
            outlier_rows.append(rec)

    outlier_rows.sort(
        key=lambda r: (r["is_mispriced_outlier"], r["mispriced_outlier_score"], r["val_residual_z"]),
        reverse=True,
    )

    return {
        "total_stocks": len(stocks or []),
        "modeled_stocks": n_samples,
        "n_clusters": k_clust,
        "n_neighbors": k_peers,
        "pca_meta": pca_meta,
        "clusters": sorted(cluster_summaries, key=lambda c: -c["count"]),
        "top_mispriced_outliers": outlier_rows[:30],
        "profiles": ml_profiles,
    }


# =========================================================================
# GLOBAL FULL-DATASET PERSISTENT MODEL & WEEKLY BACKGROUND SCHEDULER
# =========================================================================
ML_WEEKLY_MAX_AGE_DAYS = 7
_ML_CACHE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS ml_global_meta (
        scope TEXT PRIMARY KEY,
        computed_at TEXT,
        total_stocks INTEGER,
        modeled_stocks INTEGER,
        n_clusters INTEGER,
        n_neighbors INTEGER,
        clusters_json TEXT,
        top_outliers_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ml_global_profiles (
        ticker TEXT PRIMARY KEY,
        profile_json TEXT
    )
    """,
)

_ml_mem_cache: dict[str, Any] = {}
_ml_job_lock = threading.Lock()
_ml_job_status: dict[str, Any] = {
    "running": False,
    "last_started": None,
    "last_completed": None,
    "last_error": None,
    "modeled_stocks": 0,
}
_scheduler_started = False


def _ml_conn(db_path: str = "asx_announcements_cache.db"):
    from data.db import connect_db

    return connect_db(db_path, schema_key="ml_global", ddl=_ML_CACHE_DDL)


def load_full_global_stock_universe(db_path: str = "asx_announcements_cache.db") -> list[dict[str, Any]]:
    """
    Load the entire cached stock universe across all business lists, global buyback
    registries, US indices, and the ASX universe directly from SQLite (`business_list_yf_cache`
    + `asx_yf_cache`) so k-NN mathematical peers, K-Means archetypes, and Isolation Forest
    anomalies are always fit on the FULL dataset regardless of active UI filters or lists.
    """
    from data.asx import infer_currency
    from data.yf_helpers import pct_from_52w_low, reconcile_52w_range

    conn = _ml_conn(db_path)
    rows: dict[str, dict[str, Any]] = {}
    try:
        # 1. Load all active entries from business_list_yf_cache (~16,700 global stocks)
        cur = conn.execute(
            """
            SELECT yf_symbol, price, low_52w, high_52w, name, sector, metrics_json, p_fcf
            FROM business_list_yf_cache
            WHERE price IS NOT NULL AND price > 0
            """
        )
        for sym, price, low_52w, high_52w, name, sector, mj, p_fcf in cur.fetchall():
            if not sym:
                continue
            t = str(sym).strip().upper()
            try:
                m = json.loads(mj) if mj else {}
            except Exception:
                m = {}
            p = float(price or 0.0)
            low, high = reconcile_52w_range(p, float(low_52w or 0.0), float(high_52w or 0.0))
            pf = float(p_fcf or m.get("p_fcf") or 0.0)
            ccy = m.get("currency") or infer_currency(ticker=t)
            rows[t] = {
                "ticker": t,
                "name": name or t,
                "sector": sector or "Unknown",
                "current_price": p,
                "low_52w": low,
                "high_52w": high,
                "pct_from_52w_low": pct_from_52w_low(p, low),
                "pct_from_200w_ma": m.get("pct_from_200w_ma", 0),
                "rsi": m.get("rsi"),
                "market_cap": m.get("market_cap", 0),
                "currency": ccy,
                "pe": m.get("pe", 0),
                "pb": m.get("pb", 0),
                "p_fcf": pf,
                "net_cash_pct_mcap": m.get("net_cash_pct_mcap", 0),
                "net_assets_vs_mcap_pct": m.get("net_assets_vs_mcap_pct", 0),
                "annual_buyback_pct": m.get("annual_buyback_pct", 0),
                "metrics": m,
            }

        # 2. Merge any additional ASX universe stocks from asx_yf_cache (~1,750 stocks)
        cur_asx = conn.execute(
            """
            SELECT ticker, price, low_52w, high_52w, name, sector, metrics_json, p_fcf
            FROM asx_yf_cache
            WHERE price IS NOT NULL AND price > 0
            """
        )
        for base_t, price, low_52w, high_52w, name, sector, mj, p_fcf in cur_asx.fetchall():
            if not base_t:
                continue
            bt = str(base_t).strip().upper()
            t = bt if bt.endswith(".AX") else f"{bt}.AX"
            if t in rows:
                continue
            try:
                m = json.loads(mj) if mj else {}
            except Exception:
                m = {}
            p = float(price or 0.0)
            low, high = reconcile_52w_range(p, float(low_52w or 0.0), float(high_52w or 0.0))
            pf = float(p_fcf or m.get("p_fcf") or 0.0)
            rows[t] = {
                "ticker": t,
                "name": name or t,
                "sector": sector or "Unknown",
                "current_price": p,
                "low_52w": low,
                "high_52w": high,
                "pct_from_52w_low": pct_from_52w_low(p, low),
                "pct_from_200w_ma": m.get("pct_from_200w_ma", 0),
                "rsi": m.get("rsi"),
                "market_cap": m.get("market_cap", 0),
                "currency": "AUD",
                "pe": m.get("pe", 0),
                "pb": m.get("pb", 0),
                "p_fcf": pf,
                "net_cash_pct_mcap": m.get("net_cash_pct_mcap", 0),
                "net_assets_vs_mcap_pct": m.get("net_assets_vs_mcap_pct", 0),
                "annual_buyback_pct": m.get("annual_buyback_pct", 0),
                "metrics": m,
            }
    except Exception as e:
        logger.warning("load_full_global_stock_universe partial error: %s", e)
    finally:
        conn.close()

    return list(rows.values())


def _is_ml_cache_fresh(computed_at_iso: str | None, max_age_days: float = ML_WEEKLY_MAX_AGE_DAYS) -> bool:
    if not computed_at_iso:
        return False
    try:
        dt = datetime.fromisoformat(str(computed_at_iso).replace("Z", ""))
        return (datetime.now() - dt) < timedelta(days=max_age_days)
    except Exception:
        return False


def load_persisted_global_ml(db_path: str = "asx_announcements_cache.db") -> dict[str, Any] | None:
    """Load the persisted full-dataset ML run from SQLite (or in-memory cache)."""
    if _ml_mem_cache.get("payload"):
        return _ml_mem_cache["payload"]

    conn = _ml_conn(db_path)
    try:
        row = conn.execute(
            """
            SELECT computed_at, total_stocks, modeled_stocks, n_clusters, n_neighbors,
                   clusters_json, top_outliers_json
            FROM ml_global_meta
            WHERE scope = 'global'
            """
        ).fetchone()
        if not row:
            return None
        computed_at, total_s, modeled_s, n_cl, n_nb, cl_json, top_json = row
        raw_clusters = json.loads(cl_json) if cl_json else []
        if isinstance(raw_clusters, dict):
            clusters = raw_clusters.get("clusters", [])
            pca_meta = raw_clusters.get("pca_meta", {})
        else:
            clusters = raw_clusters
            pca_meta = {}
        top_outliers = json.loads(top_json) if top_json else []

        profiles: dict[str, dict[str, Any]] = {}
        for t, p_json in conn.execute("SELECT ticker, profile_json FROM ml_global_profiles").fetchall():
            try:
                prof = json.loads(p_json)
                profiles[t] = prof
                # Also index base ASX symbol without .AX for seamless lookup on ASX tab
                if t.endswith(".AX"):
                    base = t[:-3]
                    if base not in profiles:
                        profiles[base] = prof
            except Exception:
                continue

        payload = {
            "scope": "global",
            "computed_at": computed_at,
            "is_fresh": _is_ml_cache_fresh(computed_at),
            "next_scheduled_refresh": _next_refresh_iso(computed_at),
            "total_stocks": int(total_s or 0),
            "modeled_stocks": int(modeled_s or 0),
            "n_clusters": int(n_cl or 6),
            "n_neighbors": int(n_nb or 5),
            "pca_meta": pca_meta,
            "clusters": clusters,
            "top_mispriced_outliers": top_outliers,
            "profiles": profiles,
        }
        _ml_mem_cache["payload"] = payload
        return payload
    except Exception as e:
        logger.warning("load_persisted_global_ml failed: %s", e)
        return None
    finally:
        conn.close()


def _next_refresh_iso(computed_at_iso: str | None) -> str | None:
    if not computed_at_iso:
        return None
    try:
        dt = datetime.fromisoformat(str(computed_at_iso).replace("Z", ""))
        return (dt + timedelta(days=ML_WEEKLY_MAX_AGE_DAYS)).isoformat(timespec="seconds")
    except Exception:
        return None


def rebuild_global_unsupervised_ml(
    *,
    n_neighbors: int = 5,
    n_clusters: int = 6,
    contamination: float = 0.08,
    db_path: str = "asx_announcements_cache.db",
) -> dict[str, Any]:
    """
    Fit K-Means, k-NN Comps, Isolation Forest, and Huber Regression across the
    ENTIRE global cached stock universe (~17,000+ stocks) and persist to SQLite.
    Safe to call from a background thread or API trigger.
    """
    if not _ml_job_lock.acquire(blocking=False):
        return {"status": "already_running", "job": get_ml_job_status()}

    _ml_job_status["running"] = True
    _ml_job_status["last_started"] = datetime.now().isoformat(timespec="seconds")
    _ml_job_status["last_error"] = None
    t0 = time.time()
    try:
        global_stocks = load_full_global_stock_universe(db_path=db_path)
        logger.info(
            "Starting global Unsupervised ML rebuild across %d cached stocks...",
            len(global_stocks),
        )
        res = run_unsupervised_analysis(
            global_stocks,
            n_neighbors=n_neighbors,
            n_clusters=n_clusters,
            contamination=contamination,
        )
        now_iso = datetime.now().isoformat(timespec="seconds")
        profiles = res.get("profiles") or {}

        conn = _ml_conn(db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO ml_global_meta
                    (scope, computed_at, total_stocks, modeled_stocks, n_clusters, n_neighbors, clusters_json, top_outliers_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "global",
                        now_iso,
                        res.get("total_stocks", 0),
                        res.get("modeled_stocks", 0),
                        res.get("n_clusters", n_clusters),
                        res.get("n_neighbors", n_neighbors),
                        json.dumps({
                            "clusters": res.get("clusters", []),
                            "pca_meta": res.get("pca_meta", {}),
                        }),
                        json.dumps(res.get("top_mispriced_outliers", [])),
                    ),
                )
                conn.execute("DELETE FROM ml_global_profiles")
                conn.executemany(
                    "INSERT OR REPLACE INTO ml_global_profiles (ticker, profile_json) VALUES (?, ?)",
                    [(t, json.dumps(prof)) for t, prof in profiles.items()],
                )
        finally:
            conn.close()

        # Also index base ASX symbols in memory for O(1) lookup
        expanded_profiles = dict(profiles)
        for t, prof in profiles.items():
            if t.endswith(".AX"):
                base = t[:-3]
                if base not in expanded_profiles:
                    expanded_profiles[base] = prof

        payload = {
            "scope": "global",
            "computed_at": now_iso,
            "is_fresh": True,
            "next_scheduled_refresh": _next_refresh_iso(now_iso),
            "total_stocks": res.get("total_stocks", 0),
            "modeled_stocks": res.get("modeled_stocks", 0),
            "n_clusters": res.get("n_clusters", n_clusters),
            "n_neighbors": res.get("n_neighbors", n_neighbors),
            "pca_meta": res.get("pca_meta", {}),
            "clusters": res.get("clusters", []),
            "top_mispriced_outliers": res.get("top_mispriced_outliers", []),
            "profiles": expanded_profiles,
        }
        _ml_mem_cache["payload"] = payload
        elapsed = round(time.time() - t0, 2)
        _ml_job_status["last_completed"] = now_iso
        _ml_job_status["modeled_stocks"] = payload["modeled_stocks"]
        logger.info(
            "Global Unsupervised ML rebuild completed in %.2fs (%d stocks modeled).",
            elapsed,
            payload["modeled_stocks"],
        )
        # Ensure the current calendar month's point-in-time snapshot is recorded / forward returns updated
        try:
            from scoring.historical_snapshots import (
                capture_monthly_snapshot,
                has_snapshot_for_month,
                update_forward_returns_on_snapshots,
            )

            if not has_snapshot_for_month(db_path=db_path):
                capture_monthly_snapshot(force=False, db_path=db_path)
            else:
                update_forward_returns_on_snapshots(db_path=db_path)
        except Exception as snap_err:
            logger.debug("Post-ML monthly snapshot hook skipped: %s", snap_err)

        return payload
    except Exception as e:
        _ml_job_status["last_error"] = str(e)
        logger.exception("Global Unsupervised ML rebuild failed")
        raise
    finally:
        _ml_job_status["running"] = False
        _ml_job_lock.release()


def get_ml_job_status(db_path: str = "asx_announcements_cache.db") -> dict[str, Any]:
    persisted = load_persisted_global_ml(db_path=db_path)
    return {
        "running": bool(_ml_job_status["running"]),
        "last_started": _ml_job_status["last_started"],
        "last_completed": _ml_job_status["last_completed"] or (persisted and persisted.get("computed_at")),
        "last_error": _ml_job_status["last_error"],
        "modeled_stocks": (persisted and persisted.get("modeled_stocks")) or _ml_job_status["modeled_stocks"],
        "total_stocks": (persisted and persisted.get("total_stocks")) or 0,
        "is_fresh": bool(persisted and persisted.get("is_fresh")),
        "next_scheduled_refresh": persisted and persisted.get("next_scheduled_refresh"),
        "schedule_interval_days": ML_WEEKLY_MAX_AGE_DAYS,
    }


def trigger_global_ml_rebuild_async(db_path: str = "asx_announcements_cache.db") -> bool:
    """Launch `rebuild_global_unsupervised_ml` in a background daemon thread if not already running."""
    if _ml_job_status["running"]:
        return False

    def _bg():
        try:
            rebuild_global_unsupervised_ml(db_path=db_path)
        except Exception:
            pass

    threading.Thread(target=_bg, name="ml-global-rebuild", daemon=True).start()
    return True


def start_weekly_ml_scheduler(db_path: str = "asx_announcements_cache.db") -> None:
    """
    Start a background daemon scheduler that checks once every 6 hours (and on startup)
    whether the persisted global ML model is missing or older than 7 days (`ML_WEEKLY_MAX_AGE_DAYS`),
    and automatically rebuilds it in the background.
    """
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def _loop():
        # Brief initial delay so Flask startup is instantaneous
        time.sleep(1.5)
        while True:
            try:
                persisted = load_persisted_global_ml(db_path=db_path)
                if not persisted or not _is_ml_cache_fresh(persisted.get("computed_at")):
                    logger.info("Weekly Global ML Scheduler: cache missing or >7 days old — triggering background rebuild.")
                    rebuild_global_unsupervised_ml(db_path=db_path)
            except Exception as e:
                logger.warning("Weekly Global ML Scheduler check failed: %s", e)
            # Sleep 6 hours between checks
            time.sleep(6 * 3600)

    threading.Thread(target=_loop, name="ml-weekly-scheduler", daemon=True).start()

