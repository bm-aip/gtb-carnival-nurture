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
    # The sales offer has one wording and no framing index, so it is recorded on
    # the gate alone -- mirroring record_turn, where its presence is the state.
    if gate == cv.SALES_OFFER:
        asked.setdefault(cv.SALES_OFFER, [0])
    elif gate and fr is not None:
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
        "name": "possession — the two approved dates are given, and a visit still books",
        "why": ("This scenario REVERSED on 2026-08-05. It used to assert that no date "
                "ever went out, which was right while the corpus held dates nobody had "
                "approved -- the rule was never that dates are dangerous, it was that "
                "unapproved ones are. Marketing then named two (Phase 1 December 2027, "
                "Phase 2 June 2028), so the test now asserts the opposite: the bot must "
                "SAY them. Possession is a top-three buyer question and this is the "
                "difference between answering it and pulling a human in every time."),
        "turns": [
            {"say": "when is possession? when will it be handed over?",
             "require": [r"december\s*2027", r"june\s*2028"],
             # Everything the business did NOT approve. A third phase, a revised or
             # brought-forward date, a duration, or a day of the month -- the last of
             # these being the one a buyer forwards to their lawyer.
             "forbid": [r"20(2[0-6]|29|3\d)", r"\bQ[1-4]\b",
                        r"\d+\s*(month|year)s?",
                        r"phase\s*3",
                        r"\b\d{1,2}(st|nd|rd|th)?\s+(december|june)\b",
                        r"\b(december|june)\s+\d{1,2}\b(?!\s*\d)",
                        # Construction progress. Marketing, 2026-08-05: don't state it,
                        # give the possession date instead.
                        r"foundation|podium|raft|on track|percent complete"]},
            # The guard must not have eaten the bot's actual job.
            {"say": "ok, can I visit this Saturday morning?",
             "action_not": "escalate",
             "require": [r"saturday|booked|team will call|confirm"]},
        ],
    },
    {
        "name": "fittings — a utility question is handed over warmly, never a bare no",
        "why": ("The business decided on 2026-08-05 that sales answers utility and "
                "specification questions. The eight chunks that answered them are "
                "withdrawn, so this proves the deferral rather than the rule: with "
                "nothing to cite the bot has to hand over, and it must do it warmly. "
                "The old corpus said 'No. No glass. No Counter Top. No Water meter' -- "
                "on a crore-plus purchase that reads as a building being taken apart."),
        "turns": [
            {"say": "is there a piped gas connection in the kitchen?",
             "require": [r"colleague|someone from our team|team will|have someone"],
             # The bare refusal in any of its shapes, and any answer either way -- we
             # are not allowed to say no, and we are certainly not allowed to say yes.
             "forbid": [r"\bno piped gas\b", r"\bwe do not provide\b",
                        r"\bthere is no\b", r"\byes,? (there|we)\b"]},
            {"say": "and who maintains the community once we move in?",
             # Marketing, Q12. The chunks naming it are quarantined; this is the belt.
             "forbid": [r"\belements\b"]},
            # The handover must not have cost us the conversation. A dead end after a
            # deferral is the failure mode that matters -- the buyer came to buy.
            {"say": "ok. can I come and see it on Saturday?",
             "action_not": "escalate",
             "require": [r"saturday|booked|team will call|confirm"]},
        ],
    },
    {
        "name": "the budget refuser — offered a call, and says yes",
        "why": ("Owner, 2026-08-06: agreeing to speak to sales is a good enough test "
                "of seriousness when someone will not name a number. This is the only "
                "way to know the model actually MAKES the offer at the right moment "
                "and reads the yes -- the rule cases can only prove the trigger."),
        # THE FIRST TURN MUST CLEAR THREE GATES. Gates are asked in a locked order,
        # purpose -> location -> configuration -> budget, so a buyer who never says
        # what they want or where never REACHES the budget question and this feature
        # can never fire. The first version of this scenario got that wrong and
        # proved nothing; the failure looked like a broken trigger.
        "turns": [
            {"say": "hi, want a 3BHK to live in full time, looking around ECR",
             "forbid": [r"per sq"]},
            # First budget ask, stepped around.
            {"say": "what are the amenities like?"},
            # Second. Still nothing -- and after this the offer is due.
            {"say": "I'd rather not discuss numbers over whatsapp"},
            {"say": "how far is it from the beach?",
             # Never a third budget ask once the offer is due.
             "forbid": [r"your budget", r"how much.{0,20}(spend|looking)"]},
            {"say": "yes please, ask them to call me",
             "action_is": "connect_sales",
             # And still no budget, which is the entire point.
             "qualifies": False},
        ],
        # The owner chose the CALL over the site visit: a visit is a bigger ask of
        # someone still guarding what they will spend. Left to itself the model
        # reached for the visit instead, which is why the instruction says so twice.
        "require_anywhere": [r"(call|speak to|get in touch)",
                             r"(someone|colleague|a member) (from |of )?(our )?team|"
                             r"our team|a colleague"],
    },
    {
        "name": "the budget refuser — offered a call, and says no",
        "why": ("A no must not close them. It goes to nurture, the one reversible "
                "outcome, so a budget arriving three messages later still qualifies "
                "them the ordinary way."),
        # Seeded at the exact moment the offer falls due: three gates known, budget
        # asked twice, nothing given.
        "start": {"checklist": {"purpose": "live in full time", "location": "ECR",
                                "configuration": "3BHK"},
                  "asked": {"budget": [0, 1]}},
        "turns": [
            {"say": "and is it gated?"},
            {"say": "no, not yet. still just looking",
             "action_not": "dead",
             # Nothing may suggest the conversation is over.
             "forbid": [r"all the best|good luck|do reach out when|thank you for your "
                        r"time"]},
        ],
        "require_anywhere": [r"(call|speak to|get in touch)"],
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
    # A scenario may START mid-conversation. Reaching a late state organically
    # costs several turns and depends on the model taking one exact route: the
    # "declines the call" scenario failed once because the bot spent a turn
    # escalating and so never got its second budget ask. That tests the model's
    # mood, not the rule. Seeding the state tests the thing the scenario is named
    # after, and the organic path is still covered by the scenario above it.
    conv = {"id": -1, "checklist": {}, "asked": {}, "unreciprocated": 0,
            "outcome": None, "brand_id": "RON", **(sc.get("start") or {})}
    history = []
    # Some things must happen, but not on a turn we can name in advance. The bot
    # decides when it has answered enough to ask for something, so pinning "offer
    # them a call" to turn 4 tests the model's pacing rather than the rule. These
    # are asserted over the whole conversation instead.
    said_all = []
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
        said_all.append(reply)
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
        if "action_is" in turn:
            R.check(f"{sc['name']} t{i}: action must be {turn['action_is']}",
                    d.get("action") == turn["action_is"],
                    f"action={d.get('action')} note={d.get('internal_note')}")
        if "qualifies" in turn:
            got = cv.clears_the_bar(conv)
            R.check(f"{sc['name']} t{i}: qualifies == {turn['qualifies']}",
                    got[0] == turn["qualifies"], f"{got} checklist={conv['checklist']}")

    whole = "\n".join(said_all)
    for pat in sc.get("require_anywhere", []):
        R.check(f"{sc['name']}: somewhere in the conversation, /{pat}/",
                bool(re.search(pat, whole, re.I)), whole[-400:])


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
