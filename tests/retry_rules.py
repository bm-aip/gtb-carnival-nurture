"""jobs.retry_plan: a busy provider must outlive its own outage.

2026-08-18, two buyers got no reply at all. Anthropic returned
`Error code: 529 - overloaded_error`, the transient rule SHORTENED the wait each
time (5s, 10s, 15s, 20s), so all five attempts burned inside about a minute -- the
same minute the provider was saturated -- and the job was marked failed for good.
Nothing re-runs a dead job, so neither buyer was ever answered.
"""
import sys

import _bootstrap  # noqa: F401
import config
import jobs

R = _bootstrap.Results()

REAL_529 = ("Error code: 529 - {'type': 'error', 'error': {'type': "
            "'overloaded_error', 'message': 'Overloaded'}, 'request_id': "
            "'req_011CeAYo1iVExHL5tgQ2FP7c'}")


def plan(attempts, err, max_attempts=5):
    return jobs.retry_plan(attempts, err, max_attempts)


# --- the exact error from that night ------------------------------------------
R.check("the real 529 string is recognised as transient",
        not plan(1, REAL_529)[0])

# THE REGRESSION. Attempt 5 used to be the end of the road; the whole ladder fitted
# inside a minute. It must now still be retrying.
R.check("529 is still retrying at attempt 5", not plan(5, REAL_529)[0])
R.check("529 is still retrying at attempt 7", not plan(7, REAL_529)[0])
R.check("529 does eventually give up", plan(99, REAL_529)[0])

# Fast where a buyer is watching, patient once it is clearly an outage. The
# 2026-08-02 lead who typed "You there ?" is why the first rungs stay short.
LADDER = [plan(i, REAL_529)[1] for i in range(1, 5)]
R.check(f"the first four waits stay short (got {LADDER})", sum(LADDER) <= 200)
R.check("...and the first one is seconds, not half a minute", LADDER[0] <= 10)

TAIL = plan(config.JOB_MAX_ATTEMPTS_TRANSIENT - 1, REAL_529)[1]
R.check(f"the tail stretches to minutes (got {TAIL}s)", TAIL >= 300)

TOTAL = sum(config.JOB_BACKOFF_TRANSIENT)
R.check(f"the ladder spans a real outage, not a hiccup ({TOTAL}s)", TOTAL >= 900)
# A reply is only allowed inside WhatsApp's 24h window. Retrying past it would keep
# a job alive that can no longer produce a message.
R.check("...and stays well inside the 24h window", TOTAL < 6 * 3600)

# --- other transient shapes ---------------------------------------------------
for err in ("rate_limit_error 429", "Request timed out", "503 Service Unavailable",
            "connection reset by peer", "APIStatusError: 502"):
    R.check(f"transient: {err[:28]!r}", not plan(5, err)[0])

# --- a real fault must NOT get the patient ladder -----------------------------
# A bug that throws every single time would otherwise be retried for half an hour,
# holding the phone's slot in the queue while the buyer waits on nothing.
BUG = "KeyError: 'checklist'"
R.check("a code bug still gives up at max_attempts", plan(5, BUG)[0])
R.check("...and is retried slowly, not fast", plan(1, BUG)[1] >= 30)
R.check("a code bug retries below the ceiling", not plan(2, BUG)[0])

# --- the ceiling is raised, never lowered -------------------------------------
R.check("a row asking for MORE attempts keeps them",
        not plan(20, REAL_529, max_attempts=50)[0])

if __name__ == "__main__":
    sys.exit(0 if R.report("RETRY RULES") else 1)
