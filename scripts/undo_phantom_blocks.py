"""Give back the turn that a never-sent message took away. Dry run by default.

    railway run --service gtb-carnival-nurture python scripts/undo_phantom_blocks.py
    railway run --service gtb-carnival-nurture python scripts/undo_phantom_blocks.py --commit

WHAT HAPPENED
-------------
2026-09-04. Retiring a dead ad's cohort wrote 126 `knock_gave_up` rows in twenty
minutes. Those are bookkeeping -- the whole point of the row is that we have
STOPPED sending to that person -- but they are filed as direction='out' with a
detail that does not begin with `blocked:`, which is exactly what
`wati.sends_last_hour()` counts. The counter read 133 against a cap of 100 and
the door shut on every real message for an hour.

PR #81 had unjammed the re-opener minutes earlier. The lane picked 23 buyers for
the first time in 90 hours, found the door shut, and logged one
`blocked:rate_capped` row each for twenty of them. `reopener.due()` anchors
spacing on `last_try`, which counts EVERY row of that msg_type -- so twenty real
people were put to sleep for REOPEN_AFTER_DAYS having received nothing at all.

WHY DELETING IS THE HONEST FIX HERE, AND IS NOT LOSING HISTORY
--------------------------------------------------------------
These rows record a message that never reached WhatsApp. They are not evidence
about the buyer, about deliverability, or about Meta -- they are evidence about
our own counter being wrong for twenty minutes, and that fact is recorded in the
code and in the pull request, which is where it belongs.

Left in place they keep asserting something false: that we contacted these people.
Every future question -- when did we last reach them, how many tries have they
had, are they owed a nudge -- would be answered wrongly from them.

This is the narrowest possible undo. It targets one msg_type, one block reason,
one time window, and it prints every row before touching anything. It does NOT
touch `meta_refused` rows: those messages really did leave the building, and a
send that was refused on the wire must still cost a try. That distinction is the
whole of [[never-arrived-must-not-count]] and getting it backwards here would
re-create the runaway that RETRY_MAX_BURST exists to stop.
"""
import argparse
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

for _stream in ("stdout", "stderr"):
    _s = getattr(sys, _stream)
    if hasattr(_s, "buffer") and (_s.encoding or "").lower() not in ("utf-8", "utf8"):
        setattr(sys, _stream, io.TextIOWrapper(_s.buffer, encoding="utf-8",
                                               errors="replace", line_buffering=True))

if os.environ.get("DATABASE_PUBLIC_URL"):
    os.environ["DATABASE_URL"] = os.environ["DATABASE_PUBLIC_URL"]

import db  # noqa: E402

# Deliberately narrow. A wider predicate would sweep up gate blocks that are
# telling the truth -- an opt-out, a fatigue cap, a retry ceiling -- and those
# SHOULD keep somebody out of the queue.
MSG_TYPE = "reopener_t7"
REASON = "blocked:rate_capped%"
SINCE = "2026-09-04 09:00:00+00"

FIND = """
    SELECT m.id, m.ts, l.phone, l.name, m.detail
      FROM message_log m JOIN leads l ON l.id = m.lead_id
     WHERE m.msg_type = %s
       AND m.detail LIKE %s
       AND m.ts > %s::timestamptz
     ORDER BY m.ts, l.phone
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="actually delete; without it nothing is changed")
    args = ap.parse_args()

    rows = db.q(FIND, (MSG_TYPE, REASON, SINCE)) or []
    if not rows:
        print("no phantom blocks found -- nothing to undo.")
        return 0

    print("%d row(s) recording a re-open that never left the building:" % len(rows))
    for r in rows:
        print("  %s  %-14s %s" % (str(r["ts"])[11:19], r["phone"],
                                  (r["name"] or "-")[:24]))
    print()

    if not args.commit:
        print("DRY RUN -- nothing deleted. Re-run with --commit to apply.")
        return 0

    ids = [r["id"] for r in rows]
    db.x("DELETE FROM message_log WHERE id = ANY(%s)", (ids,))
    left = db.q("SELECT count(*) AS n FROM message_log WHERE id = ANY(%s)",
                (ids,), one=True) or {}
    gone = len(ids) - (left.get("n") or 0)
    print("deleted %d row(s); %d remain." % (gone, left.get("n") or 0))
    print("Those buyers are owed a re-open again on the next tick with headroom.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
