"""Every rule the bot must obey, as a test. No database, no API, ~1 second.

WHY THIS EXISTS. Owner, 2026-08-02, after the third round of live testing: "why are
we applying bandaids after bandaids". Every defect that day was found by a person
messaging the bot from their own phone. That is a slow, expensive way to learn that
a regex was wrong.

So each of those defects is written down here as a case. A change is now checked in
a second, before a buyer sees it.

A case marked known_bug=True is a defect we KNOW about and have not yet fixed. It
prints separately and does not fail the run -- so an open defect stays visible
instead of being either forgotten or silently tolerated.
"""
from _bootstrap import Results

import config
import conversation as cv
import knocks
import qualifier as q

R = Results()
CR = 10000000


# --------------------------------------------------------------------------
# Configuration and price qualify TOGETHER
# --------------------------------------------------------------------------
def _bar(cfg, budget, location="Adyar"):
    return cv.clears_the_bar({"checklist": {"configuration": cfg, "budget": budget,
                                            "location": location}})


def test_qualification():
    # THE ONE THAT LOST A BUYER. 3.5 Cr for a villa clears the 3.94 Cr floor once
    # stretched. The bot said "that sits a little above your band" and downsold.
    R.eq("villa @3.5cr qualifies (the downsell bug)", _bar("3 bedroom villa", 35 * CR // 10)[0], True)
    R.eq("villa @1.2cr does not", _bar("villa", 12 * CR // 10)[0], False)
    R.eq("compact 2bhk @1.2cr qualifies on the 25% stretch", _bar("compact 2bhk", 12 * CR // 10)[0], True)
    R.eq("3bhk @1.5cr does not reach", _bar("3BHK apartment", 15 * CR // 10)[0], False)
    R.eq("3bhk @2.0cr reaches", _bar("3BHK apartment", 2 * CR)[0], True)
    R.eq("4 bed villa @4cr does not reach", _bar("4 bed villa", 4 * CR)[0], False)

    ok, why = _bar("3BHK apartment", 15 * CR // 10)
    R.check("a short budget names the best fit for the pivot", "2BHK apartment" in why, why)

    # Configuration is a HARD GATE: no configuration, no qualification.
    R.eq("no configuration -> not qualified",
         cv.clears_the_bar({"checklist": {"budget": 2 * CR, "location": "Adyar"}})[0], False)
    R.eq("no location -> not qualified", _bar("villa", 5 * CR, location=None)[0], False)
    R.eq("no budget -> not qualified",
         cv.clears_the_bar({"checklist": {"configuration": "villa", "location": "A"}})[0], False)


def test_configuration_classifier():
    # OFF-CATEGORY MUST NOT BE PRICED. "island villa" contains "villa" and "1BHK"
    # contains "bhk"; both were quoted a floor for a product we cannot sell.
    for txt in ("1BHK", "1 bhk apartment", "one bhk", "villament", "island villa",
                "beachfront villa", "5BHK", "not sure", ""):
        R.eq(f"off-category not priced: {txt!r}", config.classify_configuration(txt)[0], None)
    for txt, want in (("villa", "3 bed villa"), ("4BHK villa", "4 bed villa"),
                      ("3bhk apartment", "3BHK apartment"), ("2 BHK", "2BHK apartment"),
                      ("compact 2bhk", "Compact 2BHK apartment"),
                      ("apartment", "Compact 2BHK apartment")):
        R.eq(f"classify {txt!r}", config.classify_configuration(txt)[0], want)


# --------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------
PRICE_CHUNK = {"id": 423, "content":
               "starting prices: Compact 2BHK ₹1.28 Cr onwards; 2BHK ₹1.46 Cr "
               "onwards; 3BHK ₹2.1 Cr onwards; 3 bed villa ₹3.94 Cr onwards; "
               "4 bed villa ₹5.5 Cr onwards.", "guardrail": "say from/starting"}
SIZE_CHUNK = {"id": 364, "content": "Villas are 2552 to 3634 sqft."}
CHUNKS = [PRICE_CHUNK, SIZE_CHUNK]


def test_price_guard():
    def problem(reply, cited=(423,), msg="what is the price"):
        return q._price_problem(reply, CHUNKS, list(cited), msg)

    R.check("cited + framed passes", problem("Villas start from ₹3.94 Cr.") is None)
    R.check("onwards passes", problem("3 bedroom apartments ₹2.1 Cr onwards.") is None)
    R.check("two starting prices pass",
            problem("Apartments from ₹1.28 Cr and villas from ₹3.94 Cr.") is None)
    R.check("the buyer's own figure passes",
            problem("₹2 crore suits our apartments.", (364,), "my budget is 2 crore") is None)

    R.check("INVENTED figure escalates", problem("Villas start from ₹2.75 Cr.") is not None)
    R.check("uncited figure escalates", problem("Villas from ₹3.94 Cr.", ()) is not None)
    R.check("flat price escalates", problem("A villa is ₹3.94 Cr.") is not None)
    R.check("per-sqft escalates", problem("About ₹12000 per sq ft, starting.") is not None)
    R.check("range with a top escalates",
            problem("Villas run from ₹3.94 Cr to ₹5.5 Cr.") is not None)
    R.check("no money at all is fine", problem("Villas are 2552 sqft.", (364,)) is None)


def test_say_a_price_once():
    hist = [{"role": "assistant",
             "content": "3 bed villas from ₹3.94 Cr and 4 bed from ₹5.5 Cr."}]
    out = " ".join(q._already_quoted(hist))
    R.check("already-quoted names the figures", "3.94" in out and "5.5" in out, out)
    R.eq("nothing quoted yet -> silent", q._already_quoted(
        [{"role": "assistant", "content": "Villas are 2552 sqft."}]), [])
    R.eq("the BUYER's figure is not ours to suppress", q._already_quoted(
        [{"role": "user", "content": "my budget is 2 crore"}]), [])


def test_affordability_is_decided_for_the_model():
    reach = " ".join(q._affordability_verdict(
        {"budget": 35 * CR // 10, "configuration": "3 bedroom villa"}))
    R.check("REACHES is stated plainly", "REACHES" in reach, reach)
    R.check("and forbids a downsell", "cheaper" in reach.lower(), reach)
    short = " ".join(q._affordability_verdict(
        {"budget": 15 * CR // 10, "configuration": "3BHK apartment"}))
    R.check("short budget names the fallback", "2BHK apartment" in short, short)
    R.eq("incomplete checklist stays silent",
         q._affordability_verdict({"budget": 2 * CR}), [])


# --------------------------------------------------------------------------
# Things that must never reach a buyer
# --------------------------------------------------------------------------
def test_locality_never_spoken():
    for src, want_absent in (("We're at Vadanemmeli on ECR.", "Vadanemmeli"),
                             ("The site at Vadanemmeli is open.", "Vadanemmeli"),
                             ("Our project in Vadanemmeli has 3 phases.", "Vadanemmeli")):
        out = q._rename_locality(src)
        R.check(f"locality removed from {src[:28]!r}", want_absent not in out, out)
        R.check("...and replaced with the positioning line",
                "Kovalam Junction" in out, out)
    R.eq("text without it is untouched", q._rename_locality("Neelankarai is 23 km away."),
         "Neelankarai is 23 km away.")


def test_mall_locked_to_a_real_objection():
    mall = ("We're on ECR. If the drive feels long, we also have an Experience Centre "
            "at Express Avenue. Would that suit?")
    R.eq("a distance QUESTION is not an objection",
         q._raised_distance("How far is this from Adyar", []), False)
    R.eq("a statement of difficulty IS", q._raised_distance("that is too far to drive", []), True)
    R.eq("...and so is 'long drive'", q._raised_distance("that's a long drive for us", []), True)
    stripped = q._strip_mall(mall)
    R.check("the mall offer is removable", "Express Avenue" not in stripped, stripped)
    R.check("...leaving the real answer", "ECR" in stripped, stripped)


def test_corruption_is_refused_not_repaired():
    NL = chr(10)
    R.check("control character", q._looks_corrupt("Perfect " + chr(8) + "ness of it.") is not None)
    R.check("form feed", q._looks_corrupt("Good " + chr(12) + "fit, then.") is not None)
    R.check("line break mid-sentence",
            q._looks_corrupt("for you " + NL + "ding: will this be a weekend place?") is not None)
    R.check("ends mid-word",
            q._looks_corrupt("Our villas are three bed and the clubhouse ha") is not None)
    R.check("a clean reply passes",
            q._looks_corrupt("We're on ECR, near Kovalam Junction. Weekend or full-time?") is None)
    R.check("a legitimate bullet list passes",
            q._looks_corrupt("Two options:" + NL + "- 3 bed villa" + NL + "- 4 bed villa") is None)
    R.check("a legitimate paragraph break passes",
            q._looks_corrupt("Happy to help." + NL + "Would a weekend morning suit?") is None)
    # KNOWN GAP: words can go missing with no control character and no truncation.
    R.check("words missing, no marker -- undetectable",
            q._looks_corrupt("share the most relevant details he home yourself on weekends,"
                             " or is this more of an investment view?") is not None,
            "no mechanical signal exists for this; see why-we-keep-patching",
            known_bug=True)


def test_garbled_is_retried_not_escalated():
    """2026-08-03: a real villa lead tapped "Need More Details" 40 seconds into
    their first conversation. The model produced "...near Kovalam Junction \\ronking
    about it \\\\ two hundred - it's a coastal community...". The guard correctly
    refused to send it and then ESCALATED, so the buyer was handed to a human and
    the bot went quiet. Corruption is a stutter, not a judgement."""
    real = ("Happy to help. Republic of Nature is on ECR, near Kovalam Junction "
            + chr(13) + "onking about it " + chr(10) + "two hundred - a coastal "
            "community with apartments and villas.")
    R.check("the real 2026-08-03 reply is caught", q._looks_corrupt(real) is not None)
    R.check("a bare carriage return is a control character",
            q._looks_corrupt("Kovalam Junction " + chr(13) + "onking") is not None)
    for reason in ("control character in reply", "line break mid-sentence",
                   "reply ends mid-sentence", "unparseable decision",
                   "no text in response", "response truncated"):
        R.check(f"retried, not escalated: {reason}", reason in q._GARBLED)
    for reason in ("model declined the request", "factual claim with no supporting chunk",
                   "reply contained a price range with a top",
                   "mall offered with no distance objection"):
        R.check(f"a judgement stands, never retried: {reason}", reason not in q._GARBLED)
    R.check("more than one extra attempt", q.GARBLE_RETRIES >= 1, str(q.GARBLE_RETRIES))


def test_never_congratulate_a_non_answer():
    R.eq("'Great.' after 'Yes' is stripped",
         q._strip_empty_affirmation("Great. Which part of Chennai?", "Yes"),
         "Which part of Chennai?")
    R.eq("'Perfect' after a real answer is kept",
         q._strip_empty_affirmation("Perfect, that helps. A colleague will call.", "1cr to 1.8 cr"),
         "Perfect, that helps. A colleague will call.")
    R.eq("an affirmation that is the whole reply is left alone",
         q._strip_empty_affirmation("Great.", "yes"), "Great.")


def test_greeting_never_mojibake():
    R.eq("decorated unicode -> there",
         knocks._first_name("ꣁ\U00011288\U00012786\U0001d418\U0001d420 \U0001d40d\U0001d400"),
         "there")
    R.eq("sell.do suffix stripped", knocks._first_name("Kothai Kannan (#53912)"), "Kothai")
    R.eq("doubled name", knocks._first_name("paul paul"), "paul")
    R.eq("empty -> there", knocks._first_name(""), "there")
    R.eq("None -> there", knocks._first_name(None), "there")


# --------------------------------------------------------------------------
# Cadence
# --------------------------------------------------------------------------
def test_knock_spacing():
    R.eq("first knock has no gap", knocks._min_gap_days(0), 0)
    R.eq("t2 waits 3 days after t1", knocks._min_gap_days(1), 3)
    R.eq("t3 waits 7 days after t2", knocks._min_gap_days(2), 7)
    R.eq("t6 waits 15 days after t3", knocks._min_gap_days(3), 15)


# --------------------------------------------------------------------------
# THE OPEN DEFECT. Left failing on purpose.
# --------------------------------------------------------------------------
def test_below_entry_should_not_kill_a_live_buyer():
    """2026-08-02: the bot said "if you can stretch a little", the buyer said
    "Okay", and had already been marked dead and suppressed. Awaiting the owner's
    decision: escalate to a human, or kill? Until then this stays visible."""
    budget = 1 * CR
    R.check("dead rule ignores the stretch every other rule applies",
            budget >= config.BUDGET_FLOOR / config.BUDGET_STRETCH,
            f"{budget} stretched = {int(budget * config.BUDGET_STRETCH)}, "
            f"floor {config.BUDGET_FLOOR}; rule 5 compares the RAW floor",
            known_bug=True)


def main():
    for fn in (test_qualification, test_configuration_classifier, test_price_guard,
               test_say_a_price_once, test_affordability_is_decided_for_the_model,
               test_locality_never_spoken, test_mall_locked_to_a_real_objection,
               test_corruption_is_refused_not_repaired, test_garbled_is_retried_not_escalated,
               test_never_congratulate_a_non_answer, test_greeting_never_mojibake,
               test_knock_spacing, test_below_entry_should_not_kill_a_live_buyer):
        fn()
    return R.report("RULES  (no database, no API)")


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
