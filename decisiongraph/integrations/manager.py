"""Integration registry, config persistence, and ingestion bridge."""
from __future__ import annotations
import os, json, tempfile
from pathlib import Path
from typing import Optional
from .base import BaseIntegration, IntegrationResult, IngestedDoc
from .slack import SlackIntegration
from .notion import NotionIntegration
from .supabase_out import SupabaseIntegration
from .jira import JiraIntegration
from .confluence import ConfluenceIntegration
from .google import GoogleDriveIntegration, GmailIntegration, GoogleCalendarIntegration
from .sharepoint import SharePointIntegration


INTEGRATIONS = {
    cls.name: cls for cls in [
        SlackIntegration, NotionIntegration, SupabaseIntegration, JiraIntegration,
        ConfluenceIntegration, GoogleDriveIntegration, GmailIntegration,
        GoogleCalendarIntegration, SharePointIntegration,
    ]
}


class IntegrationManager:
    """Holds configured instances + persists configs to disk."""

    def __init__(self, storage_path: str | None = None):
        self.storage_path = Path(storage_path or (Path(__file__).resolve().parents[2] / "storage" / "integrations.json"))
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.configs: dict[str, dict] = self._load()

    def _load(self) -> dict:
        try:
            if self.storage_path.exists(): return json.loads(self.storage_path.read_text())
        except Exception: pass
        return {}

    def _save(self) -> None:
        self.storage_path.write_text(json.dumps(self.configs, indent=2))

    def list_all(self) -> list[dict]:
        out = []
        for name, cls in INTEGRATIONS.items():
            inst = cls(self.configs.get(name, {}))
            out.append(inst.info())
        return out

    def get(self, name: str) -> BaseIntegration:
        if name not in INTEGRATIONS: raise ValueError(f"Unknown integration: {name}")
        return INTEGRATIONS[name](self.configs.get(name, {}))

    def configure(self, name: str, config: dict) -> None:
        if name not in INTEGRATIONS: raise ValueError(f"Unknown integration: {name}")
        self.configs[name] = config
        self._save()

    def clear(self, name: str) -> None:
        self.configs.pop(name, None); self._save()

    def health(self, name: str) -> IntegrationResult:
        return self.get(name).health_check()

    # ── ingest helpers ──
    def ingest_into(self, name: str, dg_ingest_fn, limit: int = 25, **kwargs) -> dict:
        """Fetch docs from `name` and feed them into a DecisionGraph ingest function.
        `dg_ingest_fn(path)` accepts a file path.
        """
        inst = self.get(name)
        docs: list[IngestedDoc] = inst.fetch(limit=limit, **kwargs)
        results = []
        for d in docs:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
            tmp.write(d.content); tmp.close()
            try:
                dg_ingest_fn(tmp.name)
                results.append({"id": d.external_id, "title": d.title, "ok": True})
            except Exception as e:
                results.append({"id": d.external_id, "title": d.title, "ok": False, "error": str(e)})
            finally:
                try: os.unlink(tmp.name)
                except Exception: pass
        return {"source": name, "fetched": len(docs), "results": results}

    def default_company_for(self, name: str) -> str:
        return (self.configs.get(name) or {}).get("default_company_id", "") or ""

    def _eligible_outputs(self, only: Optional[list[str]], auto_only: bool):
        """Yields (name, instance) tuples for output-capable, configured integrations."""
        for name, cls in INTEGRATIONS.items():
            if only and name not in only: continue
            if cls.direction not in ("output", "both"): continue
            cfg = self.configs.get(name)
            if not cfg: continue
            inst = cls(cfg)
            if inst.required_missing(): continue   # don't broadcast to half-configured
            if auto_only and not cfg.get("auto_broadcast", True): continue
            yield name, inst

    # ── output broadcast ──
    def broadcast_decision(self, decision: dict, only: Optional[list[str]] = None, auto_only: bool = False) -> dict:
        results = {}
        for name, inst in self._eligible_outputs(only, auto_only):
            try:
                r = inst.push_decision(decision); results[name] = r.to_dict()
            except NotImplementedError: continue
            except Exception as e: results[name] = {"ok": False, "message": str(e)}
        return results

    def broadcast_session(self, session: dict, only: Optional[list[str]] = None, auto_only: bool = False) -> dict:
        results = {}
        for name, inst in self._eligible_outputs(only, auto_only):
            try:
                r = inst.push_session_summary(session); results[name] = r.to_dict()
            except NotImplementedError: continue
            except Exception as e: results[name] = {"ok": False, "message": str(e)}
        return results
