"""Task 11 — try to break the brand fence, with real rows in a real database.

Run:
    railway run --service gtb-carnival-nurture python scripts/prove_brand_fence.py

WHY A SCRIPT AND NOT A ONE-OFF CHECK
------------------------------------
The fence is the single assumption the whole multi-brand design rests on: a lead
lives and dies inside its own project, and one project's answers must never reach
another project's buyer. That is not a thing to verify once and trust forever --
every future refactor of retrieval can break it silently, because a leak produces a
confident, fluent, plausible answer rather than an error. Re-run this after any
change to `kb.search`.

WHY A CANARY IS PLANTED
-----------------------
RON is currently the only brand with a corpus, so "search RON, see only RON" would
pass no matter how broken the fence was. A test that cannot fail proves nothing. So
this inserts a genuinely competitive ELEMENTS chunk -- properly embedded, worded to
sit close to RON content in vector space -- attacks the fence, and removes it again
in a `finally` block.

THE ATTACK THAT MATTERS
-----------------------
Not "search RON, get RON". The real one is a buyer in a RON conversation who TYPES
about the other project. Brand comes from the ad or the source list, never from
message text (design §2, and why `_detect_project` was deleted in Phase 0). So a
message naming Elements, asked under brand_id='RON', must still return RON rows or
nothing -- never Elements content.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Local runs reach Postgres over the public TCP proxy; the internal hostname only
# resolves inside Railway's network. The deployed app is unaffected -- it keeps
# using DATABASE_URL.
if os.environ.get("DATABASE_PUBLIC_URL"):
    os.environ["DATABASE_URL"] = os.environ["DATABASE_PUBLIC_URL"]

import db      # noqa: E402
import kb      # noqa: E402
import embed   # noqa: E402

CANARY_TITLE = "__FENCE_CANARY__ (temporary, removed by this script)"
CANARY_TEXT = (
    "Elements Senior Living at Madhuram, Vandalur is a senior living community "
    "near Chennai with assisted living apartments, wellness amenities, landscaped "
    "green spaces, clubhouse facilities and 2BHK and 3BHK residences priced for "
    "retirement buyers. Distances: Vandalur is 5 km away and the airport is 20 km."
)

# Deliberately adversarial. Several of these name the other project outright, which
# is exactly what a confused buyer does.
ATTACKS = [
    "Tell me about Elements Senior Living",
    "What is Madhuram Vandalur?",
    "Do you have senior living options?",
    "What amenities are there?",
    "How far is the airport?",
    "What configurations are available?",
    "2BHK price",
    "Is this a retirement community?",
]


def plant():
    db.x("""INSERT INTO brands (brand_id, display_name)
            VALUES ('ELEMENTS','Elements Senior Living')
            ON CONFLICT (brand_id) DO NOTHING""")
    row = db.q("""INSERT INTO kb_documents
                    (brand_id, title, source_path, doc_type, version, uploaded_by,
                     content_hash)
                  VALUES ('ELEMENTS',%s,'(test)','faq',1,'prove_brand_fence.py','canary')
                  RETURNING id""", (CANARY_TITLE,), one=True)
    doc_id = row["id"]
    vec = embed.embed_documents([CANARY_TEXT])[0]
    db.x("""INSERT INTO kb_chunks
              (brand_id, document_id, ordinal, content, embed_model, embedding)
            VALUES ('ELEMENTS',%s,0,%s,%s,%s)""",
         (doc_id, CANARY_TEXT, os.environ.get("EMBED_MODEL", "voyage-4-large"), vec))
    return doc_id


def remove(doc_id):
    # Chunks cascade on document delete.
    db.x("DELETE FROM kb_documents WHERE id=%s", (doc_id,))


def main():
    if not kb.available():
        print("kb schema unavailable:", db.get_setting("kb_schema_error"))
        return 1

    print("planting an ELEMENTS canary so the test is capable of failing...")
    doc_id = plant()
    failures = []
    try:
        print(f"  canary document id {doc_id}\n")

        print("=== ATTACK 1: RON conversation, questions that invite the other brand ===")
        for q in ATTACKS:
            rows = kb.answer_context("RON", q)
            brands = {r["brand_id"] for r in rows}
            leaked = [r for r in rows if r["brand_id"] != "RON"]
            status = "LEAK" if leaked else "ok  "
            if leaked:
                failures.append((q, leaked))
            top = rows[0]["content"][:58].replace("\n", " ") if rows else "(nothing)"
            print(f"  {status} {q[:38]:40s} {len(rows)} rows {sorted(brands) or '[]'}  top: {top}")

        print()
        print("=== ATTACK 2: the reverse direction — ELEMENTS must not see RON ===")
        for q in ["What is Republic of Nature?", "How far is Sheraton?",
                  "Tell me about Vadanemmeli ECR"]:
            rows = kb.answer_context("ELEMENTS", q)
            leaked = [r for r in rows if r["brand_id"] != "ELEMENTS"]
            if leaked:
                failures.append((q, leaked))
            print(f"  {'LEAK' if leaked else 'ok  '} {q[:38]:40s} "
                  f"{len(rows)} rows {sorted({r['brand_id'] for r in rows}) or '[]'}")

        print()
        print("=== ATTACK 3: no brand at all must be refused, not defaulted ===")
        for bad in (None, "", 0):
            try:
                kb.search(bad, [0.0] * 1024)
                failures.append((f"search(brand={bad!r})", "was allowed"))
                print(f"  LEAK search(brand={bad!r}) returned instead of raising")
            except ValueError as e:
                print(f"  ok   search(brand={bad!r}) -> ValueError: {e}")

        print()
        print("=== ATTACK 4: a brand that does not exist gets nothing, not everything ===")
        rows = kb.answer_context("NOT_A_BRAND", "What amenities are there?")
        if rows:
            failures.append(("unknown brand", rows))
        print(f"  {'LEAK' if rows else 'ok  '} unknown brand -> {len(rows)} rows")

        print()
        print("=== the canary WAS findable — proving the test could have failed ===")
        rows = kb.answer_context("ELEMENTS", "Tell me about Elements Senior Living")
        found = any("Madhuram" in r["content"] for r in rows)
        print(f"  {'ok  ' if found else 'WARN'} canary retrievable under its own brand: {found}")
        if not found:
            print("       (if this is False the attacks above prove nothing)")
            failures.append(("canary not retrievable", "test was vacuous"))

    finally:
        remove(doc_id)
        left = db.q("SELECT count(*) AS n FROM kb_chunks WHERE brand_id='ELEMENTS'",
                    one=True)
        print(f"\ncanary removed. ELEMENTS chunks remaining: {left['n']}")

    print()
    if failures:
        print(f"FENCE BREACHED — {len(failures)} failure(s)")
        for q, detail in failures:
            print(f"  {q}: {detail}")
        return 1
    print("FENCE HELD across all attacks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
