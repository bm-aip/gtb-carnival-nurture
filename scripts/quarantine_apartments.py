"""Withdraw the two FAQ answers that are about apartments and nothing else.

Run AFTER the ingest, never before:

    railway run --service gtb-carnival-nurture python scripts/ingest_kb.py
    railway run --service gtb-carnival-nurture python scripts/quarantine_apartments.py
    railway run --service gtb-carnival-nurture python scripts/quarantine_apartments.py --undo

WHY A SECOND SCRIPT AND NOT AN ENTRY IN quarantine_kb.py. That file withdraws by
absolute chunk id, and its ids are FAQ v6 -- every ingest mints new ones, so they no
longer point at anything a buyer can reach. Re-using it would print "quarantined" for
rows on a superseded document and change nothing. This resolves the chunk on whichever
FAQ version is ACTIVE right now, so it stays correct across every future ingest.

WHY ORDINAL AND NOT A TEXT SEARCH. Ordinal is what ingest_kb.py itself carries
withdrawals forward on, so this agrees with the mechanism already in place. But an
ordinal is only stable while the question count is -- so nothing is written unless the
chunk sitting at that ordinal still says what we expect. A shifted ordinal withdraws
the WRONG answer and frees the one we meant to stop, which is worse than doing
nothing. The guard is the whole point of the file.

WHY THESE TWO AND NOT THE OTHER APARTMENT ANSWERS. Villas only from 2026-09-02. Most
apartment mentions in the FAQ were mixed answers -- "Roof of Apartments and Villas",
"100% power back up for villas and apartments" -- and those were fixed in the SOURCE
LINE instead, because the villa half is a real fact worth keeping and because a chunk
whose own text names an apartment can be quoted whatever rule sits beside it. Only
these two have no villa fact left once the apartment is taken out: a villa has no
corridor lobby, and "video phone door for all apartments" says nothing about a villa.
Answering either for a villa buyer would be inventing a specification.
"""
import io
import os
import sys

for _s in ("stdout", "stderr"):
    _f = getattr(sys, _s)
    if hasattr(_f, "buffer"):
        setattr(sys, _s, io.TextIOWrapper(_f.buffer, encoding="utf-8",
                                          errors="replace", line_buffering=True))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.environ.get("DATABASE_PUBLIC_URL"):
    os.environ["DATABASE_URL"] = os.environ["DATABASE_PUBLIC_URL"]
import db  # noqa: E402

DOC_TITLE = "RON FAQ (curated)"
REASON_TAG = "[villas-only 2026-09-02]"

# ordinal -> (a phrase that MUST appear in that chunk, why it is withdrawn)
WITHDRAW = {
    52: ("corridor lobby",
         "Apartment-only specification: the width of an apartment corridor lobby. "
         "Villas have no corridor lobby, so there is no villa fact underneath this "
         "one to keep. Villas only from 2026-09-02."),
    54: ("video phone door",
         "Answers a door-lock question for apartments only -- 'video phone door for "
         "all apartments'. Retrieved by a villa buyer it either describes a home we "
         "no longer sell or, worse, reads as a villa specification nobody has "
         "confirmed. Villas only from 2026-09-02."),
}


def _active_faq():
    return db.q("""SELECT id, version FROM kb_documents
                   WHERE brand_id='RON' AND title=%s AND active
                   ORDER BY version DESC LIMIT 1""", (DOC_TITLE,), one=True)


def main():
    doc = _active_faq()
    if not doc:
        print(f"no active document titled {DOC_TITLE!r}. Run ingest_kb.py first.")
        return 1
    print(f"active {DOC_TITLE} is v{doc['version']} (document {doc['id']})")

    if "--undo" in sys.argv:
        n = db.x("""UPDATE kb_chunks SET quarantined=FALSE, quarantine_reason=NULL,
                           quarantined_at=NULL
                    WHERE document_id=%s AND quarantine_reason LIKE %s""",
                 (doc["id"], REASON_TAG + "%"))
        print(f"restored {n} chunk(s) withdrawn by this script")
        return 0

    # VERIFY EVERY ORDINAL BEFORE WRITING ANYTHING. All or nothing: a half-applied
    # run leaves the corpus in a state nobody can read off either script.
    planned = []
    for ordinal, (must_contain, reason) in sorted(WITHDRAW.items()):
        row = db.q("""SELECT id, content, quarantined FROM kb_chunks
                      WHERE document_id=%s AND ordinal=%s""",
                   (doc["id"], ordinal), one=True)
        if not row:
            print(f"  REFUSED: no chunk at ordinal {ordinal}. Nothing written.")
            return 1
        if must_contain.lower() not in (row["content"] or "").lower():
            print(f"  REFUSED: ordinal {ordinal} does not contain "
                  f"{must_contain!r}. It says: {row['content'][:120]!r}\n"
                  f"  The FAQ's question count has changed, so these ordinals no "
                  f"longer point where they did. Re-read the chunks and update "
                  f"WITHDRAW. Nothing written.")
            return 1
        planned.append((row, ordinal, reason))

    for row, ordinal, reason in planned:
        if row["quarantined"]:
            print(f"{'already out':>12}  ordinal {ordinal} (chunk {row['id']})")
            continue
        db.x("""UPDATE kb_chunks
                SET quarantined=TRUE, quarantine_reason=%s, quarantined_at=now()
                WHERE id=%s""", (f"{REASON_TAG} {reason}", row["id"]))
        print(f"{'withdrawn':>12}  ordinal {ordinal} (chunk {row['id']})")

    rows = db.q("""SELECT count(*) FILTER (WHERE c.quarantined) q,
                          count(*) FILTER (WHERE NOT c.quarantined) live
                   FROM kb_chunks c JOIN kb_documents d ON d.id=c.document_id
                   WHERE c.brand_id='RON' AND d.active""", one=True) or {}
    print(f"\nRON active corpus: {rows.get('live')} retrievable, "
          f"{rows.get('q')} quarantined")
    return 0


if __name__ == "__main__":
    sys.exit(main())
