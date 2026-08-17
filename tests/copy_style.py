"""Shorter, warmer, less interrogative. No database, no API, ~1 second.

    python tests/copy_style.py

Owner, 2026-08-17, after reading a competitor's bot: the copy is pitched too high for
the Indian context and the bot shoots a question every turn.

Measured that day across 623 turns, against that reference conversation:

                     reference    ours     our reply rate
    median length      171 ch     304 ch   120-240ch 71.6% | 240-400 42.8%
    carries a question    30%      81%     none 70.6% | one 47.9%
    uses the name         61%      <1%

Owner's calls: ask a gate every second or third turn, enforce length in code, use the
first name with a junk filter.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bootstrap import Results        # noqa: E402

import config                          # noqa: E402
import conversation as cv              # noqa: E402
import qualifier as q                  # noqa: E402

r = Results()

# --- 1. gate pacing -----------------------------------------------------------
r.eq("default is one gate every 2 turns", config.GATE_EVERY_N_TURNS, 2)
# A new conversation must still ask -- an opener that asks nothing gives the buyer
# nothing to answer, and the first turn is the one everybody receives.
r.check("a brand-new conversation may ask", cv.may_ask_gate({"turns_since_gate": 99}))
r.check("no conversation row at all may ask", cv.may_ask_gate(None))
r.check("missing counter is treated as eligible", cv.may_ask_gate({}))
r.check("the turn right after an ask may NOT ask",
        not cv.may_ask_gate({"turns_since_gate": 0}))
r.check("one turn later still may not", not cv.may_ask_gate({"turns_since_gate": 1}))
r.check("two turns later may ask again", cv.may_ask_gate({"turns_since_gate": 2}))
r.check("long silence may ask", cv.may_ask_gate({"turns_since_gate": 7}))

# --- 2. length ----------------------------------------------------------------
r.eq("cap defaults to 300", config.MAX_REPLY_CHARS, 300)

short = "Nice. It's quiet here - 32 acres, just 343 homes, sea right there."
out, how = q._shorten(short, 300)
r.eq("a short reply is untouched", out, short)
r.check("and reports no change", how is None)

# The real shape of our long replies: answer, then an unasked-for fact, then a nudge.
padded = ("We're on ECR near Kovalam Junction, about 40 minutes from Adyar.\n\n"
          "One thing people like is the man-made beach and lagoon inside the "
          "community, plus a clubhouse of over 1,00,000 sqft with a pool, gym and "
          "a mini theatre.\n\n"
          "Whenever you want to come see it, just tell me a day and I'll set up "
          "the visit - Wednesday to Sunday, or Monday afternoon.")
out, how = q._shorten(padded, 300)
r.check("padding is dropped from the END", out.startswith("We're on ECR"), detail=out)
r.check("the answer survives", "Kovalam" in out, detail=out)
r.check("the visit nudge is gone", "set up the visit" not in out, detail=out)
r.check("result is under the cap", len(out) <= 300, detail=f"{len(out)} chars")
r.check("it says what it did", bool(how), detail=str(how))

# THE BUG A DRY RUN OVER REAL HISTORY CAUGHT. Our long replies are not
# "answer + padding", they are "answer + gate question" -- one paragraph each. So
# trimming from the end deleted the question, which is the opposite of the intent.
# When the turn is allowed to ask, the last paragraph is protected.
answer_then_q = (
    "Hi! Republic of Nature is a 32-acre community on ECR, near Kovalam Junction. "
    "Apartments and villas, plus a 60,000 sqft clubhouse with pool, gym, courts, "
    "mini theatre and spa. Apartments start from Rs 1.28 Cr and villas from "
    "Rs 3.94 Cr.\n\n"
    "Are you looking at this as a weekend place, a primary home or an investment?")
out, how = q._shorten(answer_then_q, 300, keep_last=True)
r.check("the question SURVIVES when the turn may ask", out.rstrip().endswith("?"),
        detail=out)
r.check("and the answer is still there", "Kovalam" in out, detail=out)
r.check("and it fits", len(out) <= 300, detail=f"{len(out)} chars")

# Three paragraphs: answer, unasked-for fact, question. The middle should go.
three = ("We're on ECR near Kovalam Junction, about 40 minutes from Adyar.\n\n"
         "One thing people like is the man-made beach and lagoon inside the "
         "community, plus a clubhouse of over 1,00,000 sqft with a pool, a gym, "
         "tennis and badminton courts, a spa and a mini theatre inside it.\n\n"
         "Which area are you looking to buy around, and how soon?")
r.check("the three-paragraph example is actually over the cap",
        len(three) > 300, detail=f"{len(three)} chars")
out, how = q._shorten(three, 300, keep_last=True)
r.check("the middle is what goes", "man-made beach" not in out, detail=out)
r.check("answer kept", "Kovalam" in out, detail=out)
r.check("question kept", out.rstrip().endswith("?"), detail=out)

# One long paragraph has no trailing padding to drop, so it falls back to sentences.
one_para = ("The homes are 2 and 3 bedroom apartments and 3 and 4 bedroom villas. "
            "Sizes run from about 1220 sqft up to 3634 sqft. There is a clubhouse "
            "of over 1,00,000 sqft. It has a pool, a gym, tennis and badminton "
            "courts, a spa and a mini theatre inside it as well.")
out, how = q._shorten(one_para, 200)
r.check("falls back to whole sentences", len(out) <= 200, detail=f"{len(out)}")
r.check("never ends mid-word", out.endswith((".", "!", "?")), detail=out)
r.check("keeps the first sentence", out.startswith("The homes are"), detail=out)

# A single sentence longer than the cap is left alone: too long is recoverable,
# half a sentence reaching a buyer is not.
mono = "A" * 400 + "."
out, how = q._shorten(mono, 300)
r.check("an unsplittable sentence is left intact", out == mono, detail=str(how))

# --- 3. the name --------------------------------------------------------------
clean = config.clean_first_name
r.eq("plain name", clean("Balaji Subramani"), "Balaji")
r.eq("single name", clean("Akram"), "Akram")
r.eq("lowercase is capitalised", clean("ravi kumar"), "Ravi")
r.eq("emoji stripped from a real name", clean("Priya😍"), "Priya")
# A decorated real name keeps the name. "Muna💞💞💞" is a person called Muna, and
# "Hi Muna" is the warmth this change exists for -- stripping the decoration is the
# right answer, not discarding her. (I first wrote this as a rejection case and the
# implementation was right, not the test.)
r.eq("a decorated name keeps the name", clean("Muna💞💞💞"), "Muna")

# Junk the buyer controls. A message addressed to nobody reads fine; one addressed
# to "Hi 9" does not.
for junk in ("919789988124", "9876543210", "💞💞💞", "test", "N/A",
             "hi", "Guest", "", None, "   ", "A", "x"):
    r.check(f"junk rejected: {junk!r}", clean(junk) is None,
            detail=f"got {clean(junk)!r}")
r.check("a name that was mostly decoration is rejected",
        clean("💞💞Mu💞") is None, detail=repr(clean("💞💞Mu💞")))
r.check("a very long token is rejected",
        clean("Abcdefghijklmnopqrstuvwxyz") is None)

# --- 4. the dash, the biggest register tell ----------------------------------
# 70% of our replies carried it, 0% of the reference. And we manufactured it:
# _PUNCT folds every em/en dash down to " - ".
dd = q._dedash

r.eq("an aside becomes a sentence",
     dd("It's a 32-acre community on ECR, near Kovalam Junction - apartments and "
        "villas, with a big clubhouse."),
     "It's a 32-acre community on ECR, near Kovalam Junction. Apartments and "
     "villas, with a big clubhouse.")
r.eq("an em dash is handled the same way after _PUNCT folds it",
     q._clean_reply("Mornings are quiet — the city never is."),
     "Mornings are quiet. The city never is.")
r.eq("a short trailing fragment becomes a comma, not a one-word sentence",
     dd("Villas start at Rs 3.94 Cr - onwards."),
     "Villas start at Rs 3.94 Cr, onwards.")
r.check("no dash survives a normal reply",
        " - " not in dd("343 homes across 32 acres - mornings are quiet here."),
        detail=dd("343 homes across 32 acres - mornings are quiet here."))
r.eq("text with no dash is untouched",
     dd("Sure, Ravi. The clubhouse is over 1,00,000 sqft."),
     "Sure, Ravi. The clubhouse is over 1,00,000 sqft.")
r.eq("empty stays empty", dd(""), "")
r.check("None does not raise", dd(None) is None)
r.eq("a hyphenated word is NOT split",
     dd("It's a low-density community with a man-made beach."),
     "It's a low-density community with a man-made beach.")
r.eq("a negative number range is not split",
     dd("2552-2612 sqft for the 3 bedroom."), "2552-2612 sqft for the 3 bedroom.")

# The rulebook must carry the same rule in words, or the model keeps writing them
# and the code quietly cleans up after it every single turn.
import answering                        # noqa: E402
lang = answering.RULES["language"]
r.check("the rulebook bans dashes outright", "Dashes. Any of them" in lang)
r.check("the rulebook names the softeners", "softeners" in lang.lower())
r.check("the rulebook drops the British openers", "read as British" in lang)

# --- 5. the framings obey the rules they sit under ---------------------------
# These clauses ride on most questions the bot asks, so they were a large share of
# what made the copy read as written rather than spoken. Four of the twelve broke
# the style rules in the same document that forbids them: two carried em dashes,
# and "quite" and "genuinely" appeared. Checked here so an edit cannot bring them
# back quietly.
BANNED_WORDS = ("really", "actually", "quite", "genuinely", "rather", "truly")
for gate, framings in config.FRAMINGS.items():
    r.eq(f"{gate}: three framings", len(framings), 3)
    for i, f in enumerate(framings):
        tag = f"{gate}[{i}]"
        r.check(f"{tag} has no dash", not re.search(r"\s[-–—]\s", f), detail=f)
        r.check(f"{tag} has no em or en dash at all",
                "—" not in f and "–" not in f, detail=f)
        for w in BANNED_WORDS:
            if re.search(rf"\b{w}\b", f, re.I):
                r.check(f"{tag} avoids '{w}'", False, detail=f)
        r.check(f"{tag} is 12 words or fewer", len(f.split()) <= 12,
                detail=f"{len(f.split())}w: {f}")
        r.check(f"{tag} starts lowercase (it continues a sentence)",
                f[:1].islower(), detail=f)
        r.check(f"{tag} has no trailing full stop", not f.rstrip().endswith("."),
                detail=f)

avg = sum(len(f.split()) for fs in config.FRAMINGS.values() for f in fs) / 12
r.check(f"framings average under 11 words (got {avg:.1f})", avg < 11,
        detail="they averaged 14 before this rewrite")

sys.exit(0 if r.report("COPY STYLE RULES") else 1)
