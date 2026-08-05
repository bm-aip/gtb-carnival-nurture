"""Withdraw the chunks the 2026-08-03 audit found unsafe. Reversible.

Run:  railway run --service gtb-carnival-nurture python scripts/quarantine_kb.py
      railway run --service gtb-carnival-nurture python scripts/quarantine_kb.py --undo

Nothing is deleted. The text and embedding stay; retrieval stops selecting the chunk
(kb.search filters `NOT c.quarantined`). Clearing the flag restores it.

WHY THESE AND NOT OTHERS. A chunk is withdrawn when it is WRONG, when it CONTRADICTS
another chunk and the business has not yet said which is right, when it describes a
product we do not sell, or when its wording is a liability. A chunk that is merely
internal-sounding is left alone -- withdrawing it would lose a real fact, and that is
a guardrail's job, not quarantine's.

Every entry cites the audit question it is waiting on, so restoring is a decision with
a name attached rather than a guess.
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

# chunk id -> why. The text is stored in the database, so a curator opening the table
# sees the reason without reading this file.
QUARANTINE = {
    # --- Scale: three different answers exist. Waiting on Q1 and Q2. ---
    391: "WRONG AND ALREADY QUOTED TO A BUYER. '204 Units in an 8 Acre property' is a "
         "throwaway clause inside an answer about traffic management at the gate, and "
         "the bot quoted it as the project's headline scale on 2026-08-03. It also "
         "contradicts chunk 358 (19.35 acres across three phases). Waiting on Q1/Q2.",
    358: "Phase land parcel (3.05 + 4.8 + 11.5 acres). Not wrong, but it is the "
         "unconfirmed public figure and it contradicts chunk 391. The bot quoted both "
         "in one conversation and the buyer could see it. Waiting on Q1.",
    359: "Unit counts per phase, and the only place the totals come from. Contradicts "
         "chunk 391 (204). Also lists Island Villas, Beachfront Villas and 106 "
         "Villaments, none of which we sell. Waiting on Q2/Q3.",
    360: "Built-up areas including Island Villas, Beachfront Villas and Villaments. "
         "Off-category, and pure internal detail. Waiting on Q3.",

    # --- Products we do not sell. Waiting on Q3 and Q4. ---
    362: "Common-area percentages including Phase 3 Villaments. Off-category. Q3.",
    363: "Variant list: Phase 1 '1BHK', Phase 3 Island Villas 5BHK, Beachfront 4BHK, "
         "Villaments 2/3/4BHK. This is the chunk that most directly caused the bot to "
         "offer 'villas, apartments and villaments' to a real buyer. Q3/Q4.",
    369: "Ceiling heights including Island Villas, Beachfront Villas, Villaments. "
         "Off-category, and the name 'Beachfront' breaches the no-private-beach rule "
         "on its own. Q3.",
    374: "OSR land areas including Phase 3. Off-category and internal. Q3.",
    418: "'Community hall in Phase 1 & 2, Banquet Hall in Phase 3.' The community hall "
         "is real and worth saying -- it should return inside the approved amenities "
         "paragraph (Q13) rather than attached to a Phase 3 mention. Q3/Q13.",

    # --- Wording that is a liability. Waiting on Q8, Q9. ---
    407: "FLOOD RISK ASSURANCE on a coastal site: 'No Flooding has happened in the "
         "area till date ... we are confidend the water wont flood.' This promises a "
         "buyer their home will not flood. Also misspelt, and 'flow to the vest' "
         "means west. Must not be said by a bot under any circumstances. Q8.",
    414: "Answers a FIRE SAFETY question -- sprinklers, smoke detectors, alarms -- "
         "with 'Non High rise building not required'. Legally accurate, commercially "
         "indefensible. Q9.",
    365: "Not an answer at all: it is an instruction to a salesperson ('always "
         "empahsise area statement has Rera Carpet area...'). The bot can repeat "
         "'always emphasise' to the buyer.",

    # --- Contradictions and half-answers. Waiting on Q5, Q6, Q10. ---
    375: "'Separate swimming pools for apartments? No' -- directly contradicts chunk "
         "406 ('1200mm for all Pools'). Both withdrawn so the question goes to a "
         "person until the business says which is true. Q5.",
    406: "'1200mm for all Pools' -- implies several pools and contradicts chunk 375 "
         "('No'). See 375. Q5.",
    390: "Two questions merged by the source split, only one answered: power backup IS "
         "answered, 'will the centralised AC provision throughout the project' is NOT "
         "-- but the unanswered question text is still in the chunk, so the model can "
         "read it as answered and imply centralised AC. Q6.",
    383: "Construction status with NO DATE ('Apartments foundation work has started'). "
         "It goes stale silently and it is the closest thing in the corpus to a "
         "progress commitment, which is the one subject the bot must never commit on. "
         "Q10.",

    # =====================================================================
    # 2026-08-05 -- MARKETING ANSWERED. These are decisions, not open questions.
    # =====================================================================
    #
    # The eight utility answers below are the "bare No" pattern the audit named. Asked
    # whether to soften them or hand them to a person, marketing replied "answer all
    # these as a Yes" -- which, read literally, tells the bot to confirm we provide
    # piped gas, water meters, intercoms, balcony glass and a lease guarantee. We did
    # not ship that. Put back item by item, the business's decision was: SALES ANSWERS
    # THESE.
    #
    # So they leave the corpus. Not because the answers are wrong -- because a
    # one-word "No" on a crore-plus purchase is worse than a warm "let me get you a
    # proper answer on that", and because these are specification questions where the
    # detail changes and a salesperson has the current sheet in front of them.
    #
    # answering-rules.md carries the deferral wording. Withdrawing the chunk is what
    # makes the deferral happen: with nothing to cite, the bot cannot answer.
    386: "Bare 'No piped gas'. Business decision 2026-08-05: sales answers utility and "
         "specification questions. Withdrawn so the bot defers instead of stonewalling.",
    400: "Bare 'No glass' to a balcony specification question. Sales answers these "
         "(2026-08-05). Also a spec that can change between phases.",
    401: "Bare 'No Glass' to a brand question. Sales answers these (2026-08-05).",
    405: "Bare 'No Counter Top' on a premium kitchen. Was given a guardrail on "
         "2026-08-03; the business decided on 2026-08-05 that sales answers kitchen "
         "specification questions, so it is withdrawn instead.",
    409: "Bare 'No' to an intercom question. Sales answers these (2026-08-05).",
    413: "Bare 'No' to a lease-guarantee question. This one is a COMMITMENT question "
         "-- rental assurance is a financial undertaking and must never be answered by "
         "a bot in either direction. Sales answers it (2026-08-05).",
    417: "Bare 'No High tension line'. Sales answers these (2026-08-05).",
    420: "Bare 'No Water meter'. Sales answers these (2026-08-05).",

    # Naming the maintenance provider -- marketing said no (Q12).
    #
    # A guardrail cannot fix these two, and that distinction matters: a guardrail
    # constrains what the model DOES with a chunk, but the forbidden word is in the
    # chunk's own text, so retrieving it puts "elements" in front of the model. The
    # only way to make a name unsayable is to keep it out of the context window.
    366: "Names the sister company as the maintenance provider. Marketing 2026-08-05 "
         "(Q12): do not name it. The name is in the chunk text, so a guardrail cannot "
         "hold -- it has to leave the pool. Maintenance questions go to sales.",
    367: "Same as 366, and it also fails to answer its own question ('for how many "
         "years'). Marketing 2026-08-05 (Q12): do not name the provider.",
}

# Facts worth keeping, but which need a guard travelling with them. Quarantine would
# lose the fact; a guardrail keeps it and constrains how it is used.
GUARDRAILS = {
    373: "There is ONE common clubhouse of 60,000 sqft. This chunk's question text "
         "also asks how many clubhouses there are per phase -- that is NOT answered "
         "here, so never state a per-phase clubhouse count. The bioswale depth and "
         "landscape buffer are internal engineering detail: do not volunteer them.",
    # 366, 367 and 405 lived here until 2026-08-05. All three moved to QUARANTINE when
    # marketing answered -- see the notes there. A guardrail was the right holding
    # position while the question was open; it is the wrong answer once the business
    # has said the fact itself must not be stated.
}


def migrate():
    """Add the quarantine columns. Idempotent, and run here rather than waiting on a
    deploy so the unsafe chunks can be withdrawn immediately. kb.init_kb() carries the
    same DDL, so a later deploy is a no-op."""
    for sql in ("ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS "
                "quarantined BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS quarantine_reason TEXT",
                "ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS "
                "quarantined_at TIMESTAMPTZ"):
        db.x(sql)
    print("columns ready")


def main():
    migrate()
    undo = "--undo" in sys.argv
    if undo:
        n = db.x("""UPDATE kb_chunks SET quarantined=FALSE, quarantine_reason=NULL,
                           quarantined_at=NULL
                    WHERE brand_id='RON' AND quarantined""")
        print(f"restored {n} chunk(s)")
        return

    for cid, reason in QUARANTINE.items():
        n = db.x("""UPDATE kb_chunks
                    SET quarantined=TRUE, quarantine_reason=%s, quarantined_at=now()
                    WHERE id=%s AND brand_id='RON'""", (reason, cid))
        print(f"{'quarantined' if n else 'NOT FOUND':>12}  chunk {cid}")

    for cid, rail in GUARDRAILS.items():
        n = db.x("""UPDATE kb_chunks SET guardrail=%s
                    WHERE id=%s AND brand_id='RON' AND NOT quarantined""", (rail, cid))
        print(f"{'guardrail' if n else 'NOT FOUND':>12}  chunk {cid}")

    rows = db.q("""SELECT count(*) FILTER (WHERE c.quarantined) q,
                          count(*) FILTER (WHERE NOT c.quarantined) live,
                          count(*) FILTER (WHERE NOT c.quarantined
                                            AND c.guardrail IS NOT NULL) railed
                   FROM kb_chunks c JOIN kb_documents d ON d.id=c.document_id
                   WHERE c.brand_id='RON' AND d.active""", one=True) or {}
    print(f"\nRON active corpus: {rows.get('live')} retrievable "
          f"({rows.get('railed')} with a guardrail), {rows.get('q')} quarantined")


if __name__ == "__main__":
    main()
