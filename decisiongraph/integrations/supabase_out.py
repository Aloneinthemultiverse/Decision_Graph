"""Supabase integration — output (persistent decision audit trail)."""
from __future__ import annotations
from .base import BaseIntegration, IntegrationResult


class SupabaseIntegration(BaseIntegration):
    name = "supabase"
    label = "Supabase"
    direction = "output"
    config_schema = [
        {"key": "url",      "label": "Project URL (https://xxx.supabase.co)", "secret": False, "required": True},
        {"key": "api_key",  "label": "Service Role or Anon Key",              "secret": True,  "required": True},
        {"key": "decisions_table", "label": "Decisions Table",                "secret": False, "required": False, "default": "dg_decisions"},
        {"key": "sessions_table",  "label": "Sessions Table",                 "secret": False, "required": False, "default": "dg_sessions"},
    ]

    def _client(self):
        from supabase import create_client
        return create_client(self.config["url"], self.config["api_key"])

    def health_check(self) -> IntegrationResult:
        if self.required_missing():
            return IntegrationResult(False, f"Missing: {', '.join(self.required_missing())}")
        try:
            # cheap probe — query a sentinel table; if it doesn't exist that's still a valid connection
            import requests
            r = requests.get(f"{self.config['url']}/rest/v1/",
                             headers={"apikey": self.config["api_key"], "Authorization": f"Bearer {self.config['api_key']}"},
                             timeout=10)
            return IntegrationResult(r.status_code < 500, f"HTTP {r.status_code}", {"endpoint": self.config["url"]})
        except Exception as e:
            return IntegrationResult(False, f"Supabase error: {e}")

    def push_decision(self, decision: dict, **_) -> IntegrationResult:
        tbl = self.config.get("decisions_table", "dg_decisions")
        try:
            cl = self._client()
            row = {
                "decision_id": decision.get("id"),
                "question":    decision.get("question"),
                "answer":      decision.get("answer"),
                "reasoning":   decision.get("reasoning_summary"),
                "timestamp":   decision.get("timestamp"),
                "communities": decision.get("communities_used", []),
            }
            r = cl.table(tbl).insert(row).execute()
            return IntegrationResult(True, "Inserted into Supabase", {"table": tbl, "count": len(r.data or [])})
        except Exception as e:
            return IntegrationResult(False, str(e))

    def push_session_summary(self, session: dict, **_) -> IntegrationResult:
        tbl = self.config.get("sessions_table", "dg_sessions")
        try:
            cl = self._client()
            row = {
                "session_id":      session.get("id"),
                "title":           session.get("title"),
                "summary":         session.get("summary"),
                "key_decisions":   session.get("key_decisions", []),
                "open_questions":  session.get("open_questions", []),
                "duration_minutes": session.get("duration_minutes"),
                "messages":        session.get("messages", 0),
            }
            r = cl.table(tbl).insert(row).execute()
            return IntegrationResult(True, "Inserted session", {"table": tbl})
        except Exception as e:
            return IntegrationResult(False, str(e))
