"""The watchdog, as tests. No database, no API, ~1 second.

An alerting system is the one component whose failure is INVISIBLE: if it never
fires, everything looks fine. So the parts that decide whether to fire are tested
directly, with the database faked, rather than trusted.

Run: python tests/watchdog_rules.py
"""
import io
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

# One test deliberately explodes a signal to prove the other two still fire. That
# path logs a traceback on purpose; silence it so a passing run reads as passing.
logging.getLogger("watchdog").setLevel(logging.CRITICAL)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _bootstrap import Results

import config
import watchdog as w

R = Results()


class FakeDB:
    """Stands in for db. Records what was written, answers what we tell it to."""

    def __init__(self, rows=None, settings=None):
        self.rows = rows or {}
        self.settings = dict(settings or {})
        self.queries = []

    def q(self, sql, params=None, one=False):
        self.queries.append(sql)
        for key, val in self.rows.items():
            if key in sql:
                return val
        return (None if one else [])

    def get_setting(self, k, default=None):
        return self.settings.get(k, default)

    def set_setting(self, k, v):
        self.settings[k] = str(v)


def _patch(fake, sent):
    w.db = fake
    w.handoff = type("H", (), {
        "_notify": staticmethod(lambda phones, slots, kind: (
            sent.append({"phones": phones, "slots": slots, "kind": kind}) or True))})()


# --------------------------------------------------------------------------
# Who hears about it
# --------------------------------------------------------------------------
def test_recipients():
    # Owner 2026-08-08: alerts go to him ONLY. A salesperson cannot act on "the
    # queue is stalled", and putting it in the channel they trust for hot leads
    # teaches them to skim that channel.
    R.eq("alerts do not go to the whole staff list",
         config.ALERT_PHONES != config.STAFF_PHONES, True)
    R.eq("alert list is not empty (an unset default fails like the silence "
         "it detects)", bool(config.ALERT_PHONES), True)


# --------------------------------------------------------------------------
# Template parameters must survive WhatsApp
# --------------------------------------------------------------------------
def test_clean():
    # Newlines in a template parameter are rejected by Meta, and this project has
    # already lost a message to a non-ASCII character surviving into a URL query
    # param (the rupee sign).
    R.eq("newlines flattened", "\n" not in w._clean("a\nb"), True)
    R.eq("tabs flattened", "\t" not in w._clean("a\tb"), True)
    R.eq("non-ascii stripped", w._clean("Rs 3.94 ₹ Cr").isascii(), True)
    R.eq("empty becomes a placeholder, never blank", w._clean(""), "-")
    R.eq("long text truncated", len(w._clean("x" * 5000)) <= 900, True)


# --------------------------------------------------------------------------
# Failed jobs -- the incident this module exists for
# --------------------------------------------------------------------------
def test_failed_jobs_alert():
    sent = []
    fake = FakeDB(rows={"FROM job_queue": [
        {"id": 11, "kind": "reply", "phone": "919840168185", "attempts": 5,
         "last_error": "credit balance is too low"},
        {"id": 12, "kind": "reply", "phone": "919003044700", "attempts": 5,
         "last_error": "credit balance is too low"},
    ]})
    _patch(fake, sent)
    w._check_failed_jobs()
    R.eq("failed jobs raise exactly one alert", len(sent), 1)
    R.eq("headline says buyers got no reply",
         "no reply" in sent[0]["slots"][0].lower(), True)
    R.eq("the actual error reaches the owner",
         "credit" in " ".join(sent[0]["slots"]).lower(), True)
    R.eq("watermark advances so the same rows never re-alert",
         fake.settings.get(w._WM_JOBS), "12")

    # WHICH BUYER IS SITTING THERE UNANSWERED (2026-08-19). The card used to carry
    # a count and nothing else, so on 2026-08-18 two people went unanswered
    # overnight and the alert could not say who. Nobody can act on a number.
    card = " ".join(sent[0]["slots"])
    R.eq("the buyer's number reaches the owner", "919840168185" in card, True)
    R.eq("...and the second one too", "919003044700" in card, True)
    R.eq("the card says how to answer them", "replay" in card.lower(), True)


def test_undelivered_cards_count_cards_not_rows():
    """One card that retries writes two rows. 2026-08-19 that reported "2 card(s)
    did not reach anyone" for ONE card to one recipient, while the other recipient
    had received it. An alert that doubles the damage is read at the exact moment
    someone is deciding how alarmed to be."""
    sent = []
    fake = FakeDB(rows={"FROM message_log": [
        {"id": 3667, "msg_type": "handoff_wants_sales", "lead_id": 1413,
         "detail": "Read timed out. (read timeout=30)"},
        {"id": 3668, "msg_type": "handoff_wants_sales_text", "lead_id": 1413,
         "detail": '{"result":false,"message":"Ticket has been expired."}'},
    ]})
    _patch(fake, sent)
    w._check_undelivered_cards()
    R.eq("one card raises one alert", len(sent), 1)
    card = " ".join(sent[0]["slots"])
    R.eq("and it says ONE card, not two", "1 card(s)" in card, True)
    R.eq("...not the row count", "2 card(s)" in card, False)


def test_undelivered_cards_two_real_cards_still_count_two():
    sent = []
    fake = FakeDB(rows={"FROM message_log": [
        {"id": 10, "msg_type": "handoff_escalation", "lead_id": 1, "detail": "x"},
        {"id": 11, "msg_type": "handoff_escalation", "lead_id": 2, "detail": "y"},
    ]})
    _patch(fake, sent)
    w._check_undelivered_cards()
    R.eq("two different leads are two cards",
         "2 card(s)" in " ".join(sent[0]["slots"]), True)


def test_failed_jobs_watermark_blocks_repeat():
    sent = []
    # Same two rows, but both already reported.
    fake = FakeDB(rows={"FROM job_queue": []}, settings={w._WM_JOBS: "12"})
    _patch(fake, sent)
    w._check_failed_jobs()
    R.eq("already-reported failures do not alert again", len(sent), 0)


def test_mute_does_not_lose_the_backlog():
    # THE TRAP: muting must not advance the watermark, or whatever failed during
    # the quiet hour is silently forgotten and nobody ever hears about it.
    sent = []
    now = datetime.now(timezone.utc).isoformat()
    fake = FakeDB(rows={"FROM job_queue": [{"id": 20, "kind": "reply", "attempts": 5,
                                            "last_error": "boom"}]},
                  settings={w._LAST_ALERT + "jobs": now})
    _patch(fake, sent)
    w._check_failed_jobs()
    R.eq("muted: nothing sent", len(sent), 0)
    R.eq("muted: watermark NOT advanced, backlog survives",
         fake.settings.get(w._WM_JOBS), None)


# --------------------------------------------------------------------------
# A stalled queue is what a dead worker looks like
# --------------------------------------------------------------------------
def test_queue_stall():
    sent = []
    old = datetime.now(timezone.utc) - timedelta(minutes=40)
    fake = FakeDB(rows={"status='queued'": {"t": old, "n": 7}})
    _patch(fake, sent)
    w._check_queue_stalled()
    R.eq("a 40-minute-old queue alerts", len(sent), 1)
    R.eq("the fix is named in the message",
         "WORKER_IN_PROCESS" in " ".join(sent[0]["slots"]), True)

    sent2 = []
    fresh = datetime.now(timezone.utc) - timedelta(minutes=2)
    fake2 = FakeDB(rows={"status='queued'": {"t": fresh, "n": 3}})
    _patch(fake2, sent2)
    w._check_queue_stalled()
    R.eq("a busy-but-moving queue does NOT alert", len(sent2), 0)

    sent3 = []
    fake3 = FakeDB(rows={"status='queued'": {"t": None, "n": 0}})
    _patch(fake3, sent3)
    w._check_queue_stalled()
    R.eq("an empty queue does NOT alert", len(sent3), 0)


# --------------------------------------------------------------------------
# Undelivered staff cards -- watch the PR #32 fix rather than trust it
# --------------------------------------------------------------------------
def test_undelivered_cards():
    sent = []
    fake = FakeDB(rows={"FROM message_log": [
        {"id": 5, "msg_type": "handoff_escalation", "detail": "Ticket has been expired."}]})
    _patch(fake, sent)
    w._check_undelivered_cards()
    R.eq("an undelivered handoff alerts", len(sent), 1)
    R.eq("it says a human was never told",
         "nobody was told" in " ".join(sent[0]["slots"]).lower(), True)


# --------------------------------------------------------------------------
# One broken signal must not hide the other two
# --------------------------------------------------------------------------
def test_check_survives_a_broken_signal():
    sent = []

    class Exploding(FakeDB):
        def q(self, sql, params=None, one=False):
            if "job_queue" in sql and "status='failed'" in sql:
                raise RuntimeError("column gone")
            return super().q(sql, params, one)

    old = datetime.now(timezone.utc) - timedelta(minutes=40)
    fake = Exploding(rows={"status='queued'": {"t": old, "n": 2}})
    _patch(fake, sent)
    found = w.check()
    R.eq("a failing signal does not raise", isinstance(found, list), True)
    R.eq("the other signals still fire", len(sent) >= 1, True)
    R.eq("the failure is recorded, not swallowed",
         "watchdog_error" in fake.settings, True)
    R.eq("last-run stamp written even on a bad pass",
         "watchdog_last_run" in fake.settings, True)


# --------------------------------------------------------------------------
# The heartbeat -- the only proof the watching is still happening
# --------------------------------------------------------------------------
def test_daily_report():
    sent = []
    # Most specific key first: FakeDB returns the first pattern found in the SQL,
    # so a bare "count" would also swallow the failed-jobs tally and the heartbeat
    # would report a problem on a clean day.
    # STALLED CONVERSATIONS MUST BE ZERO FOR THIS FIXTURE, and it needs its own
    # key. FakeDB returns the first pattern it finds in the SQL, so the bare
    # "count" below also answered the stalled-conversation tally with 4 -- and a
    # verdict that correctly reports "4 conversations stalled 3d+" is not the
    # clean day this case is trying to describe. The assertion predates the
    # outcome signals PR #60 added to the verdict; the fixture, not the code, was
    # left behind.
    fake = FakeDB(rows={"status='failed'": {"n": 0},
                        "outcome IS NULL AND last_turn_at": {"n": 0},
                        "count": {"n": 4}})
    _patch(fake, sent)
    out = w.daily_report()
    R.eq("daily report sends", len(sent), 1)
    # "nothing failed" WAS THE OLD WORDING and has not existed for some time. The
    # verdict was rewritten to be assembled from outcome signals rather than from
    # the failed-jobs tally alone -- watchdog.py records why -- and its clean-day
    # text is now "ALL CLEAR - nothing needs you today". This assertion kept
    # looking for the retired string, so the one case covering a clean heartbeat
    # could never pass, and the suite carried a permanent red mark that trained
    # its readers to skim past exactly the kind of signal this module exists for.
    R.eq("it goes out even when nothing is wrong",
         "all clear" in " ".join(sent[0]["slots"]).lower(), True)
    R.eq("it reports today's date as sent", bool(out and out["sent"]), True)

    sent2 = []
    _patch(fake, sent2)
    w.daily_report()
    R.eq("it does not send twice in one day", len(sent2), 0)


# --------------------------------------------------------------------------
# A trickle is not a pulse -- the 2026-09-03 stall
# --------------------------------------------------------------------------
# Both knock guards asked "was the count exactly zero". 309 leads were sendable,
# one or two knocks dribbled out a day, and for nine days the honest answer to
# "is it zero" was no. The engine was 99% stopped and nothing said so.
def test_knocks_expected_arithmetic():
    """The threshold itself, as a pure function. No database.

    Named for the knock lane because that is where it was found, but the function
    is lane-agnostic and the re-opener guard shares it -- see
    test_both_lanes_share_one_threshold."""
    R.eq("nothing due means nothing expected, so a quiet day stays quiet",
         w._expected_sends(0, 6), 0)

    # 309 owed at 10% is 31. Two sends is the stall that went unreported.
    R.eq("309 owed over 6h expects 31", w._expected_sends(309, 6), 31)

    # Rounds UP, so a handful due can never expect zero and slip through.
    R.eq("3 owed still expects at least 1", w._expected_sends(3, 6), 1)
    R.eq("1 owed still expects at least 1", w._expected_sends(1, 6), 1)

    # CLAMPED TO WHAT THE SENDER MAY ACTUALLY PUSH. Without this the guard would
    # alarm about its own rate limit as soon as the backlog grew past it, which is
    # the false-positive that makes an alert worthless.
    headroom = max(1, config.MAX_SENDS_PER_HOUR - config.REPLY_RESERVE_PER_HOUR)
    R.eq("a huge backlog is clamped to the hourly headroom",
         w._expected_sends(100000, 6), headroom * 6)
    R.check("and that clamp is below the naive share",
            w._expected_sends(100000, 6) < 100000 * w.KNOCK_STALL_PCT // 100)


def test_attempted_excludes_blocked_but_not_refused():
    """`blocked:` rows are not sends. Refusals ARE -- the engine tried."""
    fake = FakeDB(rows={"count": {"n": 7, "last_at": None}})
    _patch(fake, [])
    n, _last = w._knocks_attempted(6)
    R.eq("it returns the count", n, 7)

    sql = " ".join(fake.queries)
    R.check("blocked rows are excluded -- they never touched WhatsApp",
            "NOT LIKE 'blocked:" in sql)
    # Filtering on ok would hide a working engine having a bad night with Meta,
    # which is a different alert that already exists.
    R.check("but ok is NOT the filter, so a refusal still counts as an attempt",
            "AND ok" not in sql)


def _silence_verdict(due, sent):
    """Run the silence check with `due` leads sendable and `sent` knocks out."""
    import knocks

    real = knocks.sendable_count
    knocks.sendable_count = lambda *a, **k: (due, {})
    # The check reads a count, then a last-sent timestamp; both come from the
    # same helper, so one fixture answers both.
    fake = FakeDB(rows={"count": {"n": sent, "last_at": None}})
    alerts = []
    _patch(fake, alerts)
    try:
        return w._check_nobody_contacted(), alerts
    finally:
        knocks.sendable_count = real


def test_a_trickle_still_alerts():
    # THE ACTUAL DEFECT: 309 sendable, 2 sent, nine days of silence.
    verdict, alerts = _silence_verdict(309, 2)
    R.check("309 due and 2 sent raises the alarm", bool(verdict))
    R.eq("and it actually notifies somebody", len(alerts), 1)
    body = " ".join(alerts[0]["slots"]) if alerts else ""
    R.check("the alert says how many went out, not that nobody was contacted",
            "2" in body and "nobody" not in body.lower())

    # The old behaviour is preserved, not replaced: total silence still trips.
    verdict, alerts = _silence_verdict(1, 0)
    R.check("1 due and 0 sent still raises the alarm", bool(verdict))
    R.check("and THAT one may say nobody was contacted",
            "nobody" in " ".join(alerts[0]["slots"]).lower() if alerts else False)


def test_a_working_engine_stays_quiet():
    # A healthy share must not alarm, or the guard gets muted by its own noise
    # and we are back to nobody reading it.
    verdict, alerts = _silence_verdict(309, 40)
    R.eq("309 due and 40 sent is normal", verdict, None)
    R.eq("and nobody is woken", len(alerts), 0)

    # Nothing due is the ordinary quiet day the ladder's 3/8/15-day gaps produce.
    verdict, alerts = _silence_verdict(0, 0)
    R.eq("nothing due, nothing sent, no alarm", verdict, None)
    R.eq("still nobody woken", len(alerts), 0)



def _daily_with(due, knocks_sent, stalled=0):
    """Run the daily report with `due` sendable and `knocks_sent` knocks out."""
    import knocks

    real = knocks.sendable_count
    knocks.sendable_count = lambda *a, **k: (due, {})
    fake = FakeDB(rows={"status='failed'": {"n": 0},
                        "outcome IS NULL AND last_turn_at": {"n": stalled},
                        "message_delivery": {"n": 0, "bad": 0},
                        "count": {"n": knocks_sent, "last_at": None}})
    sent = []
    _patch(fake, sent)
    try:
        return w.daily_report(force=True), sent
    finally:
        knocks.sendable_count = real


def test_the_morning_line_reports_a_stall():
    """THE LINE THE OWNER ACTUALLY READS.

    On 2026-09-03 it said "2 knocks out (309 still due)" and then pronounced the
    day fine, because the verdict needed knocks to be exactly zero. The narrative
    carried the evidence and the verdict ignored it -- which is worse than not
    measuring it at all, because it looks like it was checked.
    """
    out, sent = _daily_with(due=309, knocks_sent=2)
    R.eq("the report still goes out", len(sent), 1)
    problems = (out or {}).get("problems") or []
    R.check("a 99% stall is named as a problem",
            any("due a knock" in x for x in problems))
    verdict = " ".join(sent[0]["slots"]).lower() if sent else ""
    R.check("and the verdict says NEEDS YOU, not all clear",
            "needs you" in verdict and "all clear" not in verdict)


def test_the_morning_line_stays_calm_when_healthy():
    out, sent = _daily_with(due=309, knocks_sent=40)
    problems = (out or {}).get("problems") or []
    R.check("a healthy share is not called a problem",
            not any("due a knock" in x for x in problems))

    out, sent = _daily_with(due=0, knocks_sent=0)
    problems = (out or {}).get("problems") or []
    R.check("and neither is an ordinary quiet day",
            not any("due a knock" in x for x in problems))



# --------------------------------------------------------------------------
# Nobody is being woken up -- 379 stalled AND 0 re-openers
# --------------------------------------------------------------------------
# The daily report printed both halves of this on one line and compared neither.
def _reopener_verdict(owed, sent):
    """Run the re-opener guard with `owed` people due and `sent` re-opens out."""
    import reopener

    real = reopener.due
    reopener.due = lambda limit=None: [("row", 0, "topic")] * owed
    fake = FakeDB(rows={"count": {"n": sent, "last_at": None}})
    alerts = []
    _patch(fake, alerts)
    try:
        return w._check_reopener_silent(), alerts
    finally:
        reopener.due = real


def test_reopener_silence_alerts():
    verdict, alerts = _reopener_verdict(owed=120, sent=0)
    R.check("120 owed a re-open and none sent raises the alarm", bool(verdict))
    R.eq("and it actually notifies somebody", len(alerts), 1)
    body = " ".join(alerts[0]["slots"]) if alerts else ""
    R.check("the alert names how many are waiting", "120" in body)

    # A trickle must not silence it either -- the same lesson as the knock lane.
    verdict, alerts = _reopener_verdict(owed=120, sent=1)
    R.check("120 owed and 1 sent still raises the alarm", bool(verdict))


def test_reopener_quiet_when_healthy():
    verdict, alerts = _reopener_verdict(owed=0, sent=0)
    R.eq("nobody owed a re-open means no alarm", verdict, None)
    R.eq("and nobody woken", len(alerts), 0)

    verdict, alerts = _reopener_verdict(owed=20, sent=5)
    R.eq("20 owed and 5 sent is a working lane", verdict, None)


def test_reopener_asks_the_lane_not_the_stalled_count():
    """THE DESIGN DECISION, PINNED. `stalled 3d+` is a different population.

    This lane also needs the dormancy window, at most REOPEN_MAX deliveries, its
    own spacing, a usable topic, and it skips dead and visit-booked. Alerting on
    the stalled tally would fire about people the lane is correctly leaving alone
    -- the mismatch that produced a false NOBODY IS BEING CONTACTED on 2026-08-26.
    """
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "watchdog.py"), encoding="utf-8").read()
    body = src[src.index("def _check_reopener_silent"):
               src.index("def _check_delivery_collapse")]

    # CODE ONLY, NOT THE PROSE. The docstring explains at length why knocks.due()
    # must never be called from here, so a substring search over the whole
    # function failed on the explanation itself -- a test that could not tell a
    # warning about a thing from the thing.
    quote = chr(34) * 3
    code = body.split(quote)[2] if body.count(quote) >= 2 else body
    code = " ".join(ln for ln in code.splitlines()
                    if not ln.strip().startswith("#"))

    R.check("it asks reopener.due(), the code that does the sending",
            "reopener.due(" in code)
    R.check("it does not reach for the stalled-conversations tally",
            "last_turn_at" not in code)

    # knocks.due() calls _give_up() and therefore WRITES. A monitor must never
    # call it; wiring it in here would end people's journeys from a counter.
    R.check("and it never calls the knock picker, which has side effects",
            "knocks.due(" not in code)


def test_both_lanes_share_one_threshold():
    """reopener_t7 is business-initiated exactly like a knock, so it sits under the
    same reply reserve. Two thresholds would drift, and a monitor that measures
    something subtly different from the engine it watches is worse than none."""
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "watchdog.py"), encoding="utf-8").read()
    R.eq("both guards call the same expectation helper",
         src.count("_expected_sends(") >= 3, True)
    R.check("and there is no second, lane-specific copy of it",
            "_knocks_expected(" not in src and "_reopens_expected(" not in src)

    # One predicate for "a send was attempted", too -- the alert's own count was
    # the copy that lacked the blocked-row filter for nine days.
    R.check("and one predicate for what counts as an attempted send",
            src.count("def _sends_attempted(") == 1)


if __name__ == "__main__":
    test_recipients()
    test_clean()
    test_failed_jobs_alert()
    test_failed_jobs_watermark_blocks_repeat()
    test_mute_does_not_lose_the_backlog()
    test_queue_stall()
    test_undelivered_cards()
    test_undelivered_cards_count_cards_not_rows()
    test_undelivered_cards_two_real_cards_still_count_two()
    test_check_survives_a_broken_signal()
    test_daily_report()
    test_knocks_expected_arithmetic()
    test_attempted_excludes_blocked_but_not_refused()
    test_a_trickle_still_alerts()
    test_a_working_engine_stays_quiet()
    test_the_morning_line_reports_a_stall()
    test_the_morning_line_stays_calm_when_healthy()
    test_reopener_silence_alerts()
    test_reopener_quiet_when_healthy()
    test_reopener_asks_the_lane_not_the_stalled_count()
    test_both_lanes_share_one_threshold()
    sys.exit(0 if R.report("WATCHDOG RULES") else 1)
