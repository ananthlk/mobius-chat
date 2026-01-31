# Mobius Chat Server – Architecture (Updated)

## Decisions

- **RAG data**: Read-only access to the RAG database (no replica sync for now).
- **User validation**: Deferred. A separate module will handle validation later and pass results to all consumers; for now we get the basics right.
- **Patient RAG vs other RAG**: We are **not** doing patient RAG yet. We need an architecture that (1) parses requests into patient vs non-patient, (2) handles subquestions per stream, (3) combines responses – and for now **refuses/warns** on any patient-related part.

---

## 1. Patient vs Non-Patient Architecture

We treat every user request as potentially mixed: some parts may be about **patient-specific** data (future: patient RAG), others about **general/document** knowledge (current RAG: policies, facts, chunks).

### Flow (high level)

The pipeline has six steps: **(a)** decompose to subquestions (plan, e.g. LangChain/LangGraph), **(b)** answer all questions (patient path = warning only, non-patient path = read-only RAG), **(c)** combine to a draft answer, **(d)** critique (appropriateness, factual accuracy, tone), **(e)** retry loop until pass or max retries, **(f)** final responder. See **Section 2** for the full pipeline and schematic.

---

## 2. Full Pipeline: Plan → Answer → Combine → Critique → Final

End-to-end flow:

- **a) Decompose to subquestions (plan to answer)**  
  Build new: parse the user request and produce a **plan** (list of subquestions, each classified patient vs non-patient). Implement with **LangChain or LangGraph** (or equivalent) so the “plan” is an explicit step (e.g. a graph node that outputs the decomposition).

**What the planner knows (data boundaries)**  
The planner does **not** need to know the exact data we have (which documents, which facts, which payers/states). It only needs to know the **boundary**:

- **Patient** – we do *not* have access (no patient RAG yet). Anything that requires patient-specific data (e.g. “what did my doctor say”, “my medications”) is classified as patient; the patient path will always return the warning.
- **Non-patient** – we *might* have access (read-only RAG: policy, facts, chunks). The planner does not need a catalog of topics or documents; it just parses the question and classifies each subquestion as patient vs non-patient.

So: **the planner parses the question and decomposes into subquestions; it classifies each as patient or non-patient. The sub-modules (patient path, non-patient path) are responsible for answering or refusing.** The non-patient responder then does retrieval over the RAG; if there is no relevant context, it returns “no relevant information” or similar. “What data we have” is implicit in the responders—we do not feed the planner a list of available data. Optionally, you can give the planner a short system prompt like: “We can answer policy and document questions (non-patient). We cannot answer questions about the user’s own care, medications, or doctor (patient).” That is enough for classification; the planner does not need to know the actual RAG contents.

**Planner: generic + feedback loop; sub-modules: strict adherence**  
The planner can be **generic** (same logic for all domains) and **refined over time by its own feedback loop** (e.g. when the non-patient responder returns “no relevant information,” or when critique flags off-topic or poor decomposition—that signal can be used to improve decomposition or classification). The **sub-modules** (patient path, non-patient path) enforce **strict adherence**: they answer only the question they are given and only from the facts they retrieve (no hallucination, no off-topic). So: a generic planner that improves from feedback; strict responders that stay on-question and on-facts.

- **b) Answer all questions**  
  For each subquestion, use **patient path** (warning only for now) or **non-patient path** (read-only RAG). Each path returns an answer (or warning) per subquestion.

- **c) Combine to form an answer**  
  Merge all subquestion answers (and patient warnings) into one **draft answer**.

- **d) Critique module**  
  Evaluate the draft on:
  - **Appropriateness** – answers the request; no off-topic or inappropriate content.
  - **Factual accuracy** – grounded in retrieved context; no unsupported claims.
  - **Tone appropriate** – matches requested or default tone (e.g. professional, concise).  
  Output: **pass** or **refine** (with specific feedback: what to fix).

- **e) Retry loops**  
  If critique says **refine**, feed the feedback back into the pipeline: either **re-answer** (back to step b/c with refinement instructions) or **refine-in-place** (edit the draft using critique feedback). Re-run **Critique** after each attempt. Repeat until **pass** or **max retries** (then return best effort or a clear “could not satisfy critique” message).

- **f) Final responder**  
  Once critique passes (or max retries reached), format and return the **final answer** to the user.

### Full pipeline schematic

```mermaid
flowchart TB
  UserReq[User Request]

  subgraph a_plan [a) Plan - Decompose to subquestions]
    Parser[Parser: LangChain / LangGraph]
    Decomp[Subquestion decomposition]
    Classify[Patient vs non-patient per subquestion]
    Parser --> Decomp --> Classify
  end

  PatientSubQ[Patient subquestions]
  NonPatientSubQ[Non-patient subquestions]

  subgraph b_answer [b) Answer all questions]
    PatientPath[Patient path - warning only]
    NonPatientPath[Non-patient path - read-only RAG]
  end

  subgraph c_combine [c) Combine]
    Combiner[Combine draft answer]
  end

  subgraph d_critique [d) Critique module]
    Critique[Appropriateness, factual accuracy, tone]
  end

  Pass{Pass?}
  RetryCount{Retries left?}

  subgraph e_retry [e) Retry loop]
    Refine[Refine / re-answer with feedback]
  end

  subgraph f_final [f) Final responder]
    FinalResponder[Format and return final answer]
  end

  UserReq --> a_plan
  a_plan --> PatientSubQ
  a_plan --> NonPatientSubQ
  PatientSubQ --> PatientPath
  NonPatientSubQ --> NonPatientPath
  PatientPath --> Combiner
  NonPatientPath --> Combiner
  Combiner --> Critique
  Critique --> Pass
  Pass -->|Yes| FinalResponder
  Pass -->|No| RetryCount
  RetryCount -->|Yes| Refine
  Refine --> Combiner
  RetryCount -->|No| FinalResponder
  FinalResponder --> FinalResponse[Final Response]
```

### Steps

1. **Parse user request**  
   Single message may contain multiple intents (e.g. “What is eligibility for X and what did my doctor say about Y?”).

2. **Classify**  
   For the whole request and/or for each subquestion: **patient** vs **non-patient**.
   - **Patient**: anything that should be answered from patient-specific data (e.g. “what did my doctor say”, “my medications”, “my last visit”). For now we do **not** have patient RAG.
   - **Non-patient**: policy, eligibility, document facts, general knowledge – answered from the existing RAG (read-only).

3. **Subquestion decomposition**  
   Split the request into subquestions; each subquestion is classified as patient or non-patient.  
   Example:  
   - “What is eligibility for Medicaid in CA and what did my doctor say about my condition?”  
   - → SubQ1 (non-patient): “What is eligibility for Medicaid in CA?”  
   - → SubQ2 (patient): “What did my doctor say about my condition?”

4. **Route**  
   - **Patient subquestions** → Patient handler. **Current behavior**: do **not** answer from patient data; return a **clear warning/refusal** (e.g. “Patient-specific answers are not available yet; we can only answer policy and document questions.”).  
   - **Non-patient subquestions** → RAG chat (read-only RAG DB: semantic + facts, optional critique, etc.).

5. **Combine**  
   Merge answers and warnings into one response:  
   - Non-patient parts: RAG answers.  
   - Patient parts: warning only.  
   Format can be a single message (e.g. “For your policy question: … For the part about your care: we don’t support patient-specific answers yet.”) or structured (e.g. by subquestion).

### Why this design

- **Safe**: No patient RAG yet ⇒ no risk of leaking or hallucinating patient data; we explicitly refuse patient questions.
- **Extensible**: When you add patient RAG, you only add a real **Patient RAG handler** and keep the same parser → classify → route → combine pipeline.
- **Mixed queries**: Users can ask one message that mixes policy and patient; we handle both sides correctly (answer policy, warn on patient).
- **Clear boundaries**: Parser/classifier is the single place that decides “patient vs non-patient”; all consumers get the same contract.

### What we build (pipeline components)

- **a) Parser (plan to answer)**  
  - Input: user request (raw message).  
  - Does: decompose to subquestions; classify patient vs non-patient per subquestion.  
  - Output: **patient subquestions** and **non-patient subquestions** (the “plan”).  
  - Build new; use **LangChain or LangGraph** so the plan is an explicit step (e.g. graph node).

- **b) Answer**  
  - **Patient path**: warning only (no patient RAG yet).  
  - **Non-patient path**: read-only RAG retrieval (semantic + facts) + answer generation per subquestion.

- **c) Combine**  
  - Merge all subquestion answers and patient warnings into one **draft answer**.

- **d) Critique module**  
  - Evaluate draft: **appropriateness** (on-topic, appropriate), **factually accurate** (grounded in context), **tone appropriate**.  
  - Output: **pass** or **refine** (with feedback on what to fix).

- **e) Retry loops**  
  - If critique says refine: re-answer or refine-in-place using feedback; re-run critique.  
  - Repeat until **pass** or **max retries**; then proceed with best effort or “could not satisfy critique”.

- **f) Final responder**  
  - Format and return the **final answer** to the user.

---

## 3. Read-Only RAG Module

- Chat server uses a **read-only** connection to the RAG database (same schema: documents, chunks, facts, chunk_embeddings).
- No writes from the chat service to RAG tables.
- Reuse existing RAG schema and retrieval (pgvector semantic, facts filters) from the read-only client.
- “Production vs replica” can be a single config (e.g. `RAG_DATABASE_URL`) for now; no replica sync.

---

## 4. User Validation (Deferred)

- No auth/user validation in the chat server for this phase.
- A future **validation module** will handle identity and pass a validated context to all consumers (including the chat server). The chat server will then accept that context (e.g. in the request payload) and use it for logging/rate-limiting only at first.
- For now: focus on request parsing, patient vs non-patient routing, read-only RAG, and response combination.

---

## 5. Rest of the Stack (Unchanged)

- **Queue-based worker**: Read from request queue, write to response queue (e.g. GCP Pub/Sub); correlation ID for request–response.
- **Retrieval**: Semantic (pgvector) + facts-based; router chooses strategy per (non-patient) subquestion.
- **Optional**: Critique, formatting, configurable parameters (request + server config).
- **Metrics**: Store per-request metrics (e.g. latency, strategy, patient vs non-patient) in chat DB; no user validation yet.
- **Deployment**: GCP, same shared infra as Mobius RAG; chat service has its own DB (e.g. `mobius_chat`) for metrics and optional response lookup.

---

## 6. Summary

| Step | Component | Choice |
|------|-----------|--------|
| **a** | Plan | Decompose to subquestions (LangChain / LangGraph); patient vs non-patient per subquestion |
| **b** | Answer | Patient path (warning only); non-patient path (read-only RAG) |
| **c** | Combine | Merge all subquestion answers into one draft |
| **d** | Critique | Appropriateness, factual accuracy, tone; output pass or refine (with feedback) |
| **e** | Retry | Retry loop: refine / re-answer until pass or max retries |
| **f** | Final | Final responder formats and returns the answer |
| RAG | | Read-only; used only in non-patient path |
| User validation | | Deferred; future module will pass to everyone |
| Patient RAG | | Not implemented; architecture ready to plug in later |

This gives you a full pipeline: plan → answer → combine → critique (with retries) → final responder.
