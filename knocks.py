"""The knock engine (build-plan task 17) + stop-on-reply (task 18).

A lead who fills a form has a shut 24-hour window, so the first move must be an
approved template. Four of them, at day 0 / 3 / 10 / 25:

    t1 lifestyle -> t2 location -> t3 low-density -> t6 visit invitation

WHY THIS DID NOT EXIST UNTIL NOW. The carnival's send loops were deleted in task 1b
and their replacement was gated behind the suppression list (task 16) -- the rule
that no lead may be knocked before checking whether a salesperson already owns
them. That interlock was right for the 48,354 old Sell.do leads. It does not apply
to somebody who filled OUR form this morning, and the campaign allow-list already
restricts us to exactly those people. So the gate that blocked this is satisfied by
the allow-list, not bypassed by it.

WHAT STOPS A KNOCK. In order, cheapest first:

  * the campaign allow-list        -- only our own campaigns, never GT Bharathi's
  * any inbound, ever              -- task 18. One reply ends the sequence for good;
                                      from then on the qualifier owns the
                                      conversation and a scheduled template would
                                      talk over a live human being.
  * a terminal outcome             -- qualified / visit_booked / dead / escalated
  * fatigue.check()                -- the 4-per-journey counter AND the
                                      non-resettable 2-per-7-days ceiling
  * sendgate.check()               -- master switch, pauses, opt-out, retry ceiling

The last two are not re-implemented here. There is one door and this walks through
it like everything else.
"""
import logging
from datetime import datetime, timedelta, timezone

import config
import db
import fatigue
import sequencer

log = logging.getLogger("knocks")

# (days_after_signup, config.KNOCK_TEMPLATES key)
KNOCK_SCHEDULE = [
    (0,  "t1_lifestyle"),
    (3,  "t2_location"),
    (10, "t3_low_density"),
    (25, "t6_visit"),
]

# Templates and the variables they actually declare in Wati, verified against the
# live account 2026-08-02. A wrong parameter count is a failed send, so this is
# data, not a guess: t1/t2/t3 take the buyer's first name as {{1}}; t6 takes none.
TEMPLATE_TAKES_NAME = {
    "t1_lifestyle": True,
    "t2_location": True,
    "t3_low_density": True,
    "t6_visit": False,
}


def msg_type_for(step_key):
    return f"knock_{step_key}"


def _first_name(name):
    """Templates open 'Hi {{1}},'. A Sell.do name carries a '(#53773)' suffix and
    is often the full name doubled; neither belongs in a greeting."""
    if not name:
        return "there"
    cleaned = name.split("(")[0].strip()
    first = cleaned.split()[0] if cleaned.split() else ""
    return first[:40] or "there"


def knock_state(lead_id):
    """(how many knocks RECEIVED, when the last one went out).

    ok=TRUE only. A template that never reached the handset has not been spent,
    and counting it would silently shorten the sequence for exactly the people we
    already struggled to reach.
    """
    r = db.q("""SELECT count(*) n, max(ts) last_at FROM message_log
                WHERE lead_id=%s AND direction='out' AND ok=TRUE
                  AND msg_type LIKE 'knock\\_%%'""", (lead_id,), one=True) or {}
    return r.get("n", 0), r.get("last_at")


def _min_gap_days(step_index):
    """Days that must pass since the PREVIOUS knock before this one may go.

    Without this the schedule is anchored to signup alone, so a lead who filled
    the form 20 days ago receives knock 1 today, knock 2 tomorrow (day 3 is long
    past) and knock 3 the day after -- four templates in four days instead of
    across 25. The fatigue ceiling would blunt that, but a backstop is not a
    cadence. The spacing is part of the design, so it is enforced directly.
    """
    if step_index <= 0:
        return 0
    return KNOCK_SCHEDULE[step_index][0] - KNOCK_SCHEDULE[step_index - 1][0]


def due(limit=None):
    """Leads whose next knock is due right now. Oldest signup first."""
    limit = limit or config.SEND_BATCH_PER_TICK
    campaigns = [c.lower() for c in config.SELLDO
                 .get(config.DIRECT_INBOUND_PROJECT, {}).get("campaigns") or []]
    if not campaigns:
        return []

    rows = db.q("""
        SELECT l.*, COALESCE(l.selldo_response_at, l.created_at) AS anchor
        FROM leads l
        LEFT JOIN conversations c ON c.lead_id = l.id
        WHERE l.phone IS NOT NULL
          AND NOT l.suppressed
          AND lower(trim(l.campaign)) = ANY(%s)
          -- task 18: ANY inbound ends the sequence, permanently.
          AND l.last_inbound_at IS NULL
          AND c.outcome IS NULL
        ORDER BY COALESCE(l.selldo_response_at, l.created_at) ASC
        LIMIT %s""", (campaigns, limit * 5)) or []

    now = datetime.now(timezone.utc)
    out = []
    for lead in rows:
        sent, last_at = knock_state(lead["id"])
        if sent >= len(KNOCK_SCHEDULE) or sent >= config.KNOCK_MAX_PER_JOURNEY:
            continue
        days_after, step_key = KNOCK_SCHEDULE[sent]
        anchor = lead["anchor"]
        if anchor is None:
            continue
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        # Due against BOTH clocks: time since they signed up, and time since we
        # last knocked. A backlog lead must not receive the whole sequence at once.
        if now < anchor + timedelta(days=days_after):
            continue
        if last_at is not None:
            if last_at.tzinfo is None:
                last_at = last_at.replace(tzinfo=timezone.utc)
            if now < last_at + timedelta(days=_min_gap_days(sent)):
                continue
        out.append((lead, sent, step_key))
        if len(out) >= limit:
            break
    return out


def send_knock(lead, step_index, step_key):
    """Send one knock. Returns True only if it actually went out."""
    template = config.KNOCK_TEMPLATES.get(step_key)
    if not template:
        log.warning("no template configured for %s", step_key)
        return False

    allowed, reason = fatigue.check(lead["phone"], msg_type_for(step_key),
                                    project=lead.get("project"))
    if not allowed:
        db.log_msg(lead["id"], "out", msg_type_for(step_key), None, ok=False,
                   detail=f"blocked:fatigue:{reason}")
        return False

    if step_index == 0:
        fatigue.start_journey(lead["phone"], lead.get("project"))

    params = [_first_name(lead.get("name"))] if TEMPLATE_TAKES_NAME[step_key] else []
    ok = sequencer._send(lead, msg_type_for(step_key), template=template,
                         params=params)
    if ok:
        db.x("UPDATE leads SET wa_state=%s, updated_at=now() WHERE id=%s",
             (f"knock_{step_index + 1}_sent", lead["id"]))
        log.info("lead %s knock %s (%s) sent", lead["id"], step_index + 1, step_key)
    return ok


def knock_now(lead):
    """Send knock 1 to a lead that has just this second been created.

    The leadgen webhook path. Waiting for the next scheduled tick would cost up
    to five minutes, and the whole point of the webhook is that the buyer is
    still looking at their phone.

    Every guard still applies -- this calls the same send_knock as the scheduler,
    so fatigue, the allow-list, opt-out and both pauses are enforced identically.
    Refuses if anything has already been sent, so a Meta retry cannot double-knock.
    """
    if not lead or not lead.get("phone"):
        return False
    if not config.campaign_allowed(lead.get("project"), lead.get("campaign")):
        db.log_msg(lead["id"], "out", "knock_skipped", None, ok=False,
                   detail=f"campaign={lead.get('campaign')!r} not in allow-list")
        return False
    sent, _last = knock_state(lead["id"])
    if sent:
        return False
    return send_knock(lead, 0, KNOCK_SCHEDULE[0][1])


def run():
    """One scheduled pass. Returns how many knocks went out."""
    batch = due()
    if not batch:
        return 0
    sent = 0
    for lead, step_index, step_key in batch:
        try:
            if send_knock(lead, step_index, step_key):
                sent += 1
        except Exception as e:                      # one bad lead must not stop the pass
            log.exception("knock failed for lead %s: %s", lead.get("id"), e)
    if sent:
        db.set_setting("last_knock_run", f"{sent} sent")
    return sent
