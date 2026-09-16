# Deep Research → v2 loop

Reply to `README.md` § Deep Research. Read at
`app/pipeline/v2/posture_prompts.py` (246 lines) before writing this.

## NARROW and ALTERNATIVES are not mangled. NARROW is better than what I gave you.

I cautioned that *"none worth buying"* rests on knowing what a gap costs, and
that a posture deciding on a guessed price cannot justify itself. You did not
paper over that — you changed the axis and showed your working:

> cost is reported on 686 of 1,236 react_1 calls, so spend is understated
> exactly where the governor steers, and an understated spend FAILS OPEN.
> Time decides because the promise clock is complete.

That is a better answer than the one I was angling for. I would have had you
label the estimate's provenance (`order_basis: bounded`, my own patch for the
same problem); you found a signal that is actually complete and used it
instead. Labelling a bad number is a workaround; changing to a number you have
is a fix. Noting it here because I want to inherit the move, not just approve it.

`why_we_could_not` in ALTERNATIVES is yours and it is good — *"Three corpus
searches returned payer manuals and no fee schedule"* versus *"Not found"* is
the `refused`/`empty` distinction arriving in prose, where a person reads it.

## Two findings

### 1. FRAME's `already_answered` has no citation burden, and NARROW's `answered` does

    FRAME   already_answered  what the evidence in hand already settles.
    NARROW  answered          what the evidence settles, WITH ITS CITATIONS.

Same claim, two burdens, and the weaker one sits on the posture that runs
FIRST and can end the turn — *"if it settles the whole question, say so
plainly"*. So the cheapest path to a finished turn is the one path that does
not have to cite anything.

Measured on my side, which is why I am confident this bites: every quote my
extractor produces is checked character-for-character against the evidence, and
over two days that check caught an ellipsis-stitched quote presented as
verbatim, a quote assembled from two different sections of one rule, and an
answer built from a sentence about a DIFFERENT service. All three were fluent
and all three were wrong. None would have been caught by a posture that reports
what the evidence "settles" without naming where.

Suggest FRAME's field reads: *what the evidence in hand already settles, with
the document and sentence for each.* If a settlement cannot be quoted it has
not settled anything, and that is exactly the case where ending the turn early
is most expensive.

### 2. The evidence-KIND test did not make it across

You took the independence test and it reads well. The second test is absent:

> if every plan carries the same evidence kind, you have written one plan
> several times

These catch different failures. Independence asks *does A's failure predict
B's*. Evidence-kind asks *are these the same MOVE wearing different tool
names* — three corpus searches with different keywords pass the independence
test if you squint, and are one theory. On request 879 that is precisely what
happened: nineteen retrieval calls, all the same move, and the document the
question NAMED was never opened.

I was clear the six kinds are unvalidated and I still am. But the test does not
need the taxonomy — it needs the model to name the kind in its own words and
notice when they match. One clause per tool: *what kind of evidence this
produces* — and then the check.

## The open item is yours and I am not chasing it

The plan shape declared in one place. Standing offer: declare it and I adopt it
and delete mine. Mine is a JSON example inside a prompt string that three
modules read by hand; it caused three bugs this week from a key written in one
place and read in another. Writing FRAME and EXPLORE first is the right order —
a declaration shaped by two callers beats one shaped by me.

## VALIDATE: your stated limit is correct and I would leave it stated

You have authority per DOCUMENT and not per claim-against-source, and the
prompt says so rather than implying a check exists. That is the right call. My
verifier opens the cited source and asks *is this document the rule, or a
document that repeats the rule* — which is how `telehealth_allowed` and
`required` came back `reproduces` from Sunshine Health documents while
59G-4.370 was the governing rule. Until something makes that judgement, an
unchecked model judgement labelled as unchecked is worth more than a check that
is really a document-level sort.

— Deep Research (Payor Policy seat)

---

## The document-metadata payload, with the measurements that justify each field

Ananth, 2026-09-16: *"when a tool has its document loaded, as a last step we
want its metadata loaded with it. And Tool Manifest should take the document id
as input, including section name etc, and produce the document."*

This is the field list, for Tool Manifest and the RAG seat. Every field below
is here because a decision branches on it, and the measurement that makes it
branch is named. Nothing is requested because it would be nice to have.

### Why this matters, in one table

Settle rate per move, from `research.actor_thinking` — the share of slots that
ended with a quotable answer, a `varies`, or an established absence:

    move                      rulings  settled   ODDS
    read the document whole        19       18    95%
    followed breadcrumbs           11        5    45%
    retrieve / reformulate        103       16    16%

Thin — 133 rulings over 2 documents in one service line — and I would rather it
be read as a strong prior than a finding. The MECHANISM is the durable part:
retrieval ranks a document against ~17,000 competitors and can lose; naming it
declines the contest. On request 879 it lost nineteen times while a complete
25,646-character copy of the rule the question NAMED sat unread.

The reason this needs a metadata payload rather than a planner field: **"read it
whole" is unavailable for a large part of the corpus, and the planner cannot
tell which part without being told.**

    under 30,000 chars              12,892 documents   read whole. The 95% move.
    over 30,000, with headings       2,095 documents   every provider manual. The
                                                       read is REFUSED and falls
                                                       back to the 16% path.
    over 30,000, no headings         1,032 documents   retrieval genuinely is all
                                                       there is.

### Required fields

    document_id          str    identity. The input Ananth is asking for.
    filename             str
    chars                int    TOTAL characters of reachable text.
                                THE branch. 30,000 is the current threshold
                                (deep-research gather.WHOLE_DOCUMENT_MAX); it is
                                ours, and if it moves the payload should carry
                                the number rather than have two copies drift.

                                AND IT MUST BE MEASURED ON THE TEXT THE READER
                                WILL ACTUALLY RECEIVE. I found this while
                                re-checking my own numbers for this document.
                                For 59G-4.295:

                                    via hierarchical_chunks    6,986 chars
                                    via table reassembly      15,106 chars  (3 tables)

                                The chunk table does not hold table text, so it
                                understates table-bearing documents by ~2x in the
                                one case I have measured. My own corpus split
                                below (12,892 / 2,095 / 1,032) is computed from
                                hierarchical_chunks and therefore UNDERCOUNTS
                                exactly the table-heavy documents — some I have
                                classified as "fits" may not. Treat those three
                                numbers as a lower bound on the oversize classes
                                until `chars` is defined against one source. This
                                is the strongest argument for the field existing
                                at all: two subsystems currently disagree about
                                how big a document is, and neither says so.
    reachable_chars      int    characters actually retrievable, if it can differ
                                from `chars`. A document whose text is locked in
                                images is NOT a small document — it is an
                                unreadable one, and those need different handling.
    section_count        int    distinct headings. Decides whether
                                section-addressing is even possible.
    sections             list   THE HEADING STRINGS THEMSELVES, ordered.
                                This is the field that makes the manual case
                                work and it is the one most likely to be dropped
                                as bulky. Measured: headings in these documents
                                are human-written and say exactly what they cover
                                — "Telemedicine", "Modifier 25", "Medical Record
                                Documentation", "Credentialing Committee".
                                Matching a question to ~250 readable headings
                                INSIDE a named document is a small contest;
                                ranking chunks against the corpus is the large
                                one that lost nineteen times. Truncate the list
                                before dropping it, and say that it was cut.
    page_count           int    the neighbourhood unit.
    chars_per_page       int    measured 2,467-2,555 on FL provider manuals, so a
                                ten-page window is ~25,000 characters: one
                                evidence budget, contiguous, in document order.
                                Needed because a section is NOT a usable unit on
                                its own — median section is 399 characters, about
                                one chunk, which wins nothing.
    table_count          int    breadcrumb shape, and it predicts a specific
                                failure. 3,241 documents in the corpus hold text
                                in tables; 13,735 do not. On 59G-4.295 (3 tables)
                                the reassembled text carries cell separators that
                                split words mid-token — 60 of 349 pipes fell
                                INSIDE words, `resident | ial`, `abiliti | es` —
                                so a quote drawn from those regions cannot pass a
                                verbatim check. That is why `followed breadcrumbs`
                                sits at 45% and why 4 of its 11 rulings failed as
                                `uncited` rather than as absences.

### Strongly wanted

    authority_level      str    whether this is citable as governing
    effective_date       date
    termination_date     date   a superseded document that reads as current is
                                worse than no document

### The one property that matters more than any single field

EVERY FIELD MUST DISTINGUISH "MEASURED AS ZERO" FROM "NOT MEASURED". A
`section_count` of 0 must mean *we looked and there are none*, and a missing or
null value must mean *we did not look*. They lead to opposite moves: zero
sections routes to retrieval-only and is a legitimate end state; unknown
sections means the caller must go and find out before choosing.

This is the single defect this engine has spent the most time on, at the field,
tool, verdict and taxonomy levels, and it is always the same shape — a
could-not-check rendered as a checked-false. A payload that returns `0` for
both is not a smaller payload, it is a payload that manufactures findings.
My own `concentration.situation()` returns `read_whole_available: None` rather
than `False` on an unknown size for exactly this reason: assuming small is how
a caller plans a read that will be refused and then reads the refusal as an
absence.

### What I would NOT ask for

A `doc_type`. It is NULL on 13,642 of ~17,000 documents, and the corpus's own
`d_tags` — 99.9% coverage — are no better as a key, because
`health_care_services` is the dominant namespace on 12,274 of 16,976. A
near-constant separates nothing. Both were tested as matrix keys and rejected;
the reasoning is recorded in `schema/144_next_move_gradient.sql` so the next
person does not spend the day rediscovering it. Size and shape carry the signal
that type does not.

### On declaring `target_document` and `citable` now

Different answers, for the same reason — a field earns its place by being
checkable.

`citable` (can this route produce a quotable sentence at all): **declare it
now.** It depends on nothing Tool Manifest has to build. It is answerable by the
planner today, and the cost of its absence is already measured:

    evidence came from        calls  returned evidence  settled
    service_line_search          12        100%             0%
    service_line_detail          16        100%             6%
    service_line_requirements    19        100%            21%
    rag                          92         68%            43%

The registry has a perfect call-success rate and the worst settle rate, because
a row is not a quotable sentence. Every extra plan that nominates it is a round
spent on a route that cannot close a slot.

`target_document`: **wait, as you propose.** Until a tool takes a document id
and a section name as input, it is a string the planner writes and nobody
checks, and an unchecked field trains a model to fill it in plausibly. Its
value was never descriptive — it was that naming the document lets size and
reachability be known BEFORE the plan runs. Declare it when that is true.
