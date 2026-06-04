"""Stress test: 10 concurrent threads writing decisions to the SAME workspace.
Verifies no DB corruption and all writes land."""
import threading, time, os, sqlite3
from decisiongraph.workspace import Workspace

# Fresh workspace
ws_root = ".bench/concurrent_test_ws"
import shutil
if os.path.exists(ws_root): shutil.rmtree(ws_root)
ws = Workspace("concurrent_test", ws_root)
dg = ws.dg
print(f"baseline: {len(dg.memory.all_decisions())} decisions")

errors = []
write_count = 0
write_lock = threading.Lock()

def writer(tid: int, n: int):
    global write_count
    for i in range(n):
        try:
            dg.memory.store(
                question=f"[t{tid}] decision {i}",
                answer=f"Thread {tid} iteration {i}",
                reasoning_summary=f"smoke {tid}-{i}",
                communities_used=[], context_triples=[])
            with write_lock:
                write_count += 1
        except Exception as e:
            errors.append((tid, i, str(e)))

# 10 threads × 20 writes = 200 concurrent stores
N_THREADS, N_PER = 10, 20
t = time.time()
threads = [threading.Thread(target=writer, args=(tid, N_PER))
           for tid in range(N_THREADS)]
for th in threads: th.start()
for th in threads: th.join()
elapsed = time.time() - t

print(f"\n200 concurrent writes finished in {elapsed:.2f}s")
print(f"  successful writes: {write_count}")
print(f"  errors:            {len(errors)}")
for tid, i, e in errors[:3]:
    print(f"    t{tid}/{i}: {e[:80]}")

# Verify on disk
final = len(dg.memory.all_decisions())
print(f"\nfinal decisions on disk: {final}")
print(f"expected: {N_THREADS * N_PER}")

# Cold-restart and verify persistence
del dg, ws
ws2 = Workspace("concurrent_test", ws_root)
dg2 = ws2.dg
reload_count = len(dg2.memory.all_decisions())
print(f"cold-restart reload:     {reload_count}")

verdict = (len(errors) == 0
           and write_count == N_THREADS * N_PER
           and reload_count == N_THREADS * N_PER)
print(f"\nVERDICT: {'PASS' if verdict else 'FAIL'}")
