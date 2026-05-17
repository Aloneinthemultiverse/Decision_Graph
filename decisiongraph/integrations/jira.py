"""Jira integration — output (link decisions to tickets)."""
from __future__ import annotations
from .base import BaseIntegration, IntegrationResult


class JiraIntegration(BaseIntegration):
    name = "jira"
    label = "Jira"
    direction = "output"
    config_schema = [
        {"key": "url",         "label": "Jira URL (https://your.atlassian.net)", "secret": False, "required": True},
        {"key": "email",       "label": "Account Email",                         "secret": False, "required": True},
        {"key": "api_token",   "label": "API Token",                             "secret": True,  "required": True},
        {"key": "project_key", "label": "Default Project Key (e.g. DG)",         "secret": False, "required": False},
    ]

    def _jira(self):
        from atlassian import Jira
        return Jira(url=self.config["url"], username=self.config["email"],
                    password=self.config["api_token"], cloud=True)

    def health_check(self) -> IntegrationResult:
        if self.required_missing():
            return IntegrationResult(False, f"Missing: {', '.join(self.required_missing())}")
        try:
            j = self._jira()
            me = j.myself()
            return IntegrationResult(True, "Connected", {"account": me.get("displayName"), "email": me.get("emailAddress")})
        except Exception as e:
            return IntegrationResult(False, f"Jira error: {e}")

    def push_decision(self, decision: dict, project_key: str | None = None, **_) -> IntegrationResult:
        pk = project_key or self.config.get("project_key")
        if not pk: return IntegrationResult(False, "project_key required")
        try:
            j = self._jira()
            fields = {
                "project":     {"key": pk},
                "summary":     (decision.get("question") or "Decision")[:250],
                "description": f"*Question:* {decision.get('question','')}\n\n*Answer:*\n{(decision.get('answer','') or '')[:5000]}",
                "issuetype":   {"name": "Task"},
            }
            r = j.create_issue(fields=fields)
            return IntegrationResult(True, f"Jira ticket {r.get('key')} created", {"key": r.get("key"), "id": r.get("id")})
        except Exception as e:
            return IntegrationResult(False, str(e))

    def push_session_summary(self, session: dict, project_key: str | None = None, **_) -> IntegrationResult:
        pk = project_key or self.config.get("project_key")
        if not pk: return IntegrationResult(False, "project_key required")
        try:
            j = self._jira()
            decisions_md = "\n".join(f"* {d}" for d in (session.get("key_decisions") or [])) or "_none_"
            questions_md = "\n".join(f"* {q}" for q in (session.get("open_questions") or [])) or "_none_"
            desc = (f"*Session Summary:* {session.get('title','')}\n\n"
                    f"{session.get('summary','')}\n\n"
                    f"*Key Decisions:*\n{decisions_md}\n\n"
                    f"*Open Questions:*\n{questions_md}")
            fields = {"project":{"key":pk}, "summary": f"Session: {session.get('title','')}"[:250],
                      "description": desc[:5000], "issuetype": {"name":"Task"}}
            r = j.create_issue(fields=fields)
            return IntegrationResult(True, f"Jira ticket {r.get('key')} created", {"key": r.get("key")})
        except Exception as e:
            return IntegrationResult(False, str(e))
