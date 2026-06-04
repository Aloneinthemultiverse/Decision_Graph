"""Slice-1 Step 2 proof: the agent sandbox is a real, network-less cage.

These tests need a running Docker daemon; they SKIP cleanly if it's down.
Run: python -m pytest test_agent_sandbox.py -q
"""
import os
import json
import textwrap
import tempfile
import subprocess

import pytest

from decisiongraph.agent_sandbox import (
    run_sandboxed, docker_available, SandboxConfig,
    SandboxError, SandboxUnavailable,
)

pytestmark = pytest.mark.skipif(
    not docker_available(), reason="docker daemon not available"
)


def _agent(body: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    f.write(textwrap.dedent(body))
    f.close()
    return f.name


def test_agent_runs_and_returns_json():
    a = _agent("""
        import json
        d = json.load(open("/sandbox/input.json"))
        print(json.dumps({"echo": d["task"], "saw_topics": list(d["data"].keys())}))
    """)
    try:
        out = run_sandboxed(a, {"task": "summarize", "data": {"pricing": "v1"}})
        assert out["ok"] is True
        assert out["result"]["echo"] == "summarize"
        assert out["result"]["saw_topics"] == ["pricing"]
        assert out["exit_code"] == 0
    finally:
        os.unlink(a)


def test_no_network_outbound_is_impossible():
    # agent tries to open a TCP socket to a public IP; --network none must
    # make this fail. Agent reports whether it could connect.
    a = _agent("""
        import json, socket
        ok = False
        try:
            s = socket.create_connection(("1.1.1.1", 53), timeout=3)
            s.close(); ok = True
        except Exception:
            ok = False
        print(json.dumps({"could_connect": ok}))
    """)
    try:
        out = run_sandboxed(a, {"task": "x", "data": {}})
        assert out["result"]["could_connect"] is False  # cage holds
    finally:
        os.unlink(a)


def test_host_filesystem_not_visible():
    # agent tries to read a host-only path; container fs is isolated so it
    # must not exist inside.
    a = _agent("""
        import json, os
        leaked = os.path.exists("/etc/hostname_HOST_MARKER")
        # /sandbox is the ONLY thing mounted, and read-only
        listing = sorted(os.listdir("/sandbox"))
        print(json.dumps({"leaked": leaked, "sandbox": listing}))
    """)
    try:
        out = run_sandboxed(a, {"task": "x", "data": {"k": "v"}})
        assert out["result"]["leaked"] is False
        assert set(out["result"]["sandbox"]) == {"agent.py", "input.json"}
    finally:
        os.unlink(a)


def test_readonly_rootfs():
    # writing to the container root must fail (--read-only); /tmp is the
    # only writable scratch.
    a = _agent("""
        import json
        root_writable = True
        try:
            open("/should_not_write.txt", "w").write("x")
        except Exception:
            root_writable = False
        tmp_ok = True
        try:
            open("/tmp/ok.txt", "w").write("x")
        except Exception:
            tmp_ok = False
        print(json.dumps({"root_writable": root_writable, "tmp_ok": tmp_ok}))
    """)
    try:
        out = run_sandboxed(a, {"task": "x", "data": {}})
        assert out["result"]["root_writable"] is False
        assert out["result"]["tmp_ok"] is True
    finally:
        os.unlink(a)


def test_hard_timeout_kills_runaway_agent():
    a = _agent("""
        import time
        time.sleep(30)
        print("{}")
    """)
    try:
        with pytest.raises(SandboxError) as ei:
            run_sandboxed(a, {"task": "x", "data": {}},
                          SandboxConfig(timeout_s=3))
        assert "timeout" in str(ei.value).lower()
    finally:
        os.unlink(a)


def test_nonzero_exit_is_an_error():
    a = _agent("""
        import sys
        sys.exit(7)
    """)
    try:
        with pytest.raises(SandboxError):
            run_sandboxed(a, {"task": "x", "data": {}})
    finally:
        os.unlink(a)


def test_container_is_destroyed_after_run():
    before = subprocess.run(
        ["docker", "ps", "-aq"], capture_output=True, text=True
    ).stdout.split()
    a = _agent('import json; print(json.dumps({"done": True}))')
    try:
        run_sandboxed(a, {"task": "x", "data": {}})
    finally:
        os.unlink(a)
    after = subprocess.run(
        ["docker", "ps", "-aq"], capture_output=True, text=True
    ).stdout.split()
    # --rm means our container left no trace (count must not grow)
    assert len(after) <= len(before)
