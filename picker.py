"""ONE PAGING RULE, for every lane that chooses who to message.

FILTER BEFORE YOU LIMIT -- written down here as code because writing it down as
prose did not stop it happening four times.

A lane picks people in two stages: SQL narrows the field, then Python applies the
rules SQL cannot model. The bug is always the same shape. SQL takes a fixed window
of the oldest N candidates, the Python loop rejects every one of them on something
SQL does not know about, and the people who ARE sendable sit behind that window,
invisible, for as long as nobody looks:

  2026-08-22  knocks    the 125 oldest rows were all waiting out a 15-day gap.
                        293 rows due behind them. 63 -> 1 knocks/day for 14 days.
  2026-08-26  knocks    the 220 oldest were inside a 24h retry gap after a refusal
                        storm. 129 people owed a t2. Eleven hours of nothing.
  2026-09-03  knocks    23 burst-blocked leads, the oldest by definition, refilled
                        every batch for nine days. 309 buyers dark.
  2026-09-04  reopener  the 50 oldest ghosts had no nameable topic, so due(limit=25)
                        examined 125 rows and returned ZERO -- while 26 people with
                        real context waited. Last re-open 90 hours earlier.

The knock lane was fixed in place three times. The re-opener inherited none of it,
because a fix that lives inside one lane is not a rule, it is a habit. So the walk
lives here, both lanes call it, and lane five gets it without anyone remembering.

WHY A CALLBACK AND NOT A CLEVERER QUERY. The rejects are things SQL genuinely
cannot see: whether two lead rows are the same human being, whether a topic can be
named out of a stored checklist, how many refusals a phone has taken since its last
success. Pushing them into SQL would mean a second copy of the rules -- which is
the drift this project keeps paying for. The Python loop stays the authority; the
walk simply refuses to stop looking too early.
"""
import os

# Rows per round trip. Deliberately far larger than any batch, so the ordinary
# case is one query and the walk costs nothing.
SCAN_PAGE = int(os.environ.get("KNOCK_SCAN_PAGE", "250"))

# The bound that stops this being a table scan on every tick. A walk that has
# examined this many candidates and found nothing sendable has answered the
# question honestly enough.
SCAN_MAX = int(os.environ.get("KNOCK_SCAN_MAX", "3000"))


def scan(fetch, select, limit, page=None, cap=None):
    """Walk pages of candidates until `limit` are chosen or `cap` are examined.

        fetch(page, offset) -> rows       one page of candidates, best first
        select(row)         -> item|None  the lane's own rules; None means skip

    `select` MAY HAVE SIDE EFFECTS -- knocks._give_up() ends a journey on a ceiling
    verdict -- so it is called exactly once per row, in order, and never for rows
    beyond the point where the batch filled. Anything merely COUNTING must pass a
    side-effect-free select, or it will change the thing it is measuring. That is
    not hypothetical: the watchdog may call reopener.due() and must never call
    knocks.due(), for this reason.

    Returns the chosen items in the order the pages produced them.
    """
    page = page or SCAN_PAGE
    cap = cap or SCAN_MAX
    out = []
    offset = scanned = 0
    while scanned < cap and len(out) < limit:
        rows = fetch(page, offset) or []
        if not rows:
            break
        scanned += len(rows)
        offset += page
        for row in rows:
            picked = select(row)
            if picked is not None:
                out.append(picked)
                if len(out) >= limit:
                    break
        # A short page is the end of the table, not a slow one.
        if len(rows) < page:
            break
    return out
