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
  * the corpus contains no price, so there is no figure available to leak.
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
    },
    "required": ["reply", "action", "sources", "purpose", "location",
                 "configuration", "budget_inr", "timeline", "visit_day",
                 "visit_time", "visit_venue", "flags", "internal_note"],
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
- NEVER state a price, a price range, or a per-square-foot rate. No figure exists
  in your knowledge. Every price question is `escalate` with flag `price_question`.
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
- Always offer the SITE at Vadanemmeli first. Only if they raise distance may you
  offer the Experience Centre at Express Avenue as a first look during the week,
  with the site visit at the weekend. Never offer the mall unprompted.

# Tone
Premium, calm, experience-led. Never discount-led, never pushy. Short messages —
this is WhatsApp, not a brochure. Match their language: if they write in
Tanglish or mixed Tamil and English, reply in plain simple English they will
easily follow.

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
    """Prior turns as alternating messages. Oldest first."""
    msgs = []
    for t in turns:
        role = "user" if t["direction"] == "in" else "assistant"
        text = (t.get("body") or "").strip()
        if text:
            msgs.append({"role": role, "content": text})
    return msgs


FACTUAL = re.compile(
    r"\b(km|kilomet|sqft|sq ft|acre|bhk|villa|apartment|amenit|school|hospital|"
    r"metro|beach|lagoon|clubhouse|parking|floor|phase)\b", re.I)


def _looks_factual(text):
    return bool(FACTUAL.search(text or ""))


def run_turn(lead, message, history=None):
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
    messages.append({
        "role": "user",
        "content": (f"RETRIEVED KNOWLEDGE (brand {brand_id} only):\n\n"
                    f"{_render_context(chunks)}\n\n"
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

    return _enforce(decision, chunks, lead)


def _forced_escalation(why, chunks):
    return {
        "reply": "Let me have someone from our team come back to you on this.",
        "action": "escalate",
        "sources": [], "purpose": None, "location": None, "configuration": None,
        "budget_inr": None, "timeline": None, "visit_day": None,
        "visit_time": None, "visit_venue": None,
        "flags": ["wants_human"],
        "internal_note": f"auto-escalated: {why}",
        "_forced": why,
    }


def _enforce(d, chunks, lead):
    """Mechanical checks applied to whatever the model returned.

    Everything here is a rule the prompt also states. It is repeated in code
    because a prompt is a request and this is a guarantee.
    """
    valid_ids = {c["id"] for c in chunks}
    reply = d.get("reply") or ""

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
    if re.search(r"₹|\brs\.?\s*\d|\bcrore\b|\blakh\b|\bcr\b\s*\d|per sq", reply, re.I):
        return _forced_escalation("reply contained a price", chunks)

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
