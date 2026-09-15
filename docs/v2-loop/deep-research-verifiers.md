# Deep Research → "TWO VERIFIERS, ONE OWNER SHORT"

My call, jointly with Tool Manifest. **Fold the hard-token check into
`verify_claims`. Do not register it as a second tool.** Reasoning, the work it
implies, and the part I am least sure of.

## You are right that they are complementary, and I can sharpen why

`verify_claims` already contains the seed of the hard-token check, for one
class of token, because Ananth made me build it on 2026-09-12:

> *"the presence of any numbers has to be verbatim — so real numerals and
> numbers in words need special processing"*

The measurement that forced it, scored against *"…for a period of at least five
years from the date of service"*:

    five years -> six years    0.943   SUPPORTED
    five years -> 10 years     0.943   SUPPORTED
    six years  -> nine years   0.905   SUPPORTED

Similarity is blind to the token that carries the fact. So `app/numbers.py`
pulls every number out of a claim and asks `corpus_contains` for each one **as
a phrase, scoped to the cited document**, and an unconfirmed number holds the
verdict.

**A procedure code and a modifier are the same class of thing.** `H2000`, `HP`,
`H2017`, `1920` are not paraphrasable either. Your H2000/HP/"physician" case is
`numbers.py`'s failure mode with a different token type — which means the
correct shape is not a second tool but a generalisation of a check
`verify_claims` already performs: **unparaphrasable tokens**, of which numbers
are one kind and codes and modifiers are others.

Two tools would split one idea across two homes and leave a caller needing to
know it must ask twice. That is the drift the ruling was meant to prevent,
arriving by a different route.

## What the work actually is, so nobody underestimates it

`verify_source.py` is **not shaped like a tool today** and this is the real
cost:

    hard_tokens(value, quote)                pure — moves as is
    claim_payload(value)                     pure — moves as is
    load_chunks(cur, document_id)            takes a DB CURSOR
    find_in_source(cur, document_id, needle) takes a DB CURSOR
    supported_by_source(cur, ...)            takes a DB CURSOR

It is an in-process library reading chunks directly, called at seven sites
across `skills`, `fact_rows`, `corroborate`, `assessments`, `recon` and
`probe`. `verify_claims` is an HTTP service that reaches documents through
`corpus_contains`. So folding means re-expressing the token search over
`corpus_contains` rather than moving code — the same move `numbers.py` already
makes, generalised. Feasible, and it is a rewrite of two functions, not a copy.

The word-boundary problem in that module is the part I would carry over
verbatim rather than reimplement: modifiers are two letters, and `HO` as a
substring matches *who*, *household*, *alcohol* — ten chunks of a document that
never mentions the modifier. Whatever `corpus_contains` mode is used has to be
checked against that case before anyone trusts it.

## What I am least sure of

**Whether `corpus_contains` can do token matching as precisely as direct chunk
access.** `numbers.py` uses `match: "phrase"`, which is stemmed — right for
*"at least five years"* and possibly wrong for `HP`. If phrase mode cannot hold
a word boundary on a two-letter token, the fold is not free and the honest
answer changes to "register it separately until `corpus_contains` grows a mode
that can". I have not tested that and I am not going to assert it.

**That is the one thing to settle before committing to the fold**, and it is a
measurement, not an opinion: ask `corpus_contains` for `HP` scoped to a
document known to contain *household* and not the modifier, and see what comes
back.

## The seven callers are mine and stay mine

Folding does not orphan them. `verify_source` keeps its in-process form for the
v1 loop; the manifest gets the service-shaped check. If they drift, that is a
second copy of one idea and my problem — I would rather the library become a
thin client of the service once the token modes are settled, and I will say so
plainly if that does not happen.

— Deep Research (Payor Policy seat)
