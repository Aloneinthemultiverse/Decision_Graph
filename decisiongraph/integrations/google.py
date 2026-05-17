"""Google Drive / Gmail / Calendar — OAuth shared via service-account or saved token.

Use ONE of:
  (a) service_account_json — paste the JSON contents (best for headless, domain-wide)
  (b) oauth_token_json     — pre-obtained OAuth token JSON (from a one-time browser flow)
"""
from __future__ import annotations
import json, base64
from datetime import datetime
from .base import BaseIntegration, IntegrationResult, IngestedDoc


def _build_credentials(cfg: dict):
    """Returns google.auth.credentials.Credentials from either service_account or oauth_token."""
    sa = cfg.get("service_account_json")
    oa = cfg.get("oauth_token_json")
    if sa:
        from google.oauth2 import service_account
        info = json.loads(sa) if isinstance(sa, str) else sa
        return service_account.Credentials.from_service_account_info(info, scopes=cfg.get("scopes", []))
    if oa:
        from google.oauth2.credentials import Credentials
        info = json.loads(oa) if isinstance(oa, str) else oa
        return Credentials.from_authorized_user_info(info, scopes=cfg.get("scopes", []))
    raise ValueError("Google: provide service_account_json or oauth_token_json")


class _GoogleBase(BaseIntegration):
    config_schema = [
        {"key": "service_account_json", "label": "Service Account JSON (paste contents)", "secret": True, "required": False, "textarea": True},
        {"key": "oauth_token_json",     "label": "OAuth Token JSON (alt to SA)",          "secret": True, "required": False, "textarea": True},
    ]
    scopes: list[str] = []

    def _creds(self):
        self.config["scopes"] = self.scopes
        return _build_credentials(self.config)

    def required_missing(self) -> list[str]:
        if self.config.get("service_account_json") or self.config.get("oauth_token_json"):
            return []
        return ["service_account_json | oauth_token_json"]


# ── Google Drive ────────────────────────────────────────────────────────────────
class GoogleDriveIntegration(_GoogleBase):
    name = "google_drive"
    label = "Google Drive"
    direction = "both"
    scopes = ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/drive.file"]

    def _service(self):
        from googleapiclient.discovery import build
        return build("drive", "v3", credentials=self._creds(), cache_discovery=False)

    def health_check(self) -> IntegrationResult:
        if self.required_missing(): return IntegrationResult(False, "Missing credentials")
        try:
            r = self._service().files().list(pageSize=1, fields="files(id,name)").execute()
            return IntegrationResult(True, "Connected", {"sample": r.get("files", [])})
        except Exception as e:
            return IntegrationResult(False, str(e))

    def fetch(self, limit: int = 25, query: str | None = None, **_) -> list[IngestedDoc]:
        srv = self._service()
        q = query or "mimeType='application/pdf' or mimeType='text/plain' or mimeType contains 'document'"
        r = srv.files().list(q=q, pageSize=limit, fields="files(id,name,mimeType,modifiedTime)").execute()
        docs = []
        for f in r.get("files", []):
            try:
                if "google-apps.document" in f["mimeType"]:
                    content = srv.files().export(fileId=f["id"], mimeType="text/plain").execute().decode("utf-8", errors="ignore")
                else:
                    content = srv.files().get_media(fileId=f["id"]).execute()
                    content = content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else str(content)
            except Exception:
                continue
            if not content.strip(): continue
            docs.append(IngestedDoc(source="google_drive", external_id=f["id"], title=f["name"],
                                     content=content, metadata={"mime": f["mimeType"], "modified": f.get("modifiedTime")}))
        return docs

    def push_session_summary(self, session: dict, **_) -> IntegrationResult:
        try:
            srv = self._service()
            body = (f"# {session.get('title','Session')}\n\n"
                    f"{session.get('summary','')}\n\n"
                    f"## Key Decisions\n" + "\n".join(f"- {d}" for d in (session.get('key_decisions') or [])) + "\n\n"
                    f"## Open Questions\n" + "\n".join(f"- {q}" for q in (session.get('open_questions') or [])))
            from googleapiclient.http import MediaInMemoryUpload
            media = MediaInMemoryUpload(body.encode("utf-8"), mimetype="text/markdown")
            r = srv.files().create(body={"name": f"DG Session — {session.get('title','')}.md"}, media_body=media, fields="id,webViewLink").execute()
            return IntegrationResult(True, "File created in Drive", {"id": r.get("id"), "url": r.get("webViewLink")})
        except Exception as e:
            return IntegrationResult(False, str(e))


# ── Gmail ───────────────────────────────────────────────────────────────────────
class GmailIntegration(_GoogleBase):
    name = "gmail"
    label = "Gmail"
    direction = "both"
    scopes = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"]
    config_schema = _GoogleBase.config_schema + [
        {"key": "recipient", "label": "Default recipient (for summaries)", "secret": False, "required": False},
    ]

    def _service(self):
        from googleapiclient.discovery import build
        return build("gmail", "v1", credentials=self._creds(), cache_discovery=False)

    def health_check(self) -> IntegrationResult:
        if self.required_missing(): return IntegrationResult(False, "Missing credentials")
        try:
            p = self._service().users().getProfile(userId="me").execute()
            return IntegrationResult(True, "Connected", {"email": p.get("emailAddress"), "total": p.get("messagesTotal")})
        except Exception as e:
            return IntegrationResult(False, str(e))

    def fetch(self, limit: int = 25, query: str = "", **_) -> list[IngestedDoc]:
        srv = self._service()
        r = srv.users().messages().list(userId="me", q=query, maxResults=limit).execute()
        docs = []
        for m in r.get("messages", []):
            msg = srv.users().messages().get(userId="me", id=m["id"], format="full").execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            subject = headers.get("Subject", "(no subject)")
            sender  = headers.get("From", "?")
            # extract plain body
            body = ""
            def walk(part):
                nonlocal body
                if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                    body += base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                for p in part.get("parts", []): walk(p)
            walk(msg.get("payload", {}))
            if not body.strip(): body = msg.get("snippet", "")
            docs.append(IngestedDoc(source="gmail", external_id=m["id"], title=subject,
                                     content=f"From: {sender}\nSubject: {subject}\n\n{body}",
                                     metadata={"sender": sender, "thread": msg.get("threadId")}))
        return docs

    def _send(self, to: str, subject: str, body: str) -> dict:
        from email.mime.text import MIMEText
        srv = self._service()
        msg = MIMEText(body)
        msg["to"] = to; msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        return srv.users().messages().send(userId="me", body={"raw": raw}).execute()

    def push_session_summary(self, session: dict, to: str | None = None, **_) -> IntegrationResult:
        rcpt = to or self.config.get("recipient")
        if not rcpt: return IntegrationResult(False, "recipient required")
        body = (f"Session: {session.get('title','')}\n"
                f"Duration: {session.get('duration_minutes',0):.1f}m\n\n"
                f"Summary:\n{session.get('summary','')}\n\n"
                f"Key Decisions:\n" + "\n".join(f"- {d}" for d in (session.get('key_decisions') or [])) + "\n\n"
                f"Open Questions:\n" + "\n".join(f"- {q}" for q in (session.get('open_questions') or [])))
        try:
            r = self._send(rcpt, f"[DecisionGraph] {session.get('title','Session')}", body)
            return IntegrationResult(True, "Email sent", {"id": r.get("id")})
        except Exception as e:
            return IntegrationResult(False, str(e))

    def push_decision(self, decision: dict, to: str | None = None, **_) -> IntegrationResult:
        rcpt = to or self.config.get("recipient")
        if not rcpt: return IntegrationResult(False, "recipient required")
        body = f"Q: {decision.get('question','')}\n\nA: {decision.get('answer','')}\n\nID: {decision.get('id','')}"
        try:
            r = self._send(rcpt, f"[DecisionGraph] Decision: {decision.get('question','')[:60]}", body)
            return IntegrationResult(True, "Email sent", {"id": r.get("id")})
        except Exception as e:
            return IntegrationResult(False, str(e))


# ── Google Calendar ─────────────────────────────────────────────────────────────
class GoogleCalendarIntegration(_GoogleBase):
    name = "google_calendar"
    label = "Google Calendar"
    direction = "both"
    scopes = ["https://www.googleapis.com/auth/calendar"]
    config_schema = _GoogleBase.config_schema + [
        {"key": "calendar_id", "label": "Calendar ID (default: primary)", "secret": False, "required": False, "default": "primary"},
    ]

    def _service(self):
        from googleapiclient.discovery import build
        return build("calendar", "v3", credentials=self._creds(), cache_discovery=False)

    def health_check(self) -> IntegrationResult:
        if self.required_missing(): return IntegrationResult(False, "Missing credentials")
        try:
            cals = self._service().calendarList().list(maxResults=5).execute()
            return IntegrationResult(True, "Connected", {"calendars": [c.get("summary") for c in cals.get("items",[])]})
        except Exception as e:
            return IntegrationResult(False, str(e))

    def fetch(self, limit: int = 25, **_) -> list[IngestedDoc]:
        from datetime import timezone
        srv = self._service()
        cal = self.config.get("calendar_id") or "primary"
        now = datetime.now(timezone.utc).isoformat()
        evs = srv.events().list(calendarId=cal, timeMin=now, maxResults=limit, singleEvents=True, orderBy="startTime").execute()
        docs = []
        for ev in evs.get("items", []):
            title = ev.get("summary", "(no title)")
            desc  = ev.get("description", "")
            attendees = ", ".join(a.get("email","") for a in ev.get("attendees", []))
            content = f"Meeting: {title}\nWhen: {ev.get('start',{}).get('dateTime','')}\nAttendees: {attendees}\n\n{desc}"
            docs.append(IngestedDoc(source="google_calendar", external_id=ev["id"], title=title,
                                     content=content, metadata={"start": ev.get("start"), "attendees": attendees}))
        return docs

    def push_session_summary(self, session: dict, **_) -> IntegrationResult:
        """Create follow-up events for each open question (1 day out)."""
        from datetime import timedelta, timezone
        srv = self._service()
        cal = self.config.get("calendar_id") or "primary"
        created = []
        for q in (session.get("open_questions") or [])[:5]:
            start = datetime.now(timezone.utc) + timedelta(days=1)
            end   = start + timedelta(minutes=30)
            ev = {"summary": f"[DG follow-up] {q[:80]}",
                  "description": f"Open question from session: {session.get('title','')}\n\n{q}",
                  "start": {"dateTime": start.isoformat()},
                  "end":   {"dateTime": end.isoformat()}}
            try:
                r = srv.events().insert(calendarId=cal, body=ev).execute()
                created.append(r.get("htmlLink"))
            except Exception as e:
                return IntegrationResult(False, str(e))
        return IntegrationResult(True, f"{len(created)} follow-ups scheduled", {"events": created})
