---
name: anythingllm-search
description: >-
  Read-only RAG search over an external AnythingLLM knowledge base scoped to Data & AI
  (data engineering, ML/AI, MLOps, LLMs, RAG, vector DBs, analytics). Use as an ADDITIONAL
  knowledge source when researching or SYNTHESIZING material on Data & AI topics — e.g.
  writing an article/briefing, answering a Data & AI question, comparing approaches, or
  enriching a draft with grounded references and citations. Search-only: it never uploads,
  embeds, edits or deletes documents. Do NOT use for non-Data&AI topics or for any write
  operation on AnythingLLM.
last_accessed: 2026-04-29
relevance: 0.33
tier: archive
type: note
---

# AnythingLLM Data & AI Search

Query an **external** AnythingLLM instance as a retrieval-augmented knowledge source. The
scope is restricted by design to a single workspace dedicated to **Data & AI**. This skill is
strictly **read-only** — use it to pull in relevant material, then synthesize the answer
yourself, citing the returned sources.

## When to reach for it

Use it as a *supplementary* source (alongside the vault, web, arxiv, etc.) whenever the task
touches Data & AI and would benefit from the curated knowledge base — for example: drafting or
enriching a briefing/article, comparing techniques, or grounding a claim. It is not the only
source: combine its results with other research. If the topic is unrelated to Data & AI, skip it.

## Setup (one-time)

The script reads connection settings from the repo-root `.env` only; environment variables are
ignored by design. Required keys (see `.env.example`):

| Variable | Meaning |
| --- | --- |
| `ANYTHINGLLM_BASE_URL` | Instance URL, no `/api` suffix, e.g. `https://anythingllm.example.com` |
| `ANYTHINGLLM_API_KEY` | Developer API key (Settings → API Keys in AnythingLLM) |
| `ANYTHINGLLM_WORKSPACE` | Slug of the Data & AI workspace (the URL slug, not the display name) |

Verify the connection before first use:

```bash
python skills/anythingllm-search/scripts/anythingllm_search.py --check
```

If it reports missing config, the `.env` file or required keys are absent — ask the user to fill
them in `.env`.

## Usage

Default mode is `search` (raw retrieval — preferred for synthesis, no service LLM tokens spent):

```bash
# Retrieve the most relevant chunks + their sources (default)
python .../scripts/anythingllm_search.py "feature store vs feature platform"

# Tune recall
python .../scripts/anythingllm_search.py "RAG evaluation metrics" --top 8
python .../scripts/anythingllm_search.py "vector index HNSW vs IVF" --threshold 0.6

# Grounded answer with citations (spends the service's LLM tokens; AnythingLLM answers
# strictly from the workspace documents in query mode)
python .../scripts/anythingllm_search.py "what is GraphRAG" --mode query

# Raw JSON (for programmatic use)
python .../scripts/anythingllm_search.py "llm distillation" --json
```

### Choosing a mode

- **`search` (default)** — returns the top relevant chunks with similarity scores and source
  titles/URLs. No generation happens on the AnythingLLM side. **Prefer this for synthesis**: you
  get the raw material and the citations, and you compose the result yourself.
- **`query`** — AnythingLLM generates a short answer grounded only in the workspace documents
  and returns its sources. Use for a quick grounded summary; it consumes the service's LLM tokens
  and may reply that it cannot answer if nothing relevant is embedded.

## How to use the results

1. Run a `search` with a focused query (rephrase and re-run with synonyms if recall is low —
   retrieval is sensitive to wording; 2–3 angled queries beat one).
2. Read the returned chunks; keep only those actually relevant to the task.
3. Synthesize your answer/draft from these chunks **plus** your other sources.
4. Attribute material to the source titles/URLs the search returned — do not present the
   workspace's content as your own and do not invent citations beyond what was returned.

## Guardrails

- **Read-only.** This skill exposes only search and grounded-query. Never use it (or other
  tools) to write to, embed into, or delete from AnythingLLM.
- **Topic scope.** Only for Data & AI. The workspace slug enforces the boundary; do not point it
  at unrelated workspaces.
- **Supplementary.** Treat it as one source among several, not ground truth. If a result looks
  off or stale, corroborate it elsewhere before relying on it.
- **No results ≠ false.** An empty result means nothing relevant is embedded for that query, not
  that the fact is wrong — broaden the query or fall back to other sources.
