# DecisionGraph

**Memory OS for AI Agents**

DecisionGraph gives AI agents two types of persistent memory:

1. **Knowledge Memory** — Documents → Knowledge Graph → Queryable context
2. **Decision Memory** — Every agent decision stored as a graph node → Retrieved for similar future situations

## Quick Start

```python
from decisiongraph import DecisionGraph

dg = DecisionGraph()

# ingest documents
dg.ingest("path/to/document.pdf")

# ask questions
answer = dg.query("What are the key compliance requirements?")

# agent gets smarter with every question
```

## As an MCP Server

Any agent — Claude, Gemini, GPT — can use DecisionGraph as persistent memory via MCP.

```bash
python -m decisiongraph.mcp_server
```

### MCP Tools

- `query_knowledge(question)` — Search knowledge graph
- `store_decision(question, answer, reasoning)` — Save decision to memory
- `get_past_decisions(context)` — Retrieve similar past decisions
- `ingest_document(path)` — Add documents to knowledge base
- `get_stats()` — Knowledge graph statistics

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env with your API key
```

## Architecture

```
Documents (PDF)
    ↓
Triple Extraction (LLM)
    ↓
Knowledge Graph (NetworkX)
    ↓ entity resolution + community detection
3-Layer Hierarchy
    ↓
Beam Traversal (top-3 communities)
    ↓
Dynamic Context Assembly
    ↓
ReAct Agent (LLM reasoning)
    ↓
Decision Memory (persistent)
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| LLM_BASE_URL | LLM API base URL | https://api.anthropic.com |
| LLM_API_KEY | API key | required |
| LLM_MODEL | Model name | claude-sonnet-4-6 |
| EMBED_MODEL | Sentence transformer model | all-MiniLM-L6-v2 |
| STORAGE_DIR | Storage directory | ./storage |
