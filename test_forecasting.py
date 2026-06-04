"""Forecasting backends + MCP `forecast` tool tests.

Run: python -m pytest test_forecasting.py -q
"""
import math
import pytest

from decisiongraph.forecasting import forecast, detect_backend


def _trend(n=24, slope=2.0, intercept=10.0):
    return [intercept + slope * i for i in range(n)]


def _seasonal(n=48, period=12, amplitude=5.0, slope=0.5):
    return [slope * i + amplitude * math.sin(2 * math.pi * i / period)
            for i in range(n)]


def test_backend_chain_works():
    b = detect_backend()
    assert b in ("timesfm", "statsmodels", "linear")


def test_linear_forecast_extrapolates_trend():
    out = forecast(_trend(20), horizon=5, prefer_backend="linear")
    assert "error" not in out
    # next point should be slope=2 * 20 + 10 = 50, and so on
    assert abs(out["point"][0] - 50.0) < 0.5
    assert abs(out["point"][-1] - 58.0) < 0.5
    assert len(out["point"]) == len(out["lower"]) == len(out["upper"]) == 5


def test_statsmodels_handles_seasonal_series():
    series = _seasonal(48, period=12, amplitude=5.0, slope=0.5)
    out = forecast(series, horizon=12, prefer_backend="statsmodels")
    assert "error" not in out
    assert "holt" in out["backend"].lower()
    assert len(out["point"]) == 12
    # the seasonal pattern should keep amplitude roughly in [-7, +35] band
    assert min(out["point"]) > -10 and max(out["point"]) < 40


def test_invalid_input():
    assert "error" in forecast([], 5)
    assert "error" in forecast([42], 5)
    assert "error" in forecast(["abc", "def"], 5)


def test_horizon_clamped():
    out = forecast(_trend(10), horizon=10000, prefer_backend="linear")
    assert len(out["point"]) <= 200


def test_mcp_tool_dispatch():
    from decisiongraph.mcp_tools import _h_forecast, _h_forecast_backend_info
    info = _h_forecast_backend_info(None, {}, {}, owner=True)
    assert info["backend"] in ("timesfm", "statsmodels", "linear")

    out = _h_forecast(None, {"values": _trend(20), "horizon": 3,
                              "prefer_backend": "linear"}, {}, owner=True)
    assert "error" not in out
    assert len(out["point"]) == 3
