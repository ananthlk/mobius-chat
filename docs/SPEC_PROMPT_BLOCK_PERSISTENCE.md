# SPEC — Prompt Block Persistence (blocks · versions · compositions)

**Owner:** LLMManager (authors schema + read path; owns `prompt_blocks.py` / `BlockAssembler`)
**Gate:** **Database seat (Platform Architects — DB) must ratify the DDL before the migration lands** — same gate mig 049 (`prompt_templates`) passed 5/5 on 2026-07-25.
**Status:** DRAFT for DB-seat review. No migration file committed until ratified.
**Why:** the v2 composable-block engine (`app/services/prompt_blocks.py`) is built and tested, but **blocks live only in code today** — `Block(...)` literals. To manage prompts through the Composition Studio (author/version/sequence blocks per agent), blocks and their per-agent orderings must be **persisted, versioned, and validated in the DB**, and `BlockAssembler` must read from those rows.

---

## 1. Model in one paragraph

A **block** is a typed, owned, versioned unit of prompt text (`hipaa_context`, `module.core`, `turn_context`, …). A **composition** is an agent's **ordered sequence of blocks** — the thing the Studio's drag-and-drop produces (e.g. `integrator_a = [application_context, module.core, …, hipaa_context, forced_json]`). `BlockAssembler` takes a composition + runtime conditions and produces the final prompt, enforcing authority-last, conditional-drop (fail-closed), the `validated_at` gate, and the coherence check — **all already implemented in code**; this spec just gives them a persistent source of truth.

Three tables: **`prompt_blocks`** (versioned block definitions, append-only), **`prompt_compositions`** (versioned per-agent header), **`prompt_composition_members`** (the ordered block list). The design goal is that **the assembler's invariants become DB-level properties**, not just app policy.

---

## 2. Relationship to mig 049 (`prompt_templates`) — additive, not a replacement

- 049 stores **whole per-module prompts** (one row = a full system or user prompt). It is the ratified v1 read path and **stays**.
- These tables **decompose** that: a composition is the *source*, and the assembled text is the *output*. A `module_key` that has an active composition resolves via the block path; a `module_key` with only a 049 template falls back to the template path.
- **Coexistence is explicit and flag-gated** (§5) so the cutover is per-module and reversible — no big-bang.

---

## 3. Proposed DDL (for DB-seat ratification)

### 3.1 `prompt_blocks` — versioned, append-only block definitions

```sql
CREATE TABLE prompt_blocks (
    id            SERIAL PRIMARY KEY,
    block_key     TEXT    NOT NULL,                 -- logical name, e.g. 'hipaa_context'
    version       INT     NOT NULL DEFAULT 1,       -- monotonic per block_key
    block_kind    TEXT    NOT NULL
                    CHECK (block_kind IN ('static','conditional','derived','per_turn')),
    role          TEXT    NOT NULL
                    CHECK (role IN ('system','user')),
    template_body TEXT    NOT NULL,                 -- Jinja2 source (autoescape=False)
    condition     TEXT        NULL,                 -- NULL=always-present; else a condition key ('hipaa_on','emits_json','has_org')
    is_authority  BOOLEAN NOT NULL DEFAULT FALSE,   -- must render after non-authority; immutable per block_key
    directives    TEXT[]  NOT NULL DEFAULT '{}',    -- e.g. {'output:json'} — feeds the coherence gate
    owner         TEXT    NOT NULL,                 -- fleet agent/role that owns this block (soft ref)
    validated_at  TIMESTAMPTZ NULL,                 -- owner sign-off; authority REQUIRES non-null (see CK)
    validated_by  TEXT        NULL,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by    TEXT    NOT NULL,

    UNIQUE (block_key, version),
    -- INVARIANT (mirrors BlockAssembler.UnvalidatedAuthorityError, fail-closed at the DB):
    CONSTRAINT ck_authority_validated
        CHECK (is_authority = FALSE OR validated_at IS NOT NULL)
);

CREATE INDEX idx_prompt_blocks_key_active ON prompt_blocks (block_key, active);
```

- **Versions are immutable and append-only.** Editing a block's text = inserting a new `(block_key, version+1)` row, never `UPDATE`ing `template_body`. This is what makes "change one block → new `composition_hash`" true and auditable. Enforcement: **Q4** (trigger vs convention).
- **`is_authority` is a property of the block_key**, not of a version — it must not flip between versions of the same key. Enforcement: **Q3**.

### 3.2 `prompt_compositions` — versioned per-agent header

```sql
CREATE TABLE prompt_compositions (
    id            SERIAL PRIMARY KEY,
    module_key    TEXT    NOT NULL,                 -- the agent/stage this prompt runs as, e.g. 'integrator_a'
    variant_id    TEXT    NOT NULL DEFAULT 'default',
    variant_tags  JSONB   NOT NULL DEFAULT '{}',    -- 5-axis A/B tag-match lives HERE (not per-block) — see Q5
    version       INT     NOT NULL DEFAULT 1,       -- monotonic per (module_key, variant_id)
    status        TEXT    NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','validated','frozen')),
    active        BOOLEAN NOT NULL DEFAULT FALSE,   -- only active rows execute
    weight        REAL    NOT NULL DEFAULT 1.0,     -- A/B sampling weight within module_key (mirrors 049)
    composition_hash TEXT     NULL,                 -- sha256(ordered block_key@version)[:16]; set when frozen
    coherence_checked_at TIMESTAMPTZ NULL,          -- set when the coherence gate passed (draft→validated)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by    TEXT    NOT NULL,

    UNIQUE (module_key, variant_id, version),
    -- INVARIANTS: a validated composition passed the coherence gate; a frozen one is fully pinned + hashed.
    CONSTRAINT ck_validated_has_coherence
        CHECK (status = 'draft' OR coherence_checked_at IS NOT NULL),
    CONSTRAINT ck_frozen_has_hash
        CHECK (status <> 'frozen' OR composition_hash IS NOT NULL)
);

-- at most one ACTIVE composition per (module_key, variant_id) at a time
CREATE UNIQUE INDEX uq_active_composition
    ON prompt_compositions (module_key, variant_id) WHERE active;
CREATE INDEX idx_prompt_compositions_lookup ON prompt_compositions (module_key, active);
CREATE INDEX idx_prompt_compositions_tags ON prompt_compositions USING GIN (variant_tags);
```

- **Status lifecycle:** `draft` (editable) → `validated` (coherence gate passed) → `frozen` (all members pinned to concrete versions + `composition_hash` computed; immutable, reproducible). `frozen` is what a `calibration_snapshot` points at.

### 3.3 `prompt_composition_members` — the ordered sequence

```sql
CREATE TABLE prompt_composition_members (
    composition_id  INT NOT NULL REFERENCES prompt_compositions(id) ON DELETE CASCADE,
    position        INT NOT NULL,                   -- author order (authority-last applied at assemble time)
    block_key       TEXT NOT NULL,
    pinned_version  INT  NULL,                      -- NULL = resolve to latest active at assemble; concrete when frozen

    PRIMARY KEY (composition_id, position),
    UNIQUE (composition_id, block_key),             -- a block appears at most once in a composition
    -- when a version is pinned, it MUST exist:
    CONSTRAINT fk_member_block
        FOREIGN KEY (block_key, pinned_version) REFERENCES prompt_blocks(block_key, version)
        -- (enforceable because (block_key, version) is UNIQUE in prompt_blocks; NULL pinned_version skips the FK)
);
```

- **Version pinning is the reproducibility lever.** `pinned_version = NULL` → "track the latest active block" (authoring convenience). `frozen` resolves every NULL to the concrete version so the composition is byte-reproducible forever.

---

## 4. Read path (how the assembler resolves a call)

```
PromptManager.resolve(module_key, engagement_context, conditions):
  1. pick composition: active row for (module_key, variant_id) — variant_id via 5-axis
     tag-match on variant_tags (exact→partial→default), then A/B-sample by weight.  [mirrors 049]
  2. load members ordered by position; resolve each block_key to pinned_version
     (or latest active prompt_blocks row when NULL).
  3. hand the (composition, blocks, conditions) to BlockAssembler.assemble()  ← existing code, unchanged
  4. 60s TTL cache keyed on (module_key, variant_id, composition version) for hot-reload.  [mirrors 049]
```

No new assembler logic — the tables just feed `assemble()` the `composition` list and `blocks` dict it already takes.

---

## 5. Coexistence & cutover with 049

- New column on the resolution path (service-level, not necessarily DB): a module resolves to a **composition if one is `active`**, else falls back to its **049 template**. Per-module, reversible.
- No data migration is forced. We can backfill compositions for the already-migrated modules (`integrator_a` core) first, leave the rest on 049, and cut over module-by-module.
- **Open for DB (Q7):** do you want compositions to remain a *parallel source* (assemble at read time) — or to **compile into a 049 `template_body` row** on freeze (compositions author-time, templates run-time)? The former is simpler and keeps one source of truth; the latter keeps the hot read path identical to today.

---

## 6. `llm_calls` linkage + dereferenceable hash (Tech Health requirement)

051 already records `template_id` for the v1 path. For attribution/ablation on the v2 path we add the composition linkage **and** a snapshot registry that makes `composition_hash` dereferenceable — without the registry, a composition with `pinned_version=NULL` members resolves to "latest active" at render time, so its hash is computed over a transient resolution that is persisted nowhere, and §6's ablate/bisect/attribute claims are hollow.

```sql
-- registry: hash → exact ordered manifest, insert-on-first-render (same txn as the llm_calls write)
CREATE TABLE prompt_composition_snapshots (
    composition_hash TEXT PRIMARY KEY,
    manifest         JSONB NOT NULL,     -- ordered [[block_key, version], …] after filter + authority-last
    module_key       TEXT  NOT NULL,
    variant_id       TEXT  NOT NULL DEFAULT 'default',
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE llm_calls
    ADD COLUMN composition_id   INT  NULL REFERENCES prompt_compositions(id),
    ADD COLUMN composition_hash TEXT NULL
        REFERENCES prompt_composition_snapshots(composition_hash);   -- hash is ALWAYS dereferenceable
```

The FK makes dereferenceability a **DB property, not a policy**: a call cannot be logged with a hash that has no manifest. The snapshot upsert (`ON CONFLICT (composition_hash) DO NOTHING`) runs in the same transaction as the `llm_calls` insert.

Two hardenings from Tech Health (2026-07-26), both folded in:
- **`composition_hash` is the FULL sha256, never truncated.** With `ON CONFLICT DO NOTHING`, a truncated-hash collision would silently keep the first manifest and mis-attribute the second — silent mis-attribution in the one table whose only job is faithful attribution. TEXT PK either way, so truncation bought nothing. (`prompt_blocks.py._hash` updated + test asserts 64-hex.)
- **The Q4 `tg_block_body_immutable` trigger is a LANDING GATE for this registry.** The manifest→`prompt_blocks` dereference is only trustworthy if block bodies are immutable; otherwise historical ablation reads mutated bodies. 053 must not land while the trigger section is `[PENDING PASTE]`.

**For DB (added post-ratification, Q9):** OK to fold this 4th table into 053, `manifest` as JSONB (immutable audit artifact) vs a normalized child, and the FK direction as shown?

---

## 7. Open questions FOR THE DB SEAT (the ratification decisions)

| # | Question | LLMManager recommendation |
|---|---|---|
| **Q1** | Members as a **normalized junction table** (§3.3) vs an **ordered JSONB array** on the composition header? | **Normalized** — FK integrity to `prompt_blocks`, clean ordering, no array-index churn. |
| **Q2** | `directives` as **`TEXT[]`** vs **`JSONB`** vs a lookup table? | **`TEXT[]`** — small, closed set (`output:json`/`output:prose`); GIN if we ever query it. |
| **Q3** | Enforce "`is_authority` constant across versions of a key" + "authority ⇒ `validated_at`" as **CHECK vs trigger vs app-only**? | Authority⇒validated as the **CHECK** shown; authority-constant-per-key as a **trigger** (cross-row). |
| **Q4** | Version immutability (block bodies append-only): **trigger that blocks `UPDATE` of `template_body`** vs convention? | **Trigger** — fail-closed, same posture as the mig 042 append-only `hipaa_analysis_log`. |
| **Q5** | 5-axis A/B `variant_tags`: keep **per-composition** (§3.2) or also **per-block**? | **Per-composition only** — the `Block` dataclass carries the field, but persisting variants at the block level multiplies rows; A/B at the composition level is enough. |
| **Q6** | Scope: **GLOBAL** (fleet-wide, like 049) or **per-org `org_id`** from day one? | **GLOBAL v1**, `org_id` deferred — consistent with 049's ratified scoping. |
| **Q7** | Coexistence (§5): compositions as a **parallel read source**, or **compile into a 049 template row** on freeze? | **Parallel source** for one source of truth; open to compile-on-freeze if you want the read path byte-identical to today. |
| **Q8** | `owner` / `validated_by` / `created_by` are **soft refs** to fleet agent names (no agents table in this DB). OK, or do you want a checked domain? | **Soft ref** (TEXT) — agents aren't a table here; a CHECK domain would drift. |

---

*Drafted by LLMManager for the Database seat. The assembler + its invariants are already built and tested (`prompt_blocks.py`, 59 tests); this spec gives them a persistent, versioned, governed home. Blocks-in-code → blocks-in-DB is the last gap before the Composition Studio can manage prompts for real.*
