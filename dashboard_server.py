"""DG Mission Control server — serves the dashboard AND launches builds.

Static http.server can't accept the Dispatch button's POST, so this adds a
/launch endpoint that spawns os_build.py in the background. Run:

    python dashboard_server.py        # serves on http://localhost:7000
"""
from __future__ import annotations
import json, os, subprocess, sys, urllib.request, urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.join(ROOT, "dashboard")
PORT = int(os.environ.get("DASH_PORT", "7000"))
_proc = {"p": None}


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DASH, **k)

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        # SEPARATE DG INSTANCE — read graph_clean.pkl / decision_graph.pkl
        # DIRECTLY off disk, bypassing the DG server's stale in-memory cache.
        # /disk/graph?ws=<token>&scope=knowledge|decisions
        if self.path.startswith("/disk/graph"):
            import urllib.parse as _up, pickle
            q = _up.parse_qs(_up.urlparse(self.path).query)
            ws = (q.get("ws") or ["vcoB-xTSn7OJfDLERszlxA"])[0]
            scope = (q.get("scope") or ["knowledge"])[0]
            base = os.path.join(ROOT, "storage", "workspaces", ws, "personal")
            try:
                nodes, edges = [], []
                if scope == "knowledge":
                    p = os.path.join(base, "graph_clean.pkl")
                    if os.path.exists(p):
                        G = pickle.load(open(p, "rb"))
                        ns = list(G.nodes())
                        nodes = [{"id": n, "label": str(n)[:24], "title": str(n),
                                  "color": "#c7bfff", "size": 12} for n in ns]
                        seen = set()
                        for u, v, d in G.edges(data=True):
                            rel = (d or {}).get("relation", "")
                            key = (u, v, rel)
                            if key in seen: continue
                            seen.add(key)
                            edges.append({"from": u, "to": v, "label": rel})
                elif scope == "decisions":
                    p = os.path.join(base, "decision_graph.pkl")
                    if os.path.exists(p):
                        d = pickle.load(open(p, "rb"))
                        G = d.get("graph") if isinstance(d, dict) else d
                        for nid, a in G.nodes(data=True):
                            q_text = (a or {}).get("question", str(nid))
                            nodes.append({"id": nid, "label": str(q_text)[:24],
                                          "title": str(q_text)[:160],
                                          "color": "#34D399", "size": 12})
                        for u, v, _ in G.edges(data=True):
                            edges.append({"from": u, "to": v})
                self._json({"nodes": nodes, "edges": edges,
                            "empty": len(nodes) == 0, "source": "disk",
                            "workspace": ws, "scope": scope})
            except Exception as e:
                self._json({"nodes": [], "edges": [], "error": f"{type(e).__name__}: {e}"}, 500)
            return

        # Same-origin proxy to the DG server (8000) so the dashboard can read
        # /api/graph etc. without CORS/cookie hassles. /dg/* -> :8000/*
        if self.path.startswith("/dg/"):
            target = "http://127.0.0.1:8000/" + self.path[4:]
            ws = self.headers.get("X-DG-WS", "vcoB-xTSn7OJfDLERszlxA")
            try:
                req = urllib.request.Request(target, headers={"Cookie": f"dg_ws={ws}"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    body = r.read()
                self.send_response(200)
                self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers(); self.wfile.write(body)
            except Exception as e:
                err = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
                self.send_response(502); self.send_header("Content-Type","application/json")
                self.send_header("Content-Length", str(len(err))); self.end_headers()
                self.wfile.write(err)
            return
        return super().do_GET()

    def do_POST(self):
        if self.path.rstrip("/") != "/launch":
            self.send_error(404); return
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        task = (body.get("task") or "").strip()
        if not task:
            self._json({"ok": False, "error": "empty task"}, 400); return
        if _proc["p"] and _proc["p"].poll() is None:
            self._json({"ok": False, "error": "a build is already running"}, 409); return
        env = dict(os.environ,
                   DISPATCH_MODEL=body.get("dispatch_model", os.environ.get("DISPATCH_MODEL", "gemini-2.5-flash")),
                   BUILD_MODEL=body.get("build_model", os.environ.get("BUILD_MODEL", "gemini-2.5-flash")),
                   PYTHONUNBUFFERED="1")
        log = open(os.path.join(ROOT, ".tmp", "dash_launch.log"), "w")
        _proc["p"] = subprocess.Popen([sys.executable, "-u", "os_build.py", task],
                                      cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        self._json({"ok": True, "pid": _proc["p"].pid})

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


if __name__ == "__main__":
    os.makedirs(os.path.join(ROOT, ".tmp"), exist_ok=True)
    print(f"DG Mission Control -> http://localhost:{PORT}  (Dispatch button is live)")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
