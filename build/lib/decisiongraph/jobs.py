"""gbrain #6 — durable job queue (the "Minions" idea).

SQLite-backed so jobs SURVIVE a server restart. A worker thread drains the
queue; on startup any job left 'running' (process died mid-flight) is requeued
exactly once (replay-safety). Handlers are registered by job type.

Directly fixes the real pain we hit: long simulations ran in daemon threads
that vanished on restart (the orphaned 6-hour sim). Now they're rows in a DB.
"""
from __future__ import annotations
import os, json, time, sqlite3, threading, traceback
from datetime import datetime
from typing import Callable


class JobQueue:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._handlers: dict[str, Callable[[dict], dict]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._init_db()

    # ── schema ──
    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                result TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                workspace TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL)""")

    # ── registration ──
    def register(self, job_type: str, handler: Callable[[dict], dict]):
        self._handlers[job_type] = handler

    # ── enqueue ──
    def enqueue(self, job_type: str, payload: dict, workspace: str = "") -> int:
        now = datetime.now().isoformat()
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO jobs(type,payload,status,workspace,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (job_type, json.dumps(payload), "queued", workspace, now, now))
            return cur.lastrowid

    def get(self, job_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not r: return None
            d = dict(r)
            d["payload"] = json.loads(d["payload"] or "{}")
            d["result"] = json.loads(d["result"]) if d["result"] else None
            return d

    def list(self, workspace: str = None, limit: int = 50) -> list:
        with self._conn() as c:
            if workspace is not None:
                rows = c.execute("SELECT id,type,status,attempts,created_at,updated_at,error"
                                 " FROM jobs WHERE workspace=? ORDER BY id DESC LIMIT ?",
                                 (workspace, limit)).fetchall()
            else:
                rows = c.execute("SELECT id,type,status,attempts,created_at,updated_at,error"
                                 " FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def _set(self, job_id, **fields):
        fields["updated_at"] = datetime.now().isoformat()
        cols = ",".join(f"{k}=?" for k in fields)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id=?",
                      (*fields.values(), job_id))

    # ── replay safety: requeue jobs that were 'running' when we died ──
    def _recover(self):
        with self._lock, self._conn() as c:
            n = c.execute("UPDATE jobs SET status='queued',"
                          " updated_at=? WHERE status='running'",
                          (datetime.now().isoformat(),)).rowcount
        if n:
            print(f"[jobs] recovered {n} interrupted job(s) → requeued")

    # ── worker loop ──
    def _claim(self):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE status='queued'"
                          " ORDER BY id ASC LIMIT 1").fetchone()
            if not r:
                return None
            c.execute("UPDATE jobs SET status='running', attempts=attempts+1,"
                      " updated_at=? WHERE id=?",
                      (datetime.now().isoformat(), r["id"]))
            return dict(r)

    def _run_one(self, row):
        jid = row["id"]; jtype = row["type"]
        payload = json.loads(row["payload"] or "{}")
        handler = self._handlers.get(jtype)
        if handler is None:
            self._set(jid, status="failed", error=f"no handler for '{jtype}'")
            return
        try:
            result = handler(payload) or {}
            self._set(jid, status="done", result=json.dumps(result), error=None)
        except Exception as e:
            traceback.print_exc()
            self._set(jid, status="failed", error=str(e))

    def _loop(self):
        self._recover()
        while not self._stop.is_set():
            row = self._claim()
            if row is None:
                self._stop.wait(1.0)
                continue
            self._run_one(row)

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True, name="jobqueue")
        t.start()
        print(f"[jobs] worker started ({self.db_path})")

    def stop(self):
        self._stop.set()
