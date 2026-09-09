"""Every last-moment gate must ALSO be a SELECTION rule. Two of them, so far.

    python tests/knock_fatigue_prefilter.py

No database, no API, ~1 second.

WHAT WENT WRONG. send_knock() has always asked fatigue.check() immediately before
the wire. _verdict() -- the picker -- did not. So a lead sitting at the weekly
ceiling looked sendable to the picker, was chosen on every tick, refused at the
door, and left a `blocked:fatigue:` row behind each time. Seven days to 2026-08-31:
29,865 such rows for t6_visit against 43 real sends; 35,156 across all steps
against 382. At SEQUENCER_TICK_MIN=1 that is roughly three people re-picked and
re-refused every sixty seconds, indefinitely.

WHY THE RETRY GAP IS NOT THE FIX, which is the part worth pinning down in a test
because it is the plausible wrong answer: attempt_state() excludes `blocked:` rows
deliberately -- our own gates must not consume one of the ten attempts at reaching
a person -- so it reports zero attempts and a null clock however many times fatigue
has refused. The gap can never engage. Only a check at selection time ends the loop.

Both directions are asserted. A picker that refuses everyone would stop the loop
too, and would be a far worse bug than the one being fixed.

IT HAPPENED AGAIN, 2026-09-03, in this same function. RETRY_MAX_BURST -- sendgate's
class-agnostic backstop, added 2026-08-25 -- was a last-moment gate the picker did
not model either. 23 leads whose t6/t2 Meta had stopped delivering were chosen every
tick, refused at the door, and left a `blocked:retry_ceiling_burst` row each time:
1,380 rows an hour, flat, for nine days. 135,496 rows, 84% of all of message_log.
Real knocks fell from 162 a day to one, because those 23 were the oldest due rows
and filled every batch of ten while 309 sendable buyers waited behind them.

Identical shape, identical reason the retry gap could not save it, identical fix.
So this file now guards BOTH gates, and the source assertions at the bottom exist
so that a third last-moment gate added to sendgate is caught here rather than in
production nine days later.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bootstrap import Results        # noqa: E402  (stubs env, fixes console)

import config                          # noqa: E402
import failures                        # noqa: E402
import fatigue                         # noqa: E402
import knocks                          # noqa: E402
import optout                          # noqa: E402
import sendgate                        # noqa: E402

r = Results()

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
LEAD = {"id": 1, "phone": "919999999999", "project": "RON",
        "anchor": NOW - timedelta(days=30)}

# _verdict reaches the database twice before it ever reaches fatigue. Both are
# stubbed to the state of a lead who is unambiguously due: no knock sent yet, so
# step 1 of the schedule, and its day-0 clock long past.
_real_knock_state = knocks.knock_state
_real_attempt_state = knocks.attempt_state
_real_check = fatigue.check
_real_failures_check = failures.check

knocks.knock_state = lambda phone: (0, None)
knocks.attempt_state = lambda phone, step_key: (0, None)

# BOTH gates are stubbed in every case, and each helper drives one of them while
# holding the other open. Without this the test reaches the real failures.check(),
# which counts message_log -- and a guard test that needs a database is a guard
# test nobody runs.
_ALLOW = (True, None)

# THE PICKER NOW ASKS THE WHOLE GATE (#88), so these cases run through the real
# sendgate.would_allow() rather than around it -- which is the point: it proves the
# fatigue cap and the retry ceiling still reach the picker THROUGH the gate, and
# that their reasons still arrive in the owner's words.
#
# Three things have to be held open for that to be a test of fatigue rather than a
# test of the environment. The master switch is OFF in a test process (SEND_ENABLED
# has no value outside Railway), `paused()` reads `settings` and `is_blocked()`
# reads `optouts` -- so left alone, every case below would block on the switch,
# each assertion would pass or fail for a reason that has nothing to do with what
# it claims to check, and the two that reach a database would need one.
_real_enabled, _real_paused = sendgate.sends_enabled, sendgate.paused
_real_is_blocked = optout.is_blocked
sendgate.sends_enabled = lambda: True
sendgate.paused = lambda: False
optout.is_blocked = lambda phone, project=None: (False, None)


def verdict_when(allowed, cap):
    """Drive the fatigue cap; the retry ceiling stays open."""
    fatigue.check = lambda phone, msg_type, project=None: (allowed, cap)
    failures.check = lambda phone, msg_type=None, project=None: _ALLOW
    return knocks._verdict(dict(LEAD), NOW, set())


def verdict_when_ceiling(allowed, cap):
    """Drive the retry ceiling; the fatigue cap stays open."""
    fatigue.check = lambda phone, msg_type, project=None: _ALLOW
    failures.check = lambda phone, msg_type=None, project=None: (allowed, cap)
    return knocks._verdict(dict(LEAD), NOW, set())


# --- the loop is closed -------------------------------------------------------
# The weekly ceiling is the one that cannot be reset, so it is the one that was
# generating the 29,865 rows.
step, key, reason = verdict_when(False, fatigue.CAP_WINDOW)
r.eq("a lead at the weekly cap is NOT sendable", reason is not None, True)
r.eq("and the reason names the weekly cap", reason, "waiting on the weekly cap")
r.eq("the step is still reported, so the count can group by it", key, "t1_lifestyle")

step, key, reason = verdict_when(False, fatigue.CAP_JOURNEY)
r.eq("a lead at the journey ceiling is NOT sendable", reason is not None, True)
r.eq("and that reason is distinct from the weekly one",
     reason, "journey ceiling reached")

# An unrecognised code must still block. Failing open here would restore the exact
# loop this fix removes, quietly, the first time fatigue gains a third ceiling.
step, key, reason = verdict_when(False, "some_new_cap")
r.eq("an unknown cap code still blocks", reason is not None, True)

# --- and nothing else is caught in it -----------------------------------------
step, key, reason = verdict_when(True, None)
r.eq("a lead under the cap is still sendable", reason, None)
r.eq("and carries its step index", step, 0)

# --- THE BURST CEILING, same loop, one week later ----------------------------
# This is the 2026-09-03 defect. Before the fix _verdict never asked, so a lead
# five-times-refused stayed sendable here forever.
step, key, reason = verdict_when_ceiling(False, failures.CEILING_BURST)
r.eq("a lead at the burst ceiling is NOT sendable", reason is not None, True)
r.eq("and the reason says the send keeps being refused",
     reason, "this send keeps being refused")
r.eq("the step is still reported, so the count can group by it", key, "t1_lifestyle")

# The other two ceilings share the door and must behave the same way. Neither was
# generating rows on 2026-09-03, which is exactly why they are worth asserting:
# nothing would have noticed if they were.
step, key, reason = verdict_when_ceiling(False, failures.CEILING_RECIPIENT)
r.eq("a number that cannot receive WhatsApp is NOT sendable", reason is not None, True)
r.eq("and that reason is distinct from the burst one",
     reason, "number cannot receive WhatsApp")

step, key, reason = verdict_when_ceiling(False, failures.CEILING_TRANSIENT)
r.eq("a lead at the transient ceiling is NOT sendable", reason is not None, True)

# Fail-open here would restore the loop silently the first time failures.py gains
# a fourth ceiling -- the same argument as the unknown fatigue cap above.
step, key, reason = verdict_when_ceiling(False, "some_new_ceiling")
r.eq("an unknown ceiling code still blocks", reason is not None, True)

# --- and nothing else is caught in it ---------------------------------------
step, key, reason = verdict_when_ceiling(True, None)
r.eq("a lead under every ceiling is still sendable", reason, None)
r.eq("and carries its step index", step, 0)

# --- the switch and the pause reach the picker too, now they are asked --------
# They never did before: _verdict copied two of the gate's rules and knew nothing
# about the other three, so with sending switched off or paused the engine went on
# choosing people every minute and the door refused every one of them.
def verdict_when_gate(reason):
    real = knocks.sendgate
    knocks.sendgate = type("G", (), {
        "would_allow": staticmethod(lambda p, m, project=None: (False, reason))})()
    try:
        return knocks._verdict(dict(LEAD), NOW, set())[2]
    finally:
        knocks.sendgate = real


r.eq("with sending switched off, nobody is even chosen",
     verdict_when_gate(sendgate.BLOCKED_DISABLED), "sending is switched off")
r.eq("an operator pause stops the picker, not just the door",
     verdict_when_gate(sendgate.BLOCKED_PAUSED), "sending is paused")
r.eq("and somebody who opted out is not chosen either",
     verdict_when_gate(sendgate.BLOCKED_OPTOUT_GLOBAL), "they asked us to stop")

fatigue.check = _real_check
failures.check = _real_failures_check
knocks.knock_state = _real_knock_state
knocks.attempt_state = _real_attempt_state
sendgate.sends_enabled, sendgate.paused = _real_enabled, _real_paused
optout.is_blocked = _real_is_blocked

# --- the reasons are human, because a person reads them -----------------------
# _verdict's reasons are grouped and printed in the watchdog's NOBODY IS BEING
# CONTACTED alert as "Others waiting: 3 waiting on the weekly cap". A raw code
# there is the difference between a stuck engine and a lane correctly cooling off.
for cap, text in knocks._FATIGUE_REASON.items():
    r.check(f"{cap} reads as English, not a code", " " in text and "_" not in text)

r.eq("both of fatigue's ceilings are mapped",
     sorted(knocks._FATIGUE_REASON), sorted([fatigue.CAP_WINDOW, fatigue.CAP_JOURNEY]))

for cap, text in knocks._CEILING_REASON.items():
    r.check(f"{cap} reads as English, not a code", " " in text and "_" not in text)

# All THREE, not only the burst one that bit us. A ceiling missing from the map
# still blocks -- the .get() falls back -- but the owner would read a raw code.
r.eq("all three retry ceilings are mapped",
     sorted(knocks._CEILING_REASON),
     sorted([failures.CEILING_BURST, failures.CEILING_RECIPIENT,
             failures.CEILING_TRANSIENT]))

# --- the last door stays ------------------------------------------------------
# knock_now() sends straight from the leadgen webhook without passing through
# _verdict at all. If this fix ever "tidies up" the check inside send_knock, that
# path loses its fatigue guard entirely and a burst becomes possible.
_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "knocks.py"), encoding="utf-8").read()
# READ FROM THE PARSE TREE. These were substring searches until #88; on
# 2026-09-03 a search of this kind passed on a COMMENT saying the opposite of what
# it asserted, and the comments in this file name every call it looks for.
import ast                             # noqa: E402


def _calls_in(src, fn_name):
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    return {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}


r.check("send_knock still calls fatigue.check before the wire",
        "fatigue.check" in _calls_in(_src, "send_knock"))
# ONE QUESTION, NOT A COPY OF TWO RULES (#88). _verdict used to call fatigue.check
# and failures.check itself -- a partial, hand-maintained copy of the door, and
# each half was added only after the missing one had caused an outage. It asks the
# gate now, so a rule added to the gate reaches SELECTION on the day it is written.
r.check("_verdict asks the send gate", "sendgate.would_allow" in _calls_in(_src, "_verdict"))
r.check("and keeps no private copy of the fatigue cap",
        "fatigue.check" not in _calls_in(_src, "_verdict"))
r.check("and keeps no private copy of the retry ceiling",
        "failures.check" not in _calls_in(_src, "_verdict"))

# sendgate is the one door every send passes through, and knock_now() reaches the
# wire without the picker. Both gates must survive there, or the picker becomes
# the only guard -- which is how a webhook send would escape both.
_gate = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "sendgate.py"), encoding="utf-8").read()
r.check("sendgate still checks fatigue",
        "fatigue.check" in _calls_in(_gate, "would_allow"))
r.check("sendgate still checks the retry ceiling",
        "failures.check" in _calls_in(_gate, "would_allow"))

# THE STANDING TRAP, stated once so the next person need not rediscover it:
# attempt_state() excludes `blocked:` rows on purpose, so ANY gate that refuses
# only at the wire and logs `blocked:` is retried every tick forever. Two have
# done it. A third must be checked in _verdict, not only in sendgate.
r.check("attempt_state still excludes blocked rows (the trap this guards)",
        "NOT LIKE 'blocked:" in _src)

# The picker must ask LAST, after every cheaper reason has already returned --
# otherwise the counting queries run for every row in a 3,000-row scan window.
#
# `.find`, NOT `.index`: index() raises on a missing substring, which killed the
# whole run before r.report() could print, so removing the call under test gave a
# traceback instead of a failed case. -1 sorts before everything, so an absent
# call now fails the check it belongs to and the other 31 cases still report.
def _at(needle):
    return _src.find(needle)


r.check("_verdict asks the gate after the retry block",
        -1 < _at("waiting out the retry gap")
        < _at("allowed, cap = sendgate.would_allow("))

# THE ORDER INSIDE THE GATE, which is where these two rules now live. Cheapest and
# most absolute first: the master switch needs no database at all, and the two
# counting queries run only for somebody otherwise ready to send.
_gi = [_gate.find(x) for x in ("sends_enabled()", "optout.is_blocked(",
                               "fatigue.check(", "failures.check(")]
r.check("the gate still asks in order: switch, opt-out, fatigue, ceiling",
        all(-1 < a < b for a, b in zip(_gi, _gi[1:])), detail=str(_gi))

sys.exit(0 if r.report("KNOCK FATIGUE PREFILTER") else 1)
