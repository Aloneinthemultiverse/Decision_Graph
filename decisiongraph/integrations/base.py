"""Base classes for integrations."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Optional, Iterable
from abc import ABC, abstractmethod


@dataclass
class IntegrationResult:
    ok: bool
    message: str = ""
    data: Any = None

    def to_dict(self): return {"ok": self.ok, "message": self.message, "data": self.data}


@dataclass
class IngestedDoc:
    """A document fetched from an input integration, ready to ingest into a graph."""
    source: str
    external_id: str
    title: str
    content: str
    metadata: dict = field(default_factory=dict)


_AUTO_FIELDS_INPUT = [
    {"key": "default_company_id", "label": "Auto-route to Company ID (blank = personal graph)", "secret": False, "required": False},
    {"key": "auto_sync_enabled",  "label": "Enable automatic pulls",                            "secret": False, "required": False, "type": "bool"},
]
_AUTO_FIELDS_OUTPUT = [
    {"key": "auto_broadcast", "label": "Auto-broadcast new decisions/sessions here", "secret": False, "required": False, "type": "bool", "default": True},
]


class BaseIntegration(ABC):
    """Each integration has a name, a capability (input/output/both), and a config schema."""
    name: str = ""
    label: str = ""
    direction: str = "input"   # "input" | "output" | "both"
    config_schema: list[dict] = []   # [{"key":"token","label":"Token","secret":True,"required":True}, ...]

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}

    @classmethod
    def full_schema(cls) -> list[dict]:
        """Schema + auto-routing fields appropriate to this integration's direction."""
        extra = []
        if cls.direction in ("input", "both"):  extra += _AUTO_FIELDS_INPUT
        if cls.direction in ("output", "both"): extra += _AUTO_FIELDS_OUTPUT
        # filter out duplicates by key
        seen = {s["key"] for s in cls.config_schema}
        return cls.config_schema + [x for x in extra if x["key"] not in seen]

    # ── core lifecycle ──
    @abstractmethod
    def health_check(self) -> IntegrationResult:
        """Verify credentials work. Should make a cheap API call."""
        ...

    # ── input (override if direction in {input,both}) ──
    def fetch(self, limit: int = 50, **kwargs) -> list[IngestedDoc]:
        raise NotImplementedError(f"{self.name} does not support input")

    # ── output (override if direction in {output,both}) ──
    def push_decision(self, decision: dict, **kwargs) -> IntegrationResult:
        raise NotImplementedError(f"{self.name} does not support output")

    def push_session_summary(self, session: dict, **kwargs) -> IntegrationResult:
        raise NotImplementedError(f"{self.name} does not support output")

    # ── helpers ──
    def required_missing(self) -> list[str]:
        return [s["key"] for s in self.config_schema if s.get("required") and not self.config.get(s["key"])]

    def info(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "direction": self.direction,
            "config_schema": self.full_schema(),
            "configured": not self.required_missing(),
            "missing": self.required_missing(),
            "default_company_id": self.config.get("default_company_id", ""),
            "auto_broadcast":  bool(self.config.get("auto_broadcast", True)) if self.direction in ("output","both") else False,
        }
