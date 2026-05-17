"""End-to-end tests for the integrations framework.

Two kinds of tests:
  1. STRUCTURAL — works without any credentials. Verifies registry, classes,
     persistence, error paths, and that health_check correctly reports
     "missing config" without crashing.
  2. LIVE — only runs when env vars are set. Probes the actual external API.

Run:
    python test_integrations.py
"""
import os, sys, json, traceback
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from decisiongraph.integrations import IntegrationManager, INTEGRATIONS
from decisiongraph.integrations.base import IngestedDoc

PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"
results = {"pass":0,"fail":0,"skip":0}

def t(name):
    def wrap(fn):
        def run(*a,**kw):
            try:
                fn(*a,**kw); print(f"  {PASS} {name}"); results["pass"]+=1
            except AssertionError as e:
                print(f"  {FAIL} {name}: {e}"); results["fail"]+=1
            except Exception as e:
                print(f"  {FAIL} {name}: {type(e).__name__}: {e}")
                traceback.print_exc(); results["fail"]+=1
        return run
    return wrap

# ──────────────────────────────────────────────────────────────────────────────
# STRUCTURAL TESTS — no creds needed
# ──────────────────────────────────────────────────────────────────────────────
print("\n=== Structural tests ===")

@t("Registry contains all 9 integrations")
def _r1():
    expected = {"slack","notion","supabase","jira","confluence","google_drive","gmail","google_calendar","sharepoint"}
    assert expected == set(INTEGRATIONS.keys()), f"got {set(INTEGRATIONS.keys())}"
_r1()

@t("Each integration has name, label, direction, schema")
def _r2():
    for name, cls in INTEGRATIONS.items():
        inst = cls({})
        assert inst.name == name
        assert inst.label
        assert inst.direction in {"input","output","both"}
        assert isinstance(inst.config_schema, list)
_r2()

@t("Manager persists configs to disk")
def _r3():
    test_path = ROOT / "storage" / "_test_integrations.json"
    if test_path.exists(): test_path.unlink()
    m = IntegrationManager(str(test_path))
    m.configure("slack", {"bot_token":"xoxb-fake","channel_id":"C123"})
    assert test_path.exists()
    saved = json.loads(test_path.read_text())
    assert saved["slack"]["bot_token"] == "xoxb-fake"
    m2 = IntegrationManager(str(test_path))
    assert m2.configs["slack"]["channel_id"] == "C123"
    test_path.unlink()
_r3()

@t("health_check returns ok=False (not crash) for empty config")
def _r4():
    for name in INTEGRATIONS:
        m = IntegrationManager(str(ROOT/"storage"/"_test_empty.json"))
        r = m.health(name)
        assert not r.ok, f"{name}: expected ok=False, got {r}"
        assert "Missing" in r.message or "error" in r.message.lower() or "required" in r.message.lower(), f"{name}: {r.message}"
    p = ROOT/"storage"/"_test_empty.json"
    if p.exists(): p.unlink()
_r4()

@t("list_all() reports configured/missing correctly")
def _r5():
    p = ROOT / "storage" / "_test_list.json"
    if p.exists(): p.unlink()
    m = IntegrationManager(str(p))
    m.configure("slack", {"bot_token":"xoxb-fake"})
    items = {x["name"]: x for x in m.list_all()}
    assert items["slack"]["configured"] is True
    assert items["notion"]["configured"] is False
    assert "token" in items["notion"]["missing"]
    p.unlink()
_r5()

@t("Output-only integrations refuse fetch()")
def _r6():
    for name, cls in INTEGRATIONS.items():
        if cls.direction == "output":
            inst = cls({})
            try:
                inst.fetch()
                raise AssertionError(f"{name} should have raised")
            except NotImplementedError: pass
_r6()

@t("Input-only integrations refuse push_decision()")
def _r7():
    for name, cls in INTEGRATIONS.items():
        if cls.direction == "input":
            inst = cls({})
            try:
                inst.push_decision({})
                raise AssertionError(f"{name} should have raised")
            except NotImplementedError: pass
_r7()

@t("clear() removes config from disk")
def _r8():
    p = ROOT / "storage" / "_test_clear.json"
    if p.exists(): p.unlink()
    m = IntegrationManager(str(p))
    m.configure("jira", {"url":"x","email":"y","api_token":"z"})
    assert "jira" in m.configs
    m.clear("jira")
    assert "jira" not in m.configs
    reloaded = json.loads(p.read_text())
    assert "jira" not in reloaded
    p.unlink()
_r8()

@t("broadcast_decision skips unconfigured integrations")
def _r9():
    p = ROOT / "storage" / "_test_bc.json"
    if p.exists(): p.unlink()
    m = IntegrationManager(str(p))
    r = m.broadcast_decision({"id":"x","question":"q","answer":"a"})
    assert r == {}, f"expected empty broadcast, got {r}"
    if p.exists(): p.unlink()
_r9()

@t("IngestedDoc dataclass works")
def _r10():
    d = IngestedDoc(source="s", external_id="1", title="t", content="c")
    assert d.metadata == {}
    d2 = IngestedDoc(source="s", external_id="2", title="t", content="c", metadata={"k":"v"})
    assert d2.metadata["k"] == "v"
_r10()

@t("full_schema injects auto-routing fields (default_company_id, auto_broadcast)")
def _r11():
    from decisiongraph.integrations.slack import SlackIntegration       # both
    from decisiongraph.integrations.confluence import ConfluenceIntegration  # input only
    from decisiongraph.integrations.jira import JiraIntegration          # output only
    keys = lambda cls: [s["key"] for s in cls.full_schema()]
    # Slack (both) gets both auto-input AND auto-output fields
    assert "default_company_id" in keys(SlackIntegration)
    assert "auto_broadcast"     in keys(SlackIntegration)
    # Confluence (input) gets only auto-input fields
    assert "default_company_id" in keys(ConfluenceIntegration)
    assert "auto_broadcast"     not in keys(ConfluenceIntegration)
    # Jira (output) gets only auto-output fields
    assert "auto_broadcast"     in keys(JiraIntegration)
    assert "default_company_id" not in keys(JiraIntegration)
_r11()

@t("default_company_for() returns configured value")
def _r12():
    p = ROOT / "storage" / "_test_auto.json"
    if p.exists(): p.unlink()
    m = IntegrationManager(str(p))
    m.configure("slack", {"bot_token":"xoxb-fake","default_company_id":"acme"})
    assert m.default_company_for("slack") == "acme"
    assert m.default_company_for("notion") == ""   # unconfigured
    p.unlink()
_r12()

@t("broadcast auto_only=True respects auto_broadcast flag")
def _r13():
    p = ROOT / "storage" / "_test_autobc.json"
    if p.exists(): p.unlink()
    m = IntegrationManager(str(p))
    # configure two: one with auto on, one off — both with fake creds
    m.configure("slack",  {"bot_token":"xoxb-fake", "channel_id":"C1", "auto_broadcast": True})
    m.configure("notion", {"token":"secret_fake", "database_id":"db1", "auto_broadcast": False})
    r_all  = m.broadcast_decision({"id":"x","question":"q","answer":"a"}, auto_only=False)
    r_auto = m.broadcast_decision({"id":"x","question":"q","answer":"a"}, auto_only=True)
    # auto_only=False targets both; auto_only=True targets just slack
    assert set(r_all.keys())  == {"slack", "notion"}
    assert set(r_auto.keys()) == {"slack"}
    p.unlink()
_r13()

@t("info() surfaces default_company_id + auto_broadcast")
def _r14():
    from decisiongraph.integrations.slack import SlackIntegration
    s = SlackIntegration({"bot_token":"x", "default_company_id":"acme", "auto_broadcast": False})
    info = s.info()
    assert info["default_company_id"] == "acme"
    assert info["auto_broadcast"] is False
_r14()

# ──────────────────────────────────────────────────────────────────────────────
# LIVE TESTS — only if env vars are set
# ──────────────────────────────────────────────────────────────────────────────
print("\n=== Live tests (requires env vars) ===")

def live(name, env_keys, runner):
    if not all(os.getenv(k) for k in env_keys):
        print(f"  {SKIP} {name} (set {', '.join(env_keys)} to enable)"); results["skip"]+=1; return
    try:
        runner(); print(f"  {PASS} {name}"); results["pass"]+=1
    except Exception as e:
        print(f"  {FAIL} {name}: {e}"); results["fail"]+=1

def _slack():
    from decisiongraph.integrations.slack import SlackIntegration
    s = SlackIntegration({"bot_token":os.environ["SLACK_BOT_TOKEN"]})
    r = s.health_check(); assert r.ok, r.message
live("Slack auth.test", ["SLACK_BOT_TOKEN"], _slack)

def _notion():
    from decisiongraph.integrations.notion import NotionIntegration
    n = NotionIntegration({"token":os.environ["NOTION_TOKEN"]})
    r = n.health_check(); assert r.ok, r.message
live("Notion users.me", ["NOTION_TOKEN"], _notion)

def _supabase():
    from decisiongraph.integrations.supabase_out import SupabaseIntegration
    s = SupabaseIntegration({"url":os.environ["SUPABASE_URL"], "api_key":os.environ["SUPABASE_KEY"]})
    r = s.health_check(); assert r.ok, r.message
live("Supabase ping", ["SUPABASE_URL","SUPABASE_KEY"], _supabase)

def _jira():
    from decisiongraph.integrations.jira import JiraIntegration
    j = JiraIntegration({"url":os.environ["JIRA_URL"], "email":os.environ["JIRA_EMAIL"], "api_token":os.environ["JIRA_TOKEN"]})
    r = j.health_check(); assert r.ok, r.message
live("Jira myself()", ["JIRA_URL","JIRA_EMAIL","JIRA_TOKEN"], _jira)

def _confluence():
    from decisiongraph.integrations.confluence import ConfluenceIntegration
    c = ConfluenceIntegration({"url":os.environ["CONFLUENCE_URL"], "email":os.environ["JIRA_EMAIL"], "api_token":os.environ["JIRA_TOKEN"]})
    r = c.health_check(); assert r.ok, r.message
live("Confluence spaces", ["CONFLUENCE_URL","JIRA_EMAIL","JIRA_TOKEN"], _confluence)

def _drive():
    from decisiongraph.integrations.google import GoogleDriveIntegration
    sa = os.environ.get("GOOGLE_SA_JSON") or Path(os.environ.get("GOOGLE_SA_PATH","")).read_text() if os.environ.get("GOOGLE_SA_PATH") else None
    g = GoogleDriveIntegration({"service_account_json": sa})
    r = g.health_check(); assert r.ok, r.message
live("Google Drive list", ["GOOGLE_SA_JSON"], _drive)

# ──────────────────────────────────────────────────────────────────────────────
print(f"\n=== Results: {results['pass']} passed, {results['fail']} failed, {results['skip']} skipped ===")
sys.exit(0 if results["fail"] == 0 else 1)
