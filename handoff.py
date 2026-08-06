"""Exit router and the handoff card (task 24).

Exits (design §5, revised 2026-08-03):

    QUALIFIED  -> card to sales.
    NURTURE    -> below everything we sell. NOBODY is called and NOTHING is
                  suppressed. The bot keeps talking and probes for room.
    DEAD       -> wrong city, or a product we do not sell at all. Recorded, and the
                  lead is suppressed so no template chases them.
    ESCALATED  -> a human owns the hard part. The bot keeps talking.
    goes quiet -> NOT an exit. Loop-back into the knock engine, resuming
                  mid-checklist. Its own terminal is Dormant (task 19).

NONE OF THESE SILENCE THE BOT ANY MORE. An outcome records what we learned; it is
not a door closing on somebody who is still typing. See worker._handle_inbound.

WHY NURTURE EXISTS. Until 2026-08-03 a budget below the entry price was DEAD: the
conversation was closed, the lead suppressed, and every future template blocked
forever -- on the strength of one number typed into WhatsApp in ten seconds. Owner:
"the logic here is not to reject but to nurture and see if they are willing to make
the jump ... when the jump may happen in their thought process - so give that room -
if everything else is a tick then it makes sense to persist".

⚠️ THE DESIGN SAYS "WhatsApp group ping". THAT IS NOT BUILDABLE AS WRITTEN.
The official WhatsApp Cloud API addresses individual phone numbers; it cannot post
to a group. So the card goes to a designated number (`HANDOFF_PHONE`), and the
conversation itself is picked up in the Wati Team Inbox where sales already works.
The card content is identical either way — only the delivery channel changed.
Flagged for the owner rather than silently substituted.
"""
import logging

import config
import conversation
import db
import sequencer

log = logging.getLogger("handoff")

CRORE = 10000000


def _money(v):
    """Internal-only. This never reaches a buyer — the card goes to sales.

    Written `Rs`, not the rupee sign, for the same reason every other outbound
    figure is (PR #31): the symbol survives our database fine but depends on
    somebody else's decoder once it leaves, and it breaks every CSV and console a
    human reads it in afterwards. Staff cards never pass through the qualifier's
    normaliser, so this is the only place that rule can be applied to them.
    """
    if not isinstance(v, int) or v <= 0:
        return "not given"
    return f"Rs {v / CRORE:.2f} cr"


def _slot(text, fallback="—"):
    """One template parameter. Flattened, because WhatsApp will not take it otherwise.

    A newline, a tab, or four consecutive spaces inside a parameter makes Meta
    reject the ENTIRE send — not the character, the message. Empty values are
    rejected too. So a card that renders perfectly in a test and carries one
    stray line break in a salesperson's name delivers nothing at all, which is
    the failure we are here to end rather than reshape.
    """
    s = " ".join(str(text or "").split())
    return s if s else fallback


def _facts(c):
    """The one-line summary that fills slot 4. Order is decision order.

    Purpose and budget first because they are what decides call-now-or-later;
    the owner asked for a doorbell, not a lead page. The full conversation is in
    the Wati Team Inbox, which the template's closing line points at.
    """
    bits = [
        c.get("purpose") or "purpose not given",
        c.get("location") or "location not given",
        c.get("configuration") or "config not given",
        _money(c.get("budget")),
        c.get("timeline") or "timeline not given",
    ]
    if c.get("flags"):
        bits.append("flags: " + ", ".join(c["flags"]))
    return " · ".join(bits)


def build_card(lead, conv, reason=""):
    """The five template slots for a qualified lead or a booked visit.

    Returns a list, not a string: this is a template now, and each slot is filled
    separately. See config.STAFF_TEMPLATE for why.
    """
    c = conv["checklist"] or {}
    booked = c.get("visit_day")
    if booked:
        action = (f"{booked} {c.get('visit_time') or ''} at "
                  f"{c.get('visit_venue') or 'site'} — CONFIRM THE TIME")
    else:
        action = reason or "Cleared every gate — call today."
    return [
        _slot(f"{'SITE VISIT BOOKED' if booked else 'Qualified lead'} — "
              f"{lead.get('project')}"),
        _slot(lead.get("name"), "name not given"),
        _slot(lead.get("phone")),
        _slot(_facts(c)),
        _slot(action),
    ]


def build_escalation(lead, conv, decision):
    """The five template slots for an escalation. Same shape, different headline."""
    c = dict(conv["checklist"] or {})
    # The decision's own flags matter more here than anywhere else -- they are the
    # reason a human is being called -- so they ride in the same facts line rather
    # than being dropped for the sake of a tidier slot.
    c["flags"] = list(c.get("flags") or []) + list(decision.get("flags") or [])
    return [
        _slot(f"Escalation — {lead.get('project')}"),
        _slot(lead.get("name"), "name not given"),
        _slot(lead.get("phone")),
        _slot(_facts(c)),
        _slot(f"{decision.get('internal_note') or 'the bot could not answer'}. "
              f"It has told them a person will come back."),
    ]


def _readable(slots):
    """The card as a human reads it. Goes to message_log, never to WhatsApp.

    A template send carries only five loose parameter values, so without this the
    audit trail would record that a qualified card went out and not what it said.
    """
    return "\n".join([slots[0], f"{slots[1]} — {slots[2]}", slots[3], slots[4]])


def _notify(phones, slots, kind, lead_id=None):
    """Send a card to every recipient. Same send gate as everything else.

    A notification to our own salesperson is still an outbound WhatsApp message —
    it is not exempt from the master switch, and pretending otherwise would put a
    second door in a system whose whole design is one door.

    Each recipient is attempted independently: one bad number must not stop the
    other from hearing about a qualified lead.

    ⚠️ THE 24-HOUR WINDOW USED TO EAT THESE. Until 2026-08-06 the card went as
    free session text, which WhatsApp delivers only to someone who messaged the
    business number in the previous 24 hours. Salespeople do not message their own
    business number, so delivery was luck: 5 of 24 cards over 30 days came back
    "Ticket has been expired." — four of them escalations, where a buyer had
    asked for a human and the request was thrown away with nothing reporting it.
    An approved template ignores the window, which is what templates are for.

    THE FREE-TEXT FALLBACK IS DELIBERATE. If the template send fails — it is not
    approved yet, the name changed with the account move, Meta rejected a
    parameter — we retry the old way rather than give up. That path is unreliable,
    but it is the path that delivered 79% of cards last month, so falling back to
    it can only ever be better than sending nothing. Both attempts are logged, so
    "the template is failing" stays visible instead of being papered over.
    """
    if not phones:
        log.warning("%s card not sent: no destination configured", kind)
        return False
    body = _readable(slots)
    ok_any = False
    for phone in phones:
        # `id` is the STAFF pseudo-lead's, and stays None on purpose: sequencer
        # bumps send_attempts and can suppress by that id, and a salesperson's dead
        # handset must never mark the BUYER invalid. `log_lead_id` carries who the
        # card is about, which is the thing the audit trail was missing.
        pseudo = {"id": None, "phone": phone, "project": None, "send_attempts": 0}
        # msg_type is NOT prefixed 'knock' -- a card to our own team must never
        # consume the buyer-facing messaging tier or count toward anyone's fatigue.
        ok = sequencer._send(pseudo, f"handoff_{kind}", body=body,
                             template=config.STAFF_TEMPLATE, params=slots,
                             log_lead_id=lead_id)
        if not ok:
            log.warning("%s card to %s: template send failed, trying session text",
                        kind, phone)
            ok = sequencer._send(pseudo, f"handoff_{kind}_text", body=body,
                                 log_lead_id=lead_id)
        if ok:
            ok_any = True
        else:
            log.warning("%s card to %s not delivered", kind, phone)
    return ok_any


def route(lead, conv, decision):
    """Act on the qualifier's exit decision. Returns the outcome applied, or None.

    `goes quiet` is deliberately absent: silence is not something the qualifier can
    report — it is the absence of a turn, detected by the knock engine's scheduler
    (task 17). Treating it as an exit here would be the rev-1 mistake.
    """
    action = decision.get("action")

    # NURTURE. Deliberately the shortest branch in this file: no card, no
    # suppression, no state change on the lead. Recording it is the whole job -- it
    # tells the admin view who is one number away, and it lets the bot keep going.
    #
    # It must NOT set leads.suppressed. That column blocks every future send
    # permanently (knocks.py:138, and any re-opener built later), so suppressing a
    # buyer whose thinking might move is the exact thing this change undoes.
    if action == "nurture":
        conversation.set_outcome(conv["id"], "nurture")
        log.info("lead %s -> NURTURE, no handover (%s)",
                 lead["id"], decision.get("internal_note"))
        return "nurture"

    if action == "dead":
        # Reserved for wrong city and products we do not sell. A low budget no
        # longer arrives here -- the qualifier turns that into `nurture` before this
        # runs -- so suppression stays safe: there is genuinely nothing to sell.
        conversation.set_outcome(conv["id"], "dead")
        db.x("""UPDATE leads SET wa_state='dead', suppressed=TRUE, updated_at=now()
                WHERE id=%s""", (lead["id"],))
        log.info("lead %s -> DEAD (%s)", lead["id"], decision.get("internal_note"))
        return "dead"

    if action == "escalate":
        # The bot no longer goes mute after escalating, so it can escalate again a
        # few turns later. Sales must not get the same card five times -- an alert
        # that repeats is an alert people stop reading. First one wins; later
        # escalations are recorded and stay silent.
        if conv.get("outcome") == "escalated" and conv.get("handoff_sent_at"):
            db.x("""UPDATE conversations SET updated_at=now() WHERE id=%s""",
                 (conv["id"],))
            log.info("lead %s escalated again; card already sent, not re-notifying",
                     lead["id"])
            return "escalated"
        conversation.set_outcome(conv["id"], "escalated")
        _notify(config.STAFF_PHONES, build_escalation(lead, conv, decision),
                "escalation", lead_id=lead["id"])
        conversation.mark_handed_off(conv["id"])
        return "escalated"

    # THE SECOND SUCCESS EXIT. Owner 2026-08-01: "our job is to book the site visit
    # - not just qualified ... two exits - qualified without date, and booked site
    # visit - both are good to escalate."
    #
    # A lead who was already qualified and has now named a day has got BETTER, so
    # this fires a second, upgraded card rather than staying silent. Checked before
    # the `qualified` branch because such a conversation already carries that
    # outcome and would otherwise fall through to nothing.
    booked = (conv.get("checklist") or {}).get("visit_day")
    if booked and conv.get("outcome") == "qualified":
        conversation.set_outcome(conv["id"], "visit_booked", upgrade_from="qualified")
        db.x("UPDATE leads SET wa_state='visit_booked', updated_at=now() WHERE id=%s",
             (lead["id"],))
        conv = conversation.get_or_create(lead)
        _notify(config.STAFF_PHONES, build_card(lead, conv, "SITE VISIT BOOKED"),
                "visit_booked", lead_id=lead["id"])
        conversation.mark_handed_off(conv["id"])
        log.info("lead %s -> VISIT BOOKED (%s)", lead["id"], booked)
        return "visit_booked"

    if action == "qualified":
        ok, reason = conversation.clears_the_bar(conv)
        if not ok:
            # The agent said qualified; the arithmetic disagrees. The arithmetic
            # wins -- "sales receives nobody unqualified" is the §3 political
            # mechanism this whole system rests on.
            log.warning("lead %s claimed qualified but %s -- not handing off",
                        lead["id"], reason)
            return None

        # SEND THE CARD ONCE. The escalate branch above has always had this guard;
        # this branch did not, and it matters MORE here -- the qualified queue is
        # what sales judges us on, so a card that arrives five times trains them to
        # ignore the one channel this system depends on.
        #
        # Reachable, not theoretical: the bot deliberately keeps talking after
        # qualifying (see worker._handle_inbound), the checklist stays complete, and
        # nothing in the schema or the handover copy stops the model reporting
        # `qualified` again on a later turn. Every one of those turns re-sent a card.
        prior = conv.get("outcome")
        if prior in ("qualified", "visit_booked") and conv.get("handoff_sent_at"):
            db.x("UPDATE conversations SET updated_at=now() WHERE id=%s", (conv["id"],))
            log.info("lead %s qualified again; card already sent, not re-notifying",
                     lead["id"])
            return prior

        if prior == "escalated":
            # A SECOND NAMED TRANSITION, for the same reason visit_booked exists:
            # this lead has got BETTER. They were escalated for something the bot
            # could not answer, have since cleared every gate, and `escalated` is
            # write-once -- so without this the outcome column would keep saying
            # "escalated" for somebody who is actually qualified, and every later
            # turn would fall through and fire another card.
            #
            # The escalation card was already sent, so nothing is lost by relabelling
            # -- the human who needed to know already knows.
            conversation.set_outcome(conv["id"], "qualified", upgrade_from="escalated")
        else:
            conversation.set_outcome(conv["id"], "qualified")

        db.x("UPDATE leads SET wa_state='qualified', updated_at=now() WHERE id=%s",
             (lead["id"],))
        _notify(config.STAFF_PHONES, build_card(lead, conv, reason), "qualified",
                lead_id=lead["id"])
        conversation.mark_handed_off(conv["id"])
        log.info("lead %s -> QUALIFIED%s", lead["id"],
                 " (was escalated)" if prior == "escalated" else "")
        return "qualified"

    return None


def notify_human_flagged(lead, conv):
    """Three asks, no answers. A person should look — the bot carries on.

    Distinct from an escalation: the conversation is NOT handed over, and no
    outcome is set. This is a nudge, and it fires once per conversation.
    """
    c = conv["checklist"] or {}
    slots = [
        _slot(f"Stalling — {lead.get('project')}"),
        _slot(lead.get("name"), "name not given"),
        _slot(lead.get("phone")),
        _slot(_facts(c)),
        _slot(f"Asked {conv['unreciprocated']} times with no answer. The bot is "
              f"still replying to their questions."),
    ]
    _notify(config.STAFF_PHONES, slots, "stalling", lead_id=lead["id"])
