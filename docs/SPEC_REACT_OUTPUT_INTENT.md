# SPEC — ReAct final-round output-intent + display summary

**Proposed by:** LLMManager (validated the design against real Claude; owns the DB-backed prompt-block engine ReAct is migrating onto)
**For:** ReAct agent (owns `app/pipeline/react/prompts.py`, currently mid-decomposition into v2 blocks) + Chat Architecture (owns the emit/enricher pipeline this feeds into)
**Status:** DESIGN VALIDATED (real-Claude test, 4/4 cases). Not built. Routing for fold-in to the in-flight ReAct dynamic-block work, not a standalone patch.
**Origin:** Ananth, live session 2026-07-30 — "react needs to understand the type of answer (email, sms, report etc) this question is best represented by... and the last round of react is really to generate a summarized answer that can be displayed to the user."

---

## 1. The gap this closes

Today there are **two separate LLM syntheses** per turn, not one:

1. **ReAct's last round** produces a rough draft — `{"answer": "<bold line + 2-4 bullets>", "confidence": ..., "is_complete": true}` (see `app/pipeline/react/prompts.py:344-353`). This is never shown to the user directly.
2. A **separate enricher call** (the v2 modular prompt, `integrator_enricher_factual`/`_blended`, LIVE on dev) takes ReAct's draft + all retrieved tool results and resynthesizes the actual displayed card — `direct_answer`, `sections`, `citations`, `gaps`, `next_steps`, `next_questions_for_user`.

Neither stage today decides **what kind of deliverable this answer should be** (in-chat read vs. an email vs. an SMS-length fact vs. an appeal letter). That's the output-intent axis from the v2 enricher work (`read/report/email/sms/emr/appeal/payor_report`), and right now nothing computes it — the frontend always renders `read`.

ReAct's last round is the right place to decide this: by definition, it's the one call that already has the full question, every followup/context signal, every retrieved fact, and the draft answer — the same inputs a human would use to judge "does this read like an email ask or an in-chat lookup."

## 2. Proposed design — additive, not a replacement

**Two new fields on ReAct's final-round JSON only** (fires when `is_complete: true`; the tool-call shape is unchanged):

```json
{
  "thought": "...", "tool": null, "inputs": {}, "is_complete": true,
  "answer": "<existing rough draft — unchanged>",
  "sources": [], "confidence": "high",

  "output_intent": "read | report | email | sms | emr | appeal | payor_report",
  "display_summary": "<a clean, ready-to-show deliverable — NOT a repeat of 'answer'>"
}
```

Instruction block (tested verbatim, see §3):

```
OUTPUT_INTENT — classify based on the question's own nature, not a guess:
- "read" (default): the user is asking to understand something, in-chat.
- "report": the user wants something exportable/shareable.
- "email": the question implies sending this to someone else.
- "sms": very short, single-fact lookup where a text-length answer is natural.
- "emr": clinical/patient-record-adjacent phrasing suggesting it belongs in a chart note.
- "appeal": the question is specifically about drafting/understanding an appeal or dispute.
- "payor_report": the question is about payer-facing documentation/compliance reporting.
If genuinely ambiguous, use "read" — do not force a more specific intent without a real signal.

DISPLAY_SUMMARY — write this as if handing the user the finished answer directly:
plain language, verdict up front. For "email" phrase it as something ready to paste (with a
greeting/sign-off placeholder if appropriate); for "sms" keep it to 1-2 sentences; for "appeal"
frame it as appeal-letter-ready language.
```

**This does NOT replace the enricher call.** `output_intent` + `display_summary` ride alongside ReAct's existing `answer` as additional signal the enricher consumes — the enricher still runs, still produces citations/gaps/sections, but now starts from a strong intent prior and a cleaner draft instead of the rough bullet answer. (Whether the enricher should eventually be *skippable* when ReAct is confident is a real Option-B question — explicitly out of scope for this spec; see §5.)

## 3. Validated — real Claude, 4/4 discriminated correctly, not defaulted

Same tool-result content, four question phrasings, same harness:

| Question phrasing | `output_intent` | `display_summary` (excerpt) |
|---|---|---|
| "What are the requirements to submit a claim reconsideration..." | **read** | Plain in-chat paragraph, verdict first |
| "Can you draft something I can **email** to our billing team..." | **email** | Full email: subject line, greeting, body, sign-off placeholder |
| "**Quick** — what's the reconsideration deadline?" | **sms** | One sentence, no markdown structure |
| "Write the **appeal argument** for why..." | **appeal** | Formal letter: "Dear [Payor/Plan]... we respectfully request reconsideration..." |

Not shown defaulting to `read` for everything — genuinely reads the question's phrasing. Full harness: `scratchpad` (LLMManager session), reproducible against any model.

## 4. Integration questions — for ReAct + Chat Architecture to rule, not decided here

1. **Where does this land in the block decomposition?** ReAct's Phase A (structural decomposition, in flight) vs Phase B (role-based/agent_role variance) — this spec's own read is that it belongs with whichever phase owns the FINAL round specifically (not every round; the fields are meaningless mid-search).
2. **Does the enricher trust `output_intent`, or re-derive/override it?** i.e. is ReAct's classification authoritative, or a prior the enricher can override if the retrieved facts suggest otherwise?
3. **Does `display_summary` become the enricher's starting `direct_answer`, or stay a separate hint field the enricher may or may not use?**
4. **Frontend consumption** — once an adopted surface (email/report/etc., per UX+PA's list) exists, does it read `output_intent` to auto-select the format chip, or is chip-selection still fully separate from this signal?

None of these block validating the design (done, §3) — they block *shipping* it, and are exactly the kind of decision that should route through ReAct + Chat Architecture rather than be assumed.

## 5. Explicitly out of scope for this spec

- **Option B** (ReAct's last round *becomes* the final answer, enricher call skipped/shortened) — a bigger structural change to the 2-stage pipeline; not proposed here.
- Building the actual code change to `prompts.py` — that's ReAct agent's file and ownership.
- Deciding how the frontend renders per-`output_intent` — routes through UX/PA's adopted-surfaces work, separately.

---

*LLMManager validated this is real and workable before proposing it — the point of routing it now, while ReAct's decomposition is in flight, is to fold it in as designed rather than bolt it on after Phase A ships.*
