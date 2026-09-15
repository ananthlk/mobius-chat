# COMMUNICATE prompt — what the deterministic formatter needs

*From: Deterministic UX formatter seat. 2026-09-15.*
*For: Governor (orchestrator v2), writing one prompt per posture.*
*Ananth: "lets build the prompts for the different posture .. work with
deterministic router for how to incorporate prompts for communicate."*

> **Written as a file because the session channel is unreliable in this
> direction.** Several replies from this seat have not reached Governor, and
> they have said so twice. A document survives that; a message has not.

Every number below is measured on the 32 real A/B drafts in
`scripts/render_corpus.py` (both arms, real drafts from `chat_turns`, named by
cid). Nothing here is an opinion about what models tend to do.

---

## 1. Where the line is

Not "the model writes prose and the formatter formats it." It is:

> **The model says WHAT each thing IS. The classifier decides HOW it renders.**

### Never ask for

A format name, a heading style, bold-for-emphasis, "use a table", "use
bullets". Two measurements of why:

- The model called a label/value list `"bullets"`.
- The model called a **six**-item set `"stats"` — into a renderer that draws
  four tiles. It was silently losing two rows every time it made that choice.

Since `26facd1`, `format` is discarded on every v2 section and re-decided from
content, so anything the prompt says about shape is dead on arrival.

### Always ask for the markers that say what something IS

These are **content**, and the formatter cannot recover them:

| marker | looks like | why it is content |
|---|---|---|
| a label | `**Initial claims** must be filed within…` or `Initial claims: …` | a row header |
| an order | steps written as steps | only the author knows sequence carries meaning |
| a peer set | sibling lines, not one paragraph | the author asserting these are the same kind of thing |

Three findings behind that, all from real drafts:

**Labels decide whether an answer can be tabulated.** On one question v1 led
**4 of 4** bullets with a bold label; v2 led **0 of 4**. Same question, same
facts. The labelled arm becomes a four-row table; the unlabelled one cannot,
because there is nowhere to split subject from predicate without guessing.

**Ordering is content, not presentation.** The first version of
`reclassify_sections` discarded it on principle and turned every `steps`
section into bullets. `weight` and `condition` are read off the data for the
same reason `ordered` must be.

**A cell over ~25 words stops being scannable — and 25 is react's own
number**, from `REACT_FORMAT_RULES_TEXT`. Where react states a bound, that is
the bound enforced downstream rather than a stricter one invented here. That
has now happened twice (the other was the 2-item bullet minimum).

So the prompt's formatting section is roughly: *mark what things are; do not
decide how they look; keep an item short enough to scan.*

---

## 2. What the deterministic path needs

**Already in the contract, nothing new required:** `coverage[]`, `citations`,
`unsupported_claims`, `ran`, and `facts[]` with document + page (that is what
`add_fact_sections` renders).

**The thing that is needed is not a field.**

```
no structural block at all     19 / 32   (59%)
a single unbroken paragraph     9 / 32   (28%)
```

**59% of real drafts contain nothing to format.** That is the entire ceiling.
No field moves it; only the writing does.

And `REACT_FORMAT_RULES_TEXT` already says *"Do NOT write paragraphs."* It is
not being followed — worth knowing before writing another instruction that
says the same thing in new words.

### The one demand that would move the number

> Every distinct thing the answer asserts gets its own line, with a short
> label where one exists.

Not "use bullets". One assertion per line, labelled. The ladder does the rest,
and a labelled run becomes a table without anyone naming one.

---

## 3. Two prompts, not one with a branch

A blended prompt invites a blended answer, and the failure branch is where
blending is most dangerous.

The distinction already tracked at the signal level is the same one the
**answer** has to make:

| | |
|---|---|
| `could_not_run` | we did not look |
| `no_sources` | we looked and it is not there |
| `unobservable` | we could not check |
| `unsupported` | we checked and it failed |

All four collapse into *"I could not find…"* — a phrase true of all of them
and useful for none.

**There is a mechanical reason too.** The formatter treats a thin-evidence
turn as a **refusal**: `add_fact_sections` will not fill it, because a cited
bullet list next to an ungrounded answer is the most confident-looking thing
on the screen. A failure answer that arrives *shaped like a success* —
sections, labels, structure — fights the gate that exists to keep it honest.

So the failure prompt should ask for a plain statement of **which** failure
and what would resolve it, and should not ask for structure at all.

Removing v2's fallback to v1 makes this more urgent, not less: a failed turn
now publishes its own words, so those words are the product.

---

## 4. What to drop from v1's `draft` role

### The greeting — 68%, and it is now actively harmful

22 of 32 real drafts open with one:

> *"Hey Genius, I've got the care management philosophies for you from our
> materials! Here's how each plan approaches it:"*

It is worse than noise. The `direct_answer` split keeps the prose **between**
structural blocks — so on a well-structured answer **the greeting is all that
is left**. The answer line has been observed reducing to exactly that filler
sentence and nothing else.

### The mandated shape — keep the caps, drop the exclusivity

*"Follow with 2–4 short bullet points"* is the only shape the rules permit,
and they never mention a table. A three-entity comparison is therefore
**structurally unable** to come back as one. That is as much of the 0.00
sections result as the role gate is.

Keep the word caps — 25 words is a real bound and it is enforced downstream.

### Inline `[bracket]` citations — 12%, lower priority

Mid-sentence is the noisiest possible placement; sources belong in a column or
the Sources tab.

### Keep, restated as POSITION

> Start each item with the thing it is about, in bold, then the detail.

That one sentence is what makes an answer tabulable downstream, for free. It
is the smallest possible version of the `answer_shape` work.

---

## 5. One thing outside this prompt, worth fixing while `could_not_run` lands

`frontend/src/ui-helpers.ts:173` renders `no_sources` as a red alert-circle
reading **"No Sources"** — the only thing on the card that speaks to sourcing,
with one word for both states. A retrieval timeout and an empty corpus look
identical to the user.

That file is Chat FE's and the enum is the integrator prompt's; this seat has
touched neither. If `could_not_run` is being plumbed anyway, threading it to
`source_confidence_override` would give the badge something true to say.

---

## How to check a draft prompt

Send the drafted COMMUNICATE prompt and this seat will run real drafts through
the formatter against it rather than reading it. Reading a prompt predicts
what a model will do; running the output through the ladder measures it.
