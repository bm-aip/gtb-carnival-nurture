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


def q_src():
    return open(os.path.join(os.path.dirname(__file__), "..", "qualifier.py"),
                encoding="utf-8").read()


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
def test_the_two_approved_possession_dates_are_sayable():
    """Marketing authorised exactly two dates on 2026-08-05. These must go out.

    Until that day the guard refused every possession date, which was right while the
    corpus held dates nobody had approved. Now the bot has an answer to a top-three
    buyer question and the guard must not eat it.
    """
    ok = [
        "Phase 1 is scheduled for possession in December 2027.",
        "Phase 2 hands over in June 2028.",
        "Possession for Phase 1 is Dec 2027 and Phase 2 is Jun 2028.",
        "Handover is scheduled for December 2027 — shall I show you the apartments?",
    ]
    for reply in ok:
        R.check(f"approved date allowed: {reply[:44]!r}",
                q._possession_problem(reply) is None)

    # And they still have to be cited like any other fact -- the approved chunk is in
    # the corpus, so this is a real citation, not an exemption.
    for reply in ok:
        R.check(f"...still looks factual: {reply[:34]!r}", q._looks_factual(reply))


def test_unapproved_possession_dates_are_still_refused():
    bad = [
        "Possession is scheduled for Q3 2028.",
        "The villas will be ready to move in 18 months.",
        "We hand over the apartments in mid-2027.",
        "Completion is targeted for end of next year.",
        "Move-in is planned for March 2029.",
        # A day of the month. "December 2027" is approved; the 15th of it is not --
        # a precise handover day is a different promise, and it is the shape a buyer
        # forwards to their lawyer. This passed the scrub-then-check on its own,
        # which is why _DAY_OF_MONTH runs first.
        "Handover is on 15 December 2027.",
        "We hand over December 15, 2027.",
        # Right date, wrong phase count -- a third phase has no approved date at all.
        "Phase 3 possession is expected by 2030.",
        # One approved clause used to smuggle an unapproved one.
        "Phase 1 hands over in December 2027 and Phase 3 should be ready by 2031.",
        # Bringing it forward is the most damaging version: it is the one a buyer
        # acts on.
        "Possession is December 2027, though we may hand over as early as Q2 2027.",
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


def test_the_maintenance_provider_is_never_named():
    """Marketing, 2026-08-05 (Q12): do not name it.

    The two chunks holding the name are quarantined, so this is the belt. It is
    proximity-based on purpose -- see the false-positive block below, which is the
    whole reason it is not a word ban.
    """
    named = [
        "Maintenance is handled by Elements, our facility management partner.",
        "The common areas are maintained by Elements.",
        "Elements will be the service provider for upkeep of the community.",
        "Housekeeping and upkeep are managed by Elements throughout the year.",
    ]
    for reply in named:
        R.check(f"blocked: {reply[:46]!r}", q._maintenance_naming(reply) is True)

    # THIS IS THE POINT OF THE PROXIMITY RULE. Republic of Nature is a nature-led
    # brand and its own approved copy reaches for this word. A bare token ban would
    # escalate every one of these -- good replies, killed by a guard.
    innocent = [
        "The design lets you live close to the elements — sea air, light, open sky.",
        "Water, light and greenery are the elements the masterplan is built around.",
        "The natural elements are what make the site what it is.",
        "Two elements make this different: the acreage and how few homes there are.",
        # Same word, same reply as a maintenance sentence, but far apart. 120
        # characters is roughly a sentence either side.
        "The community is professionally maintained. Living here puts you close to "
        "the sea air, the light and the open sky, which are the elements the whole "
        "masterplan was drawn around.",
    ]
    for reply in innocent:
        R.check(f"not blocked: {reply[:46]!r}", q._maintenance_naming(reply) is False)


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
            'prior in ("escalated", "wants_sales")' in branch
            and "upgrade_from=prior" in branch,
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

    # BELOW-ENTRY nurture is arithmetic and the model never gets a say in it --
    # that is what the four cases above prove, and it is unchanged.
    #
    # From 2026-08-06 the model MAY report nurture, for exactly one situation: a
    # buyer declining the offer of a call. Without it the decline could not be
    # recorded at all -- the model returned "answer" with the word nurture in its
    # note, the outcome column stayed empty, and /admin/nurture could not see the
    # people it exists to show. Observed live before the enum changed.
    actions = q.DECISION_SCHEMA["properties"]["action"]["enum"]
    R.check("nurture is available for the declined-call case", "nurture" in actions,
            f"schema enum is {actions}")
    R.check("...and the model is only invited to use it there",
            q_src().count("action='nurture'") == 1
            and "DECLINED" in q_src(),
            "nurture must not become a general permission; below-entry stays "
            "arithmetic")


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


def test_a_question_is_not_a_claim():
    """The citation floor must not fire on the bot asking something.

    2026-08-05, live: a buyer had given configuration, purpose and location. The bot
    asked for the last one -- "the budget band you have in mind for the villa - just
    a rough figure is fine" -- and the floor saw the word `villa`, found no citation
    (correctly: a question about someone's wallet cites nothing), binned the reply
    and handed the conversation to a human. One gate from qualified.
    """
    asking = [
        "Sorry, I meant the budget band you have in mind for the villa - just a "
        "rough figure is fine, only so I show you homes that are genuinely in range.",
        "Would you prefer a villa or an apartment?",
        "Is the apartment for you or for family?",
        "Which floor were you hoping for?",
        "Are you looking at 3 bedroom or 4 bedroom villas?",   # config names, not sizes
        "Got it, a villa. Is this for weekends or to live in full-time?",
        "Nice. Which area are you looking to buy around?",
    ]
    for reply in asking:
        R.check(f"no citation needed: {reply[:46]!r}", q._needs_citation(reply) is False)

    # AND THE GUARD MUST STILL DO ITS JOB. Every one of these is the failure it was
    # built for: a value, or a thing that does not exist in the corpus at all.
    claiming = [
        "The 3 bedroom villas are 2552 sqft.",
        "Villas start from ₹3.94 Cr onwards.",
        "There is a good school about 10 minutes away.",
        "The nearest hospital is 4 km away.",           # no corpus entry exists
        "It is a 32-acre community with 343 homes.",
        "Phase 1 is scheduled for possession in December 2027.",
        "The clubhouse is 60,000 sqft.",
        "The RERA number is TN/35/Building/0523/2024.",
        # A school with no number is still an invented school -- this is why the
        # test is not "does it contain a digit".
        "There is a school right next to the community.",
        # Our claims rule forbids implying a beach. Even as a question it implies
        # one, so it must not go out uncited.
        "Are you thinking beach side or inland?",
    ]
    for reply in claiming:
        R.check(f"citation required: {reply[:46]!r}", q._needs_citation(reply) is True)

    # The possession belt reads _looks_factual, NOT _needs_citation, and must keep
    # its wide net -- narrowing that one would reopen the uncited-date hole.
    R.check("possession belt keeps the wide net",
            q._looks_factual("It's ready to move whenever you are.") is True)


def test_the_factual_net_catches_plurals():
    """Found 2026-08-05 while narrowing the citation floor, and older than it.

    Every noun in FACTUAL sat between two word boundaries in the singular. Property
    copy is written in the plural almost throughout, so the net had a hole exactly
    where claims get made: "the villa is lovely" was factual, "villas start from
    ₹3.94 Cr onwards" was not factual at all.

    `amenit` and `kilomet` were worse -- a prefix inside \\b...\\b needs a word
    boundary after it, which "amenities" and "kilometres" do not have. Neither entry
    could ever match anything.
    """
    for text in ("Villas start from ₹3.94 Cr onwards.",
                 "Our apartments are lovely.",
                 "It spans 32 acres.",
                 "What are the amenities?",
                 "It is 5 kilometres away.",
                 "There are two clubhouses.",
                 "Both phases are on the same road.",
                 "Which floors are available?",
                 "The nearest schools are close by.",
                 "There are two hospitals nearby."):
        R.check(f"plural is factual: {text[:42]!r}", q._looks_factual(text) is True)

    # The singulars must not have been lost in the process.
    for text in ("The villa is lovely.", "It spans one acre.",
                 "The apartment is ready.", "The clubhouse is open."):
        R.check(f"singular still factual: {text[:42]!r}", q._looks_factual(text) is True)

    # And the net must not have widened into ordinary words.
    for text in ("Thanks, that helps.", "Which area are you looking around?",
                 "Saturday works well.", "Let me get someone to call you."):
        R.check(f"not factual: {text[:42]!r}", q._looks_factual(text) is False)


def test_the_voice_is_plain_and_the_examples_survive():
    """The register the owner chose on 2026-08-05, and the examples that carry it.

    "Keep it simple. Short sentences." was already in this document and the bot still
    wrote "this is really where the place comes into its own". Description alone did
    not move it; the before/after pairs did. So the pairs are the asset -- a future
    tidy-up that deletes them as clutter would quietly restore the brochure voice,
    and nothing else in the system would notice.
    """
    lang = answering.RULES["language"]
    voice = answering.RULES["voice"]

    R.check("the language rules are substantial", len(lang) > 900, lang[:80])
    # Counted on the PARSED text, not the file. `>` lines are notes to the editor and
    # are stripped by design -- these examples were written as blockquotes first and
    # reached the model as four blank lines. The document looked right and the bot was
    # told nothing. Anything meant for the model has to be asserted after parsing.
    R.check("...and show real before/after pairs", lang.count("TOO MUCH:") >= 4
            and lang.count("BETTER:") >= 4,
            f"{lang.count('TOO MUCH:')} bad, {lang.count('BETTER:')} good")

    # The specific tics seen in live replies. Each one must stay named: a banned
    # phrase list that loses its entries is a list that bans nothing.
    for tic in ("comes into its own", "resort-style", "world-class", "nestled",
                "boasts", "an array of"):
        R.check(f"language rules still ban {tic!r}", tic in lang, lang[:120])

    R.check("contractions are encouraged", "contraction" in lang.lower(), lang[:120])
    R.check("voice says talk like a person", "like a person" in voice.lower(), voice[:120])

    # Casual was the owner's choice; careless was not. Without this the register
    # drifts to flippant, which on a crore-plus purchase reads as not caring.
    R.check("voice keeps the floor under casual",
            "not careless" in voice.lower() or "not cold" in voice.lower(), voice[:160])


def test_the_approved_answers_are_in_the_corpus_file():
    """Marketing's answers reached the corpus intact, with their rules attached.

    Runs the real chunker over the real file -- no database, no embeddings. What this
    catches is the failure that would be invisible in production: a heading renamed,
    a marker dropped, and a fact going out with nobody's guardrail on it.
    """
    import re as _re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "scripts", "ingest_kb.py"), encoding="utf-8").read()
    ns = {"re": _re}
    exec(src[src.index("APPROVED_GUARDRAILS = {"):src.index("SOURCES = {")], ns)
    text = open(os.path.join(root, "kb", "RON", "approved-answers.md"),
                encoding="utf-8").read()
    chunks = ns["chunk_approved"](text)
    blob = " ".join(c["content"] for c in chunks)

    R.check("the approved answers chunk", len(chunks) >= 9, f"{len(chunks)} chunks")

    # The figures the business actually chose, in their words.
    for fact in ("32-acre", "343 homes", "60,000 sqft",
                 "TN/35/Building/0523/2024", "December 2027", "June 2028", "58 years"):
        R.check(f"corpus states {fact!r}", fact in blob)

    # EVERY approved chunk carries a guardrail. An approved fact with no rule on it
    # is the exact shape of the 2026-08-03 defect: 64 of 67 chunks unguarded.
    for c in chunks:
        head = c["content"].split("\n")[0][:44]
        R.check(f"guarded: {head!r}", bool(c["guardrail"]) and len(c["guardrail"]) > 60)

    # The rules table is addressed to us. A bot that ingests its own instructions
    # reads them aloud, and this one would read out the words it must never say.
    for leak in ("Attached to", "villaments", "island villas", "Rules that travel"):
        R.check(f"instruction text {leak!r} stayed out of the corpus",
                leak.lower() not in blob.lower())

    # The withdrawn flood prediction must not creep back through the source file.
    R.check("no flood prediction in the approved text",
            "won't flood" not in blob and "wont flood" not in blob
            and "confident the water" not in blob)


def test_no_rupee_sign_leaves_the_building():
    """Outbound text is ASCII money; inbound still understands the symbol.

    Free session text reaches Wati as a URL QUERY PARAMETER, not a JSON body, so a
    non-ASCII character goes out percent-encoded and we are trusting their decoder
    to put it back. The same character also breaks every place a human reads the
    text afterwards -- Excel opens a CSV export as cp1252, and so does the Windows
    console, which raised UnicodeEncodeError on this exact character while this
    change was being written.

    Both halves matter. Dropping the symbol from OUTBOUND is the fix; keeping it on
    INBOUND is what stops the fix costing us a buyer's stated budget.
    """
    R_SIGN = chr(0x20B9)

    # -- outbound: the reply is normalised before any guard reads it -----------
    R.eq("rupee sign becomes Rs", q._clean_reply(f"Villas from {R_SIGN}3.94 Cr."),
         "Villas from Rs 3.94 Cr.")
    R.eq("a space after the sign does not double up",
         q._clean_reply(f"Apartments from {R_SIGN} 1.28 Cr."),
         "Apartments from Rs 1.28 Cr.")
    R.check("the one quotable price carries no symbol",
            R_SIGN not in config.VILLA_PRICE_TEXT, config.VILLA_PRICE_TEXT)

    # -- inbound: a buyer typing the symbol must still be understood -----------
    R.eq("buyer's budget in rupee sign still parses",
         q.budget_from_text(f"my budget is {R_SIGN}1.5 cr"), 15 * CR // 10)
    R.eq("buyer's budget in Rs still parses",
         q.budget_from_text("my budget is Rs 1.5 cr"), 15 * CR // 10)
    R.check("a figure written Rs is still a figure to the price guard",
            q._money_figures("Villas from Rs 3.94 Cr.") == {"3.94"})

    # -- the guards did not soften when the currency changed ------------------
    def problem(reply, cited=(423,), msg="what is the price"):
        return q._price_problem(reply, CHUNKS, list(cited), msg)

    R.check("flat Rs price still escalates", problem("A villa is Rs 3.94 Cr.") is not None)
    R.check("invented Rs figure still escalates",
            problem("Villas start from Rs 2.75 Cr.") is not None)
    R.check("Rs range with a top still escalates",
            problem("Villas run from Rs 3.94 Cr to Rs 5.5 Cr.") is not None)
    R.check("cited + framed Rs price passes",
            problem("Villas start from Rs 3.94 Cr.") is None)

    # -- the corpus itself, as CHUNKS and not as files ------------------------
    #
    # Asserting on the file would have missed the real risk. chunk_pricing matches
    # table rows on the currency marker: change the document without the pattern and
    # it returns ZERO chunks, silently withdrawing the only price the bot may say.
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "ingest_kb", os.path.join(root, "scripts", "ingest_kb.py"))
    ing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ing)

    priced = ing.chunk_pricing(
        open(os.path.join(root, "kb/RON/pricing.md"), encoding="utf-8").read())
    R.check("the pricing document still yields a chunk", len(priced) == 1,
            f"{len(priced)} chunks -- a silent zero withdraws the only sayable price")
    for cfg in ("Compact 2BHK", "3 bed villa", "4 bed villa"):
        R.check(f"{cfg} survived the rewrite", cfg in priced[0]["content"])

    approved = ing.chunk_approved(
        open(os.path.join(root, "kb/RON/approved-answers.md"), encoding="utf-8").read())
    carrying = [c for c in priced + approved
                if R_SIGN in c["content"] or R_SIGN in (c.get("guardrail") or "")]
    R.check("no ingested chunk carries the symbol", not carrying,
            f"{len(carrying)} chunks still do")


def test_the_account_move_did_not_cross_two_templates():
    """The number stayed; the Facebook business under it changed 2026-08-06, so
    every template was resubmitted with a `_newac` suffix.

    All five names were compared against their predecessors on the live account:
    body, variable count and buttons identical.

    The case that matters is the crossing. The first attempt at the new visit
    template carried the GHOST RE-OPENER's copy under the visit template's name.
    Wiring T6 to that would have sent someone who has never replied "we were
    talking about {{2}} — shall I pick up where we left off?", and would have
    failed on arity too: the schedule sends T6 with no variables, that template
    declares two. It was deleted and both were resubmitted correctly.

    Names are asserted EXACTLY rather than by suffix, because the suffixes do not
    agree -- three are `_newac`, the visit invitation is `_new_acc`. A rule like
    "ends with _newac" would pass on a name Meta has never heard of.
    """
    T = config.KNOCK_TEMPLATES
    expected = {
        "t1_lifestyle":   "ron_nurture_01_lifestyle_newac",
        "t2_location":    "ron_nurture_02_location_newac",
        "t3_low_density": "ron_nurture_03_low_density_newac",
        "t6_visit":       "ron_nurture_06_visit_new_acc",
    }
    for key, name in expected.items():
        R.eq(f"{key} points at the approved new-business template", T[key], name)
    R.eq("the re-opener moved too", config.REOPENER_TEMPLATE, "t7_reopener_newac")
    R.check("the visit invitation and the re-opener are not the same template",
            T["t6_visit"] != config.REOPENER_TEMPLATE
            and "visit" not in config.REOPENER_TEMPLATE,
            "one carries a stranger's greeting, the other continues a conversation")
    R.check("no step is left on a pre-move name",
            not any(v in ("ron_nurture_01_lifestyle", "ron_nurture_02_location",
                          "ron_nurture_03_low_density", "ron_nurture_06_visit",
                          "t7_reopener")
                    for v in list(T.values()) + [config.REOPENER_TEMPLATE]),
            "the old Facebook business no longer owns these")
    R.check("every scheduled step still has a template",
            all(k in T for _, k in knocks.KNOCK_SCHEDULE),
            "a step with no template is a send that fails at day 25")
    R.check("the variable count per template is still declared",
            set(knocks.TEMPLATE_TAKES_NAME) == set(T),
            "a wrong parameter count is a failed send, not a wrong-looking one")


def test_the_budget_refuser_can_still_reach_sales():
    """Owner, 2026-08-06: "if someone is not giving budget - we should ask them - if
    they want to speak to sales team and take it forward - that is good enough test
    of their seriousness".

    The danger in this feature is not that it fails to fire. It is that it fires as
    a QUALIFIED lead, because that would quietly spend the one promise the system
    rests on -- sales receives nobody unqualified. So the cases below check the
    trigger, and then check just as hard that the budget gate did not move.
    """
    import conversation as c

    def conv(checklist, asked, outcome=None):
        return {"id": -1, "checklist": checklist, "asked": asked, "outcome": outcome,
                "unreciprocated": 0}

    known = {"location": "Adyar", "configuration": "3BHK"}

    R.eq("one budget ask is not enough",
         c.sales_offer_state(conv(known, {"budget": [0]})), None)
    R.eq("two is", c.sales_offer_state(conv(known, {"budget": [0, 1]})), "due")
    R.eq("no location, no offer -- the call would be worthless",
         c.sales_offer_state(conv({"configuration": "3BHK"}, {"budget": [0, 1]})), None)
    R.eq("no configuration, no offer",
         c.sales_offer_state(conv({"location": "Adyar"}, {"budget": [0, 1]})), None)
    R.eq("a budget on file means this never runs",
         c.sales_offer_state(conv(dict(known, budget=21000000), {"budget": [0, 1]})),
         None)
    R.eq("once offered, we read the answer instead of offering again",
         c.sales_offer_state(conv(known, {"budget": [0, 1], "sales_offer": [0]})),
         "answered")

    # -- THE GATE DID NOT MOVE --------------------------------------------------
    R.check("no budget still fails the bar",
            cv.clears_the_bar({"checklist": known})[0] is False,
            "a missing budget must never become a passing budget")
    R.eq("...and says why", cv.clears_the_bar({"checklist": known})[1],
         "budget not captured")

    # -- the card cannot be mistaken for a qualified lead -----------------------
    import handoff as h
    lead = {"id": 91, "name": "Ramesh K", "phone": "919876543210",
            "project": "Republic of Nature"}
    slots = h.build_sales_request(lead, conv(dict(known, purpose="buy to live"), {}))
    R.check("the headline says they only agreed to talk",
            slots[0].startswith("Wants to speak to sales"), slots[0])
    R.check("the missing figure is spelled out, not left blank",
            "budget not given" in slots[3], slots[3])
    R.check("no slot claims they are qualified",
            not any("ualified" in s for s in slots), slots)
    for i, v in enumerate(slots, 1):
        R.check(f"sales-request slot {i} is one line and filled",
                "\n" not in v and bool(v.strip()))

    # -- the wiring ------------------------------------------------------------
    src = open(os.path.join(os.path.dirname(__file__), "..", "handoff.py"),
               encoding="utf-8").read()
    branch = src.split('if action == "connect_sales":')[1].split("if action ==")[0]
    R.check("this exit does NOT go through clears_the_bar",
            "clears_the_bar" not in branch,
            "routing it through the bar would either fail always or loosen the bar")
    R.check("...and it still only fires once",
            "handoff_sent_at" in branch,
            "a repeated card trains sales to ignore the channel")
    R.check("a wants_sales lead who later names a budget can still qualify",
            'prior in ("escalated", "wants_sales")' in src)
    R.check("connect_sales is a choice the model is allowed to make",
            "connect_sales" in q.DECISION_SCHEMA["properties"]["action"]["enum"])
    R.check("a 'qualified' with no budget after the offer is relabelled, not binned",
            'action = "connect_sales"' in src,
            "seen live: the buyer said yes, the model said qualified, the bar "
            "correctly refused it, and the accepted call was thrown away")
    R.check("the offer wording belongs to sales, not to code",
            "SALES_OFFER_FRAMING" in open(
                os.path.join(os.path.dirname(__file__), "..", "config.py"),
                encoding="utf-8").read())
    R.check("declining the offer counts as engagement",
            '("connect_sales", "nurture")' in open(
                os.path.join(os.path.dirname(__file__), "..", "conversation.py"),
                encoding="utf-8").read(),
            "otherwise a polite 'not yet' is counted as another silence")
    R.check("the card nobody understood is not called stalling any more",
            "Stalling —" not in src and "No answers yet" in src)


def test_staff_cards_go_by_template():
    """The card that fetches a human must not depend on that human's 24h window.

    Staff cards went as free session text until 2026-08-06, so they only reached a
    salesperson who had messaged the business number in the previous 24 hours.
    Salespeople do not do that. Measured over 30 days: 5 of 24 (21%) came back
    "Ticket has been expired." -- four of them escalations, discarded silently.

    The slot rules are the fragile part and they fail LOUDLY on WhatsApp's side but
    SILENTLY on ours: a newline inside any parameter makes Meta reject the entire
    message, so one salesperson with a two-line name in the CRM would take the card
    down for everybody.
    """
    import handoff as h

    lead = {"id": 91, "name": "Ramesh\nK", "phone": "919876543210",
            "project": "Republic of Nature"}
    booked = {"checklist": {"purpose": "buy to live", "location": "Adyar",
                            "configuration": "3BHK", "budget": 39400000,
                            "timeline": "3-6 months", "visit_day": "Saturday",
                            "visit_time": "11am", "visit_venue": "site"}}
    plain = {"checklist": {"purpose": "buy to live", "location": "Adyar",
                           "configuration": "3BHK", "budget": 39400000}}

    for name, slots in (
            ("visit booked", h.build_card(lead, booked)),
            ("qualified", h.build_card(lead, plain, "Cleared on budget")),
            ("escalation", h.build_escalation(lead, plain,
                                              {"internal_note": "asked about the beach",
                                               "flags": ["wants_human"]})),
    ):
        R.eq(f"{name} card fills exactly five slots", len(slots), 5)
        for i, v in enumerate(slots, 1):
            R.check(f"{name} slot {i} is on one line", "\n" not in v and "\t" not in v,
                    "a newline in ANY parameter makes WhatsApp reject the whole send")
            R.check(f"{name} slot {i} is not empty", bool(v.strip()),
                    "an empty parameter is rejected too")
            R.check(f"{name} slot {i} carries no rupee sign", chr(0x20B9) not in v)

    R.check("the headline says which card this is",
            h.build_card(lead, booked)[0].startswith("SITE VISIT BOOKED"),
            h.build_card(lead, booked)[0])
    R.check("a booked visit still asks somebody to confirm the time",
            "CONFIRM THE TIME" in h.build_card(lead, booked)[4])
    R.check("budget reaches sales in the same currency as everything else",
            "Rs 3.94 cr" in h.build_card(lead, plain)[3], h.build_card(lead, plain)[3])
    R.check("the escalation reason survives into the action line",
            "beach" in h.build_escalation(lead, plain, {"internal_note":
                                                        "asked about the beach"})[4])

    # -- the wiring, read as source: these are the parts a rename would break ---
    src = open(os.path.join(os.path.dirname(__file__), "..", "handoff.py"),
               encoding="utf-8").read()
    R.check("every card is sent as the approved template",
            src.count("template=config.STAFF_TEMPLATE") == 1
            and "_notify(config.HANDOFF_PHONES" not in src
            and "_notify(config.ESCALATION_PHONES" not in src,
            "a card still going out as free text is a card that may not arrive")
    R.check("a failed template still falls back to session text",
            'f"handoff_{kind}_text"' in src,
            "79% of cards got through the old way -- never send nothing instead")
    R.check("the card is filed against the buyer, not the salesperson",
            src.count("lead_id=lead[\"id\"]") == 5,
            "message_log recorded lead_id NULL on all 24 cards, so the audit trail "
            "went blank at the exact moment that decides who gets paid")
    R.check("the staff template name is env-overridable",
            config.STAFF_TEMPLATE and "WATI_TPL_STAFF" in
            open(os.path.join(os.path.dirname(__file__), "..", "config.py"),
                 encoding="utf-8").read(),
            "the account move renames templates; that must not need a deploy")
    R.check("staff recipients are one list", bool(config.STAFF_PHONES))


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
               test_the_two_approved_possession_dates_are_sayable,
               test_unapproved_possession_dates_are_still_refused,
               test_the_maintenance_provider_is_never_named,
               test_a_question_is_not_a_claim,
               test_the_factual_net_catches_plurals,
               test_the_voice_is_plain_and_the_examples_survive,
               test_the_approved_answers_are_in_the_corpus_file,
               test_no_rupee_sign_leaves_the_building,
               test_the_account_move_did_not_cross_two_templates,
               test_the_budget_refuser_can_still_reach_sales,
               test_staff_cards_go_by_template,
               test_qualified_card_is_sent_once):
        fn()
    return R.report("RULES  (no database, no API)")


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
