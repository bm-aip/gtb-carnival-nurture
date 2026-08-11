"""Recover Meta click ids for ad arrivals that predate the capture code.

    railway run --service gtb-carnival-nurture python scripts/backfill_ctwa.py
    railway run --service gtb-carnival-nurture python scripts/backfill_ctwa.py --commit

Dry run by default: it prints exactly what it would write and writes nothing.

WHY THIS EXISTS AND WHY IT IS URGENT
------------------------------------
`ctwa_clid` is fetched from Wati's message history, not received in the webhook, so
every ad arrival before the capture code shipped has a click id that is still
sitting in Wati and not in our database. Wati keeps the history; Meta's event window
does not wait. Once an arrival ages past that window its click id is worthless even
though it is still readable -- so this is a one-time sweep with a deadline, not a
maintenance chore.

It is also the safety net for the live path: capture dedups permanently per phone,
so a lookup that exhausted its retries is never retried automatically. Running this
occasionally sweeps those up.

WHO IS A CANDIDATE
------------------
Two independent signals, unioned, because each misses cases the other catches:

  (a) an adoption log line carrying `sourceId=` -- the webhook's own evidence, but
      only ever written for STRANGERS, so it misses every returning lead that
      clicked an ad (the leak this branch fixes).

  (b) an INBOUND message opening with `Hi!` -- the prefilled text of the CTWA ad.
      `Hi,` is the landing page and is deliberately NOT a candidate: no Meta click
      happened, so there is nothing to fetch.

      ⚠️ `ml.direction='in'` is load-bearing. The BOT's own replies open with "Hi!"
      too -- 97 outbound `qualifier_turn` rows on 2026-08-10 -- so without the
      filter this matched almost every lead the bot had ever greeted. That inflated
      the candidate set from 40 to 85 and spent 48 Wati calls proving that people
      who were never CTWA have no CTWA referral. Cheap mistake here, but it also
      produced a wrong claim in reporting: the extra 45 looked like leads whose
      click id the old leak had dropped, and they were nothing of the kind. The
      webhook's `sourceId` turned out to carry 36 of the 37 real referrals on its
      own; this signal adds one.

Leads already carrying a click id, or already looked at and found to have none, are
skipped. Safe to re-run.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# DATABASE_URL points at postgres.railway.internal, which resolves only inside
# Railway. From a laptop that reads as "the database is down".
if os.environ.get("DATABASE_PUBLIC_URL"):
    os.environ["DATABASE_URL"] = os.environ["DATABASE_PUBLIC_URL"]

import db      # noqa: E402
import wati    # noqa: E402

CANDIDATES = """
SELECT l.id, l.phone, l.name, l.wa_state, l.ctwa_clid, l.last_inbound_at,
       max(CASE WHEN ml.detail ~ 'sourceId=' THEN 1 ELSE 0 END) AS webhook_evidence,
       max(CASE WHEN ml.direction = 'in' AND ml.body ~* '^\\s*hi!'
                THEN 1 ELSE 0 END) AS opener_evidence
  FROM leads l
  JOIN message_log ml ON ml.lead_id = l.id
 WHERE l.ctwa_clid IS NULL
   AND l.ctwa_looked_at IS NULL
 GROUP BY l.id
HAVING max(CASE WHEN ml.detail ~ 'sourceId=' THEN 1 ELSE 0 END) = 1
    OR max(CASE WHEN ml.direction = 'in' AND ml.body ~* '^\\s*hi!'
                THEN 1 ELSE 0 END) = 1
 ORDER BY l.last_inbound_at DESC NULLS LAST
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="actually write. Without it, nothing is modified.")
    ap.add_argument("--limit", type=int, default=0, help="stop after N leads")
    ap.add_argument("--sleep", type=float, default=0.4,
                    help="seconds between Wati calls (default 0.4)")
    args = ap.parse_args()

    rows = db.q(CANDIDATES) or []
    if args.limit:
        rows = rows[:args.limit]

    print(f"candidates: {len(rows)}   mode: {'COMMIT' if args.commit else 'DRY RUN'}\n")
    found = missing = errored = 0

    for r in rows:
        why = ("webhook" if r["webhook_evidence"] else "") + \
              ("+opener" if r["opener_evidence"] else "")
        try:
            ref = wati.fetch_referral(r["phone"])
        except Exception as e:
            errored += 1
            print(f"  ERR   lead={r['id']:<5} {r['phone']}  {str(e)[:90]}")
            time.sleep(args.sleep)
            continue

        if not ref or not (ref.get("ctwa_clid") or ref.get("source_id")):
            missing += 1
            print(f"  none  lead={r['id']:<5} {r['phone']}  ({why})")
            if args.commit:
                db.x("UPDATE leads SET ctwa_looked_at=now() WHERE id=%s", (r["id"],))
            time.sleep(args.sleep)
            continue

        found += 1
        clid = ref.get("ctwa_clid") or ""
        print(f"  FOUND lead={r['id']:<5} {r['phone']}  ad={ref.get('source_id')} "
              f"clid={clid[:24]}...({len(clid)})  {ref.get('headline')!r}  ({why})")

        if args.commit:
            # COALESCE so a re-run can fill a gap without blanking anything held.
            db.x("""UPDATE leads
                       SET ctwa_clid        = COALESCE(%s, ctwa_clid),
                           ctwa_source_id   = COALESCE(%s, ctwa_source_id),
                           ctwa_source_url  = COALESCE(%s, ctwa_source_url),
                           ctwa_headline    = COALESCE(%s, ctwa_headline),
                           ctwa_captured_at = now(),
                           ctwa_looked_at   = now(),
                           inflow           = 'ctwa',
                           updated_at       = now()
                     WHERE id = %s""",
                 (ref.get("ctwa_clid"), ref.get("source_id"), ref.get("source_url"),
                  ref.get("headline"), r["id"]))
            db.log_msg(r["id"], "in", "ctwa_captured", None,
                       detail=f"backfill ad={ref.get('source_id')} "
                              f"clid_len={len(clid)}")
        time.sleep(args.sleep)

    print(f"\nfound {found} · no referral {missing} · errors {errored}")
    if not args.commit:
        print("DRY RUN -- nothing written. Re-run with --commit.")


if __name__ == "__main__":
    main()
