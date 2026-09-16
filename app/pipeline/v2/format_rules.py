"""v2's answer FORMAT rules — the shape is chosen by the content, not fixed.

Ananth, 2026-09-15: "actually create a new prompt set so that we dont disrupt
v1.. this way we can use that modular".

WHY A SECOND SET RATHER THAN AN EDIT. v1's REACT_FORMAT_RULES_TEXT
(react/prompts.py) is one of the most-read strings in the product and v1 still
serves traffic. This file is v2-only, appended by _v2_system_prompt exactly
like the posture block, so v1's answers are byte-identical to yesterday's.

WHAT WAS WRONG, MEASURED. v1's rules say, on EVERY answer:

    "Follow with 2-4 short bullet points (each 10-25 words)"

So every answer reaches the formatter as bullets. Hand the classifier the same
appeal answer written three ways and it returns three different formats —
`*` bullets -> bullets, `1. 2. 3.` -> steps, `Label: value` -> stats. It reads
shape correctly every time. The shape never varied because nothing ever asked
for a different one.

Our own user documentation already promises the opposite
(docs/product-docs/response-cards.md, authored by the UX team):

    "Inside the Details tab, each section chooses the format that fits its
     content — not every answer is a bullet list. Six formats ...
     The format is chosen automatically, but you can steer it by asking."

Six formats, and the instruction permitted one.

THE SHAPES NAMED HERE ARE THE ONES THE CLASSIFIER CAN ACTUALLY DETECT
(app.responder.envelope_classifier), which are the ones the front end can
actually draw. Naming a shape nothing detects would produce prose with extra
steps; naming one nothing renders would produce a blank.

THE INLINE "NEXT STEP" IS GONE, deliberately. v1's rules ask for a trailing
"→ Next step: [action]" inside the answer text. v2 now produces a real
next_steps BLOCK, rendered as its own card section — so the inline line was
printing the same instruction twice on every card. Ananth saw both on one
screen. One producer, one place.
"""

from __future__ import annotations

#: Appended to v2's system prompt in place of v1's fixed-shape rules.
V2_FORMAT_RULES_TEXT = """\
FORMAT RULES for the "answer" field (USER PREFERENCES appended later take FINAL
AUTHORITY over all of this, including length):

• Start with ONE bold sentence giving the direct bottom line: **The answer.**

• THEN CHOOSE THE SHAPE THE CONTENT ACTUALLY IS. Do not default to bullets.
  Write whichever of these the evidence fits, and write it in plain markdown —
  the shape is detected from how you write it:

    A PROCEDURE — how to do something, where order matters:
        numbered steps, one action per step, in the order performed.
        1. Complete the X form.
        2. Attach the Y.
        3. Submit via Z within N days.
      Put the deadline, contact or form name INSIDE the step it belongs to.
      A step may run longer than a bullet; an instruction that omits its own
      deadline is not a shorter step, it is an incomplete one.

    VALUES PER NAMED THING — deadlines, rates, limits:
        one "Label: value" per line.
        Initial claims: 180 days
        Reconsiderations: 90 days
      Not a sentence containing the numbers — the reader is looking one up.

    THE SAME FACTS ACROSS TWO OR MORE SUBJECTS — a comparison:
        a markdown pipe table, one row per subject, one column per fact.
      A comparison written as prose makes the reader do the comparing.

    CONDITIONAL RULES — if/then:
        one line per rule, condition first, result second.

    ANYTHING ELSE:
        2-4 short bullets, 10-25 words each.

  If the evidence does not fill the shape the question suggests, say so in one
  line and give what you have. Never pad a table or a step list to fit.

• Use **bold** for entity names, deadlines, codes and contact details so they
  scan.

• Do NOT write paragraphs. Do NOT repeat the question. Do NOT hedge vaguely.

• Do NOT end with a "Next step:" line. The next steps are produced separately
  and shown to the person as their own section; writing them here prints them
  twice.

• If the answer is genuinely unknown after searching, say so in ONE sentence
  and name the specific thing that would answer it — a document, a payer
  department, a phone number.

NEVER write prose outside the JSON. Put the formatted answer in the "answer"
field. Prose outside the JSON breaks the pipeline.
"""
