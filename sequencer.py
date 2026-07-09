"""Sequencer: runs every SEQUENCER_TICK_MIN minutes (IST-aware)."""
import hashlib
import random
import time
from datetime import datetime, date, timedelta, timezone
import config
import db
import wati
import parser as reply_parser

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST)


def _fmt(d: date):
    return d.strftime("%A %d %B")  # Friday 10 July


BRAND = {"RON": "Republic Of Nature", "ELEMENTS": "Elements Senior Living"}

# Tappable poll (rendered by Wasender) + the reliable text fallback ("1/2/3").
# Poll option text matches parser weekday tokens (Fri/Sat/Sun), so a tap round-
# trips exactly like a typed "1/2/3".
DAY_LINES = ("1\ufe0f\u20e3 Fri 10 July\n"
             "2\ufe0f\u20e3 Sat 11 July\n"
             "3\ufe0f\u20e3 Sun 12 July")
REPLY_HINT = "_Just reply 1, 2 or 3._"
DAY_POLL_Q = "Which day will you visit?"
DAY_POLL_OPTS = ["Fri 10 July", "Sat 11 July", "Sun 12 July"]

# ---------------------------------------------------------------------------
# Copy variation (anti-bulk-blast). Only the "soft" wrapper sentences swap;
# every FACT (dates, venue, savings line, reply instruction, entry-pass line)
# is fixed in the assembly below and never varies. Which option a given lead
# gets is deterministic on their lead id, so one person always sees one
# consistent version while different people scatter across wordings.
# Phrase banks approved by owner 7 July 2026.
# ---------------------------------------------------------------------------
V = {
    "ron_greet":   ["Hi {name}!", "Hello {name}!", "Hi {name}, hope you're doing well \u2014"],
    "ron_thanks":  ["Thank you for your interest in Republic Of Nature.",
                    "Thanks so much for your interest in Republic Of Nature.",
                    "Great to see your interest in Republic Of Nature."],
    "ron_invite":  ["You're invited to the {event}",
                    "We'd love to welcome you to the {event}",
                    "Come be our guest at the {event}"],
    # Greeting + org identity combined (avoids a double "greetings").
    "el_greet":    ["Hello {name}, greetings from Elements Senior Living.",
                    "Dear {name}, warm greetings from Elements Senior Living.",
                    "Hello {name}, a warm hello from Elements Senior Living."],
    "el_invite":   ["We'd be delighted to meet you at the {event}",
                    "We'd be glad to welcome you to the {event}",
                    "It would be our pleasure to host you at the {event}"],
    "confirm_close": ["we look forward to seeing you!", "we can't wait to welcome you!"],
    "greet":       ["Hi {name},", "Hello {name},", "Hi {name} \u2014"],
    "m2_checkin":  ["checking in from {brand}", "following up from {brand}",
                    "reaching out again from {brand}"],
    "m2_want":     ["we'd love to have you at the {event} this weekend",
                    "we'd be glad to see you at the {event} this weekend"],
    "m3_remind":   ["reminder from {brand}", "a quick reminder from {brand}"],
    "m3_lookfwd":  ["we look forward to seeing you", "we're looking forward to seeing you"],
    "close":       ["See you there!", "Looking forward to it!", "See you soon!"],
    "gen_invite":  ["we'd love to meet you", "we'd be glad to see you", "do drop by"],
    "gen_close":   ["See you there!", "Looking forward to it!"],
    "ack_open":    ["Noted", "Perfect", "Wonderful"],
}


def _pick(lead, slot):
    """Deterministic per-lead choice from bank V[slot]. Stable across process
    restarts (hashlib, not the salted built-in hash) so a re-sent message keeps
    the same wording. Returns the raw template (caller does {name}/{brand}/... )."""
    opts = V[slot]
    if len(opts) == 1:
        return opts[0]
    seed = f"{lead.get('id', '')}:{slot}".encode()
    idx = int(hashlib.md5(seed).hexdigest(), 16) % len(opts)
    return opts[idx]


def m1_body(lead, combined=False):
    name = (lead.get("name") or "").split(" ")[0] or "there"
    venue = config.EVENT_VENUE
    ev = config.EVENT_NAME
    if lead["project"] == "RON":
        greet = _pick(lead, "ron_greet").format(name=name)
        thanks = _pick(lead, "ron_thanks")
        invite = _pick(lead, "ron_invite").format(event=ev)
        intro = (f"{greet}\n\n{thanks}\n\n{invite}.\n"
                 f"\U0001F389 Carnival savings up to \u20b987L* \u2014 no registration fees, no pre-EMI.\n"
                 f"\U0001F4CD {venue}\n\U0001F5D3\ufe0f 10, 11 & 12 July")
    else:
        greet = _pick(lead, "el_greet").format(name=name)
        invite = _pick(lead, "el_invite").format(event=ev)
        intro = (f"{greet}\n\nThank you for your interest in Madhuram, Vandalur.\n\n"
                 f"{invite}.\n\U0001F4CD {venue}\n\U0001F5D3\ufe0f 10, 11 & 12 July")
    if lead.get("selected_date"):
        # Form already captured their preferred day: confirm, don't re-ask
        close = _pick(lead, "confirm_close")
        body = intro + (f"\n\nYou've chosen {_fmt(lead['selected_date'])} \u2014 {close}\n\n"
                        f"If you'd like to change the day:\n{DAY_LINES}")
    else:
        body = intro + f"\n\nWhich day will you visit?\n{DAY_LINES}\n\n{REPLY_HINT}"
    if combined:
        body += f"\n\n\U0001F4CD Location: {config.EVENT_MAPS_LINK}\nOpen all day."
    return body


def m2_body(lead):
    name = (lead.get("name") or "").split(" ")[0] or "there"
    brand = BRAND[lead["project"]]
    greet = _pick(lead, "greet").format(name=name)
    checkin = _pick(lead, "m2_checkin").format(brand=brand)
    want = _pick(lead, "m2_want").format(event=config.EVENT_NAME)
    return (f"{greet}\n\n{checkin} \u2014 {want}.\n\U0001F4CD {config.EVENT_VENUE}\n\n"
            f"Which day suits you?\n{DAY_LINES}\n\n{REPLY_HINT}")


def m3_body(lead):
    name = (lead.get("name") or "").split(" ")[0] or "there"
    brand = BRAND[lead["project"]]
    d = lead["selected_date"]
    when = "today" if d == now_ist().date() else f"tomorrow, {_fmt(d)}"
    greet = _pick(lead, "greet").format(name=name)
    remind = _pick(lead, "m3_remind").format(brand=brand)
    lookfwd = _pick(lead, "m3_lookfwd")
    close = _pick(lead, "close")
    return (f"{greet}\n\n{remind}: {lookfwd} {when} at {config.EVENT_VENUE}.\nOpen all day.\n\n"
            f"\U0001F4CD Location: {config.EVENT_MAPS_LINK}\n\n"
            f"Show this message at the entrance as your entry pass.\n{close}")


def m3_generic_body(lead):
    name = (lead.get("name") or "").split(" ")[0] or "there"
    brand = BRAND[lead["project"]]
    greet = _pick(lead, "greet").format(name=name)
    invite = _pick(lead, "gen_invite")
    close = _pick(lead, "gen_close")
    return (f"{greet}\n\nThe {config.EVENT_NAME} by {brand} runs this "
            f"Friday to Sunday (10\u201312 July) at {config.EVENT_VENUE}, all day.\n\n"
            f"Walk in on any day \u2014 {invite}.\n\n"
            f"\U0001F4CD Location: {config.EVENT_MAPS_LINK}\n\n"
            f"Show this message at the entrance as your entry pass.\n{close}")


def ack_body(lead):
    opener = _pick(lead, "ack_open")
    close = _pick(lead, "close")
    return (f"{opener} \u2014 see you on {_fmt(lead['selected_date'])} at {config.EVENT_VENUE}.\n\n"
            f"\U0001F4CD Location: {config.EVENT_MAPS_LINK}\n\n"
            f"Show this message at the entrance as your entry pass.\n{close}")


def paused():
    return (db.get_setting("global_pause", "false") == "true") or config.GLOBAL_PAUSE_ENV


# Quiet hours: no PROACTIVE cold sends (M1/M2/M3) after 19:30 IST -- late-night
# marketing annoys recipients and drags sender quality. Acks (1:1 replies to a
# lead's own tap) are exempt: they run through handle_inbound, not tick(), so a
# lead who picks a day at 9pm still gets their pass confirmation immediately.
QUIET_AFTER = (19, 30)  # IST hour, minute


def _quiet_now(n):
    return (n.hour, n.minute) >= QUIET_AFTER


def _template_for(lead, msg_type):
    """Map a sequencer message type to (template_name, params) for Wati.

    Returns None for types that go as free session text (acks -- always inside
    the 24h window opened by the customer's own reply, so no template needed).
    Proactive cold sends (m1/m2/m3/m3_generic) MUST be templates -- WhatsApp
    forbids cold free text. Param order matches the approved template's
    {{1}},{{2}},{{3}} slots; change a template's variables -> update here."""
    name = (lead.get("name") or "").split(" ")[0] or "there"
    proj = lead["project"]
    brand = BRAND[proj]
    T = config.WATI_TEMPLATES
    if msg_type == "m1":
        key = T["m1_ron"] if proj == "RON" else T["m1_elements"]
        return key, [name]                          # {{1}} name
    if msg_type == "m2":
        return T["m2"], [name, brand]               # {{1}} name, {{2}} brand
    if msg_type == "m3":
        d = lead["selected_date"]
        when = "today" if d == now_ist().date() else f"tomorrow, {_fmt(d)}"
        return T["m3"], [name, brand, when]         # {{1}} name {{2}} brand {{3}} when
    if msg_type == "m3_generic":
        return T["m3_generic"], [name, brand]       # {{1}} name, {{2}} brand
    return None                                     # ack -> session text


def _send(lead, msg_type, body, jitter=True):
    if paused():
        return False
    if not wati.rate_ok():
        db.set_setting("rate_capped_at", now_ist().isoformat())
        return False
    # Human-like pause before bulk outbound so back-to-back sends don't read as
    # a machine burst. Acks (1:1 replies) pass jitter=False and go immediately.
    if jitter and config.SEND_JITTER_MAX_SEC > 0:
        lo = min(config.SEND_JITTER_MIN_SEC, config.SEND_JITTER_MAX_SEC)
        time.sleep(random.uniform(lo, config.SEND_JITTER_MAX_SEC))
    # Proactive stages (m1/m2/m3/m3_generic) send as approved templates; acks
    # (no template mapping) go as free session text inside the reply window.
    # `body` is still logged so the dashboard shows readable copy per send.
    tpl = _template_for(lead, msg_type)
    if tpl:
        template_name, params = tpl
        ok, detail = wati.send_template(lead["phone"], template_name, params)
    else:
        ok, detail = wati.send_text(lead["phone"], body)
    db.log_msg(lead["id"], "out", msg_type, body, ok=ok, detail=detail)
    if not ok:
        # Suppress ONLY on a clearly per-lead permanent error (number not on
        # WhatsApp). We deliberately do NOT suppress on the attempt count: while
        # templates are still PENDING every send fails for a reason that has
        # nothing to do with the lead, and a 3-strike rule would wrongly kill
        # off good leads. Such failures just keep their state and retry once the
        # template is approved. `detail` is matched loosely across Wati/WhatsApp
        # phrasings for an invalid/nonexistent recipient.
        attempts = (lead.get("send_attempts") or 0) + 1
        d = (detail or "").lower()
        permanent = any(s in d for s in
                        ("does not exist", "not a valid whatsapp",
                         "invalid whatsapp number", "not a whatsapp"))
        db.x("UPDATE leads SET send_attempts=%s, updated_at=now() WHERE id=%s",
             (attempts, lead["id"]))
        if permanent:
            db.x("UPDATE leads SET wa_state='invalid', suppressed=TRUE, updated_at=now() WHERE id=%s",
                 (lead["id"],))
    return ok


def _send_poll(lead, msg_type, jitter=True):
    """No-op under Wati. On official WhatsApp the day-picker is quick-reply
    buttons baked INTO the approved M1/M2 templates, so they render with the
    template send in _send -- there is no separate poll message. Kept as a stub
    so tick()'s call sites stay unchanged; a tapped button returns through the
    webhook as its label text ('Fri 10 July') and the reply parser handles it
    exactly like a typed '1/2/3'."""
    return True


def tick():
    n = now_ist()
    today = n.date()
    last_event_day = config.EVENT_DATES[-1]
    day_before_first = config.EVENT_DATES[0] - timedelta(days=1)   # July 9

    # Quiet hours: after 19:30 IST do no proactive sends this tick. Acks are
    # unaffected (they fire from the inbound webhook, not here).
    if _quiet_now(n):
        return

    # Bounded send budget per tick: keeps one tick short (jitter can make each
    # send take seconds) and spreads a backlog across ticks -> more human, and
    # /admin/poll-now returns without hanging. Remaining leads picked next tick.
    budget = config.SEND_BATCH_PER_TICK

    # ---- M1 for queued leads ----
    for lead in db.q("""SELECT * FROM leads WHERE wa_state='queued'
                        AND NOT suppressed AND phone IS NOT NULL"""):
        if budget <= 0:
            return
        if today > last_event_day:
            continue
        combined = (today >= day_before_first)  # late qualifier: fold venue into M1
        if _send(lead, "m1", m1_body(lead, combined=combined)):
            budget -= 1
            new_state = "date_selected" if lead.get("selected_date") else "m1_sent"
            db.x("UPDATE leads SET wa_state=%s, m1_sent_at=now(), updated_at=now() WHERE id=%s",
                 (new_state, lead["id"]))
            if not lead.get("selected_date"):   # date-ask -> offer tappable poll
                _send_poll(lead, "m1_poll")

    # ---- M2: 24h after M1, no reply, no date ----
    for lead in db.q("""SELECT * FROM leads WHERE wa_state='m1_sent' AND NOT suppressed
                        AND selected_date IS NULL AND last_inbound_at IS NULL
                        AND m1_sent_at < now() - interval '24 hours'
                        AND m2_sent_at IS NULL"""):
        if budget <= 0:
            return
        if today > last_event_day:
            continue
        if _send(lead, "m2", m2_body(lead)):
            budget -= 1
            db.x("UPDATE leads SET wa_state='m2_sent', m2_sent_at=now(), updated_at=now() WHERE id=%s",
                 (lead["id"],))
            _send_poll(lead, "m2_poll")

    # ---- M3 rules ----
    # (a) evening before selected date, from 18:00 IST
    # (b) selected date == today (lead picked same-day): send from 08:00 IST,
    #     unless M1 went out today (combined M1 + ack already carried the venue)
    for lead in db.q("""SELECT * FROM leads WHERE selected_date IS NOT NULL
                        AND NOT suppressed AND m3_sent_at IS NULL"""):
        if budget <= 0:
            return
        d = lead["selected_date"]
        m1_today = bool(lead["m1_sent_at"] and lead["m1_sent_at"].astimezone(IST).date() == today)
        send_now = ((d - timedelta(days=1) == today and n.hour >= 18) or
                    (d == today and n.hour >= 8 and not m1_today))
        if send_now and _send(lead, "m3", m3_body(lead)):
            budget -= 1
            db.x("UPDATE leads SET m3_sent_at=now(), updated_at=now() WHERE id=%s",
                 (lead["id"],))

    # No generic M3 to no-day leads: owner chose not to remind non-responders the
    # night before (no gtb_m3_generic template approved). Leads who never pick a
    # day simply get no further message.


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
        _send(lead, "ack", ack_body(lead), jitter=False)
