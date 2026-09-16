"""The plan shape, declared ONCE.

Deep Research offered to adopt this declaration and delete theirs, on the
condition that it exist in one place rather than as prose inside a prompt. It
had been prose inside EXPLORE, which is why I could not hand it over: you
cannot adopt a paragraph.

A PLAN is one theory about where an answer lives, and the tool call that tests
it. Two plans in the same round must be two THEORIES — see `independent_because`
below, which is the whole reason this shape is not just a tool-call list.

Consumers derive their field list from `FIELDS`. Nothing restates it. If a
field's wording only exists in a prompt string, a second copy has been born and
this module has already failed at its one job.
"""

from __future__ import annotations

from typing import NamedTuple


class Field(NamedTuple):
    name: str
    required: bool
    text: str
    """The instruction as the model reads it. Carries its own justification
    where we have measured one — a rule the model can see the reason for
    survives paraphrase; a bare rule does not."""


#: Per tool named in the round.
PER_PLAN: tuple[Field, ...] = (
    Field(
        "ask", True,
        "what to send it. THIS IS A QUERY, NOT A QUESTION. Measured on our own\n"
        "corpus: a question naming the payer built a 324-document pool and\n"
        "reached zero billing codes; the same question written as retrieval\n"
        "vocabulary built 1,814 and returned three codes with their rates.",
    ),
    Field(
        "holds_this_if", True,
        "what would have to be TRUE for this tool to cover this subject.\n"
        "Do not plan a tool because it exists.",
    ),
    Field(
        "cheap_test", False,
        "how to find that out cheaply, or null.",
    ),
    # deep-research, 2026-09-16. `evidence_kind` gets close to this and does
    # not reach it, and they have the measurement that separates them:
    #
    #     evidence came from        calls  returned evidence  settled
    #     service_line_search          12        100%             0%
    #     service_line_detail          16        100%             6%
    #     service_line_requirements    19        100%            21%
    #     rag                          92         68%            43%
    #
    # The registry has a PERFECT call-success rate and the worst settle rate,
    # because a row is not a quotable sentence. Their planner nominates it as
    # an independent route in nearly every plan — and it IS independent. It is
    # independently unable to close a slot.
    #
    # `same_kind_check` cannot catch this: "structured registry row" and
    # "policy prose" are genuinely different KINDS. The question neither field
    # asked is whether the kind can be QUOTED.
    Field(
        "citable", True,
        "can this route produce a sentence you could QUOTE? A structured row\n"
        "or a lookup result is evidence you can act on, not evidence you can\n"
        "cite. If nothing here is quotable, this plan cannot close a claim on\n"
        "its own — say so, and name what would.",
    ),
    # deep-research, 2026-09-15: the second test, which did not make it across
    # when I took the first.
    Field(
        "evidence_kind", True,
        "what KIND of evidence this produces, in your own words. Not the tool\n"
        "name — what sort of thing comes back.",
    ),
)

#: Required only when the round names more than one tool. This is the
#: independence test, and it is the load-bearing field: without it a "plan" is
#: a retry wearing a plan's clothes, and a round spends twice for one theory.
ACROSS_PLANS: tuple[Field, ...] = (
    # TWO TESTS, NOT ONE. Independence asks "does A's failure predict B's".
    # Evidence-kind asks "are these the same MOVE wearing different tool
    # names" — three corpus searches with different keywords pass the
    # independence test if you squint, and are one theory. On deep-research's
    # request 879 that is exactly what happened: nineteen retrieval calls, all
    # the same move, and the document the question NAMED was never opened.
    #
    # The test does not need their six-kind taxonomy, which they are explicit
    # is unvalidated. It needs the model to name the kind in its own words and
    # notice when two match.
    Field(
        "same_kind_check", True,
        "look at the evidence_kind you wrote for each. If they are the same\n"
        "kind, you have written ONE plan several times — say so and replace\n"
        "all but one with a plan that produces a DIFFERENT kind of evidence.",
    ),
    Field(
        "independent_because", True,
        "what makes this a DIFFERENT theory rather than a retry. Apply the\n"
        "test before you write the second one down: if the first returns\n"
        "nothing, does that tell you anything about whether the second will\n"
        "work? If it does, they are ONE plan with two steps — order them. If\n"
        "it does not, they are independent — name them together and they run\n"
        "in the same round.",
    ),
)

#: Round-scoped, not plan-scoped. Deep Research's executor walks plans to
#: exhaustion inside one round; ours has a governor deciding whether there IS
#: another round. `stop_rule` is theirs and does not cross — asking a
#: round-scoped planner for a stop rule invites it to promise something it does
#: not control. This is the field their architecture does not need and ours does.
PER_ROUND: tuple[Field, ...] = (
    Field(
        "would_establish", True,
        "what this round settles if it works. Not what it might find — what\n"
        "it would let us stop asking.",
    ),
)

FIELDS: tuple[Field, ...] = PER_PLAN + ACROSS_PLANS + PER_ROUND


def render(fields: tuple[Field, ...], indent: str = "  ") -> str:
    """Render fields as the prompt block. The ONE place field text becomes
    prompt text, so a field cannot drift between its declaration and its use."""
    width = max(len(f.name) for f in fields)
    out = []
    for f in fields:
        head, *rest = f.text.split("\n")
        pad = indent + " " * (width + 1)
        out.append(f"{indent}{f.name.ljust(width)} {head}")
        out.extend(f"{pad}{line}" for line in rest)
    return "\n".join(out)
