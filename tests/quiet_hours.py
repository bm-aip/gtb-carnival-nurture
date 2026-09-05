"""Cold messages wait for morning, and waiting is not trying. ~1 second, no database.

    python tests/quiet_hours.py

WHY THIS EXISTS
---------------
`sequencer.quiet_now()` was written with the comment "held for the knock engine to
consult". Nothing ever consulted it. On 2026-09-04 six cold nurture templates went
out at 23:40 IST -- for several of those people the first message they had ever
had from us, at twenty to midnight.

It is the sixth rule found in one week that was written correctly and connected to
nothing: delivery callbacks arriving and read by no code, phantom rows written and
counted by nothing, a failed-boot alarm nobody hears, an experiment that finished
with nobody watching, a daily send cap whose function no caller calls. A rule with
no test is a comment, and this file is the difference.

THE TWO PROPERTIES, AND WHY EACH ONE MATTERS
--------------------------------------------
1. COLD WAITS, LIVE DOES NOT. Quiet hours belong to the KIND of message, not to
   the person. Somebody who writes at 11pm gets an answer at 11pm: it is inside a
   session they opened, it costs nothing with Meta, and making them wait until
   morning is worse service dressed as better manners.

2. WAITING IS NOT TRYING, AND MUST LEAVE NO ROW. `last_try` counts every row of a
   msg_type, so a 2am non-event would push that person's next message days into
   the future. That is precisely what happened on 2026-09-04: twenty buyers had a
   re-open blocked by our own rate cap, the block was written down as an attempt,
   and they were put to sleep for three days having received nothing at all.
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _bootstrap import Results        # noqa: E402

import knocks                          # noqa: E402
import reopener                        # noqa: E402
import sequencer                       # noqa: E402
import wati                            # noqa: E402

R = Results()


def at(hour, minute=0):
    return dt.datetime(2026, 9, 5, hour, minute)


# --- the window itself --------------------------------------------------------
for h, m, quiet, label in [(19, 29, False, "19:29 still sends"),
                           (19, 30, True, "19:30 is quiet"),
                           (23, 40, True, "23:40 -- the send that started this"),
                           (2, 0, True, "2am is quiet"),
                           (7, 59, True, "07:59 still quiet"),
                           (8, 0, False, "08:00 opens up"),
                           (12, 0, False, "midday sends")]:
    R.eq(label, sequencer.quiet_now(at(h, m)), quiet)

# --- cold waits, live does not ------------------------------------------------
# Reusing the reserve's own predicate rather than a second list: a rule that lives
# in two places is how the 2026-08-22 bookkeeping fix named one word instead of
# making a list, which is why the same bug recurred on 2026-09-04.
for msg_type in ("knock_t1_lifestyle", "knock_t6_visit", "reopener_t7"):
    R.check("%s is cold, so it waits" % msg_type,
            wati.is_business_initiated(msg_type))
for msg_type in ("qualifier_turn", "answer"):
    R.check("%s is a live reply, so it goes at any hour" % msg_type,
            not wati.is_business_initiated(msg_type),
            detail="somebody who writes at 11pm is answered at 11pm")


# --- the door holds, and leaves NO row ----------------------------------------
class Spy:
    """Stands in for the database so a logged row can be caught in the act."""

    def __init__(self):
        self.rows = []
        self.settings = []

    def log_msg(self, *a, **k):
        self.rows.append((a, k))

    def set_setting(self, *a, **k):
        self.settings.append(a)

    def q(self, *a, **k):
        return {"n": 0} if k.get("one") else []

    def x(self, *a, **k):
        return 0


def send_at(hour, msg_type):
    """Call the real door at a given hour. Returns (result, rows written)."""
    spy = Spy()
    real_db, real_now, real_gate = sequencer.db, sequencer.now_ist, sequencer.sendgate
    sequencer.db = spy
    sequencer.now_ist = lambda: at(hour)
    sequencer.sendgate = type("G", (), {
        "check": staticmethod(lambda *a, **k: (True, None)),
        "sends_enabled": staticmethod(lambda: True)})()
    try:
        out = sequencer._send({"id": 1, "phone": "919000000000", "project": "RON"},
                              msg_type, template="t", params={})
    except Exception as e:                      # a live reply raises to be retried
        out = ("raised", type(e).__name__)
    finally:
        sequencer.db, sequencer.now_ist = real_db, real_now
        sequencer.sendgate = real_gate
    return out, spy.rows


out, rows = send_at(2, "knock_t1_lifestyle")
R.eq("a cold knock at 2am does not go", out, False)
R.check("and writes NO row -- waiting is not trying", len(rows) == 0,
        detail="a row here would move that buyer's clock days out for a non-event; "
               "got %d row(s)" % len(rows))

out, rows = send_at(23, "reopener_t7")
R.eq("a re-open at 11pm does not go", out, False)
R.eq("and writes no row either", len(rows), 0)

# --- the lanes stop early -----------------------------------------------------
# The door is the authority; the lane check is so a whole page of candidates is
# not walked and rejected one by one. Same reasoning as the hourly brake.
def lane_runs_at(module, hour):
    """Run the lane's real run() at a given hour. Did it even look for work?"""
    looked = []
    # `module.sequencer` IS the sequencer module -- the same object, not a copy --
    # so the real function has to be captured before it is replaced, or the stub
    # calls itself forever.
    real_due, real_quiet = module.due, sequencer.quiet_now
    real_budget = sequencer.daily_budget
    module.due = lambda *a, **k: looked.append(1) or []
    sequencer.quiet_now = lambda n=None: real_quiet(at(hour))
    # Stubbed generous: this test is about the CLOCK. Leaving the real one here
    # would reach for a database and, worse, let a spent daily budget mask a
    # broken quiet-hours check by stopping the lane for the wrong reason.
    sequencer.daily_budget = lambda: 999
    try:
        module.run()
    finally:
        module.due, sequencer.quiet_now = real_due, real_quiet
        sequencer.daily_budget = real_budget
    return bool(looked)


R.check("the knock engine does not even pick candidates at 3am",
        not lane_runs_at(knocks, 3))
R.check("but it does at 10am", lane_runs_at(knocks, 10))
R.check("the re-opener does not pick candidates at 3am",
        not lane_runs_at(reopener, 3))
R.check("but it does at 10am", lane_runs_at(reopener, 10))

# --- THE REGRESSION THAT MATTERS ----------------------------------------------
# quiet_now() existed for weeks with a comment saying the knock engine consults
# it, and the knock engine did not. These assertions fail the moment it is
# disconnected again, in the door or in either lane.
here = os.path.dirname(__file__)


def src(name):
    return open(os.path.join(here, "..", name), encoding="utf-8").read()


R.check("the send door still calls quiet_now()", "quiet_now()" in src("sequencer.py"))
R.check("knocks.run() still calls it", "sequencer.quiet_now()" in src("knocks.py"),
        detail="its docstring claimed this for weeks while nothing called it")
R.check("reopener.run() still calls it", "sequencer.quiet_now()" in src("reopener.py"))
R.check("the door gates on is_business_initiated, not a hand-written list",
        "is_business_initiated(msg_type) and quiet_now()" in src("sequencer.py"),
        detail="one rule, one place -- two copies is how the counters drifted")


# --- THE DAILY TIER CAP -------------------------------------------------------
# `daily_budget()` carried the docstring "used by the knock engine's scheduler"
# and was called by nothing. DAILY_SEND_CAP=500 sat in the config, and on the page
# that displays it, and bounded nothing at all -- the real ceiling was the hourly
# one, about 1,900 a day. Two bugs, in code no caller called.
import config                            # noqa: E402
import fatigue                           # noqa: E402


class CountingSpy(Spy):
    """A database that reports a fixed number of proactive sends in 24h."""

    def __init__(self, used):
        Spy.__init__(self)
        self.used = used

    def q(self, sql, params=None, one=False):
        return {"n": self.used} if one else [{"n": self.used}]


def budget_with(used):
    real = sequencer.db
    sequencer.db = CountingSpy(used)
    try:
        return sequencer.daily_budget()
    finally:
        sequencer.db = real


R.eq("a quiet day leaves the whole allowance", budget_with(0), config.DAILY_SEND_CAP)
R.eq("spent sends come off it", budget_with(100), config.DAILY_SEND_CAP - 100)
R.eq("at the cap there is nothing left", budget_with(config.DAILY_SEND_CAP), 0)
R.eq("over the cap never goes negative", budget_with(config.DAILY_SEND_CAP + 50), 0)

# IT MUST NOT RAISE. The old query held a bare `%` inside a call psycopg2 was
# asked to parameterise, so every invocation died on `IndexError: tuple index out
# of range`. Nothing noticed, because nothing called it.
raised = None
try:
    budget_with(0)
except Exception as e:
    raised = type(e).__name__
R.eq("counting the day's sends does not raise", raised, None)


def sql_of(used):
    """The SQL daily_budget() actually issues, so the population can be asserted."""
    seen = {}

    class Capture(CountingSpy):
        def q(self, sql, params=None, one=False):
            seen["sql"], seen["params"] = sql, params
            return {"n": self.used}

    real = sequencer.db
    sequencer.db = Capture(used)
    try:
        sequencer.daily_budget()
    finally:
        sequencer.db = real
    return seen


got = sql_of(0)
R.check("the cap counts only messages that ACTUALLY went",
        "ok" in got["sql"] and "direction='out'" in got["sql"],
        detail="a refused or held message must not eat the day's allowance")
R.check("the pattern is a parameter, not a literal % in the query",
        "knock%" in str(got["params"]),
        detail="the bare literal is what made this raise on every call")
R.check("re-openers count against the daily cap too",
        "reopener_t7" in str(got["params"]),
        detail="the old query counted `knock%%` alone, so every re-opener and "
               "first-touch was invisible to a cap meant to bound them")
R.check("...and the m-series first touches",
        "m1" in str(got["params"]) and "m3" in str(got["params"]))
R.check("the population comes from wati's own list, not a second copy",
        set(wati.COLD_FIRST_TOUCH) <= set(got["params"][1]),
        detail="one definition -- a copy is how it drifted in the first place")

# --- the cap holds at the door, and leaves no row -----------------------------
def send_with_budget_spent(msg_type):
    """Call the real door with the day's allowance exhausted."""
    spy = CountingSpy(config.DAILY_SEND_CAP + 10)
    real_db, real_now = sequencer.db, sequencer.now_ist
    real_gate, real_rate = sequencer.sendgate, sequencer.wati.rate_ok
    sequencer.db = spy
    sequencer.now_ist = lambda: at(11)          # broad daylight, so only the cap bites
    sequencer.sendgate = type("G", (), {
        "check": staticmethod(lambda *a, **k: (True, None)),
        "sends_enabled": staticmethod(lambda: True)})()
    sequencer.wati.rate_ok = lambda mt=None: True
    try:
        out = sequencer._send({"id": 1, "phone": "919000000000", "project": "RON"},
                              msg_type, template="t", params={})
    except Exception as e:
        out = ("raised", type(e).__name__)
    finally:
        sequencer.db, sequencer.now_ist = real_db, real_now
        sequencer.sendgate, sequencer.wati.rate_ok = real_gate, real_rate
    return out, spy.rows


out, rows = send_with_budget_spent("knock_t1_lifestyle")
R.eq("a cold knock past the daily cap does not go", out, False)
R.check("and writes no row -- tomorrow is not a failed attempt", len(rows) == 0,
        detail="got %d row(s)" % len(rows))

# --- and it is actually wired in ----------------------------------------------
R.check("the send door calls daily_budget()",
        "daily_budget() <= 0" in src("sequencer.py"),
        detail="it went months with a docstring saying it was called, and no caller")
R.check("knocks.run() calls it", "sequencer.daily_budget()" in src("knocks.py"))
R.check("reopener.run() calls it", "sequencer.daily_budget()" in src("reopener.py"))
R.check("the cap applies to cold sends only, never to a live reply",
        "is_business_initiated(msg_type) and daily_budget()" in src("sequencer.py"),
        detail="answering someone mid-conversation must never hit a marketing cap")


if __name__ == "__main__":
    sys.exit(0 if R.report("QUIET HOURS AND THE DAILY CAP") else 1)
