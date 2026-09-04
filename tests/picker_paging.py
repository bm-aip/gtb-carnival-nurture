"""One paging rule, and the starvation it exists to prevent. No database, ~1 second.

    python tests/picker_paging.py

FOUR TIMES, ONE BUG. A lane narrows candidates in SQL, then applies in Python the
rules SQL cannot model. Take a fixed window of the oldest N and the rejects -- which
are the oldest rows BY DEFINITION, because whatever disqualifies them is why they
have been sitting there -- fill the whole window, and everybody sendable behind them
is invisible. Knocks starved three times (14 days, 11 hours, 9 days). The re-opener
starved on 2026-09-04: 90 hours, 26 people with real context, nothing sent.

The knock lane was repaired in place each time. None of it reached the second lane,
because a fix that lives inside one lane is a habit, not a rule. These tests pin the
rule itself, and then pin that BOTH lanes obey it -- so lane five is a test failure
rather than another week of silence.

THE PROOF THAT MATTERS is `the ghosts no longer hide the 26`. Put the old
`limit * 5` window back and it returns 0 where it must return 25.
"""
import ast
import io
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

logging.getLogger("reopener").setLevel(logging.CRITICAL)
logging.getLogger("knocks").setLevel(logging.CRITICAL)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _bootstrap import Results        # noqa: E402

import picker                          # noqa: E402
import reopener                        # noqa: E402

R = Results()
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def source(name):
    return io.open(os.path.join(ROOT, name), encoding="utf-8").read()


def calls_in(src, fn_name):
    """Every call target inside one function, read from the PARSE TREE.

    NOT a substring search. On 2026-09-03 a source assertion passed on a comment
    that explained why a call must never be made, which is the exact opposite of
    what it claimed to prove. Comments and docstrings cannot reach an AST.
    """
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    return {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}


def multiplies_limit(src, fn_name):
    """True if the function still computes `limit * N` -- the fixed window."""
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    for n in ast.walk(fn):
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mult):
            if "limit" in (ast.unparse(n.left), ast.unparse(n.right)):
                return True
    return False


# --- the walk itself ----------------------------------------------------------
def table(n_rejects, n_good):
    return ["reject"] * n_rejects + ["good"] * n_good


def pager(rows, seen):
    def fetch(page, offset):
        seen.append((page, offset))
        return rows[offset:offset + page]
    return fetch


seen = []
got = picker.scan(pager(table(200, 5), seen),
                  lambda r: r if r == "good" else None, limit=5, page=50)
R.eq("a wall of rejects cannot hide a sendable person", len(got), 5)
R.check("and it kept walking rather than giving up on page one",
        len(seen) > 1, detail=str(len(seen)) + " pages fetched")

seen = []
picker.scan(pager(table(10000, 0), seen), lambda r: None, limit=5, page=50, cap=200)
R.eq("a walk that finds nothing is still bounded", sum(p for p, _ in seen), 200)

seen = []
picker.scan(pager(table(0, 100), seen), lambda r: r, limit=5, page=50)
R.eq("it stops fetching the moment the batch is full", len(seen), 1)

seen = []
R.eq("a short page ends the walk instead of looping",
     len(picker.scan(pager(table(0, 3), seen), lambda r: r, limit=5, page=50)), 3)
R.eq("and it asked once", len(seen), 1)

R.eq("an empty table is an empty batch",
     picker.scan(lambda page, offset: [], lambda r: r, limit=5), [])

# ONE VERDICT PER ROW. The knock lane's select calls _give_up(), which ENDS a
# journey. The walk it replaced ran _verdict over the same rows a second time to
# guess whether it held enough candidates; a walk that decided a row twice would
# now end journeys twice, and one that decided rows past the batch would end them
# for people it never even messaged.
decided = []
picker.scan(pager(table(0, 100), []), lambda r: decided.append(r) or r,
            limit=5, page=50)
R.eq("no row is decided twice, and none past the batch", len(decided), 5)

# --- both lanes obey it -------------------------------------------------------
ksrc, rsrc = source("knocks.py"), source("reopener.py")

R.check("the knock lane walks with the shared pager",
        "picker.scan" in calls_in(ksrc, "due"))
R.check("the re-opener lane walks with the shared pager",
        "picker.scan" in calls_in(rsrc, "due"))
R.check("no lane still takes a fixed multiple of the batch",
        not multiplies_limit(ksrc, "due") and not multiplies_limit(rsrc, "due"),
        detail="limit * 5 was called headroom; it was starvation")
R.check("the knock lane no longer double-checks candidates to guess ahead",
        "probe" not in calls_in(ksrc, "due"))

# --- the ceiling is asked at selection, in BOTH lanes -------------------------
# 2026-09-03: knocks chose people the door would refuse, the refusal logged a
# `blocked:` row, `blocked:` rows are not attempts, so the retry clock never moved
# and the same 23 leads refilled every batch for nine days. The re-opener had 12
# such sends in seven days and no such check. A door alone cannot stop this; only
# not CHOOSING them can.
R.check("the knock lane asks the ceiling before choosing",
        "failures.check" in calls_in(ksrc, "_verdict"))
R.check("the re-opener lane asks the ceiling before choosing",
        "failures.check" in calls_in(rsrc, "due"))

# --- reopener.due stays safe for a monitor to call ----------------------------
# The watchdog calls reopener.due() every 15 minutes and must never call
# knocks.due(), which writes. Adding a check to this lane must not change which
# of those two is true.
R.check("reopener.due() still writes nothing",
        not {c for c in calls_in(rsrc, "due") if c in ("db.x", "db.set_setting")},
        detail="the watchdog probes this lane on a timer")

# --- the lane, end to end, without a database ---------------------------------
NOW = datetime.now(timezone.utc)


def row(i, topic=True, phone=None):
    return {"conv_id": i, "id": i, "phone": phone or "9190000%05d" % i,
            "name": "Test", "project": "RON", "campaign": "RON_Villa_BM",
            "checklist": {"purpose": "investment"} if topic else {},
            "last_turn_at": NOW - timedelta(days=30),
            "delivered": 0, "attempts": 0, "last_try": None}


def always_allow(phone, msg_type, project=None):
    return (True, None)


def run_due(rows, check=always_allow, limit=25):
    real_q, real_check = reopener.db.q, reopener.failures.check

    def fake_q(sql, params=None, one=False):
        page, offset = params[-2], params[-1]
        return rows[offset:offset + page]

    reopener.db.q, reopener.failures.check = fake_q, check
    try:
        return reopener.due(limit=limit)
    finally:
        reopener.db.q, reopener.failures.check = real_q, real_check


# THE 2026-09-04 INCIDENT, EXACTLY. The quietest conversations are ghosts -- an ad
# tap, a bare "Hi", nothing we can name -- so they are both the front of the queue
# and permanently unsendable. 26 people with real context sat behind them.
ghosts = [row(i, topic=False) for i in range(260)]
real = [row(1000 + i) for i in range(26)]
R.eq("90 hours of silence: the ghosts no longer hide the 26",
     len(run_due(ghosts + real)), 25)

R.eq("with no wall in front of them, nothing changes", len(run_due(real)), 25)

# The burst ceiling, at selection time.
blocked_phone = real[0]["phone"]


def one_blocked(phone, msg_type, project=None):
    return (False, "ceiling:burst") if phone == blocked_phone else (True, None)


picked = run_due(ghosts + real, check=one_blocked)
R.check("a send the door will refuse is never chosen",
        blocked_phone not in set(r["phone"] for r, _t, _topic in picked),
        detail="12 re-opens in 7 days were chosen and then blocked, forever")
R.eq("and refusing one person does not cost anybody else their nudge",
     len(picked), 25)

# Phone-keying survives the move into a callback: one human, two lead rows.
twins = [row(1, phone="919111111111"), row(2, phone="919111111111")]
R.eq("one human with two lead rows still gets one re-open", len(run_due(twins)), 1)

if __name__ == "__main__":
    sys.exit(0 if R.report("PICKER PAGING") else 1)
