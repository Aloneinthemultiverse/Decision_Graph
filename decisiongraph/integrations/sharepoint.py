"""SharePoint integration — input (enterprise documents via Microsoft Graph)."""
from __future__ import annotations
import requests
from .base import BaseIntegration, IntegrationResult, IngestedDoc


class SharePointIntegration(BaseIntegration):
    name = "sharepoint"
    label = "SharePoint"
    direction = "input"
    config_schema = [
        {"key": "tenant_id",     "label": "Azure Tenant ID",   "secret": False, "required": True},
        {"key": "client_id",     "label": "App Client ID",     "secret": False, "required": True},
        {"key": "client_secret", "label": "App Client Secret", "secret": True,  "required": True},
        {"key": "site_id",       "label": "SharePoint Site ID","secret": False, "required": False},
    ]

    def _token(self) -> str:
        import msal
        app = msal.ConfidentialClientApplication(
            self.config["client_id"],
            authority=f"https://login.microsoftonline.com/{self.config['tenant_id']}",
            client_credential=self.config["client_secret"],
        )
        r = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in r:
            raise RuntimeError(f"MSAL: {r.get('error_description','no token')}")
        return r["access_token"]

    def health_check(self) -> IntegrationResult:
        if self.required_missing():
            return IntegrationResult(False, f"Missing: {', '.join(self.required_missing())}")
        try:
            tok = self._token()
            r = requests.get("https://graph.microsoft.com/v1.0/sites?search=*",
                             headers={"Authorization": f"Bearer {tok}"}, timeout=15)
            r.raise_for_status()
            sites = r.json().get("value", [])
            return IntegrationResult(True, "Connected", {"sites": [s.get("displayName") for s in sites[:5]]})
        except Exception as e:
            return IntegrationResult(False, str(e))

    def fetch(self, limit: int = 25, site_id: str | None = None, **_) -> list[IngestedDoc]:
        tok = self._token()
        sid = site_id or self.config.get("site_id")
        if not sid: raise ValueError("site_id required")
        # list drive items in default doc library
        r = requests.get(f"https://graph.microsoft.com/v1.0/sites/{sid}/drive/root/children?$top={limit}",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=20)
        r.raise_for_status()
        items = r.json().get("value", [])
        docs = []
        for it in items[:limit]:
            if it.get("folder"): continue
            try:
                url = it.get("@microsoft.graph.downloadUrl")
                if not url: continue
                content = requests.get(url, timeout=20).text[:200_000]
                docs.append(IngestedDoc(source="sharepoint", external_id=it["id"], title=it["name"],
                                         content=content, metadata={"mime": it.get("file",{}).get("mimeType")}))
            except Exception:
                continue
        return docs
