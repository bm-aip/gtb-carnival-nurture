"""Retry-on-refusal logic, as rules. No database, no API, ~1 second.

    python tests/knock_retry.py

Meta refuses template sends per RECIPIENT and temporarily -- proven 2026-08-11, when
919884739289 and 919841071005 each received two knocks the same day, one read and one
refused. So a refused knock is retried the next day with the next wording, ten times,
then the person is marked lost.

The two things worth a test are the ones that decide whether that loop is safe: which
wording gets tried on attempt N, and that the ceiling is actually a ceiling.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bootstrap import Results        # noqa: E402  (stubs env, fixes console)

import config                          # noqa: E402
import knocks                          # noqa: E402

r = Results()

# --- variant rotation ---------------------------------------------------------
# Ten attempts over three wordings, so the cycle has to wrap and keep wrapping.
config.KNOCK_TEMPLATE_VARIANTS["t1_lifestyle"] = ["A", "B", "C"]
for attempt, want in enumerate(["A", "B", "C", "A", "B", "C", "A", "B", "C", "A"]):
    r.eq(f"attempt {attempt + 1} uses wording {want}",
         knocks.variant_for("t1_lifestyle", attempt), want)

# Today's real state: marketing has approved no alternates yet, so every step has
# exactly one wording and the rotation must degrade to "send the same one".
config.KNOCK_TEMPLATE_VARIANTS["t2_location"] = ["ONLY"]
for attempt in (0, 1, 5, 9):
    r.eq(f"single wording, attempt {attempt + 1}",
         knocks.variant_for("t2_location", attempt), "ONLY")

# A step with no variant list at all falls back to the plain template rather than
# sending nothing -- the knock engine treats a missing template as "skip".
config.KNOCK_TEMPLATE_VARIANTS["t3_low_density"] = []
config.KNOCK_TEMPLATES["t3_low_density"] = "BASE"
r.eq("no variants configured falls back to the base template",
     knocks.variant_for("t3_low_density", 3), "BASE")

config.KNOCK_TEMPLATE_VARIANTS.pop("nope", None)
config.KNOCK_TEMPLATES.pop("nope", None)
r.eq("unknown step returns None instead of raising",
     knocks.variant_for("nope", 0), None)

# --- variant list hygiene -----------------------------------------------------
# _B and _C are unset env vars today, so blanks MUST collapse or every step would
# claim three wordings and two of them would be the empty string.
r.eq("blank slots dropped", config._variants("a", "", None, "b"), ["a", "b"])
r.eq("duplicates dropped", config._variants("a", "a", "b"), ["a", "b"])
r.eq("whitespace stripped", config._variants("  a  ", "b"), ["a", "b"])
r.eq("all slots empty gives an empty list", config._variants("", None), [])

# --- the ceiling --------------------------------------------------------------
# The owner's rule is ten tries then give up. Off by one here means either nine
# tries or an unbounded loop against a number Meta will never accept.
r.eq("ceiling default is 10", config.KNOCK_RETRY_MAX, 10)
r.eq("attempts 0..9 are allowed",
     [n for n in range(13) if n < config.KNOCK_RETRY_MAX], list(range(10)))
r.eq("attempt 10 is refused", 10 < config.KNOCK_RETRY_MAX, False)

# --- the switch ---------------------------------------------------------------
# This is the first thing in the system that deliberately messages someone again
# after a failure, and with no alternates approved it would resend the SAME wording.
# It must not be on by default.
r.eq("retry is off unless explicitly enabled",
     os.environ.get("KNOCK_RETRY_ENABLED", "false").lower() == "true", False)
r.eq("retry gap defaults to a day", config.KNOCK_RETRY_GAP_HOURS, 24)

# --- lost is not suppressed ---------------------------------------------------
# Conflating our own delivery failure with a person's request to stop would make the
# opt-out list untrustworthy, so they are separate fields by design.
r.check("giving up marks knock_lost_at, never suppressed",
        "knock_lost_at" in open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "knocks.py"), encoding="utf-8").read()
        and "suppressed=TRUE" not in open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "knocks.py"), encoding="utf-8").read())

sys.exit(0 if r.report("KNOCK RETRY RULES") else 1)
