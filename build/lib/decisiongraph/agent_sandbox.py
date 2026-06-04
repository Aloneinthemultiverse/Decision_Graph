"""Slice-1 Step 2 — The sandbox.

Runs an external/hired agent's code inside a throwaway, network-less Docker
container. The container:

  * has NO network at all (`--network none`) — it physically cannot phone
    home, exfiltrate, or call any external API;
  * sees ONLY the scoped data slice the host already fetched through the
    Step-1 access gate (mounted read-only at /sandbox/input.json);
  * runs read-only with tmpfs scratch, capped memory/CPU, and a hard
    wall-clock timeout;
  * is `--rm` and force-killed on timeout — destroyed after the job.

Security model: the *host* performs the authorized, audited MCP read via
agent_access.AgentAccessStore BEFORE the container starts, and injects only
that approved slice. The agent never touches the network, so "no outbound
except the audited MCP channel" holds by construction — the only data that
ever crossed the wall was already gated and logged in Step 1.

This module is stdlib-only and degrades gracefully: if Docker is unavailable
`docker_available()` returns False and run_sandboxed raises SandboxUnavailable
(callers/tests can skip).
"""
from __future__ import annotations

import os
import json
import time
import shutil
import tempfile
import subprocess
from dataclasses import dataclass


class SandboxUnavailable(Exception):
    """Docker daemon not reachable / docker CLI missing."""


class SandboxError(Exception):
    """The sandboxed run failed (non-zero exit, timeout, bad output)."""


def docker_available() -> bool:
    """True iff the docker CLI exists AND the daemon answers."""
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


@dataclass
class SandboxConfig:
    image: str = "python:3.12-slim"
    timeout_s: int = 60          # hard wall-clock cap
    memory: str = "256m"
    cpus: str = "1.0"
    # pids limit blunts fork bombs
    pids_limit: int = 128


def run_sandboxed(
    agent_script: str,
    scoped_input: dict,
    cfg: SandboxConfig | None = None,
) -> dict:
    """Run `agent_script` (a path to a .py file) inside the cage.

    Contract for the agent script: it reads a JSON object from
    /sandbox/input.json (the host-approved scoped slice + task) and writes a
    JSON object to stdout. Anything on stdout that is not the final JSON line
    is ignored; the LAST non-empty stdout line must be valid JSON.

    Returns: {"ok": bool, "result": <agent json>, "duration_s": float,
              "exit_code": int, "stderr": str}
    Raises SandboxUnavailable if Docker is down, SandboxError on failure.
    """
    cfg = cfg or SandboxConfig()

    if not os.path.isfile(agent_script):
        raise SandboxError(f"agent script not found: {agent_script}")
    if not docker_available():
        raise SandboxUnavailable("docker daemon not reachable")

    work = tempfile.mkdtemp(prefix="dg_sbx_")
    try:
        # stage the agent + the host-approved scoped input into an isolated dir
        shutil.copyfile(agent_script, os.path.join(work, "agent.py"))
        with open(os.path.join(work, "input.json"), "w", encoding="utf-8") as f:
            json.dump(scoped_input, f)

        # the cage:
        #   --network none      : no outbound, no DNS, nothing
        #   --read-only         : immutable container fs
        #   --tmpfs /tmp        : the only writable scratch (capped)
        #   -v work:/sandbox:ro : agent + input, READ ONLY
        #   --memory/--cpus/--pids-limit : resource caps
        #   --cap-drop ALL      : no Linux capabilities
        #   --security-opt no-new-privileges
        #   --rm                : destroyed on exit
        cmd = [
            "docker", "run", "--rm",
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=64m",
            "--memory", cfg.memory,
            "--cpus", cfg.cpus,
            "--pids-limit", str(cfg.pids_limit),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-v", f"{work}:/sandbox:ro",
            "-w", "/sandbox",
            cfg.image,
            "python", "/sandbox/agent.py",
        ]

        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=cfg.timeout_s,
            )
        except subprocess.TimeoutExpired:
            # belt-and-suspenders: ensure nothing lingers
            _force_cleanup()
            raise SandboxError(
                f"sandbox exceeded {cfg.timeout_s}s hard timeout; killed")
        dur = round(time.time() - t0, 3)

        if proc.returncode != 0:
            raise SandboxError(
                f"sandbox exited {proc.returncode}: "
                f"{(proc.stderr or '').strip()[:500]}")

        result = _parse_last_json(proc.stdout)
        if result is None:
            raise SandboxError("agent produced no valid JSON on stdout")

        return {
            "ok": True,
            "result": result,
            "duration_s": dur,
            "exit_code": proc.returncode,
            "stderr": (proc.stderr or "").strip()[:1000],
        }
    finally:
        # destroy the staged dir no matter what
        shutil.rmtree(work, ignore_errors=True)


def _parse_last_json(stdout: str):
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def run_sandboxed_mcp(
    agent_script: str,
    gateway,                       # MCPGateway instance, holds scope + audit
    task_payload: dict | None = None,
    cfg: SandboxConfig | None = None,
    max_calls: int = 32,
) -> dict:
    """Run an MCP-speaking agent inside the sandbox.

    The agent reads/writes newline-delimited JSON-RPC 2.0 messages on
    stdin/stdout. The host owns those pipes, applies the scope gate via the
    gateway, and forwards approved tool calls to the real adapter. Network is
    `--network none` — the ONLY way out is the gateway, which is the host
    process holding the pipe.

    Returns {ok, final_answer, calls, duration_s, exit_code, stderr}.
    """
    cfg = cfg or SandboxConfig()
    if not os.path.isfile(agent_script):
        raise SandboxError(f"agent script not found: {agent_script}")
    if not docker_available():
        raise SandboxUnavailable("docker daemon not reachable")

    work = tempfile.mkdtemp(prefix="dg_mcp_")
    try:
        shutil.copyfile(agent_script, os.path.join(work, "agent.py"))
        with open(os.path.join(work, "task.json"), "w", encoding="utf-8") as f:
            json.dump(task_payload or {}, f)

        cmd = [
            "docker", "run", "--rm", "-i",       # -i: keep stdin open
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=64m",
            "--memory", cfg.memory,
            "--cpus", cfg.cpus,
            "--pids-limit", str(cfg.pids_limit),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-v", f"{work}:/sandbox:ro",
            "-w", "/sandbox",
            cfg.image,
            "python", "-u", "/sandbox/agent.py",  # -u: unbuffered stdio
        ]

        import subprocess as _sp
        import json as _j

        t0 = time.time()
        proc = _sp.Popen(cmd, stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE,
                         bufsize=1, text=True, encoding="utf-8")
        calls = 0
        try:
            while True:
                if time.time() - t0 > cfg.timeout_s:
                    proc.kill()
                    raise SandboxError(
                        f"sandbox exceeded {cfg.timeout_s}s hard timeout")
                line = proc.stdout.readline()
                if not line:
                    break                                  # agent exited
                line = line.strip()
                if not line:
                    continue
                try:
                    req = _j.loads(line)
                except _j.JSONDecodeError:
                    continue                               # ignore noise
                if not isinstance(req, dict) or req.get("jsonrpc") != "2.0":
                    continue

                resp = gateway.handle(req)
                proc.stdin.write(_j.dumps(resp) + "\n")
                proc.stdin.flush()
                calls += 1

                if calls > max_calls:
                    proc.kill()
                    raise SandboxError(
                        f"agent exceeded max MCP calls ({max_calls})")
                if gateway.final_answer is not None:
                    # agent called done(); give it a moment to exit cleanly
                    try:
                        proc.wait(timeout=5)
                    except _sp.TimeoutExpired:
                        proc.kill()
                    break
        finally:
            try: proc.stdin.close()
            except Exception: pass
            stderr = (proc.stderr.read() or "").strip()[:1000] if proc.stderr else ""
            try: proc.wait(timeout=5)
            except Exception:
                proc.kill()

        if gateway.final_answer is None:
            raise SandboxError("agent exited without calling done()")

        return {
            "ok": True,
            "final_answer": gateway.final_answer,
            "calls": calls,
            "duration_s": round(time.time() - t0, 3),
            "exit_code": proc.returncode,
            "stderr": stderr,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _force_cleanup():
    """Best-effort: remove any leftover dg-sandbox containers."""
    try:
        ids = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "ancestor=python:3.12-slim",
             "--filter", "status=running"],
            capture_output=True, text=True, timeout=10,
        ).stdout.split()
        for cid in ids:
            subprocess.run(["docker", "kill", cid],
                           capture_output=True, timeout=10)
    except Exception:
        pass
