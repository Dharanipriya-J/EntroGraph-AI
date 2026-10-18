"""Behavioural metrics: Shannon entropy, normalised entropy, Jensen-Shannon divergence, drift.

All functions are pure (no database access) so they are easy to test.
Logarithms are base 2, so entropy is in bits and JS divergence lies in [0, 1].
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

import numpy as np

from .config import RiskConfig

Counts = Mapping[str, float] | Sequence[float]


def _as_array(counts: Counts) -> np.ndarray:
    values = list(counts.values()) if isinstance(counts, Mapping) else list(counts)
    arr = np.asarray(values, dtype=float)
    if arr.size and (np.any(arr < 0) or not np.all(np.isfinite(arr))):
        raise ValueError("counts must be finite and non-negative")
    return arr


def shannon_entropy(counts: Counts) -> float:
    """H = -sum(p_i * log2 p_i), in bits. Empty or all-zero input -> 0.0 (no information)."""
    arr = _as_array(counts)
    total = arr.sum() if arr.size else 0.0
    if total <= 0:
        return 0.0
    p = arr[arr > 0] / total
    h = float(-(p * np.log2(p)).sum())
    return max(0.0, h)  # guard against -0.0


def normalized_entropy(counts: Counts, n_categories: int | None = None) -> float:
    """H / log2(K) in [0, 1].

    K is `n_categories` when given (a fixed universe makes customers comparable),
    otherwise the number of observed categories. If K <= 1 the value is defined as 0.0
    (a single category carries no uncertainty); empty input is also 0.0.
    """
    arr = _as_array(counts)
    k = n_categories if n_categories is not None else int((arr > 0).sum())
    if k is None or k <= 1 or arr.sum() <= 0:
        return 0.0
    observed = int((arr > 0).sum())
    if observed > k:
        raise ValueError("n_categories is smaller than the number of observed categories")
    return min(1.0, shannon_entropy(arr) / math.log2(k))


def jensen_shannon_divergence(p: Counts, q: Counts) -> float:
    """JSD(P||Q) = H(M) - (H(P)+H(Q))/2 with M=(P+Q)/2, base 2, in [0, 1].

    `p` and `q` are count/probability vectors over the SAME ordered categories.
    Raises ValueError for different lengths, negative values or an all-zero vector
    (the divergence is undefined without data on both sides).
    """
    a, b = _as_array(p), _as_array(q)
    if a.shape != b.shape:
        raise ValueError("p and q must have the same length")
    if a.sum() <= 0 or b.sum() <= 0:
        raise ValueError("p and q must each contain at least one observation")
    a, b = a / a.sum(), b / b.sum()
    m = 0.5 * (a + b)
    jsd = shannon_entropy(m) - 0.5 * (shannon_entropy(a) + shannon_entropy(b))
    return float(min(1.0, max(0.0, jsd)))


def js_between_counters(base: Mapping[str, float], recent: Mapping[str, float]) -> float:
    """JSD between two label->count mappings (aligned over the union of labels)."""
    keys = sorted(set(base) | set(recent))
    return jensen_shannon_divergence([base.get(k, 0) for k in keys], [recent.get(k, 0) for k in keys])


def hour_bucket(ts: int) -> str:
    h = datetime.fromtimestamp(ts, tz=timezone.utc).hour
    return ("night", "morning", "afternoon", "evening")[h // 6]


@dataclass
class DimensionStats:
    entropy_bits: float
    normalized_entropy: float
    n_events: int
    distribution: dict[str, float]


@dataclass
class BehaviorMetrics:
    status: str  # ok | insufficient_baseline | insufficient_recent
    baseline_events: int
    recent_events: int
    category_baseline: DimensionStats
    category_recent: DimensionStats
    hour_baseline: DimensionStats
    hour_recent: DimensionStats
    drift_category: float | None
    drift_hour: float | None
    drift_composite: float | None
    entropy_shift: float | None  # signed: recent - baseline normalised category entropy
    new_devices_recent: list[str] = field(default_factory=list)
    recent_coupon_orders: int = 0
    notes: list[str] = field(default_factory=list)


def _stats(counter: Counter, universe: int) -> DimensionStats:
    total = sum(counter.values())
    return DimensionStats(
        entropy_bits=round(shannon_entropy(counter), 6),
        normalized_entropy=round(normalized_entropy(counter, max(universe, len(counter))), 6),
        n_events=int(total),
        distribution={k: round(v / total, 4) for k, v in sorted(counter.items())} if total else {},
    )


def compute_behavior_metrics(
    events: Sequence[Mapping],
    sessions: Sequence[Mapping],
    orders: Sequence[Mapping],
    ref_ts: int,
    cfg: RiskConfig,
) -> BehaviorMetrics:
    """Split the customer's own events at `ref_ts - recent_days` and compare the two halves.

    Only 'category' behaviour (viewed/bought product categories) and time-of-day buckets are
    used. High entropy by itself is NEVER scored - only the CHANGE in entropy and the drift.
    """
    cutoff = ref_ts - cfg.recent_days * 86400
    base_cat, rec_cat, base_hr, rec_hr = Counter(), Counter(), Counter(), Counter()
    for e in events:
        cat, ts = e.get("category"), int(e["ts"])
        if not cat:
            continue
        (rec_cat if ts > cutoff else base_cat)[cat] += 1
        (rec_hr if ts > cutoff else base_hr)[hour_bucket(ts)] += 1
    cat_b, cat_r = _stats(base_cat, cfg.category_universe_size), _stats(rec_cat, cfg.category_universe_size)
    hr_b, hr_r = _stats(base_hr, cfg.hour_universe_size), _stats(rec_hr, cfg.hour_universe_size)
    nb, nr = sum(base_cat.values()), sum(rec_cat.values())

    status, notes = "ok", []
    d_cat = d_hr = d_comp = shift = None
    if nb < cfg.min_baseline_events:
        status = "insufficient_baseline"
        notes.append(f"only {nb} baseline events (< {cfg.min_baseline_events}); drift not computed")
    elif nr < cfg.min_recent_events:
        status = "insufficient_recent"
        notes.append(f"only {nr} recent events (< {cfg.min_recent_events}); drift not computed")
    else:
        d_cat = js_between_counters(base_cat, rec_cat)
        d_hr = js_between_counters(base_hr, rec_hr)
        wsum = cfg.drift_weight_category + cfg.drift_weight_hour
        d_comp = (cfg.drift_weight_category * d_cat + cfg.drift_weight_hour * d_hr) / wsum
        shift = cat_r.normalized_entropy - cat_b.normalized_entropy

    base_dev = {s["device_id"] for s in sessions if s.get("device_id") and int(s["started_at"]) <= cutoff}
    rec_dev = {s["device_id"] for s in sessions if s.get("device_id") and int(s["started_at"]) > cutoff}
    new_dev = sorted(rec_dev - base_dev) if base_dev else []
    coupons = sum(1 for o in orders if o.get("coupon") and int(o["placed_at"]) > cutoff)
    return BehaviorMetrics(
        status=status, baseline_events=nb, recent_events=nr,
        category_baseline=cat_b, category_recent=cat_r, hour_baseline=hr_b, hour_recent=hr_r,
        drift_category=None if d_cat is None else round(d_cat, 6),
        drift_hour=None if d_hr is None else round(d_hr, 6),
        drift_composite=None if d_comp is None else round(d_comp, 6),
        entropy_shift=None if shift is None else round(shift, 6),
        new_devices_recent=new_dev, recent_coupon_orders=coupons, notes=notes,
    )


def auc_from_scores(scores: Iterable[float], labels: Iterable[int]) -> float | None:
    """Rank-based ROC-AUC (ties count 0.5). None if only one class is present."""
    s, y = np.asarray(list(scores), float), np.asarray(list(labels), int)
    pos, neg = s[y == 1], s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    wins = (pos[:, None] > neg[None, :]).sum() + 0.5 * (pos[:, None] == neg[None, :]).sum()
    return float(wins / (len(pos) * len(neg)))
