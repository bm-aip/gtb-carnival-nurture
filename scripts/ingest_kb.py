"""Load a brand's curated corpus into kb_documents + kb_chunks (task 10).

Run:
    python scripts/ingest_kb.py --brand RON --dry-run    # chunk only, no DB, no API
    python scripts/ingest_kb.py --brand RON             # embed and load

--dry-run needs neither a database nor an API key, so the chunking can be reviewed
before a single token is spent or a single row written. Use it first.

CHUNKING IS PER-FILE AND DELIBERATE
-----------------------------------
Generic fixed-size chunking would be wrong for both of these files:

  faqs.md      -- already atomic. One `## question` + answer IS the unit of meaning.
                  Splitting by character count would cut answers in half and pair
                  the tail of one with the head of the next.

  location.md  -- an 18-row distance table. One chunk per row would mean "what is
                  nearby?" retrieves three landmarks out of eighteen and looks
                  ignorant. The whole table is ~700 characters, so it goes in as ONE
                  chunk and every distance question gets every distance.

Anything in these files addressed to US rather than to a buyer -- the curation notes,
the "what this file does NOT contain" section, the rule tables -- is excluded. A bot
that ingests its own instructions will read them aloud.

RE-RUNNING IS SAFE
------------------
Documents are versioned and matched on a content hash. An unchanged file is skipped.
A changed file creates a NEW version, and the previous version is marked inactive
rather than deleted -- so an answer given last week can still be traced to the exact
text that produced it (design §10, the audit guardrail).
"""
import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config          # noqa: E402
import db              # noqa: E402
import kb              # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The claims rule that must travel WITH the location facts rather than living only
# in template copy. Attached to the chunk, so it is retrieved alongside any
# distance answer and cannot be separated from it.
BEACH_GUARDRAIL = (
    "Never imply direct access to a natural or private beach. Approved wording: "
    "a planned man-made beach and lagoon experience within the community. "
    "Covelong is a nearby place, not an amenity of this project.")

# A NEGATIVE guardrail, and the reason it exists is worth stating.
#
# Asked "which schools are nearby?" or "nearest hospital?", retrieval correctly
# returns the location chunk -- it is genuinely the closest thing in the corpus. But
# that chunk contains no school and no hospital. An answering layer that trusts
# "I retrieved a relevant location chunk" would improvise, and a plausible invented
# school near a Chennai project is exactly the kind of confident wrongness that
# destroys trust.
#
# The confidence floor (tasks 20/25) is the real fix. Until it exists, the chunk
# declares its own gaps so whatever reads it cannot mistake proximity for coverage.
LOCATION_GAPS = (
    "This list contains NO schools, NO hospitals, NO offices or IT corridors and NO "
    "metro information. If asked about any of those, say we will have someone "
    "confirm and escalate to a human. Never estimate, never infer one from a nearby "
    "landmark, and never convert a distance into a drive time.")


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_faqs(text):
    """One chunk per `## question` section. Skips the generated header and the
    excluded-rows appendix, both of which are notes to us."""
    body = text.split("## Excluded rows, by rule")[0]
    chunks = []
    for m in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", body, re.S | re.M):
        question = m.group(1).strip()
        answer = m.group(2)
        answer = re.sub(r"<!--.*?-->", "", answer, flags=re.S)
        # Strip the document's own trailing horizontal rule. The last Q&A section
        # runs to end-of-body, so markdown's `---` separator was being glued onto
        # the final answer -- "Roof of Apartments and Villas / / / ---". A parser
        # artefact, not bad source data, but it would have been embedded and
        # retrieved exactly as if it were part of the answer.
        answer = re.sub(r"\n\s*-{3,}\s*$", "", answer).strip()
        if not answer:
            continue
        chunks.append({
            "content": f"Q: {question}\nA: {answer}",
            "guardrail": None,
        })
    return chunks


def chunk_location(text):
    """The whole distance table as ONE chunk, plus the project's own siting.

    Deliberately not one chunk per landmark -- see the module docstring.
    """
    rows = re.findall(r"^\|\s*([^|]+?)\s*\|\s*([\d.]+\s*(?:m|km))\s*\|\s*$",
                      text, re.M | re.I)
    rows = [(n.strip(), d.strip()) for n, d in rows
            if n.strip().lower() not in ("landmark", "")]
    if not rows:
        return []
    listing = "; ".join(f"{name} — {dist}" for name, dist in rows)
    return [{
        "content": ("Republic of Nature is at Vadanemmeli on ECR (East Coast Road), "
                    "Chennai. Distances from the project: " + listing + ". "
                    "Nearest railway station is Central at 43 km and the airport is "
                    "39 km. No school, hospital, office campus or metro station is "
                    "recorded for this project."),
        "guardrail": BEACH_GUARDRAIL + " " + LOCATION_GAPS,
    }]


def chunk_inventory(text):
    """The sellable-configuration table, plus the two rules that must travel with it.

    One chunk, same reasoning as the distance table: a buyer asking "what sizes do you
    have?" should get the whole list, not three rows of it.

    `pricing-internal.md` is deliberately ABSENT from SOURCES. The budget gate needs
    those numbers; the bot must never be able to say them. Keeping them out of
    kb_chunks makes that structural rather than a matter of the model behaving well --
    the figures are not in the cabinet, so they cannot be quoted.
    """
    rows = re.findall(r"^\|\s*([^|]+?)\s*\|\s*(\d+)\s*sqft\s*\|\s*$", text, re.M | re.I)
    rows = [(t.strip(), s.strip()) for t, s in rows if t.strip().lower() != "type"]
    if not rows:
        return []
    listing = "; ".join(f"{t} at {s} sqft" for t, s in rows)
    return [{
        "content": ("Republic of Nature currently offers these configurations: "
                    + listing + ". Apartments run 1220 to 2133 sqft and villas run "
                    "2552 to 3634 sqft. 'Compact 2BHK' is a smaller two-bedroom "
                    "apartment and is a different product from the 2BHK. "
                    "1BHK apartments, villaments, island villas and beachfront "
                    "villas are described in older material but are not currently "
                    "being sold."),
        "guardrail": ("Never state a price, a price range, or a per-square-foot rate. "
                      "Every price question goes to a human. Do not volunteer the top "
                      "of the villa range as exact -- sources disagree (3634 vs 3643)."),
    }]


SOURCES = {
    "RON": [
        {"path": "kb/RON/faqs.md", "title": "RON FAQ (curated)",
         "doc_type": "faq", "chunker": chunk_faqs},
        {"path": "kb/RON/location.md", "title": "RON location and distances",
         "doc_type": "location", "chunker": chunk_location},
        {"path": "kb/RON/inventory.md", "title": "RON sellable configurations",
         "doc_type": "inventory", "chunker": chunk_inventory},
    ],
}


def build(brand):
    """Read + chunk every source for a brand. No database, no API."""
    out = []
    for src in SOURCES.get(brand, []):
        full = os.path.join(ROOT, src["path"])
        if not os.path.exists(full):
            print(f"  MISSING  {src['path']}")
            continue
        text = open(full, encoding="utf-8").read()
        chunks = src["chunker"](text)
        # Hash what actually gets STORED, not the source file.
        #
        # This was a bug worth remembering: the hash used to cover `text` alone, so
        # editing the chunker or a guardrail changed nothing the hash could see. The
        # ingest reported "unchanged, skipped" and left the old chunks in place --
        # a silent no-op that looked like success. A negative guardrail written to
        # stop the bot inventing schools simply never reached the database.
        #
        # Hashing the chunk payload means any change to content, chunking strategy
        # or guardrail text produces a new version, which is the whole point of
        # versioning them.
        payload = json.dumps([[c["content"], c.get("guardrail")] for c in chunks],
                             sort_keys=True)
        out.append({**src, "text": text, "hash": _hash(payload), "chunks": chunks})
    return out


def load(brand, docs, dry_run):
    if not kb.available():
        print("kb schema unavailable: " + (db.get_setting("kb_schema_error") or "?"))
        return 1
    if not kb.dim_matches():
        print(f"EMBED_DIM changed (stored {db.get_setting('kb_embed_dim')}, config "
              f"{config.EMBED_DIM}). This needs a full re-index, not an update.")
        return 1

    import embed
    total = 0
    for d in docs:
        prior = db.q("""SELECT id, version, content_hash FROM kb_documents
                        WHERE brand_id=%s AND title=%s AND active
                        ORDER BY version DESC LIMIT 1""",
                     (brand, d["title"]), one=True)
        if prior and prior["content_hash"] == d["hash"]:
            print(f"  unchanged, skipped: {d['title']} (v{prior['version']})")
            continue

        version = (prior["version"] + 1) if prior else 1
        if dry_run:
            print(f"  would write: {d['title']} v{version}, {len(d['chunks'])} chunks")
            continue

        vectors = embed.embed_documents([c["content"] for c in d["chunks"]])

        # Supersede rather than delete: an answer given last week must still be
        # traceable to the text that produced it.
        if prior:
            db.x("UPDATE kb_documents SET active=FALSE WHERE id=%s", (prior["id"],))

        row = db.q("""INSERT INTO kb_documents
                        (brand_id, title, source_path, doc_type, version,
                         uploaded_by, content_hash)
                      VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                   (brand, d["title"], d["path"], d["doc_type"], version,
                    "ingest_kb.py", d["hash"]), one=True)
        doc_id = row["id"]

        for i, (c, v) in enumerate(zip(d["chunks"], vectors)):
            db.x("""INSERT INTO kb_chunks
                      (brand_id, document_id, ordinal, content, guardrail,
                       embed_model, embedding)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                 (brand, doc_id, i, c["content"], c["guardrail"],
                  config.EMBED_MODEL, v))
        total += len(d["chunks"])
        print(f"  loaded: {d['title']} v{version}, {len(d['chunks'])} chunks")

    print(f"\nchunks written: {total}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default="RON")
    ap.add_argument("--dry-run", action="store_true",
                    help="chunk and report only; no database, no API key needed")
    ap.add_argument("--show", type=int, default=0,
                    help="print the first N chunks in full for review")
    args = ap.parse_args()

    if args.brand not in SOURCES:
        print(f"no sources configured for brand {args.brand!r}")
        return 1

    docs = build(args.brand)
    print(f"brand: {args.brand}")
    for d in docs:
        lens = [len(c["content"]) for c in d["chunks"]]
        print(f"  {d['path']:24s} {len(d['chunks']):3d} chunks  "
              f"chars min/median/max: {min(lens) if lens else 0}/"
              f"{sorted(lens)[len(lens)//2] if lens else 0}/{max(lens) if lens else 0}  "
              f"guardrails: {sum(1 for c in d['chunks'] if c['guardrail'])}")
    print(f"  TOTAL {sum(len(d['chunks']) for d in docs)} chunks")
    print()

    if args.show:
        for d in docs:
            for c in d["chunks"][:args.show]:
                print("-" * 70)
                print(c["content"][:600])
                if c["guardrail"]:
                    print(f"  [guardrail] {c['guardrail'][:110]}...")
        print()

    if args.dry_run:
        print("--dry-run: nothing embedded, nothing written")
        return 0
    return load(args.brand, docs, dry_run=False)


if __name__ == "__main__":
    sys.exit(main())
