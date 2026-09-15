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
)

#: Required only when the round names more than one tool. This is the
#: independence test, and it is the load-bearing field: without it a "plan" is
#: a retry wearing a plan's clothes, and a round spends twice for one theory.
ACROSS_PLANS: tuple[Field, ...] = (
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
