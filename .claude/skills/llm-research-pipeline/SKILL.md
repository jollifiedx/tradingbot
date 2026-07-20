---
name: llm-research-pipeline
description: The nightly slow path — Anthropic Batch API research runs, thesis prompts, pgvector research memory, thesis-outcome feedback loop, LLM cost logging. Use for work on the overnight research pipeline in backend/app/research/.
argument-hint: [pipeline task]
---

Research pipeline task: $ARGUMENTS

Invariant 1 is the ceiling: LLM output (theses, watchlist, conviction) is written to the
DB and STOPS there. No LLM call may exist in or influence the order path in-process.

Pipeline rules:
- Deep research: `claude-opus-4-8` via the Batch API (50% discount; overnight latency is
  free). Cheap tasks (classification, summarization): `claude-haiku-4-5`.
- Prompts structured for prompt caching: stable system prompt first, per-symbol content last.
- Before researching a symbol: retrieve its past theses AND recorded outcomes via pgvector
  similarity; the prompt must include what the bot previously got wrong about this symbol.
- Every LLM response parses into a validated Pydantic model (thesis text, conviction score,
  risk notes). Malformed output → dropped and logged, never best-effort written.
- Every call logged to `llm_calls`: model, input/output tokens, computed cost.
- When a trade closes, back-link the outcome (P&L, thesis-correct?) to the originating
  thesis row — this feedback loop is the "memory" feature.
- Ingested source documents carry true published-at timestamps (look-ahead hygiene).
