"""Test mirofish via DG's MiroFishClient using its actual signatures."""
import time
from decisiongraph.simulation import MiroFishClient

c = MiroFishClient(base_url="http://localhost:5001", timeout=180)
print("=== health ===", c.health())

seed = ".bench/mirofish_seed.txt"
open(seed, "w", encoding="utf-8").write(
    "Customers visit our SaaS app weekly. Heavy users upgrade to paid. "
    "Light users churn after trial. Trial extension boosts conversion. "
    "Negative support tickets increase churn risk.")

# Stage 1
t = time.time()
o = c.generate_ontology(seed, "predict 90-day conversion",
                         project_name="saas-test")
print(f"S1 ontology  {time.time()-t:.1f}s  pid={o['project_id']}  "
      f"entities={len(o.get('ontology',{}).get('entity_types',[]))}")

# Stage 2
project_id = o["project_id"]
t = time.time()
task_id = c.build_graph(project_id)
for _ in range(30):
    s = c.task_status(task_id)
    if s.get("status") in ("done","completed","ready"): break
    time.sleep(1)
graph_id = s.get("graph_id") or s.get("result", {}).get("graph_id")
print(f"S2 build     {time.time()-t:.1f}s  status={s.get('status')}  "
      f"graph_id={graph_id}  status_keys={list(s.keys())}")
if not graph_id:
    # fall through with a synthetic id — many mirofish_lite endpoints don't strictly need it
    graph_id = "graph_" + project_id

# Stage 3
t = time.time()
sim_id = c.create_simulation(project_id, graph_id)
print(f"S3 create    {time.time()-t:.1f}s  sim_id={sim_id}")

# Stage 4: prepare
t = time.time()
p = c.prepare_simulation(sim_id)
for _ in range(60):
    ps = c.prepare_status(sim_id)
    if ps.get("status") in ("done","completed","ready"): break
    time.sleep(2)
print(f"S4 prepare   {time.time()-t:.1f}s  status={ps.get('status')}  "
      f"profiles={len(ps.get('profiles',[]))}")

# Stage 5: start
t = time.time()
c.start_simulation(sim_id)
for _ in range(60):
    rs = c.run_status(sim_id)
    if rs.get("status") in ("done","completed","finished"): break
    time.sleep(2)
print(f"S5 run       {time.time()-t:.1f}s  status={rs.get('status')}")

# Stage 6: report
t = time.time()
gen = c.generate_report(sim_id)
task_id = gen.get("task_id")
for _ in range(40):
    s = c.report_generate_status(task_id)
    if s.get("status") in ("done","completed","ready"): break
    time.sleep(2)
report_id = s.get("report_id")
rep = c.get_report(report_id)
content = rep.get("content") or rep.get("report","")
print(f"S6 report    {time.time()-t:.1f}s  length={len(content)} chars")
print(f"\n--- report preview ---\n{content[:600]}")
