"""Live run-state emitter — the OS writes its progress to a JSON file as it builds
so the DG Mission Control dashboard can poll and render it in real time."""
from __future__ import annotations
import json, os, time, tempfile


class RunState:
    def __init__(self, path: str, task: str = "", model: str = ""):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.data = {
            "task": task, "model": model, "status": "dispatching",
            "started": time.time(), "updated": time.time(),
            "task_type": "", "cross_cutting": [], "subtasks": [],
            "code_graph": {"symbols": 0, "calls": 0}, "cag_files": 0,
            "scaffold": None, "self_correction": {"rounds": []},
            "boot_gate": {"status": "pending", "errors": 0, "detail": []},
            "site_url": "", "files": [],
        }
        self.flush()

    def flush(self):
        self.data["updated"] = time.time()
        self.data["elapsed_s"] = round(self.data["updated"] - self.data["started"], 1)
        # atomic write so the dashboard never reads a half-written file
        d = os.path.dirname(self.path)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    def set(self, **kw):
        self.data.update(kw); self.flush()

    def set_plan(self, task_type, cross_cutting, subtasks):
        self.data["task_type"] = task_type
        self.data["cross_cutting"] = cross_cutting
        self.data["subtasks"] = [{
            "id": s.get("id", i + 1), "step": s.get("step", ""),
            "section": s.get("section", ""), "title": s.get("title", ""),
            "agents": s.get("agents", []), "skills": s.get("skills", []),
            "rules": s.get("rules", []), "files": s.get("files", []),
            "builder": None, "status": "pending",
        } for i, s in enumerate(subtasks)]
        self.data["status"] = "building"
        self.flush()

    def subtask(self, idx, **kw):
        if 0 <= idx < len(self.data["subtasks"]):
            self.data["subtasks"][idx].update(kw); self.flush()

    def graph(self, symbols, calls):
        self.data["code_graph"] = {"symbols": symbols, "calls": calls}; self.flush()

    def cag(self, n):
        self.data["cag_files"] = n; self.flush()

    def add_round(self, rnd, errors, fixed, reverted=False):
        self.data["self_correction"]["rounds"].append(
            {"round": rnd, "errors": errors, "fixed": fixed, "reverted": reverted})
        self.flush()

    def boot(self, status, errors=0, detail=None):
        self.data["boot_gate"] = {"status": status, "errors": errors,
                                  "detail": detail or []}; self.flush()

    def done(self, site_url=""):
        self.data["status"] = "done"; self.data["site_url"] = site_url; self.flush()
