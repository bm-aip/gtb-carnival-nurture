"""ONE GATE. Every lane that messages a buyer asks the same question. ~1 second.

    python tests/one_gate.py

THE DEFECT BEHIND FOUR OUTAGES. Two different pieces of code decided whether a
person could be messaged: the door (`sendgate`), which knew all the rules, and each
picker, which kept a partial hand-written copy of some of them. The picker chooses
somebody the door will refuse, the door refuses, the refusal writes a `blocked:`
row -- and a `blocked:` row is not an attempt, so no clock moves and the same
person, being the oldest, is chosen again on the very next tick. Forever.

    #76        an 11-hour t2 outage
    #77/#78    35,156 phantom rows -- the fatigue cap, not modelled by the picker
    #80       135,496 phantom rows, 23 leads, nine days, 309 buyers dark
    #81        the re-opener starved 90 hours, having inherited none of the above

Each was repaired inside one lane. A repair that lives in one lane is a habit, not
a rule, so the next lane rebuilt it. These tests pin the rule itself:

  1. the predicate exists, is pure, and is what the door itself asks
  2. EVERY lane sequencer.tick() runs either asks it before choosing, or declares
     in the module -- and proves in its code -- that it never messages a buyer
  3. no lane keeps a private copy of a rule the gate already owns

Point 2 is enumerated from the PARSE TREE of sequencer.tick(), never from a list
written here. A test with its own copy of the lane list is one more thing to
remember to update, and forgetting is the entire bug. Lane four is covered on the
day it is written, by a test nobody has to touch.
"""
import ast
import io
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

logging.getLogger("knocks").setLevel(logging.CRITICAL)
logging.getLogger("reopener").setLevel(logging.CRITICAL)
logging.getLogger("handraiser").setLevel(logging.CRITICAL)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _bootstrap import Results        # noqa: E402

import knocks                          # noqa: E402
import sendgate                        # noqa: E402
import watchdog                        # noqa: E402

R = Results()
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def source(name):
    return io.open(os.path.join(ROOT, name), encoding="utf-8").read()


def _fn(tree, name):
    return next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)


def calls_in(src, fn_name):
    """Every call target inside one function, read from the PARSE TREE.

    NOT a substring search. On 2026-09-03 a source assertion passed on a COMMENT
    explaining why a call must never be made -- the exact opposite of what it
    claimed to prove. Comments and docstrings cannot reach an AST. Nested
    functions count: a lane's rules live in the `select` closure inside `due()`.
    """
    fn = _fn(ast.parse(src), fn_name)
    if fn is None:
        return set()
    return {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}


def reaches(src, fn_name, target, depth=4):
    """Does `fn_name` call `target`, directly or through this module's own helpers?

    The lanes do not agree on where their rules live -- knocks has `_verdict`,
    the re-opener has a `select` closure -- and forcing them to would be a naming
    convention, which is a rule nobody can enforce. Following the call graph asks
    the question that actually matters: on the way from "who is due" to "send it",
    is the gate consulted at all?
    """
    tree = ast.parse(src)
    local = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    seen, frontier = set(), [fn_name]
    for _ in range(depth):
        nxt = []
        for name in frontier:
            if name in seen:
                continue
            seen.add(name)
            calls = calls_in(src, name)
            if target in calls:
                return True
            nxt += [c for c in calls if c in local]
        frontier = nxt
    return False


# --------------------------------------------------------------------------
# 1. The predicate itself
# --------------------------------------------------------------------------
gsrc = source("sendgate.py")

R.check("the gate offers a predicate a picker may ask before choosing",
        _fn(ast.parse(gsrc), "would_allow") is not None)

# PURE, OR IT CANNOT BE ASKED ABOUT HUNDREDS OF PEOPLE. A picker calls this once
# per candidate and the watchdog calls reopener.due() on a timer; a write in here
# would mean a monitor changes the thing it is measuring.
WRITES = {"db.x", "db.set_setting", "db.log_msg", "log_msg",
          "fatigue.start_journey"}
R.check("and asking it writes nothing",
        not (calls_in(gsrc, "would_allow") & WRITES),
        detail="a monitor must not change what it measures")

# THE DOOR ASKS THE PREDICATE. If check() ever grew its own copy of a rule, the
# pickers would be back to guessing -- with the difference well hidden, because
# both names would still exist and both would look right.
R.check("the door asks the same predicate rather than repeating it",
        "would_allow" in calls_in(gsrc, "check"))

for rule in ("optout.is_blocked", "fatigue.check", "failures.check",
             "sends_enabled", "paused"):
    R.check("the predicate still knows about %s" % rule,
            rule in calls_in(gsrc, "would_allow"))


# --------------------------------------------------------------------------
# 2. Every lane, enumerated from sequencer.tick() itself
# --------------------------------------------------------------------------
def lanes_from_tick():
    """[(watchdog key, module name)] for every lane tick() actually runs.

    Read from `_run_lane("knock", knocks.run)` calls in the parse tree, which is
    the only place that cannot lie: a lane is in this list because it RUNS, not
    because somebody remembered to register it.
    """
    fn = _fn(ast.parse(source("sequencer.py")), "tick")
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or ast.unparse(node.func) != "_run_lane":
            continue
        key = node.args[0].value
        out.append((key, ast.unparse(node.args[1]).split(".")[0]))
    return out


LANES = lanes_from_tick()
R.check("the lanes are discovered, not listed here", len(LANES) >= 3,
        detail=str(LANES))

# EVERY LANE tick() RUNS IS ALSO WATCHED. Same reasoning, different failure: the
# hand-raiser lane came within one commit of being unwatched, and an unwatched
# lane is how the knock engine spent a fortnight silent.
watched = {k for k, _label in watchdog.LANES}
for key, module in LANES:
    R.check("the watchdog knows about the %s lane" % key, key in watched)

for key, module in LANES:
    src = source(module + ".py")
    mod = __import__(module)
    flag = getattr(mod, "SENDS_TO_BUYER", None)
    R.check("the %s lane says whether it messages buyers" % key,
            flag is not None,
            detail="set SENDS_TO_BUYER; tests/one_gate.py is asking")

    if flag:
        # THE RULE. Between "who is due" and the wire, the gate is asked.
        R.check("the %s lane asks the gate before choosing" % key,
                reaches(src, "due", "sendgate.would_allow"),
                detail="a picker that does not ask chooses people the door "
                       "refuses, and re-chooses them forever")
        # AND KEEPS NO PRIVATE COPY. Both of these were added to a picker only
        # after the missing one had cost an outage; either on its own is the
        # partial copy that drifts.
        for rule in ("fatigue.check", "failures.check"):
            R.check("the %s lane no longer keeps its own %s" % (key, rule),
                    not reaches(src, "due", rule),
                    detail="the gate owns this rule now")
    else:
        # A LANE CANNOT DECLARE ITS WAY OUT. `SENDS_TO_BUYER = False` is only
        # honest if the module really never sends to the person it selected --
        # the hand-raiser's card goes to STAFF_PHONES, and its own send passes
        # the door on the salesperson's number.
        #
        # READ FROM THE PARSE TREE, and the first draft of this assertion is why:
        # written as a substring search it failed on a COMMENT of mine that named
        # `sequencer._send` while explaining that this lane does not call it. That
        # is the 2026-09-03 trap in miniature -- a source assertion answering a
        # question about prose. Comments cannot reach an AST.
        calls = {ast.unparse(n.func) for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call)}
        R.check("the %s lane really does not send to the buyer" % key,
                not ({"sequencer._send"} & calls)
                and not {c for c in calls if c.startswith("wati.send")},
                detail="it declares SENDS_TO_BUYER = False")
        R.check("and what it does send goes to staff, through the door",
                "handoff._notify" in calls)


# --------------------------------------------------------------------------
# 3. The knock picker, end to end, without a database
# --------------------------------------------------------------------------
NOW = datetime.now(timezone.utc)
_REAL = (knocks.sendgate, knocks.knock_state, knocks.attempt_state,
         knocks.fatigue.check, knocks.failures.check)


def verdict_with(gate):
    """One _verdict() call on a lead that is due in every other respect.

    fatigue.check and failures.check are stubbed too, though _verdict must no
    longer call either. That is deliberate: WITHOUT the stubs, a lane that went
    back to its private copy would reach the real ones, hit a database this test
    does not have, and take the whole run down with a traceback -- so the three
    assertions above that exist to catch exactly that regression would never
    print. A guard test must FAIL, not crash. (Proved by reverting the fix: the
    run died before reporting anything.)
    """
    lead = {"id": 1, "phone": "919000000001", "project": "RON",
            "anchor": NOW - timedelta(days=30), "name": "Test"}
    knocks.sendgate = type("G", (), {"would_allow": staticmethod(gate)})()
    knocks.knock_state = lambda phone: (0, None)
    knocks.attempt_state = lambda phone, step_key: (0, None)
    knocks.fatigue.check = lambda phone, msg_type, project=None: (True, None)
    knocks.failures.check = lambda phone, msg_type=None, project=None: (True, None)
    try:
        return knocks._verdict(lead, NOW, set())
    finally:
        (knocks.sendgate, knocks.knock_state, knocks.attempt_state,
         knocks.fatigue.check, knocks.failures.check) = _REAL


_idx, _key, why = verdict_with(lambda p, m, project=None: (True, "ok"))
R.eq("a lead the gate allows is sendable", why, None)

_idx, _key, why = verdict_with(
    lambda p, m, project=None: (False, sendgate.BLOCKED_PAUSED))
R.check("a paused system stops the picker, not just the door", why is not None)
R.check("and the reason is in the owner's words, not a code",
        why == "sending is paused", detail=repr(why))

_idx, _key, why = verdict_with(
    lambda p, m, project=None: (False, sendgate.BLOCKED_OPTOUT_GLOBAL))
R.eq("somebody who asked us to stop is never chosen again",
     why, "they asked us to stop")

# A REASON THE MAP HAS NEVER HEARD OF MUST STILL PRINT. The watchdog groups these
# strings into "Others waiting: 3 waiting on the weekly cap". A new gate rule
# whose code fell through to None would make a whole population invisible on the
# one line that says why nothing is going out.
_idx, _key, why = verdict_with(lambda p, m, project=None: (False, "some_new_rule"))
R.check("a reason nobody has mapped yet is still printed, not swallowed",
        why == "waiting on some_new_rule", detail=repr(why))

# NOT A GIVE-UP. `_give_up()` ends somebody's journey for good, and due() fires it
# on the reason "ceiling" alone. A gate refusal is a WAIT -- the pause lifts, the
# week rolls over -- so it must never be spelled in a way that ends a journey.
gate_reasons = {r for r in knocks._GATE_REASON.values()}
R.check("no gate refusal can be mistaken for the end of a journey",
        "ceiling" not in gate_reasons)

if __name__ == "__main__":
    sys.exit(0 if R.report("ONE GATE") else 1)
