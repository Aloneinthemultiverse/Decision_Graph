from dotenv import load_dotenv
load_dotenv()

from decisiongraph import DecisionGraph

dg = DecisionGraph()
print(f"Graph loaded: {dg.stats()}\n")

question = "What is multi-head attention and why does the Transformer use it instead of single attention?"

print("=" * 60)
print("MODE 1: NORMAL (past decisions + LLM reasoning, no graph)")
print("=" * 60)
answer_normal = dg.query(question, mode="normal")
print(answer_normal)

print("\n" + "=" * 60)
print("MODE 2: SESSION (graph + past decisions, single LLM call)")
print("=" * 60)
answer_session = dg.query(question, mode="session")
print(answer_session)

print("\n" + "=" * 60)
print("MODE 3: DEEP (graph + past decisions + ReAct loop)")
print("=" * 60)
answer_deep = dg.query(question, mode="deep")
print(answer_deep)

print("\n" + "=" * 60)
print("DECISIONS STORED:", dg.stats()["decisions"])
print("=" * 60)
