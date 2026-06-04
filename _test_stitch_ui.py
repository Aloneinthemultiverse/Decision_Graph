"""Headless-browser test of the Stitch UI pages."""
import os
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8000"
results = {}

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context()
    page = ctx.new_page()
    errors_per_page = {}
    page.on("pageerror", lambda e: errors_per_page.setdefault(
        page.url, []).append(str(e)))
    page.on("requestfailed", lambda req: errors_per_page.setdefault(
        page.url, []).append(f"req-failed {req.url} -> {req.failure}"))

    for path, name in [
        ("/",        "landing"),
        ("/app",     "app-shell"),
    ]:
        try:
            page.goto(BASE + path, wait_until="domcontentloaded",
                       timeout=15000)
            page.wait_for_timeout(800)
            title  = page.title()
            body   = page.evaluate("document.body.innerText.length")
            errs   = errors_per_page.get(page.url, [])
            results[name] = {
                "ok": True, "title": title[:80],
                "body_chars": body, "errors": errs[:3]}
        except Exception as e:
            results[name] = {"ok": False, "error": str(e)[:120]}

    # Direct static pages (not via routes)
    for fname in ["index.html", "landing.html", "knowledge_base.html",
                   "query_mode.html", "graph_visualizer.html",
                   "discussion_chat.html"]:
        from pathlib import Path
        path = Path(f"stitch_ui/{fname}").resolve()
        url = "file:///" + str(path).replace("\\", "/")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(600)
            body  = page.evaluate("document.body.innerText.length")
            title = page.title()
            errs  = errors_per_page.get(page.url, [])
            results[fname] = {
                "ok": True, "title": title[:80],
                "body_chars": body, "errors": errs[:3]}
        except Exception as e:
            results[fname] = {"ok": False, "error": str(e)[:120]}

    browser.close()

print(f"=== Stitch UI page render check ===")
all_ok = True
for name, r in results.items():
    if r.get("ok"):
        err_count = len(r.get("errors", []))
        print(f"  [{'OK' if err_count == 0 else 'WARN'}] {name:35} "
              f"title={r['title']!r:40}  body={r['body_chars']} chars"
              f"  errs={err_count}")
        if err_count and err_count > 0:
            for e in r["errors"][:1]: print(f"        ! {e[:140]}")
            all_ok = False
    else:
        print(f"  [FAIL] {name:35} {r['error']}")
        all_ok = False
print(f"\nVERDICT: {'PASS' if all_ok else 'WARN'}")
