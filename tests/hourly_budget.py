"""A bookkeeping row is not a send. No database, no API, ~1 second.

    python tests/hourly_budget.py

TWICE NOW, THE SAME MISTAKE, AND THE FIRST FIX IS WHY THE SECOND HAPPENED.

`message_log` is the diary of what happened to a lead, not a log of messages. Some
rows are filed as direction='out' and never touched WhatsApp:

  2026-08-22  `matched`        a lead paired with a phone number. Four rows ate
                               four of a hundred hourly slots.
  2026-09-04  `knock_gave_up`  we STOPPED chasing somebody -- the end of sending,
                               recorded as though it were a send. Retiring 127
                               leads from a dead ad wrote 126 of them in twenty
                               minutes, the counter read 133 against a cap of 100,
                               and every real message was refused for an hour. The
                               re-opener, unjammed minutes earlier, picked 23
                               people and had 20 turned away at the door.

The 2026-08-22 fix named `matched` in a WHERE clause. That is why the second one was
free to happen: the rule lived as one word rather than as a list, so nothing about
adding a new bookkeeping type made anybody check this counter. NOT_A_SEND is that
list, and these tests assert the BEHAVIOUR of the counter, not the text of it.

WHY IT MATTERS MORE THAN THE ARITHMETIC. The hourly cap is what holds back the
reply reserve, so a counter reading high does not merely delay marketing -- it
starves live buyers. That is the 2026-08-22 incident this cap was built for.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _bootstrap import Results        # noqa: E402

import config                          # noqa: E402
import wati                            # noqa: E402

R = Results()


class FakeLog:
    """Answers sends_last_hour()'s query against rows held in memory.

    Applies the same three predicates the real query does -- outbound, not a
    bookkeeping type, and not a gate block -- so the test exercises the rule
    rather than a hand-written expectation of it.
    """

    def __init__(self, rows):
        self.rows = rows

    def q(self, sql, params=None, one=False):
        excluded = set(params[0]) if params else set()
        n = 0
        for r in self.rows:
            if r.get("direction") != "out":
                continue
            if r["msg_type"] in excluded:
                continue
            if not (r.get("ok") or not str(r.get("detail") or "").startswith("blocked:")):
                continue
            n += 1
        return {"n": n} if one else [{"n": n}]


def row(msg_type, ok=True, detail=None, direction="out"):
    return {"msg_type": msg_type, "ok": ok, "detail": detail, "direction": direction}


def count(rows):
    real = wati.db.q
    wati.db = type("D", (), {"q": staticmethod(FakeLog(rows).q)})()
    try:
        return wati.sends_last_hour()
    finally:
        wati.db = type("D", (), {"q": staticmethod(real)})()


# --- the three states of a row, unchanged ------------------------------------
R.eq("a delivered message counts", count([row("knock_t1_lifestyle")]), 1)
R.eq("a message the provider refused still counts",
     count([row("knock_t1_lifestyle", ok=False, detail="meta refused")]), 1)
R.eq("a message our own gate blocked does not count",
     count([row("knock_t1_lifestyle", ok=False, detail="blocked:rate_capped")]), 0)
R.eq("an inbound message is not a send",
     count([row("qualifier_turn", direction="in")]), 0)

# --- the bookkeeping rows -----------------------------------------------------
R.eq("2026-08-22: `matched` is not a send", count([row("matched")]), 0)
R.eq("2026-09-04: `knock_gave_up` is not a send",
     count([row("knock_gave_up", ok=False, detail="12 attempts all refused")]), 0)

# THE INCIDENT, AT ITS REAL SIZE. 126 retirement rows plus a handful of genuine
# sends. Before the fix this returned 133 against a cap of 100 and shut the door
# on everybody.
incident = ([row("knock_gave_up", ok=False, detail="ad 52553896609352 retired")] * 126
            + [row("reopener_t7")] * 3
            + [row("handoff_alert")] * 2
            + [row("knock_t3_low_density", ok=False, detail="meta refused")] * 2)
used = count(incident)
R.eq("retiring a dead ad's cohort costs no send allowance at all", used, 7)
R.check("and the hour stays open for real messages",
        used < config.MAX_SENDS_PER_HOUR - config.REPLY_RESERVE_PER_HOUR,
        detail="used %d of %d proactive slots"
               % (used, config.MAX_SENDS_PER_HOUR - config.REPLY_RESERVE_PER_HOUR))

# --- the reserve still does its job ------------------------------------------
# Excluding bookkeeping must not accidentally excuse real traffic. A genuine
# marketing burst must still hit the proactive ceiling and leave the reply
# reserve untouched -- the whole point of the cap (Sanjay Agarwalla, 2026-08-22).
headroom = config.MAX_SENDS_PER_HOUR - config.REPLY_RESERVE_PER_HOUR
burst = [row("knock_t1_lifestyle")] * headroom
real_q = wati.db.q
wati.db = type("D", (), {"q": staticmethod(FakeLog(burst).q)})()
try:
    R.check("a real burst still closes the door on more marketing",
            not wati.rate_ok("knock_t1_lifestyle"),
            detail="%d proactive sends should exhaust the headroom" % headroom)
    R.check("and a live buyer can still be answered",
            wati.rate_ok("qualifier_turn"),
            detail="the reply reserve is what this cap exists to protect")
finally:
    wati.db = type("D", (), {"q": staticmethod(real_q)})()

# --- the list is the rule, not a special case --------------------------------
R.check("bookkeeping types are held in one named list",
        isinstance(wati.NOT_A_SEND, (tuple, list)) and len(wati.NOT_A_SEND) >= 2,
        detail="one word in a WHERE clause is what let the second one through")
R.check("`matched` is still in it", "matched" in wati.NOT_A_SEND)
R.check("`knock_gave_up` is in it", "knock_gave_up" in wati.NOT_A_SEND)

if __name__ == "__main__":
    sys.exit(0 if R.report("HOURLY BUDGET") else 1)
