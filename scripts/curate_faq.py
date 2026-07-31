"""Curate `RON Faqs.xlsx` into a buyer-safe corpus file.

Run:  python scripts/curate_faq.py            # writes kb/RON/faqs.md + audit
      python scripts/curate_faq.py --audit     # print the audit only, write nothing

WHY THIS IS A SCRIPT AND NOT A HAND-EDITED FILE
-----------------------------------------------
Sales owns this content (design §3) and the FAQ will be updated. A hand-curated
copy would drift from the source the first time somebody fills in a blank row, and
nobody would know which version the bot was answering from. Re-running this is the
whole maintenance procedure.

Every exclusion is attributable to a named rule, and the audit prints them all. The
rules come from `kb/RON/curation-rules.md`, which is the owner-approved document;
this file is its enforcement.
"""
import argparse
import hashlib
import os
import re
import sys

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCE = os.path.join(os.path.dirname(ROOT), "RON Faqs.xlsx")
OUT = os.path.join(ROOT, "kb", "RON", "faqs.md")

# --- rules -------------------------------------------------------------------

# Internal chatter. A bot with these in its corpus reads staff conversation aloud
# to a buyer. "Not to be answered" is an explicit instruction and is the single
# most important phrase in this list.
# The `already*` and `as in the spec` variants were added after task 11 surfaced
# them as the TOP retrieved answer for "what configurations are available?" and
# "what amenities are there?" -- two of the commonest buyer questions. Matching
# "already given" but not "already briefed" is the kind of gap that only shows up
# when you look at real retrieval output.
CHATTER = re.compile(
    r"kk to come up|to come up with|not understood|cant understand|can't understand"
    r"|to be discussed|really necessary|shared on email|refer to the spec"
    r"|refer spec|not sure of this|not sure what this means|not to be answered"
    r"|already\s+(given|briefed|answered|discussed|shared|explained)"
    r"|as in the spec|spec email|answered in question|sales presentation"
    r"|if then we will updated|price sheet"
    # Deferrals and instructions to staff. Added after "price enna" retrieved
    # "To be Checked with Shakti - But Sales should ideally avoid this questions"
    # as its TOP answer: a colleague's name plus an instruction to dodge the
    # question, ready to be read aloud to a buyer.
    r"|to be checked|check(ed)? with|confirm with|revert with|will check"
    r"|sales should|should ideally|should avoid|avoid this question"
    r"|to be confirmed|need to confirm|yet to be|not decided|will decide"
    # Round four, found by reading all 72 surviving answers rather than by
    # guessing more patterns. Each of these was phrased differently enough to slip
    # the previous list; one of them named a colleague, one instructed the reader
    # to withhold information, one coached a salesperson on what to emphasise.
    r"|did not understand|could not understand|couldn't understand"
    r"|not to be disclosed|awaiting details|to be understood|tentative"
    r"|covered in the|always emphasi|what happens if",
    re.I)

# An answer that trails off is worse than none: the bot states half a fact with
# whole confidence. Catches "Phase 3 -", "Roof of Apartments and Villas / / ---".
TRAILING_JUNK = re.compile(r"[-–/,:]\s*$|-{2,}\s*$|Phase\s*\d\s*[-–]\s*$", re.I)

# A question that asks WHAT / WHICH / HOW MANY, answered with a bare yes/no or a
# shrug, is worse than no answer at all: the bot sounds like it responded while
# telling the buyer nothing. "What concierge services are we providing?" -> "Yes".
# Escalating to a human is the better outcome, so these are excluded.
OPEN_QUESTION = re.compile(r"^\s*(what|which|how many|how much|where|list|detail|brands? of)",
                           re.I)
NON_ANSWERS = {"yes", "no", "na", "n/a", "not applicable", "none", "nil",
               "yes.", "no.", "tbd", "-"}

# Price, cost and commercial commitments. RON publishes none of it, so the budget
# gate is internal arithmetic and every price question escalates. Matched on the
# QUESTION as well as the answer: "what is the maintenance cost" is a price
# question even when the answer looks like a harmless number.
# "maintenance cost", not bare "maintenance": WHO maintains the common areas is a
# responsibility question with a perfectly safe answer, and the blunt version
# excluded it.
PRICE = re.compile(
    r"\bcost\b|\bprice\b|\bcharges?\b|\bfees?\b|\bemi\b|payment plan|\brupees?\b"
    r"|\blakh|\bcrore|₹|rental assurance|lease guarantee|roi\b|resale value"
    r"|booking amount|maintenance cost|maintenance charge",
    re.I)

# Possession / handover. Owner decision 2026-07-30: never stated.
# NOT "rera": RERA is a regulator, not a date. Including it wrongly excluded the
# carpet-area answer and the construction warranty, both of which are safe and
# useful ("as per RERA norms" reveals no possession claim).
HANDOVER = re.compile(
    r"handover|hand over|possession|completion|when can i move|ready to move",
    re.I)

# Apartment sizes: source holds two conflicting floors (818 "Actual" vs 1220 "For
# Sales Person") and it is unresolved whether they are two products. Villa sizes
# are unambiguous and stay in.
# Apartments only. "starting to ending sizes" as a pattern also matched the VILLA
# size row, whose numbers (2552-3643 sqft) are unambiguous and perfectly safe.
APT_SIZE = re.compile(r"sizes? in apartments?", re.I)

# Answers that are cut off mid-sentence in the source. A truncated fact is worse
# than no fact -- the bot would state half of it with full confidence.
TRUNCATED_ROWS = {79}          # "Phase 2  - has a comm"

# Judgement calls that no regex should try to encode, each with its reason. Kept
# small and visible on purpose -- an override list that grows silently is how a
# curation rule stops meaning anything.
FORCE_INCLUDE = {
    51: 'question mentions charges as an aside; the answer names only the water '
        'source ("Panchayat supply, Bore, tankers") and says nothing commercial',
    99: 'answer is a flat "No" — declining a lease guarantee publishes no '
        'commercial information',
}
FORCE_EXCLUDE = {}

# Buyer-facing vocabulary. Source terms nobody outside the office would recognise.
#
# CORRECTED 2026-07-31. This previously rewrote C2BHK to "2BHK", which was wrong in a
# way that would have surfaced at a site visit: the owner's price sheet lists Compact
# 2BHK (1220/1250 sqft) and 2BHK (1422 sqft) as SEPARATE products, ~200 sqft and
# ~18 lakh apart. Collapsing them meant a buyer asking for a 2BHK could be shown a
# compact one. "C2BHK" is still never spoken -- it is internal shorthand -- but the
# expansion has to preserve the distinction.
TRANSFORMS = [
    (re.compile(r"\bC2\s?BHK\b", re.I), "Compact 2BHK"),
]

MIN_ANSWER_CHARS = 2


def load_rows():
    wb = openpyxl.load_workbook(SOURCE, data_only=True)
    ws = wb["Sheet1"]
    out = []
    for i, r in enumerate(ws.iter_rows(min_row=3, values_only=True), start=3):
        q = r[1]
        if not q:
            continue
        a = str(r[4]).strip() if r[4] else ""
        out.append({"xlsx_row": i, "question": str(q).strip(), "answer": a})
    return out


def faq_number(question):
    m = re.match(r"\s*\.?\s*(\d+)\s*\.", question)
    return int(m.group(1)) if m else None


def clean_question(question):
    return re.sub(r"^\s*\.?\s*\d+\s*\.\s*", "", question).strip()


def classify(row):
    """(verdict, rule, note). verdict is 'include' or 'exclude'."""
    q, a = row["question"], row["answer"]
    xr = row["xlsx_row"]

    if not a or len(a) < MIN_ANSWER_CHARS:
        return "exclude", "blank", "no answer in the source"

    # Overrides sit after the blank check -- an override must never resurrect a row
    # with nothing in it -- and before every content rule.
    if xr in FORCE_EXCLUDE:
        return "exclude", "override_exclude", FORCE_EXCLUDE[xr]
    if xr in FORCE_INCLUDE:
        return "include", "override_include", FORCE_INCLUDE[xr]

    if row["xlsx_row"] in TRUNCATED_ROWS or TRAILING_JUNK.search(a.strip()):
        return "exclude", "truncated", "answer is cut off mid-sentence in the source"

    if CHATTER.search(a):
        return "exclude", "internal_chatter", "written for a colleague, not a buyer"

    if OPEN_QUESTION.match(clean_question(q)) and a.strip().lower() in NON_ANSWERS:
        return "exclude", "non_answer", \
               "an open question answered with a bare yes/no tells the buyer nothing"

    if HANDOVER.search(q) or HANDOVER.search(a):
        return "exclude", "handover_dates", "owner: never state handover or possession"

    if APT_SIZE.search(q):
        return "exclude", "apartment_sizes", "source holds two conflicting floors (818 vs 1220); open with sales"

    if PRICE.search(q) or PRICE.search(a):
        return "exclude", "price", "nothing commercial is published; escalates by design"

    return "include", "", ""


def transform(text):
    """Apply buyer-facing vocabulary substitutions.

    A `dedupe_config_lists` helper used to live here and has been DELETED, not fixed.
    It existed only because the old rename collapsed C2BHK into 2BHK, which produced
    "Apts 2BHK, 2BHK, 3BHK" in FAQ row 6. With the correct expansion the line reads
    "Apts Compact 2BHK, 2BHK, 3BHK" -- no duplicate, nothing to collapse.

    Keeping it would have been actively harmful: its adjacent-repeat rule would have
    matched "Compact 2BHK" followed by "2BHK" and deleted the real 2BHK product. A
    workaround for a wrong transform becomes a bug the moment the transform is right.
    """
    for pat, repl in TRANSFORMS:
        text = pat.sub(repl, text)
    return text


def curate():
    rows = load_rows()
    included, excluded = [], []
    for r in rows:
        verdict, rule, note = classify(r)
        r["rule"], r["note"] = rule, note
        if verdict == "include":
            r["q_clean"] = transform(clean_question(r["question"]))
            r["a_clean"] = transform(r["answer"])
            r["transformed"] = (r["a_clean"] != r["answer"]
                                or r["q_clean"] != clean_question(r["question"]))
            included.append(r)
        else:
            excluded.append(r)
    return rows, included, excluded


def write_corpus(included, excluded, total):
    by_rule = {}
    for r in excluded:
        by_rule.setdefault(r["rule"], []).append(r)

    body = []
    body.append("# RON — buyer-facing FAQ corpus\n")
    body.append("**GENERATED FILE — do not edit by hand.**")
    body.append("Produced by `scripts/curate_faq.py` from `RON Faqs.xlsx`.")
    body.append("Rules live in `kb/RON/curation-rules.md`. To change what the bot may")
    body.append("say, edit the rules or the source spreadsheet and re-run the script.\n")
    body.append(f"- Source rows: **{total}**")
    body.append(f"- Included: **{len(included)}**")
    body.append(f"- Excluded: **{len(excluded)}**\n")
    body.append("Every excluded topic has no source, so the confidence floor fires and it")
    body.append("**escalates to a human by design**. That is correct behaviour, not a gap —")
    body.append("but it is human workload.\n")
    body.append("---\n")

    for r in included:
        n = faq_number(r["question"])
        body.append(f"## {r['q_clean']}\n")
        body.append(f"{r['a_clean']}\n")
        tag = f"<!-- faq:{n} xlsx:{r['xlsx_row']}"
        if r["transformed"]:
            # The marker must NOT name the internal term it removed. A naive chunker
            # would otherwise pull "C2BHK" into the corpus via the provenance
            # comment -- defeating the transform it is recording.
            tag += " transformed:config-vocab"
        body.append(tag + " -->\n")

    body.append("---\n")
    body.append("## Excluded rows, by rule\n")
    for rule in sorted(by_rule):
        rs = by_rule[rule]
        body.append(f"### `{rule}` — {len(rs)} row(s)")
        body.append(f"_{rs[0]['note']}_\n")
        for r in rs:
            q = clean_question(r["question"])[:110]
            a = (r["answer"] or "")[:70].replace("\n", " / ")
            body.append(f"- **{q}**" + (f"  \n  source answer: `{a}`" if a else ""))
        body.append("")

    text = "\n".join(body) + "\n"
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    return OUT, hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true", help="print the audit, write nothing")
    args = ap.parse_args()

    if not os.path.exists(SOURCE):
        print(f"source not found: {SOURCE}", file=sys.stderr)
        return 1

    rows, included, excluded = curate()

    by_rule = {}
    for r in excluded:
        by_rule.setdefault(r["rule"], []).append(r)

    print(f"source rows : {len(rows)}")
    print(f"included    : {len(included)}")
    print(f"excluded    : {len(excluded)}")
    print()
    print("exclusions by rule:")
    for rule in sorted(by_rule, key=lambda k: -len(by_rule[k])):
        print(f"  {rule:20s} {len(by_rule[rule]):3d}   {by_rule[rule][0]['note']}")
    print()
    for rule in sorted(by_rule):
        print(f"--- {rule} ---")
        for r in by_rule[rule]:
            print(f"  xlsx{r['xlsx_row']:4d}  {clean_question(r['question'])[:74]}")
        print()

    tf = [r for r in included if r["transformed"]]
    print(f"vocabulary transforms applied: {len(tf)}")
    for r in tf:
        print(f"  xlsx{r['xlsx_row']:4d}  {clean_question(r['question'])[:66]}")
    print()

    if args.audit:
        print("--audit: nothing written")
        return 0
    path, digest = write_corpus(included, excluded, len(rows))
    print(f"written: {path}")
    print(f"content hash: {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
