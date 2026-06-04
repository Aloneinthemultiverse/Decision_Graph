"""Agent listings = third-party sellers' submitted agents.

Stored in storage/listings.json. Each listing references a seller and an
execution mode:

  · execution = "bundled"     → script_path (only set by us for built-ins)
  · execution = "webhook"     → seller_webhook_url (hires POST to seller's URL)
                                 with a buyer-scoped grant; seller's agent
                                 connects back to our MCP endpoint with it.

Approval workflow: every new listing starts `approved=false`. The platform
operator (you) flips `approved=true` before it shows up on the public
marketplace. UI fetches both bundled (from JSON catalog) AND approved
listings here when rendering /marketplace.

For Phase-4 MVP, auto-approve on submit (gate is on/off via env flag); real
moderation queue is a later refinement.
"""
from __future__ import annotations

import os
import re
import json
import time
import secrets
import threading
from typing import Optional

from . import config


_LOCK = threading.RLock()
_FILE = "listings.json"
AUTO_APPROVE = os.getenv("DG_LISTINGS_AUTO_APPROVE", "1") not in ("0", "false", "False")


def _path() -> str:
    return os.path.join(config.STORAGE_DIR, _FILE)


def _load() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"listings": {}}


def _save(d: dict) -> None:
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


def _norm_url(url: str) -> str:
    return (url or "").strip()


def _validate_webhook(url: str) -> Optional[str]:
    """Return error msg if URL is invalid; None if OK. We allow only
    http(s) and refuse localhost-style URLs to make demoing public listings
    safer (operator can override via env if testing locally)."""
    if not url:
        return "webhook URL required"
    if not re.match(r"^https?://", url, re.I):
        return "webhook URL must start with http:// or https://"
    if os.getenv("DG_ALLOW_LOCAL_WEBHOOKS", "0") != "1":
        if re.search(r"//(localhost|127\.|192\.168\.|10\.|0\.|169\.254\.)",
                     url, re.I):
            return ("local-network URLs are not allowed for public listings; "
                    "set DG_ALLOW_LOCAL_WEBHOOKS=1 if you're testing")
    return None


def submit(seller_id: str, *, name: str, description: str = "",
           tags: Optional[list[str]] = None,
           suggested_topics: Optional[list[str]] = None,
           webhook_url: str = "",
           expected_scope_hint: str = "") -> dict:
    """Submit a new listing. Returns the listing record. Auto-approved unless
    DG_LISTINGS_AUTO_APPROVE=0."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name required")
    err = _validate_webhook(webhook_url)
    if err:
        raise ValueError(err)
    with _LOCK:
        d = _load()
        lid = "lst_" + secrets.token_urlsafe(10)
        rec = {
            "id": lid,
            "seller_id": seller_id,
            "name": name,
            "description": (description or "").strip(),
            "tags": [t for t in (tags or []) if t][:20],
            "suggested_topics": [t for t in (suggested_topics or []) if t][:20],
            "execution": "webhook",
            "webhook_url": _norm_url(webhook_url),
            "expected_scope_hint": (expected_scope_hint or "").strip(),
            "approved": bool(AUTO_APPROVE),
            "created_at": time.time(),
            "deleted": False,
            "hire_count": 0,
            "rating_sum": 0.0,
            "rating_count": 0,
        }
        d.setdefault("listings", {})[lid] = rec
        _save(d)
        return rec


def get(listing_id: str) -> Optional[dict]:
    with _LOCK:
        rec = (_load().get("listings") or {}).get(listing_id)
        if rec and not rec.get("deleted"):
            return rec
    return None


def list_public(include_unapproved: bool = False) -> list[dict]:
    """Listings visible on the public marketplace."""
    with _LOCK:
        out = []
        for rec in (_load().get("listings") or {}).values():
            if rec.get("deleted"):
                continue
            if not rec.get("approved") and not include_unapproved:
                continue
            out.append(rec)
        out.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return out


def list_by_seller(seller_id: str) -> list[dict]:
    with _LOCK:
        out = []
        for rec in (_load().get("listings") or {}).values():
            if rec.get("seller_id") == seller_id and not rec.get("deleted"):
                out.append(rec)
        out.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return out


def update(listing_id: str, *, description: Optional[str] = None,
           tags: Optional[list[str]] = None,
           suggested_topics: Optional[list[str]] = None,
           webhook_url: Optional[str] = None,
           expected_scope_hint: Optional[str] = None) -> Optional[dict]:
    with _LOCK:
        d = _load()
        rec = (d.get("listings") or {}).get(listing_id)
        if not rec or rec.get("deleted"):
            return None
        if description is not None:
            rec["description"] = description.strip()
        if tags is not None:
            rec["tags"] = [t for t in tags if t][:20]
        if suggested_topics is not None:
            rec["suggested_topics"] = [t for t in suggested_topics if t][:20]
        if webhook_url is not None:
            err = _validate_webhook(webhook_url)
            if err:
                raise ValueError(err)
            rec["webhook_url"] = _norm_url(webhook_url)
        if expected_scope_hint is not None:
            rec["expected_scope_hint"] = expected_scope_hint.strip()
        _save(d)
        return rec


def delete(listing_id: str) -> bool:
    with _LOCK:
        d = _load()
        rec = (d.get("listings") or {}).get(listing_id)
        if not rec or rec.get("deleted"):
            return False
        rec["deleted"] = True
        rec["deleted_at"] = time.time()
        _save(d)
        return True


def approve(listing_id: str, approved: bool = True) -> Optional[dict]:
    """Operator-only — flip approval state."""
    with _LOCK:
        d = _load()
        rec = (d.get("listings") or {}).get(listing_id)
        if not rec:
            return None
        rec["approved"] = bool(approved)
        _save(d)
        return rec


def bump_hire(listing_id: str) -> None:
    with _LOCK:
        d = _load()
        rec = (d.get("listings") or {}).get(listing_id)
        if not rec:
            return
        rec["hire_count"] = (rec.get("hire_count") or 0) + 1
        _save(d)


def add_review(listing_id: str, stars: float) -> None:
    with _LOCK:
        d = _load()
        rec = (d.get("listings") or {}).get(listing_id)
        if not rec:
            return
        rec["rating_sum"] = (rec.get("rating_sum") or 0.0) + float(stars)
        rec["rating_count"] = (rec.get("rating_count") or 0) + 1
        _save(d)


def average_rating(listing: dict) -> Optional[float]:
    n = listing.get("rating_count") or 0
    return (listing.get("rating_sum") or 0.0) / n if n > 0 else None
