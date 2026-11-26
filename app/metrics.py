"""Pure, DB-free financial metrics. All returns / volatility / drawdown are in PERCENT.

Conventions (also documented in README):
* NAV "as of" a date = last NAV on or before it, but only if it is within MAX_GAP_DAYS
  (so a missing month of data yields null, not a silently wrong number).
* Periods > 1 year are annualised (CAGR); periods <= 1 year are absolute.
* Daily returns spanning a gap > MAX_GAP_DAYS are dropped from volatility / Sharpe.
* Volatility = stdev(daily simple returns) * sqrt(252).
* Sharpe = mean(daily return - rf/252) / stdev * sqrt(252).
"""
from __future__ import annotations

import math
import re
from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS = 252
DEFAULT_RF = 6.5          # % p.a., approx. India 91-day T-bill / overnight rate
MAX_GAP_DAYS = 7
MIN_OBS = 20
POINT_PERIODS = ["1w", "1m", "3m", "6m", "1y", "3y", "5y", "10y"]


def parse_period(text: str) -> int:
    """'6m' -> 6, '3y' -> 36 (months). Raises ValueError."""
    m = re.fullmatch(r"(\d{1,2})([my])", text.strip().lower())
    if not m or int(m.group(1)) == 0:
        raise ValueError(f"Invalid period '{text}'. Use e.g. 6m, 1y, 3y.")
    n = int(m.group(1))
    return n * 12 if m.group(2) == "y" else n


def _r(x: Any, nd: int = 4) -> float | None:
    if x is None:
        return None
    x = float(x)
    return round(x, nd) if math.isfinite(x) else None


def make_series(dates, navs) -> pd.Series:
    s = pd.Series(np.asarray(navs, dtype=float), index=pd.DatetimeIndex(pd.to_datetime(list(dates))))
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s[np.isfinite(s.values) & (s.values > 0)]


def _asof(s: pd.Series, ts: pd.Timestamp, max_gap: int = MAX_GAP_DAYS):
    i = s.index.searchsorted(ts, side="right") - 1
    if i < 0 or (ts - s.index[i]).days > max_gap:
        return None
    return s.index[i], float(s.iloc[i])


def _ret(a: float, b: float, years: float, annualise: bool) -> float:
    g = b / a
    if annualise and years > 0:
        g = g ** (1 / years)
    return (g - 1) * 100


def point_returns(s: pd.Series, as_of=None) -> dict[str, float | None]:
    keys = POINT_PERIODS + ["ytd", "since_inception"]
    if as_of is not None:
        s = s.loc[: pd.Timestamp(as_of)]
    if s.empty:
        return {k: None for k in keys}
    end_ts, end_v = s.index[-1], float(s.iloc[-1])
    out: dict[str, float | None] = {}
    for k in POINT_PERIODS:
        off = pd.DateOffset(days=7) if k == "1w" else pd.DateOffset(months=parse_period(k))
        st = _asof(s, end_ts - off)
        if st is None:
            out[k] = None
            continue
        years = (end_ts - st[0]).days / 365.25
        out[k] = _r(_ret(st[1], end_v, years, annualise=k != "1w" and parse_period(k) > 12))
    st = _asof(s, pd.Timestamp(end_ts.year - 1, 12, 31))
    out["ytd"] = _r(_ret(st[1], end_v, 0, False)) if st else None
    yrs = (end_ts - s.index[0]).days / 365.25
    out["since_inception"] = _r(_ret(float(s.iloc[0]), end_v, yrs, yrs > 1)) if len(s) > 1 else None
    return out


def rolling_returns(s: pd.Series, months: int, max_gap: int = MAX_GAP_DAYS) -> pd.Series:
    """Return on every NAV date over the trailing `months`; annualised if > 12 months."""
    idx = s.index
    targets = idx - pd.DateOffset(months=months)
    pos = idx.searchsorted(targets, side="right") - 1
    ok = pos >= 0
    safe = np.where(ok, pos, 0)
    gap = np.asarray((targets - idx[safe]).days)
    ok &= gap <= max_gap
    ratio = s.values / s.values[safe]
    if months > 12:
        ratio = ratio ** (12 / months)
    return pd.Series((ratio - 1)[ok] * 100, index=idx[ok])


def rolling_summary(r: pd.Series) -> dict[str, Any]:
    if r.empty:
        return {"count": 0}
    return {
        "count": int(len(r)), "min": _r(r.min()), "p25": _r(r.quantile(.25)),
        "median": _r(r.median()), "mean": _r(r.mean()), "p75": _r(r.quantile(.75)),
        "max": _r(r.max()), "pct_positive": _r((r > 0).mean() * 100, 2),
    }


def daily_returns(s: pd.Series, max_gap: int = MAX_GAP_DAYS) -> pd.Series:
    r = s.pct_change().dropna()
    gaps = s.index.to_series().diff().dt.days.loc[r.index]
    return r[gaps <= max_gap]


def max_drawdown(s: pd.Series) -> dict[str, Any]:
    dd = s / s.cummax() - 1
    trough = dd.idxmin()
    if dd.loc[trough] >= 0:
        return {"max_drawdown_pct": 0.0, "peak_date": None, "trough_date": None, "recovery_date": None}
    peak = s.loc[:trough].idxmax()
    rec = s.loc[trough:]
    rec = rec[rec >= s.loc[peak]]
    return {
        "max_drawdown_pct": _r(dd.loc[trough] * 100),
        "peak_date": peak.date().isoformat(), "trough_date": trough.date().isoformat(),
        "recovery_date": rec.index[0].date().isoformat() if len(rec) else None,
    }


def risk_metrics(s: pd.Series, months: int, rf: float = DEFAULT_RF) -> dict[str, Any]:
    base = {"period": f"{months}m", "risk_free_rate_pct": rf}

    def na(reason):
        return {**base, "available": False, "reason": reason}

    if s.empty:
        return na("no NAV data")
    end = s.index[-1]
    st = _asof(s, end - pd.DateOffset(months=months))
    if st is None:
        return na("NAV history shorter than requested period")
    w = s.loc[st[0]:]
    r = daily_returns(w)
    if len(r) < MIN_OBS:
        return na("too few observations")
    sd = float(r.std(ddof=1))
    sharpe = float(((r - rf / 100 / TRADING_DAYS).mean() / sd) * math.sqrt(TRADING_DAYS)) if sd > 1e-10 else None
    return {**base, "available": True, "start_date": st[0].date().isoformat(),
            "end_date": end.date().isoformat(), "observations": int(len(r)),
            "volatility_pct": _r(sd * math.sqrt(TRADING_DAYS) * 100),
            "sharpe_ratio": _r(sharpe), "max_drawdown": max_drawdown(w)}


def summary_metrics(s: pd.Series, rf: float = DEFAULT_RF) -> dict[str, float | None]:
    """Flat dict used for the precomputed scheme_metrics table."""
    pr = point_returns(s)
    out = {f"ret_{k}": pr[k] for k in ("1m", "3m", "6m", "1y", "3y", "5y")}
    for y in (1, 3):
        rk = risk_metrics(s, 12 * y, rf)
        out[f"vol_{y}y"] = rk.get("volatility_pct")
        out[f"mdd_{y}y"] = (rk.get("max_drawdown") or {}).get("max_drawdown_pct")
        out[f"sharpe_{y}y"] = rk.get("sharpe_ratio")
    return out


# ---------------------------------------------------------------- data quality / category index
def data_quality(s: pd.Series, rejected_rows: int = 0) -> dict[str, Any]:
    """How trustworthy / complete is this scheme's NAV history? (business-day based, holidays ignored)."""
    if s.empty:
        return {"available": False, "reason": "no NAV data"}
    expected = len(pd.bdate_range(s.index[0], s.index[-1]))
    gaps = s.index.to_series().diff().dt.days.dropna()
    big = gaps[gaps > MAX_GAP_DAYS].sort_values(ascending=False)
    ends = s.index.to_series().shift(1)
    return {
        "available": True, "first_date": s.index[0].date().isoformat(), "last_date": s.index[-1].date().isoformat(),
        "nav_count": int(len(s)), "expected_business_days": int(expected),
        "coverage_pct": _r(min(100.0, len(s) / expected * 100), 2) if expected else None,
        "largest_gap_days": int(gaps.max()) if len(gaps) else 0,
        "gaps_over_threshold": {"threshold_days": MAX_GAP_DAYS, "count": int(len(big)),
                                "worst": [{"from": ends.loc[d].date().isoformat(), "to": d.date().isoformat(),
                                           "days": int(n)} for d, n in big.head(5).items()]},
        "rejected_rows_at_ingest": int(rejected_rows),
    }


def category_index(series: list[pd.Series], min_constituents: int = 3) -> pd.Series:
    """Equal-weighted daily-rebalanced index (base 100) of several schemes' NAVs.

    Each day's index return = mean of the daily returns of schemes that have a valid return that day
    (gap-spanning returns are excluded, as everywhere else); days with fewer than `min_constituents`
    contributing schemes are skipped. Note: built from currently-active schemes only (survivorship bias).
    """
    series = [s for s in series if len(s) > 1]
    if not series:
        return pd.Series(dtype=float)
    rets = pd.concat([daily_returns(s) for s in series], axis=1, sort=True)
    avg = rets.mean(axis=1)[rets.notna().sum(axis=1) >= min(min_constituents, len(series))]
    if avg.empty:
        return pd.Series(dtype=float)
    idx = 100 * (1 + avg).cumprod()
    base = pd.Series([100.0], index=pd.DatetimeIndex([avg.index[0] - pd.Timedelta(days=1)]))
    return pd.concat([base, idx])


# ---------------------------------------------------------------- SIP / XIRR
def xirr(cashflows: list[tuple[pd.Timestamp, float]]) -> float | None:
    """Annualised internal rate of return (as a fraction) for dated cash flows, or None.

    Uses Newton's method with a bisection fallback. Needs at least one negative and one positive flow.
    """
    if len(cashflows) < 2 or not any(v < 0 for _, v in cashflows) or not any(v > 0 for _, v in cashflows):
        return None
    t0 = min(d for d, _ in cashflows)
    yrs = np.array([(d - t0).days / 365.0 for d, _ in cashflows])
    amt = np.array([v for _, v in cashflows], dtype=float)

    def npv(r):
        return float(np.sum(amt / (1 + r) ** yrs))

    r = 0.1
    for _ in range(50):
        if r <= -0.999999:
            break
        f = npv(r)
        df = float(np.sum(-yrs * amt / (1 + r) ** (yrs + 1)))
        if not math.isfinite(f) or not math.isfinite(df) or df == 0:
            break
        nr = r - f / df
        if abs(nr - r) < 1e-10:
            return nr if nr > -1 else None
        r = nr
    lo, hi = -0.9999, 10.0                      # bisection fallback over a wide bracket
    flo, fhi = npv(lo), npv(hi)
    if not (math.isfinite(flo) and math.isfinite(fhi)) or flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-9 or hi - lo < 1e-12:
            return mid
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


def _first_on_or_after(s: pd.Series, ts: pd.Timestamp, max_gap: int = MAX_GAP_DAYS):
    i = s.index.searchsorted(ts, side="left")
    if i >= len(s) or (s.index[i] - ts).days > max_gap:
        return None
    return s.index[i], float(s.iloc[i])


def sip_returns(s: pd.Series, amount: float, start, end=None, day: int = 1) -> dict[str, Any]:
    """Monthly SIP simulation on published NAVs (no stamp duty, exit load or taxes).

    Each instalment buys at the first NAV on or after its scheduled date (within MAX_GAP_DAYS);
    instalments with no NAV in that window are skipped and counted. The holding is valued at the
    last NAV on or before `end` (default: latest NAV).
    """
    if s.empty:
        return {"available": False, "reason": "no NAV data"}
    end_ts = pd.Timestamp(end) if end is not None else s.index[-1]
    val = _asof(s, end_ts)
    if val is None:
        return {"available": False, "reason": "no NAV within 7 days on or before end date"}
    start_ts = pd.Timestamp(start)
    first = pd.Timestamp(start_ts.year, start_ts.month, 1)
    flows, units, skipped, schedule = [], 0.0, 0, []
    m = first
    while m <= val[0]:
        due = m + pd.Timedelta(days=min(day, m.days_in_month) - 1)
        if start_ts <= due <= val[0]:
            schedule.append(due)
        m = m + pd.DateOffset(months=1)
    for due in schedule:
        hit = _first_on_or_after(s, due)
        if hit is None or hit[0] > val[0]:
            skipped += 1
            continue
        units += amount / hit[1]
        flows.append((hit[0], -float(amount)))
    if not flows:
        return {"available": False, "reason": "no instalment could be invested in the requested range"}
    invested = amount * len(flows)
    value = units * val[1]
    irr = xirr(flows + [(val[0], value)])
    return {
        "available": True, "monthly_amount": amount, "day_of_month": day,
        "first_instalment": flows[0][0].date().isoformat(), "valuation_date": val[0].date().isoformat(),
        "instalments": len(flows), "skipped_instalments": skipped,
        "total_invested": _r(invested, 2), "units": _r(units, 4), "valuation_nav": _r(val[1], 4),
        "current_value": _r(value, 2), "gain": _r(value - invested, 2),
        "absolute_return_pct": _r((value / invested - 1) * 100),
        "xirr_pct": _r(irr * 100) if irr is not None else None,
    }
