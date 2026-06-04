"""Headless-browser test of HTML viz interactivity."""
import os, json
from pathlib import Path
from playwright.sync_api import sync_playwright
from decisiongraph.code_graph_viz import export_html

DB = r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db"
out = export_html(DB, ".bench/viz_test.html", repo="pallets/flask", max_nodes=200)
print(f"exported: {out}")
url = "file:///" + str(Path(".bench/viz_test.html").resolve()).replace("\\", "/")

results = {}
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda msg: msg.type == "error" and errors.append(msg.text))
    page.goto(url, wait_until="load")
    # Wait for vis-network to stabilise (physics)
    page.wait_for_timeout(1500)

    # 1. Toolbar present
    results["toolbar_present"] = page.locator("#toolbar").count() == 1
    # 2. Viz canvas rendered
    results["canvas_present"]  = page.locator("#viz canvas").count() >= 1
    # 3. Search input present
    results["search_input"]    = page.locator("#search").count() == 1
    # 4. Hide-AMBIGUOUS checkbox present
    results["hide_checkbox"]   = page.locator("#hideAmbig").count() == 1
    # 5. Inline vis-network loaded (no CDN)
    results["network_initd"]   = page.evaluate(
        "typeof vis !== 'undefined' && typeof vis.Network === 'function'")
    # 6. DATA payload accessible + has nodes
    n_nodes = page.evaluate("(typeof DATA !== 'undefined') ? DATA.nodes.length : 0")
    n_edges = page.evaluate("(typeof DATA !== 'undefined') ? DATA.edges.length : 0")
    results["nodes_loaded"] = n_nodes
    results["edges_loaded"] = n_edges
    # 7. Toggle hide-AMBIGUOUS — should mutate edges dataset
    page.click("#hideAmbig")    # uncheck (was checked=true by default)
    page.wait_for_timeout(300)
    # the toggle is on the checkbox; verify the listener fired (no errors)
    # Check that toggling caused some edges to gain/lose `hidden` attr
    hidden_before = page.evaluate("edges.get().filter(e => e.hidden).length")
    page.click("#hideAmbig")    # back to checked
    page.wait_for_timeout(300)
    hidden_after  = page.evaluate("edges.get().filter(e => e.hidden).length")
    results["hide_toggle_works"] = hidden_before != hidden_after
    results["hidden_before/after"] = [hidden_before, hidden_after]
    # 8. Search bar — type and see if it changes node colors
    page.fill("#search", "Flask")
    page.wait_for_timeout(300)
    n_red = page.evaluate(
        "nodes.get().filter(n => JSON.stringify(n.color).includes('f85149')).length")
    results["search_highlights"] = n_red
    # 9. Console errors during full run
    results["page_errors"] = errors

    browser.close()

print(json.dumps(results, indent=2, default=str))
passed = (results["toolbar_present"] and results["canvas_present"]
          and results["search_input"] and results["hide_checkbox"]
          and results["network_initd"]
          and results["nodes_loaded"] > 0
          and results["edges_loaded"] > 0
          and results["hide_toggle_works"]
          and not results["page_errors"])
print(f"\nVERDICT: {'PASS' if passed else 'FAIL'}")
