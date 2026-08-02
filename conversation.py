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
    """Is this a villa enquiry? Unknown configuration counts as NOT a villa.

    Deliberately generous. Every live ad is a villa ad, so assuming villa for
    anyone who has not said would apply the ₹3.94 Cr floor to people who never
    asked for one and kill them. The stricter floor is applied only to someone
    who actually said villa.
    """
    cfg = str((checklist or {}).get("configuration") or "").lower()
    return "villa" in cfg and "apartment" not in cfg


def clears_the_bar(conv):
    """(qualified, reason). Budget and location are the only hard gates.

    Deliberately NOT a judgement call — the qualifier reports what it heard and
    this decides. Configuration only rejects off-category, which the agent flags
    rather than this function inferring.

    THE VILLA FLOOR (owner, 2026-08-02). Every live ad sells villas from ₹3.94 Cr,
    but the general floor is ₹1.28 Cr -- the cheapest apartment. Without a second
    floor, someone who clicked a villa ad and has ₹1.2 Cr passes as QUALIFIED and
    reaches a salesperson as a villa buyer who cannot afford any villa. That is
    the handoff that costs sales their trust in the queue.

    So a villa enquirer must clear the VILLA floor. Below it they are not dead --
    the bot offers apartments, and if they accept, `configuration` changes and
    they qualify on the apartment floor instead. Owner: "if they say yes for
    apartments and 1.2 cr of budget then it is a qualified lead - or else we dont
    qualify". Interim rule pending a decision with marketing.
    """
    c = conv["checklist"] or {}
    budget = c.get("budget")
    if not isinstance(budget, int) or budget <= 0:
        return False, "budget not captured"
    if not c.get("location"):
        return False, "location not captured"

    if wants_villa(c):
        if budget < config.VILLA_FLOOR:
            return False, (f"villa enquiry, budget {budget} below the villa entry "
                           f"{config.VILLA_FLOOR}; has not accepted apartments")
        return True, "villa budget and location clear"

    if budget < config.BUDGET_FLOOR:
        return False, f"below floor ({budget} < {config.BUDGET_FLOOR})"
    return True, "budget and location clear"


def set_outcome(conv_id, outcome, upgrade_from=None):
    """Record a terminal outcome. First write wins, with one exception.

    `upgrade_from` allows exactly one transition: qualified -> visit_booked. A
    qualified lead who then names a day has got BETTER, and the salesperson needs
    to know a visit exists. Every other outcome is still write-once, so nothing
    can quietly overwrite a `dead` or an `escalated`.
    """
    if upgrade_from:
        db.x("""UPDATE conversations SET outcome=%s, outcome_at=now(), updated_at=now()
                WHERE id=%s AND outcome=%s""", (outcome, conv_id, upgrade_from))
        return
    db.x("""UPDATE conversations SET outcome=%s, outcome_at=now(), updated_at=now()
            WHERE id=%s AND outcome IS NULL""", (outcome, conv_id))


def mark_handed_off(conv_id):
    db.x("UPDATE conversations SET handoff_sent_at=now() WHERE id=%s", (conv_id,))
