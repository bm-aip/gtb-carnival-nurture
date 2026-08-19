"""next_gate: an unanswered soft gate must never block a required one.

Lead 9840168185 (2026-08-19) is the case this locks. He ignored the purpose
question, asked twice about price, and `next_gate` returned `purpose` on every
turn -- so location, configuration and budget were unreachable behind a soft
question he had already declined to answer. He reached a salesperson with nothing
known about him.
"""
import sys

import _bootstrap  # noqa: F401
import config
import conversation as cv


def C(checklist=None, asked=None):
    return {"checklist": checklist or {}, "asked": asked or {}}


CASES = [
    # A fresh lead is asked purpose first: it decides which benefits the whole
    # conversation pitches, so it is worth the opening question.
    ("fresh lead", C(), "purpose"),

    # THE REGRESSION. Purpose was put once and ignored. Step over it.
    ("purpose asked, ignored (lead 9840168185)",
     C({}, {"purpose": [0]}), "location"),

    ("purpose answered", C({"purpose": "weekend"}), "location"),

    # An ignored purpose stays stepped over for the whole required run.
    ("purpose ignored, location in",
     C({"location": "OMR"}, {"purpose": [0]}), "configuration"),
    ("purpose ignored, location + configuration in",
     C({"location": "OMR", "configuration": "villa"}, {"purpose": [0]}), "budget"),

    # Required gates all in: purpose comes back round, where it costs nothing and
    # still sharpens how sales pitches them.
    ("all required in, purpose still missing",
     C({"location": "OMR", "configuration": "villa", "budget": 40000000},
       {"purpose": [0]}), "purpose"),

    ("nothing left to ask",
     C({"purpose": "weekend", "location": "OMR", "configuration": "villa",
        "budget": 40000000}, {"purpose": [0]}), None),

    # Three framings spent on purpose and still no answer. Let it go.
    ("purpose asked three ways, all required in",
     C({"location": "OMR", "configuration": "villa", "budget": 40000000},
       {"purpose": [0, 1, 2]}), None),
]


# A price question missing one word must be ASKED BACK, never handed to a person.
# It must NOT fire once they have named a home -- then we can retrieve and answer.
PRICE_CASES = [
    ("Pls share cost", True),
    ("price?", True),
    ("what is the cost", True),
    ("How much", True),
    ("send me the pricing", True),
    ("kitna hai", True),
    ("what is your budget range", True),

    # They named the product -- answerable, so leave the model alone.
    ("what do the villas start at?", False),
    ("how much for a 2 bedroom apartment?", False),
    ("cost of 3 bhk", False),
    ("price for a 3 bed villa", False),
    ("what is the per square foot rate?", False),
    ("price of the 2552 sqft unit", False),

    # Not a price question at all.
    ("where is the project", False),
    ("Need More Details", False),
    ("can I visit on Saturday", False),
    ("", False),
]


def main():
    bad = 0
    for name, conv, want in CASES:
        got = cv.next_gate(conv)
        ok = got == want
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:44} -> {got!r} (want {want!r})")

    print()
    for msg, want in PRICE_CASES:
        got = config.asks_price_without_product(msg)
        ok = got == want
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'}  ask-which-home {msg!r:42} -> {got} "
              f"(want {want})")

    total = len(CASES) + len(PRICE_CASES)
    print(f"\n{total - bad}/{total} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
