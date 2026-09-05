"""Speak only when a template rollout is going wrong. Read-only.

    railway run --service gtb-carnival-nurture python scripts/refusal_watch.py

Written 2026-09-05 to watch the t1 hold coming off after eleven days.
Kept because the shape is reusable: run it on a loop during any bulk
send and it stays silent unless something needs a human.

Prints NOTHING on a healthy pass, so every line that appears is worth reading.
Silence is only safe because the failure cases below are enumerated -- a watch
that greps for good news stays quiet through a crash and looks identical to
"still fine".

WHAT COUNTS AS WRONG
  1. Meta refusing MORE than it was. Baseline is the first batch after the hold
     came off: 25 tried, 12 accepted -- 52% refused. A climb means Meta is
     reacting to the volume, which is how a yellow quality rating becomes red.
  2. Nothing going out while the window is open and people are due. That is the
     shape of every silent-lane incident on this project.
  3. Anything raising.
"""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
for _s in ("stdout", "stderr"):
    _f = getattr(sys, _s)
    if hasattr(_f, "buffer"):
        setattr(sys, _s, io.TextIOWrapper(_f.buffer, encoding="utf-8",
                                          errors="replace", line_buffering=True))
if os.environ.get("DATABASE_PUBLIC_URL"):
    os.environ["DATABASE_URL"] = os.environ["DATABASE_PUBLIC_URL"]

import db          # noqa: E402
import knocks      # noqa: E402
import sequencer   # noqa: E402

# The first batch after the hold lifted. A few points of drift is noise; ten is
# a trend worth waking somebody for.
BASELINE_REFUSED = 52.0
ALERT_AT = 65.0
MIN_SAMPLE = 20          # below this, one bad batch swings the percentage wildly


def main():
    now = sequencer.now_ist()
    quiet = sequencer.quiet_now()

    r = db.q("""SELECT count(*) AS tried,
                       count(*) FILTER (WHERE ok) AS accepted
                  FROM message_log
                 WHERE direction='out' AND msg_type LIKE 'knock\\_t1%%'
                   AND ts > now() - interval '90 minutes'
                   AND COALESCE(detail,'') NOT LIKE 'blocked:%%'""",
             one=True) or {}
    tried, accepted = r.get("tried") or 0, r.get("accepted") or 0
    refused_pct = 100.0 * (tried - accepted) / tried if tried else None

    due = knocks.due_count()

    if quiet:
        # Nothing should go out. Say so once, then stay silent: this is the first
        # night the rule has ever been connected, so its first hold is worth one line.
        if tried:
            print("QUIET HOURS but %d t1 sends in the last 90 min -- the rule is "
                  "not holding. %s IST" % (tried, now.strftime("%H:%M")))
        return

    if tried == 0 and due > 0:
        print("t1 STALLED: %d people due, window open, nothing sent in 90 min. %s IST"
              % (due, now.strftime("%H:%M")))
        return

    if tried >= MIN_SAMPLE and refused_pct is not None and refused_pct >= ALERT_AT:
        print("REFUSALS CLIMBING: Meta refused %.0f%% of the last %d t1 sends "
              "(was %.0f%%). %d still due. %s IST -- consider putting the hold back."
              % (refused_pct, tried, BASELINE_REFUSED, due, now.strftime("%H:%M")))
        return

    # Healthy: say nothing.


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("WATCH ITSELF FAILED: %s: %s" % (type(e).__name__, e))
