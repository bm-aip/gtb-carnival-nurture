"""Stop chasing the arrivals from one ad that never worked. Answer them if they write.

    railway run --service gtb-carnival-nurture python scripts/retire_ad_cohort.py
    railway run --service gtb-carnival-nurture python scripts/retire_ad_cohort.py --commit

Dry run by default: prints exactly who it would mark, and writes nothing.

WHY
---
Owner, 2026-09-04, reading the per-ad split. Ad 52553896609352, headline "Republic
of Nature by GTB", brought 133 conversations, asked 128 of them a question, and got
3 answers and 0 qualified leads. The ads beside it carry headlines naming the
product and the place -- "Luxury Nature Villas on ECR" -- and answer about four
times better on the same bot, the same opener, the same words. A brand-name
headline tells a stranger nothing, so it buys taps from people who do not know what
they tapped.

Continuing to send cold templates to that cohort costs twice: it spends the daily
send budget, and every ignored or blocked marketing message pushes the WhatsApp
quality rating down. The rating was YELLOW on 2026-09-04, one step above a tier
downgrade, and the recovery lever is sending fewer and better-aimed messages.

WHAT IT DOES, AND WHAT IT DELIBERATELY DOES NOT
-----------------------------------------------
Sets `leads.knock_lost_at`. Both send lanes already exclude that column, so the
knock ladder and the re-opener both stop choosing these people, permanently.

It does NOT set `suppressed`, and the difference is the whole design:

    knock_lost_at   OUR decision -- we stopped chasing
    suppressed      THEIR instruction -- they asked us to stop

Owner 2026-08-11 drew that line and db.py enforces it in a schema comment. Folding
our own judgement into the opt-out list makes an opt-out list nobody can trust, and
that is the one list on this system where being wrong does real harm.

It does NOT delete anything, and deleting was the owner's first instinct. Deleting
the row does not delete the person: they would return through the ad webhook as a
fresh stranger with a clean history and start the ladder at message one, so a purge
guarantees the very messages it was meant to prevent. It would also destroy "133
conversations, 0 qualified", which is the evidence for cutting the spend.

The bot still REPLIES to anyone in this cohort who writes to us. Owner's call, and
`knock_lost_at` only gates business-initiated sends -- an inbound message is
answered exactly as it is for anyone else.

WHO IS SPARED
-------------
Anyone who answered a question. Three of the 133 told us a purpose or a location,
and that is precisely the context the re-opener needs to write "we were talking
about a weekend place" and mean it. A bad ad is not the same as a bad person, and
somebody who answered is a person, not a tap. Owner, 2026-09-04: "leave 3".

Counted the way the qualifier reads itself -- a checklist key present but null or
empty is NOT an answer. Raw jsonb key-presence inflates this and would spare people
who told us nothing. Same counting rule as /admin/ads.

Also spared: anyone already lost, anyone suppressed, and anyone who has reached a
human, because re-stamping them would misreport when and why they stopped.
"""
import argparse
import io
import os
import sys

# The Windows console is cp1252 and dies on any name outside Latin-1 -- this script
# crashed on a real buyer's name mid-listing. A tool that reports on people must
# never be unable to print one of them. Same wrapper as tests/_bootstrap.py.
for _stream in ("stdout", "stderr"):
    _s = getattr(sys, _stream)
    if hasattr(_s, "buffer") and (_s.encoding or "").lower() not in ("utf-8", "utf8"):
        setattr(sys, _stream, io.TextIOWrapper(_s.buffer, encoding="utf-8",
                                               errors="replace", line_buffering=True))

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# Under `railway run` from a laptop, DATABASE_URL points at
# postgres.railway.internal, which only resolves inside Railway's own network. The
# public URL is injected alongside it and is what a laptop can actually reach.
# Same swap tests/_bootstrap.py makes, for the same reason.
if os.environ.get("DATABASE_PUBLIC_URL"):
    os.environ["DATABASE_URL"] = os.environ["DATABASE_PUBLIC_URL"]

import db  # noqa: E402

AD_ID = os.environ.get("RETIRE_AD_ID", "52553896609352")
AD_LABEL = "Republic of Nature by GTB"

# The four qualifying gates, named the way the checklist stores them.
GATES = ("purpose", "location", "configuration", "budget")

# A conversation that reached one of these is with a human, or finished. Whatever
# happens next is not the ladder's business and not ours to restamp.
TERMINAL = ("dead", "visit_booked", "qualified", "handed_off")

CANDIDATES = """
    SELECT l.id, l.phone, l.name, l.knock_lost_at, l.suppressed,
           c.outcome,
           COALESCE((SELECT count(*) FROM jsonb_each_text(c.checklist) kv
                      WHERE kv.key = ANY (%s)
                        AND kv.value IS NOT NULL
                        AND kv.value NOT IN ('', 'null')), 0) AS answered
      FROM leads l
      LEFT JOIN conversations c ON c.lead_id = l.id
     WHERE l.ctwa_source_id = %s
     ORDER BY l.id
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="actually write; without it nothing is changed")
    ap.add_argument("--ad", default=AD_ID, help="ctwa_source_id to retire")
    args = ap.parse_args()

    rows = db.q(CANDIDATES, (list(GATES), args.ad)) or []
    if not rows:
        print("no leads carry ctwa_source_id=%s -- nothing to do." % args.ad)
        print("check the id against /admin/ads before assuming the cohort is clean.")
        return 0

    mark, spared = [], []
    for r in rows:
        if r["answered"]:
            spared.append((r, "answered %d question(s)" % r["answered"]))
        elif r["knock_lost_at"]:
            spared.append((r, "already lost"))
        elif r["suppressed"]:
            spared.append((r, "suppressed -- they asked us to stop"))
        elif (r["outcome"] or "") in TERMINAL:
            spared.append((r, "outcome=%s" % r["outcome"]))
        else:
            mark.append(r)

    print("AD %s  (%s)" % (args.ad, AD_LABEL))
    print("  leads carrying this source id : %d" % len(rows))
    print("  would mark knock_lost_at      : %d" % len(mark))
    print("  spared                        : %d" % len(spared))
    print()
    for r, why in spared:
        print("  SPARED  %-14s %-22s %s" % (r["phone"], (r["name"] or "-")[:22], why))
    print()

    if not args.commit:
        print("DRY RUN -- nothing written. Re-run with --commit to apply.")
        print("Sample of who would be marked:")
        for r in mark[:10]:
            print("  MARK    %-14s %s" % (r["phone"], (r["name"] or "-")[:22]))
        if len(mark) > 10:
            print("  ... and %d more" % (len(mark) - 10))
        return 0

    if not mark:
        print("nothing left to mark.")
        return 0

    # TWO STATEMENTS, NOT TWO PER LEAD. db.q/db.x open a connection PER CALL, so
    # the row-at-a-time version cost 254 connections over the Railway proxy and was
    # killed by a client timeout after 73 of 127 -- leaving the job half done and
    # looking finished. Set-based, it is two round trips and cannot half-finish.
    #
    # `AND knock_lost_at IS NULL` mirrors _give_up(): re-running this script must
    # never move a timestamp that already recorded when somebody stopped. That is
    # also what made the interrupted run safe to simply repeat.
    ids = [r["id"] for r in mark]
    note = ("ad %s retired by owner 2026-09-04: %d convs, 0 qualified"
            % (args.ad, len(rows)))

    db.x("""UPDATE leads SET knock_lost_at=now(), wa_state='knock_lost',
                             updated_at=now()
             WHERE id = ANY(%s) AND knock_lost_at IS NULL""", (ids,))
    db.x("""INSERT INTO message_log (lead_id, direction, msg_type, body, ok,
                                     detail, fail_class)
            SELECT unnest(%s), 'out', 'knock_gave_up', NULL, FALSE, %s, 'retired'""",
         (ids, note))

    left = db.q("""SELECT count(*) AS n FROM leads
                    WHERE id = ANY(%s) AND knock_lost_at IS NULL""",
                (ids,), one=True) or {}
    print("marked %d lead(s) as lost; %d still unmarked."
          % (len(ids) - (left.get("n") or 0), left.get("n") or 0))
    print("The bot will still reply if any of them writes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
