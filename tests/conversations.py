"""Scripted buyers, replayed against the REAL model and the REAL corpus.

rules.py catches a broken regex. It cannot catch the defect that actually cost a
buyer on 2026-08-02: the arithmetic was right, `clears_the_bar` said QUALIFIED, and
the model still wrote "that sits a little above your band" and offered apartments.
Only a real turn shows that.

So this replays conversations and asserts on what the bot SAYS.

  python tests/conversations.py            # every scenario
  python tests/conversations.py downsell   # just the ones whose name matches

Reads the corpus and calls the model, so it needs credentials -- run it under
`railway run`. It creates no lead, writes no row and sends no WhatsApp message: the
lead and conversation are dictionaries that live for the length of the test.
"""
import os
import re
import sys

from _bootstrap import ROOT, Results  # noqa: F401  (path side effect)

import config
import conversation as cv
import qualifier as q

R = Results()

LEAD = {"id": -1, "project": "RON", "name": "Test Buyer", "phone": "910000000000",
        "campaign": "RON_Meta_BM", "selldo_status": "meta_direct"}


def _fold(conv, decision):
    """What record_turn would have stored, without touching the database.

    Mirrors conversation.record_turn: first write wins, EXCEPT configuration, which
    a buyer is allowed to change their mind about (the apartment pivot).
    """
    checklist = dict(conv["checklist"])
    for field, key in (("purpose", "purpose"), ("location", "location"),
                       ("configuration", "configuration"), ("budget_inr", "budget"),
                       ("timeline", "timeline"), ("visit_day", "visit_day"),
                       ("visit_time", "visit_time"), ("visit_venue", "visit_venue")):
        val = decision.get(field)
        if val in (None, "", "unknown"):
            continue
        if checklist.get(key) and key != "configuration":
            continue
        checklist[key] = val
    asked = {k: list(v) for k, v in conv["asked"].items()}
    gate, fr = decision.get("gate_asked"), decision.get("framing_used")
    if gate and fr is not None:
        asked.setdefault(gate, [])
        if fr not in asked[gate]:
            asked[gate].append(fr)
    return {**conv, "checklist": checklist, "asked": asked}


# ---------------------------------------------------------------------------
# Scenarios. Each turn: what the buyer says, and what must be true of the reply.
#
#   forbid       regex that must NOT appear (case-insensitive)
#   require      regex that MUST appear
#   action_not   the decision's action must not be this
#   qualifies    clears_the_bar on the folded checklist must equal this
# ---------------------------------------------------------------------------
SCENARIOS = [
    {
        "name": "downsell — a 3.5cr villa buyer must not be offered apartments",
        "why": "2026-08-02: qualified on the arithmetic, downsold by the model.",
        "turns": [
            {"say": "I'm looking for a villa"},
            {"say": "we're in Adyar, looking to buy on ECR"},
            {"say": "Budget is 3 to 3.5 crore",
             "forbid": [r"apartment", r"above (your band|what you)", r"instead"],
             "qualifies": True},
        ],
    },
    {
        "name": "value — 'what do I get for 3.94 cr' must be sold, not deflected",
        "why": "2026-08-02: answered with 'a colleague will come back to you'.",
        "turns": [
            {"say": "I want a 3 bedroom villa, budget 4 crore"},
            {"say": "tell me what all it promises for 3.94 cr",
             "action_not": "escalate",
             "forbid": [r"colleague will (come back|call)"],
             "require": [r"sqft|acre|clubhouse|coast|space|villa"]},
        ],
    },
    {
        "name": "button — 'Need More Details' must be answered, not escalated",
        "why": ("It is the template's own quick-reply and the FIRST thing a knocked "
                "buyer taps. Escalating it wastes the one moment we paid an ad for."),
        "turns": [
            {"say": "Need More Details",
             "action_not": "escalate",
             "forbid": [r"colleague (will|can) (come back|call|confirm)"],
             "require": [r"villa|apartment|ECR|acre|coast|sqft"]},
        ],
    },
    {
        "name": "apologetic — full-time living must not be interrogated",
        "why": "Owner: 'we are apologetic ... already defensive if that works out'.",
        "turns": [
            {"say": "Need More Details"},
            {"say": "Full time home",
             "forbid": [r"whether (this|we|it)( stretch)? .{0,20}(works|suits|right fit)",
                        r"be honest with you about whether",
                        r"right fit", r"daily commute"]},
        ],
    },
    {
        "name": "locality — the word Vadanemmeli must never be spoken",
        "turns": [
            {"say": "where exactly is the project located?",
             "forbid": [r"vadanemmeli"], "require": [r"Kovalam Junction"]},
        ],
    },
    {
        "name": "mall — a distance question must not unlock Express Avenue",
        "why": "Owner: 'just answer the distance and keep pushing the site'.",
        "turns": [
            {"say": "How far is this from Adyar?",
             "forbid": [r"express avenue", r"experience cent"]},
        ],
    },
    {
        "name": "repetition — a price is said once, not four times",
        "why": "2026-08-02: 3.94 Cr appeared in four consecutive messages.",
        "turns": [
            {"say": "I want a villa, what do they cost?"},
            {"say": "and how big are they?", "forbid": [r"3\.94"]},
            {"say": "ok and what is the clubhouse like?", "forbid": [r"3\.94"]},
        ],
    },
    {
        "name": "pivot — a 1.5cr 3BHK buyer is offered the 2BHK, warmly",
        "turns": [
            {"say": "I'm after a 3 bedroom apartment"},
            {"say": "my budget is 1.5 crore",
             "require": [r"2 ?bed|2BHK|two bed"], "qualifies": False},
        ],
    },
    {
        "name": "below entry — an 80 lakh buyer is nurtured, never shown the door",
        "why": ("Owner 2026-08-03, choosing this over killing them: 'the logic here is "
                "not to reject but to nurture and see if they are willing to make the "
                "jump ... when the jump may happen in their thought process - so give "
                "that room'. Before today this was `dead` + suppressed forever."),
        "turns": [
            {"say": "looking for a 2 bedroom apartment"},
            {"say": "my budget is 80 lakhs",
             "action_not": "dead",
             # No rejection language, and nobody is called -- option 2 was chosen
             # precisely so sales is not handed an unaffordable lead.
             "forbid": [r"cannot afford", r"can't afford", r"out of (your|their) (league|range|budget)",
                        r"nothing (for you|available|in your)", r"colleague will (come back|call)",
                        r"keep you posted", r"discount"],
             "qualifies": False},
            # The probe. It may come on either turn, so the assertion is on the reply
            # that follows -- what must never happen is the conversation closing.
            {"say": "yes I am still interested, it is a beautiful place",
             "action_not": "dead",
             "forbid": [r"cannot afford", r"can't afford", r"best of luck",
                        r"do let us know if.{0,30}(changes|increases)"]},
        ],
    },
    {
        "name": "possession — a handover date is never given, and a visit still books",
        "why": ("The design doc names an invented handover date as the worst thing this "
                "bot can produce. Until 2026-08-03 the ban was prompt-only and the "
                "citation guard could not see it either: no date vocabulary in FACTUAL."),
        "turns": [
            {"say": "when is possession? when will it be handed over?",
             # No year, no month, no month-count. A refusal is the right answer here.
             "forbid": [r"20[2-9]\d", r"\bQ[1-4]\b",
                        r"\b(january|february|march|april|june|july|august|september|"
                        r"october|november|december)\b",
                        r"\d+\s*(month|year)s?"]},
            # The guard must not have eaten the bot's actual job.
            {"say": "ok, can I visit this Saturday morning?",
             "action_not": "escalate",
             "require": [r"saturday|booked|team will call|confirm"]},
        ],
    },
    {
        "name": "refusals — no per-square-foot rate, no Tuesday visit",
        "turns": [
            {"say": "what is the per square foot rate?", "forbid": [r"per sq"]},
            {"say": "can I visit on Tuesday?", "forbid": [r"\btuesday\b.{0,30}\b(booked|confirmed|see you)\b"]},
        ],
    },
]


def run_scenario(sc, verbose):
    conv = {"id": -1, "checklist": {}, "asked": {}, "unreciprocated": 0,
            "outcome": None, "brand_id": "RON"}
    history = []
    print(f"\n  {sc['name']}")
    if sc.get("why"):
        print(f"    ({sc['why']})")
    for i, turn in enumerate(sc["turns"], 1):
        said = turn["say"]
        try:
            d = q.run_turn(LEAD, said, history=history, conv=conv)
        except Exception as e:
            R.check(f"{sc['name']} turn {i}", False, f"run_turn raised: {e}")
            return
        reply = d.get("reply") or ""
        history.append({"direction": "in", "body": said})
        history.append({"direction": "out", "body": reply})
        conv = _fold(conv, d)

        print(f"    [{i}] BUYER: {said}")
        if verbose:
            print(f"        BOT  : {reply}")
        else:
            print(f"        BOT  : {reply[:150]}{'...' if len(reply) > 150 else ''}")

        for pat in turn.get("forbid", []):
            R.check(f"{sc['name']} t{i}: must not say /{pat}/",
                    not re.search(pat, reply, re.I), reply[:220])
        for pat in turn.get("require", []):
            R.check(f"{sc['name']} t{i}: must say /{pat}/",
                    bool(re.search(pat, reply, re.I)), reply[:220])
        if "action_not" in turn:
            R.check(f"{sc['name']} t{i}: action must not be {turn['action_not']}",
                    d.get("action") != turn["action_not"],
                    f"action={d.get('action')} note={d.get('internal_note')}")
        if "qualifies" in turn:
            got = cv.clears_the_bar(conv)
            R.check(f"{sc['name']} t{i}: qualifies == {turn['qualifies']}",
                    got[0] == turn["qualifies"], f"{got} checklist={conv['checklist']}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "-v" in sys.argv
    if not os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-"):
        print("conversations.py needs real credentials -- run it under `railway run`.")
        return True
    picked = [s for s in SCENARIOS
              if not args or any(a.lower() in s["name"].lower() for a in args)]
    print(f"CONVERSATIONS  ({len(picked)} scenario(s), real model, real corpus)")
    for sc in picked:
        run_scenario(sc, verbose)
    return R.report("CONVERSATIONS")


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
