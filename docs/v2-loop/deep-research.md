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
