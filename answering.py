"""The answering rules — one place for everything the bot SAYS.

Ported from the AskAshwin pattern (D:/AAAI/askashwin/src/content/rules.ts), whose
own docstring names the problem exactly: wording spread across several code files
meant "in practice it did not get changed". GTB had it worse -- a 795-line prompt
string, 13 code guards, 33 framings in config, 16 corpus guardrails and 6 prompt
blocks. Five places to put a rule is five places for it to go missing, which is how
a villa-size caveat ended up in one document while the wrong figure sat in another
and reached a real buyer.

WHAT IS DIFFERENT HERE, AND WHY IT IS NOT A STRAIGHT COPY. AskAshwin answers
questions; this bot QUALIFIES and ESCALATES. A question-answering bot's rules are
almost entirely about how to speak, so prose covers them. A qualifying bot's rules
include decisions -- floors per configuration, a stretch allowance, gate order,
which exit routes where -- and 2026-08-02 proved those cannot live in prose: the
instruction "do not do this arithmetic in your head" was ignored twice, and a
qualified buyer was talked out of a villa. Computing it in Python and handing the
model the conclusion worked first time.

So the split is deliberate:

    THIS FILE          the words. Voice, wording, the reason given for each ask,
                       the copy for a handover. Owner edits English, no deploy of
                       logic required.

    config / qualifier  the decisions. CONFIG_FLOORS, BUDGET_STRETCH,
                       clears_the_bar, the guards in _enforce. Not negotiable and
                       not restatable here.

AND THE RULE THAT STOPS THE DRIFT: anything the code ENFORCES must not also be
asserted as a rule in the document. Where both are involved the document says what
to SAY, and notes that the code decides. Two copies of one rule is how they diverge.

Validation is strict on purpose. A section quietly missing would mean the bot
answering a real buyer without its price rules, so a malformed document raises and
the process refuses to start -- the previous deployment keeps answering. A missing
section is never filled in with a default.
"""
import os
import re

# Headings are the contract. Change one in the document and the section goes
# missing -- which is why an unknown heading is REPORTED rather than ignored.
SECTIONS = {
    "Who you are": "identity",
    "Voice": "voice",
    "Language": "language",
    "How a turn works": "turn",
    "What you are trying to learn": "gates",
    "Naming the location": "location_wording",
    "Never apologise for the project": "no_apology",
    "Talking about price": "price_wording",
    "When they ask what they get for it": "value_question",
    "When their budget does not reach": "pivot_wording",
    "When their budget is below anything we sell": "below_entry",
    "When they ask about the GTB Carnival": "carnival",
    "When they ask vaguely for information": "vague_ask",
    "When their reply says nothing": "empty_reply",
    "Saying a price more than once": "repetition",
    "Site visits": "visits",
    "Never say these": "never_say",
    "Actions": "actions",
    "Handover — qualified": "handover_qualified",
    "Handover — escalated": "handover_escalated",
    "When we cannot answer": "escalation_reply",
}

# Every section must be present and non-trivial. A one-word price section is a
# mistake, and the mistake reaches a buyer.
MIN_CHARS = {
    "identity": 60, "voice": 80, "language": 60, "turn": 60, "gates": 80,
    "location_wording": 40, "no_apology": 60, "price_wording": 120,
    "value_question": 80, "pivot_wording": 80, "below_entry": 200, "carnival": 150, "vague_ask": 60,
    "empty_reply": 60, "repetition": 40, "visits": 60, "never_say": 80,
    "actions": 120, "handover_qualified": 80, "handover_escalated": 80,
    "escalation_reply": 20,
}

# Sections assembled into the system prompt, in this order. Listed explicitly
# rather than derived from SECTIONS, so adding a heading to the document cannot
# silently change what the model is told -- the wiring stays a deliberate act.
PROMPT_ORDER = ("identity", "turn", "gates", "price_wording", "value_question",
                "pivot_wording", "below_entry", "carnival", "vague_ask",
                "location_wording", "visits",
                "never_say", "voice", "language", "no_apology", "empty_reply",
                "repetition", "actions")

# Sections used as their own block at the point of need, never in the prompt body.
STANDALONE = ("handover_qualified", "handover_escalated", "escalation_reply")

_HEADING = {v: k for k, v in SECTIONS.items()}


class RulesError(RuntimeError):
    """The document is unusable. Raised at import so a bad edit cannot go live."""


def parse(markdown):
    """Markdown -> {field: text}. Also returns headings it did not recognise.

    `>` lines are notes to whoever edits the document and are stripped, so
    guidance can sit beside a rule without becoming part of what the bot is told.
    """
    sections, current, unknown = {}, None, []
    for line in (markdown or "").replace("\r\n", "\n").split("\n"):
        h2 = re.match(r"^##\s+(?!#)(.*)$", line)
        if h2:
            heading = h2.group(1).strip()
            field = SECTIONS.get(heading)
            if field is None:
                unknown.append(heading)
                current = None
            else:
                current = field
                sections[field] = ""
            continue
        if current is None:
            continue
        if re.match(r"^#\s", line) or re.match(r"^-{3,}\s*$", line):
            continue
        if re.match(r"^\s*>", line):
            continue
        sections[current] += line + "\n"
    return sections, unknown


def validate(sections, unknown):
    problems = []
    for field in SECTIONS.values():
        text = (sections.get(field) or "").strip()
        if not text:
            problems.append(f"missing or empty section: '{_HEADING[field]}'")
        elif len(text) < MIN_CHARS.get(field, 20):
            problems.append(f"section '{_HEADING[field]}' is only {len(text)} chars; "
                            f"expected at least {MIN_CHARS.get(field, 20)}")
    for heading in unknown:
        problems.append(f"unrecognised heading '{heading}' -- nothing reads it, so "
                        f"the edit does nothing. Fix the heading or add it to SECTIONS.")
    if problems:
        raise RulesError("answering-rules.md is unusable:\n  - " + "\n  - ".join(problems))
    return {k: v.strip() for k, v in sections.items()}


def load(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "content", "answering-rules.md")
    if not os.path.exists(path):
        raise RulesError(f"answering-rules.md not found at {path}")
    with open(path, encoding="utf-8") as fh:
        return validate(*parse(fh.read()))


RULES = load()


def system_prompt(brand_name):
    """Assemble the system prompt. Stable per brand, so it caches."""
    parts = [f"You are the presales assistant for {brand_name}, answering buyers on "
             f"WhatsApp."]
    for field in PROMPT_ORDER:
        parts.append(f"# {_HEADING[field]}\n{RULES[field]}")
    return "\n\n".join(parts)
