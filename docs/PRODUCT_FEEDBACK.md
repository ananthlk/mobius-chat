# Product feedback (v1) — next iteration

Product-level feedback from v1 review. Use this to prioritize the next iteration; do not lose the detail below.

---

## What’s working (don’t change)

- **Mental model is clear.** Left rail = memory + navigation; center = answer + reasoning; bottom = action loop. Right separation for CMHC / ops; front-desk and admin users won’t get lost.
- **Citations as first-class objects.** Citations are clickable; clicking opens source context; you can challenge a fact. This is an auditable answer system, not “chatbot theater.” Match/confidence (e.g. 0.92 / 0.9) is already better than human recall in ops workflows.
- **Recent searches before “Most helpful searches.”** Admins think “what did I just ask?” not “what’s globally helpful?” — ordering is correct.
- **Visual restraint.** No gradients, gimmicks, or “AI sparkle.” Fits Medicaid, compliance, utilization management, and users who want the answer and to move on. Keep that restraint.

---

## Where friction is showing (and why it matters)

1. **Answer block is too monolithic.**  
   The response reads like a policy paragraph, not a decision aid. Users want: *Can I proceed or not? What’s the exception? What do I do next?*  
   **Next move:** Structure as decision aid (same content, different framing):
   - Short Answer (e.g. “Yes — prior authorization is required for certain services.”)
   - When X IS required / When X is NOT typically required (bullets)
   - What to do next (e.g. “Use the Pre-Auth Check Tool to confirm by service code.”)  
   This reduces cognitive load for high-volume users; it’s not cosmetic.

2. **“Thinking (16)” is ambiguous.**  
   Ops users don’t care about token steps; compliance users do care about justification. Right now it’s unclear which this is.  
   **Next move:** Rename to one of: “Reasoning summary,” “Evidence synthesis,” “How this was determined.” That language aligns with audits, not LLM internals.

3. **Highlighting feels accidental, not intentional.**  
   Blue highlight in the paragraph looks like user selection, system emphasis, or leftover dev behavior. That ambiguity matters.  
   **Next move:** If highlighting = system emphasis, make it a boxed callout or label (“Key exception” / “Important”). If highlighting = user interaction, remove default styling. Resolve ambiguity so it’s not visually noisy without semantic meaning.

4. **Confidence score is hidden value.**  
   Match/confidence (e.g. 0.92 / 0.9) is gold but the UI doesn’t surface how to act on it.  
   **Next move:** Add a subtle affordance: e.g. “High confidence” badge, or hover tooltip “Based on N sources, high agreement.” Do not show raw numbers to end users by default; give auditors and power users a way in.

---

## Left rail: one strong suggestion

**“Most Helpful Documents” is doing real work — lean into it.**

This is a differentiator. Consider:

- Show *why* it’s helpful (1-line reason), or show last cited timestamp.
- Example: *Sunshine Provider Manual — Cited in 6 recent answers.*

That reinforces trust and teaches users where truth lives.
