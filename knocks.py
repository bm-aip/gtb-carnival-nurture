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
import failures
import fatigue
import picker
import sequencer
import wati

log = logging.getLogger("knocks")

# How long an ad-tapper must have been silent before the ladder may treat them as
# somebody who never spoke. Short enough that the lead is still warm, long enough
# that nobody mid-conversation is interrupted -- their anchor is the tap itself, so
# without this t1 would be due the moment they went quiet.
QUIET_DAYS = int(os.environ.get("KNOCK_REVIVE_QUIET_DAYS", "3"))

# How far down the candidate queue due() will walk looking for a sendable lead,
# and in what size steps. Bounded so a tick cannot turn into a table scan, but far
# larger than any plausible backlog of rejects sitting at the front of the queue.
# THE WALK ITSELF NOW LIVES IN picker.py, shared with the re-opener. Three
# lane-local fixes to one bug was three too many, and the fourth lane inherited
# none of them. Re-exported under the old names because this is where every one
# of those incidents happened and this is where a reader will look for them.
SCAN_PAGE = picker.SCAN_PAGE
SCAN_MAX = picker.SCAN_MAX

# Fatigue's reason codes in the words the watchdog puts in front of a person.
# _verdict()'s reasons are not debug strings: they are grouped and printed as
# "Others waiting: 3 waiting on the weekly cap" in the NOBODY IS BEING CONTACTED
# alert, and that line is the difference between a stuck engine and a lane
# correctly cooling off.
_FATIGUE_REASON = {
    fatigue.CAP_WINDOW: "waiting on the weekly cap",
    fatigue.CAP_JOURNEY: "journey ceiling reached",
}

# Plain-English for the retry ceilings. These strings are what the watchdog prints
# in its "Others waiting:" line, so they are written for the owner, not for us.
_CEILING_REASON = {
    failures.CEILING_BURST: "this send keeps being refused",
    failures.CEILING_RECIPIENT: "number cannot receive WhatsApp",
    failures.CEILING_TRANSIENT: "too many transient failures",
}

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

# STEPS HELD BACK. Comma-separated KNOCK_SCHEDULE keys, e.g. "t1_lifestyle".
#
# WHY A PER-STEP HOLD AND NOT A PAUSE. Owner, 2026-08-25, after the first honest
# measurement of the sequence: t1 woke 5.5% of 309 people, t2 2.4% of 83, t3 5.0%
# of 80, and t6_visit had never been sent at all -- it sat on day 25 of a cycle
# almost nobody survives. Moving the cycle to 15 days makes 72 people due for that
# visit invitation at once.
#
# Sending those 72 asks alongside 127 more t1s would bury the only untested
# template in the one we have just measured as weak, and the dashboard could not
# tell the two apart afterwards. So t1 is held while the visit ask goes out alone.
#
# This is a SEQUENCING tool, not a kill switch: a held step blocks nobody's
# journey permanently, it just stops NEW sends of that one step until the hold is
# lifted. sendgate's pause remains the way to stop everything.
KNOCK_STEPS_PAUSED = tuple(
    x.strip() for x in os.environ.get("KNOCK_STEPS_PAUSED", "").split(",")
    if x.strip())


def _paused_step_positions():
    """1-based positions of held steps, matching the SQL's `sent + 1` subscript.

    Returned as a list so the same fact reaches the SQL candidate filter AND the
    Python loop. Both must agree: due_count() is SQL-only and the watchdog alerts
    when leads are due and nothing goes out, so a hold the count cannot see would
    look exactly like a starving engine.
    """
    return [i + 1 for i, (_, key) in enumerate(KNOCK_SCHEDULE)
            if key in KNOCK_STEPS_PAUSED]


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
          -- Held steps. Same 1-based subscript as the two clocks above, so the
          -- count the watchdog reads and the batch the engine sends agree.
          AND NOT (COALESCE(ks.sent, 0) + 1 = ANY (%s::int[]))
"""


def _due_params():
    """The bind values _DUE_FROM_WHERE expects, or None if nothing is live."""
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
            [_min_gap_days(i) for i in range(len(KNOCK_SCHEDULE))],
            _paused_step_positions())


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
    # PAGE, DO NOT TAKE ONE FIXED WINDOW.
    #
    # 2026-08-26: 129 people were owed a t2 and the engine sent nothing for eleven
    # hours. The query took the oldest `limit * 5` = 125 candidates; the Python loop
    # then rejected 220 of them on things the SQL does not model -- 133 waiting out
    # the 24h retry gap after Meta refused a day of sends with code 131049, and 87
    # duplicate phone rows. Those rejects are the OLDEST rows by definition, so they
    # filled the whole window and the sendable leads behind them were never seen.
    #
    # The old comment called `limit * 5` "headroom". Headroom is a guess, and a
    # refusal storm falsifies it: any fixed multiple starves once the reject backlog
    # exceeds it. So we walk pages until the batch is full or SCAN_MAX rows have
    # been examined -- bounded work, and no amount of stale rejects can hide a
    # sendable lead behind them.
    #
    # Same failure as the fortnight-long knock deadlock: FILTER BEFORE YOU LIMIT.
    now = datetime.now(timezone.utc)

    def fetch(page, offset):
        return db.q(_DUE_CTE + _DUE_SELECT + _DUE_FROM_WHERE + """
            ORDER BY COALESCE(l.selldo_response_at, l.created_at) ASC
            LIMIT %s OFFSET %s""",
                    _due_params() + (page, offset))

    # Phone-keyed within the batch too. knock_state reads what has already been
    # SENT, so two rows for one person both read zero and both qualify -- which is
    # exactly how lavanya was messaged twice. The database check stops it across
    # runs; this stops it inside one.
    claimed = set()

    def select(lead):
        step_index, step_key, reason = _verdict(lead, now, claimed)
        if reason == "ceiling":
            # The ONE side effect in this pass, and it belongs to the engine, not
            # to anything merely counting: ten attempts refused means stop forever.
            _give_up(lead, step_key, config.KNOCK_RETRY_MAX)
            return None
        if reason:
            return None
        claimed.add(lead["phone"])
        return (lead, step_index, step_key)

    # THE PROBE IS GONE, and losing it is a simplification rather than a loss. It
    # fetched pages, then guessed whether it held enough sendable leads by running
    # _verdict over them a second time and throwing every answer away. picker.scan
    # decides each row once, as it walks, and stops the instant the batch is full:
    # it can never fetch more than it needs, and never pays for a verdict twice.
    # Same bound, same oldest-first order, one pass.
    return picker.scan(fetch, select, limit)


def _verdict(lead, now, claimed):
    """(step_index, step_key, reason) for ONE lead. reason is None if sendable.

    THE ENGINE'S DECISION, IN ONE PLACE, BECAUSE TWO COPIES DRIFTED.

    due_count() counts what the SQL accepts; due() sends what this loop accepts.
    They are legitimately different numbers -- duplicate phone rows, both clocks,
    a held step, the retry gap -- and on 2026-08-26 that difference was 349 against
    129. The watchdog was reading the first and alerting "NOBODY IS BEING CONTACTED"
    while the engine was correctly waiting out a 24h retry gap after Meta refused
    1,688 marketing messages with code 131049.

    The comment above the shared SQL already warned about this exact failure in the
    other direction -- "a monitor that measures something subtly different from the
    engine it watches is worse than no monitor". So the monitor now calls THIS.

    Deliberately free of side effects. `ceiling` is REPORTED rather than acted on,
    and only due() turns it into _give_up(); a counter must never mark a lead lost.
    """
    if lead["phone"] in claimed:
        return None, None, "duplicate phone in batch"
    sent, last_at = knock_state(lead["phone"])
    if sent >= len(KNOCK_SCHEDULE) or sent >= config.KNOCK_MAX_PER_JOURNEY:
        return None, None, "journey complete"
    days_after, step_key = KNOCK_SCHEDULE[sent]
    # The loop is the authority; the SQL is a candidate filter. A held step is
    # re-checked here so a stale query string can never send one.
    if step_key in KNOCK_STEPS_PAUSED:
        return sent, step_key, f"held:{step_key}"
    anchor = lead["anchor"]
    if anchor is None:
        return sent, step_key, "no anchor"
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    # Due against BOTH clocks: time since they signed up, and time since we last
    # knocked. A backlog lead must not receive the whole sequence at once.
    if now < anchor + timedelta(days=days_after):
        return sent, step_key, "waiting on the signup clock"
    if last_at is not None:
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        if now < last_at + timedelta(days=_min_gap_days(sent)):
            return sent, step_key, "waiting on the spacing gap"

    # RETRY BOOKKEEPING. Only reached when the step is otherwise due.
    #
    # ENTIRELY INSIDE THE SWITCH, on purpose. A knock blocked by the fatigue cap
    # already leaves an ok=FALSE row and is correctly retried once the window
    # clears; guarding on attempt count regardless of the switch would kill that --
    # the lead would lose the knock permanently. So with the switch off this block
    # does nothing at all and the engine behaves exactly as before.
    #
    # With it on, both guards are load-bearing: without the ceiling a
    # permanently-refused number is retried forever, and without the gap the refused
    # attempt is invisible to the clock above (knock_state cannot see it) so the same
    # knock fires on every tick of the day.
    if config.KNOCK_RETRY_ENABLED:
        attempts, last_try = attempt_state(lead["phone"], step_key)
        if attempts >= config.KNOCK_RETRY_MAX:
            return sent, step_key, "ceiling"
        if attempts and last_try is not None:
            if last_try.tzinfo is None:
                last_try = last_try.replace(tzinfo=timezone.utc)
            if now < last_try + timedelta(hours=config.KNOCK_RETRY_GAP_HOURS):
                return sent, step_key, "waiting out the retry gap"

    # THE FATIGUE CAP IS A SELECTION RULE, NOT A LAST-MOMENT ONE.
    #
    # send_knock() has always called fatigue.check() immediately before the wire,
    # and that door stays -- knock_now() reaches it without passing through here.
    # But the picker did not model it, so a lead at the weekly ceiling stayed
    # sendable in this function's eyes, was chosen on every tick, refused at the
    # door, and left a `blocked:fatigue:` row behind. Measured over the seven days
    # to 2026-08-31: 29,865 such rows for t6_visit against 43 real sends, and
    # 35,156 across all steps against 382. At SEQUENCER_TICK_MIN=1 that is about
    # three people being re-picked and re-refused every sixty seconds, forever.
    #
    # THE RETRY GAP ABOVE CANNOT COVER THIS, and it is worth saying why, because
    # switching KNOCK_RETRY_ENABLED on looks like the fix and is not:
    # attempt_state() excludes `blocked:` rows on purpose (our own gates must not
    # consume one of the ten attempts at reaching a person), so it reports zero
    # attempts and a null clock no matter how many times fatigue has refused. The
    # gap never engages. Only a check at selection time ends the loop.
    #
    # LAST, DELIBERATELY. Every cheaper reason has already returned by this point,
    # so the two counting queries inside fatigue.check() run for leads that are
    # otherwise ready to send -- a handful per tick, not the scan window.
    #
    # SIDE-EFFECT FREE, like the rest of this function: check() only counts
    # message_log; start_journey() is a separate call made by send_knock().
    allowed, cap = fatigue.check(lead["phone"], msg_type_for(step_key),
                                 project=lead.get("project"))
    if not allowed:
        return sent, step_key, _FATIGUE_REASON.get(cap, f"waiting on {cap}")

    # THE RETRY CEILING IS A SELECTION RULE TOO. Same lesson as the block above,
    # in the same function, one week later -- and it cost 135,496 rows.
    #
    # RETRY_MAX_BURST was added 2026-08-25 as sendgate's last and widest guard:
    # five refusals of one (phone, msg_type) and that particular send stops, while
    # the person stays reachable by every other lane. It works perfectly on the
    # wire -- measured 2026-09-03, every affected lead had exactly 4 delivered and
    # 5 refused, so nobody was spammed.
    #
    # But the picker did not model it. 23 leads whose t6/t2 Meta had stopped
    # delivering stayed sendable in this function's eyes, were chosen on every
    # tick, refused at the door, and left a `blocked:retry_ceiling_burst` row
    # behind each time: 1,380 rows an hour, flat, for nine days. 135,496 rows,
    # 84% of everything in message_log. Real knocks fell from 162 a day to one,
    # because those 23 are the OLDEST due rows and so filled every batch of ten
    # -- 309 sendable buyers sat behind them, unreachable.
    #
    # attempt_state() cannot close this, for precisely the reason spelled out
    # above: it excludes `blocked:` rows on purpose, so the ceiling reads zero
    # attempts and a null clock however many times the burst cap refused, and
    # _give_up() counts those same attempts and therefore never fires. Only a
    # check at selection time ends the loop. Third time that has been true here.
    #
    # AFTER FATIGUE, MIRRORING sendgate's order, and last because it is the
    # widest: the counting queries inside failures.check() run only for leads
    # already otherwise ready to send. Side-effect free, like the rest of this
    # function -- check() only reads message_log.
    #
    # sendgate's own call STAYS. knock_now() reaches the wire from the leadgen
    # webhook without passing through the picker, so two doors is correct.
    allowed, cap = failures.check(lead["phone"], msg_type_for(step_key),
                                  project=lead.get("project"))
    if not allowed:
        return sent, step_key, _CEILING_REASON.get(cap, f"waiting on {cap}")
    return sent, step_key, None


def sendable_count(limit=2000):
    """(how many the engine WOULD send right now, why the rest are waiting).

    What the watchdog must alert on. due_count() answers a different question --
    how many rows the SQL accepts -- and alerting on it cried wolf the morning
    after Meta refused a day's worth of marketing messages.

    Writes nothing. `ceiling` leads are counted as waiting rather than given up:
    only the engine may end somebody's journey.
    """
    params = _due_params()
    if params is None:
        return 0, {}
    rows = db.q(_DUE_CTE + _DUE_SELECT + _DUE_FROM_WHERE + """
        ORDER BY COALESCE(l.selldo_response_at, l.created_at) ASC
        LIMIT %s""", params + (limit,)) or []
    now = datetime.now(timezone.utc)
    claimed, n, why = set(), 0, {}
    for lead in rows:
        _idx, _key, reason = _verdict(lead, now, claimed)
        if reason:
            why[reason] = why.get(reason, 0) + 1
            continue
        n += 1
        claimed.add(lead["phone"])
    return n, why


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
    # Quiet hours are a property of the CLOCK, not of any one person, so the
    # answer is the same for every name in the batch. Checking here rather than
    # per-message saves walking a page of candidates to reject all of them --
    # the same reasoning as the hourly brake below. sequencer._send() enforces it
    # again at the door, which is what makes it true for lanes that forget.
    if sequencer.quiet_now():
        log.info("knock pass skipped: quiet hours until %02d:%02d IST",
                 *sequencer.QUIET_END)
        return 0

    # The daily tier cap, checked once for the same reason as the clock above: the
    # answer is identical for every name in the batch, so walking a page of them to
    # reject each one is wasted work. sequencer._send() enforces it again at the
    # door, which is what makes it true rather than merely usual.
    if sequencer.daily_budget() <= 0:
        log.info("knock pass skipped: daily cap of %d reached",
                 config.DAILY_SEND_CAP)
        return 0

    batch = due()
    if not batch:
        return 0
    sent = 0
    for lead, step_index, step_key in batch:
        # A FULL HOUR ENDS THE PASS -- see the same guard in reopener.run(). The
        # hourly cap belongs to the clock, not to this lead, so once it is closed
        # every remaining name in the batch would only earn a `blocked:rate_capped`
        # row. Here those rows are counted by the retry bookkeeping instead of the
        # spacing clock, but the principle is the one this project keeps relearning:
        # a message that never reached WhatsApp must not cost the person anything.
        if not wati.rate_ok(msg_type_for(step_key)):
            log.info("knock pass stopped: no hourly headroom, %d left for the "
                     "next tick", len(batch) - sent)
            break
        try:
            if send_knock(lead, step_index, step_key):
                sent += 1
        except Exception as e:                      # one bad lead must not stop the pass
            log.exception("knock failed for lead %s: %s", lead.get("id"), e)
    if sent:
        db.set_setting("last_knock_run", f"{sent} sent")
    return sent
