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
        "action": {"type": "string",
                   "enum": ["answer", "ask", "escalate", "qualified", "dead"]},
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
        "visit_venue": {"anyOf": [{"type": "string",
                                   "enum": ["site", "experience_centre"]},
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
                                           "budget"]},
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


FACTUAL = re.compile(
    r"\b(km|kilomet|sqft|sq ft|acre|bhk|villa|apartment|amenit|school|hospital|"
    r"metro|beach|lagoon|clubhouse|parking|floor|phase)\b", re.I)


def _looks_factual(text):
    return bool(FACTUAL.search(text or ""))


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

    return _enforce(decision, chunks, lead, message=message, history=history)


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
    said = []
    for m in history or []:
        if m.get("role") != "assistant":
            continue
        for fig in sorted(_money_figures(m.get("content") or "")):
            if fig not in said:
                said.append(fig)
    if not said:
        return []
    return ["", "ALREADY QUOTED to this buyer: " + ", ".join("Rs " + s for s in said)
                + ". Do NOT state these figures again -- they have them. Refer back "
                  "briefly if you must ('as I mentioned') and otherwise move on. "
                  "Quote a price again ONLY if they ask again, or for a "
                  "configuration you have not priced yet."]


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
    if not gate:
        lines.append("The checklist is COMPLETE. Do not ask another gate question.")
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
    return re.sub(r"[ \t]{2,}", " ", reply).strip()


# A buyer message that carries nothing: a bare acknowledgement, not an answer.
_EMPTY_REPLY = re.compile(
    r"^\s*(y(es|eah|ep|a)?|ok(ay)?|k|sure|fine|hmm+|hm|nice|got it|alright|"
    r"right|good|👍|ok\.)\s*[.!]?\s*$", re.I)

# How the model opens when it thinks it heard something useful.
_AFFIRMATION = re.compile(
    r"^\s*(great|perfect|good to know|excellent|wonderful|lovely|"
    r"that'?s (great|good|helpful)|noted|understood|good choice|"
    r"thanks for (that|sharing))\b[\s,.!:—-]*", re.I)


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


# Any money-shaped figure, and the number inside it.
_MONEY = re.compile(r"(?:₹|\brs\.?\s*)?\s*(\d+(?:[.,]\d+)?)\s*"
                    r"(cr\b|crore|lakh|lac|l\b)", re.I)
_BARE_RUPEE = re.compile(r"₹\s*(\d+(?:[.,]\d+)?)")
_STARTING = re.compile(r"\b(from|starting|onwards|starts? at|begins? at)\b", re.I)
_PER_SQFT = re.compile(r"per\s*(sq|square)\s*(ft|foot|feet)|/\s*sq", re.I)
# "₹3.94 Cr to ₹5.5 Cr" -- a range has a TOP, and a top reads as a cap we have not
# agreed to. Two separate starting prices ("apartments from X, villas from Y") are
# fine and deliberately do not match this.
_PRICE_RANGE = re.compile(
    r"\d[\d.,]*\s*(?:cr\b|crore|lakh|lac)?\s*(?:to|up\s*to|until|[-–—])\s*"
    r"₹?\s*\d[\d.,]*\s*(?:cr\b|crore|lakh|lac)", re.I)


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

    from_corpus = False
    for fig in figures:
        if fig in buyer_figures:
            continue                       # their number, handed back to them
        if fig in _money_figures(cited_text) or fig in cited_text:
            from_corpus = True
            continue
        return f"reply contained an unsupported price figure ({fig})"

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

    # 0. THE EXPERIENCE CENTRE LOCK. The mall may only be offered against a real
    #    distance objection. Seen on the first live turn: the buyer asked "how far
    #    is this from Adyar" -- a question -- and the model volunteered the mall
    #    ("if the drive feels long..."), pre-empting an objection never made. The
    #    prompt already forbids this; enforcing it here is what makes it true.
    if _MALL.search(reply) and not _raised_distance(message, history):
        stripped = _strip_mall(reply)
        if stripped:
            d["reply"] = reply = stripped
            d["internal_note"] = (d.get("internal_note") or "") + \
                " | mall offer removed: no distance objection from buyer"
        else:
            # The whole reply was the mall offer; there is nothing left to send.
            out = _forced_escalation("mall offered with no distance objection", chunks)
            out["internal_note"] += f" | suppressed reply: {reply[:160]}"
            return out
        if d.get("visit_venue") == "experience_centre":
            d["visit_venue"] = "site"

    # 1. CONFIDENCE FLOOR. A factual-sounding claim with no chunk behind it is
    #    exactly how invented schools and possession dates reach a buyer.
    cited = [s for s in (d.get("sources") or []) if s in valid_ids]
    if _looks_factual(reply) and not cited:
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

    # 4. Visit day. Tuesday is the team's day off and Monday mornings are their
    #    weekly meeting -- a bot that books either sends someone to a locked gate.
    day = (d.get("visit_day") or "").strip().lower()
    if day:
        if day.startswith("tue"):
            return _forced_escalation("tried to book a Tuesday (team's day off)", chunks)
        if day.startswith("mon") and "after" not in (d.get("visit_time") or "").lower():
            return _forced_escalation("tried to book Monday morning (team meeting)", chunks)

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
