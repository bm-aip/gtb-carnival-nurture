"""The funnel counts honestly. No database, no network, ~1 second.

    python tests/funnel_counting.py

WHAT THESE PROTECT
------------------
2026-09-05. One question -- "is the nurture working" -- got three answers from
three counters in one afternoon: 2.3%, 40%, 66%. All arithmetically right, all
counting different things, nothing saying which. funnel.py exists so there is one
answer; these tests exist so it stays one, and stays honest.

The rules that keep it honest are small and easy to erode:

  UNKNOWN IS NEVER FOLDED. 19% of sends are accepted-and-never-heard-of-again.
  Folded into failure they read 45% delivery; into success, 90%. Both "true".

  A RATE WITH NO DENOMINATOR IS NOT ZERO. `0%` and "nothing was measured" look
  identical on a dashboard and mean opposite things.

  DELIVERED OVER SENT IS THE ONLY FAIR JUDGE OF A TEMPLATE. Acceptance and
  delivery pull in opposite directions -- t1_lifestyle was accepted 85% and
  delivered 57%; t6_visit accepted 17% and delivered 91%. Either column alone
  ranks them backwards.

  SQL MUST NOT MIX %-FORMATTING WITH psycopg2 PLACEHOLDERS. That collision broke
  this module's first run, the verification sheet before it, and an earlier
  script the same afternoon -- three times in one day, each costing a round trip
  to a live database to discover.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _bootstrap import Results        # noqa: E402

import config                          # noqa: E402
import funnel                          # noqa: E402
import wati                            # noqa: E402

R = Results()

# --- a rate with nothing behind it is not zero --------------------------------
R.check("no denominator gives None, not 0", funnel._rate(0, 0) is None,
        detail="a dash is honest; a zero invents a failure nobody measured")
R.eq("an ordinary rate", funnel._rate(1, 4), 25.0)
R.eq("nothing got through IS zero", funnel._rate(0, 4), 0.0)
R.eq("rates are rounded to one place", funnel._rate(1, 3), 33.3)


def row(sent=100, accepted=80, delivered=40, read_=20, unknown=30,
        rejected=20, failed_late=10, people=50, people_reached=25):
    return dict(sent=sent, accepted=accepted, delivered=delivered, read_=read_,
                unknown=unknown, rejected=rejected, failed_late=failed_late,
                people=people, people_reached=people_reached)


d = funnel._decorate(row())
R.eq("delivery is measured against ACCEPTED", d["delivery_rate"], 50.0)
R.eq("read is measured against DELIVERED", d["read_rate"], 50.0)
R.eq("reach is people, not messages", d["reach_rate"], 50.0)

# THE COLUMN THAT JUDGES THE TEMPLATE. Denominator must be everything we tried.
R.eq("end-to-end is delivered over SENT", d["end_to_end"], 40.0)

# The real 2026-09-05 pair, which rank differently on every other column.
t1 = funnel._decorate(row(sent=259, accepted=220, delivered=125))
t6 = funnel._decorate(row(sent=259, accepted=43, delivered=39))
R.check("t1 wins on acceptance", 220 > 43)
R.check("t6 wins on delivery-of-accepted", t6["delivery_rate"] > t1["delivery_rate"],
        detail="90.7%% against 56.8%% -- reading this alone ranks them backwards")
R.check("but t1 actually reaches more people, and end-to-end says so",
        t1["end_to_end"] > t6["end_to_end"],
        detail="48.3%% against 15.1%%")

# --- unknown is never folded --------------------------------------------------
empty = funnel._decorate(row(sent=0, accepted=0, delivered=0, read_=0, unknown=0,
                             rejected=0, people=0, people_reached=0))
for k in ("delivery_rate", "read_rate", "unknown_rate", "reach_rate", "end_to_end"):
    R.check("a quiet week reports %s as nothing, not as zero" % k,
            empty[k] is None)

R.eq("unknown is reported against accepted", funnel._decorate(
    row(accepted=100, unknown=19))["unknown_rate"], 19.0)
R.check("unknown has its own key and is not merged into delivered",
        "unknown_rate" in d and d["delivered"] != d["delivered"] + d["unknown"])

# --- the vocabulary is written down, and says what it means -------------------
R.check("every handed-off outcome has a plain-English meaning",
        all(o in funnel.OUTCOME_MEANINGS for o in config.HANDED_OFF_OUTCOMES),
        detail="missing: %s" % [o for o in config.HANDED_OFF_OUTCOMES
                                if o not in funnel.OUTCOME_MEANINGS])
R.check("`escalated` is described as a failure, not a success",
        "NOT a success" in funnel.OUTCOME_MEANINGS["escalated"],
        detail="the word flatters itself; 12 of 17 outcomes were escalations "
               "and nobody had noticed")
R.check("the two goals are named as goals",
        funnel.OUTCOME_MEANINGS["qualified"].startswith("GOAL")
        and funnel.OUTCOME_MEANINGS["visit_booked"].startswith("GOAL"))

# --- bookkeeping rows are excluded, and stay in step with the sender ----------
# The 2026-09-04 outage was a bookkeeping row counted as a send. The 2026-08-22
# one was the same bug, and the fix named one word instead of making a list --
# which is why the second was free to happen. Two lists now exist; they must agree.
R.check("funnel excludes everything wati calls not-a-send",
        set(wati.NOT_A_SEND) <= set(funnel.NOT_A_MESSAGE),
        detail="wati has %s; funnel has %s"
               % (sorted(wati.NOT_A_SEND), sorted(funnel.NOT_A_MESSAGE)))
R.check("`knock_gave_up` is excluded", "knock_gave_up" in funnel.NOT_A_MESSAGE)
R.check("`matched` is excluded", "matched" in funnel.NOT_A_MESSAGE)

# --- the escaping trap, guarded ----------------------------------------------
# Three times in one afternoon a query died on `IndexError: tuple index out of
# range` because Python's %-formatting and psycopg2's placeholders were fighting
# over the same character. Each cost a round trip to a live database to find.
SQL_ATTRS = ("_STATES", "_CALLBACKS", "_FROM", "TEMPLATE_LANES")
for name in SQL_ATTRS:
    sql = getattr(funnel, name)
    # A literal % for psycopg2 must be doubled. Strip the legal forms, then any
    # bare % left over is the bug.
    stripped = sql.replace("%%", "").replace("%s", "")
    R.check("%s has no unescaped %% left for psycopg2 to choke on" % name,
            "%" not in stripped,
            detail="found %r" % (re.findall(r".{0,18}%.{0,18}", stripped)[:2],))

src = open(os.path.join(os.path.dirname(__file__), "..", "funnel.py"),
           encoding="utf-8").read()
R.check("no SQL string is assembled with %-formatting",
        '""" % ' not in src and "''' % " not in src,
        detail="use .format() -- then a doubled percent means exactly one thing")

# --- totals cannot disagree with the table -----------------------------------
# Two queries for one number is how this codebase ended up with three answers for
# one lane. report() must SUM the lane rows, never re-query them.
report_src = src[src.index("def report("):]
R.check("report() sums the lane rows rather than querying again",
        "sum(" in report_src and report_src.count("by_lane(") == 1,
        detail="the headline and the table must be the same arithmetic")

if __name__ == "__main__":
    sys.exit(0 if R.report("FUNNEL COUNTING") else 1)
