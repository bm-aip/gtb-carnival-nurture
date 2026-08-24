"""The qualifier agent — one bot turn (task 20).

Implements design §9 exactly:

    1. lead sends a message
    2. LOCK BRAND     — brand_id from the LEAD RECORD, never from message text
    3. RETRIEVE       — WHERE brand_id = lead.brand (a DB filter, not a prompt)
    4. CONFIDENCE FLOOR — nothing solid retrieved? escalate, do not answer from
                          general knowledge
    5. ANSWER FIRST   — answer what they asked, from the retrieved chunks
    6. THEN ASK       — exactly one ask, carrying its reason line
    7. LOG            — the reply WITH the chunk ids that produced it

WHAT IS STRUCTURAL AND WHAT IS PROMPTED
---------------------------------------
Anything that must not fail is enforced in code, not asked for in the prompt:

  * the brand fence is `kb.answer_context(brand_id, ...)` — a WHERE clause. The
    model never chooses which corpus to read.
  * the budget gate is arithmetic: config.budget_reaches() against one derived
    floor. The model reports the number it heard; Python decides whether it passes.
    There is deliberately no second floor to disagree with the first.
  * prices: the corpus holds STARTING prices only, and every figure in a reply
    must be traceable to a cited chunk or to the buyer's own words. An untraceable
    figure is an invented one and the turn escalates. The exact per-unit sheet is
    still absent from the corpus, so it cannot be quoted at all.
  * the model must cite the chunk ids behind any factual claim, and a factual
    answer citing nothing is downgraded to an escalation before it is sent.

The prompt handles tone, ask order and persuasion — the things a rule cannot
express. It is not load-bearing for safety.
"""
import json
import logging
import re

import anthropic

import answering
import config
import kb

log = logging.getLogger("qualifier")

MODEL = "claude-opus-5"

# Thinking is ON BY DEFAULT on Opus 5, and max_tokens caps thinking + reply
# together -- a budget sized for a two-line WhatsApp message would truncate the
# answer mid-sentence. This is deliberately generous; the reply itself is short.
MAX_TOKENS = 8000

# `medium` rather than `high`. A buyer is waiting on WhatsApp, and low/medium
# effort on this model is strong enough for a turn that is mostly "read six
# retrieved chunks, answer one question, ask one thing".
EFFORT = "medium"

# The structured shape of a turn. Forcing the model to fill this rather than
# free-writing is what makes the turn auditable: every reply arrives with the
# chunks that justify it and the fields it captured.
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string",
                  "description": "The WhatsApp message to send. Plain text."},
        # `nurture` is in this list for ONE case only: a buyer who declines the
        # offer of a call. Everywhere else nurture stays arithmetic, decided in
        # _enforce from the budget, and the rulebook never invites the model to
        # choose it. Without it here the model has no way to report the decline --
        # it returns "answer" with the word nurture in its note, the outcome column
        # stays empty, and the /admin/nurture view cannot see the very people it
        # exists to show. A wrong nurture is also the cheapest mistake available:
        # it is the one provisional outcome, suppresses nothing, and upgrades.
        "action": {"type": "string",
                   "enum": ["answer", "ask", "escalate", "qualified", "dead",
                            "connect_sales", "nurture"]},
        "sources": {"type": "array", "items": {"type": "integer"},
                    "description": "chunk ids that support any factual claim in "
                                   "the reply. Empty only for pure greetings or "
                                   "questions back to the buyer."},
        "purpose": {"anyOf": [{"type": "string",
                               "enum": ["weekend_home", "primary_residence",
                                        "investment", "unknown"]},
                              {"type": "null"}]},
        "location": {"anyOf": [{"type": "string"}, {"type": "null"}],
                     "description": "where the buyer wants to buy, in their words"},
        "configuration": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "budget_inr": {"anyOf": [{"type": "integer"}, {"type": "null"}],
                       "description": "budget in RUPEES if stated. 1.5 crore = "
                                      "15000000. Null if not stated."},
        "timeline": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "visit_day": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "visit_time": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        # `experience_centre` is retired (owner 2026-08-11) but stays in the enum:
        # removing it would make the model's occasional stale answer a SCHEMA
        # failure, losing the whole turn, where _enforce can quietly normalise it
        # to "site" instead. `virtual` is the ask for anyone not in Chennai.
        "visit_venue": {"anyOf": [{"type": "string",
                                   "enum": ["site", "virtual", "experience_centre"]},
                                  {"type": "null"}]},
        "flags": {"type": "array",
                  "items": {"type": "string",
                            "enum": ["price_question", "primary_residence_fit",
                                     "off_category", "not_currently_sold",
                                     "objection", "wants_human", "location_doubt"]}},
        "internal_note": {"type": "string",
                          "description": "one line for the salesperson. Never sent."},
        "gate_asked": {"anyOf": [{"type": "string",
                                  "enum": ["purpose", "location", "configuration",
                                           "budget", "sales_offer"]},
                                 {"type": "null"}],
                       "description": "which gate you asked about in this reply, "
                                      "if any"},
        "framing_used": {"anyOf": [{"type": "integer"}, {"type": "null"}],
                         "description": "the index of the framing you used from "
                                        "the list provided. Null if you did not ask."},
    },
    "required": ["reply", "action", "sources", "purpose", "location",
                 "configuration", "budget_inr", "timeline", "visit_day",
                 "visit_time", "visit_venue", "flags", "internal_note",
                 "gate_asked", "framing_used"],
    "additionalProperties": False,
}


def configured():
    """The SDK reads ANTHROPIC_API_KEY from the environment itself."""
    import os
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _system_prompt(brand_name):
    """The whole prompt now comes from content/answering-rules.md.

    It used to be 795 lines of string literal in this file, which is why editing the
    bot's voice meant editing Python. Ported from the AskAshwin pattern -- see
    answering.py for the split between what the document decides and what the code
    decides.
    """
    return answering.system_prompt(brand_name)


def _render_context(chunks):
    """The retrieved corpus, plus any guardrail attached to a chunk.

    Guardrails travel WITH their facts (they are a column on kb_chunks), so the
    no-beach-claim rule arrives glued to the distance list rather than living in
    a prompt someone can edit away.
    """
    if not chunks:
        return "(nothing retrieved — you have no source. Escalate.)"
    out = []
    for c in chunks:
        block = f"[chunk {c['id']}] {c['content']}"
        if c.get("guardrail"):
            block += f"\n  !! RULE FOR THIS CHUNK: {c['guardrail']}"
        out.append(block)
    return "\n\n".join(out)


def _history(turns):
    """Prior turns as alternating messages. Oldest first.

    Corrupted OWN output is dropped rather than replayed. On 2026-08-01 one
    mangled reply entered the history and every later turn read it back as its
    own prior words; the corruption compounded turn over turn until the reply was
    truncated mid-word. A bad message we already sent is a fact, but feeding it
    back as an example of how we write is what turned one bad turn into four.
    Buyer messages are never dropped -- whatever they typed is the truth.
    """
    msgs = []
    for t in turns:
        inbound = t["direction"] == "in"
        text = (t.get("body") or "").strip()
        if not text:
            continue
        if not inbound and _CONTROL_CHARS.search(text):
            continue
        msgs.append({"role": "user" if inbound else "assistant", "content": text})
    return msgs


# PLURALS AND PREFIXES, fixed 2026-08-05. Every noun below sat between two word
# boundaries in the singular, so the guard read "the villa is lovely" as factual and
# "villas start from ₹3.94 Cr onwards" as not factual at all -- and property copy is
# written in the plural almost throughout.
#
# Worse, `amenit` and `kilomet` could never match ANYTHING: a prefix inside \b...\b
# requires a word boundary after "amenit", which "amenities" does not have. Two
# entries doing nothing since the day they were written.
#
# So the citation floor was loose exactly where claims are made and tight exactly
# where questions are asked. Both halves are fixed here: `\w*` for the prefixes,
# `s?` for the countable nouns.
FACTUAL = re.compile(
    r"\b(km|kilomet\w*|sq\s?ft|acres?|bhk|villas?|apartments?|amenit\w*|"
    r"schools?|hospitals?|metro|beach(es)?|lagoons?|clubhouses?|parking|"
    r"floors?|phases?|rera|"
    # Date vocabulary, added 2026-08-03. Without it "Handover is expected by
    # December 2027" matched nothing here, so _looks_factual said False, the
    # citation requirement never ran, and a fabricated completion date could be
    # sent uncited -- in the one function written to stop exactly that.
    r"possession|handover|hand over|completion|occupancy|ready to move|"
    r"move[-\s]?in)\b", re.I)


def _looks_factual(text):
    return bool(FACTUAL.search(text or ""))


# --- does this reply actually CLAIM anything? (2026-08-05) --------------------
#
# `_looks_factual` asks "is this about the project", by vocabulary. That is the
# right question for the possession belt, which wants a wide net. It is the WRONG
# question for the citation floor, which was using it too -- and a bare noun is not
# a claim.
#
# What it cost: a real buyer, one gate from qualified. The bot asked "the budget
# band you have in mind for the villa - just a rough figure is fine". The word
# `villa` matched, nothing was cited (correctly -- a question about someone's wallet
# cites nothing), and the reply was thrown away and the conversation handed to a
# human. Four more of the bot's ordinary questions do the same: "villa or
# apartment?", "is the apartment for you or family?", "which floor?".
#
# So the floor now asks a narrower question: does the sentence carry a VALUE, or name
# a thing we are prone to inventing? Everything the guard was built to stop -- an
# invented school, an invented hospital, a made-up size, a made-up date -- does one
# or the other.

# Things that exist in the world and that a model will happily invent because they
# are plausible near any Chennai project. The corpus contains NO school, hospital,
# office campus or metro (see LOCATION_GAPS in the ingest), so naming one is
# invention by definition. `beach` is here deliberately: our claims rule forbids
# implying a private or natural beach, so "beach" in an uncited reply should stop --
# including in a question like "beach side or inland?", which implies it too.
_HARD_FACT = re.compile(
    r"\b(school|college|hospital|clinic|pharmacy|metro|railway station|"
    r"airport|mall|highway|temple|beach|lagoon|clubhouse|acre|sqft|sq ft|"
    r"km|kilomet|rera)\b", re.I)

# "3 bedroom" and "4BHK" are the NAMES of our products, not measurements. Stripped
# before looking for digits, so "are you looking at 3 bedroom or 4 bedroom villas?"
# is read as the question it is rather than as a claim carrying two numbers.
_CONFIG_TOKEN = re.compile(r"\b\d\s*(bed(room)?s?|bhk)\b", re.I)


def _needs_citation(reply):
    """True when the reply asserts something it must be able to point at.

    Three gates, cheapest first. It has to be about the project at all; then either
    it names something we are prone to inventing, or it carries a number -- a size,
    a price, a distance, a year. A sentence with neither is conversation, and
    conversation cites nothing because there is nothing to cite.
    """
    text = reply or ""
    # Hard facts stand alone. They are checked FIRST and independently of the
    # vocabulary net, because the thing they catch -- an invented school, an invented
    # hospital, a RERA number nobody approved -- is invention whether or not a number
    # comes with it, and it must not depend on the wider list being complete.
    if _HARD_FACT.search(text):
        return True
    if not _looks_factual(text):
        return False
    return bool(re.search(r"\d", _CONFIG_TOKEN.sub(" ", text)))


# --- possession / handover timeline (rulebook: never state one) ---------------
# Words that mean "when will it be finished".
_POSSESSION = re.compile(
    # `hand(s|ing|ed)?` -- the bare plural was missing until 2026-08-05, so "Phase 1
    # hands over in December 2027" matched nothing and the whole possession guard was
    # skipped for the most natural way to write the sentence. Found by a test that
    # appeared to pass for the right reason and was passing for no reason at all.
    r"\b(possession|handover|hand(s|ing|ed)?\s+over|completion|"
    r"occupancy\s+certificate|completion\s+certificate|"
    r"ready\s+(to\s+move|for\s+(possession|occupancy))|move[-\s]?in)\b", re.I)

# Anything that pins a time to it.
#
# Deliberately does NOT match a bare weekday or a clock time: "Saturday at 11" and
# "Monday afternoon" are how a SITE VISIT is booked, which is the bot's whole job, so
# matching those would escalate the win. Bare "May" is also excluded -- it collides
# with the ordinary verb ("you may visit on Saturday"), and losing one month name is
# cheaper than escalating every polite sentence.
_WHEN = re.compile(
    r"\b(20[2-9]\d"                                    # a year: 2027, 2031
    r"|q[1-4]\s*20\d\d"                                # Q3 2027
    r"|jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|jun(e)?|jul(y)?"
    r"|aug(ust)?|sep(t|tember)?|oct(ober)?|nov(ember)?|dec(ember)?"
    r"|\d+\s*(month|year)s?"                           # "in 18 months"
    r"|end\s+of\s+(this|next)?\s*(year|month)"
    r"|mid[-\s]?20\d\d)\b", re.I)


# THE TWO DATES THE BUSINESS HAS AUTHORISED. Marketing, 2026-08-05, answering audit
# questions 10 and 11: Phase 1 December 2027, Phase 2 June 2028 -- and "don't state
# progress, just say the possession date".
#
# This reverses the standing rule, so it is worth being precise about what changed.
# The old rule was "never state a completion timeline", written when the corpus held
# dates nobody had approved. It was never a claim that dates are dangerous; it was a
# claim that UNAPPROVED dates are. Two of them now have a name behind them, so the
# guard narrows from "no date" to "these two, exactly".
#
# Everything else still escalates: a third phase, a revised date, a day of the month,
# "should be ready sooner", "in about 18 months".
_APPROVED_DATES = (
    re.compile(r"\bdec(ember)?\.?\s*,?\s*20\s?27\b", re.I),   # Phase 1
    re.compile(r"\bjun(e)?\.?\s*,?\s*20\s?28\b", re.I),       # Phase 2
)

# A day of the month pinned to any month name. Checked BEFORE the approved dates are
# scrubbed out, because "15 December 2027" would otherwise pass: removing the approved
# "December 2027" leaves a bare "15", which no time pattern matches.
#
# A precise handover day is a different promise from a month, and it is the shape a
# buyer forwards to their lawyer.
_DAY_OF_MONTH = re.compile(
    r"\b\d{1,2}(st|nd|rd|th)?\s+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+"
    r"\d{1,2}(st|nd|rd|th)?\b(?!\s*\d)", re.I)


def _possession_problem(reply):
    """An UNAPPROVED completion timeline in the reply, or None.

    Requires a possession word before anything is judged. A date on its own is
    legitimate -- one appears whenever a visit is booked, which is the bot's whole
    job -- so it is the combination that commits us to something.

    The two approved dates are scrubbed out and whatever time expression is LEFT is
    the problem. Scrubbing rather than allow-listing the whole sentence matters: it
    means "Phase 1 is December 2027, and Phase 3 should be ready by 2030" is caught
    on the part that is not approved instead of being waved through on the part that
    is.
    """
    text = reply or ""
    if not _POSSESSION.search(text):
        return None
    if _DAY_OF_MONTH.search(text):
        return "reply pinned possession to a specific day of the month"
    scrubbed = text
    for rx in _APPROVED_DATES:
        scrubbed = rx.sub(" ", scrubbed)
    if _WHEN.search(scrubbed):
        return "reply stated a possession timeline that is not one of the two approved"
    return None


# --- naming the maintenance provider (marketing 2026-08-05, audit Q12) -------
_MAINTENANCE = re.compile(
    r"maintain|maintenance|upkeep|facility manage|facilities manage|"
    r"managed\s+by|service\s+provider|housekeep|common\s+area", re.I)
_PROVIDER = re.compile(r"\belements\b", re.I)


def _maintenance_naming(reply):
    """True when the reply names the maintenance provider.

    Proximity, not presence. "Elements" alone is ordinary English and this brand
    writes about nature constantly, so banning the token outright would escalate good
    replies.

    The window is 60 characters. It started at 120 and a legitimate reply tripped it
    at a gap of 97 -- "the community is professionally maintained" in one sentence and
    "the elements the masterplan was drawn around" two clauses later. Naming a company
    is tight by construction ("maintained by Elements", "managed by Elements"), so 60
    catches every real form and leaves the prose alone.
    """
    text = reply or ""
    for m in _PROVIDER.finditer(text):
        window = text[max(0, m.start() - 60):m.end() + 60]
        if _MAINTENANCE.search(window):
            return True
    return False


def run_turn(lead, message, history=None, conv=None):
    """One turn. Returns the decision dict, never raises for model reasons.

    `lead` must carry `project` and `id`. The brand comes from that record and
    nowhere else — this is the fence.
    """
    brand_id = lead["project"]
    brand_name = {"RON": "Republic of Nature",
                  "ELEMENTS": "Elements Senior Living"}.get(brand_id, brand_id)

    chunks = kb.answer_context(brand_id, _retrieval_query(message))

    client = anthropic.Anthropic()
    messages = list(_history(history or []))
    # The ladder was written in task 23 and then never passed to the model -- the
    # `conv` argument was accepted and dropped, so every turn was chosen blind and
    # only history stopped the bot repeating a framing. Now it is actually sent.
    # Fold a budget stated in THIS message into the state before building it, so
    # the affordability verdict exists on the turn it is needed rather than the
    # turn after. Never overwrites a budget we already hold.
    live_conv = conv
    if conv is not None and not (conv.get("checklist") or {}).get("budget"):
        heard = budget_from_text(message)
        if heard:
            live_conv = {**conv,
                         "checklist": {**(conv.get("checklist") or {}),
                                       "budget": heard}}

    state = _ladder(live_conv) + "\n".join(_already_quoted(messages))

    # WHERE THIS BUYER IS DECIDES WHAT WE ASK THEM FOR (owner, 2026-08-11).
    #
    # Told per turn rather than baked into the system prompt, because the prompt is
    # assembled once per brand and cached -- it cannot know which person is on the
    # other end. Lead 1016 is why this exists: a +966 number asked to be called five
    # different ways and was answered with "just tell me a day and I'll set up the
    # visit" every time.
    # THEY ASKED TO BE PHONED. Decided from their words in code, because the model
    # got this right once in four: it escalated correctly and then replied about
    # apartment prices, so sales was notified while the buyer heard nothing about a
    # call. Told first, and enforced afterwards in _enforce.
    if message and config.WANTS_CALL.search(message):
        state += (f"\n\nTHEY HAVE ASKED TO BE PHONED. Say so FIRST, in these words in "
                  f"spirit:\n  {config.CALL_ACK_FRAMING}\n"
                  f"Then answer anything else they asked. Do NOT offer a site visit "
                  f"instead, do NOT add a fact they did not ask for, and do NOT "
                  f"promise a time. Set action='connect_sales' unless a colleague "
                  f"has already been told about this person.")

    # THEY ASKED THE PRICE WITHOUT SAYING WHICH HOME (owner, 2026-08-19).
    #
    # "Pls share cost" is not a question for a colleague -- it is a question missing
    # one word. Lead 9840168185 sent it, was told someone would come back to him,
    # and had to ask a second time for a figure we hold. Told in code because the
    # rulebook says the same thing and the model obeyed it once in two replays.
    #
    # It also heads off the price guard: with nothing retrieved to cite, the model
    # reaches for a figure it half-remembers, _price_problem correctly refuses it,
    # and the buyer gets an escalation instead of a question. Asking which home is
    # the answer that needs no source at all.
    if message and config.asks_price_without_product(message):
        state += ("\n\nTHEY ASKED THE PRICE WITHOUT SAYING WHICH HOME. Do NOT hand "
                  "this to a colleague and do NOT quote any figure this turn -- you "
                  "have nothing retrieved to support one. ASK THEM WHICH: the "
                  "apartments or the villas, and say you will give them the starting "
                  "price for it. That is the whole reply. Set "
                  "gate_asked='configuration' and report the framing you used.")

    # THEY HAVE PUT YOU OFF. A deferral leaves the model with nothing to say and
    # something to fill, which is where padding comes from. Lead 1413 said "Will
    # tell you later" about his visit day and was given the possession dates for
    # both phases, unasked. Decided in code for the same reason as the rest of this
    # block: "do not pad" has been in the rulebook since it was written.
    if message and config.DEFERS.search(message) and not config.WANTS_CALL.search(message):
        state += ("\n\nTHEY HAVE PUT YOU OFF FOR NOW. Say one warm line -- that is "
                  "fine, whenever suits them -- and STOP. Do NOT add a fact they "
                  "did not ask for, do NOT restate anything, do NOT ask another "
                  "question, and do NOT push the visit again. Leaving them alone is "
                  "the reply. Set gate_asked=null.")

    # THEY ASKED WHERE IT IS. A bare "Location" collides with our own gate of the
    # same name, so the model reads it as an ANSWER. It is a question.
    if message and config.ASKS_LOCATION.match(message.strip()):
        state += ("\n\nTHIS IS A QUESTION ABOUT WHERE THE PROJECT IS, not their "
                  "answer to the location gate. Tell them: ECR, near Kovalam "
                  "Junction. Do NOT record it as their location and do NOT treat it "
                  "as answered.")

    # THEIR NAME. The reference conversation used it in 61% of messages; we used it
    # in 5 of 624. It is the cheapest warmth available and it is how business is
    # spoken here. Offered, not mandated -- a name wedged into every sentence reads
    # like a mail merge, which is the opposite of the point.
    #
    # clean_first_name returns None for junk, and the guard matters because the buyer
    # controls this string: WhatsApp profile names in this database include
    # "Muna💞💞💞" and bare phone numbers. "Hi Muna💞💞💞" is worse than no name.
    first = config.clean_first_name((lead or {}).get("name"))
    if first:
        state += (f"\n\nTHEIR NAME IS {first}. Use it naturally now and then -- an "
                  f"opening, a reassurance -- not in every message and never twice "
                  f"in one. If it looks wrong for the person, leave it out.")

    if config.is_overseas(lead):
        state += (
            "\n\nTHIS BUYER IS NOT IN INDIA. Do NOT ask them to visit the site and "
            "do NOT ask for a day for a visit. Offer A LIVE VIDEO WALKTHROUGH "
            "instead -- one of our team walks them through the site on a call. It is "
            "booked the same way, a day and a time, and there are no directions "
            "because it is a call. If they ask to be phoned, that is a yes to this: "
            "take the day and set visit_venue='virtual'.")

    if conv and conv.get("outcome") == "qualified":
        state = HANDOVER_MODE + "\n\n" + state
    elif conv and conv.get("outcome") == "escalated":
        state = ESCALATED_MODE + "\n\n" + state

    messages.append({
        "role": "user",
        "content": (f"RETRIEVED KNOWLEDGE (brand {brand_id} only):\n\n"
                    f"{_render_context(chunks)}\n\n"
                    f"---\n{state}\n\n"
                    f"---\nBuyer's message:\n{message}"),
    })

    # ASK AGAIN IF THE TEXT COMES BACK MANGLED, before giving up on the buyer.
    #
    # 2026-08-03, a real villa lead tapped "Need More Details" and the model
    # produced: "Republic of Nature is on ECR, near Kovalam Junction \ronking about
    # it \\ two hundred - it's a coastal community...". The corruption guard
    # correctly refused to send it -- and then escalated, so a buyer forty seconds
    # into their first conversation was handed to a human and the bot went quiet.
    #
    # Every part behaved as designed and the outcome was still bad, because
    # corruption was treated as a judgement ("we cannot answer this") when it is a
    # stutter ("that came out wrong"). It is intermittent: clean turns sit either
    # side of a mangled one. So try again, and only escalate if it keeps happening.
    decision = None
    for attempt in range(1, GARBLE_RETRIES + 2):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            output_config={
                "effort": EFFORT,
                "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
            },
            # The system prompt is stable per brand, so it caches; the retrieved
            # chunks sit after it in the messages and change every turn.
            system=[{"type": "text", "text": _system_prompt(brand_name),
                     "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        decision = _decide(resp, chunks, lead, message, history)
        if decision.get("_forced") not in _GARBLED:
            if attempt > 1:
                decision["internal_note"] = (
                    f"clean on attempt {attempt} | " + (decision.get("internal_note") or ""))
            return decision
        log.warning("lead %s: garbled reply (%s), attempt %s of %s",
                    lead.get("id"), decision.get("_forced"), attempt, GARBLE_RETRIES + 1)
    return decision


def _decide(resp, chunks, lead, message, history):
    """One model response -> a decision. Split out so run_turn can retry."""

    # Opus 5 can decline a request outright. Check before reading content --
    # indexing content[0] on a refusal raises.
    if resp.stop_reason == "refusal":
        return _forced_escalation("model declined the request", chunks)
    if resp.stop_reason == "max_tokens":
        return _forced_escalation("response truncated", chunks)

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        return _forced_escalation("no text in response", chunks)
    try:
        decision = json.loads(text)
    except Exception:
        return _forced_escalation("unparseable decision", chunks)

    return _answer_the_question(
        _enforce(decision, chunks, lead, message=message, history=history), message)


def _answer_the_question(d, message):
    """Two things a buyer asks plainly that we were failing to answer.

    RUNS AFTER _enforce, ON PURPOSE, and that placement is the fix rather than a
    detail. Inside _enforce these guards were defeated by the confidence floor: a
    reply with an uncited factual claim is replaced wholesale by the escalation line,
    so a buyer who asked "Location" was told "someone will come back to you" and
    still never learned where the project is. Out here, every path is covered --
    including a forced escalation.

    Both sentences are OUR OWN approved config text, not retrieved knowledge, so
    stating them cannot invent anything and neither needs a citation.

    THEY ASKED TO BE PHONED. Measured 2026-08-17: six buyers did, and four of the
    replies never mentioned a call or a person -- one answered "Call me" with
    apartment prices and a site visit. Three of those conversations were `escalated`,
    so a colleague HAD been told while the buyer read about the clubhouse. The
    routing worked and the words did not, which from their side is being ignored.

    THEY ASKED WHERE IT IS. A bare "Location" collides with our own gate of the same
    name, so the model reads it as an ANSWER. It is a question, and it is also not
    their location -- recording it would tell a buyer in Adyar where the site is
    instead of noting that they are in Adyar.
    """
    if not d or not message:
        return d
    reply = (d.get("reply") or "").strip()
    note = d.get("internal_note") or ""

    if (config.WANTS_CALL.search(message)
            and not re.search(r"\bcall\b|colleague|our team|someone (from|will)",
                              reply, re.I)):
        reply = f"{config.CALL_ACK_FRAMING} {reply}".strip()
        note += " | call request was not acknowledged; framing prepended"

    if (config.ASKS_LOCATION.match(message.strip())
            and not re.search(r"ECR|Kovalam", reply, re.I)):
        # Marketing's own sentence, with the map link they supplied on the 2026-08-17
        # voice sheet. Replaces my composed "We're on the site on ECR..." -- theirs
        # says "5 kms from Kovalam" and carries a link, which we never had.
        reply = f"{config.LOCATION_ANSWER} {reply}".strip()
        note += " | location question was not answered; marketing's answer prepended"

    # THEY ASKED FOR DOCUMENTS. Marketing answers this by naming a colleague rather
    # than by declining or by quoting prices, so the bot does the same. Checked for
    # the colleague's name AND for a brochure word, because a reply that merely says
    # "I can't send photos" satisfies neither.
    if (config.ASKS_DOCS.search(message)
            and config.BROCHURE_CONTACT.lower() not in reply.lower()):
        reply = (config.BROCHURE_FRAMING.format(name=config.BROCHURE_CONTACT)
                 + " " + reply).strip()
        note += f" | document request handed to {config.BROCHURE_CONTACT}"

    if config.ASKS_LOCATION.match(message.strip()):
        # Their words were a question, so they said nothing about where THEY are.
        d["location"] = None
        if d.get("gate_asked") == "location":
            d["gate_asked"] = None
            d["framing_used"] = None

    d["reply"] = reply
    d["internal_note"] = note
    return d


# Sent instead of the normal checklist push once the lead is already qualified.
# Owner 2026-08-01: booking the site visit is the job, not merely qualifying --
# and the wind-down must not be cold, "because there are so many slips between the
# cup and the lip - we shouldnt drop the ball untill we know sales is really ON it".
HANDOVER_MODE = answering.RULES["handover_qualified"]


# Sent once a conversation has been escalated. The bot keeps talking -- see the
# comment in worker._handle_inbound -- but it must not keep raising the alarm.
ESCALATED_MODE = answering.RULES["handover_escalated"]


def _already_quoted(history):
    """Prices we have ALREADY given this buyer, so we stop repeating them.

    2026-08-02, live: the bot said "3 bedroom villas from Rs 3.94 Cr" in four
    consecutive messages. The price chunk is retrieved on every price-adjacent turn
    and its guardrail says to always say from/starting/onwards, so the model kept
    restating the whole formula. It reads like hammering.

    A person says a price once. Telling the model what it has already said is more
    reliable than asking it to remember -- the same approach as the affordability
    verdict, and for the same reason.
    """
    said, facts = [], []
    for m in history or []:
        if m.get("role") != "assistant":
            continue
        body = m.get("content") or ""
        for fig in sorted(_money_figures(body)):
            if fig not in said:
                said.append(fig)
        for name, pat in _SIGNATURE_FACTS:
            if pat.search(body):
                facts.append(name)

    out = []
    if said:
        out += ["", "ALREADY QUOTED to this buyer: " + ", ".join("Rs " + s for s in said)
                + ". Do NOT state these figures again -- they have them. Refer back "
                  "briefly if you must ('as I mentioned') and otherwise move on. "
                  "Quote a price again ONLY if they ask again, or for a "
                  "configuration you have not priced yet."]
        # A YES TO THE FIT-CHECK IS THE BUDGET (2026-08-19).
        #
        # The rulebook now says to quote a starting figure and then ask, plainly,
        # whether it sits in the range they had in mind -- far better salesmanship
        # than "what is your budget?". But budget is stored as a NUMBER, so lead
        # 1413's "Yes it sound fine" landed nowhere: the hardest fact to get out of
        # a buyer, given freely, recorded as another dodge, and it tripped the
        # third-strike alarm on a man who had answered everything else.
        #
        # The figure is one he has now confirmed he can reach, so it IS his budget
        # as far as the affordability sum is concerned. Told explicitly, with the
        # numbers, because the model must not have to infer which price it meant.
        out += ["If they confirm that price works for them -- 'yes', 'that's fine', "
                "'that works', 'sounds ok' -- that IS their budget. Set "
                f"budget_inr to the figure they just agreed to, in rupees "
                f"({', '.join('Rs ' + s + ' Cr' for s in said)}). Do NOT record it "
                "if they said the price is too high, or said nothing about it."]

    # THE SAME DEFECT, ON FACTS RATHER THAN PRICES. Owner, 2026-08-07: "the 32 acre
    # thing is over emphasised - in some chats I see upto 6 mentions of the same
    # thing - bit boring really". In the transcript he sent, "32 acres" appears in
    # SIX consecutive replies.
    #
    # Nothing is malfunctioning. The overview chunk is retrieved on almost every
    # early turn because almost every early question is about the project, and the
    # model restates its headline each time. A person mentions the size of the place
    # once and then talks about something else.
    #
    # Counted rather than trusted, exactly like the price rule above: telling the
    # model what it has already said beats asking it to remember.
    # ONCE IS ENOUGH -- the threshold used to be >= 2 (2026-08-19).
    #
    # That warned only about a fact ALREADY said twice, so the second telling was
    # never prevented; the guard could only ever stop the third. Lead 1413 shows the
    # cost: the opener gave 32 acres, Kovalam Junction, the clubhouse size and the
    # amenity list, and the very next message gave all four again, unprompted. By
    # the time this fired, the buyer had heard it twice, which is the repetition the
    # owner was complaining about in the first place.
    # The count is kept where there IS one to report -- six times is a different
    # problem from twice, and the model should feel the difference.
    repeated = [n if facts.count(n) == 1 else f"{n} ({facts.count(n)}x)"
                for n in dict.fromkeys(facts)]
    if repeated:
        out += ["", "ALREADY TOLD this buyer: " + ", ".join(repeated)
                + ". They have heard it. Do NOT say it again in this reply -- find "
                  "something they do not know yet, or just answer the question "
                  "without the scene-setting. Repeating the same headline every "
                  "message is the fastest way to sound like an advert."]
    return out


# Facts the bot reaches for as scene-setting, and therefore repeats. Matched loosely
# because the model rephrases: "32 acres", "32-acre", "the 32 acre community".
_SIGNATURE_FACTS = (
    ("the 32 acres",        re.compile(r"\b32[\s-]*acre", re.I)),
    ("343 homes",           re.compile(r"\b343\b", re.I)),
    ("the clubhouse size",  re.compile(r"1,?00,?000\+?\s*(sq\s*ft|sqft)", re.I)),
    ("near Kovalam Junction", re.compile(r"kovalam\s+junction", re.I)),
    ("the man-made beach/lagoon", re.compile(r"man[\s-]*made\s+beach|lagoon", re.I)),
    # Added 2026-08-19: lead 1413 got the whole amenity list verbatim in two
    # consecutive messages. It travels as one block, so it is one fact.
    ("the clubhouse amenities",
     re.compile(r"(pool|gym).{0,40}(mini\s*theatre|theatre|spa)", re.I | re.S)),
    ("the ECR distances",
     re.compile(r"(covelong|mahabalipuram|\d+\s*kms?\s+from)", re.I)),
)


# Messages that carry no searchable content. Embedding "Need More Details" finds
# nothing about the project, so the model wrote facts, the citation rule could not
# match them to a chunk, and rule 1 forced an escalation -- roughly one turn in
# three. The bot looked disobedient; it was actually being failed by retrieval.
#
# "Need More Details" is the template's own quick-reply and the FIRST thing a
# knocked buyer taps, so this is the most expensive turn in the funnel to waste.
_LOW_CONTENT = re.compile(
    r"^\s*(need\s+more\s+details?|more\s+details?|tell\s+me\s+more|send\s+details?|"
    r"details?\s*(please|pls)?|inf(o|ormation)\s*(please|pls)?|hi+|hey+|hello+|"
    r"interested|yes\s*interested|ok(ay)?|\?+)\s*[.!]?\s*$", re.I)

# What such a buyer actually wants: the project, described.
_OVERVIEW_QUERY = ("Republic of Nature overview: where it is on ECR, the size of the "
                   "community, the apartments and villas available, and the amenities")


def _retrieval_query(message):
    """What to embed. Usually the buyer's words; sometimes they carry none."""
    return _OVERVIEW_QUERY if _LOW_CONTENT.match(message or "") else message


_CRORE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:cr\b|crore|c\b)", re.I)
_LAKH = re.compile(r"(\d+(?:\.\d+)?)\s*(?:lakh|lac|l\b)", re.I)


def budget_from_text(text):
    """A rupee amount stated in this message, in rupees, or None.

    Needed because the affordability verdict is built from the checklist, which
    holds the state BEFORE this turn -- so on the very turn the buyer names their
    budget, there was no verdict and the model did the sum itself. That is exactly
    when it went wrong: "Budget is 3 to 3.5 crore" for a villa produced "that sits
    a little above what you have in mind" and an offer of apartments, for a buyer
    who qualifies.

    Takes the TOP of a range, matching how the rest of the system reads a budget:
    people understate, and "3 to 3.5" means they can find 3.5.
    """
    if not text:
        return None
    crores = [float(m) for m in _CRORE.findall(text)]
    if crores:
        return int(max(crores) * 10000000)
    lakhs = [float(m) for m in _LAKH.findall(text)]
    if lakhs:
        return int(max(lakhs) * 100000)
    return None


def _affordability_verdict(known):
    """TELL the model whether the budget reaches the configuration. Do not ask it.

    2026-08-02, live: a buyer said "3 to 3.5 c" for a 3 bedroom villa. The villa
    floor is 3.94 Cr and the stretch allowance is 25%, so 3.5 x 1.25 = 4.375 Cr and
    clears_the_bar returns QUALIFIED. The bot decided in its own head that this
    "sits a little above your band", offered apartments instead, and lost a
    genuinely qualified villa buyer.

    The prompt already said not to do borderline arithmetic. Instructing a model
    not to reason is unreliable; handing it the conclusion is not. Python does the
    sum -- the same sum clears_the_bar will do -- and the model is told the answer.
    """
    budget = known.get("budget")
    cfg = known.get("configuration")
    if not (isinstance(budget, int) and budget > 0 and cfg):
        return []
    label, floor = config.classify_configuration(cfg)
    if not label:
        return []
    if config.budget_reaches(budget, floor):
        return ["", f"AFFORDABILITY: their budget REACHES {label}. Do NOT suggest "
                    f"anything cheaper and do NOT imply it is out of reach -- buyers "
                    f"stretch, and this one qualifies. Carry on towards the visit."]
    reach = config.affordable_configs(budget)
    best = reach[-1][0] if reach else None
    if best:
        return ["", f"AFFORDABILITY: their budget does NOT reach {label}. The best "
                    f"they can reach is {best} -- name what {label} starts from and "
                    f"offer {best} warmly."]
    # BELOW EVERYTHING WE SELL, and this is where the bot used to hand over and go
    # quiet. Owner, 2026-08-03: "the logic here is not to reject but to nurture and
    # see if they are willing to make the jump ... when the jump may happen in their
    # thought process - so give that room". Nobody is called; the bot keeps talking
    # and probes for room. See the rulebook section 'When their budget is below
    # anything we sell' for how, which is editable English.
    return ["", "AFFORDABILITY: their budget reaches NOTHING in the current release. "
                "Do NOT hand this to a colleague and do NOT close the conversation. "
                "Keep helping them, and probe gently for room -- whether the figure "
                "is firm, their timeline, funding, who else decides. Follow 'When "
                "their budget is below anything we sell'."]


def _ladder(conv):
    """What is already known, and which framings are still unspent.

    Passing the REMAINING framings rather than all of them is what stops repeats:
    the model cannot reuse one it never sees. Running out of framings is itself
    information — three different ways of asking, three refusals.
    """
    if not conv:
        return "CHECKLIST: nothing known yet. Ask about purpose first."
    import conversation as convmod
    known = conv.get("checklist") or {}
    gate = convmod.next_gate(conv)
    lines = ["ALREADY KNOWN (never ask for these again):"]
    lines.append("  " + (", ".join(f"{k}={v}" for k, v in known.items()) or "nothing yet"))
    lines.extend(_affordability_verdict(known))

    # THE BUYER WHO WILL NOT NAME A BUDGET. Decided in code, not by the model --
    # see conversation.sales_offer_state. The model is told the moment has arrived
    # and given the owner's wording; it never picks the moment itself.
    offer = convmod.sales_offer_state(conv)
    if offer == "due":
        lines.append(
            "\nTHEY HAVE STEPPED AROUND THE BUDGET QUESTION TWICE. Do NOT ask for "
            "it again this turn. Answer whatever they asked, then offer them A "
            "PHONE CALL FROM OUR TEAM, in these words in spirit:\n"
            f"  {config.SALES_OFFER_FRAMING}\n"
            "It must be the CALL. Not a site visit -- a visit is a far bigger ask "
            "of somebody who is still guarding what they will spend, and offering "
            "it here loses the smaller yes we can actually get. Set "
            "gate_asked='sales_offer'. Keep it light: they are allowed to say no, "
            "and if they do we simply carry on.")
        return "\n".join(lines)
    if offer == "answered":
        lines.append(
            "\nYOU HAVE ALREADY OFFERED TO HAVE SOMEONE CALL THEM, and they still "
            "have not named a budget. Do NOT offer again and do NOT ask for the "
            "budget again.\n"
            "  If they have now ACCEPTED that offer, set action='connect_sales'.\n"
            "  If they have DECLINED it, set action='nurture' and simply carry on "
            "answering their questions warmly.\n"
            "  If they said neither, carry on as normal.")
        return "\n".join(lines)

    if not gate:
        lines.append("The checklist is COMPLETE. Do not ask another gate question.")
        return "\n".join(lines)

    # PACING. Whether this turn may ask is decided in code -- see
    # conversation.may_ask_gate -- and the model is told the answer, never asked to
    # judge it. Left to the prompt it asked something in 81% of turns.
    if not convmod.may_ask_gate(conv):
        lines.append(
            "\nDO NOT ASK A QUALIFYING QUESTION THIS TURN. They stepped around your "
            "last one, so give them a turn of pure usefulness before you come back to "
            "it -- you WILL come back to it, with a different question, next turn. "
            "Answer what they asked, warmly and well. Set gate_asked=null.\n"
            "Earn the next question by being worth talking to on this one.")
        return "\n".join(lines)

    remaining = convmod.unused_framings(conv, gate)
    all_f = config.FRAMINGS.get(gate, [])
    lines.append(f"\nNEXT GATE TO ASK: {gate}")
    if remaining:
        lines.append("Use ONE of these reasons, verbatim in spirit, and report its "
                     "index in framing_used:")
        for f in remaining:
            lines.append(f"  [{all_f.index(f)}] {f}")
    else:
        lines.append("You have already asked this three different ways with no "
                     "answer. Do NOT ask again. Just answer their question warmly "
                     "and leave it.")
    return "\n".join(lines)


def _forced_escalation(why, chunks):
    return {
        "reply": answering.RULES["escalation_reply"],
        "action": "escalate",
        "sources": [], "purpose": None, "location": None, "configuration": None,
        "budget_inr": None, "timeline": None, "visit_day": None,
        "visit_time": None, "visit_venue": None,
        "flags": ["wants_human"],
        "internal_note": f"auto-escalated: {why}",
        "gate_asked": None, "framing_used": None,
        "_forced": why,
    }


# C0 control characters. A WhatsApp message never legitimately contains one, so
# their presence means the reply is corrupt -- see _looks_corrupt.
# \x0d (carriage return) is included: a bare CR is never legitimate in a WhatsApp
# message, and a real reply on 2026-08-03 contained "Kovalam Junction \ronking about
# it" -- it was only caught by the mid-sentence rule, by luck.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x0d\x0e-\x1f\x7f]")

# How many times to ask again when the text comes back mangled. Two extra tries:
# corruption is intermittent, and clean turns sit either side of a bad one.
GARBLE_RETRIES = 2

# Failures that mean "that came out wrong", not "we cannot answer this". Only these
# are retried -- a refusal or an uncited claim is a judgement and stands.
_GARBLED = ("control character in reply", "line break mid-sentence",
            "reply ends mid-sentence", "unparseable decision",
            "no text in response", "response truncated")

_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")

# Typographic characters a model reaches for that add nothing on WhatsApp and
# render inconsistently across handsets. Mapped to plain ASCII.
_PUNCT = {
    "—": " - ", "–": " - ",          # em / en dash
    "‘": "'", "’": "'",              # curly single quotes
    "“": '"', "”": '"',              # curly double quotes
    "…": "...",                           # ellipsis
    # The rupee sign is here for a harder reason than looks. Free session text is
    # sent to Wati as a URL QUERY PARAMETER (`wati.send_text` ->
    # params={"messageText": ...}), not as a JSON body the way templates are. A
    # non-ASCII character therefore leaves us percent-encoded and we are trusting
    # someone else's decoder to put it back. It also breaks in every place a human
    # later reads the same text: Excel opens a CSV export as cp1252, and so does
    # the Windows console.
    #
    # "Rs 3.94 Cr" is ordinary Indian property language, so nothing is lost with
    # the buyer, and a whole class of encoding failure stops being possible.
    # INBOUND is deliberately untouched: buyers do type the symbol, and _MONEY /
    # _BARE_RUPEE still read it when working out their budget.
    "₹": "Rs ",                           # rupee sign -- outbound only
    " ": " ",                             # non-breaking space
}


def _clean_reply(reply):
    """Make the outbound text safe to put in front of a buyer.

    Two problems, both seen in the first live turn (2026-08-01):

    1. The model sometimes DOUBLE-escapes a unicode character inside its JSON
       reply, so json.loads yields the six literal characters ``\\u2014`` instead
       of an em dash. The buyer reads "the villa \\u2014 a weekend place". Decode
       any such leftover escape.
    2. Even decoded, typographic punctuation renders unevenly across handsets and
       buys us nothing. Fold it down to ASCII.

    Done here rather than in the prompt because a prompt is a request; every reply
    passes through _enforce, so this is a guarantee.
    """
    if not reply:
        return reply
    reply = _ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), reply)
    for bad, good in _PUNCT.items():
        reply = reply.replace(bad, good)
    reply = _dedash(reply)
    return re.sub(r"[ \t]{2,}", " ", reply).strip()


# A dash with spaces around it, used to hang a second thought off the first.
_ASIDE = re.compile(r"\s+[-–—]\s+")


def _dedash(reply):
    """Turn "X - Y" into "X. Y" (or "X, Y"), because the dash is the tell.

    THIS IS THE BIGGEST SINGLE DIFFERENCE IN REGISTER, and we were manufacturing it.
    Measured 2026-08-17 across 625 replies: the dash-as-aside appears in 70% of ours
    and 0% of the reference conversation the owner supplied. Median words per
    sentence is actually LOWER than the reference (13 against 15), so what reads as
    "high standard" is not length -- it is this one editorial construction, stacking
    a second clause onto a sentence that had finished.

    We caused it. _PUNCT folds every em and en dash down to " - ", so a model writing
    an ordinary em dash had it converted into the exact shape we did not want.

    A FULL STOP, not a comma, when what follows can stand on its own. On every real
    example a full stop read better and produced the reference's register directly:

        "...near Kovalam Junction - apartments and villas, a beach and lagoon..."
     -> "...near Kovalam Junction. Apartments and villas, a beach and lagoon..."

    Short trailing fragments ("Rs 3.94 Cr - onwards") become a comma instead, since
    splitting those would leave a one-word sentence. The rulebook asks for the same
    thing in words; this makes it true.
    """
    if not reply or not _ASIDE.search(reply):
        return reply

    parts = _ASIDE.split(reply)
    out = parts[0]
    for seg in parts[1:]:
        stripped = seg.lstrip()
        # Three or more words can carry a sentence; fewer is a trailing fragment.
        # NEVER CAPITALISE A URL. Marketing's location answer is
        # "...exact location - https://maps.app.goo.gl/..." and a naive split turned
        # that into "Https://maps...". The scheme still resolves, but it reads as
        # broken to a buyer, which is worse than the dash we were removing.
        starts_url = bool(re.match(r"(https?://|www\.)", stripped, re.I))

        def _cap(t):
            return t if starts_url else t[:1].upper() + t[1:]

        if (len(stripped.split()) >= 3 and out.rstrip()
                and not out.rstrip().endswith((".", "!", "?", ",", ":", ";"))):
            out = out.rstrip() + ". " + _cap(stripped)
        elif out.rstrip().endswith((".", "!", "?")):
            out = out.rstrip() + " " + _cap(stripped)
        else:
            out = out.rstrip().rstrip(",") + ", " + stripped
    return out


# A buyer message that carries nothing: a bare acknowledgement, not an answer.
_EMPTY_REPLY = re.compile(
    r"^\s*(y(es|eah|ep|a)?|ok(ay)?|k|sure|fine|hmm+|hm|nice|got it|alright|"
    r"right|good|👍|ok\.|"
    # A warm reaction is still no information -- and it is exactly the message the
    # model most wants to congratulate. Lead 1413 sent "This looks good..." after
    # the villa photo and got "Glad to hear that." back (2026-08-19).
    r"(this |that |it )?(looks?|sounds?) (good|nice|great|lovely|interesting)|"
    r"cool|wow|super|beautiful|"
    r"very (nice|good)|thank ?s?( you)?)\s*[.!…]*\s*$", re.I)

# How the model opens when it thinks it heard something useful.
_AFFIRMATION = re.compile(
    r"^\s*(great|perfect|good to know|excellent|wonderful|lovely|"
    r"that'?s (great|good|helpful)|noted|understood|good choice|"
    # Added 2026-08-19: the same move in different words, which is how this list
    # keeps being escaped. "Glad to hear that." on a reply that told us nothing.
    r"(so |very )?(glad|happy|pleased) to (hear|know)( that| it)?|"
    r"thanks for (that|sharing|letting me know))\b[\s,.!:—-]*", re.I)


def _strip_empty_affirmation(reply, message):
    """Do not congratulate someone for saying "yes".

    Live on 2026-08-02: asked which areas they were looking at, the buyer replied
    "Yes". The bot opened its next turn with "Great." -- affirming a reply that
    answered nothing, which reads as not listening. The prompt now forbids it;
    this makes it true.

    Only fires when the buyer's message really was empty, so a genuine "Perfect,
    that helps" after a real answer is untouched.
    """
    if not reply or not message or not _EMPTY_REPLY.match(message):
        return reply
    stripped = _AFFIRMATION.sub("", reply, count=1)
    if not stripped.strip():
        return reply                      # the affirmation was the whole reply
    return stripped[0].upper() + stripped[1:]


# Every currency marker these guards must read. Outbound text is normalised to "Rs"
# by _clean_reply, but a BUYER still types the symbol, and older corpus chunks and
# conversation history still hold it -- so each of these patterns has to accept both
# forms. Writing it once stops them drifting apart: the range guard below was
# symbol-only, and quietly stopped catching "Rs 3.94 Cr to Rs 5.5 Cr" the moment the
# currency changed. The test suite caught that; a buyer would have been the
# alternative.
_CUR = r"(?:₹|\brs\.?)"

# Any money-shaped figure, and the number inside it.
_MONEY = re.compile(_CUR + r"?\s*(\d+(?:[.,]\d+)?)\s*"
                    r"(cr\b|crore|lakh|lac|l\b)", re.I)
# A figure carrying a currency marker but NO unit -- "Rs 12000 per sq ft". _MONEY
# cannot see these because it requires cr/lakh, and an untraceable figure is the
# thing rule 1 exists to stop.
_BARE_RUPEE = re.compile(_CUR + r"\s*(\d+(?:[.,]\d+)?)", re.I)
_STARTING = re.compile(r"\b(from|starting|onwards|starts? at|begins? at)\b", re.I)
_PER_SQFT = re.compile(r"per\s*(sq|square)\s*(ft|foot|feet)|/\s*sq", re.I)
# "Rs 3.94 Cr to Rs 5.5 Cr" -- a range has a TOP, and a top reads as a cap we have
# not agreed to. Two separate starting prices ("apartments from X, villas from Y")
# are fine and deliberately do not match this.
_PRICE_RANGE = re.compile(
    r"\d[\d.,]*\s*(?:cr\b|crore|lakh|lac)?\s*(?:to|up\s*to|until|[-–—])\s*"
    + _CUR + r"?\s*\d[\d.,]*\s*(?:cr\b|crore|lakh|lac)", re.I)


def _money_figures(text):
    """Every monetary number in the text, as bare strings: {'3.94', '1.28'}."""
    out = set()
    for m in _MONEY.finditer(text or ""):
        out.add(m.group(1).replace(",", ""))
    for m in _BARE_RUPEE.finditer(text or ""):
        out.add(m.group(1).replace(",", ""))
    return out


def _price_problem(reply, chunks, cited, message):
    """Why this reply's pricing is unsendable, or None if it is fine.

    Replaces the old "any money escalates" rule. Three conditions:

    1. EVERY figure must be traceable -- present in a cited chunk, or in the
       buyer's own message (echoing "so 2 crore is fine?" back is not us quoting
       a price). An untraceable figure is an invented one, which is the failure
       the blanket ban really existed to prevent. This is stricter than the ban:
       it survives the corpus gaining prices, which the ban could not.
    2. If we are quoting a price FROM the corpus, the reply must say from /
       starting / onwards. Owner: "always say starting or onwards - so that we
       are safe." A flat price is a commitment nobody authorised.
    3. Per-square-foot rates are never allowed. They invite arithmetic against a
       specific unit, which is exactly what starting prices avoid.
    """
    figures = _money_figures(reply)
    if not figures and not _PER_SQFT.search(reply or ""):
        return None
    if _PER_SQFT.search(reply or ""):
        return "reply quoted a per-square-foot rate"
    if _PRICE_RANGE.search(reply or ""):
        return "reply quoted a price range with a top"

    cited_text = " ".join((c.get("content") or "") + " " + (c.get("guardrail") or "")
                          for c in (chunks or []) if c.get("id") in set(cited or []))
    buyer_figures = _money_figures(message or "")

    # THE FIVE APPROVED STARTING PRICES ARE ALWAYS TRACEABLE.
    #
    # They are config.CONFIG_FLOORS -- the same figures the code already uses to
    # decide whether a budget qualifies -- so quoting one is never an invention.
    #
    # Without this, a price answer depended on whether retrieval happened to return
    # the chunk holding that number. Same question, different answer, no rule behind
    # it: measured 2026-08-24, the bot quoted these five 135 times and deferred a
    # price to a human 9 times, and nothing in the system decided which. The owner
    # asked for consistency in the answering behaviour; this is where the randomness
    # was coming from.
    approved = {f"{floor / 10000000:g}" for _label, floor in config.CONFIG_FLOORS}

    from_corpus = False
    quoted = set()
    for fig in figures:
        if fig in buyer_figures:
            continue                       # their number, handed back to them
        if fig in approved:
            from_corpus = True
            quoted.add(fig)
            continue
        if fig in _money_figures(cited_text) or fig in cited_text:
            from_corpus = True
            quoted.add(fig)
            continue
        return f"reply contained an unsupported price figure ({fig})"

    # NEVER A MENU. One question, one price.
    #
    # Owner 2026-08-24: "dont just hand them price of all the units without knowing
    # what they are looking for - this is also a conversation where u can get enough
    # of their inputs".
    #
    # A real buyer asked "Project price", was handed apartments from 1.28 Cr AND
    # villas from 3.94 Cr in one reply, and answered "Very expensive sorry" seconds
    # later. Two prices, no configuration learned, no budget learned, and a buyer
    # talked out of the project by the larger number. The rules document tells the
    # bot to ask which home first; this makes it so.
    if len(quoted) > 1:
        return (f"reply quoted {len(quoted)} prices at once "
                f"({', '.join(sorted(quoted))}) -- ask which home first")

    if from_corpus and not _STARTING.search(reply or ""):
        return "quoted a price without saying from/starting/onwards"
    return None


def _looks_corrupt(reply):
    """Is this reply damaged? Returns a reason, or None.

    Seen live on 2026-08-01: the model emitted stray C0 control characters mid
    sentence with words missing around them ("Perfect \\x08ness of it"), and one
    reply stopped mid-word ("...helps me shortlist properly - do yo").

    Deliberately NOT repaired. Stripping the control character out of
    "Good \\x0cfit \\x0c., then" leaves "Good fit ., then" -- still broken, and now
    it looks fine to every check downstream. A damaged reply is escalated to a
    human, which is the same thing we do for any claim we cannot stand behind.
    """
    if not reply:
        return None
    if _CONTROL_CHARS.search(reply):
        return "control character in reply"
    # A newline is legitimate on WhatsApp, so it is not in _CONTROL_CHARS -- but a
    # newline that INTERRUPTS a sentence is the same damage wearing a legal
    # character: "...for you \nding: will this be" was a real one. A genuine line
    # break follows sentence-ending punctuation or a colon, and a genuine new line
    # does not begin lowercase mid-clause.
    if re.search(r"[^.!?:\n]\s*\n\s*[a-z]", reply):
        return "line break mid-sentence"
    # Truncated mid-word: ends on a letter with no sentence-ending punctuation.
    # A bullet or numbered last line legitimately ends without punctuation, so it
    # is exempt -- otherwise every list we send would be escalated.
    last_line = reply.rsplit("\n", 1)[-1].strip()
    listish = bool(re.match(r"^([-*•]|\d+[.)])\s", last_line))
    if len(reply) > 30 and not listish and re.search(r"[A-Za-z]$", reply):
        return "reply ends mid-sentence"
    return None


# --- how the location is named (owner rule, 2026-08-01) ----------------------
# "we dont say the word vadanemmeli - because it is not helping the luxury
# positionining plus no one knows where is vadanememli - we say - ECR Near
# Kovalam Junction".
#
# The locality name stays in the corpus because it is factually correct and helps
# retrieval when a buyer uses it. This rewrites it on the way OUT, so whatever the
# model retrieves, the buyer reads the positioning line.
# Swallows a trailing ", ECR" / " on ECR" when the retrieved text already pairs
# them, so we do not emit "ECR, near Kovalam Junction, ECR" -- but the group is
# OPTIONAL, so a bare "Vadanemmeli is..." keeps its following space.
_LOCALITY = re.compile(
    r"\bvadanemmeli\b(?:\s*,?\s*(?:on|in|at)?\s*\bECR\b"
    r"(?:\s*\(east\s+coast\s+road\))?)?", re.I)
LOCATION_PHRASE = "ECR, near Kovalam Junction"


def _rename_locality(reply):
    if not reply or not _LOCALITY.search(reply):
        return reply
    out = _LOCALITY.sub(LOCATION_PHRASE, reply)
    out = re.sub(r"\bECR,\s*(?=ECR, near Kovalam Junction)", "", out, flags=re.I)
    return re.sub(r"\s{2,}", " ", out).strip()


# --- the Experience Centre lock (owner rule, 2026-08-01) ---------------------
# The site visit IS the win. Volunteering the mall quietly converts site visits
# into mall visits, so Express Avenue is unlocked ONLY by a real distance
# OBJECTION. Owner, asked directly whether "how far is it from Adyar?" should
# unlock it: "just answer the distance and keep pushing the site."
#
# So a distance QUESTION must not match. Every pattern below is a statement of
# difficulty, never an enquiry -- "how far is X" contains "far" and is
# deliberately not matched.
_MALL = re.compile(r"experience\s+cent(?:re|er)|express\s+avenue|\bEA\s+mall\b", re.I)
_DISTANCE_OBJECTION = re.compile(
    r"\b(too\s+far|very\s+far|bit\s+far|quite\s+far|so\s+far|far\s+away|"
    r"long\s+(?:drive|way|journey|distance)|"
    r"can(?:'?t|not)\s+(?:drive|travel|come|make)|"
    r"hard\s+to\s+(?:reach|get)|difficult\s+to\s+(?:reach|travel|come)|"
    r"that'?s\s+far|its\s+far|it\s+is\s+far)\b", re.I)


def _raised_distance(message, history):
    """Did the BUYER object to the distance? Their words only, never ours."""
    texts = [message or ""]
    for h in history or []:
        if (h.get("direction") or "") == "in":
            texts.append(h.get("body") or "")
    return any(_DISTANCE_OBJECTION.search(t) for t in texts)


def _shorten(reply, cap, keep_last=False):
    """Bring a reply under `cap` characters without cutting mid-sentence.

    `keep_last` protects the final paragraph, and it exists because the first version
    of this function was wrong in a way only a dry run over real history exposed.

    The assumption was that our long replies end in padding -- an unasked-for fact,
    another nudge about a visit -- so dropping from the end would remove exactly the
    slack. Run over all 623 replies we had actually sent, it removed THE QUESTION:
    the real shape is "answer" then "gate question", one paragraph each. That is the
    opposite of the intent, and it would also have left `gate_asked` recorded for a
    question the buyer never saw, quietly spending a framing on nothing.

    So when this turn is allowed to ask, the last paragraph is protected and the trim
    eats the middle instead -- the middle is where the unasked-for fact lives. When
    the turn asks nothing, dropping from the end is right after all.

    Never returns a fragment. A single sentence longer than the cap is left intact:
    too long is recoverable, half a sentence reaching a buyer is not.
    """
    reply = (reply or "").strip()
    if len(reply) <= cap:
        return reply, None

    paras = [p.strip() for p in re.split(r"\n\s*\n", reply) if p.strip()]

    if keep_last and len(paras) > 1:
        head, tail = paras[:-1], paras[-1]
        # Drop from the END of the head -- nearest the question, furthest from the
        # answer they asked for.
        while len(head) > 1 and len("\n\n".join(head + [tail])) > cap:
            head.pop()
        out = "\n\n".join(head + [tail])
        if len(out) <= cap:
            return out, f"dropped {len(reply) - len(out)} chars, kept the question"
        # Head is one paragraph and it is still too long: shrink it by sentences,
        # never below one, so the answer and the question both survive.
        shrunk, _ = _shorten(head[0], max(cap - len(tail) - 2, 80))
        out = f"{shrunk}\n\n{tail}"
        return out, f"answer shortened to fit the question ({len(out)} chars)"

    while len(paras) > 1 and len("\n\n".join(paras)) > cap:
        paras.pop()
    out = "\n\n".join(paras)
    if len(out) <= cap:
        return out, f"dropped {len(reply) - len(out)} chars of trailing padding"

    sentences = re.split(r"(?<=[.!?])\s+", out)
    kept = []
    for s in sentences:
        candidate = " ".join(kept + [s])
        if kept and len(candidate) > cap:
            break
        kept.append(s)
    trimmed = " ".join(kept).strip()
    if not trimmed:
        return out, "over cap but unsplittable; left intact"
    return trimmed, f"trimmed to {len(trimmed)} chars from {len(reply)}"


def _strip_mall(reply):
    """Remove the sentences that offer the mall, keep the rest of the answer."""
    parts = re.split(r"(?<=[.!?])\s+", reply)
    kept = [p for p in parts if not _MALL.search(p)]
    return re.sub(r"\s{2,}", " ", " ".join(kept)).strip()


def _enforce(d, chunks, lead, message=None, history=None):
    """Mechanical checks applied to whatever the model returned.

    Everything here is a rule the prompt also states. It is repeated in code
    because a prompt is a request and this is a guarantee.
    """
    valid_ids = {c["id"] for c in chunks}
    reply = _strip_empty_affirmation(
        _rename_locality(_clean_reply(d.get("reply") or "")), message)
    d["reply"] = reply

    # 00. CORRUPTION. Before any other rule, because a damaged reply cannot be
    #     judged: the checks below read the text, and text with words missing can
    #     pass them while still being unsendable.
    damage = _looks_corrupt(reply)
    if damage:
        out = _forced_escalation(damage, chunks)
        out["internal_note"] += f" | suppressed reply: {reply[:160]!r}"
        return out

    # 0. THE EXPERIENCE CENTRE IS GONE. Owner, 2026-08-11: "stop asking for visit to
    #    experience center - we want ppl to visit the site if they are in chennai -
    #    if they are outside chennai ... we have to push them for a virtual walk
    #    thru".
    #
    #    This used to be a CONDITIONAL lock -- the mall was allowed once the buyer
    #    objected to the distance. That condition is what the owner removed: a
    #    distant buyer now gets a video walkthrough, which is a better answer than a
    #    miniature model in a shopping mall, and a Chennai buyer gets the site.
    #    So the strip is now unconditional and _raised_distance is no longer
    #    consulted. Simpler, and a guarantee instead of a judgement.
    if _MALL.search(reply):
        stripped = _strip_mall(reply)
        if stripped:
            d["reply"] = reply = stripped
            d["internal_note"] = (d.get("internal_note") or "") + \
                " | experience centre offer removed: venue retired 2026-08-11"
        else:
            # The whole reply was the mall offer; there is nothing left to send.
            out = _forced_escalation("experience centre offered; venue retired", chunks)
            out["internal_note"] += f" | suppressed reply: {reply[:160]}"
            return out
    # Stale model output can still name the retired venue in the STRUCTURED field
    # even when the reply text does not, so this normalisation runs unconditionally
    # rather than only inside the strip above -- where it silently did nothing
    # whenever the mall was named in the field but not in the sentence.
    if d.get("visit_venue") == "experience_centre":
        d["visit_venue"] = "site"

    # 0b. LENGTH. Applied AFTER the mall strip so the two cannot fight over which
    #     sentence goes, and BEFORE the citation and price checks so those judge the
    #     text a buyer will actually receive rather than a longer draft.
    #
    #     Enforced rather than requested: the rulebook has asked for "two or three
    #     lines" since it was written, and the median reply came out at 304
    #     characters. Turns of 120-240 chars were replied to at 71.6%, turns of
    #     240-400 at 42.8%.
    if len(reply) > config.MAX_REPLY_CHARS:
        asked_something = bool(d.get("gate_asked"))
        shorter, how = _shorten(reply, config.MAX_REPLY_CHARS,
                                keep_last=asked_something)
        if shorter and shorter != reply:
            d["reply"] = reply = shorter
            d["internal_note"] = (d.get("internal_note") or "") + f" | length: {how}"
        # LAST RESORT, and the reason this check exists at all: if the question did
        # not survive, the turn did not ask. Leaving gate_asked set would record a
        # question the buyer never saw, mark the gate as spent and burn one of its
        # three framings on nothing -- the conversation would then move on to the
        # next gate having never put this one.
        if asked_something and "?" not in reply:
            d["gate_asked"] = None
            d["framing_used"] = None
            d["internal_note"] += " | gate cleared: the question did not survive"

    # 1. CONFIDENCE FLOOR. A factual CLAIM with no chunk behind it is exactly how
    #    invented schools and possession dates reach a buyer.
    #
    #    `_needs_citation`, not `_looks_factual`, since 2026-08-05. The wide test
    #    fired on any reply containing a product noun, so the bot asking "what budget
    #    band did you have in mind for the villa?" was treated as an uncited claim,
    #    binned, and escalated -- one gate short of a qualified buyer. A question
    #    asserts nothing and has nothing to cite.
    cited = [s for s in (d.get("sources") or []) if s in valid_ids]
    if _needs_citation(reply) and not cited:
        out = _forced_escalation("factual claim with no supporting chunk", chunks)
        out["internal_note"] += f" | suppressed reply: {reply[:160]}"
        return out

    # 2. PRICE. The corpus holds no figure, but a model can still produce one
    #    from the buyer's own message ("so 2 crore is fine?"). Nothing that looks
    #    like money leaves this function.
    #    The blanket ban is gone (owner, 2026-08-02: "we should be able to talk
    #    about price - always say starting or onwards - so that we are safe"), and
    #    the starting prices are now a citable corpus document. Three rules replace
    #    it, and together they are stricter than the ban was about the thing that
    #    actually mattered -- an INVENTED figure.
    price_problem = _price_problem(reply, chunks, cited, message)
    if price_problem:
        return _forced_escalation(price_problem, chunks)

    # 3. Beach claim.
    if re.search(r"private beach|natural beach|own beach|beach access", reply, re.I):
        return _forced_escalation("reply implied private beach access", chunks)

    # 3b. POSSESSION / HANDOVER TIMELINE. The design doc names an invented handover
    #     date as the worst thing this bot can produce, and until 2026-08-03 the ban
    #     was prompt-only -- the citation guard could not catch it either, because
    #     `FACTUAL` had no date vocabulary, so "Handover is expected by December 2027"
    #     matched nothing, looked non-factual, skipped rule 1 and went out.
    #
    #     In real estate this is not an embarrassment, it is a commitment a buyer may
    #     act on. So it is enforced here rather than asked for.
    possession = _possession_problem(reply)
    if possession:
        return _forced_escalation(possession, chunks)

    # 3c. NAMING THE MAINTENANCE PROVIDER. Marketing, 2026-08-05 (audit Q12): no.
    #
    #     The two chunks that named it are quarantined, so in the ordinary case the
    #     name is not in the context window at all and this never fires. This is the
    #     belt: the name is also the sister brand's, it appears in this codebase, and
    #     a model that has seen it once can produce it unprompted.
    #
    #     Deliberately NOT a bare word ban. "Elements" is ordinary English and this
    #     project's own copy is nature-led -- "the elements", "natural elements" are
    #     sentences we want to send. So it fires only when the word sits next to
    #     maintenance vocabulary, which is the only context in which it is a company.
    if _maintenance_naming(reply):
        return _forced_escalation("reply named the maintenance provider", chunks)

    # 4. Visit day. Tuesday is the team's day off and Monday mornings are their
    #    weekly meeting -- a bot that books either sends someone to a locked gate.
    day = (d.get("visit_day") or "").strip().lower()
    if day:
        if day.startswith("tue"):
            return _forced_escalation("tried to book a Tuesday (team's day off)", chunks)
        if day.startswith("mon") and "after" not in (d.get("visit_time") or "").lower():
            return _forced_escalation("tried to book Monday morning (team meeting)", chunks)

    # 4b. THE MAP. Code attaches it; the model is forbidden to type a URL.
    #
    #     Lead 1413 got a correct maps link -- typed by the model, copied out of a
    #     voice sample in the rulebook. It was right that time. A model willing to
    #     type a URL will eventually type one that does not resolve, and a buyer who
    #     taps a dead link stops trusting the rest of the message. Exactly the
    #     argument for never letting it invent a price.
    #
    #     Any link the model produced is stripped first, so this is the only route a
    #     URL can reach a buyer. Sent when they ask where we are, or when a visit is
    #     booked -- directions belong with the booking, not with a callback a day
    #     later. The venue is no longer ambiguous: the Experience Centre is retired
    #     and rule 0 above strips it, so there is one place to send anyone.
    stripped = config.ANY_URL.sub("", d["reply"]).strip()
    if stripped != d["reply"]:
        d["reply"] = re.sub(r"[ \t]{2,}", " ", stripped)
        d["internal_note"] = ((d.get("internal_note") or "")
                              + " | stripped a URL the model typed")
    wants_map = bool(message and config.ASKS_LOCATION.match(message.strip())) \
        or bool(message and config.ASKS_DIRECTIONS.search(message)) \
        or bool(day and d.get("visit_venue") in (None, "", "site"))
    if wants_map and config.SITE_MAP_URL and config.SITE_MAP_URL not in d["reply"]:
        d["reply"] = d["reply"].rstrip() + f"\n\n{config.SITE_MAP_LINE}"

    # 5. BUDGET GATE — arithmetic, not judgement. The model reports the number it
    #    heard; Python decides. Only the floor rejects: someone above the ceiling
    #    is a good problem, not an unqualified lead.
    #
    #    THE COMPARISON IS STRETCHED, and that is not a loosening -- it is the same
    #    sum clears_the_bar has always done. This line compared the RAW figure until
    #    2026-08-03 while clears_the_bar stretched it, so a ₹1.1 cr buyer who can
    #    reach the ₹1.28 cr entry apartment was killed here before the stretch was
    #    ever applied. See config.ENTRY_FLOOR.
    #    AND BELOW THE FLOOR IS NURTURE, NOT DEATH. Owner, 2026-08-03: "the logic
    #    here is not to reject but to nurture and see if they are willing to make the
    #    jump - most of the leads come with the bit of an understanding of the price -
    #    if they say lower number it may be low balling - but we never know - when the
    #    jump may happen in their thought process - so give that room".
    #
    #    `nurture` is deliberately NOT in DECISION_SCHEMA's enum. The model never
    #    chooses it -- it is arithmetic, decided here, the same way the affordability
    #    verdict is computed rather than reasoned about. What the model gets is the
    #    verdict and the rulebook section telling it how to probe.
    budget = d.get("budget_inr")
    if (isinstance(budget, int) and budget > 0
            and not config.budget_reaches(budget, config.ENTRY_FLOOR)):
        # Never downgrade a better exit. Someone who names a low figure AND books a
        # visit has given us the visit; that is worth more than the number.
        if d.get("action") in ("answer", "ask", "dead"):
            d["action"] = "nurture"
        d["internal_note"] = (f"below entry floor ({budget} stretched does not reach "
                              f"{config.ENTRY_FLOOR}) -- nurturing, no handover; "
                              + (d.get("internal_note") or ""))

    d["sources"] = cited
    return d
