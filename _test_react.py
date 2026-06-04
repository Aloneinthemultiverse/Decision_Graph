"""Test ReAct deep query mode against an ingested graph."""
import os, time
os.environ["LLM_BASE_URL"] = "http://localhost:8080"
os.environ["LLM_API_KEY"] = "test"
from dotenv import load_dotenv; load_dotenv()

from decisiongraph.core import DecisionGraph

# Use the existing flask workspace which has a real graph
dg = DecisionGraph(storage_dir="storage/workspaces/p4jIEgrAJd33s3qH-DZbhQ/personal")
dg._ensure_graph_loaded()
print(f"graph: {dg.G.number_of_nodes() if dg.G else 0} nodes, "
      f"{dg.G.number_of_edges() if dg.G else 0} edges")

t = time.time()
print("\n=== mode='normal' (single-shot RAG) ===")
ans = dg.query("What is the relationship between AppContext and RequestContext "
                "in this codebase?", mode="normal")
print(f"  elapsed={time.time()-t:.1f}s  length={len(ans)} chars")
print(f"  answer (first 400): {ans[:400]}")

t = time.time()
print("\n=== mode='deep' (ReAct loop) ===")
ans = dg.query("Walk me through the request-handling lifecycle from the moment "
                "a request arrives to when the response is sent. Use multiple "
                "graph lookups if needed.", mode="deep")
print(f"  elapsed={time.time()-t:.1f}s  length={len(ans)} chars")
print(f"  answer (first 800): {ans[:800]}")
