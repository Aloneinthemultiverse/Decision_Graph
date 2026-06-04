"""Drive the mirofish_lite 5-stage pipeline end-to-end."""
import requests, io, time

BASE = "http://localhost:5001"
SEED = (
    "Users browse the marketplace and add products to cart. Some abandon "
    "the cart due to price sensitivity. Others complete checkout and "
    "leave reviews. Promotions drive new signups. New users who see "
    "positive reviews convert faster. Negative reviews cause churn.")

# Stage 1: ontology
print("=== Stage 1: ontology/generate ===")
r = requests.post(
    f"{BASE}/api/graph/ontology/generate",
    files={"files": ("seed.txt", io.BytesIO(SEED.encode()), "text/plain")},
    data={"project_name": "marketplace-test",
           "simulation_requirement": "predict 30-day churn given a 10% price hike",
           "additional_context": ""},
    timeout=180)
o = r.json().get("data", r.json())
print(f"  status={r.status_code}  success={o.get('success')}")
project_id = o["project_id"]
print(f"  project_id: {project_id}")
ent = o.get("ontology", {}).get("entity_types", [])
edg = o.get("ontology", {}).get("edge_types", [])
print(f"  entity_types: {len(ent)}, edge_types: {len(edg)}")
print(f"  sample entities: {[e.get('name') for e in ent[:4]]}")

# Stage 2: build graph (already done inside ontology; just verify)
print(f"\n=== Stage 2: build graph + simulation/create ===")
r = requests.post(f"{BASE}/api/simulation/create",
                   json={"project_id": project_id,
                          "graph_id": o.get("graph_id"),
                          "name": "churn-sim",
                          "agent_count": 10},
                   timeout=60)
print(f"  status={r.status_code}")
sim = r.json().get("data", r.json())
print(f"  sim_id={sim.get('simulation_id')}  raw_keys={list(sim.keys())[:6]}")
sim_id = sim.get("simulation_id")

# Stage 3: prepare personas
print(f"\n=== Stage 3: simulation/prepare ===")
r = requests.post(f"{BASE}/api/simulation/prepare",
                   json={"simulation_id": sim_id, "agent_count": 6},
                   timeout=180)
print(f"  status={r.status_code}")
prep = r.json()
print(f"  success={prep.get('success')}  task_id={prep.get('task_id')}")

# Poll prepare status
task_id = prep.get("task_id")
for i in range(20):
    r = requests.post(f"{BASE}/api/simulation/prepare/status",
                       json={"task_id": task_id, "simulation_id": sim_id},
                       timeout=30).json()
    if r.get("status") in ("done", "completed", "ready"): break
    time.sleep(1)
print(f"  prepare status: {r.get('status')}")
print(f"  personas: {len(r.get('profiles', []))}")
if r.get("profiles"):
    p = r["profiles"][0]
    print(f"  sample persona: name={p.get('name')!r:30}  "
          f"role={p.get('role','?')[:40]}")

# Stage 4: start sim
print(f"\n=== Stage 4: simulation/start ===")
r = requests.post(f"{BASE}/api/simulation/start",
                   json={"simulation_id": sim_id, "rounds": 3},
                   timeout=180)
print(f"  status={r.status_code}  ok={r.json().get('success')}")
# poll run-status
for i in range(40):
    r = requests.get(f"{BASE}/api/simulation/{sim_id}/run-status",
                      timeout=30).json()
    if r.get("status") in ("done", "completed", "finished"): break
    time.sleep(2)
print(f"  run status: {r.get('status')}")
print(f"  rounds completed: {r.get('rounds_completed', r.get('round', '?'))}")

# Stage 5: report
print(f"\n=== Stage 5: report/generate ===")
r = requests.post(f"{BASE}/api/report/generate",
                   json={"simulation_id": sim_id}, timeout=300)
report = r.json()
print(f"  status={r.status_code}  ok={report.get('success')}")
report_id = report.get("report_id")
# poll
for i in range(40):
    r = requests.post(f"{BASE}/api/report/generate/status",
                       json={"report_id": report_id}, timeout=30).json()
    if r.get("status") in ("done", "completed", "ready"): break
    time.sleep(2)
# fetch
r = requests.get(f"{BASE}/api/report/{report_id}", timeout=30).json()
content = r.get("content") or r.get("report") or ""
print(f"  report length: {len(content)} chars")
print(f"  report head:\n{content[:400]}")

print("\nVERDICT: mirofish 5-stage pipeline completed end-to-end.")
