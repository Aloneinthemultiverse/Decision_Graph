"""Slack integration — input (channel messages) + output (post summaries)."""
from __future__ import annotations
from .base import BaseIntegration, IntegrationResult, IngestedDoc


class SlackIntegration(BaseIntegration):
    name = "slack"
    label = "Slack"
    direction = "both"
    config_schema = [
        {"key": "bot_token",  "label": "Bot Token (xoxb-…)", "secret": True,  "required": True},
        {"key": "channel_id", "label": "Default Channel ID", "secret": False, "required": False},
    ]

    def _client(self):
        from slack_sdk import WebClient
        token = self.config.get("bot_token") or ""
        return WebClient(token=token)

    def health_check(self) -> IntegrationResult:
        if self.required_missing():
            return IntegrationResult(False, f"Missing: {', '.join(self.required_missing())}")
        try:
            r = self._client().auth_test()
            return IntegrationResult(True, "Connected", {"team": r.get("team"), "user": r.get("user"), "user_id": r.get("user_id")})
        except Exception as e:
            return IntegrationResult(False, f"Slack error: {e}")

    def fetch(self, limit: int = 50, channel: str | None = None, **_) -> list[IngestedDoc]:
        from slack_sdk import WebClient
        cl = self._client()
        ch = channel or self.config.get("channel_id")
        if not ch: raise ValueError("Slack: channel_id required")
        r = cl.conversations_history(channel=ch, limit=limit)
        docs = []
        for m in r.get("messages", []):
            if m.get("subtype"): continue  # skip joins/leaves
            user = m.get("user", "?")
            txt = m.get("text", "")
            if not txt: continue
            docs.append(IngestedDoc(
                source="slack",
                external_id=f"{ch}:{m.get('ts')}",
                title=f"Slack #{ch} — {user}",
                content=f"From: {user}\nChannel: {ch}\n\n{txt}",
                metadata={"channel": ch, "ts": m.get("ts"), "user": user},
            ))
        return docs

    def push_decision(self, decision: dict, channel: str | None = None, **_) -> IntegrationResult:
        ch = channel or self.config.get("channel_id")
        if not ch: return IntegrationResult(False, "channel_id required")
        try:
            text = (f"*Decision recorded*\n*Q:* {decision.get('question','')}\n"
                    f"*A:* {(decision.get('answer','') or '')[:1500]}\n"
                    f"_ID: {decision.get('id','')}_")
            r = self._client().chat_postMessage(channel=ch, text=text)
            return IntegrationResult(True, "Posted to Slack", {"ts": r.get("ts")})
        except Exception as e:
            return IntegrationResult(False, str(e))

    def push_session_summary(self, session: dict, channel: str | None = None, **_) -> IntegrationResult:
        ch = channel or self.config.get("channel_id")
        if not ch: return IntegrationResult(False, "channel_id required")
        decisions = "\n".join(f"• {d}" for d in (session.get("key_decisions") or [])[:10]) or "_none_"
        questions = "\n".join(f"• {q}" for q in (session.get("open_questions") or [])[:10]) or "_none_"
        body = (f"*Session Summary — {session.get('title','')}*\n"
                f"_Duration: {session.get('duration_minutes',0):.1f}m · {session.get('messages',0)} messages_\n\n"
                f"{session.get('summary','')}\n\n"
                f"*Key Decisions*\n{decisions}\n\n"
                f"*Open Questions*\n{questions}")
        try:
            r = self._client().chat_postMessage(channel=ch, text=body)
            return IntegrationResult(True, "Posted summary", {"ts": r.get("ts")})
        except Exception as e:
            return IntegrationResult(False, str(e))
