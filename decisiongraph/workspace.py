"""Per-visitor workspace isolation for safe public deployment.

Each visitor gets their own DecisionGraph / EnterpriseHub / DiscussionManager
rooted at storage/workspaces/<token>/ — fully isolated, concurrency-safe
(every instance carries its own storage_dir; no global config mutation).

The SentenceTransformer embedding model is loaded ONCE process-wide and shared
(read-only) across all workspaces — without this, N visitors = N model copies
= out of memory.
"""
from __future__ import annotations
import os, time, threading, secrets, shutil
from pathlib import Path
from collections import OrderedDict
from . import config


# ── shared embedding model (loaded once, read-only, thread-safe to share) ──────
_EMBED_LOCK = threading.Lock()
_EMBED_MODEL = None

def get_shared_embed_model():
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        with _EMBED_LOCK:
            if _EMBED_MODEL is None:
                from sentence_transformers import SentenceTransformer
                print(f"[workspace] loading shared embed model: {config.EMBED_MODEL}")
                _EMBED_MODEL = SentenceTransformer(config.EMBED_MODEL)
                print("[workspace] shared embed model ready")
    return _EMBED_MODEL


class Workspace:
    """One visitor's isolated world. Heavy objects are built lazily on first use."""

    def __init__(self, token: str, root_dir: str):
        self.token = token
        self.root = root_dir
        os.makedirs(self.root, exist_ok=True)
        self.created_at = time.time()
        self.last_seen = time.time()
        # per-workspace lock: serialises mutations to THIS workspace only
        # (e.g. same visitor double-clicking ingest). Different workspaces
        # never share state so they never block each other.
        self.lock = threading.RLock()
        self._dg = None
        self._hub = None
        self._dm = None
        # lightweight per-workspace rate-limit counters
        self.rl = {}   # {bucket: [timestamps]}

    def touch(self):
        self.last_seen = time.time()

    # lazy heavy objects ------------------------------------------------------
    @property
    def dg(self):
        if self._dg is None:
            from .core import DecisionGraph
            self._dg = DecisionGraph(
                storage_dir=os.path.join(self.root, "personal"),
                embed_model=get_shared_embed_model(),
            )
        return self._dg

    @property
    def hub(self):
        if self._hub is None:
            from .company import EnterpriseHub
            self._hub = EnterpriseHub(
                storage_dir=self.root,
                embed_model=get_shared_embed_model(),
            )
        return self._hub

    @property
    def dm(self):
        if self._dm is None:
            from .discussion import DiscussionManager
            self._dm = DiscussionManager(
                storage_dir=os.path.join(self.root, "discussions")
            )
        return self._dm

    def unload(self):
        """Drop heavy in-memory objects (data stays on disk). Reloaded lazily."""
        self._dg = None
        self._hub = None
        self._dm = None


class WorkspaceManager:
    """Lazy create + LRU evict, hard cap on concurrent in-memory workspaces."""

    def __init__(self, base_dir: str = None, max_live: int = 40,
                 idle_evict_s: int = 3600):
        self.base = base_dir or os.path.join(config.STORAGE_DIR, "workspaces")
        os.makedirs(self.base, exist_ok=True)
        self.max_live = max_live
        self.idle_evict_s = idle_evict_s
        self._ws: "OrderedDict[str, Workspace]" = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def new_token() -> str:
        return secrets.token_urlsafe(16)

    @staticmethod
    def _safe(token: str) -> bool:
        # tokens we mint are url-safe base64; reject anything path-traversal-y
        return bool(token) and "/" not in token and "\\" not in token \
            and ".." not in token and len(token) <= 64

    def get(self, token: str) -> Workspace:
        if not self._safe(token):
            raise ValueError("invalid workspace token")
        with self._lock:
            ws = self._ws.get(token)
            if ws is None:
                ws = Workspace(token, os.path.join(self.base, token))
                self._ws[token] = ws
                self._evict_if_needed_locked()
            else:
                self._ws.move_to_end(token)
            ws.touch()
            return ws

    def _evict_if_needed_locked(self):
        # time-based: drop idle workspaces from memory (disk untouched)
        now = time.time()
        for tok in list(self._ws.keys()):
            if now - self._ws[tok].last_seen > self.idle_evict_s:
                self._ws[tok].unload()
                del self._ws[tok]
        # size-based: evict least-recently-used beyond the cap
        while len(self._ws) > self.max_live:
            tok, ws = self._ws.popitem(last=False)
            ws.unload()

    def stats(self) -> dict:
        with self._lock:
            return {"live": len(self._ws), "max_live": self.max_live}

    def count_persisted(self) -> int:
        try:
            return sum(1 for p in Path(self.base).iterdir() if p.is_dir())
        except Exception:
            return 0
