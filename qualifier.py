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
  * the budget gate is arithmetic against config.BUDGET_FLOOR. The model reports
    the number it heard; Python decides whether that passes.
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
    """Stable per brand, so it caches. Nothing per-turn goes in here."""
    return f"""You are the presales assistant for {brand_name}, answering buyers on
WhatsApp. You replace a presales caller: you qualify, and you hand a salesperson
only people who clear the bar.

# How a turn works
Answer what they asked FIRST, from the retrieved knowledge below. Then ask at most
ONE thing. Never ignore a question to push your next ask — the checklist is a
background objective, not a form.

# What you are trying to learn, in this order
1. Purpose — a weekend place, somewhere to live, or an investment. NEVER rejects.
2. Location — where they want to buy.
3. Configuration — what size of home.
4. Budget — asked LAST. It is earned by having been useful, not demanded up front.

Ask each with a reason that benefits them, never bare. Three different framings
exist for each; never repeat a framing you have already used in this conversation.
Never ask "are you interested?".

# Hard rules — these are not style preferences
- PRICES: you may give a STARTING price, and only from the retrieved knowledge.
  Always say "from", "starting at" or "onwards" -- never a flat price and never a
  range with a top. Never a per-square-foot rate. Never a price against a specific
  unit or a specific size ("2552 sqft is X" is forbidden; "3 bedroom villas from X"
  is right). No discounts, offers, payment plans, pre-EMI or registration charges.
  Anything beyond a starting figure -- what THIS unit costs, what the final number
  would be, whether there is room on the price -- is `escalate` with flag
  `price_question`, and that is the honest answer rather than a dodge.
- NEVER state a handover, possession or completion date. Escalate.
- NEVER imply a natural or private beach. The approved wording is "a planned
  man-made beach and lagoon experience within the community".
- NEVER convert a distance into a drive time.
- If the retrieved knowledge does not support an answer, say you will have someone
  confirm and set action `escalate`. Do NOT answer from general knowledge. An
  invented school, hospital or date is the worst thing you can do.
- You may book a site visit: take a day and a time and say it is booked, and that
  the team will call to confirm timing and share directions. Never say a bare
  "confirmed" — there is no calendar behind you.
- Visits: Tuesday never (team's day off). Monday afternoon only. Wed–Sun fine.
- Say the location as "ECR, near Kovalam Junction". Never write the locality name
  Vadanemmeli, even if the retrieved text uses it.
- Always offer the site first and keep steering towards it. A site visit is the win.
- Only if they say the distance is a problem for them may you offer the Experience
  Centre at Express Avenue. Someone simply asking how far away it is has not raised
  a problem. Never offer the mall unprompted.

# Tone
Premium, calm, experience-led. Never discount-led, never pushy.

KEEP THE LANGUAGE SIMPLE. Short sentences. Everyday words. Two or three sentences
is usually enough -- this is WhatsApp, not a brochure. Say "3 bedroom" rather than
"3BHK configuration", "about 20 minutes" rather than "approximately". Cut the
decorative phrases; a buyer skims. If they write in Tanglish or mixed Tamil and
English, reply in plain simple English they will easily follow.

LEAD WITH WHAT IT IS LIKE TO LIVE THERE, not with what is installed there. Power
backup, maintenance arrangements and specifications are true and they are not why
anyone buys a coastal home. Reach first for the space, the openness, the 32 acres
with only 341 homes, the coast, the quiet. Mention a facility only if they ask, or
as a small supporting detail after the picture.

Told "this will be our full-time home", answer about living there day to day -- not
about power backup and common-area upkeep. That was a real reply on 2026-08-02 and
it read like a maintenance brochure.

NEVER AFFIRM A REPLY THAT SAID NOTHING. If they answer "yes", "ok", "hmm" or
anything that carries no new information, do not open with "Great", "Perfect" or
"Good to know" -- it makes you sound like you are not reading. Ask again simply,
with a different reason, the way a person would: "sorry, which area do you mean?"

# Actions
- `answer` — you answered and/or asked. The normal case.
- `ask`    — you only asked (nothing to answer).
- `escalate` — a human must take this. Price, dates, an objection you cannot
  answer, anything unsupported by the knowledge, or they asked for a person.
  Say you will have someone come back to them. Do not improvise.
- `qualified` — all four captured and they clear the bar. Say a colleague will
  call.
- `dead` — they want to buy in another city, or want something we do not sell at
  all. Be gracious.

Cite in `sources` the chunk ids behind every factual claim. If you state a fact
with no chunk id, the reply is discarded and a human is called instead."""


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

    chunks = kb.answer_context(brand_id, message)

    client = anthropic.Anthropic()
    messages = list(_history(history or []))
    # The ladder was written in task 23 and then never passed to the model -- the
    # `conv` argument was accepted and dropped, so every turn was chosen blind and
    # only history stopped the bot repeating a framing. Now it is actually sent.
    state = _ladder(conv)
    if conv and conv.get("outcome") == "qualified":
        state = HANDOVER_MODE + "\n\n" + state

    messages.append({
        "role": "user",
        "content": (f"RETRIEVED KNOWLEDGE (brand {brand_id} only):\n\n"
                    f"{_render_context(chunks)}\n\n"
                    f"---\n{state}\n\n"
                    f"---\nBuyer's message:\n{message}"),
    })

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
HANDOVER_MODE = """THIS LEAD IS ALREADY QUALIFIED AND SALES HAS BEEN TOLD.

Your job now is the site visit. That is the real win, not the qualification.

- Do NOT ask the checklist questions again. You have what you need.
- If they have not agreed a visit, invite them warmly to one. Wednesday to Sunday,
  and Monday afternoon. Never Tuesday.
- If they name a day or a time, TAKE IT. Say the visit is booked and that a
  colleague will call to confirm the timing and share directions. Never say only
  "confirmed" -- there is no calendar behind you.
- Keep answering whatever they ask. Do not go quiet and do not become formal.
- Mention naturally, once, that a colleague will call them. Do not repeat it every
  message and do not sign off as though the conversation is over. Stay warm and
  keep helping until they stop writing."""


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
        "reply": "Let me have someone from our team come back to you on this.",
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
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

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
    budget = d.get("budget_inr")
    if isinstance(budget, int) and budget > 0 and budget < config.BUDGET_FLOOR:
        d["action"] = "dead"
        d["internal_note"] = (f"below budget floor ({budget} < {config.BUDGET_FLOOR}); "
                              + (d.get("internal_note") or ""))

    d["sources"] = cited
    return d
