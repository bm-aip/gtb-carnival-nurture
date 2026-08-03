"""Conversation state and the persuasion ladder (task 23).

Holds what the bot has learned, which framings it has already used, and how many
times in a row it has asked without getting an answer.

WHY THE CHECKLIST LIVES HERE AND NOT ON `leads`
------------------------------------------------
A ghost who returns must RESUME mid-checklist, never restart (design §6). Asking a
buyer for their budget a second time, after they already gave it, is the single
most obvious way for this system to look broken.

WHY THE COUNTER FLAGS RATHER THAN STOPS
---------------------------------------
Owner requirement §7: after three unreciprocated asks, flag a human *while the bot
keeps answering*. A buyer asking good questions and dodging the checklist is
engaged, not obstructive — cutting them off would be the wrong read. So the flag
is a notification, not a state change.
"""
import json

import config
import db

GATES = ("purpose", "location", "configuration", "budget")


def get_or_create(lead):
    row = db.q("SELECT * FROM conversations WHERE lead_id=%s", (lead["id"],), one=True)
    if row:
        return row
    db.x("""INSERT INTO conversations (lead_id, brand_id)
            VALUES (%s,%s) ON CONFLICT (lead_id) DO NOTHING""",
         (lead["id"], lead["project"]))
    return db.q("SELECT * FROM conversations WHERE lead_id=%s", (lead["id"],), one=True)


def next_gate(conv):
    """The next thing to ask, in the locked order.

    Purpose → Location → Configuration → Budget. That is the INVERSE of gate
    hardness on purpose: budget is the sharpest filter but a brutal opening line,
    so it is earned by having been useful first (design §2).
    """
    checklist = conv["checklist"] or {}
    for g in GATES:
        if not checklist.get(g):
            return g
    return None


def unused_framings(conv, gate):
    """Framings not yet spent on this gate, in order. Empty when all three are used.

    Running out is itself a signal: three different ways of asking, three refusals.
    """
    if not gate:
        return []
    used = set((conv["asked"] or {}).get(gate, []))
    return [f for i, f in enumerate(config.FRAMINGS.get(gate, [])) if i not in used]


def record_turn(conv, decision, gate_asked, framing_index):
    """Fold one turn's outcome into the conversation state.

    Returns the refreshed row. `unreciprocated` resets the moment the buyer gives
    us anything — engagement is engagement, even if it wasn't the field we asked
    for.
    """
    checklist = dict(conv["checklist"] or {})
    learned = False
    # visit_* are captured too: booking the site visit is an OUTCOME, not a nicety
    # (owner 2026-08-01: "our job is to book the site visit - not just qualified").
    # build_card already prints them; without this they were never stored, so a
    # booked visit could never reach the salesperson.
    for field, key in (("purpose", "purpose"), ("location", "location"),
                       ("configuration", "configuration"), ("budget_inr", "budget"),
                       ("timeline", "timeline"), ("visit_day", "visit_day"),
                       ("visit_time", "visit_time"), ("visit_venue", "visit_venue")):
        val = decision.get(field)
        if val in (None, "", "unknown"):
            continue
        # First write wins -- EXCEPT configuration, which a buyer is allowed to
        # change their mind about. A villa enquirer who accepts the apartment
        # offer must be able to become an apartment enquirer, or the pivot can
        # never be recorded and clears_the_bar keeps applying the villa floor to
        # someone who just agreed to look at apartments.
        if checklist.get(key) and key != "configuration":
            continue
        if checklist.get(key) == val:
            continue
        checklist[key] = val
        learned = True

    asked = {k: list(v) for k, v in (conv["asked"] or {}).items()}
    if gate_asked and framing_index is not None:
        asked.setdefault(gate_asked, [])
        if framing_index not in asked[gate_asked]:
            asked[gate_asked].append(framing_index)

    # Reset on ANY new information, increment only when we asked and got nothing.
    if learned:
        unrec = 0
    elif gate_asked:
        unrec = (conv["unreciprocated"] or 0) + 1
    else:
        unrec = conv["unreciprocated"] or 0

    flag_now = (unrec >= config.UNRECIPROCATED_LIMIT
                and not conv.get("human_flagged_at"))

    db.x("""UPDATE conversations
            SET checklist=%s, asked=%s, unreciprocated=%s,
                human_flagged_at = CASE WHEN %s THEN now() ELSE human_flagged_at END,
                last_turn_at=now(), updated_at=now()
            WHERE id=%s""",
         (json.dumps(checklist), json.dumps(asked), unrec, flag_now, conv["id"]))

    fresh = db.q("SELECT * FROM conversations WHERE id=%s", (conv["id"],), one=True)
    fresh["_newly_flagged"] = flag_now
    return fresh


def wants_villa(checklist):
    """Is this a villa enquiry? Unknown configuration counts as NOT a villa."""
    cfg = str((checklist or {}).get("configuration") or "").lower()
    return "villa" in cfg and "apartment" not in cfg


def clears_the_bar(conv):
    """(qualified, reason). Budget and location are the only hard gates.

    Deliberately NOT a judgement call — the qualifier reports what it heard and
    this decides. Configuration only rejects off-category, which the agent flags
    rather than this function inferring.

    PRICE AND CONFIGURATION QUALIFY TOGETHER (owner, 2026-08-02): "each
    configuration and price has to be tied together - we cant qualify someone who
    says 1.2 without we confirming that config is apartment - so both go hand in
    hand - our job is to qualify for the price and unit configuration".

    So configuration is a HARD GATE alongside budget and location. A project-wide
    floor is not enough: a ₹1.5 Cr buyer asking about a 3BHK (from ₹2.1 Cr) clears
    the cheapest apartment and still cannot afford what they asked for. Sales
    receiving "qualified, wants 3BHK" for that person is the handoff that loses
    their trust in the queue.

    Budget is compared AFTER stretching -- buyers understate and can reach higher
    (owner: "20% to 25% more is usually fine"), which is why ₹1.2 Cr qualifies for
    a ₹1.28 Cr apartment. Below what they asked for they are not dead: the bot
    names what it starts at and offers what they CAN reach, and if they accept,
    `configuration` changes and they qualify against the new floor.
    """
    c = conv["checklist"] or {}
    budget = c.get("budget")
    if not isinstance(budget, int) or budget <= 0:
        return False, "budget not captured"
    if not c.get("location"):
        return False, "location not captured"

    raw_cfg = c.get("configuration")
    if not raw_cfg:
        return False, "configuration not captured"
    label, floor = config.classify_configuration(raw_cfg)
    if not label:
        return False, f"configuration not recognised ({raw_cfg!r})"

    if not config.budget_reaches(budget, floor):
        reach = config.affordable_configs(budget)
        can = reach[-1][0] if reach else "nothing in the current release"
        return False, (f"{label} starts at {floor}; budget {budget} does not reach "
                       f"it even stretched. Best fit: {can}")
    return True, f"{label} within budget {budget}, location clear"


# The one PROVISIONAL outcome. Everything else is write-once, so nothing can quietly
# overwrite a `dead`, an `escalated` or a `qualified` -- in particular an escalation
# three turns after qualifying must not demote a lead sales has already been sent.
#
# `nurture` is provisional because the whole point of nurturing is that the number
# can change. A buyer told us ₹80 lakh, we kept talking, and three messages later
# they say the loan is approved for more -- if `nurture` were write-once that person
# could never become qualified, and the state we added to keep them alive would be
# the thing that buried them. Owner: "when the jump may happen in their thought
# process - so give that room".
#
# qualified -> visit_booked is NOT here. It is a named transition and stays explicit,
# via upgrade_from.
UPGRADABLE = ("nurture",)


def set_outcome(conv_id, outcome, upgrade_from=None):
    """Record an outcome. First write wins, except for the upgrades above.

    Called with `upgrade_from` for the one named transition qualified ->
    visit_booked. Called without it for a new outcome, which still lands on a
    conversation sitting in an UPGRADABLE state — so a nurtured buyer whose budget
    moves becomes qualified, while a `dead` or `escalated` stays put.
    """
    if upgrade_from:
        db.x("""UPDATE conversations SET outcome=%s, outcome_at=now(), updated_at=now()
                WHERE id=%s AND outcome=%s""", (outcome, conv_id, upgrade_from))
        return
    # `nurture` never overwrites itself: re-recording it on every turn would reset
    # outcome_at and lose when we first learned the budget was short.
    db.x("""UPDATE conversations SET outcome=%s, outcome_at=now(), updated_at=now()
            WHERE id=%s AND outcome IS DISTINCT FROM %s
              AND (outcome IS NULL OR outcome = ANY(%s))""",
         (outcome, conv_id, outcome, list(UPGRADABLE)))


def mark_handed_off(conv_id):
    db.x("UPDATE conversations SET handoff_sent_at=now() WHERE id=%s", (conv_id,))
