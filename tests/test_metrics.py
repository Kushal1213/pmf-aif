import pandas as pd
import pytest
from app import metrics as M


def S(dates, navs):
    return M.make_series(dates, navs)


def test_absolute_one_year_return():
    s = S(["2022-11-18", "2023-11-18"], [100, 125])
    assert M.point_returns(s)["1y"] == pytest.approx(25.0)


def test_three_year_is_cagr():
    s = S(["2020-11-18", "2023-11-18"], [100, 800])
    r = M.point_returns(s)
    assert r["3y"] == pytest.approx(100.0, abs=0.1)
    assert r["since_inception"] == pytest.approx(100.0, abs=0.1)
    assert r["5y"] is None          # not enough history -> null, not a guess


def test_gap_in_data_yields_null_not_wrong_value():
    s = S(["2022-01-03", "2022-09-01", "2023-11-18"], [100, 110, 120])  # nothing near 2022-11-18
    assert M.point_returns(s)["1y"] is None


def test_max_drawdown_with_recovery():
    s = S(pd.date_range("2024-01-01", periods=5), [100, 120, 60, 90, 130])
    d = M.max_drawdown(s)
    assert d["max_drawdown_pct"] == -50.0
    assert (d["peak_date"], d["trough_date"], d["recovery_date"]) == ("2024-01-02", "2024-01-03", "2024-01-05")


def test_drawdown_unrecovered_and_monotonic():
    assert M.max_drawdown(S(pd.date_range("2024-01-01", periods=3), [100, 50, 60]))["recovery_date"] is None
    assert M.max_drawdown(S(pd.date_range("2024-01-01", periods=3), [1, 2, 3]))["max_drawdown_pct"] == 0.0


def test_volatility_alternating_returns():
    days = pd.bdate_range("2023-01-02", periods=800)
    nav = [100.0]
    for i in range(1, len(days)):
        nav.append(nav[-1] * (1.01 if i % 2 else 0.99))
    rk = M.risk_metrics(S(days, nav), 12)
    assert rk["available"] and rk["volatility_pct"] == pytest.approx(0.01 * 252 ** .5 * 100, rel=0.02)


def test_sharpe_matches_manual_and_zero_vol_is_null():
    days = pd.bdate_range("2023-01-02", periods=800)
    s = S(days, [100 * 1.0003 ** i for i in range(800)])
    assert M.risk_metrics(s, 12)["sharpe_ratio"] is None      # constant growth => zero stdev
    import numpy as np
    rng = np.random.default_rng(1)
    s = S(days, 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, 800))))
    got = M.risk_metrics(s, 12, rf=6.5)
    st = s.index[-1] - pd.DateOffset(months=12)
    r = M.daily_returns(s.loc[s.index[s.index.searchsorted(st, side="right") - 1]:])
    exp = ((r - 0.065 / 252).mean() / r.std(ddof=1)) * 252 ** .5
    assert got["sharpe_ratio"] == pytest.approx(exp, abs=1e-3)


def test_insufficient_history_flagged():
    s = S(pd.bdate_range("2024-01-01", periods=100), range(1, 101))
    assert M.risk_metrics(s, 36)["available"] is False


def test_rolling_returns_doubling_each_year():
    days = pd.date_range("2018-01-01", "2021-12-31")
    s = S(days, [100 * 2 ** ((d - days[0]).days / 365) for d in days])
    r = M.rolling_returns(s, 12)
    assert r.index[0] >= pd.Timestamp("2019-01-01")
    assert r.min() == pytest.approx(100, abs=0.5) and r.max() == pytest.approx(100, abs=0.5)
    r3 = M.rolling_returns(s, 36)            # annualised -> still ~100%
    assert r3.mean() == pytest.approx(100, abs=0.5)


def test_parse_period():
    assert M.parse_period("3y") == 36 and M.parse_period("6M") == 6
    for bad in ("0y", "abc", "1.5y", "y"):
        with pytest.raises(ValueError):
            M.parse_period(bad)


def test_category_index_and_data_quality():
    import numpy as np
    idx = pd.bdate_range("2023-01-02", periods=300)
    a = S(idx, 100 * 1.001 ** np.arange(300))
    b = S(idx, 50 * 1.003 ** np.arange(300))
    c = S(idx, 10 * 1.002 ** np.arange(300))
    ci = M.category_index([a, b, c])
    assert ci.iloc[0] == 100 and ci.is_monotonic_increasing
    assert ci.iloc[-1] == pytest.approx(100 * 1.002 ** 299, rel=0.01)   # mean of daily returns ~ 0.2%
    assert M.category_index([]).empty
    holes = S(["2024-01-01", "2024-01-02", "2024-02-20"], [1, 1.1, 1.2])
    dq = M.data_quality(holes, rejected_rows=4)
    assert dq["gaps_over_threshold"]["count"] == 1 and dq["gaps_over_threshold"]["worst"][0]["days"] == 49
    assert dq["rejected_rows_at_ingest"] == 4 and not M.data_quality(S([], [])).get("available")
