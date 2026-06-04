"""Concurrent decision writes shouldn't corrupt the workspace."""
import threading
from decisiongraph.workspace import Workspace


def test_200_concurrent_decision_writes(tmp_path):
    ws = Workspace("ct", str(tmp_path / "ws"))
    dg = ws.dg
    errors = []
    n_threads, n_per = 10, 20

    def writer(tid):
        for i in range(n_per):
            try:
                dg.memory.store(
                    question=f"[t{tid}] {i}",
                    answer=f"thread {tid} iteration {i}",
                    reasoning_summary=f"s{tid}{i}",
                    communities_used=[], context_triples=[])
            except Exception as e:
                errors.append((tid, i, str(e)))

    ts = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
    for t in ts: t.start()
    for t in ts: t.join()

    assert errors == []
    final = len(dg.memory.all_decisions())
    assert final == n_threads * n_per

    # Cold-restart persistence check
    ws2 = Workspace("ct", str(tmp_path / "ws"))
    assert len(ws2.dg.memory.all_decisions()) == n_threads * n_per
