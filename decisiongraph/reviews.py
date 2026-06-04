"""Reviews — buyer feedback on a third-party agent listing after a hire.

Stored at storage/reviews.json. One file because they're public; visible
on each listing's profile. Buyer identity is the workspace token of the
project they hired from (no need for real auth — the hire flow already
established who hired).
"""
from __future__ import annotations

import os
import json
import time
import secrets
import threading
from typing import Optional

from . import config
from . import listings as _listings


_LOCK = threading.RLock()
_FILE = "reviews.json"


def _path() -> str:
    return os.path.join(config.STORAGE_DIR, _FILE)


def _load() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"reviews": {}}


def _save(d: dict) -> None:
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


def submit(listing_id: str, *, buyer_workspace: str,
           stars: float, comment: str = "",
           hire_job_id: Optional[str] = None) -> dict:
    """Submit a review. stars: 1–5. comment optional."""
    stars = max(1.0, min(float(stars), 5.0))
    if not _listings.get(listing_id):
        raise ValueError("listing not found")
    with _LOCK:
        d = _load()
        rid = "rv_" + secrets.token_urlsafe(8)
        rec = {
            "id": rid,
            "listing_id": listing_id,
            "buyer_workspace": buyer_workspace,
            "stars": stars,
            "comment": (comment or "").strip()[:1000],
            "hire_job_id": hire_job_id,
            "created_at": time.time(),
        }
        d.setdefault("reviews", {})[rid] = rec
        _save(d)
    # bump aggregate on the listing
    _listings.add_review(listing_id, stars)
    return rec


def for_listing(listing_id: str, limit: int = 20) -> list[dict]:
    with _LOCK:
        out = [r for r in (_load().get("reviews") or {}).values()
               if r.get("listing_id") == listing_id]
        out.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return out[:limit]
