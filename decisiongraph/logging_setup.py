"""Structured logging for DecisionGraph.

Replaces ad-hoc print() with a real logging module. Honours env vars:

    DG_LOG_LEVEL    debug | info | warning | error   (default: info)
    DG_LOG_JSON     true to emit JSON lines (for ingestion to ELK / Loki)
    DG_LOG_FILE     path to log file (default: stderr)

Usage:
    from decisiongraph.logging_setup import get_logger
    log = get_logger(__name__)
    log.info("ingested %d files", n)
"""
from __future__ import annotations
import logging
import os
import sys
import json
from typing import Any


class _JsonFormatter(logging.Formatter):
    """One JSON object per line — production-friendly for log shippers."""
    def format(self, rec: logging.LogRecord) -> str:
        # Use datetime.fromtimestamp for proper microsecond support — Python's
        # strftime drops sub-second precision on Windows.
        from datetime import datetime
        payload: dict[str, Any] = {
            "ts":     datetime.fromtimestamp(rec.created).isoformat(timespec="milliseconds"),
            "level":  rec.levelname,
            "logger": rec.name,
            "msg":    rec.getMessage(),
        }
        if rec.exc_info:
            payload["exc"] = self.formatException(rec.exc_info)
        # carry any structured kwargs the caller stuffed in
        for key in ("event", "workspace", "tool", "duration_ms",
                     "file", "symbol", "repo", "elapsed_s"):
            v = rec.__dict__.get(key)
            if v is not None:
                payload[key] = v
        return json.dumps(payload, default=str)


def _human_formatter() -> logging.Formatter:
    return logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%H:%M:%S")


_CONFIGURED = False


def _configure_root():
    global _CONFIGURED
    if _CONFIGURED: return
    _CONFIGURED = True
    level_name = os.environ.get("DG_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    use_json = os.environ.get("DG_LOG_JSON", "").lower() in ("1", "true", "yes")
    log_file = os.environ.get("DG_LOG_FILE")
    root = logging.getLogger("decisiongraph")
    root.setLevel(level)
    # don't propagate to Python's default root logger (avoids duplicates)
    root.propagate = False
    # clear pre-existing handlers (if module reloaded)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = (logging.FileHandler(log_file, encoding="utf-8")
                if log_file else logging.StreamHandler(sys.stderr))
    handler.setFormatter(_JsonFormatter() if use_json else _human_formatter())
    root.addHandler(handler)


def get_logger(name: str = "decisiongraph") -> logging.Logger:
    """Get the structured logger for a module. Lazy-configures the root
    logger on first call."""
    _configure_root()
    # always under 'decisiongraph' prefix even when caller passes __name__
    if not name.startswith("decisiongraph"):
        name = f"decisiongraph.{name.split('.')[-1]}"
    return logging.getLogger(name)
