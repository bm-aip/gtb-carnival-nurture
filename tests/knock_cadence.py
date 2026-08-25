"""The knock ladder against the one ceiling it cannot argue with. ~1 second.

    python tests/knock_cadence.py

t6_visit sat on day 25 and was never sent once -- 0 deliveries against 83 for t2
and 80 for t3 -- because almost no journey survives 25 days intact. Owner moved
the cycle to 15 days on 2026-08-25.

The trap this pins: FATIGUE_MAX_PER_WINDOW (2 per 7 days) can never be reset, so
a schedule that puts three steps inside any 7-day window does not send faster --
the third send is refused and the step is simply LOST. A compressed ladder must
be checked against the ceiling, not just eyeballed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bootstrap import Results        # noqa: E402

import config                          # noqa: E402
import knocks                          # noqa: E402

r = Results()

DAYS = [d for d, _ in knocks.KNOCK_SCHEDULE]
KEYS = [k for _, k in knocks.KNOCK_SCHEDULE]

r.eq("the cycle finishes within 15 days", max(DAYS), 15)
r.eq("still four steps", len(knocks.KNOCK_SCHEDULE),
     config.KNOCK_MAX_PER_JOURNEY)
r.check("days strictly increase", all(b > a for a, b in zip(DAYS, DAYS[1:])),
        detail=str(DAYS))
r.eq("the visit ask is the last step", KEYS[-1], "t6_visit")
r.check("every step has a template configured",
        all(k in config.KNOCK_TEMPLATES for k in KEYS), detail=str(KEYS))

# THE CEILING. Any window of FATIGUE_WINDOW_DAYS may hold at most
# FATIGUE_MAX_PER_WINDOW steps, or the extra ones are refused and lost.
W, CAP = config.FATIGUE_WINDOW_DAYS, config.FATIGUE_MAX_PER_WINDOW
worst, worst_at = 0, None
for start in range(0, max(DAYS) + 1):
    n = sum(1 for d in DAYS if start <= d < start + W)
    if n > worst:
        worst, worst_at = n, start
r.check(f"no {W}-day window holds more than {CAP} knocks", worst <= CAP,
        detail=f"worst window starts day {worst_at} and holds {worst}")

# The min-gap guard derives from the schedule, so it must stay consistent with it.
gaps = [knocks._min_gap_days(i) for i in range(len(knocks.KNOCK_SCHEDULE))]
r.eq("first step has no gap requirement", gaps[0], 0)
r.check("later gaps match the schedule's own spacing",
        gaps[1:] == [b - a for a, b in zip(DAYS, DAYS[1:])], detail=str(gaps))
r.check("no gap is zero, so a backlog cannot fire the ladder in one day",
        all(g >= 1 for g in gaps[1:]), detail=str(gaps))

if __name__ == "__main__":
    sys.exit(0 if r.report("KNOCK CADENCE") else 1)
