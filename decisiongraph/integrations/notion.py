"""Notion integration — input (wikis/SOPs) + output (decision log pages)."""
from __future__ import annotations
from .base import BaseIntegration, IntegrationResult, IngestedDoc


class NotionIntegration(BaseIntegration):
    name = "notion"
    label = "Notion"
    direction = "both"
    config_schema = [
        {"key": "token",       "label": "Integration Token (secret_…)", "secret": True,  "required": True},
        {"key": "database_id", "label": "Decisions Database ID",        "secret": False, "required": False},
    ]

    def _client(self):
        from notion_client import Client
        return Client(auth=self.config.get("token") or "")

    def health_check(self) -> IntegrationResult:
        if self.required_missing():
            return IntegrationResult(False, f"Missing: {', '.join(self.required_missing())}")
        try:
            me = self._client().users.me()
            return IntegrationResult(True, "Connected", {"bot": me.get("name"), "id": me.get("id")})
        except Exception as e:
            return IntegrationResult(False, f"Notion error: {e}")

    def _extract_text(self, blocks: list) -> str:
        out = []
        for b in blocks:
            t = b.get("type")
            if not t: continue
            data = b.get(t, {})
            rich = data.get("rich_text") or []
            line = "".join(x.get("plain_text","") for x in rich)
            if line: out.append(line)
        return "\n".join(out)

    def fetch(self, limit: int = 50, **_) -> list[IngestedDoc]:
        cl = self._client()
        docs = []
        # search all pages accessible to the integration
        r = cl.search(filter={"property":"object","value":"page"}, page_size=min(limit, 100))
        for page in r.get("results", [])[:limit]:
            pid = page["id"]
            # title
            title = "Untitled"
            props = page.get("properties", {})
            for p in props.values():
                if p.get("type") == "title":
                    title = "".join(x.get("plain_text","") for x in p.get("title", [])) or title
                    break
            # body
            try:
                blocks = cl.blocks.children.list(block_id=pid, page_size=100).get("results", [])
                content = self._extract_text(blocks)
            except Exception:
                content = ""
            if not content.strip(): continue
            docs.append(IngestedDoc(
                source="notion",
                external_id=pid,
                title=title,
                content=f"{title}\n\n{content}",
                metadata={"url": page.get("url"), "page_id": pid},
            ))
        return docs

    def push_decision(self, decision: dict, **_) -> IntegrationResult:
        db = self.config.get("database_id")
        if not db: return IntegrationResult(False, "database_id required for Notion output")
        try:
            cl = self._client()
            page = cl.pages.create(
                parent={"database_id": db},
                properties={
                    "Name": {"title": [{"text": {"content": (decision.get("question") or "Decision")[:100]}}]},
                },
                children=[
                    {"object":"block","type":"heading_2","heading_2":{"rich_text":[{"text":{"content":"Question"}}]}},
                    {"object":"block","type":"paragraph","paragraph":{"rich_text":[{"text":{"content":decision.get("question","")[:1900]}}]}},
                    {"object":"block","type":"heading_2","heading_2":{"rich_text":[{"text":{"content":"Answer"}}]}},
                    {"object":"block","type":"paragraph","paragraph":{"rich_text":[{"text":{"content":(decision.get("answer","") or "")[:1900]}}]}},
                ],
            )
            return IntegrationResult(True, "Notion page created", {"url": page.get("url"), "id": page.get("id")})
        except Exception as e:
            return IntegrationResult(False, str(e))

    def push_session_summary(self, session: dict, **_) -> IntegrationResult:
        db = self.config.get("database_id")
        if not db: return IntegrationResult(False, "database_id required")
        try:
            cl = self._client()
            children = [
                {"object":"block","type":"heading_2","heading_2":{"rich_text":[{"text":{"content":"Summary"}}]}},
                {"object":"block","type":"paragraph","paragraph":{"rich_text":[{"text":{"content":(session.get("summary","") or "")[:1900]}}]}},
            ]
            decs = session.get("key_decisions") or []
            if decs:
                children.append({"object":"block","type":"heading_2","heading_2":{"rich_text":[{"text":{"content":"Key Decisions"}}]}})
                for d in decs[:10]:
                    children.append({"object":"block","type":"bulleted_list_item","bulleted_list_item":{"rich_text":[{"text":{"content":d[:1900]}}]}})
            qs = session.get("open_questions") or []
            if qs:
                children.append({"object":"block","type":"heading_2","heading_2":{"rich_text":[{"text":{"content":"Open Questions"}}]}})
                for q in qs[:10]:
                    children.append({"object":"block","type":"bulleted_list_item","bulleted_list_item":{"rich_text":[{"text":{"content":q[:1900]}}]}})

            page = cl.pages.create(
                parent={"database_id": db},
                properties={"Name": {"title": [{"text": {"content": session.get("title","Session")[:100]}}]}},
                children=children,
            )
            return IntegrationResult(True, "Notion session page created", {"url": page.get("url")})
        except Exception as e:
            return IntegrationResult(False, str(e))
