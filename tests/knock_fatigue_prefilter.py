"""The fatigue cap must be a SELECTION rule, not only a last-moment one.

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
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bootstrap import Results        # noqa: E402  (stubs env, fixes console)

import config                          # noqa: E402
import fatigue                         # noqa: E402
import knocks                          # noqa: E402

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

knocks.knock_state = lambda phone: (0, None)
knocks.attempt_state = lambda phone, step_key: (0, None)


def verdict_when(allowed, cap):
    fatigue.check = lambda phone, msg_type, project=None: (allowed, cap)
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

fatigue.check = _real_check
knocks.knock_state = _real_knock_state
knocks.attempt_state = _real_attempt_state

# --- the reasons are human, because a person reads them -----------------------
# _verdict's reasons are grouped and printed in the watchdog's NOBODY IS BEING
# CONTACTED alert as "Others waiting: 3 waiting on the weekly cap". A raw code
# there is the difference between a stuck engine and a lane correctly cooling off.
for cap, text in knocks._FATIGUE_REASON.items():
    r.check(f"{cap} reads as English, not a code", " " in text and "_" not in text)

r.eq("both of fatigue's ceilings are mapped",
     sorted(knocks._FATIGUE_REASON), sorted([fatigue.CAP_WINDOW, fatigue.CAP_JOURNEY]))

# --- the last door stays ------------------------------------------------------
# knock_now() sends straight from the leadgen webhook without passing through
# _verdict at all. If this fix ever "tidies up" the check inside send_knock, that
# path loses its fatigue guard entirely and a burst becomes possible.
_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "knocks.py"), encoding="utf-8").read()
r.check("send_knock still calls fatigue.check before the wire",
        "allowed, reason = fatigue.check(" in _src)
r.check("_verdict checks fatigue too", "allowed, cap = fatigue.check(" in _src)

# The picker must ask LAST, after every cheaper reason has already returned --
# otherwise two counting queries run for every row in a 3,000-row scan window.
r.check("_verdict asks fatigue after the retry block",
        _src.index("waiting out the retry gap") < _src.index("allowed, cap = fatigue.check("))

sys.exit(0 if r.report("KNOCK FATIGUE PREFILTER") else 1)
