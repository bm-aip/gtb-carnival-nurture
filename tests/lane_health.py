"""A send lane that has died must say so. No database, no API, ~1 second.

    python tests/lane_health.py

sequencer.tick() runs two lanes -- the knock engine and the re-opener -- each
wrapped in its own try/except so a fault in one cannot silence the other. Each
wrote its exception to `<lane>_error`. Nothing in the system read those keys: not
the daily report, not any dashboard route. The only other mention in the codebase
was an alert string pointing at /admin/config-check, a page that does not contain
them. A lane could therefore throw on every tick from the moment of a deploy and
every surface would still read healthy -- the same shape as the fortnight the
knock engine spent silent, and as the watchdog wire that was never joined.

THE TWO FAILURE MODES THIS PINS DOWN, because getting either backwards restores
the original bug in a way that looks fixed:

  * crying wolf -- an error string that is never cleared makes the alarm permanent
    after one transient blip, and a permanent alarm is an ignored one.
  * silence on absence -- NO error and NO success is not health. A lane that never
    runs raises nothing, so an error-only check stays quiet forever about the
    worst case. Measured from boot, like the poller check.
"""
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

logging.getLogger("watchdog").setLevel(logging.CRITICAL)
logging.getLogger("sequencer").setLevel(logging.CRITICAL)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _bootstrap import Results        # noqa: E402

import watchdog as w                   # noqa: E402

R = Results()


class FakeDB:
    """Stands in for db. Records what was written, answers what we tell it to."""

    def __init__(self, rows=None, settings=None):
        self.rows = rows or {}
        self.settings = dict(settings or {})

    def q(self, sql, params=None, one=False):
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


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


_REAL_DB, _REAL_HANDOFF = w.db, w.handoff


# --------------------------------------------------------------------------
# The alarm fires when it should
# --------------------------------------------------------------------------
def test_dead_lane_alerts():
    sent = []
    _patch(FakeDB(settings={
        "reopener_error": "psycopg2.OperationalError: connection closed",
        "reopener_last_ok": _ago(180),
        "knock_last_ok": _ago(1),
    }), sent)
    out = w._check_lane_broken()
    R.eq("a lane erroring for 3h alerts", len(sent), 1)
    R.check("the alert names the lane in English, not a key",
            "RE-OPENER" in sent[0]["slots"][0].upper())
    R.check("the error text reaches the reader",
            "OperationalError" in sent[0]["slots"][3])
    R.check("the return value reports it", "reopener" in (out or ""))


def test_never_ran_alerts():
    # THE WORST CASE AND THE QUIETEST. No error, no success -- a lane that has
    # never executed. An error-only check would say nothing here forever.
    sent = []
    _patch(FakeDB(settings={"app_boot_at": _ago(240), "knock_last_ok": _ago(1)}), sent)
    w._check_lane_broken()
    R.eq("a lane that has never run alerts", len(sent), 1)
    R.check("and says NEVER, which is a worse fact than 'stopped'",
            "NEVER" in sent[0]["slots"][3])


def test_each_lane_alerts_separately():
    # One alert per lane. A broken knock engine must not mask a broken re-opener.
    sent = []
    _patch(FakeDB(settings={
        "knock_error": "boom", "knock_last_ok": _ago(120),
        "reopener_error": "bang", "reopener_last_ok": _ago(120)}), sent)
    w._check_lane_broken()
    R.eq("both broken lanes alert independently", len(sent), 2)


# --------------------------------------------------------------------------
# And stays quiet when it should
# --------------------------------------------------------------------------
def test_transient_blip_is_not_an_alarm():
    # Threw, then recovered. This is the case that would make the alarm permanent
    # if `_error` were read without `_last_ok`.
    sent = []
    _patch(FakeDB(settings={
        "knock_error": "one bad row", "knock_last_ok": _ago(2),
        "reopener_last_ok": _ago(2)}), sent)
    R.eq("a lane that threw but ran a minute ago does not alert", len(sent), 0)
    R.eq("and reports nothing", w._check_lane_broken(), None)


def test_healthy_is_silent():
    sent = []
    _patch(FakeDB(settings={"knock_last_ok": _ago(1),
                            "reopener_last_ok": _ago(1)}), sent)
    w._check_lane_broken()
    R.eq("two healthy lanes are silent", len(sent), 0)


def test_quiet_but_not_broken_is_not_this_check():
    # Ran a while ago, no error. That is _check_nobody_contacted's job (leads due,
    # none sent). Two checks alerting on one fact teaches a reader to skim both.
    sent = []
    _patch(FakeDB(settings={"knock_last_ok": _ago(300),
                            "reopener_last_ok": _ago(300)}), sent)
    w._check_lane_broken()
    R.eq("stale but not erroring is left to the knocks-silent check", len(sent), 0)


def test_no_boot_marker_is_not_an_alarm():
    # First deploy of this code: nothing trustworthy to measure against.
    sent = []
    _patch(FakeDB(settings={}), sent)
    w._check_lane_broken()
    R.eq("no heartbeat and no boot marker stays quiet", len(sent), 0)


def test_mute_holds():
    sent = []
    _patch(FakeDB(settings={
        "reopener_error": "boom", "reopener_last_ok": _ago(180),
        "knock_last_ok": _ago(1),
        "watchdog_last_alert_lane_reopener": datetime.now(timezone.utc).isoformat(),
    }), sent)
    out = w._check_lane_broken()
    R.eq("an already-reported lane does not re-alert within the mute", len(sent), 0)
    R.check("but is still reported to the caller", "muted" in (out or ""))


# --------------------------------------------------------------------------
# Wiring -- the part that was missing, so the part most worth asserting
# --------------------------------------------------------------------------
def test_wiring():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "watchdog.py"), encoding="utf-8").read()
    R.check("the check is registered in check()", "_check_lane_broken," in src)
    R.check("a broken lane reaches the daily report too",
            "is failing" in src)
    R.check("the daily report counts re-openers, which 'knock%' never matched",
            "msg_type='reopener_t7'" in src)
    R.check("the stale pointer to /admin/config-check is gone",
            "/admin/config-check and the knock_error setting" not in src)

    seq = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "sequencer.py"), encoding="utf-8").read()
    R.check("the lane clears its error on success",
            '_error", "")' in seq)
    R.check("and records a success heartbeat", '_last_ok"' in seq)
    R.check("both lanes go through the one helper", seq.count("_run_lane(") >= 3)

    app = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app.py"), encoding="utf-8").read()
    R.check("both lanes are visible on /api/summary", '"lanes": lanes' in app)

    R.eq("every lane in the tick is watched",
         sorted(k for k, _ in w.LANES), ["knock", "reopener"])


for fn in (test_dead_lane_alerts, test_never_ran_alerts,
           test_each_lane_alerts_separately, test_transient_blip_is_not_an_alarm,
           test_healthy_is_silent, test_quiet_but_not_broken_is_not_this_check,
           test_no_boot_marker_is_not_an_alarm, test_mute_holds, test_wiring):
    fn()

w.db, w.handoff = _REAL_DB, _REAL_HANDOFF
sys.exit(0 if R.report("LANE HEALTH") else 1)
