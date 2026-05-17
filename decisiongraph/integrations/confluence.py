"""Confluence integration — input (engineering docs, product specs)."""
from __future__ import annotations
from .base import BaseIntegration, IntegrationResult, IngestedDoc


class ConfluenceIntegration(BaseIntegration):
    name = "confluence"
    label = "Confluence"
    direction = "input"
    config_schema = [
        {"key": "url",       "label": "Confluence URL (https://your.atlassian.net/wiki)", "secret": False, "required": True},
        {"key": "email",     "label": "Account Email",                                    "secret": False, "required": True},
        {"key": "api_token", "label": "API Token",                                        "secret": True,  "required": True},
        {"key": "space_key", "label": "Default Space Key",                                "secret": False, "required": False},
    ]

    def _conf(self):
        from atlassian import Confluence
        return Confluence(url=self.config["url"], username=self.config["email"],
                          password=self.config["api_token"], cloud=True)

    def health_check(self) -> IntegrationResult:
        if self.required_missing():
            return IntegrationResult(False, f"Missing: {', '.join(self.required_missing())}")
        try:
            c = self._conf()
            spaces = c.get_all_spaces(start=0, limit=5)
            return IntegrationResult(True, "Connected", {"spaces": [s.get("key") for s in (spaces.get("results") or [])]})
        except Exception as e:
            return IntegrationResult(False, f"Confluence error: {e}")

    def fetch(self, limit: int = 50, space_key: str | None = None, **_) -> list[IngestedDoc]:
        import re
        c = self._conf()
        sp = space_key or self.config.get("space_key")
        if sp:
            pages = c.get_all_pages_from_space(sp, start=0, limit=limit, expand="body.storage")
        else:
            # cql search of recent pages
            cql = "type = page order by lastmodified desc"
            res = c.cql(cql, limit=limit, expand="content.body.storage")
            pages = [r.get("content", {}) for r in (res.get("results") or [])]
        docs = []
        for p in pages[:limit]:
            title = p.get("title") or "Untitled"
            html = ((p.get("body") or {}).get("storage") or {}).get("value", "")
            # crude html → text
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).strip()
            if not text: continue
            docs.append(IngestedDoc(
                source="confluence",
                external_id=p.get("id",""),
                title=title,
                content=f"{title}\n\n{text}",
                metadata={"page_id": p.get("id"), "space": (p.get("space") or {}).get("key")},
            ))
        return docs
