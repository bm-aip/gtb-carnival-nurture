"""The watchdog, as tests. No database, no API, ~1 second.

An alerting system is the one component whose failure is INVISIBLE: if it never
fires, everything looks fine. So the parts that decide whether to fire are tested
directly, with the database faked, rather than trusted.

Run: python tests/watchdog_rules.py
"""
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
        {"id": 11, "kind": "reply", "attempts": 5, "last_error": "credit balance is too low"},
        {"id": 12, "kind": "reply", "attempts": 5, "last_error": "credit balance is too low"},
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
    fake = FakeDB(rows={"status='failed'": {"n": 0}, "count": {"n": 4}})
    _patch(fake, sent)
    out = w.daily_report()
    R.eq("daily report sends", len(sent), 1)
    R.eq("it goes out even when nothing is wrong",
         "nothing failed" in " ".join(sent[0]["slots"]).lower(), True)
    R.eq("it reports today's date as sent", bool(out and out["sent"]), True)

    sent2 = []
    _patch(fake, sent2)
    w.daily_report()
    R.eq("it does not send twice in one day", len(sent2), 0)


if __name__ == "__main__":
    test_recipients()
    test_clean()
    test_failed_jobs_alert()
    test_failed_jobs_watermark_blocks_repeat()
    test_mute_does_not_lose_the_backlog()
    test_queue_stall()
    test_undelivered_cards()
    test_check_survives_a_broken_signal()
    test_daily_report()
    sys.exit(0 if R.report("WATCHDOG RULES") else 1)
