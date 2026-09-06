"""A pressed button must reach a human. ~1 second, no database.

    python tests/handraiser_rules.py

WHY THIS EXISTS
---------------
2026-09-06. The daily report had said "373 conversations stalled 3d+" for weeks.
Bucketed against the real predicates, 74 of the 373 were owned by no lane at all,
and **fourteen of those had pressed a button on one of our own templates** --
thirteen "Need More Details" and one "Plan a Site Visit". Each got an answer, said
nothing further, and was never contacted by anything again.

Two correct rules produced it. knocks.py stops forever on any inbound, and PR #62
made a pressed button count as one so that a hand-raiser is never answered with
another marketing blast. reopener.py needs a topic it can name, and somebody who
only pressed a button has named nothing. The rule that protects a hand-raiser is
the rule that orphans them.

THE FOUR PROPERTIES
-------------------
1. A PRESSED BUTTON IS THE TRIGGER, and the labels come from the same config list
   knocks.py tests against. A second copy of that list is a copy that drifts, and
   the drift would reclassify a live buyer.
2. THE RE-OPENER GETS FIRST REFUSAL. If it can name the topic it owns them. Two
   lanes chasing one buyer is worse than neither.
3. ONCE PER PERSON, EVER, AND KEYED ON PHONE. One human is routinely several lead
   rows, and a card that repeats is a card people stop reading.
4. THE BUYER IS NEVER MESSAGED. The card goes to staff. 18% of buyer sends were
   already bouncing when this was written; the answer to a raised hand is a human,
   not another template.
"""
import ast
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _bootstrap import Results        # noqa: E402

import config                          # noqa: E402
import handraiser                      # noqa: E402
import picker                          # noqa: E402

R = Results()

SRC = io.open(os.path.join(os.path.dirname(__file__), "..", "handraiser.py"),
              encoding="utf-8").read()


def row(last_in, checklist=None, **kw):
    r = {"id": 1, "conv_id": 9, "phone": "919000000001", "name": "A Buyer",
         "project": "RON", "campaign": "ron_villa_bm", "last_in": last_in,
         "checklist": checklist or {}, "last_turn_at": None,
         "last_inbound_at": None}
    r.update(kw)
    return r


# --- 1. the trigger -----------------------------------------------------------
def test_a_pressed_button_is_the_trigger():
    # The real labels, not a fixture: if somebody edits the env list, this test
    # follows them rather than passing on a copy that no longer matches.
    for label in config.TEMPLATE_BUTTON_LABELS:
        R.check("%r is a hand raised" % label, handraiser._is_button(label))

    R.check("and case and padding do not matter -- WhatsApp echoes the label back "
            "as the buyer typed nothing", handraiser._is_button("  Need More DETAILS  "))

    # THE OTHER HALF OF PR #62. "I need more details on the 3BHK price" is a
    # sentence, not a button; the qualifier owns that person and a card would be
    # noise. Anchored comparison, never a substring match.
    R.check("a sentence containing the label is NOT a button press",
            not handraiser._is_button("I need more details on the 3BHK price"))
    R.check("an ad prefill is not a button press",
            not handraiser._is_button("Hi! Need more details about republic of nature."))
    R.check("silence is not a button press", not handraiser._is_button(None))
    R.check("nor is an empty body", not handraiser._is_button("   "))


def test_it_picks_the_hand_raiser():
    picked = handraiser._pick(row("Need More Details"))
    R.check("somebody who tapped and told us nothing is picked", picked is not None)

    R.eq("somebody who typed a real question is left to the qualifier",
         handraiser._pick(row("what is the price of the 4 bhk")), None)


# --- 2. the re-opener gets first refusal --------------------------------------
def test_the_reopener_owns_anyone_it_can_name():
    """Exclusive ownership, checked against reopener.topic_for itself."""
    named = row("Need More Details", {"configuration": "4 bed villa"})
    R.eq("a topic the re-opener can name means it owns them, not us",
         handraiser._pick(named), None)

    parked = row("Need More Details", {"visit_day": "Saturday"})
    R.eq("and a parked visit is very much its lane",
         handraiser._pick(parked), None)

    # The gate is the TOPIC, not merely a non-empty checklist: a checklist holding
    # only an apartment configuration yields no topic under villas-only, and that
    # person would otherwise fall between both lanes exactly as these 74 did.
    apartment = row("Need More Details", {"configuration": "Compact 2BHK apartment"})
    R.check("a checklist with nothing nameable in it stays ours",
            handraiser._pick(apartment) is not None)


# --- 3. once per person, and the buyer is never messaged ----------------------
def test_the_card_goes_to_staff_and_only_once():
    calls = {n for n in ast.walk(ast.parse(SRC)) if isinstance(n, ast.Call)}
    targets = {ast.unparse(n.func) for n in calls}

    # READ FROM THE AST, NOT THE TEXT. On 2026-09-03 a source assertion in this
    # project passed on a comment that said the opposite of what it claimed.
    R.check("the card is sent through handoff._notify, the one card path",
            "handoff._notify" in targets)
    R.check("this lane never calls the send door directly -- one door, and the "
            "card path is already through it",
            "sequencer._send" not in targets)

    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "run")
    args = {ast.unparse(a) for n in ast.walk(fn) if isinstance(n, ast.Call)
            for a in n.args}
    R.check("addressed to STAFF_PHONES, never to the buyer",
            "config.STAFF_PHONES" in args)

    sql = SRC[SRC.index("def _fetch"):SRC.index("def _is_button")]
    R.check("a person who already had a card is excluded in SQL",
            "NOT EXISTS" in sql and "message_log h" in sql)
    R.check("and that exclusion is keyed on phone, because one human is several "
            "lead rows", "l3.phone = l.phone" in sql)
    R.check("opt-outs are excluded", "FROM optouts o" in sql)
    R.check("so are numbers we already gave up on -- they belong on the call list",
            "knock_lost_at IS NULL" in sql)


# --- 4. the lane is watched, paged, and asleep at night -----------------------
def test_the_lane_is_wired_in_and_watched():
    here = os.path.join(os.path.dirname(__file__), "..")
    seq = io.open(os.path.join(here, "sequencer.py"), encoding="utf-8").read()
    wd = io.open(os.path.join(here, "watchdog.py"), encoding="utf-8").read()

    fn = next(n for n in ast.walk(ast.parse(seq))
              if isinstance(n, ast.FunctionDef) and n.name == "tick")
    ran = {ast.unparse(a) for n in ast.walk(fn) if isinstance(n, ast.Call)
           for a in n.args}
    R.check("tick() actually runs the lane", "handraiser.run" in ran)

    # A LANE ABSENT FROM watchdog.LANES IS UNWATCHED, and unwatched is how the
    # ghost lane ran for a fortnight with nobody looking. Asserted here so lane
    # four cannot be added without it either.
    R.check("and the watchdog watches it", '"handraiser"' in wd)


def test_it_waits_for_morning():
    """Fourteen cards at 3am is worse service than the same fourteen at 8am."""
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "run")
    calls = {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    R.check("run() checks quiet hours before sending anything",
            "sequencer.quiet_now" in calls)


def test_it_pages_instead_of_taking_a_fixed_window():
    """The bound that starved the re-opener for 90 hours. Not rebuilt here."""
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "due")
    calls = {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    R.check("due() walks pages with the shared picker", "picker.scan" in calls)

    for n in ast.walk(fn):
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mult):
            R.check("and never multiplies the limit into a fixed window", False)
    R.check("and never multiplies the limit into a fixed window", True)


def test_scan_stops_at_the_batch():
    """The lane's own selection, driven through the real picker."""
    people = ([row("hello there, what is the price") for _ in range(40)]
              + [row("Need More Details") for _ in range(9)])

    def fetch(page, offset):
        return people[offset:offset + page]

    got = picker.scan(fetch, handraiser._pick, 5)
    R.eq("five cards from a page of rejects deeper than one window", len(got), 5)

    got = picker.scan(fetch, handraiser._pick, 25)
    R.eq("and all nine when more are asked for", len(got), 9)


def test_the_card_says_what_they_tapped():
    card = handraiser._card(row("Plan a Site Visit", last_inbound_at=None,
                                name="Priya"))
    R.eq("five slots, like every other card", len(card), 5)
    joined = " | ".join(card)
    R.check("it names the button they pressed", "Plan a Site Visit" in joined)
    R.check("and their name", "Priya" in joined)
    # The point of the card: there is nothing to prepare, the call is the discovery.
    R.check("and says plainly that we know nothing about them",
            "answered no questions" in joined)
    for slot in card:
        R.check("no newline survives into a template parameter -- Meta rejects "
                "the entire send, not the character", "\n" not in slot)


if __name__ == "__main__":
    test_a_pressed_button_is_the_trigger()
    test_it_picks_the_hand_raiser()
    test_the_reopener_owns_anyone_it_can_name()
    test_the_card_goes_to_staff_and_only_once()
    test_the_lane_is_wired_in_and_watched()
    test_it_waits_for_morning()
    test_it_pages_instead_of_taking_a_fixed_window()
    test_scan_stops_at_the_batch()
    test_the_card_says_what_they_tapped()
    sys.exit(0 if R.report("HAND-RAISER RULES") else 1)
