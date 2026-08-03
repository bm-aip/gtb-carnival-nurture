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
import os

from _bootstrap import Results

import answering
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
# Possession / handover dates. The design doc calls an invented handover date the
# worst thing this bot can produce, and until 2026-08-03 the ban was prompt-only:
# FACTUAL had no date vocabulary, so "Handover is expected by December 2027"
# looked non-factual, skipped the citation rule, and could be sent uncited.
# --------------------------------------------------------------------------
def test_possession_dates_are_refused():
    bad = [
        "Handover is expected by December 2027.",
        "Possession is scheduled for Q3 2028.",
        "The villas will be ready to move in 18 months.",
        "We hand over the apartments in mid-2027.",
        "Completion is targeted for end of next year.",
        "Move-in is planned for March 2029.",
    ]
    for reply in bad:
        R.check(f"refused: {reply[:44]!r}", q._possession_problem(reply) is not None)
        R.check(f"...and now looks factual: {reply[:30]!r}", q._looks_factual(reply))

    # FALSE POSITIVES ARE THE REAL RISK HERE. Booking a site visit is the bot's job
    # and every booking names a day, so a guard that trips on dates would escalate
    # the win. None of these may match.
    fine = [
        "Saturday at 11am works, I'll book that for you.",
        "Monday afternoon is fine, the team will call to confirm timing.",
        "Any day from Wednesday to Sunday works. Which suits you?",
        "You may visit whenever suits you.",                 # bare 'may' is not a month
        "The 3 bedroom villas are 2552 sqft and start from ₹3.94 Cr onwards.",
        "The clubhouse is 60,000 sqft with a mini theatre.",
        "Phase 2 sits closer to the lagoon.",
        "It's ready to move whenever you are.",              # no time expression
        "We're open Wednesday to Sunday, 10am to 6pm.",
    ]
    for reply in fine:
        R.check(f"allowed: {reply[:44]!r}", q._possession_problem(reply) is None)


# --------------------------------------------------------------------------
# The qualified card is sent ONCE. The escalate branch always had this guard;
# the qualified branch did not, and it matters more -- the qualified queue is what
# sales judges us on.
# --------------------------------------------------------------------------
def test_qualified_card_is_sent_once():
    src = open(os.path.join(os.path.dirname(__file__), "..", "handoff.py"),
               encoding="utf-8").read()
    branch = src.split('if action == "qualified":')[1]

    R.check("the qualified branch checks it has not already notified",
            "handoff_sent_at" in branch,
            "no re-fire guard: the bot keeps talking after qualifying, so the model "
            "can report `qualified` again and sales gets the same card every turn")
    R.check("...and relabels a previously-escalated lead instead of re-firing",
            'upgrade_from="escalated"' in branch,
            "escalated is write-once, so without this the outcome column keeps "
            "saying escalated and every later turn falls through and sends a card")
    R.check("escalated -> qualified is a NAMED transition, not a general permission",
            cv.UPGRADABLE == ("nurture",),
            f"UPGRADABLE is {cv.UPGRADABLE}")


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


def test_the_rulebook_loads_and_fails_loudly():
    """content/answering-rules.md is now the bot's mouth. A section quietly missing
    would mean answering a real buyer without its price rules, so a bad edit must
    stop the process rather than degrade the conversation."""
    import io as _io
    import answering as a

    R.eq("every section present", sorted(a.RULES) == sorted(a.SECTIONS.values()), True)
    R.check("the prompt is assembled from the document",
            len(a.system_prompt("Republic of Nature")) > 4000)
    R.check("the escalation sentence comes from the document",
            a.RULES["escalation_reply"].startswith("Let me have someone"),
            a.RULES["escalation_reply"])
    R.check("qualifier uses it", q._forced_escalation("x", [])["reply"]
            == a.RULES["escalation_reply"])

    doc = _io.open(a.__file__.replace("answering.py", "content/answering-rules.md"),
                   encoding="utf-8").read()

    # A RENAMED HEADING is the classic silent edit: the section vanishes and nothing
    # complains. It must report BOTH the missing field and the orphaned heading.
    renamed = doc.replace("## Talking about price", "## Talking about pricing")
    try:
        a.validate(*a.parse(renamed))
        R.check("a renamed heading raises", False, "no error raised")
    except a.RulesError as e:
        R.check("a renamed heading names the missing section",
                "Talking about price" in str(e), str(e)[:200])
        R.check("...and the orphaned heading", "does nothing" in str(e), str(e)[:200])

    # An emptied section must not be silently defaulted.
    gutted = doc.replace("Let me have someone from our team come back to you on this.", "")
    try:
        a.validate(*a.parse(gutted))
        R.check("an emptied section raises", False, "no error raised")
    except a.RulesError:
        R.check("an emptied section raises", True)

    # `>` notes are for whoever edits the file and must never reach the model.
    parsed, _ = a.parse(doc)
    R.check("editor notes are stripped",
            "WHAT IS NOT IN THIS FILE" not in " ".join(parsed.values()))

    # The document must NOT restate what the code enforces -- two copies drift.
    body = " ".join(parsed.values())
    for leaked in ("39400000", "12800000", "1.25", "BUDGET_STRETCH", "CONFIG_FLOORS"):
        R.check(f"the document does not restate {leaked!r}", leaked not in body,
                "a rule the code enforces must not also live in prose")


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
# One floor, stretched. The bug this replaces: rule 5 compared the RAW figure
# while clears_the_bar compared the STRETCHED one, so buyers in the gap between
# the two were killed and suppressed by the rule that ran first.
# --------------------------------------------------------------------------
def _rule5(budget):
    """Run only the budget gate. Returns the action it left behind."""
    d = {"action": "answer", "reply": "Sure.", "sources": [], "budget_inr": budget}
    return q._enforce(d, (), {"id": 1, "project": "RON"}, message="", history=[])["action"]


def test_one_floor_and_it_is_stretched():
    R.eq("there is only one entry floor, read off CONFIG_FLOORS",
         config.ENTRY_FLOOR, config.CONFIG_FLOORS[0][1])
    R.check("and no second floor survives anywhere",
            not hasattr(config, "BUDGET_FLOOR"),
            "config.BUDGET_FLOOR still exists -- two floors is the whole defect")

    # ₹1.1 cr: stretched = ₹1.375 cr, which reaches the ₹1.28 cr entry apartment.
    # This is the buyer the raw comparison was killing.
    R.eq("1.1cr is NOT killed -- stretched it reaches the entry apartment",
         _rule5(11 * CR // 10), "answer")
    R.eq("...and clears_the_bar agrees, which is the point",
         cv.clears_the_bar({"checklist": {"budget": 11 * CR // 10, "location": "ECR",
                                          "configuration": "compact 2bhk"}})[0], True)


# --------------------------------------------------------------------------
# Below the entry price is NURTURE, not death. Owner 2026-08-03: "the logic here
# is not to reject but to nurture and see if they are willing to make the jump".
# --------------------------------------------------------------------------
def test_below_entry_is_nurtured_not_killed():
    # 80 lakh stretched is ₹1 cr, genuinely short of the ₹1.28 cr entry.
    R.eq("80 lakh is nurtured, not killed", _rule5(8000000), "nurture")
    R.eq("...and 20 lakh too, however far below", _rule5(2000000), "nurture")

    R.eq("a reachable budget is left alone", _rule5(2 * CR), "answer")

    # A better exit must survive. Someone who low-balls AND books a visit has given
    # us the visit; the number is worth less than the appointment.
    for better in ("qualified", "visit_booked", "escalate"):
        d = {"action": better, "reply": "Sure.", "sources": [], "budget_inr": 8000000}
        R.eq(f"{better} is not downgraded to nurture",
             q._enforce(d, (), {"id": 1, "project": "RON"}, message="", history=[])["action"],
             better)

    # The model must never pick this itself -- it is arithmetic, not judgement.
    actions = q.DECISION_SCHEMA["properties"]["action"]["enum"]
    R.check("nurture is NOT offered to the model", "nurture" not in actions,
            f"schema enum is {actions}")


def test_nurture_never_suppresses_and_can_be_upgraded():
    R.check("nurture is the only provisional outcome", cv.UPGRADABLE == ("nurture",),
            f"UPGRADABLE is {cv.UPGRADABLE}; qualified/escalated/dead must stay "
            f"write-once or an escalation would demote a lead sales already has")

    src = open(os.path.join(os.path.dirname(__file__), "..", "handoff.py"),
               encoding="utf-8").read()
    nurture_branch = src.split('if action == "nurture":')[1].split("if action ==")[0]
    R.check("the nurture branch never suppresses a lead",
            "suppressed" not in nurture_branch,
            "suppressed=TRUE blocks every future send permanently (knocks.py), which "
            "is the exact thing nurture exists to undo")
    R.check("...and never notifies sales", "_notify" not in nurture_branch,
            "owner chose option 2: bot keeps talking, no salesperson called")


def test_no_outcome_silences_the_bot():
    """Removed three times, once per outcome. It must not come back a fourth."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "worker.py"),
               encoding="utf-8").read()
    body = src.split("def _handle_inbound")[1].split("\ndef ")[0]
    for outcome in ("qualified", "escalated", "dead", "nurture"):
        R.check(f"{outcome} does not stop the bot replying",
                f'outcome") == "{outcome}"' not in body
                and f"outcome') == '{outcome}'" not in body,
                f"_handle_inbound returns early on outcome={outcome}; silence has "
                f"exactly two legitimate sources, STOP and an operator pause")


def test_the_below_entry_rules_are_editable_english():
    rules = answering.RULES["below_entry"]
    R.check("the probing rules exist in the document", len(rules) > 400, rules[:80])
    for must in ("firm", "loan", "one question at a time"):
        R.check(f"...and mention {must!r}", must in rules.lower(), rules[:120])
    R.check("the model is told NOT to say they cannot afford it",
            "cannot afford" in rules.lower(), rules[:120])
    # Same no-restatement rule as every other section: the arithmetic is code's.
    for forbidden in ("12800000", "1.25", "BUDGET_STRETCH", "ENTRY_FLOOR"):
        R.check(f"and does not restate {forbidden} (code owns that)",
                forbidden not in rules, rules[:120])


def main():
    for fn in (test_qualification, test_configuration_classifier, test_price_guard,
               test_say_a_price_once, test_affordability_is_decided_for_the_model,
               test_locality_never_spoken, test_mall_locked_to_a_real_objection,
               test_corruption_is_refused_not_repaired, test_garbled_is_retried_not_escalated,
               test_never_congratulate_a_non_answer, test_greeting_never_mojibake,
               test_knock_spacing, test_the_rulebook_loads_and_fails_loudly,
               test_one_floor_and_it_is_stretched,
               test_below_entry_is_nurtured_not_killed,
               test_nurture_never_suppresses_and_can_be_upgraded,
               test_no_outcome_silences_the_bot,
               test_the_below_entry_rules_are_editable_english,
               test_possession_dates_are_refused,
               test_qualified_card_is_sent_once):
        fn()
    return R.report("RULES  (no database, no API)")


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
