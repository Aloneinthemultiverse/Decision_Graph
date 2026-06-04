"""Sellers = people/teams who list agents on the marketplace.

Stored centrally in storage/sellers.json (NOT per-device — sellers are
shared across the platform; their listings are visible to all buyers).

Each seller record:
  { id, display_name, email?, bio?, verified, created_at, deleted }

For now: no real auth. A seller "signs up" by claiming a slug + getting an
opaque `seller_key` which they keep to manage their listings later. Real
auth (passkey/OAuth) is Phase 5.
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
_FILE = "sellers.json"


def _path() -> str:
    return os.path.join(config.STORAGE_DIR, _FILE)


def _load() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sellers": {}, "by_slug": {}}


def _save(d: dict) -> None:
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (s or "seller")[:40]


def signup(display_name: str, email: Optional[str] = None,
           bio: Optional[str] = None) -> dict:
    """Register a new seller. Returns the seller record INCLUDING the
    seller_key (one-time visible — store it; needed to manage listings)."""
    name = (display_name or "").strip()
    if not name:
        raise ValueError("display_name required")
    with _LOCK:
        d = _load()
        slug = _slugify(name)
        original = slug
        i = 1
        while slug in d.get("by_slug", {}):
            i += 1
            slug = f"{original}-{i}"
        sid = "s_" + secrets.token_urlsafe(8)
        key = "sk_" + secrets.token_urlsafe(20)
        rec = {
            "id": sid,
            "slug": slug,
            "display_name": name,
            "email": (email or "").strip() or None,
            "bio": (bio or "").strip() or None,
            "verified": False,
            "created_at": time.time(),
            "deleted": False,
            "key_hash": _hash_key(key),    # stored hashed; raw shown once
        }
        d.setdefault("sellers", {})[sid] = rec
        d.setdefault("by_slug", {})[slug] = sid
        _save(d)
        # Return the record + the raw key once. Strip key_hash.
        out = {k: v for k, v in rec.items() if k != "key_hash"}
        out["seller_key"] = key
        return out


def _hash_key(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def authenticate(seller_key: str) -> Optional[dict]:
    """Look up a seller by their key. Returns the (key-stripped) record
    or None."""
    if not seller_key:
        return None
    target = _hash_key(seller_key)
    with _LOCK:
        d = _load()
        for sid, rec in (d.get("sellers") or {}).items():
            if rec.get("deleted"):
                continue
            if rec.get("key_hash") == target:
                return {k: v for k, v in rec.items() if k != "key_hash"}
    return None


def get(seller_id: str) -> Optional[dict]:
    with _LOCK:
        d = _load()
        rec = (d.get("sellers") or {}).get(seller_id)
        if rec and not rec.get("deleted"):
            return {k: v for k, v in rec.items() if k != "key_hash"}
    return None


def list_all(include_deleted: bool = False) -> list[dict]:
    with _LOCK:
        d = _load()
        out = []
        for rec in (d.get("sellers") or {}).values():
            if rec.get("deleted") and not include_deleted:
                continue
            out.append({k: v for k, v in rec.items() if k != "key_hash"})
        out.sort(key=lambda s: s.get("created_at", 0))
        return out


def update(seller_id: str, *, bio: Optional[str] = None,
           email: Optional[str] = None,
           display_name: Optional[str] = None) -> Optional[dict]:
    with _LOCK:
        d = _load()
        rec = (d.get("sellers") or {}).get(seller_id)
        if not rec or rec.get("deleted"):
            return None
        if bio is not None:
            rec["bio"] = bio.strip() or None
        if email is not None:
            rec["email"] = email.strip() or None
        if display_name is not None and display_name.strip():
            rec["display_name"] = display_name.strip()
        _save(d)
        return {k: v for k, v in rec.items() if k != "key_hash"}


def delete(seller_id: str) -> bool:
    with _LOCK:
        d = _load()
        rec = (d.get("sellers") or {}).get(seller_id)
        if not rec or rec.get("deleted"):
            return False
        rec["deleted"] = True
        rec["deleted_at"] = time.time()
        # also remove the slug mapping so the slug can be reused
        slug = rec.get("slug")
        if slug and d.get("by_slug", {}).get(slug) == seller_id:
            d["by_slug"].pop(slug, None)
        _save(d)
        return True
