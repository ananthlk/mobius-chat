# Follow-up Question Continuity Plan

## Problem

When a user continues on the same topic (e.g., clicks "What information is needed to file a standard appeal?" after getting an answer about Sunshine Health appeals), retrieval often returns **fewer or less relevant** documents because:

1. **Query loses context** – The follow-up question "what information is needed to file a standard appeal" is sent to retrieval as-is, without "Sunshine Health" or "appeals process," so BM25 may not retrieve the same relevant docs.
2. **Fresh retrieval** – Each turn does a fresh retrieval; previous turn's sources are not reused.

## Two Approaches

### Option A: Better Query Refinement (Recommended First)

Inject conversation context into the **retrieval question** so that follow-ups are scoped to the same topic and jurisdiction.

**Flow:**
```
User: "What information is needed to file a standard appeal?"
Context: last_refined_query = "What is the general process for filing a healthcare appeal for Sunshine Health"
         jurisdiction = Sunshine Health
         last_turn topic = appeals
Retrieval question: "What information is needed to file a standard appeal with Sunshine Health"
```

**Changes:**

1. **`reframe_for_retrieval`** (`app/state/query_refinement.py`)
   - Add optional params: `last_refined_query`, `jurisdiction_summary`, `is_followup`
   - When it's a follow-up and we have last_refined_query/jurisdiction: merge jurisdiction into the question (e.g., append " with Sunshine Health" or " for Sunshine Health")
   - When last_refined_query contains key terms (payor, topic), ensure they're in the reframed query

2. **`build_blueprint`** (`app/planner/blueprint.py`)
   - Accept `ctx` or `retrieval_context` from `run_plan`
   - Pass `last_refined_query`, jurisdiction summary to `reframe_for_retrieval`

3. **Decompose prompt** (`config/prompts_llm.yaml`, `chat_config.py`)
   - Add explicit instruction: *"When the user asks a follow-up that continues the same topic (e.g., after an answer about appeals), include the jurisdiction (payor, state) and topic from the previous turn in the subquestion text so retrieval finds the right documents."*

4. **Detect follow-up** (`app/state/refined_query.py` or `classify.py`)
   - When `classification == "new_question"` but we have `last_refined_query` and the message looks like a follow-up (same thread, question references same domain), treat as "continuation" for retrieval purposes
   - Heuristic: user message is a full question (has "what", "how") and last turn had a substantive answer on the same topic

### Option B: Include Previous Documents

Pass the **previous turn's sources** into retrieval so they are always available as context.

**Flow:**
```
Turn 1: retrieve("appeals for Sunshine Health") → 4 chunks (Provider Manual, Member Handbook, ...)
Turn 2: retrieve("what information to file standard appeal") 
        + previous_document_ids = [doc1, doc2, doc3, doc4]
        → ensure those docs are included or used as seed
```

**Challenges:**

1. **Storage** – `chat_turns.sources` stores `document_id`, `document_name`, `page_number` but not full chunk text. We'd need either:
   - Store chunk text in persistence (heavy), or
   - Re-fetch chunks by `document_id` + `page_number` from the RAG DB (requires new endpoint or chat-side logic)

2. **Retriever API** – `retrieve_for_chat` and the RAG API do not currently accept `include_document_ids` or `previous_chunk_ids`. Would require:
   - `mobius-rag-api`: add `include_document_ids: list[str]` to `/retrieve` request
   - `mobius-retriever`: ensure those docs are included in the result (e.g., fetch by ID and merge with BM25 results)

3. **Merge strategy** – How to combine: (a) union previous + new, (b) previous as seed with new as supplement, (c) rerank union.

**Implementation sketch (future):**
- Add `get_last_turn_sources(thread_id)` in `app/storage/turns.py` (query `chat_turns` for most recent turn with sources, return `document_id` list)
- Add `previous_document_ids` to `retrieve_for_chat` and RAG API payload
- In retriever: fetch those docs by ID, merge with BM25 results, dedupe

---

## Recommended Implementation Order

1. **Phase 1: Query refinement (Option A)**
   - Update decompose prompt for follow-up continuity
   - Extend `reframe_for_retrieval` to accept and use `last_refined_query` + jurisdiction
   - Wire `build_blueprint` to pass context from `run_plan`

2. **Phase 2 (if needed): Previous documents (Option B)**
   - Add `get_last_turn_sources`
   - Extend RAG API and retriever to support `include_document_ids`
   - Merge previous docs with new retrieval in resolve/doc_assembly

---

## Key Files

| Area | Files |
|------|-------|
| Query refinement | `app/state/query_refinement.py`, `app/planner/blueprint.py` |
| Parser context | `app/state/context_pack.py`, `app/stages/plan.py` |
| Prompts | `config/prompts_llm.yaml`, `app/chat_config.py` |
| Retrieval | `app/services/retriever_backend.py`, `app/stages/resolve.py` |
| Previous sources | `app/storage/turns.py`, `app/storage/threads.py` |
