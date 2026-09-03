"""Nothing was watching this system. This watches it.

2026-08-05: the Anthropic account ran out of credit overnight. FIVE REAL BUYERS
messaged between 02:02 and 08:18 and got complete silence for up to eight hours.
Every failure was recorded correctly -- the jobs sat in `job_queue` marked
`failed`, exactly as designed. Nobody looked. It surfaced only because a test run
happened to hit the same error hours later.

A 400 `credit balance is too low` is not transient, so `jobs.py` burns all five
attempts and stops forever. That is correct behaviour for a permanent error and
useless without someone to tell.

Every piece needed already existed: failed jobs are kept on purpose, `jobs.stats()`
already reports them, `/api/queue` already serves them, `handoff._notify` became a
reliable sender in PR #32, and the scheduler already runs four jobs. Somebody wrote
the smoke detector, wired the siren, and never joined the two wires. This is the
wire.

THREE SIGNALS, because each is a different way a buyer ends up ignored:

  1. Jobs that gave up      -- a buyer wrote and will never get a reply.
  2. Staff cards that failed -- a human was told about a buyer and never heard.
  3. The queue backing up    -- what a dead worker looks like from outside.

AND ONE HEARTBEAT. A watchdog that has quietly died looks exactly like a system
with no problems: both are silence. The daily line is the only evidence that the
watching is still happening, so it is not decoration -- it is the thing that makes
the silence trustworthy.
"""
import logging
import os
from datetime import datetime, timedelta

import config
import db
import handoff

log = logging.getLogger("watchdog")

# How stale the front of the queue may get before it means the worker is dead.
# The sequencer ticks every few minutes and the worker drains continuously, so
# anything older than this is not backlog, it is a stopped process.
QUEUE_STALE_MIN = 15

# ---------------------------------------------------------------------------
# THE THREE SIGNALS ABOVE WATCH PLUMBING. THESE THREE WATCH OUTCOMES.
#
# 2026-08-22. The knock engine sent one message in fourteen days while 293 people
# waited, and the owner received fourteen consecutive daily reports saying
# "nothing failed". Every word of that was true. Failed jobs were zero, the queue
# was draining, staff cards were landing -- the three things being watched were
# genuinely healthy. Nothing was watching whether anyone was being CONTACTED.
#
# The same blind spot hid two more faults: 45% of all sends bouncing off Meta's
# quality restrictions, and poll_meta_leads wedged for 24 hours so new form
# submissions stopped arriving.
#
# Plumbing checks pass in exactly the situation you most need warning about. So
# each signal below is a CONTRADICTION -- a pair of facts that cannot both be
# true in a working system. That is what makes them safe to alert on: there is
# no quiet-day false positive to train anyone into ignoring them.
# ---------------------------------------------------------------------------

# Leads are due for a knock, and this many hours have passed with none sent.
# Longer than one tick so a slow batch is not an alarm; far shorter than the
# fortnight it actually took someone to notice.
KNOCK_SILENT_HOURS = int(os.environ.get("WATCHDOG_KNOCK_SILENT_HOURS", "6"))

# Delivery failure rate over 24h that means Meta is refusing us, not that one
# number is bad. Measured at 45% on 2026-08-22, so the threshold is set where a
# real deterioration trips it and ordinary noise does not.
DELIVERY_FAIL_PCT = int(os.environ.get("WATCHDOG_DELIVERY_FAIL_PCT", "35"))
DELIVERY_MIN_SENDS = int(os.environ.get("WATCHDOG_DELIVERY_MIN_SENDS", "20"))

# A TRICKLE MUST NOT COUNT AS A PULSE. Both knock guards used to ask "was the count
# exactly zero", and on 2026-09-03 that let a 99% stall run for nine days: 309 leads
# were sendable, one or two knocks dribbled out each day, and the answer to "is it
# zero" was legitimately no. So the question is now "did we send a plausible SHARE
# of what was owed", and this is that share, as a percentage.
#
# 10% is deliberately forgiving. The engine cannot reach everyone due -- the weekly
# fatigue cap alone forbids it -- so anything near 100% would cry wolf every day.
# 2 sends against 309 owed is 0.6% and trips it; 40 against 309 is 13% and does not.
KNOCK_STALL_PCT = int(os.environ.get("WATCHDOG_KNOCK_STALL_PCT", "10"))


def _knocks_attempted(hours):
    """(how many knocks reached the wire in the window, when the last one did).

    THE PREDICATE, IN ONE PLACE, BECAUSE TWO COPIES DISAGREED. The daily report
    already excluded `blocked:` rows; this alert's own count did not, so on
    2026-09-03 the 1,380 blocked rows an hour that WERE the bug read to it as 1,380
    healthy knocks and silenced it. One function now answers for both.

    `ok` IS DELIBERATELY NOT THE TEST, and the distinction matters:

        delivered   ok = TRUE                              engine works
        refused     we sent it, the provider said no       engine works
        blocked     our own gate stopped it                engine never tried

    This guard asks "is the engine sending", so a refusal is evidence FOR it. Only
    the third state is silence. Filtering on `ok` would raise an alarm every time
    Meta had a bad night, which is a different alert that already exists.
    """
    row = db.q("""SELECT count(*) AS n, max(ts) AS last_at FROM message_log
                  WHERE direction='out' AND msg_type LIKE 'knock%%'
                    AND (detail IS NULL OR detail NOT LIKE 'blocked:%%')
                    AND ts > now() - (%s || ' hours')::interval""",
               (int(hours),), one=True) or {}
    return (row.get("n") or 0), row.get("last_at")


def _knocks_expected(due, hours):
    """The fewest knocks a working engine would have sent, given `due` were owed.

    Zero due means zero expected, so a quiet day stays quiet. Otherwise it is a
    share of what was owed -- but CLAMPED to what the sender is actually permitted
    to push in the window, or the guard would scream at its own rate limit the
    moment the backlog grew past it.

    The ceiling reuses wati.rate_ok()'s own arithmetic rather than restating it:
    a proactive send may only use the hour's allowance MINUS the reserve held back
    for people who are actually talking to us. If those two ever disagreed, the
    watchdog would be alarming about a limit the sender does not have.
    """
    if due <= 0:
        return 0
    share = -(-due * KNOCK_STALL_PCT // 100)          # ceil, no float
    headroom = max(1, config.MAX_SENDS_PER_HOUR - config.REPLY_RESERVE_PER_HOUR)
    return max(1, min(share, headroom * int(hours)))

# How long poll_meta_leads may go without completing before new leads are
# presumed to have stopped arriving. It runs every minute; a full sweep can
# legitimately take several, so this is generous and still catches a 24h wedge.
POLLER_STALE_MIN = int(os.environ.get("WATCHDOG_POLLER_STALE_MIN", "90"))
_HB_META_LEADS = "meta_leads_last_ok"

# A send lane has been throwing for this long. The tick runs every minute, so an
# hour is roughly sixty consecutive failures -- comfortably past a transient
# database blip or one bad row, and far short of the fortnight a silent lane
# managed to hide for. Owner's call, 2026-08-31: alert rather than wait for the
# morning report, but not on a single blip.
LANE_BROKEN_MIN = int(os.environ.get("WATCHDOG_LANE_BROKEN_MIN", "60"))

# The lanes inside sequencer.tick(), and the plain-English name for each. Adding a
# third lane means adding it HERE -- a lane absent from this list is unwatched, and
# unwatched is precisely how the ghost lane came to have no monitoring at all.
LANES = (("knock", "the follow-up engine"),
         ("reopener", "the re-opener (dead conversations)"))

# One message per problem per hour. An alert that repeats every 15 minutes is an
# alert people mute, and a muted alert is worse than none because it still looks
# like coverage. Same reasoning as the send-once guard on lead cards.
MUTE_HOURS = 1

# Watermarks live in `settings` so a redeploy does not re-report old failures.
_WM_JOBS = "watchdog_jobs_watermark"
_WM_CARDS = "watchdog_cards_watermark"
_LAST_ALERT = "watchdog_last_alert_"     # + kind
_LAST_DAILY = "watchdog_last_daily"


def _int_setting(key, default=0):
    try:
        return int(db.get_setting(key, default) or default)
    except (TypeError, ValueError):
        return default


def _muted(kind):
    """True if this kind of problem was already reported inside MUTE_HOURS."""
    raw = db.get_setting(_LAST_ALERT + kind)
    if not raw:
        return False
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return False
    return datetime.now(last.tzinfo) - last < timedelta(hours=MUTE_HOURS)


def _mark_alerted(kind):
    db.set_setting(_LAST_ALERT + kind, datetime.now().astimezone().isoformat())


# WhatsApp template parameters may not contain newlines, and this project has
# already been bitten once by a non-ASCII character surviving into a URL query
# param (the rupee sign). Alerts are ASCII and single-line by construction.
def _clean(s):
    out = " ".join(str(s or "").split())
    return out.encode("ascii", "ignore").decode("ascii")[:900] or "-"


def _alert(kind, headline, detail, action):
    """Send one alert card. Returns True if it actually went out.

    Reuses `ron_staff_card_01`, the approved staff template, with the headline
    slot carrying the problem instead of a buyer's name. No second Meta approval
    to wait for, and it travels the same one door as every other outbound message
    -- so the master switch still governs it. An alerting system that bypassed the
    send gate would be a second door in a system whose whole design is one door.
    """
    slots = [_clean(headline), _clean("RON bot watchdog"), _clean("system"),
             _clean(detail), _clean(action)]
    return handoff._notify(config.ALERT_PHONES, slots, "alert")


def _check_failed_jobs():
    """Jobs that exhausted their retries. Each one is a buyer owed a reply."""
    wm = _int_setting(_WM_JOBS)
    rows = db.q("""SELECT id, kind, phone, attempts, last_error
                   FROM job_queue
                   WHERE status='failed' AND id > %s
                   ORDER BY id""", (wm,)) or []
    if not rows:
        return None
    if _muted("jobs"):
        # Deliberately does NOT advance the watermark: the next alert reports the
        # whole backlog rather than losing what happened while muted.
        return f"{len(rows)} failed jobs (muted)"

    err = _clean(rows[-1].get("last_error"))[:120]
    # NAME THEM (2026-08-19). The card used to carry only a count, so the first
    # question it raised -- WHICH buyer is sitting there unanswered -- could only be
    # answered by logging into the dashboard. On 2026-08-18 two people went
    # unanswered overnight and nobody could tell who they were from the alert.
    who = ", ".join(str(r.get("phone") or "?") for r in rows[:5])
    if len(rows) > 5:
        who += f" +{len(rows) - 5} more"

    ok = _alert("jobs",
                f"BOT PROBLEM - {len(rows)} buyer(s) got no reply",
                f"{who}. Gave up after retrying. Latest error: {err}",
                "POST /api/queue/replay to have the bot answer them now. It only "
                "works inside 24h of their message -- after that, reply by hand in "
                "the Wati inbox.")
    if ok:
        db.set_setting(_WM_JOBS, rows[-1]["id"])
        _mark_alerted("jobs")
    return f"{len(rows)} failed jobs"


def _check_undelivered_cards():
    """Staff cards WhatsApp refused. Watch the fix rather than trust it.

    PR #32 moved cards onto an approved template so the 24-hour window stops
    eating them. This is the check that tells us if that stopped being true.
    """
    wm = _int_setting(_WM_CARDS)
    rows = db.q("""SELECT id, msg_type, lead_id, detail
                   FROM message_log
                   WHERE direction='out' AND msg_type LIKE 'handoff%%'
                     AND ok IS NOT TRUE AND id > %s
                   ORDER BY id""", (wm,)) or []
    if not rows:
        return None
    if _muted("cards"):
        return f"{len(rows)} undelivered staff cards (muted)"

    # COUNT CARDS, NOT LOG ROWS (2026-08-19).
    #
    # One card writes two rows when it retries -- the template attempt and the
    # free-text fallback, the second with a `_text` suffix. On 2026-08-19 that
    # reported "2 card(s) did not reach anyone" for ONE card to one recipient, while
    # the other recipient had received it. An alert that doubles the damage is read
    # at the exact moment someone is deciding how alarmed to be.
    # `lead_id` here is WHO THE CARD IS ABOUT -- sequencer stores `log_lead_id`
    # in that column, because the recipient is a salesperson with no lead row.
    cards = {(r.get("lead_id"), (r.get("msg_type") or "").replace("_text", ""))
             for r in rows}
    n = len(cards)

    ok = _alert("cards",
                f"HANDOFF FAILED - {n} card(s) did not reach anyone",
                f"{n} staff card(s) were rejected after retrying. Latest: "
                f"{_clean(rows[-1].get('detail'))[:150]}",
                "A buyer asked for a human and nobody was told. Check the Wati "
                "Team Inbox for these leads.")
    if ok:
        db.set_setting(_WM_CARDS, rows[-1]["id"])
        _mark_alerted("cards")
    return f"{len(rows)} undelivered cards"


def _check_queue_stalled():
    """The oldest queued job. Old means the worker is not running."""
    row = db.q("""SELECT min(created_at) AS t, count(*) AS n
                  FROM job_queue WHERE status='queued'""", one=True)
    if not row or not row.get("t"):
        return None
    age_min = (datetime.now(row["t"].tzinfo) - row["t"]).total_seconds() / 60
    if age_min < QUEUE_STALE_MIN:
        return None
    if _muted("queue"):
        return f"queue stalled {int(age_min)}m (muted)"

    ok = _alert("queue",
                "BOT STALLED - messages are queued and not being sent",
                f"{row['n']} job(s) waiting, oldest {int(age_min)} minutes old.",
                "The worker is probably not running. Check WORKER_IN_PROCESS is "
                "true on the service, then redeploy.")
    if ok:
        _mark_alerted("queue")
    return f"queue stalled {int(age_min)}m"


def _check_nobody_contacted():
    """Leads are due a knock and none has gone out. THE FORTNIGHT BUG.

    Imported inside the function: knocks imports sequencer, and a module-level
    import here would close a circle.

    The contradiction is the whole point. "No knocks sent" alone is a normal
    quiet day -- the ladder has 3, 7 and 15-day gaps built into it. "Leads are
    due" alone is normal too. Together they are impossible in a working engine,
    which is why this can shout without ever crying wolf.
    """
    import knocks

    # WHAT THE ENGINE WOULD ACTUALLY SEND, not what the SQL accepts. due_count()
    # counts candidates; the engine then applies both clocks, the held-step check
    # and the retry gap. On 2026-08-26 that was 349 against 129, and this alert
    # fired "NOBODY IS BEING CONTACTED" while the engine was correctly waiting out
    # a 24h retry gap after Meta refused 1,688 messages with code 131049.
    due, waiting = knocks.sendable_count()
    if not due:
        return None

    # A SHARE, NOT A ZERO. `if sent: return None` lived here, and two knocks a day
    # against 309 owed satisfied it for nine days. The count and the threshold both
    # come from shared helpers so this test and the daily report cannot drift.
    sent, _last_in_window = _knocks_attempted(KNOCK_SILENT_HOURS)
    expected = _knocks_expected(due, KNOCK_SILENT_HOURS)
    if sent >= expected:
        return None

    # Strictly wider than the old test: with anything due, `expected` is at least 1,
    # so total silence still trips exactly as before.

    if _muted("knocks_silent"):
        return f"{due} due, {sent} sent (muted)"

    _all_time, when = _knocks_attempted(24 * 365 * 10)
    ago = "never" if not when else f"{int((datetime.now(when.tzinfo) - when).total_seconds() / 3600)}h ago"

    # Why everyone ELSE is waiting, biggest reason first. Without it the reader
    # cannot tell a stuck engine from a lane that is correctly cooling off, which
    # is the difference between an emergency and a normal morning.
    held = ", ".join(f"{n} {r}" for r, n in
                     sorted(waiting.items(), key=lambda kv: -kv[1])[:3])
    # THE HEADLINE MUST NOT OVERSTATE. "NOBODY IS BEING CONTACTED" was written for
    # a count of exactly zero; saying it while two messages went out is the kind of
    # inaccuracy that teaches a reader to discount the next alert. So the number is
    # in the headline and the word "nobody" is gone.
    headline = ("NOBODY IS BEING CONTACTED" if not sent
                else "KNOCKS HAVE ALL BUT STOPPED")
    ok = _alert("knocks_silent",
                f"{headline} - {due} lead(s) waiting",
                f"{due} lead(s) could be knocked right now and only {sent} went out "
                f"in {KNOCK_SILENT_HOURS}h -- a working engine would have sent at "
                f"least {expected}. Last knock: {ago}."
                + (f" Others waiting: {held}." if held else ""),
                "The knock engine is running but barely sending. Check the "
                "knock_error and knock_last_ok lines on /api/summary, then "
                "/admin/config-check for the send gates.")
    if ok:
        _mark_alerted("knocks_silent")
    return f"{due} due, {sent} sent, {expected} expected"


def _check_delivery_collapse():
    """Most of what we send is bouncing. Meta is refusing us, not one bad number."""
    row = db.q("""SELECT count(*) AS n,
                         count(*) FILTER (WHERE status='failed') AS bad
                  FROM message_delivery
                  WHERE created_at > now() - interval '24 hours'""", one=True) or {}
    n, bad = row.get("n") or 0, row.get("bad") or 0
    if n < DELIVERY_MIN_SENDS:
        return None                      # too little traffic to mean anything
    pct = round(100.0 * bad / n)
    if pct < DELIVERY_FAIL_PCT:
        return None
    if _muted("delivery"):
        return f"{pct}% of sends failing (muted)"

    top = db.q("""SELECT COALESCE(reason,'(no reason given)') AS r, count(*) AS n
                  FROM message_delivery
                  WHERE status='failed' AND created_at > now() - interval '24 hours'
                  GROUP BY 1 ORDER BY 2 DESC LIMIT 1""", one=True) or {}

    ok = _alert("delivery",
                f"MESSAGES ARE NOT ARRIVING - {pct}% failed in 24h",
                f"{bad} of {n} sends failed. Most common: {_clean(top.get('r'))[:150]}",
                "Meta is restricting the number. Sending harder makes it worse -- "
                "consider pausing knocks until the quality rating recovers.")
    if ok:
        _mark_alerted("delivery")
    return f"{pct}% of sends failing"


def _check_poller_wedged():
    """New leads stopped arriving because the Meta poller never finished.

    Reads a heartbeat that poll_meta_leads writes on each SUCCESSFUL completion.
    A run that hangs writes nothing, which is precisely the state that is
    invisible from the outside -- APScheduler logs "maximum number of running
    instances reached" and carries on looking healthy.
    """
    # NO HEARTBEAT IS NOT THE SAME AS NO PROBLEM.
    #
    # This originally returned None whenever the heartbeat was missing, reasoning
    # "never run yet, nothing to compare against". That made a poller which hangs on
    # its FIRST run after a deploy permanently invisible: it never writes a
    # heartbeat, so the check that exists to catch it stays silent forever. The bug
    # this whole module was written to remove, reintroduced inside the fix for it.
    #
    # So when the heartbeat is absent we measure from process start instead. The
    # poller runs every minute; if the service has been up far longer than
    # POLLER_STALE_MIN and no run has ever finished, that is a wedge, not a warm-up.
    raw = db.get_setting(_HB_META_LEADS)
    since = "completed"
    if not raw:
        raw = db.get_setting("app_boot_at")
        since = "started"
        if not raw:
            # Pre-dates the boot marker (first deploy of this code). Nothing
            # trustworthy to measure against; the next boot supplies one.
            return None
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return None
    age_min = (datetime.now(last.tzinfo) - last).total_seconds() / 60
    if age_min < POLLER_STALE_MIN:
        return None
    if _muted("poller"):
        return f"meta poller stale {int(age_min)}m (muted)"

    detail = (f"The Meta lead poller has not completed for {int(age_min)} minutes. "
              f"Form submissions are probably not being picked up.")
    if since == "started":
        # Measured from boot, not from a previous success. Say so -- "has never
        # completed" is a different and worse fact than "has stopped completing".
        detail = (f"The Meta lead poller has NEVER completed since the service "
                  f"started {int(age_min)} minutes ago. Form submissions are "
                  f"probably not being picked up at all.")

    ok = _alert("poller",
                "NEW LEADS MAY HAVE STOPPED ARRIVING",
                detail,
                "A poll run is hung and holding its slot. Redeploy the service to "
                "clear it, then watch that this alert does not come back.")
    if ok:
        _mark_alerted("poller")
    return f"meta poller stale {int(age_min)}m"


def _check_lane_broken():
    """A send lane inside the tick has been throwing, and nobody could have known.

    THE WIRE THAT WAS NEVER JOINED, AGAIN. sequencer.tick() runs two lanes, each
    wrapped in its own try/except so one cannot silence the other, and each writing
    its exception to `<lane>_error`. Nothing read those keys. Not the daily report,
    not any dashboard route; the only other mention in the codebase was the string
    in _check_nobody_contacted below, telling the reader to look at
    /admin/config-check -- a page that does not contain them. So the ghost lane
    could have failed on its first tick after deploy and every screen would have
    read healthy.

    WHY BOTH KEYS ARE NEEDED, and this is the part that makes the check honest:

      * an error with a RECENT success is a lane that threw and recovered. Not an
        alarm -- exactly the transient this must not cry wolf about.
      * an error with a STALE success is a lane that has been down for an hour.
      * NO success and NO error is not health, it is a lane that has never run.
        Measured from boot, like the poller check, for the same reason: a check
        that stays quiet because it has nothing to look at is the failure this
        module exists to remove.

    One alert per lane, so a broken knock engine cannot mask a broken re-opener.
    """
    out = []
    for key, label in LANES:
        err = (db.get_setting(f"{key}_error") or "").strip()
        raw = db.get_setting(f"{key}_last_ok")
        since = "completed"
        if not raw:
            raw = db.get_setting("app_boot_at")
            since = "started"
            if not raw:
                # Pre-dates the boot marker. Nothing trustworthy to measure
                # against; the next boot supplies one.
                continue
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:
            continue
        age_min = (datetime.now(last.tzinfo) - last).total_seconds() / 60
        if age_min < LANE_BROKEN_MIN:
            # Ran recently. An error string here is a blip it recovered from.
            continue
        if since == "completed" and not err:
            # Succeeded within living memory but not lately, and is not throwing.
            # That is _check_nobody_contacted's territory (leads due, none sent) and
            # not a fault in the lane itself -- two checks alerting on one fact is
            # how a reader learns to skim both.
            continue
        if _muted(f"lane_{key}"):
            out.append(f"{key} lane broken {int(age_min)}m (muted)")
            continue

        if since == "started":
            detail = (f"{label} has NEVER completed since the service started "
                      f"{int(age_min)} minutes ago.")
        else:
            detail = (f"{label} has not completed for {int(age_min)} minutes.")
        if err:
            detail += f" Last error: {err[:200]}"

        ok = _alert(f"lane_{key}",
                    f"{label.upper()} IS NOT RUNNING",
                    detail,
                    "This lane is failing on every tick and sends nothing. Check "
                    f"the {key}_error line on /api/summary, then the deploy logs.")
        if ok:
            _mark_alerted(f"lane_{key}")
        out.append(f"{key} lane broken {int(age_min)}m")
    return "; ".join(out) if out else None


def _check_unbacked_promises():
    """The bot mentioned a person, and no card was ever sent. THE LOOSE NET.

    handoff.route() force-escalates when a reply MATCHES a promise pattern, which
    handles the phrasings we have seen. It cannot handle the ones we have not: every
    round of widening that regex against the live corpus found another wording --
    "let me get someone", "happy to have someone call you", "photos are on the way
    from my colleague". A pattern over the model's own prose is never finished, and
    this project already has a name for that ceiling: guards judge words, not facts.

    So this asks a deliberately sloppier question -- did we mention a human at all,
    and did anyone get told? -- and hands the answer to a person instead of acting on
    it. A false positive costs one glance. A miss costs a buyer.
    """
    rows = db.q("""
        SELECT DISTINCT l.phone, l.name, m.ts
        FROM message_log m
        JOIN leads l ON l.id = m.lead_id
        LEFT JOIN conversations c ON c.lead_id = m.lead_id
        WHERE m.direction='out' AND m.msg_type='qualifier_turn' AND m.ok
          AND m.ts > now() - interval '48 hours'
          AND m.body ~* '(colleague|our team|the team|someone|somebody|from my team)'
          AND COALESCE(c.outcome,'') NOT IN
              ('escalated','qualified','wants_sales','visit_booked')
          AND NOT EXISTS (SELECT 1 FROM message_log h
                           WHERE h.lead_id = m.lead_id
                             AND h.msg_type LIKE 'handoff%%')
        ORDER BY m.ts DESC""") or []
    if not rows:
        return None
    if _muted("promises"):
        return f"{len(rows)} unbacked promises (muted)"

    who = ", ".join(str(r.get("phone") or "?") for r in rows[:5])
    if len(rows) > 5:
        who += f" +{len(rows) - 5} more"

    ok = _alert("promises",
                f"CHECK {len(rows)} BUYER(S) - we mentioned a person, nobody was told",
                f"{who}. The bot referred to a colleague or the team in the last 48h "
                f"and no handoff card exists for them.",
                "Read these in the Wati inbox. If the bot promised contact, somebody "
                "needs to make it -- the automatic escalation only catches wordings "
                "we have seen before.")
    if ok:
        _mark_alerted("promises")
    return f"{len(rows)} unbacked promises"


def check():
    """One pass over every signal. Never raises -- a broken watchdog must
    not take the scheduler down with it, and each signal is independent so one
    failing query must not hide the others."""
    found = []
    for fn in (_check_failed_jobs, _check_undelivered_cards, _check_queue_stalled,
               _check_nobody_contacted, _check_delivery_collapse,
               _check_poller_wedged, _check_lane_broken, _check_unbacked_promises):
        try:
            r = fn()
            if r:
                found.append(r)
        except Exception as e:                       # noqa: BLE001
            log.exception("watchdog signal %s failed", fn.__name__)
            db.set_setting("watchdog_error", f"{fn.__name__}: {e}"[:400])
    db.set_setting("watchdog_last_run", datetime.now().astimezone().isoformat())
    return found


def daily_report(force=False):
    """One line a day, whether or not anything is wrong.

    THIS IS THE POINT OF IT. Without a heartbeat, a watchdog that has stopped
    running is indistinguishable from a quiet day -- and the owner would read the
    silence as good news, which is precisely the failure this module exists to
    remove. It costs one WhatsApp message a day.

    Also carries the numbers worth seeing daily, so it earns its place twice.
    """
    today = datetime.now().astimezone().date().isoformat()
    if not force and db.get_setting(_LAST_DAILY) == today:
        return None

    def n(sql, params=None):
        r = db.q(sql, params, one=True)
        return (r or {}).get("n") or 0

    convos = n("""SELECT count(DISTINCT lead_id) AS n FROM message_log
                  WHERE direction='in' AND ts > now() - interval '24 hours'""")
    replies = n("""SELECT count(*) AS n FROM message_log
                   WHERE direction='out' AND msg_type='qualifier_turn'
                     AND ts > now() - interval '24 hours'""")
    # Was a correct copy of the same query. Now the shared helper, so a change to
    # what counts as an attempted knock reaches the alert and the report together.
    knocks, _last_knock_at = _knocks_attempted(24)
    # COUNTED SEPARATELY, BECAUSE 'knock%' DOES NOT MATCH IT. reopener_t7 was
    # invisible in this report and in /admin/drip alike, so the one number a reader
    # sees each morning could not distinguish a working ghost lane from one that had
    # never sent anything at all.
    reopens = n("""SELECT count(*) AS n FROM message_log
                   WHERE direction='out' AND msg_type='reopener_t7'
                     AND ts > now() - interval '24 hours'
                     AND (detail IS NULL OR detail NOT LIKE 'blocked:%%')""")
    visits = n("""SELECT count(*) AS n FROM message_log
                  WHERE msg_type='handoff_visit'
                    AND ts > now() - interval '24 hours'""")
    qualified = n("""SELECT count(*) AS n FROM message_log
                     WHERE msg_type='handoff_qualified'
                       AND ts > now() - interval '24 hours'""")
    stuck = n("""SELECT count(*) AS n FROM message_log
                 WHERE msg_type IN ('handoff_escalation','handoff_stalling')
                   AND ts > now() - interval '24 hours'""")
    failed = n("""SELECT count(*) AS n FROM job_queue WHERE status='failed'""")

    # THE VERDICT USED TO READ `"nothing failed" if not failed else ...`, where
    # `failed` counted failed JOBS and nothing else. Failed jobs were zero all
    # through the fortnight the knock engine sent nothing, so the report said
    # "nothing failed" every morning -- accurately, about the only thing it looked
    # at. A daily line that can only report on plumbing trains its reader to skim
    # it. So the verdict is now assembled from the same outcome signals the alerts
    # use, and says plainly when it has nothing bad to report.
    # Aliased: `knocks` is already the local 24h knock COUNT above, and importing
    # over it made the report print a module object where a number belonged.
    import knocks as knocks_mod
    try:
        due, _waiting = knocks_mod.sendable_count()
    except Exception:                                # noqa: BLE001
        due = 0
    deliv = db.q("""SELECT count(*) AS n,
                           count(*) FILTER (WHERE status='failed') AS bad
                    FROM message_delivery
                    WHERE created_at > now() - interval '24 hours'""", one=True) or {}
    dn, dbad = deliv.get("n") or 0, deliv.get("bad") or 0
    fail_pct = round(100.0 * dbad / dn) if dn else 0
    stalled = n("""SELECT count(*) AS n FROM conversations
                   WHERE outcome IS NULL AND last_turn_at < now() - interval '3 days'""")

    problems = []
    if failed:
        problems.append(f"{failed} failed jobs")
    # A SHARE, NOT A ZERO -- the same test the silence alert uses, from the same
    # helper. `due and not knocks` needed knocks to be exactly 0, so the morning
    # after 309 leads sat unreachable this line read as a clean bill of health
    # because two messages had crept out.
    expected_knocks = _knocks_expected(due, 24)
    if due and knocks < expected_knocks:
        problems.append(f"{due} leads due a knock, only {knocks} went out "
                        f"(expected at least {expected_knocks})")
    if dn >= DELIVERY_MIN_SENDS and fail_pct >= DELIVERY_FAIL_PCT:
        problems.append(f"{fail_pct}% of sends not arriving")
    if stalled:
        problems.append(f"{stalled} conversations stalled 3d+")
    # A LANE THAT IS THROWING BELONGS IN THE HEARTBEAT, not only in an alert. The
    # alert mutes after one send per hour and a phone can be missed; the daily line
    # is the surface that cannot be missed, and a broken lane is exactly the kind of
    # thing that quietly persists for a fortnight. Same reasoning as the verdict
    # rewrite above.
    for key, label in LANES:
        if (db.get_setting(f"{key}_error") or "").strip():
            problems.append(f"{label} is failing")

    verdict = ("ALL CLEAR - nothing needs you today" if not problems
               else "NEEDS YOU: " + "; ".join(problems))

    ok = _alert("daily",
                "RON bot - 24 hour report",
                f"{convos} buyers talked to us, {replies} replies sent, "
                f"{knocks} knocks out ({due} still due), {reopens} re-openers. "
                f"{visits} visits booked, "
                f"{qualified} qualified, {stuck} handed to a human. "
                f"{fail_pct}% of sends failed.",
                verdict)
    if ok:
        db.set_setting(_LAST_DAILY, today)
    return {"conversations": convos, "replies": replies, "knocks": knocks,
            "reopeners": reopens,
            "visits": visits, "qualified": qualified, "stuck": stuck,
            "failed_jobs": failed, "due_now": due, "delivery_fail_pct": fail_pct,
            "stalled_conversations": stalled, "problems": problems,
            "sent": bool(ok)}
