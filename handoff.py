"""Exit router and the handoff card (task 24).

Three terminal exits and one loop-back (design §5):

    QUALIFIED  -> card to sales. Terminal.
    DEAD       -> suppressed permanently. Terminal.
    ESCALATED  -> a human owns the conversation now. Terminal for the bot.
    goes quiet -> NOT an exit. Loop-back into the knock engine, resuming
                  mid-checklist. Its own terminal is Dormant (task 19).

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
    """Internal-only. This never reaches a buyer — the card goes to sales."""
    if not isinstance(v, int) or v <= 0:
        return "not given"
    return f"₹{v / CRORE:.2f} cr"


def build_card(lead, conv, reason=""):
    """What a salesperson needs to make the call, and nothing else.

    Deliberately not a lead page: the owner chose a message because a page is
    something you have to remember to go and look at.
    """
    c = conv["checklist"] or {}
    lines = [
        (f"*SITE VISIT BOOKED — {lead.get('project')}*" if c.get("visit_day")
         else f"*Qualified lead — {lead.get('project')}*"),
        f"Name: {lead.get('name') or 'not given'}",
        f"Phone: {lead.get('phone')}",
        "",
        f"Purpose: {c.get('purpose') or 'not given'}",
        f"Location: {c.get('location') or 'not given'}",
        f"Configuration: {c.get('configuration') or 'not given'}",
        f"Budget: {_money(c.get('budget'))}",
        f"Timeline: {c.get('timeline') or 'not given'}",
    ]
    if c.get("visit_day"):
        lines.append(f"Visit: {c.get('visit_day')} {c.get('visit_time') or ''} "
                     f"at {c.get('visit_venue') or 'site'} — CONFIRM THE TIME")
    if c.get("flags"):
        lines.append(f"Flags: {', '.join(c['flags'])}")
    if reason:
        lines.append(f"\n{reason}")
    lines.append(f"\nSource: {lead.get('selldo_status') or 'unknown'}")
    return "\n".join(lines)


def build_escalation(lead, conv, decision):
    c = conv["checklist"] or {}
    return "\n".join([
        f"*Escalation — {lead.get('project')}*",
        f"Phone: {lead.get('phone')}",
        f"Reason: {decision.get('internal_note') or 'bot could not answer'}",
        f"Flags: {', '.join(decision.get('flags') or []) or 'none'}",
        "",
        f"Known so far — purpose: {c.get('purpose') or '?'} · "
        f"location: {c.get('location') or '?'} · "
        f"config: {c.get('configuration') or '?'} · "
        f"budget: {_money(c.get('budget'))}",
        "",
        "The bot has stopped and told them a person will come back.",
    ])


def _notify(phones, body, kind):
    """Send a card to every recipient. Same send gate as everything else.

    A notification to our own salesperson is still an outbound WhatsApp message —
    it is not exempt from the master switch, and pretending otherwise would put a
    second door in a system whose whole design is one door.

    Each recipient is attempted independently: one salesperson whose 24h window is
    shut must not stop the other from hearing about a qualified lead.

    ⚠️ THE 24-HOUR WINDOW APPLIES TO STAFF TOO. Free session text only delivers if
    that person messaged the business number within the last 24 hours. A card sent
    to a quiet handset is accepted by us and dropped by WhatsApp — silently, from
    our side. Watch `message_delivery` (task 5) for it; the durable fix is a
    utility template, noted in docs/BUILD-PLAN.md.
    """
    if not phones:
        log.warning("%s card not sent: no destination configured", kind)
        return False
    ok_any = False
    for phone in phones:
        pseudo = {"id": None, "phone": phone, "project": None, "send_attempts": 0}
        # msg_type is NOT prefixed 'knock' -- a card to our own team must never
        # consume the buyer-facing messaging tier or count toward anyone's fatigue.
        if sequencer._send(pseudo, f"handoff_{kind}", body=body):
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

    if action == "dead":
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
        _notify(config.ESCALATION_PHONES, build_escalation(lead, conv, decision),
                "escalation")
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
        _notify(config.HANDOFF_PHONES, build_card(lead, conv, "SITE VISIT BOOKED"),
                "visit_booked")
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
        conversation.set_outcome(conv["id"], "qualified")
        db.x("UPDATE leads SET wa_state='qualified', updated_at=now() WHERE id=%s",
             (lead["id"],))
        _notify(config.HANDOFF_PHONES, build_card(lead, conv, reason), "qualified")
        conversation.mark_handed_off(conv["id"])
        log.info("lead %s -> QUALIFIED", lead["id"])
        return "qualified"

    return None


def notify_human_flagged(lead, conv):
    """Three asks, no answers. A person should look — the bot carries on.

    Distinct from an escalation: the conversation is NOT handed over, and no
    outcome is set. This is a nudge, and it fires once per conversation.
    """
    c = conv["checklist"] or {}
    body = "\n".join([
        f"*Stalling — {lead.get('project')}*",
        f"Phone: {lead.get('phone')}",
        f"Asked {conv['unreciprocated']} times with no answer. The bot is still "
        f"replying to their questions.",
        "",
        f"Known — purpose: {c.get('purpose') or '?'} · "
        f"location: {c.get('location') or '?'} · "
        f"config: {c.get('configuration') or '?'} · "
        f"budget: {_money(c.get('budget'))}",
    ])
    _notify(config.ESCALATION_PHONES, body, "stalling")
