"""Sequencer: runs every SEQUENCER_TICK_MIN minutes (IST-aware).

STATE AFTER PHASE 0 TASK 1b (2026-07-30): the carnival lifecycle is gone and the
replacement engine is not built yet. What remains is deliberately a skeleton:

  * the ONE send door (`_send`) with the safety gate in front of it
  * the rolling-24h tier arithmetic and quiet hours, which are number-level
    protections that survive the redesign unchanged
  * inbound recording -- every inbound message is still matched to a lead and
    written to `message_log`, so nothing arriving today is lost
  * `tick()` as a no-op scaffold

What was removed and where it goes:

  | Removed                                   | Rebuilt by |
  |-------------------------------------------|------------|
  | M1/M2/M3 send loops, event-day guards     | task 17 (knock engine, day 0/3/10/25) |
  | Carnival copy banks + body builders       | task 17 (approved templates, not code) |
  | Day-picker reply parsing + ack            | nothing -- replies are never predefined; the qualifier reads them (task 20) |
  | `welcome_body` / walk-in lead creation    | tasks 7 + 14 (intake stamps project from the ad or list) |
  | `_detect_project` (brand from message text) | nothing -- rev 2 forbids it outright, see below |

`_detect_project` is not coming back. It read the brand out of the customer's own
message text, and the customer controls that string. Rev 2 requires `project` to
be stamped from the ad or the source list at ingestion and never inferred from
what someone types -- that stamp is the brand fence the whole KB ring-fence rests
on. It was only ever reachable with WALKIN_ENABLED=true, which has always
defaulted false, so removing it changes no live behaviour.
"""
from datetime import datetime, timedelta, timezone
import logging
import config
import db
import wati
import sendgate
import optout
import failures
import jobs

log = logging.getLogger("sequencer")

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST)


BRAND = {"RON": "Republic Of Nature", "ELEMENTS": "Elements Senior Living"}


def paused():
    """Kept as the dashboard's read (app.py). The authoritative implementation
    lives in sendgate so there is one definition of "are we allowed to send",
    not two that can drift apart."""
    return sendgate.paused()


# Quiet hours: 19:30 -> 08:00 IST. Held for the knock engine to consult (task
# 17): a cold nurture knock must never land late at night. Kept out of
# sendgate.check() on purpose -- quiet hours are a property of the KIND of
# message, not of the person, and a live reply inside an open 24h window is
# rightly exempt. sendgate holds the person-level rules only.
QUIET_START = (19, 30)   # IST hour, minute
QUIET_END = (8, 0)


def quiet_now(n=None):
    n = n or now_ist()
    t = (n.hour, n.minute)
    return t >= QUIET_START or t < QUIET_END


def _daily_sends():
    """Count PROACTIVE outbound sends in the last rolling 24h. This is what
    Meta's messaging tier limits -- each is a business-initiated conversation.
    Session replies inside a window the customer opened are excluded: they do not
    consume the tier. Only ok=TRUE rows count, so neither failed attempts nor
    gate-blocked sends ever eat the daily allowance."""
    r = db.q("""SELECT count(*) AS n FROM message_log
                WHERE direction='out' AND ok AND msg_type LIKE 'knock%'
                AND ts > now() - interval '24 hours'""", one=True)
    return r["n"] if r else 0


def daily_budget():
    """How many proactive sends are left against the number's tier allowance.
    Used by the knock engine's scheduler (task 17)."""
    left = config.DAILY_SEND_CAP - _daily_sends()
    if left <= 0:
        db.set_setting("daily_capped_at", now_ist().isoformat())
    return max(0, left)


def _send(lead, msg_type, body=None, template=None, params=None, sources=None,
          log_lead_id=None):
    """THE one door. Every outbound message in the system leaves through here.

    What goes on the wire:
      * `template` + `params` -> approved WhatsApp template. Required for any
        cold/proactive send; WhatsApp forbids cold free text.
      * `body` alone          -> free session text. Only delivers inside the 24h
        window the customer opened by messaging us.

    Passing BOTH sends the template and files `body` as the readable record of it.
    A template send is five loose parameter values, so without that the log would
    say a message went out and not what it said. Used by the staff handoff card.

    `log_lead_id` decides WHO THE MESSAGE IS ABOUT, which is not always who it is
    sent to. A staff card is addressed to a salesperson but concerns a buyer; it
    must be filed under the buyer, while `lead["id"]` stays the recipient's so the
    failure handling below cannot suppress the wrong person.

    The template NAME is now passed in by the caller rather than looked up from a
    hardcoded map. The old code resolved `msg_type` against six carnival template
    names in config, which coupled the send path to one campaign's copy; the
    knock engine and the qualifier need different sets, and a lookup table would
    have to grow a branch per campaign forever.

    The four Phase 0 safety rules live inside sendgate.check(), not here, so this
    call site does not change again as tasks 2-4 land.
    """
    allowed, reason = sendgate.check(lead.get("phone"), msg_type,
                                     project=lead.get("project"))
    if not allowed:
        # Logged, not silently dropped: a blocked send is a fact worth counting.
        # ok=False keeps it out of the rate/tier arithmetic, which sums ok=TRUE.
        db.log_msg(log_lead_id or lead["id"], "out", msg_type, body, ok=False,
                   detail=f"blocked:{reason}")
        return False
    if not wati.rate_ok():
        db.set_setting("rate_capped_at", now_ist().isoformat())
        return False

    # No send jitter. It was defensive cover for Wasender -- an unofficial
    # automation bridge, where a burst of identical outbounds reads as a bot and
    # gets the number banned. On the official WhatsApp Cloud API via Wati, bursts
    # are not a ban signal: the messaging tier is the real constraint and it is
    # enforced explicitly by the provider, which DAILY_SEND_CAP and
    # MAX_SENDS_PER_HOUR already respect. Sleeping bought nothing and delayed
    # every send. (Owner, 2026-07-31.)
    if template:
        ok, detail = wati.send_template(lead["phone"], template, params)
    else:
        ok, detail = wati.send_text(lead["phone"], body)
    # Classify the failure before logging it, so the retry ceiling (task 4) can
    # count only the failures that are actually about this recipient. Successes
    # carry no class.
    fail_class = None if ok else failures.classify(detail)

    # Store the provider's message id alongside the send so a delivery callback
    # (task 5) can be joined back to the message that caused it. Best-effort: if
    # the id is absent the callback still lands, matched on phone instead.
    db.log_msg(log_lead_id or lead["id"], "out", msg_type, body, ok=ok, detail=detail,
               provider_msg_id=wati.extract_msg_id(detail),
               fail_class=fail_class, sources=sources)

    if ok:
        # Reset on success. The ceiling itself counts only failures since the last
        # successful send, so this is bookkeeping for the dashboard rather than the
        # mechanism -- but leaving a stale attempt count on a working number reads
        # as a problem that no longer exists.
        if lead.get("send_attempts"):
            db.x("UPDATE leads SET send_attempts=0, updated_at=now() WHERE id=%s",
                 (lead["id"],))
        return True

    db.x("UPDATE leads SET send_attempts=%s, updated_at=now() WHERE id=%s",
         ((lead.get("send_attempts") or 0) + 1, lead["id"]))

    # Suppress on the FIRST hard recipient failure rather than after three. A
    # number that is not on WhatsApp will never become one, so spending two more
    # attempts to confirm it only wastes sends and buries real failures in noise.
    # Everything else -- transient and system failures -- is left to the ceiling in
    # sendgate, which is what stops our own unapproved template from killing off a
    # good lead.
    if failures.is_hard_recipient_failure(detail):
        db.x("""UPDATE leads SET wa_state='invalid', suppressed=TRUE, updated_at=now()
                WHERE id=%s""", (lead["id"],))
    return False


def tick():
    """Scheduled pass. Runs the knock engine.

    Empty until 2026-08-02. The carnival send loops were deleted in task 1b and
    their replacement (task 17) was gated behind the suppression list -- the rule
    that no lead may be knocked before checking whether a salesperson already owns
    them. That interlock protected the 48,354 old Sell.do leads; it does not apply
    to somebody who filled our own form this morning, and the campaign allow-list
    now restricts every send to exactly those people. Meanwhile 39 buyers filled
    the live forms and heard nothing.

    Imported here rather than at module scope: knocks imports sequencer for the
    one send door, and a top-level import would be circular.
    """
    db.set_setting("last_tick_at", now_ist().isoformat())
    import knocks
    try:
        n = knocks.run()
        if n:
            log.info("knock engine sent %s", n)
    except Exception as e:
        log.exception("knock engine failed: %s", e)
        db.set_setting("knock_error", str(e)[:500])


def _adopt_direct_inbound(phone, sender_name, opted_out=False, source=None):
    """A stranger messaged us. Make them a lead so the bot can answer.

    Returns the new lead row, or None if we must stay silent.

    Only ever called when the phone has NO lead of any kind -- that lookup is the
    GT Bharathi guard. A number already attached to one of their leads keeps that
    row, stays out of the allow-list, and is left alone.

    The project comes from config, never from the customer's words. That was the
    objection to the old walk-in path (task 1b) and it is why this is safe now:
    one project runs on this number, so there is nothing to guess.
    """
    if not config.DIRECT_INBOUND_ENABLED:
        return None
    if opted_out:
        # They opened with a stop word. Creating a lead for someone whose first act
        # was to opt out is exactly how a suppression list gets quietly undone.
        return None

    project = config.DIRECT_INBOUND_PROJECT
    db.x("""INSERT INTO leads (project, selldo_lead_id, name, phone, campaign,
                               selldo_status, wa_state, last_inbound_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, 'direct_inbound', 'queued', now(), now())
            ON CONFLICT (project, selldo_lead_id) DO NOTHING""",
         (project, f"direct:{phone}", sender_name, phone,
          config.DIRECT_INBOUND_CAMPAIGN))
    lead = db.q("""SELECT * FROM leads WHERE project=%s AND selldo_lead_id=%s""",
                (project, f"direct:{phone}"), one=True)
    if lead:
        db.log_msg(lead["id"], "in", "direct_inbound_lead", None,
                   detail=f"stranger {phone} adopted as {config.DIRECT_INBOUND_CAMPAIGN} "
                          f"({project}); no prior lead existed"
                          + (f" | source: {source}" if source else " | source: none"))
    return lead


def _queue_ctwa_capture(lead, phone, source):
    """Ask the worker to go and fetch this arrival's click id, if it looks like one.

    Deliberately does NOT call Wati here. This runs inside the webhook request, and
    the webhook's only job is to answer WhatsApp fast; an HTTP round-trip to Wati on
    the inbound path is how a working integration starts looking broken.

    THE TRIGGER IS THE WEBHOOK'S OWN EVIDENCE, not a guess from the message text.
    `sourceId=` in the flattened source string means Meta told Wati this came from an
    ad. Landing-page walk-ins arrive as `sourceType=7 bsuid=...` with no sourceId and
    are skipped -- they have no click id to fetch, so asking would burn a call per
    walk-in to learn nothing.

    Dedup is per phone and permanent, so a chatty buyer produces one lookup, not one
    per message. The cost of that choice: a lookup that exhausts its retries is never
    retried automatically. scripts/backfill_ctwa.py exists to sweep those up.
    """
    if not lead or not source:
        return
    if "sourceId=" not in source:
        return
    if lead.get("ctwa_clid"):
        return
    try:
        jobs.enqueue(jobs.KIND_CTWA_CAPTURE,
                     {"lead_id": lead["id"], "phone": phone},
                     phone=None,
                     dedup_key=f"ctwa:{phone}",
                     # Let the reply go out first, and give Wati a moment to have
                     # the message queryable through its own API.
                     delay_seconds=30)
    except Exception as e:
        # A missed click id must never cost us the inbound. Losing the message is
        # unrecoverable; losing the attribution is a row in a report.
        log.warning("ctwa capture enqueue failed for %s: %s", phone, e)


def handle_inbound(phone, text, sender_name=None, allow_create=False, source=None):
    """Called by the webhook. Records an inbound message against its lead.

    RECORD-ONLY at this stage, deliberately. The bot does not reply: replies are
    the qualifier's job (task 20) and it does not exist yet, and a knock engine
    that must stand down on reply (task 18) does not exist either. Recording now
    still matters -- an inbound that is not written down is unrecoverable, and
    `last_inbound_at` is what the stop-on-reply and window-state logic will read.

    Lead CREATION from an inbound message is removed. It previously guessed the
    brand from the customer's own message text, which rev 2 forbids; project must
    be stamped from the ad (`ctwa_clid`) or the source list at ingestion (tasks 7
    and 14). Until then an unrecognised number is logged for a human, not turned
    into a marketing target.

    `allow_create` is retained in the signature because app.py's authenticated
    webhook route passes it. It is currently inert; task 14 gives it meaning
    again, on the correct basis.
    """
    lead = db.q("""SELECT * FROM leads WHERE phone=%s
                   ORDER BY updated_at DESC LIMIT 1""", (phone,), one=True)

    # OPT-OUT RUNS FIRST, and runs even when we cannot identify the sender.
    # A person who says "stop" from a number we have no lead for is still a person
    # who said stop -- recording it means a later import or form fill can never
    # turn them into a target. This ordering is the whole point: the check happens
    # before any agent, any reply logic and any lead lookup can matter.
    scope, matched = optout.handle_inbound_text(
        phone, text, project=(lead or {}).get("project"))

    if not lead:
        lead = _adopt_direct_inbound(phone, sender_name, opted_out=bool(scope),
                                     source=source)
    if not lead:
        db.log_msg(None, "in", "unattributed", text,
                   detail=f"phone={phone} no_lead needs_human"
                          + (f" optout={scope}:{matched}" if scope else "")
                          + (f" | {source}" if source else ""))
        return

    db.x("""UPDATE leads SET last_inbound_at=now(), last_inbound_text=%s,
                             updated_at=now() WHERE id=%s""", (text, lead["id"]))
    db.log_msg(lead["id"], "in", "inbound", text,
               detail=(f"optout={scope}:{matched}" if scope else None))

    # THE CLICK ID, and the leak this closes.
    #
    # `source` was previously used only inside _adopt_direct_inbound, so it reached
    # the database for STRANGERS and was silently dropped for anyone who was already
    # a lead. That is exactly backwards: a retargeting ad is aimed at people we
    # already know, and the referral rides the first message after the click and no
    # other. Every dropped one was unrecoverable.
    #
    # Placed after the lead is resolved so both paths -- new stranger and returning
    # lead -- go through the one door.
    _queue_ctwa_capture(lead, phone, source)
