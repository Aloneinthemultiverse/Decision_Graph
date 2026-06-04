"""Time-series forecasting backends.

Tries in order:
  1. TimesFM microservice (separate Python 3.11 env on :5002) — Google's 200M
     foundation model. Pre-trained on 100B time-points; best zero-shot quality.
     Probed via HTTP; if unreachable, falls through.
  2. statsmodels Holt-Winters (additive trend + seasonal). Already installed.
     CPU, fast, good for short series.
  3. Naive linear extrapolation (numpy). Always works.

API: `forecast(values: list[float], horizon: int) -> dict`
Returns: { backend, point: [float, ...], lower: [...], upper: [...], notes }
"""
from __future__ import annotations
import os
from typing import Optional


_BACKEND: Optional[str] = None
TIMESFM_SVC_URL = os.getenv("TIMESFM_SVC_URL", "http://127.0.0.1:5002")


def _timesfm_svc_healthy(timeout_s: float = 2.0) -> bool:
    try:
        import urllib.request as _r, json as _j
        with _r.urlopen(f"{TIMESFM_SVC_URL}/health", timeout=timeout_s) as r:
            j = _j.loads(r.read().decode("utf-8"))
        return r.status == 200 and "service" in j
    except Exception:
        return False


def detect_backend() -> str:
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    if _timesfm_svc_healthy():
        _BACKEND = "timesfm-svc"
        return _BACKEND
    try:
        import timesfm   # noqa: F401  in-process fallback (rarely available)
        _BACKEND = "timesfm"
        return _BACKEND
    except Exception:
        pass
    try:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing  # noqa: F401
        _BACKEND = "statsmodels"
        return _BACKEND
    except Exception:
        pass
    _BACKEND = "linear"
    return _BACKEND


def reset_backend_cache():
    """Force re-detection on the next forecast (useful after starting the
    microservice mid-process)."""
    global _BACKEND
    _BACKEND = None


def _timesfm_svc_forecast(values: list[float], horizon: int,
                           freq: int = 0) -> dict:
    """HTTP-call the TimesFM microservice running in the side conda env."""
    import urllib.request as _r, urllib.error as _e, json as _j
    body = _j.dumps({"values": values, "horizon": horizon,
                      "freq": freq}).encode("utf-8")
    req = _r.Request(f"{TIMESFM_SVC_URL}/forecast", data=body,
                      headers={"Content-Type": "application/json"})
    try:
        # first-call cold-start (model download / load) can take a minute
        with _r.urlopen(req, timeout=180) as resp:
            payload = _j.loads(resp.read().decode("utf-8"))
        return payload
    except _e.HTTPError as e:
        raise RuntimeError(f"timesfm-svc HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}")
    except Exception as e:
        raise RuntimeError(f"timesfm-svc unreachable: {type(e).__name__}: {e}")


def _linear_forecast(values: list[float], horizon: int) -> dict:
    import numpy as np
    n = len(values)
    x = np.arange(n, dtype=float)
    y = np.asarray(values, dtype=float)
    # linear least-squares
    slope, intercept = np.polyfit(x, y, 1)
    residuals = y - (slope * x + intercept)
    std = float(np.std(residuals) or 1e-6)
    fx = np.arange(n, n + horizon, dtype=float)
    point = (slope * fx + intercept).tolist()
    lower = (np.array(point) - 1.96 * std).tolist()
    upper = (np.array(point) + 1.96 * std).tolist()
    return {
        "backend": "linear",
        "point": point, "lower": lower, "upper": upper,
        "notes": (
            f"naive linear extrapolation (slope={slope:.4f}, intercept="
            f"{intercept:.4f}); 95% CI from residual std={std:.4f}."),
    }


def _holt_winters_forecast(values: list[float], horizon: int,
                            seasonal_periods: Optional[int] = None) -> dict:
    import numpy as np
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    y = np.asarray(values, dtype=float)
    n = len(y)
    # decide whether to use seasonality
    sp = seasonal_periods
    if sp is None and n >= 24:
        # try weekly (7) or monthly-ish (12) seasonality if series is long enough
        sp = 12 if n >= 24 else (7 if n >= 14 else None)
    kwargs = dict(trend="add", initialization_method="estimated")
    if sp and n >= 2 * sp:
        kwargs.update(seasonal="add", seasonal_periods=sp)
    try:
        model = ExponentialSmoothing(y, **kwargs).fit(optimized=True)
    except Exception:
        # fall back to no seasonality
        model = ExponentialSmoothing(y, trend="add",
                                      initialization_method="estimated").fit(
                                          optimized=True)
    point = np.asarray(model.forecast(horizon)).tolist()
    # crude 95% CI from in-sample residual std
    fitted = np.asarray(model.fittedvalues)
    residuals = y[-len(fitted):] - fitted
    std = float(np.std(residuals) or 1e-6)
    arr = np.asarray(point)
    return {
        "backend": "statsmodels-holt-winters",
        "point": point,
        "lower": (arr - 1.96 * std).tolist(),
        "upper": (arr + 1.96 * std).tolist(),
        "notes": (
            f"Holt-Winters (trend={'add' if 'trend' in kwargs else 'none'}"
            f", seasonal={kwargs.get('seasonal','none')}"
            f", period={kwargs.get('seasonal_periods','-')}); 95% CI from "
            f"residual std={std:.4f}."),
    }


_TIMESFM_MODEL = None


def _timesfm_forecast(values: list[float], horizon: int) -> dict:
    import timesfm
    global _TIMESFM_MODEL
    if _TIMESFM_MODEL is None:
        _TIMESFM_MODEL = timesfm.TimesFm(
            hparams=timesfm.TimesFmHparams(
                backend="cpu", per_core_batch_size=1,
                horizon_len=max(1, int(horizon)),
                input_patch_len=32, output_patch_len=128,
                num_layers=20, model_dims=1280,
            ),
            checkpoint=timesfm.TimesFmCheckpoint(
                huggingface_repo_id="google/timesfm-1.0-200m-pytorch"),
        )
    fc, _ = _TIMESFM_MODEL.forecast(
        inputs=[values], freq=[0])    # freq=0 = high freq / generic
    point = list(fc[0])
    # TimesFM 1.0 doesn't expose quantiles in this minimal API; widen by
    # in-sample std as a poor-man's CI
    import numpy as np
    y = np.asarray(values, dtype=float)
    std = float(np.std(np.diff(y)) or 1e-6)
    arr = np.asarray(point)
    return {
        "backend": "timesfm-1.0-200m",
        "point": point,
        "lower": (arr - 1.96 * std).tolist(),
        "upper": (arr + 1.96 * std).tolist(),
        "notes": "TimesFM 200M, CPU, zero-shot; 95% CI approximated via diff std.",
    }


def forecast(values: list[float], horizon: int = 12,
             seasonal_periods: Optional[int] = None,
             prefer_backend: Optional[str] = None) -> dict:
    if not isinstance(values, (list, tuple)) or len(values) < 2:
        return {"error": "values must be a list of >=2 numeric points"}
    horizon = max(1, min(int(horizon), 200))
    try:
        values = [float(v) for v in values]
    except Exception as e:
        return {"error": f"values must be numeric: {e}"}

    chain = []
    if prefer_backend:
        chain.append(prefer_backend)
    else:
        chain.append(detect_backend())
    # fallbacks (try each in order if the preferred one fails)
    for b in ("timesfm-svc", "statsmodels", "linear"):
        if b not in chain:
            chain.append(b)

    last_err = None
    for backend in chain:
        try:
            if backend == "timesfm-svc":
                return _timesfm_svc_forecast(values, horizon)
            if backend == "timesfm":
                return _timesfm_forecast(values, horizon)
            if backend == "statsmodels":
                return _holt_winters_forecast(values, horizon,
                                                seasonal_periods=seasonal_periods)
            if backend == "linear":
                return _linear_forecast(values, horizon)
        except Exception as e:
            last_err = f"{backend}: {type(e).__name__}: {e}"
            continue
    return {"error": last_err or "no forecasting backend worked"}
