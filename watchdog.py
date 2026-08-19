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
from datetime import datetime, timedelta

import config
import db
import handoff

log = logging.getLogger("watchdog")

# How stale the front of the queue may get before it means the worker is dead.
# The sequencer ticks every few minutes and the worker drains continuously, so
# anything older than this is not backlog, it is a stopped process.
QUEUE_STALE_MIN = 15

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


def check():
    """One pass over all three signals. Never raises -- a broken watchdog must
    not take the scheduler down with it, and each signal is independent so one
    failing query must not hide the other two."""
    found = []
    for fn in (_check_failed_jobs, _check_undelivered_cards, _check_queue_stalled):
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
    knocks = n("""SELECT count(*) AS n FROM message_log
                  WHERE direction='out' AND msg_type LIKE 'knock%%'
                    AND ts > now() - interval '24 hours'""")
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

    verdict = "nothing failed" if not failed else f"{failed} FAILED JOBS - check /api/queue"
    ok = _alert("daily",
                "RON bot - 24 hour report",
                f"{convos} buyers talked to us, {replies} replies sent, "
                f"{knocks} knocks out. {visits} visits booked, {qualified} qualified, "
                f"{stuck} handed to a human.",
                verdict)
    if ok:
        db.set_setting(_LAST_DAILY, today)
    return {"conversations": convos, "replies": replies, "knocks": knocks,
            "visits": visits, "qualified": qualified, "stuck": stuck,
            "failed_jobs": failed, "sent": bool(ok)}
