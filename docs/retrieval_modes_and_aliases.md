# Retrieval modes and tool aliases

**Audience:** the planner / ReAct prompt designers, MCP tool-agent maintainers,
and anyone wiring new skills that surface to the chat reasoning loop.

**TL;DR:** chat now exposes **three** retrieval tools. They map 1:1 to the
three call modes inside `retriever_backend.retrieve_for_chat`. The planner
can use the canonical names or any of the documented aliases — `react_loop._normalize_tool_name` canonicalizes at the dispatch boundary so downstream code only sees the canonical names.

---

## The three tools

| Canonical name | Method | Best for |
|---|---|---|
| **`search_corpus`** | **Hybrid BM25 ⊕ vector** via Reciprocal Rank Fusion (RRF), with the canonical (`n_hierarchical`) vs factual (`n_factual`) blend applied **after** fusion | Default. Most policy / process / definitional questions. |
| **`recall_search`** | **Vector-only**, no confidence floor, higher k (16). | Paraphrased queries with no keyword overlap; "what do we know about X" exploratory passes; agentic first-pass before a heavier retrieval round. |
| **`precision_search`** | **BM25-only**, exact-phrase boost, no semantic similarity. | Specific code / ID lookups (HCPCS, CPT, ICD-10, FL.UM.87, CP.MP.98); exact-phrase questions. |

**Default = `search_corpus`.** Plan to fall back to `recall_search` when the
hybrid returns nothing, and to `precision_search` when the user names a code
or policy ID verbatim.

---

## Aliases the planner can emit

Aliases exist because (a) different planner/critic prompts call the same idea
different things, and (b) we want to discover usage patterns. All aliases
funnel through `_normalize_tool_name` → canonical name.

### `search_corpus` accepts:
- `corpus`
- `corpus_search`
- `default_search`
- `hybrid_search`
- `hybrid`

### `recall_search` accepts:
- `lazy_corpus_search` (back-compat — old name kept indefinitely)
- `broad`
- `broad_search`
- `explore`
- `vector_search`
- `semantic_search`

### `precision_search` accepts:
- `exact`
- `exact_match`
- `keyword_search`
- `bm25_search`
- `bm25`
- `lookup`

Aliases are case- and whitespace-tolerant — `"  EXACT "` → `precision_search`.

---

## Adding a new alias

1. Append to `_TOOL_ALIASES` in `app/pipeline/react_loop.py`.
2. Add a one-line mention to the relevant block in
   `app/pipeline/tool_manifest.py` so the planner sees it in its system prompt.
3. Don't reuse aliases across different canonical names — each alias maps to
   exactly one canonical tool.

---

## How the modes route inside `retrieve_for_chat`

```
search_corpus (or any alias)
  → retrieve_for_chat(mode="corpus")
    → retriever_hybrid.retrieve_corpus_hybrid
      ├── BM25 arm (mobius-retriever.retrieve_bm25 → Postgres)
      └── Vector arm (published_rag_search.search_published_rag → Chroma)
      → RRF fusion → blend selection (n_hierarchical, n_factual)

recall_search (or alias)
  → retrieve_for_chat(mode="recall")
    → retriever_hybrid.retrieve_recall
      → vector arm only (Chroma; no confidence floor)

precision_search (or alias)
  → retrieve_for_chat(mode="precision")
    → retriever_hybrid.retrieve_precision
      → BM25 arm only (Postgres; same path as legacy fallback)
```

---

## Telemetry

Each call returns a telemetry dict (when `include_trace=True`) with:

| Key | Modes | Meaning |
|---|---|---|
| `mode` | all | `corpus_hybrid` / `corpus_recall` / `corpus_precision` |
| `k` | all | requested top_k |
| `arm_bm25_hits` | hybrid, precision | BM25 chunk count |
| `arm_vector_hits` | hybrid, recall | vector chunk count |
| `fused_count` | hybrid | post-RRF chunk count |
| `fusion_overlap` | hybrid | chunks surfaced by both arms |
| `bm25_ms` / `vector_ms` | hybrid | per-arm wall time |
| `total_ms` | all | end-to-end wall time |
| `arm_errors` | hybrid (when present) | `{arm_name: error_string}` if an arm raised |

Per-chunk provenance (hybrid only):
- `retrieval_arms`: `["bm25"]`, `["vector"]`, or `["bm25", "vector"]`
- `arm_ranks`: `{"bm25": 1, "vector": 4}` (1-indexed rank within each arm)
- `arm_scores`: `{"bm25": 0.83, "vector": 0.91}` (original match_score per arm)
- `rrf_score`: fused RRF score (monotonic with quality, NOT a [0,1] similarity)

This lets you answer questions like "did BM25 or vector find this chunk?" in
post-mortem analysis without extra queries.

---

## Testing

`scripts/test_hybrid_retrieval.py` covers:

1. Alias resolution (every documented alias → expected canonical).
2. precision_search returns hits on keyword-heavy queries.
3. recall_search returns hits on a paraphrase with zero keyword overlap.
4. Hybrid fuses both arms — no duplicate IDs, telemetry shows arm hit counts.
5. Canonical/factual blend (`n_hierarchical=2, n_factual=3`) is honored after fusion.
6. Unknown modes fall back gracefully to BM25 instead of raising.

Run:
```bash
export CHROMA_AUTH_TOKEN=$(gcloud secrets versions access latest \
  --secret=chroma-auth-token --project=mobius-os-dev)
export VERTEX_PROJECT_ID=mobius-os-dev
export CHROMA_HOST=34.170.243.161
export CHAT_RAG_DATABASE_URL='postgresql+psycopg2://postgres:<pw>@127.0.0.1:5433/mobius_chat'
./.venv/bin/python scripts/test_hybrid_retrieval.py
```

Exit code 0 = all pass. Run it after any change to the retrieval pipeline
(retriever_backend, retriever_hybrid, published_rag_search, alias map).

---

## When to call which

A short prompt the planner should internalize:

- The user names a **specific code or policy ID** → `precision_search`.
  *Examples: "What does CP.MP.98 say?", "Look up HCPCS H0036."*
- The user asks an **exact phrase** they expect to be in the corpus →
  `precision_search`.
- The user asks a **conceptual / paraphrased / 'what do we know about X'**
  question with no obvious keyword → `recall_search`.
- **Anything else** → `search_corpus` (the hybrid default).

When unsure, prefer `search_corpus` — the hybrid degrades gracefully to
whichever arm has hits.
