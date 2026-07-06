"""Sequencer: runs every SEQUENCER_TICK_MIN minutes (IST-aware)."""
from datetime import datetime, date, timedelta, timezone
import config
import db
import wasender
import parser as reply_parser

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST)


def _fmt(d: date):
    return d.strftime("%A %d %B")  # Friday 10 July


BRAND = {"RON": "Republic Of Nature", "ELEMENTS": "Elements Senior Living"}

DATE_ASK = ("Which day will you visit? Reply 1 \u2013 Fri 10 July, "
            "2 \u2013 Sat 11 July, 3 \u2013 Sun 12 July.")

def m1_body(lead, combined=False):
    name = (lead.get("name") or "").split(" ")[0] or "there"
    venue = f"{config.EVENT_VENUE}"
    if lead["project"] == "RON":
        intro = (f"Hi {name}! Thank you for your interest in Republic Of Nature. "
                 f"You're invited to the {config.EVENT_NAME} \u2014 10, 11 & 12 July at {venue}. "
                 f"Carnival savings up to \u20b987L*, no registration fees, no pre-EMI. ")
    else:
        intro = (f"Hello {name}, greetings from Elements Senior Living. "
                 f"Thank you for your interest in Madhuram, Vandalur. "
                 f"We'd be delighted to meet you at the {config.EVENT_NAME} \u2014 "
                 f"10, 11 & 12 July at {venue}. ")
    if lead.get("selected_date"):
        # Form already captured their preferred day: confirm, don't re-ask
        body = intro + (f"You've chosen {_fmt(lead['selected_date'])} \u2014 we look "
                        f"forward to seeing you! If you'd like to change the day, "
                        f"reply 1 (Fri 10), 2 (Sat 11), 3 (Sun 12).")
    else:
        body = intro + DATE_ASK
    if combined:
        body += f"\n\nLocation: {config.EVENT_MAPS_LINK}\nOpen all day."
    return body


def m2_body(lead):
    name = (lead.get("name") or "").split(" ")[0] or "there"
    return (f"Hi {name}, checking in from {BRAND[lead['project']]} \u2014 we'd love to have you "
            f"at the {config.EVENT_NAME} this weekend at {config.EVENT_VENUE}. "
            f"Which day suits you? Reply 1 (Fri 10), 2 (Sat 11), 3 (Sun 12).")


def m3_body(lead):
    name = (lead.get("name") or "").split(" ")[0] or "there"
    d = lead["selected_date"]
    when = "today" if d == now_ist().date() else f"tomorrow, {_fmt(d)}"
    return (f"Hi {name}, reminder from {BRAND[lead['project']]}: we look forward to seeing you "
            f"{when} at {config.EVENT_VENUE}. Open all day. "
            f"Location: {config.EVENT_MAPS_LINK}. Show this message at the entrance as your entry pass. See you there!")


def m3_generic_body(lead):
    name = (lead.get("name") or "").split(" ")[0] or "there"
    return (f"Hi {name}, the {config.EVENT_NAME} by {BRAND[lead['project']]} runs this "
            f"Friday to Sunday (10\u201312 July) at {config.EVENT_VENUE}, all day. "
            f"Walk in on any day \u2014 we'd love to meet you. "
            f"Location: {config.EVENT_MAPS_LINK}. Show this message at the entrance as your entry pass.")


def ack_body(lead):
    return (f"Noted \u2014 see you on {_fmt(lead['selected_date'])} at {config.EVENT_VENUE}. "
            f"Location: {config.EVENT_MAPS_LINK}. Show this message at the entrance as your entry pass.")


def paused():
    return (db.get_setting("global_pause", "false") == "true") or config.GLOBAL_PAUSE_ENV


def _send(lead, msg_type, body):
    if paused():
        return False
    if not wasender.rate_ok():
        db.set_setting("rate_capped_at", now_ist().isoformat())
        return False
    ok, detail = wasender.send_text(lead["phone"], body)
    db.log_msg(lead["id"], "out", msg_type, body, ok=ok, detail=detail)
    return ok


def tick():
    n = now_ist()
    today = n.date()
    last_event_day = config.EVENT_DATES[-1]
    day_before_first = config.EVENT_DATES[0] - timedelta(days=1)   # July 9

    # ---- M1 for queued leads ----
    for lead in db.q("""SELECT * FROM leads WHERE wa_state='queued'
                        AND NOT suppressed AND phone IS NOT NULL"""):
        if today > last_event_day:
            continue
        combined = (today >= day_before_first)  # late qualifier: fold venue into M1
        if _send(lead, "m1", m1_body(lead, combined=combined)):
            new_state = "date_selected" if lead.get("selected_date") else "m1_sent"
            db.x("UPDATE leads SET wa_state=%s, m1_sent_at=now(), updated_at=now() WHERE id=%s",
                 (new_state, lead["id"]))

    # ---- M2: 24h after M1, no reply, no date ----
    for lead in db.q("""SELECT * FROM leads WHERE wa_state='m1_sent' AND NOT suppressed
                        AND selected_date IS NULL AND last_inbound_at IS NULL
                        AND m1_sent_at < now() - interval '24 hours'
                        AND m2_sent_at IS NULL"""):
        if today > last_event_day:
            continue
        if _send(lead, "m2", m2_body(lead)):
            db.x("UPDATE leads SET wa_state='m2_sent', m2_sent_at=now(), updated_at=now() WHERE id=%s",
                 (lead["id"],))

    # ---- M3 rules ----
    # (a) evening before selected date, from 18:00 IST
    # (b) selected date == today (lead picked same-day): send from 08:00 IST,
    #     unless M1 went out today (combined M1 + ack already carried the venue)
    for lead in db.q("""SELECT * FROM leads WHERE selected_date IS NOT NULL
                        AND NOT suppressed AND m3_sent_at IS NULL"""):
        d = lead["selected_date"]
        m1_today = bool(lead["m1_sent_at"] and lead["m1_sent_at"].astimezone(IST).date() == today)
        send_now = ((d - timedelta(days=1) == today and n.hour >= 18) or
                    (d == today and n.hour >= 8 and not m1_today))
        if send_now and _send(lead, "m3", m3_body(lead)):
            db.x("UPDATE leads SET m3_sent_at=now(), updated_at=now() WHERE id=%s",
                 (lead["id"],))

    # ---- Generic M3 on July 9 evening to non-responders ----
    if today == day_before_first and n.hour >= 18:
        for lead in db.q("""SELECT * FROM leads WHERE selected_date IS NULL
                            AND wa_state IN ('m1_sent','m2_sent') AND NOT suppressed
                            AND m3_sent_at IS NULL"""):
            if _send(lead, "m3_generic", m3_generic_body(lead)):
                db.x("UPDATE leads SET m3_sent_at=now(), updated_at=now() WHERE id=%s",
                     (lead["id"],))


def handle_inbound(phone, text):
    """Called by webhook. Matches lead by phone, parses date, acks."""
    lead = db.q("""SELECT * FROM leads WHERE phone=%s
                   ORDER BY updated_at DESC LIMIT 1""", (phone,), one=True)
    if not lead:
        return
    db.x("UPDATE leads SET last_inbound_at=now(), last_inbound_text=%s, updated_at=now() WHERE id=%s",
         (text, lead["id"]))
    db.log_msg(lead["id"], "in", "inbound", text)

    d = reply_parser.parse_date_reply(text)
    if d:
        db.x("""UPDATE leads SET selected_date=%s, wa_state='date_selected', updated_at=now()
                WHERE id=%s""", (d, lead["id"]))
        lead = db.q("SELECT * FROM leads WHERE id=%s", (lead["id"],), one=True)
        _send(lead, "ack", ack_body(lead))
