"""The service comes back by itself, and the buyers waiting get answered. ~1s.

    python tests/resume_step.py

2026-09-09. The provider went down, two buyers got nothing, and they were only
recovered because a human read an alert card six hours later and asked for a
replay by hand. Owner: *"I don't need an alarm but a resume step -- every hour
from the outage first surfaced it should poll the api key, if it serves we should
resume the service"*.

THE LINE THIS WALKS. `jobs.replay()` is manual on purpose: an automatic
resurrection re-runs whatever killed the job, which for a fault in our own code is
an infinite loop dressed as a feature. Probing FIRST is what makes an automatic
recovery safe -- nothing is replayed until the provider has, that second, answered
a real request. So the tests that matter most here are the ones proving we do not
replay on a clock, and do not replay our own bugs at all.
"""
import ast
import io
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

logging.getLogger("resume").setLevel(logging.CRITICAL)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _bootstrap import Results        # noqa: E402

import jobs                            # noqa: E402
import resume                          # noqa: E402

R = Results()
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CREDIT = ("Error code: 400 - {'type': 'error', 'error': {'type': "
          "'invalid_request_error', 'message': 'Your credit balance is too low.'}}")
NOW = datetime.now(timezone.utc)


def source(name):
    return io.open(os.path.join(ROOT, name), encoding="utf-8").read()


class Fake:
    """db + sequencer + jobs + the provider, all stubbed. Records what happened."""

    def __init__(self, rows=None, settings=None, provider_up=False):
        self.rows = rows if rows is not None else []
        self.settings = dict(settings or {})
        self.provider_up = provider_up
        self.replayed, self.marked, self.cards, self.probes = [], [], [], 0

    # --- db
    def q(self, sql, params=None, one=False):
        if "job_queue" in sql:
            page, offset = params[-2], params[-1]
            return self.rows[offset:offset + page]
        return None if one else []

    def x(self, sql, params=None):
        self.marked.append(params[0])

    def get_setting(self, k, default=None):
        return self.settings.get(k, default)

    def set_setting(self, k, v):
        self.settings[k] = str(v)

    # --- sequencer
    def now_ist(self):
        return NOW

    # --- jobs
    def replay(self, job_id=None):
        self.replayed.append(job_id)
        return [{"id": job_id}]


def row(i, err=CREDIT, phone=None, age_h=1):
    return {"id": i, "kind": "inbound_message", "phone": phone or "9190000%05d" % i,
            "last_error": err, "created_at": NOW - timedelta(hours=age_h)}


_REAL = (resume.db, resume.sequencer, resume.probe, resume._alert, resume.jobs.replay,
         resume.sendgate)


def run_with(fake, gate=None):
    """One resume.run() against a stubbed world."""
    resume.db = fake
    resume.sequencer = fake
    resume.probe = lambda: (fake.provider_up, "stub")
    resume._alert = lambda h, d, a: fake.cards.append(h)
    resume.jobs = type("J", (), {"FAILED": jobs.FAILED, "KIND_INBOUND": jobs.KIND_INBOUND,
                                 "REPLAY_MAX_AGE_HOURS": jobs.REPLAY_MAX_AGE_HOURS,
                                 "replay": staticmethod(fake.replay)})()
    resume.sendgate = type("G", (), {"would_allow": staticmethod(
        gate or (lambda p, m, project=None: (True, "ok")))})()
    try:
        return resume.run()
    finally:
        (resume.db, resume.sequencer, resume.probe, resume._alert, _r,
         resume.sendgate) = _REAL
        resume.jobs = jobs


def _probe_counting(fake):
    def p():
        fake.probes += 1
        return (fake.provider_up, "stub")
    return p


OPEN = {"outage_since": (NOW - timedelta(hours=1)).isoformat()}

# --------------------------------------------------------------------------
# Nothing broken: this costs one read and asks the provider nothing
# --------------------------------------------------------------------------
f = Fake(rows=[row(1)], settings={})
R.eq("a healthy system replays nobody", run_with(f), 0)
R.eq("and does not probe at all", f.replayed, [])

# --------------------------------------------------------------------------
# Down: we wait, we do NOT replay on a clock
# --------------------------------------------------------------------------
f = Fake(rows=[row(1), row(2)], settings=dict(OPEN), provider_up=False)
R.eq("while the provider is still down, nobody is replayed", run_with(f), 0)
R.eq("really nobody", f.replayed, [])
R.check("and the outage stays open", bool(f.settings.get("outage_since")))

# --------------------------------------------------------------------------
# It answers: everyone waiting is put back, marked for an apology
# --------------------------------------------------------------------------
f = Fake(rows=[row(1), row(2)], settings=dict(OPEN), provider_up=True)
R.eq("when the provider answers, both waiting buyers are replayed", run_with(f), 2)
R.eq("each one exactly once", sorted(f.replayed), [1, 2])
R.eq("and each is marked so their reply apologises", sorted(f.marked), [1, 2])
R.eq("the outage is closed", f.settings.get("outage_since"), "")
R.check("and one card says the service resumed",
        any("RESUMED" in c for c in f.cards), detail=str(f.cards))

# --------------------------------------------------------------------------
# OUR OWN BUG IS NEVER REPLAYED. The whole reason jobs.replay() was manual.
# --------------------------------------------------------------------------
f = Fake(rows=[row(1, err="KeyError: 'checklist'")], settings=dict(OPEN),
         provider_up=True)
R.eq("a job that died on our bug is not replayed, even when the provider is fine",
     run_with(f), 0)
R.eq("nothing was requeued", f.replayed, [])

# A cohort of both: the outage victims go back, our bug stays put.
f = Fake(rows=[row(1, err="KeyError: 'x'"), row(2), row(3, err="TypeError: no")],
         settings=dict(OPEN), provider_up=True)
R.eq("in a mixed queue only the outage victims are replayed", run_with(f), 1)
R.eq("and it is the right one", f.replayed, [2])

# --------------------------------------------------------------------------
# The send gate has the last word, exactly as in every other lane
# --------------------------------------------------------------------------
blocked = "919000000002"
f = Fake(rows=[row(1, phone="919000000001"), row(2, phone=blocked)],
         settings=dict(OPEN), provider_up=True)
run_with(f, gate=lambda p, m, project=None: (
    (False, "opted_out_global") if p == blocked else (True, "ok")))
R.eq("somebody who opted out during the outage is not answered by a queue",
     f.replayed, [1])

# One human who wrote three times gets one answer, not three.
f = Fake(rows=[row(1, phone="919111111111"), row(2, phone="919111111111"),
               row(3, phone="919111111111")], settings=dict(OPEN), provider_up=True)
R.eq("three messages from one person are one answer", run_with(f), 1)
R.eq("and it is their first question", f.replayed, [1])

# --------------------------------------------------------------------------
# The 12-hour deadline
# --------------------------------------------------------------------------
OLD = {"outage_since": (NOW - timedelta(hours=13)).isoformat()}
f = Fake(rows=[row(1, age_h=13)], settings=dict(OLD), provider_up=True)
R.eq("past 12 hours the bot stops trying and hands it to a person", run_with(f), 0)
R.check("with a card that says so", any("STILL DOWN" in c for c in f.cards),
        detail=str(f.cards))
R.eq("and the card is sent once, not every pass", run_with(f), 0)
R.eq("still one card", len([c for c in f.cards if "STILL DOWN" in c]), 1)

# --------------------------------------------------------------------------
# Hourly, not every minute -- the lane runs on the per-minute tick
# --------------------------------------------------------------------------
f = Fake(rows=[row(1)], settings=dict(OPEN), provider_up=True)
f.settings["outage_last_probe_at"] = (NOW - timedelta(minutes=5)).isoformat()
R.eq("a probe five minutes ago is not repeated", run_with(f), 0)
f.settings["outage_last_probe_at"] = (NOW - timedelta(minutes=61)).isoformat()
R.eq("an hour later it asks again, and recovers", run_with(f), 1)

# A stamp we cannot read must not hold the recovery shut forever.
f = Fake(rows=[row(1)], settings=dict(OPEN), provider_up=True)
f.settings["outage_last_probe_at"] = "not a timestamp"
R.eq("a corrupt probe stamp fails towards trying, not towards silence",
     run_with(f), 1)

# --------------------------------------------------------------------------
# Everyone aged out: close the outage rather than probing forever
# --------------------------------------------------------------------------
f = Fake(rows=[], settings=dict(OPEN), provider_up=False)
R.eq("with nobody left waiting, nothing happens", run_with(f), 0)
R.eq("and the outage is closed rather than probed forever",
     f.settings.get("outage_since"), "")

# --------------------------------------------------------------------------
# The apology, and only where it belongs
# --------------------------------------------------------------------------
R.eq("a replayed answer opens with an apology",
     resume.with_apology("The villas are 3 and 4 bed.", {"delayed": True}),
     "Sorry for the delay! The villas are 3 and 4 bed.")
R.eq("an ordinary answer is untouched",
     resume.with_apology("The villas are 3 and 4 bed.", {}),
     "The villas are 3 and 4 bed.")
R.eq("and a missing payload is not a crash",
     resume.with_apology("Hello", None), "Hello")

# --------------------------------------------------------------------------
# Wiring: the lane runs, is watched, and asks the gate
# --------------------------------------------------------------------------
ssrc, wsrc = source("sequencer.py"), source("worker.py")
tick = next(n for n in ast.walk(ast.parse(ssrc))
            if isinstance(n, ast.FunctionDef) and n.name == "tick")
lanes = [ast.unparse(c.args[0]) for c in ast.walk(tick)
         if isinstance(c, ast.Call) and ast.unparse(c.func) == "_run_lane"]
R.check("the resume step runs on the scheduler", "'resume'" in lanes,
        detail=str(lanes))

import watchdog                        # noqa: E402
R.check("and the watchdog knows about it",
        "resume" in {k for k, _l in watchdog.LANES},
        detail="an unwatched recovery path is the worst thing to find out late")

rsrc = source("resume.py")
due_fn = next(n for n in ast.walk(ast.parse(rsrc))
              if isinstance(n, ast.FunctionDef) and n.name == "due")
due_calls = {ast.unparse(c.func) for c in ast.walk(due_fn) if isinstance(c, ast.Call)}
R.check("it asks the send gate before choosing anybody",
        "sendgate.would_allow" in due_calls)
R.check("and walks pages like every other lane", "picker.scan" in due_calls)

# THE PROBE MUST NOT BE AN IMPORT-TIME DEPENDENCY. If the SDK itself is the broken
# thing, a module-scope import would take down the holding note too -- the one
# message that has to survive an outage.
top = {n.names[0].name for n in ast.parse(rsrc).body if isinstance(n, ast.Import)}
R.check("anthropic is not imported at module scope", "anthropic" not in top,
        detail=str(sorted(top)))
probe_fn = next(n for n in ast.walk(ast.parse(rsrc))
                if isinstance(n, ast.FunctionDef) and n.name == "probe")
R.check("it is imported inside probe(), where it belongs",
        "anthropic" in {a.name for i in ast.walk(probe_fn)
                        if isinstance(i, ast.Import) for a in i.names})

wfn = next(n for n in ast.walk(ast.parse(wsrc))
           if isinstance(n, ast.FunctionDef) and n.name == "_handle_inbound")
R.check("the reply path applies the apology",
        "resume.with_apology" in {ast.unparse(c.func) for c in ast.walk(wfn)
                                  if isinstance(c, ast.Call)})

if __name__ == "__main__":
    sys.exit(0 if R.report("RESUME STEP") else 1)
