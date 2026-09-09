"""A buyer whose message died on an outage must hear from us. No database, ~1s.

    python tests/holding_note.py

2026-09-09. The Anthropic key hit a zero credit balance, five retries burned in
eight minutes, and two people got NOTHING -- one of them mid-conversation, having
just tapped "Need More Details" on one of our own templates. They were recovered
only because a human read an alert card six hours later.

THE TWO HALVES THIS PINS, because getting either backwards is worse than the bug:

  * a PROVIDER outage ends by itself, so telling the buyer to wait is honest and
    an automatic recovery is safe.
  * OUR OWN bug does not end by itself. It must not produce a holding note that
    promises a reply nobody is coming to give, and (pass 2) must never be
    auto-replayed -- `jobs.replay()` is manual precisely because re-running a real
    fault is an infinite loop dressed as a feature.

And one rule that outranks both: NOTHING HERE MAY NEED THE MODEL. The message the
bot sends when the model is unreachable cannot be written by the model.
"""
import ast
import io
import logging
import os
import sys

logging.getLogger("resume").setLevel(logging.CRITICAL)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _bootstrap import Results        # noqa: E402

import jobs                            # noqa: E402
import resume                          # noqa: E402
import wati                            # noqa: E402

R = Results()
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def source(name):
    return io.open(os.path.join(ROOT, name), encoding="utf-8").read()


# --------------------------------------------------------------------------
# 1. Whose problem is it?
# --------------------------------------------------------------------------
CREDIT = ("Error code: 400 - {'type': 'error', 'error': {'type': "
          "'invalid_request_error', 'message': 'Your credit balance is too low to "
          "access the Anthropic API. Please go to Plans & Billing to upgrade or "
          "purchase credits.'}}")

for name, err in (
        ("the exact 2026-09-09 credit error", CREDIT),
        ("529 overloaded", "Error code: 529 - overloaded_error"),
        ("a rate limit", "Error code: 429 - rate_limit_error"),
        ("a revoked key", "Error code: 401 - authentication_error: invalid x-api-key"),
        ("a read timeout", "HTTPSConnectionPool: Read timed out."),
        ("a bad gateway", "502 Bad Gateway")):
    R.check("%s is an outage" % name, resume.is_outage(err))

for name, err in (
        ("a typo in our code", "KeyError: 'checklist'"),
        ("a bad payload", "TypeError: expected str, got None"),
        ("a template we got wrong",
         "Check your template, it cannot have typos or blank text"),
        ("nothing at all", "")):
    R.check("%s is NOT an outage" % name, not resume.is_outage(err),
            detail="our bug does not end on its own; a note would promise a reply "
                   "nobody is coming to give")

# THE ONE THAT COST TWO BUYERS. `credit balance` is a 400 -- an
# "invalid_request_error" -- so the retry ladder reads it as a permanent, our-fault
# error and burns five attempts in eight minutes. That is not wrong of the ladder,
# and this module does not change it; it changes what the buyer is told afterwards.
R.check("the credit error still does NOT look transient to the retry ladder",
        not jobs._TRANSIENT.search(CREDIT),
        detail="if this ever flips, the fast ladder is no longer the reason the "
               "job died and this test's premise needs rereading")
R.check("but it IS an outage to us", resume.is_outage(CREDIT))


# --------------------------------------------------------------------------
# 2. The note itself
# --------------------------------------------------------------------------
# WRITTEN BY CODE, NOT BY THE MODEL. Read from the parse tree.
#
# MODULE SCOPE IS THE THING THAT MATTERS, not the whole file. `probe()` genuinely
# has to call the provider, so it imports `anthropic` (and `qualifier`, for the
# model name -- a second copy of that string would drift). Those imports sit INSIDE
# the function on purpose: if the SDK itself were the broken thing, a module-scope
# import would fail at import time and take the holding note down with it -- the
# one message that has to survive an outage.
#
# So: nothing heavy at the top, and nothing at all from the answer-writing path.
mod = ast.parse(source("resume.py"))
top = {n.names[0].name for n in mod.body if isinstance(n, ast.Import)}
top |= {n.module for n in mod.body if isinstance(n, ast.ImportFrom)}
for forbidden in ("anthropic", "answering", "qualifier", "kb", "embed"):
    R.check("resume.py does not import %s at module scope" % forbidden,
            forbidden not in top,
            detail="the message sent when the model is unreachable cannot need it")

anywhere = {n.names[0].name for n in ast.walk(mod) if isinstance(n, ast.Import)}
anywhere |= {n.module for n in ast.walk(mod) if isinstance(n, ast.ImportFrom)}
for forbidden in ("answering", "kb", "embed"):
    R.check("and never depends on %s at all" % forbidden, forbidden not in anywhere,
            detail="the answer-writing path is exactly what has failed")

hold_fn = next(n for n in ast.walk(mod)
               if isinstance(n, ast.FunctionDef) and n.name == "hold")
R.check("hold() imports nothing at all",
        not [n for n in ast.walk(hold_fn) if isinstance(n, (ast.Import, ast.ImportFrom))],
        detail="it runs when things are already broken")

R.eq("the buyer reads exactly what the owner asked for",
     resume.HOLDING_TEXT, "Got your message. We'll be back to you shortly.")
R.check("and it does not mention a technical problem",
        not any(w in resume.HOLDING_TEXT.lower()
                for w in ("technical", "error", "issue", "api", "system", "down")),
        detail="owner 2026-09-09: 'dont need to say technical issue or something'")
R.check("it is one short line",
        len(resume.HOLDING_TEXT) < 90 and "\n" not in resume.HOLDING_TEXT)

# NOT A PROACTIVE SEND. This is a reply inside the window the buyer opened seconds
# ago. If its type ever reads as business-initiated it would be held until morning
# by quiet hours, consume the daily messaging tier, and eat this person's fatigue
# allowance -- three ways to make a buyer's silence worse.
R.check("the note is not business-initiated",
        not wati.is_business_initiated(resume.MSG_TYPE),
        detail="quiet hours and the daily cap must not hold back a reply")


# --------------------------------------------------------------------------
# 3. Who gets one, and when
# --------------------------------------------------------------------------
class Fake:
    """Stands in for db, sequencer and the world. Records what was sent."""

    def __init__(self, told=False, lead=True):
        self.told, self.lead, self.sent, self.settings = told, lead, [], {}

    # db
    def q(self, sql, params=None, one=False):
        if "message_log" in sql:
            return {"?column?": 1} if self.told else None
        if "FROM leads" in sql:
            return ({"id": 7, "phone": params[0], "name": "Meshak", "project": "RON"}
                    if self.lead else None)
        return None if one else []

    def get_setting(self, k, default=None):
        return self.settings.get(k, default)

    def set_setting(self, k, v):
        self.settings[k] = str(v)

    # sequencer
    def _send(self, lead, msg_type, body=None, **kw):
        self.sent.append((lead["phone"], msg_type, body))
        return True

    def now_ist(self):
        import datetime
        return datetime.datetime(2026, 9, 9, 8, 58)


_REAL = (resume.db, resume.sequencer)


def hold_with(job, err, fake):
    resume.db, resume.sequencer = fake, fake
    try:
        return resume.hold(job, err)
    finally:
        resume.db, resume.sequencer = _REAL


INBOUND = {"id": 1358, "kind": "inbound_message", "phone": "918122334406"}

f = Fake()
R.check("the buyer whose message died on an outage is told", hold_with(INBOUND, CREDIT, f))
R.eq("and told exactly once, in their own chat",
     f.sent, [("918122334406", resume.MSG_TYPE, resume.HOLDING_TEXT)])

f = Fake()
R.check("a buyer whose message died on OUR bug is not told",
        not hold_with(INBOUND, "KeyError: 'checklist'", f),
        detail="we would be promising a reply that is not coming")
R.eq("and nothing is sent to them", f.sent, [])

f = Fake(told=True)
R.check("somebody already told is not told twice",
        not hold_with(INBOUND, CREDIT, f),
        detail="three identical 'we'll be back' lines is a new kind of broken")

f = Fake(lead=False)
R.check("a phone with no lead row is skipped rather than crashing",
        not hold_with(INBOUND, CREDIT, f))

f = Fake()
R.check("a click-id fetch gets no holding note",
        not hold_with({"id": 9, "kind": "meta_click", "phone": "918122334406"},
                      CREDIT, f),
        detail="nobody is sitting in a chat window waiting on one")

# THE OUTAGE CLOCK. Pass 2 probes from this moment and gives up 12 hours later, so
# it must be stamped by the FIRST buyer we fail -- and never moved by the next one,
# or a steady trickle of failures would push the deadline forever.
f = Fake()
hold_with(INBOUND, CREDIT, f)
first = f.settings.get("outage_since")
R.check("the outage clock starts at the first buyer we fail", bool(first))
f.told = True
hold_with({"id": 1359, "kind": "inbound_message", "phone": "919000000001"}, CREDIT, f)
R.eq("and a later failure does not push it forward", f.settings.get("outage_since"), first)


# --------------------------------------------------------------------------
# 4. It is actually wired to the place jobs die
# --------------------------------------------------------------------------
wsrc = source("worker.py")
tree = ast.parse(wsrc)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "run_once")
calls = {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
R.check("the worker calls resume.hold when a job dies", "resume.hold" in calls,
        detail="a module nothing calls is the commonest defect in this codebase")

# ONLY AT GIVE-UP. Called on every failure it would message a buyer 30 seconds into
# a hiccup that is about to resolve itself, and then again on the retry.
guarded = any(isinstance(n, ast.If)
              and "retrying" in ast.unparse(n.test)
              and "resume.hold" in ast.unparse(n)
              for n in ast.walk(fn))
R.check("and only once the retry ladder is spent", guarded,
        detail="a 30-second blip must never produce a holding note")

if __name__ == "__main__":
    sys.exit(0 if R.report("HOLDING NOTE") else 1)
