from dotenv import load_dotenv
load_dotenv()

import sys
sys.path.insert(0, ".")

print("=" * 60)
print("TEST 1: Basic DecisionGraph load + stats")
print("=" * 60)
from decisiongraph import DecisionGraph
dg = DecisionGraph()
stats = dg.stats()
print(f"Nodes: {stats['nodes']}, Edges: {stats['edges']}, Communities: {stats['communities']}, Decisions: {stats['decisions']}")

print()
print("=" * 60)
print("TEST 2: CompanyMemory / EnterpriseHub")
print("=" * 60)
from decisiongraph.company import EnterpriseHub

hub = EnterpriseHub()

# add a company
acme = hub.add_company("acme_001", "Acme Corp")
print(f"Company created: {acme.company_name}")

# check stats
acme_stats = acme.stats()
print(f"Acme stats: {acme_stats}")

# list companies
companies = hub.list_companies()
print(f"Total companies in hub: {len(companies)}")
for c in companies:
    print(f"  - {c['name']} ({c['id']})")

print()
print("=" * 60)
print("TEST 3: document_handlers — TXT ingestion via CompanyMemory")
print("=" * 60)
import tempfile, os

# write a small temp txt file
sample_text = """Q3 2024 Decision: Vendor Selection
The board approved migrating to AWS from on-premise. Key reasons:
1. Cost reduction: estimated 30% savings over 3 years
2. Scalability: auto-scaling for peak loads
3. Security: SOC 2 Type II compliance

Action items: Notify current vendor by Oct 1. Begin migration January 2025."""

with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
    f.write(sample_text)
    tmp_path = f.name

try:
    doc_id = acme.ingest(tmp_path, category="DECISIONS")
    print(f"Ingested doc_id: {doc_id}")
    docs = acme.list_documents()
    print(f"Documents in registry: {len(docs)}")
    for d in docs:
        print(f"  - [{d['category']}] {d['filename']} (id={d['id'][:40]}...)")
finally:
    os.unlink(tmp_path)

print()
print("=" * 60)
print("TEST 4: document_handlers.read_document for CSV")
print("=" * 60)
from decisiongraph.document_handlers import read_document

csv_content = "name,role,department\nAlice,Engineer,Platform\nBob,Manager,Operations\nCarol,Analyst,Finance"
with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
    f.write(csv_content)
    csv_path = f.name

try:
    text = read_document(csv_path)
    print("CSV parsed output:")
    print(text)
finally:
    os.unlink(csv_path)

print()
print("All tests passed.")
