"""Scoring.

Phase 1: momentum score (0-100) and the basic liquidity/volume/age filters.
Phase 3 adds social velocity and safety margin into a composite score.

Momentum is about *acceleration*, not size:
  * volume_acceleration - last-5-minute volume vs. its recent baseline
  * buy_pressure        - share of transactions that are buys (5m and 1h)
  * price_trend         - price up over both 5m and 1h
and a penalty when the move looks like one giant candle rather than a climb.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Filters, MomentumSettings
from .models import Snapshot


@dataclass
class MomentumResult:
    score: float                                  # 0-100
    parts: dict[str, float]                       # each 0-100
    volume_ratio: float | None                    # 5m volume / baseline 5m volume
    baseline_source: str                          # "history" / "1h" / "6h" / "none"
    buy_ratio_m5: float | None
    buy_ratio_h1: float | None
    giant_candle: bool
    signals: list[str] = field(default_factory=list)


@dataclass
class FilterResult:
    passed: bool
    reasons: list[str]


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _ratio(buys: int | None, sells: int | None) -> float | None:
    if buys is None or sells is None or buys + sells == 0:
        return None
    return buys / (buys + sells)


def baseline_volume_5m(snap: Snapshot, history: list[Snapshot],
                       min_history_points: int = 3) -> tuple[float | None, str]:
    """Typical 5-minute volume before now.

    Prefer our own stored history (average of earlier 5m-volume readings);
    otherwise estimate from the pair's 1h or 6h volume.
    """
    past = [h.volume_m5 for h in history if h.volume_m5 is not None]
    if len(past) >= min_history_points:
        avg = sum(past) / len(past)
        if avg > 0:
            return avg, "history"
    if snap.volume_h1 and snap.volume_m5 is not None:
        # The 1h window includes the latest 5m; exclude it to avoid dilution.
        rest = snap.volume_h1 - snap.volume_m5
        if rest > 0:
            return rest / 11.0, "1h"
    if snap.volume_h6:
        return snap.volume_h6 / 72.0, "6h"
    return None, "none"


def momentum_score(snap: Snapshot, history: list[Snapshot], m: MomentumSettings) -> MomentumResult:
    signals: list[str] = []

    # --- volume acceleration
    baseline, baseline_src = baseline_volume_5m(snap, history)
    vol_ratio = None
    vol_part = 0.0
    if baseline and snap.volume_m5 is not None:
        vol_ratio = snap.volume_m5 / baseline
        full = m.volume_acceleration_full_score_ratio
        vol_part = _clamp01((vol_ratio - 1.0) / (full - 1.0)) * 100
        if vol_ratio >= 2.0:
            signals.append("volume_spike")

    # --- buy pressure (5m weighted more than 1h)
    r5 = _ratio(snap.buys_m5, snap.sells_m5)
    r1 = _ratio(snap.buys_h1, snap.sells_h1)
    span = m.buy_ratio_full_score - 0.5

    def _bp(r: float | None) -> float | None:
        return None if r is None else _clamp01((r - 0.5) / span) * 100

    b5, b1 = _bp(r5), _bp(r1)
    if b5 is not None and b1 is not None:
        buy_part = 0.65 * b5 + 0.35 * b1
    else:
        buy_part = b5 if b5 is not None else (b1 if b1 is not None else 0.0)
    if r5 is not None and r5 >= 0.6:
        signals.append("buy_pressure")

    # --- price trend: must be up on both 5m and 1h
    pc5 = snap.price_change_m5
    pc1 = snap.price_change_h1
    price_part = 0.0
    if pc1 is not None and pc1 > 0:
        price_part = _clamp01(pc1 / m.price_change_1h_full_score_pct) * 100
        if pc5 is None or pc5 <= 0:
            price_part *= 0.5
        elif pc5 > 0:
            signals.append("price_uptrend")

    # --- giant candle guard
    giant = False
    if pc5 is not None and pc5 > 0:
        if pc5 >= m.giant_candle_pct_5m:
            giant = True
        elif pc1 is not None and pc1 > 5 and pc5 / pc1 >= m.giant_candle_share_of_1h:
            giant = True

    parts = {"volume_acceleration": vol_part, "buy_pressure": buy_part, "price_trend": price_part}
    total_w = sum(m.weights.values())
    score = sum(parts[k] * m.weights[k] for k in parts) / total_w
    if giant:
        score *= m.giant_candle_penalty
        signals.append("giant_candle")

    return MomentumResult(
        score=round(score, 1),
        parts={k: round(v, 1) for k, v in parts.items()},
        volume_ratio=vol_ratio,
        baseline_source=baseline_src,
        buy_ratio_m5=r5,
        buy_ratio_h1=r1,
        giant_candle=giant,
        signals=signals,
    )


def basic_filters(snap: Snapshot, f: Filters, now: float | None = None) -> FilterResult:
    reasons: list[str] = []
    if snap.liquidity_usd is None:
        reasons.append("liquidity unknown")
    elif snap.liquidity_usd < f.min_liquidity_usd:
        reasons.append(f"liquidity ${snap.liquidity_usd:,.0f} < ${f.min_liquidity_usd:,.0f}")
    if snap.volume_h1 is None:
        reasons.append("1h volume unknown")
    elif snap.volume_h1 < f.min_volume_1h_usd:
        reasons.append(f"1h volume ${snap.volume_h1:,.0f} < ${f.min_volume_1h_usd:,.0f}")
    age = snap.age_minutes(now)
    if age is None:
        reasons.append("pair age unknown")
    elif age < f.min_pair_age_minutes:
        reasons.append(f"pair age {age:.0f}m < {f.min_pair_age_minutes:.0f}m")
    return FilterResult(passed=not reasons, reasons=reasons)


# ---------------------------------------------------------------------------
# Phase 3: composite score
# ---------------------------------------------------------------------------

@dataclass
class CompositeResult:
    score: float                       # 0-100; 0 when safety FAILED
    parts: dict[str, float | None]     # momentum / social / safety (None = not available)
    risk: str                          # LOW / MEDIUM / HIGH
    risk_reasons: list[str] = field(default_factory=list)


def safety_margin(snap: Snapshot, safety, cfg) -> float | None:
    """0-100: how comfortably the token cleared the safety gates.

    Averages whatever measurements exist: liquidity (10x the minimum = full
    marks), top-10 holder share, tax and LP-locked share. None if unchecked."""
    import math
    from .safety import STATUS_FAIL, STATUS_UNVERIFIED

    if safety is None:
        return None
    if safety.status == STATUS_FAIL:
        return 0.0
    comps: list[float] = []
    min_liq = cfg.filters.min_liquidity_usd
    if snap.liquidity_usd and min_liq > 0:
        comps.append(_clamp01(math.log10(max(snap.liquidity_usd / min_liq, 1e-9))))
    values = {c.name: c.value for c in safety.checks if c.value is not None and c.status == "pass"}
    limit = cfg.safety.max_top10_holders_pct
    if "top10_holders" in values and limit > 0:
        comps.append(_clamp01((limit - values["top10_holders"]) / limit))
    tax_limit = max(cfg.safety.evm.max_buy_tax_pct, cfg.safety.evm.max_sell_tax_pct)
    if "taxes" in values and tax_limit > 0:
        comps.append(_clamp01((tax_limit - values["taxes"]) / tax_limit))
    if "lp_locked" in values:
        comps.append(_clamp01(values["lp_locked"] / 100.0))
    margin = (sum(comps) / len(comps) * 100) if comps else 50.0
    if safety.status == STATUS_UNVERIFIED:
        margin *= cfg.scoring.unverified_safety_multiplier
    return round(margin, 1)


def risk_level(snap: Snapshot, safety, now: float | None = None) -> tuple[str, list[str]]:
    from .safety import STATUS_PASS

    high, medium = [], []
    if safety is None or safety.status != STATUS_PASS:
        high.append("safety not fully verified")
    liq = snap.liquidity_usd or 0
    if liq < 50_000:
        high.append("thin liquidity")
    elif liq < 250_000:
        medium.append("modest liquidity")
    age = snap.age_minutes(now)
    if age is not None and age < 60:
        high.append("pool under 1 hour old")
    elif age is not None and age < 24 * 60:
        medium.append("pool under 1 day old")
    mcap = snap.market_cap_usd or snap.fdv_usd
    if mcap is not None and mcap < 1_000_000:
        medium.append("micro cap")
    if high:
        return "HIGH", high + medium
    if medium:
        return "MEDIUM", medium
    return "LOW", []


def composite_score(snap: Snapshot, momentum: MomentumResult, safety, cfg,
                    social: float | None = None, now: float | None = None) -> CompositeResult:
    from .safety import STATUS_FAIL

    margin = safety_margin(snap, safety, cfg)
    parts = {"momentum": momentum.score, "social": social, "safety": margin}
    w = cfg.scoring.weights
    used = {k: w[k] for k, v in parts.items() if v is not None and w[k] > 0}
    total_w = sum(used.values())
    score = sum(parts[k] * wt for k, wt in used.items()) / total_w if total_w else 0.0
    if safety is not None and safety.status == STATUS_FAIL:
        score = 0.0
    risk, reasons = risk_level(snap, safety, now)
    return CompositeResult(score=round(score, 1), parts=parts, risk=risk, risk_reasons=reasons)
