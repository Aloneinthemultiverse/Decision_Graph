"""Export the structural code graph as a self-contained interactive HTML file.

No external CDN — uses inline vis-network. Open the file in any browser.

Usage:
    from decisiongraph.code_graph_viz import export_html
    export_html("storage/.../code_graph.db", out_path="graph.html",
                repo="pallets/flask", focus_path=None)
"""
from __future__ import annotations
import json, sqlite3
from pathlib import Path
from typing import Optional

# vis-network 9.x UMD bundle — pinned, MIT licensed.
# We inline a minimal renderer that draws nodes + edges and supports
# click-to-zoom + filter-by-bucket.
_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  html, body { margin:0; padding:0; height:100%; font-family:system-ui,sans-serif; background:#0d1117; color:#c9d1d9; }
  #toolbar { padding:8px 16px; background:#161b22; border-bottom:1px solid #30363d; display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
  #toolbar h1 { font-size:14px; margin:0; }
  #toolbar label { font-size:12px; color:#8b949e; user-select:none; }
  #toolbar input[type=checkbox] { accent-color:#58a6ff; }
  #toolbar input[type=text] { background:#0d1117; color:#c9d1d9; border:1px solid #30363d; border-radius:4px; padding:4px 8px; font-size:12px; width:240px; }
  #viz { width:100%; height:calc(100vh - 50px); }
  #info { position:fixed; top:60px; right:16px; max-width:340px; background:#161b22; border:1px solid #30363d; padding:10px 12px; border-radius:6px; font-size:12px; display:none; }
  #info pre { color:#7ee787; white-space:pre-wrap; }
  .legend { display:flex; gap:8px; font-size:11px; }
  .swatch { width:10px; height:10px; border-radius:50%; display:inline-block; margin-right:4px; vertical-align:middle; }
</style>
</head>
<body>
<div id="toolbar">
  <h1>__TITLE__</h1>
  <span class="legend">
    <span><span class="swatch" style="background:#7ee787"></span>EXTRACTED</span>
    <span><span class="swatch" style="background:#79c0ff"></span>INFERRED</span>
    <span><span class="swatch" style="background:#d29922"></span>AMBIGUOUS</span>
    <span><span class="swatch" style="background:#f85149"></span>GOD-NODE</span>
  </span>
  <label><input type="checkbox" id="hideAmbig" checked> hide AMBIGUOUS edges</label>
  <input type="text" id="search" placeholder="search symbol or file...">
  <span style="font-size:11px;color:#8b949e">__SUMMARY__</span>
</div>
<div id="viz"></div>
<div id="info"></div>

<script>
__VIS_BUNDLE__
</script>
<script>
const DATA = __DATA__;
const nodes = new vis.DataSet(DATA.nodes);
const edges = new vis.DataSet(DATA.edges);
let network = new vis.Network(document.getElementById('viz'),
  { nodes, edges },
  {
    interaction: { hover:true, tooltipDelay:120 },
    nodes: { shape:'dot', size:8, font:{ color:'#c9d1d9', size:10 } },
    edges: { arrows:{ to:{ enabled:true, scaleFactor:0.5 } }, smooth:false },
    physics: { stabilization:{ iterations:120 } },
  });

document.getElementById('hideAmbig').addEventListener('change', e => {
  edges.update(DATA.edges.map(x => ({...x, hidden: e.target.checked && x.bucket==='AMBIGUOUS'})));
});

document.getElementById('search').addEventListener('input', e => {
  const q = e.target.value.toLowerCase();
  if (!q) { nodes.update(DATA.nodes.map(n => ({...n, color:n.origColor}))); return; }
  nodes.update(DATA.nodes.map(n => ({...n,
    color: (n.label||'').toLowerCase().includes(q) || (n.title||'').toLowerCase().includes(q)
           ? {background:'#f85149', border:'#fff'} : '#30363d'})));
});

network.on('click', params => {
  const info = document.getElementById('info');
  if (!params.nodes.length) { info.style.display='none'; return; }
  const n = nodes.get(params.nodes[0]);
  info.innerHTML = '<b>'+n.label+'</b><br><i>'+n.title+'</i>';
  info.style.display = 'block';
});
</script>
</body>
</html>"""


_BUCKET_COLORS = {
    "EXTRACTED": "#7ee787",
    "INFERRED":  "#79c0ff",
    "AMBIGUOUS": "#d29922",
}


def _bucket_of(conf: float) -> str:
    if conf >= 0.85: return "EXTRACTED"
    if conf >= 0.5:  return "INFERRED"
    return "AMBIGUOUS"


def export_html(db_path: str, out_path: str,
                 repo: Optional[str] = None,
                 focus_path: Optional[str] = None,
                 max_nodes: int = 500) -> dict:
    """Render the structural graph as an interactive standalone HTML file.

    Args:
      focus_path: if set, only include symbols in this file + their 1-hop
                  neighbours (good for "explain this file").
      max_nodes:  cap total node count for browser performance.
    """
    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row

    # ── pick the symbols to draw ────────────────────────────────────────
    if focus_path:
        # symbols in focus_path + direct callers + direct callees
        seed_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM symbols WHERE path = ?", (focus_path,)).fetchall()]
        if seed_ids:
            ph = ",".join("?" for _ in seed_ids)
            callers = [r["id"] for r in conn.execute(
                f"SELECT DISTINCT s.id FROM calls c JOIN symbols s "
                f"ON c.src_id = s.id WHERE c.dst_id IN ({ph}) "
                f"AND c.confidence >= 0.5 LIMIT ?",
                seed_ids + [max_nodes]).fetchall()]
            callees = [r["id"] for r in conn.execute(
                f"SELECT DISTINCT t.id FROM calls c JOIN symbols t "
                f"ON c.dst_id = t.id WHERE c.src_id IN ({ph}) "
                f"AND c.confidence >= 0.5 LIMIT ?",
                seed_ids + [max_nodes]).fetchall()]
            wanted = set(seed_ids + callers + callees)
        else:
            wanted = set()
    else:
        # top-N most-connected symbols (god-nodes + their neighbours)
        rows = conn.execute(
            "SELECT s.id, COUNT(*) c FROM calls cl JOIN symbols s "
            "ON cl.dst_id = s.id "
            + ("WHERE cl.repo = ?" if repo else "")
            + " GROUP BY s.id ORDER BY c DESC LIMIT ?",
            ((repo, max_nodes // 2) if repo else (max_nodes // 2,))).fetchall()
        seed_ids = [r["id"] for r in rows]
        if seed_ids:
            ph = ",".join("?" for _ in seed_ids)
            neighbours = [r["id"] for r in conn.execute(
                f"SELECT DISTINCT s.id FROM calls c JOIN symbols s "
                f"ON c.src_id = s.id WHERE c.dst_id IN ({ph}) "
                f"AND c.confidence >= 0.5 LIMIT ?",
                seed_ids + [max_nodes // 2]).fetchall()]
            wanted = set(seed_ids + neighbours)
        else:
            wanted = set()

    if not wanted:
        wanted = set(r["id"] for r in conn.execute(
            "SELECT id FROM symbols LIMIT ?", (max_nodes,)).fetchall())

    # ── god-node set for highlighting ───────────────────────────────────
    god_rows = conn.execute(
        "SELECT s.id FROM calls c JOIN symbols s ON c.dst_id = s.id "
        + ("WHERE c.repo = ?" if repo else "")
        + " GROUP BY s.id ORDER BY COUNT(*) DESC LIMIT 10",
        (repo,) if repo else ()).fetchall()
    god_ids = {r["id"] for r in god_rows}

    # ── build nodes ─────────────────────────────────────────────────────
    ph_w = ",".join("?" for _ in wanted)
    sym_rows = conn.execute(
        f"SELECT id, name, qualified_name, path FROM symbols "
        f"WHERE id IN ({ph_w})", list(wanted)).fetchall()
    nodes = []
    for r in sym_rows:
        is_god = r["id"] in god_ids
        nodes.append({
            "id":         r["id"],
            "label":      r["name"][:30],
            "title":      f"{r['qualified_name']} ({r['path']})",
            "color":      "#f85149" if is_god else "#30363d",
            "origColor":  "#f85149" if is_god else "#30363d",
            "size":       16 if is_god else 8,
        })

    # ── build edges (only between included nodes) ───────────────────────
    edge_rows = conn.execute(
        f"SELECT src_id, dst_id, confidence FROM calls "
        f"WHERE src_id IN ({ph_w}) AND dst_id IN ({ph_w})",
        list(wanted) + list(wanted)).fetchall()
    edges = []
    for r in edge_rows:
        bucket = _bucket_of(r["confidence"] or 0.0)
        edges.append({
            "from":   r["src_id"],
            "to":     r["dst_id"],
            "color":  _BUCKET_COLORS[bucket],
            "bucket": bucket,
            "width":  1.5 if bucket == "EXTRACTED" else 1.0,
        })

    conn.close()

    # ── render HTML ─────────────────────────────────────────────────────
    # Inline vis-network (~630 KB) so the graph works fully offline. We ship
    # the bundle alongside the package so no network call is ever needed.
    bundle_path = Path(__file__).parent / "_vis_network.js"
    if bundle_path.exists():
        vis_bundle = bundle_path.read_text(encoding="utf-8")
    else:
        # Fallback: load from CDN (only if someone deleted the bundle file)
        vis_bundle = ('document.write(\'<script src="https://unpkg.com/'
                      'vis-network/standalone/umd/vis-network.min.js">'
                      '<\\/script>\')')

    title = f"DecisionGraph — {repo or Path(db_path).stem}"
    summary = (f"{len(nodes)} nodes · {len(edges)} edges · "
               f"{len(god_ids)} god-nodes")
    html = (_HTML_TEMPLATE
            .replace("__TITLE__",   title)
            .replace("__SUMMARY__", summary)
            .replace("__VIS_BUNDLE__", vis_bundle)
            .replace("__DATA__",    json.dumps({"nodes": nodes, "edges": edges})))

    Path(out_path).write_text(html, encoding="utf-8")
    return {
        "path":  str(out_path),
        "nodes": len(nodes),
        "edges": len(edges),
        "size_kb": round(len(html) / 1024, 1),
    }


def export_cypher(db_path: str, out_path: str,
                   repo: Optional[str] = None) -> dict:
    """Export to Neo4j Cypher load format. Same data, importable into Neo4j."""
    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    where = "WHERE repo = ?" if repo else ""
    args = (repo,) if repo else ()
    lines = ["// DecisionGraph code graph -> Cypher", ""]
    n = 0; m = 0
    for r in conn.execute(
        f"SELECT id, qualified_name, name, path, kind FROM symbols {where}",
        args).fetchall():
        n += 1
        # escape single quotes
        qn = (r['qualified_name'] or '').replace("'", "\\'")
        nm = (r['name'] or '').replace("'", "\\'")
        pa = (r['path'] or '').replace("'", "\\'")
        kd = (r['kind'] or '').replace("'", "\\'")
        lines.append(
            f"CREATE (s{r['id']}:Symbol {{id:{r['id']}, "
            f"qualified_name:'{qn}', name:'{nm}', path:'{pa}', kind:'{kd}'}})")
    lines.append("")
    for r in conn.execute(
        f"SELECT src_id, dst_id, confidence FROM calls "
        f"WHERE dst_id IS NOT NULL "
        + ("AND repo = ?" if repo else ""),
        args).fetchall():
        m += 1
        bucket = _bucket_of(r["confidence"] or 0.0)
        lines.append(f"MATCH (a:Symbol {{id:{r['src_id']}}}), "
                     f"(b:Symbol {{id:{r['dst_id']}}}) "
                     f"CREATE (a)-[:CALLS {{bucket:'{bucket}'}}]->(b)")
    conn.close()
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(out_path), "nodes": n, "edges": m}
