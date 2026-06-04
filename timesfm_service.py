"""TimesFM microservice on :5002 — runs in the timesfm-env conda env
(Python 3.11 + torch CPU). The main DG server (Python 3.13) HTTP-calls it
through the forecasting backend chain.

Start (Windows, from this repo dir):
    "C:\\Users\\<you>\\anaconda3\\envs\\timesfm-env\\python.exe" timesfm_service.py
or:
    conda run -n timesfm-env python timesfm_service.py

Health: GET  http://localhost:5002/health
Use:    POST http://localhost:5002/forecast   body: {values, horizon}
"""
from __future__ import annotations

import os
import time
import math
from typing import Optional, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn


app = FastAPI(title="TimesFM Microservice")
_MODEL = None
_LOAD_ERROR: Optional[str] = None
_HORIZON = int(os.getenv("TIMESFM_HORIZON_LEN", "128"))


class ForecastReq(BaseModel):
    values: List[float]
    horizon: int = 12
    freq: int = 0     # 0=high freq (default, generic) · 1=med · 2=low


class ForecastResp(BaseModel):
    backend: str
    point: List[float]
    lower: List[float]
    upper: List[float]
    horizon: int
    notes: str


def _load_model():
    """Lazy-load the 200M PyTorch checkpoint from HuggingFace on first call.
    First call downloads ~800MB; subsequent calls cached. We do NOT cache
    load errors permanently — each call retries from scratch if the previous
    one failed."""
    global _MODEL, _LOAD_ERROR
    if _MODEL is not None:
        return _MODEL
    try:
        import timesfm
        _MODEL = timesfm.TimesFm(
            hparams=timesfm.TimesFmHparams(
                backend="cpu",
                per_core_batch_size=1,
                horizon_len=_HORIZON,
                input_patch_len=32,
                output_patch_len=128,
                num_layers=20,
                model_dims=1280,
            ),
            checkpoint=timesfm.TimesFmCheckpoint(
                huggingface_repo_id="google/timesfm-1.0-200m-pytorch"),
        )
        return _MODEL
    except Exception as e:
        _LOAD_ERROR = f"{type(e).__name__}: {e}"
        raise


@app.get("/health")
def health():
    return {
        "service": "timesfm",
        "model_loaded": _MODEL is not None,
        "load_error": _LOAD_ERROR,
        "horizon_capacity": _HORIZON,
    }


@app.post("/forecast", response_model=ForecastResp)
def forecast(req: ForecastReq):
    if not req.values or len(req.values) < 2:
        raise HTTPException(400, "values must have at least 2 numeric points")
    horizon = max(1, min(int(req.horizon), _HORIZON))
    try:
        model = _load_model()
    except Exception as e:
        raise HTTPException(500, f"TimesFM model failed to load: {e}")

    t0 = time.time()
    try:
        # TimesFM 1.x: forecast(inputs=[series], freq=[freq_code])
        fc, _ = model.forecast(inputs=[list(req.values)], freq=[int(req.freq)])
    except Exception as e:
        raise HTTPException(500, f"forecast failed: {type(e).__name__}: {e}")
    point = [float(x) for x in fc[0][:horizon]]
    # 1.0 PyTorch checkpoint doesn't expose per-step quantiles in this minimal
    # API; approximate a 95% CI from the std of first-differences of the input
    import statistics
    diffs = [req.values[i+1] - req.values[i] for i in range(len(req.values)-1)]
    std = statistics.pstdev(diffs) if len(diffs) > 1 else 0.0
    if std <= 0:
        std = max(abs(req.values[-1]) * 0.01, 1e-6)
    return ForecastResp(
        backend="timesfm-1.0-200m-pytorch",
        point=point,
        lower=[v - 1.96 * std for v in point],
        upper=[v + 1.96 * std for v in point],
        horizon=horizon,
        notes=(f"TimesFM 200M (PyTorch CPU); forecast in {round(time.time()-t0,2)}s; "
               f"95% CI approximated via input diff std={std:.4f}"),
    )


if __name__ == "__main__":
    print(f"[timesfm-svc] starting on :5002  horizon_capacity={_HORIZON}")
    print(f"[timesfm-svc] model loads lazily on first /forecast call")
    uvicorn.run(app, host="127.0.0.1", port=5002, log_level="warning")
