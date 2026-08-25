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
import os
import re
from datetime import datetime, timedelta, timezone

import config
import db
import fatigue
import sequencer

log = logging.getLogger("knocks")

# How long an ad-tapper must have been silent before the ladder may treat them as
# somebody who never spoke. Short enough that the lead is still warm, long enough
# that nobody mid-conversation is interrupted -- their anchor is the tap itself, so
# without this t1 would be due the moment they went quiet.
QUIET_DAYS = int(os.environ.get("KNOCK_REVIVE_QUIET_DAYS", "3"))

# (days_after_signup, config.KNOCK_TEMPLATES key)
#
# FIFTEEN DAYS, NOT TWENTY-FIVE. Owner, 2026-08-25. The visit invitation sat on
# day 25 and had therefore NEVER BEEN SENT -- not once in the life of the system,
# while t2 and t3 went out 83 and 80 times. Almost no journey survives 25 days
# intact, so the one template whose job is asking for a site visit never fired.
#
# WHY THESE DAYS AND NOT TIGHTER. FATIGUE_MAX_PER_WINDOW is 2 per 7 days and
# nothing can reset it. This ladder sits exactly on that ceiling -- 0+3, then
# 3+8, then 8+15 -- so every step can actually go out. Compressing further, e.g.
# (0, 2, 7, 14), puts three sends inside the first week: the third is silently
# refused by the fatigue cap, so the sequence would look faster and deliver LESS.
# The gaps are the schedule's own guardrail, not a preference.
#
# The order is unchanged and deliberate: three selling points, then the ask.
KNOCK_SCHEDULE = [
    (0,  "t1_lifestyle"),
    (3,  "t2_location"),
    (8,  "t3_low_density"),
    (15, "t6_visit"),
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
    """Templates open 'Hi {{1}},'. Return something that reads like a name.

    A Sell.do name carries a '(#53773)' suffix and is often the full name doubled.
    Meta names are worse: people fill forms with decorated unicode, and a real one
    in the queue today was "꧁𓊈𒆜𝘔𝘠 𝘕𝘈𝘔𝘌 𝘚𝘏𝘈𝘐𝘓 𝘉𝘖𝘚". Greeting somebody with that
    looks broken, and it risks the template send failing outright.

    So: strip the id suffix, keep only plain letters, and fall back to "there"
    when nothing sensible survives. "Hi there," is a perfectly good greeting; a
    mojibake salutation is not.
    """
    if not name:
        return "there"
    cleaned = re.sub(r"\(#?\d+\)", " ", str(name))
    for word in cleaned.split():
        # ASCII letters only -- decorated unicode look-alikes are not letters here,
        # which is exactly why this rejects them.
        plain = re.sub(r"[^A-Za-z'\-]", "", word)
        if len(plain) >= 2:
            return plain[:40]
    return "there"


def knock_state(phone):
    """(how many knocks this PERSON received, when the last one went out).

    KEYED ON PHONE, NOT LEAD ID -- the Phase 0 rule, and it was broken here until
    2026-08-02. One buyer, lavanya, was created twice: once by the promote path
    from her Meta form (campaign RON_Villa_BM) and forty minutes later by the
    Sell.do poll (campaign RON_Meta_BM). Both rows were allow-listed, both looked
    un-knocked, and she received the same template twice.

    `leads` is UNIQUE (project, selldo_lead_id), so the schema GUARANTEES one human
    can be several rows. Counting per lead therefore counts the rows, not the
    person -- and the person is who receives the message.

    ok=TRUE only: a template that never reached the handset has not been spent.
    """
    r = db.q("""SELECT count(*) n, max(ml.ts) last_at
                FROM message_log ml JOIN leads l ON l.id = ml.lead_id
                WHERE l.phone = %s AND ml.direction='out' AND ml.ok=TRUE
                  AND ml.msg_type LIKE 'knock\\_%%'""", (phone,), one=True) or {}
    return r.get("n", 0), r.get("last_at")


def attempt_state(phone, step_key):
    """(attempts at this step, when the last one was tried) counting REFUSALS too.

    THE COUNTERPART TO knock_state, AND IT MUST NOT FILTER ON `ok`.

    knock_state deliberately counts only sends that reached the handset, so when
    db.mark_meta_refused() flips a refused knock to ok=FALSE the step becomes due
    again -- that is how a retry happens at all. But it also means the refused
    attempt becomes invisible, and two things then break unless something counts it:

      * the ceiling. Ten attempts have to be ten, not infinity.
      * the clock. `last_at` in due() falls back to the previous SUCCESSFUL knock,
        or to None when there was none -- and a None gap means the retry fires again
        on the very next tick, in a loop, all day.

    So attempts are counted from every row for this step, ok or not, and the retry
    gap is measured from the last ATTEMPT rather than the last delivery.

    `blocked:` rows are EXCLUDED. Those are our own gates -- fatigue cap, opt-out,
    send disabled -- and nothing went on the wire, so they are not attempts at
    reaching the person and must not consume one of the ten. Counting them would let
    a week of fatigue blocks exhaust the ceiling without Meta ever seeing a send.
    """
    r = db.q("""SELECT count(*) AS n, max(ml.ts) AS last_at
                  FROM message_log ml JOIN leads l ON l.id = ml.lead_id
                 WHERE l.phone = %s AND ml.direction = 'out'
                   AND ml.msg_type = %s
                   AND (ml.detail IS NULL OR ml.detail NOT LIKE 'blocked:%%')""",
             (phone, msg_type_for(step_key)), one=True) or {}
    return r.get("n", 0) or 0, r.get("last_at")


def variant_for(step_key, attempts):
    """Which wording to try now. Cycles, so 10 attempts over 3 variants is fine.

    Falls back to the single configured template when no alternates exist, which is
    the state of every step until marketing has alternates approved.
    """
    variants = config.KNOCK_TEMPLATE_VARIANTS.get(step_key) or []
    if not variants:
        return config.KNOCK_TEMPLATES.get(step_key)
    return variants[attempts % len(variants)]


def _give_up(lead, step_key, attempts):
    """Ten attempts and Meta refused every one. Stop, permanently.

    Owner 2026-08-11: "left alone permanently marked as lost". NOT `suppressed` --
    that field means the person asked us to stop, and conflating our own delivery
    failure with their instruction would make an opt-out list that cannot be trusted.
    """
    db.x("""UPDATE leads SET knock_lost_at=now(), wa_state='knock_lost',
                             updated_at=now()
             WHERE id=%s AND knock_lost_at IS NULL""", (lead["id"],))
    db.log_msg(lead["id"], "out", "knock_gave_up", None, ok=False,
               fail_class="meta_refused",
               detail=f"{step_key}: {attempts} attempts all refused by Meta; "
                      f"marked lost, no further knocks")


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


# The eligibility and due-ness test, written ONCE and shared.
#
# due() takes a page of it; due_count() counts all of it, and the watchdog
# compares that count against what actually went out. A monitor that measures
# something subtly different from the engine it watches is worse than no monitor:
# it reports healthy while the engine starves -- which is the exact failure it
# exists to catch. So the two share these strings rather than resembling them.
#
# Split in three because the CTE must precede the SELECT, and the two callers
# need different SELECT lists over the same FROM/WHERE.
_DUE_CTE = r"""
        WITH ks AS (
            -- Phone-keyed, exactly like knock_state(): one human is routinely
            -- several lead rows, and a knock reaches the person, not the row.
            SELECT l2.phone,
                   count(*) FILTER (WHERE ml.ok)   AS sent,
                   max(ml.ts) FILTER (WHERE ml.ok) AS last_at
            FROM message_log ml
            JOIN leads l2 ON l2.id = ml.lead_id
            WHERE ml.direction = 'out' AND ml.msg_type LIKE 'knock\_%%'
            GROUP BY l2.phone
        )
"""

_DUE_SELECT = " SELECT l.*, COALESCE(l.selldo_response_at, l.created_at) AS anchor "

_DUE_FROM_WHERE = """
        FROM leads l
        LEFT JOIN conversations c ON c.lead_id = l.id
        LEFT JOIN ks ON ks.phone = l.phone
        WHERE l.phone IS NOT NULL
          AND NOT l.suppressed
          -- Ten refusals and we stopped. Excluded here rather than checked per
          -- lead so a lost number cannot occupy a slot in the batch forever.
          AND l.knock_lost_at IS NULL
          AND lower(trim(l.campaign)) = ANY(%s)
          -- task 18: ANY inbound ends the sequence, permanently -- UNLESS the only
          -- thing that ever arrived was an ad prefill the buyer never typed.
          --
          -- 2026-08-22: 278 conversations were stalled with no outcome, and every
          -- one of them was excluded here. 237 had opened with a click-to-WhatsApp
          -- prefill ("Hi! Need more details about republic of nature.") and 253 had
          -- answered nothing at all. A tap on an ad had bought them one reply and
          -- then permanent silence. Owner's call: treat them as a lead who has not
          -- spoken, because they have not.
          --
          -- THREE GUARDS, all required, so this can never talk over a live person:
          --   * every inbound they have sent matches a prefill pattern in full
          --   * they have answered nothing on the checklist
          --   * they have been quiet for QUIET_DAYS -- someone who tapped an hour
          --     ago may be mid-conversation, and their anchor makes t1 due at once
          AND (l.last_inbound_at IS NULL OR (
                   l.last_inbound_at < now() - (%s || ' days')::interval
               AND c.checklist = '{}'::jsonb
               AND NOT EXISTS (
                       SELECT 1 FROM message_log im
                        WHERE im.lead_id = l.id
                          AND im.direction = 'in' AND im.msg_type = 'inbound'
                          -- Engaged if they typed something that is not a prefill,
                          -- OR pressed one of our own template buttons. WhatsApp
                          -- returns a button label as an ordinary inbound, so
                          -- without the second clause a person tapping "Need More
                          -- Details" on our nurture template reads as silence and
                          -- keeps getting knocked after raising their hand.
                          AND (NOT (COALESCE(im.body, '') ~* ANY(%s))
                               OR lower(trim(COALESCE(im.body, ''))) = ANY(%s)))
          ))
          AND c.outcome IS NULL
          AND COALESCE(ks.sent, 0) < %s
          -- Clock 1: time since they signed up. Array is 1-based, hence sent+1;
          -- a subscript past the end yields NULL, which fails the comparison and
          -- drops the row -- the same answer the loop gives for a finished journey.
          AND now() >= COALESCE(l.selldo_response_at, l.created_at)
                     + ((%s::int[])[COALESCE(ks.sent, 0) + 1] || ' days')::interval
          -- Clock 2: time since we last knocked. A backlog lead must not receive
          -- the whole sequence in four days.
          AND (ks.last_at IS NULL
               OR now() >= ks.last_at
                         + ((%s::int[])[COALESCE(ks.sent, 0) + 1] || ' days')::interval)
"""


def _due_params():
    """The four bind values _DUE_FROM_WHERE expects, or None if nothing is live."""
    campaigns = [c.lower() for c in config.SELLDO
                 .get(config.DIRECT_INBOUND_PROJECT, {}).get("campaigns") or []]
    if not campaigns:
        return None
    return (campaigns,
            QUIET_DAYS,
            config.CTWA_PREFILL_PATTERNS,
            config.TEMPLATE_BUTTON_LABELS,
            min(len(KNOCK_SCHEDULE), config.KNOCK_MAX_PER_JOURNEY),
            [step[0] for step in KNOCK_SCHEDULE],
            [_min_gap_days(i) for i in range(len(KNOCK_SCHEDULE))])


def due_count():
    """How many lead rows are due for a knock RIGHT NOW, ignoring batch size.

    The watchdog's contradiction test: leads are due and nothing is going out.
    That pair cannot both be true in a healthy system, which is what makes it
    safe to alert on -- it has no quiet-day false positive.
    """
    params = _due_params()
    if not params:
        return 0
    r = db.q(_DUE_CTE + " SELECT count(*) AS n " + _DUE_FROM_WHERE, params, one=True)
    return (r or {}).get("n") or 0


def due(limit=None):
    """Leads whose next knock is due right now. Oldest signup first."""
    limit = limit or config.SEND_BATCH_PER_TICK
    campaigns = [c.lower() for c in config.SELLDO
                 .get(config.DIRECT_INBOUND_PROJECT, {}).get("campaigns") or []]
    if not campaigns:
        return []

    # THE FETCH WINDOW MUST BE FILTERED BEFORE IT IS LIMITED.
    #
    # This query used to select the oldest eligible leads and test due-ness in the
    # Python loop below. That deadlocks, and did: on 2026-08-22 all 125 rows in the
    # window were leads who had already had knocks 1-3 and were waiting out the
    # 15-day gap before knock 4. They are the OLDEST by definition, so they sat at
    # the front of the queue permanently and every tick returned an empty batch --
    # while 293 rows (172 people) further back were due and unreachable. Knocks per
    # day went 63 -> 35 -> 16 -> 1 with nothing erroring and nothing in the log.
    #
    # So the due-ness clocks are applied HERE, in SQL, and LIMIT now bounds the
    # leads we could actually send to. Oldest-first is kept, because that is the
    # right fairness rule AMONG due leads -- it was only ever wrong as a prefilter.
    #
    # The Python loop below still re-checks everything and remains the authority;
    # this is a candidate filter, not a replacement for it. The retry bookkeeping in
    # particular is not modelled here, which is why LIMIT still takes headroom.
    rows = db.q(_DUE_CTE + _DUE_SELECT + _DUE_FROM_WHERE + """
        ORDER BY COALESCE(l.selldo_response_at, l.created_at) ASC
        LIMIT %s""",
                _due_params() + (limit * 5,)) or []

    now = datetime.now(timezone.utc)
    out = []
    # Phone-keyed within the batch too. knock_state reads what has already been
    # SENT, so two rows for one person both read zero and both qualify -- which is
    # exactly how lavanya was messaged twice. The database check stops it across
    # runs; this stops it inside one.
    claimed = set()
    for lead in rows:
        if lead["phone"] in claimed:
            continue
        sent, last_at = knock_state(lead["phone"])
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

        # RETRY BOOKKEEPING. Only reached when the step is otherwise due.
        #
        # ENTIRELY INSIDE THE SWITCH, on purpose. A knock blocked by the fatigue cap
        # already leaves an ok=FALSE row and is correctly retried once the window
        # clears; guarding on attempt count regardless of the switch would kill that
        # -- the lead would lose the knock permanently. So with the switch off this
        # block does nothing at all and the engine behaves exactly as before.
        #
        # With it on, both guards are load-bearing: without the ceiling a
        # permanently-refused number is retried forever, and without the gap the
        # refused attempt is invisible to the clock above (knock_state cannot see it)
        # so the same knock fires on every tick of the day.
        if config.KNOCK_RETRY_ENABLED:
            attempts, last_try = attempt_state(lead["phone"], step_key)
            if attempts >= config.KNOCK_RETRY_MAX:
                _give_up(lead, step_key, attempts)
                continue
            if attempts and last_try is not None:
                if last_try.tzinfo is None:
                    last_try = last_try.replace(tzinfo=timezone.utc)
                if now < last_try + timedelta(hours=config.KNOCK_RETRY_GAP_HOURS):
                    continue

        out.append((lead, sent, step_key))
        claimed.add(lead["phone"])
        if len(out) >= limit:
            break
    return out


def send_knock(lead, step_index, step_key):
    """Send one knock. Returns True only if it actually went out."""
    # Which wording. On a first attempt this is variant 0, i.e. exactly what the
    # step always sent; on a retry it is the next one in the rotation.
    attempts, _last_try = attempt_state(lead["phone"], step_key)
    template = variant_for(step_key, attempts)
    if not template:
        log.warning("no template configured for %s", step_key)
        return False
    if attempts:
        log.info("knock %s retry %d/%d for %s using variant %s",
                 step_key, attempts + 1, config.KNOCK_RETRY_MAX,
                 lead["phone"], template)

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
    sent, _last = knock_state(lead["phone"])
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
