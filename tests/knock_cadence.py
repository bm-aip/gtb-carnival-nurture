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

# --- the per-step hold --------------------------------------------------------
# A hold must be visible to BOTH the SQL candidate filter and the Python loop.
# due_count() is SQL-only and the watchdog alerts when leads are due and nothing
# goes out, so a hold only the loop could see would look like a starving engine.
ksrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                         "knocks.py"), encoding="utf-8").read()
r.check("the SQL filters held steps",
        "COALESCE(ks.sent, 0) + 1 = ANY (%s::int[])" in ksrc,
        detail="the count the watchdog reads must match the batch that sends")
r.check("the loop re-checks held steps",
        "if step_key in KNOCK_STEPS_PAUSED:" in ksrc,
        detail="the loop is the authority; SQL is only a candidate filter")

held = knocks.KNOCK_STEPS_PAUSED
pos = knocks._paused_step_positions()
r.eq("positions are 1-based and match the held keys", len(pos), len(held))
r.check("every held key is a real step",
        all(k in KEYS for k in held), detail=str(held))
r.check("holding nothing yields an empty list, not a null",
        isinstance(pos, list), detail=str(pos))
if held:
    for k in held:
        r.eq(f"held step {k} maps to position {KEYS.index(k) + 1}",
             KEYS.index(k) + 1 in pos, True)
r.check("the visit ask is never held by default",
        "t6_visit" not in held,
        detail="holding the only untested template would defeat the purpose")

# --- the watchdog must measure the ENGINE, not the SQL -------------------------
# due_count() counts SQL candidates; due() sends what the loop accepts. On
# 2026-08-26 that was 349 vs 129 and the watchdog shouted "NOBODY IS BEING
# CONTACTED" while the engine was correctly waiting out a 24h retry gap.
wsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                         "watchdog.py"), encoding="utf-8").read()
r.check("the silence alert reads sendable_count, not due_count",
        "knocks.sendable_count()" in wsrc
        and "due = knocks.due_count()" not in wsrc,
        detail="alerting on candidates cries wolf every time a lane cools off")
r.check("the daily report reads it too",
        "knocks_mod.sendable_count()" in wsrc and
        "knocks_mod.due_count()" not in wsrc)

r.check("knocks exposes sendable_count", hasattr(knocks, "sendable_count"))
r.check("and a single shared verdict", hasattr(knocks, "_verdict"),
        detail="one decision function, so counter and engine cannot drift")

# The counter must never end a journey. _give_up belongs to due() alone.
# Parsed rather than grepped: the docstrings mention _give_up on purpose, and a
# substring search cannot tell an explanation from a call.
import ast as _ast

_tree = _ast.parse(ksrc)


def _calls_in(fn_name):
    for node in _ast.walk(_tree):
        if isinstance(node, _ast.FunctionDef) and node.name == fn_name:
            body = list(node.body)
            if (body and isinstance(body[0], _ast.Expr)
                    and isinstance(body[0].value, _ast.Constant)):
                body = body[1:]                      # drop the docstring
            out = set()
            for sub in body:
                for n in _ast.walk(sub):
                    if isinstance(n, _ast.Call):
                        f = n.func
                        out.add(getattr(f, "id", None) or getattr(f, "attr", None))
            return out
    return set()


r.check("_verdict has no side effects", "_give_up" not in _calls_in("_verdict"),
        detail="it REPORTS 'ceiling'; only the engine acts on it")
r.check("sendable_count writes nothing",
        not ({"_give_up", "x"} & _calls_in("sendable_count")),
        detail="a monitor that marks leads lost is not a monitor")
r.check("due() still acts on the ceiling",
        "_give_up(lead, step_key, config.KNOCK_RETRY_MAX)" in ksrc)

# --- filter before you limit ---------------------------------------------------
# due() used to take ONE window of limit*5 candidates, oldest first, then reject
# in Python on things the SQL does not model (retry gap, duplicate phones). On
# 2026-08-26 the 220 oldest rows were all rejects, they filled the 125-row window,
# and 129 sendable leads behind them were invisible for eleven hours.
r.check("due() pages through candidates", "OFFSET %s" in ksrc,
        detail="a fixed window is starved by a backlog of stale rejects")
r.check("the scan is bounded", "SCAN_MAX" in ksrc and "SCAN_PAGE" in ksrc,
        detail="paging must not become a table scan on every tick")
r.check("the page is bigger than one batch",
        knocks.SCAN_PAGE > config.SEND_BATCH_PER_TICK,
        detail=f"page={knocks.SCAN_PAGE} batch={config.SEND_BATCH_PER_TICK}")
r.check("and the walk goes far past any plausible reject backlog",
        knocks.SCAN_MAX >= 10 * config.SEND_BATCH_PER_TICK,
        detail=str(knocks.SCAN_MAX))

if __name__ == "__main__":
    sys.exit(0 if r.report("KNOCK CADENCE") else 1)
