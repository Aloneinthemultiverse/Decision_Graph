import re
from . import config
from .query import beam_query, multi_graph_beam_query, check_uncertainty
from .decisions import DecisionMemory


def _check_uncertainty_across_graphs(question: str, graphs_list: list, embed_model,
                                      threshold: float = 0.3, enabled: bool = True):
    """If a graph is available and EVERY graph reports uncertain, return a
    refusal payload. If at least one graph is confident, return None and the
    caller proceeds normally. With no graphs available (e.g., normal_mode),
    or with `enabled=False`, skips the check entirely.
    """
    if not enabled:
        return None    # gate disabled — never refuse
    if not graphs_list or embed_model is None:
        return None
    best_conf = 0.0
    best_msg, best_suggestion = None, None
    for g in graphs_list:
        u = check_uncertainty(
            question,
            g.get("community_embeddings"),
            g.get("community_ids"),
            g.get("community_summaries"),
            embed_model,
            threshold,
        )
        if not u["uncertain"]:
            return None    # at least one graph is confident → proceed
        if u["confidence"] > best_conf:
            best_conf = u["confidence"]
            best_msg = u.get("message")
            best_suggestion = u.get("suggestion")
    return {
        "uncertain": True,
        "confidence": best_conf,
        "message": best_msg or "I don't have enough context to answer confidently.",
        "suggestion": best_suggestion or "Consider ingesting more documents on this topic.",
    }


def _format_uncertain_reply(u: dict) -> str:
    """Build a user-facing string for a refusal. Callers return this in place
    of an LLM answer so the chat/query UI shows it as the agent's response."""
    return (f"[UNCERTAIN — confidence {u['confidence']:.2f}] {u['message']}\n\n"
            f"Suggestion: {u['suggestion']}")


def _normalize_graphs(graphs, G, community_summaries, community_ids, community_embeddings):
    """If a `graphs` list isn't passed explicitly, wrap the old single-graph args
    into a one-element list so internally we always run multi-graph beam search."""
    if graphs:
        return graphs
    if G is None:
        return []
    return [{
        "name": "Knowledge Graph",
        "G": G,
        "community_summaries": community_summaries or {},
        "community_ids": community_ids or [],
        "community_embeddings": community_embeddings,
    }]


def _flatten_communities(hit_report: dict) -> list:
    """Flatten the per-graph hit report into a tag-qualified list of community ids
    that's safe to pickle for decision-memory persistence."""
    out = []
    for name, info in (hit_report or {}).items():
        for cid in info.get("communities", []):
            out.append(f"{name}:{cid}")
    return out


def summarize_reasoning(reasoning_steps: list, client) -> str:
    if not reasoning_steps:
        return "Direct answer from context."

    reasoning_text = " | ".join(reasoning_steps[:5])
    response = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=80,
        messages=[{"role": "user", "content": f"""Summarize this reasoning chain in ONE sentence, max 15 words.
Just the sentence, nothing else.

Reasoning: {reasoning_text}"""}]
    )
    raw = next((b.text.strip() for b in response.content if b.type == "text" and b.text.strip()), "")
    return raw if raw else "Applied relevant knowledge to reach conclusion."


def normal_mode(
    question: str,
    client,
    memory: DecisionMemory,
    embed_model
) -> str:
    """Mode 1 — past decisions + LLM own reasoning. No knowledge graph."""
    print("Normal mode — past decisions + reasoning...")
    past = memory.query(question, embed_model)

    past_context = ""
    if past:
        past_context = "\nRELEVANT PAST DECISIONS:\n"
        for p in past:
            past_context += f"- Previous Q: {p['question'][:100]}\n"
            past_context += f"  Reasoning: {p['reasoning_summary']}\n"
            past_context += f"  Answer: {p['answer'][:200]}\n\n"
        past_context += "Use these past decisions as context. Build on them.\n"
    else:
        print("  No past decisions found.")

    response = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=3000,
        messages=[{"role": "user", "content": f"""You are an expert professional assistant.
{past_context}
Answer this question using past decisions as context and your own reasoning:
{question}"""}]
    )

    answer = next(
        (b.text.strip() for b in response.content if b.type == "text" and b.text.strip()),
        "No answer generated."
    )

    memory.store(
        question=question,
        answer=answer,
        reasoning_summary="Normal mode — past decisions + own reasoning",
        communities_used=[],
        context_triples=[]
    )
    memory.save()
    return answer


def session_mode(
    question: str,
    client,
    G=None,
    community_summaries: dict = None,
    community_ids: list = None,
    community_embeddings=None,
    embed_model=None,
    memory: DecisionMemory = None,
    company_metadata: dict = None,
    graphs: list = None,    # ← new: pass multiple graphs for multi-graph beam
    uncertainty_enabled: bool = False,
    uncertainty_threshold: float = 0.3,
) -> str:
    """Mode 2 — graph context + decision memory. Single LLM call, no ReAct loop.

    Pass either:
      - graphs=[{"name", "G", "community_summaries", "community_ids", "community_embeddings"}, ...]
      - or the legacy single-graph args (G, community_summaries, ...) for backward compatibility.
    """
    print("Session mode — multi-graph context + past decisions...")

    # uncertainty gate — refuse rather than hallucinate when all graphs lack relevant content
    graphs_list = _normalize_graphs(graphs, G, community_summaries, community_ids, community_embeddings)
    refusal = _check_uncertainty_across_graphs(question, graphs_list, embed_model,
                                                threshold=uncertainty_threshold,
                                                enabled=uncertainty_enabled)
    if refusal is not None:
        print(f"  UNCERTAIN ({refusal['confidence']:.2f}) — refusing to answer.")
        return _format_uncertain_reply(refusal)

    # get past decisions
    past = memory.query(question, embed_model)
    past_context = ""
    if past:
        past_context = "\nRELEVANT PAST DECISIONS:\n"
        for p in past:
            past_context += f"- {p['question'][:80]}: {p['answer'][:150]}\n"
    context_triples, hit_report = multi_graph_beam_query(question, graphs_list, embed_model)
    matched = _flatten_communities(hit_report)
    for name, info in hit_report.items():
        if info.get("skipped"):
            print(f"  [{name}] skipped: {info['skipped']}")
        else:
            print(f"  [{name}] {len(info['communities'])} communities, {info['triples']} unique triples")
    kg_context = "\n".join(context_triples[:30])

    # company metadata context
    company_context = ""
    if company_metadata:
        company_context = f"""
COMPANY CONTEXT:
Company: {company_metadata.get('name', '')}
Documents ingested: {company_metadata.get('documents_ingested', 0)}
Categories: {company_metadata.get('categories', {})}
"""
        # Task 5 — append cross-company wisdom if present
        wisdom = company_metadata.get("wisdom")
        if wisdom:
            company_context += f"\nCROSS-COMPANY WISDOM:\n{wisdom}\n"

    response = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=3000,
        messages=[{"role": "user", "content": f"""You are an expert assistant with access to company knowledge.
{company_context}
KNOWLEDGE GRAPH CONTEXT:
{kg_context}
{past_context}
Answer this question using all available context above:
{question}"""}]
    )

    answer = next(
        (b.text.strip() for b in response.content if b.type == "text" and b.text.strip()),
        "No answer generated."
    )

    # store decision
    memory.store(
        question=question,
        answer=answer,
        reasoning_summary="Session mode — company data + decision memory",
        communities_used=matched,
        context_triples=context_triples[:10]
    )
    memory.save()

    return answer


def react_agent(
    question: str,
    client,
    G=None,
    community_summaries: dict = None,
    community_ids: list = None,
    community_embeddings=None,
    embed_model=None,
    memory: DecisionMemory = None,
    max_steps: int = None,
    graphs: list = None,    # ← new: multi-graph beam during ReAct loop
    uncertainty_enabled: bool = False,
    uncertainty_threshold: float = 0.3,
) -> str:
    max_steps = max_steps or config.MAX_STEPS
    graphs_list = _normalize_graphs(graphs, G, community_summaries, community_ids, community_embeddings)

    # uncertainty gate — refuse before burning ReAct steps when no graph is relevant
    refusal = _check_uncertainty_across_graphs(question, graphs_list, embed_model,
                                                threshold=uncertainty_threshold,
                                                enabled=uncertainty_enabled)
    if refusal is not None:
        print(f"  UNCERTAIN ({refusal['confidence']:.2f}) — refusing to answer.")
        return _format_uncertain_reply(refusal)

    # check past decisions
    print("Checking past decisions...")
    past = memory.query(question, embed_model)

    past_context = ""
    if past:
        past_context = "\n\nRELEVANT PAST DECISIONS:\n"
        for p in past:
            past_context += f"- Previous question: {p['question'][:100]}\n"
            past_context += f"  Reasoning: {p['reasoning_summary']}\n"
            past_context += f"  Key findings: {p['answer'][:200]}\n\n"
        past_context += "Use these past findings as a starting point. Build on them.\n"
    else:
        print("  No relevant past decisions.")

    messages = [{"role": "user", "content": f"""You are an expert professional assistant with deep domain knowledge.
Answer ONLY in English.
{past_context}
You have ONE tool: query_graph("search term")
Use it to retrieve relevant context from the knowledge graph.
After sufficient context write your complete ANSWER:

QUESTION:
{question}

Start with THINK:"""}]

    queries_made = 0
    all_communities_used = []
    all_context_used = []
    reasoning_steps = []

    for step in range(max_steps):
        response = client.messages.create(
            model=config.LLM_MODEL,
            max_tokens=config.MAX_TOKENS,
            messages=messages
        )

        text_blocks = [b for b in response.content if b.type == "text" and b.text.strip()]
        if not text_blocks:
            messages.append({"role": "assistant", "content": "..."})
            messages.append({"role": "user", "content": "Continue. Write THINK: then ACT: or ANSWER:"})
            continue

        raw = text_blocks[-1].text.strip()
        print(f"\nStep {step+1}:\n{raw[:200]}...\n")

        for line in raw.split('\n'):
            if line.startswith('THINK:'):
                reasoning_steps.append(line.replace('THINK:', '').strip()[:100])

        if "ACT: query_graph" in raw:
            # Accept double quotes, single quotes, or backticks around the arg.
            matches = re.findall(
                r'query_graph\(\s*[\"\'`]([^\"\'`]+)[\"\'`]\s*\)', raw)
            observations = []

            for search_term in matches:
                context_triples, hit_report = multi_graph_beam_query(
                    search_term, graphs_list, embed_model
                )
                queries_made += 1
                all_communities_used.extend(_flatten_communities(hit_report))
                all_context_used.extend(context_triples)

                if context_triples:
                    observations.append(f"Results for '{search_term}':\n" + "\n".join(context_triples))
                else:
                    observations.append(f"No results for '{search_term}'")

            observation = "\n\n".join(observations)
            force = "\n\nYou have enough context. Write ANSWER: now." if queries_made >= 3 else ""

            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"OBSERVATION:\n{observation}{force}"})

        elif "ANSWER:" in raw:
            answer = raw.split("ANSWER:")[-1].strip()
            reasoning_summary = summarize_reasoning(reasoning_steps, client)

            memory.store(
                question=question,
                answer=answer,
                reasoning_summary=reasoning_summary,
                communities_used=list(set(all_communities_used)),
                context_triples=all_context_used[:20]
            )
            memory.save()

            print("\n" + "="*50)
            print("FINAL ANSWER:")
            print("="*50)
            print(answer)
            return answer

    # Force-final-answer path: agent ran out of steps without saying ANSWER:.
    # Be assertive — explicitly tell the LLM to drop the THINK/ACT framing.
    messages.append({"role": "user", "content":
        "Stop the THINK/ACT framing. Using everything you've learned, write "
        "a single comprehensive final answer to the user's question. Do NOT "
        "include 'THINK:', 'ACT:', or 'ANSWER:' prefixes. Just the answer."})
    response = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=config.MAX_TOKENS,
        messages=messages
    )
    raw = next((b.text for b in response.content if b.type == "text" and b.text.strip()), "")

    # Defensive cleanup: even if the LLM ignored us, strip leftover prefixes
    # so users never see 'THINK: ... ACT: query_graph(...)' as their answer.
    cleaned = raw
    if "ANSWER:" in cleaned:
        # take only what's after the LAST ANSWER:
        cleaned = cleaned.split("ANSWER:")[-1].strip()
    else:
        # drop any leading THINK:/ACT: lines
        import re as _re
        cleaned = _re.sub(r'^(THINK:|ACT:[^\n]*\n)', '', cleaned, flags=_re.M)
        cleaned = "\n".join(
            ln for ln in cleaned.split("\n")
            if not ln.strip().startswith(("THINK:", "ACT:"))
        ).strip()
    # If after stripping we have nothing useful, fall back to the raw text
    # rather than returning empty.
    if len(cleaned) < 30:
        cleaned = raw.strip() or "(no answer generated)"

    reasoning_summary = summarize_reasoning(reasoning_steps, client)
    memory.store(
        question=question,
        answer=cleaned,
        reasoning_summary=reasoning_summary,
        communities_used=list(set(all_communities_used)),
        context_triples=all_context_used[:20]
    )
    memory.save()
    print("\nFINAL ANSWER:\n", cleaned)
    return cleaned
