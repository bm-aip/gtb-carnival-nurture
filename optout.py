"""Opt-out detection and ledger (Phase 0, task 2).

Two things live here: deciding whether an inbound message is an opt-out, and
recording/reading the ledger. `sendgate.check()` consults `is_blocked()`; nothing
else may decide whether a person is contactable.

WHY DETECTION IS DELIBERATELY DUMB
----------------------------------
This module matches phrases. It does not ask a language model whether someone
meant to opt out, and it must not be changed to. An agent that judges intent will
sometimes judge wrong, and being wrong here means messaging a person who told us
to stop -- a compliance incident, not a bug. So the hard stop runs BEFORE the
agent ever sees the message, on rules a human can read and audit.

The qualifier agent (task 20) may later classify the softer, ambiguous cases into
the 'project' scope, where a wrong call costs one project rather than a person's
permanent record. It may never write a 'global' row.

TWO SCOPES (owner decision, 2026-07-30)
---------------------------------------
global  -- permanent, all projects. Explicit stop words, the Stop-updates button,
           and wrong-number / by-mistake replies. The last group is global rather
           than per-project because it means this phone is not the lead at all,
           so no project has any business contacting it.
project -- "not interested" and equivalents. Stops this project, keeps the
           record, leaves another project free to reach them later. The owner's
           reasoning: "not interested" usually means not interested in THIS.
"""
import re

import db

# --- Phrase banks ------------------------------------------------------------
#
# Multi-word phrases are matched anywhere in the message with word boundaries.
# Single words are matched ONLY when they are effectively the whole message --
# see _has_phrase. "stop" is the reason for that rule: "bus stop", "stop by
# tomorrow" and "non-stop flight" are not opt-outs, and treating them as one
# would silently delete a live buyer.

STOP_PHRASES_STANDALONE = (
    "stop", "unsubscribe", "stopall", "cancel", "block", "blocked",
)

STOP_PHRASES_ANYWHERE = (
    "stop updates", "stop messaging", "stop sending", "stop this",
    "unsubscribe me", "remove my number", "delete my number",
    "do not message", "dont message", "don't message",
    "do not contact", "dont contact", "don't contact",
    "do not call", "dont call", "don't call",
    "do not disturb", "dont disturb", "don't disturb",
    "opt out", "opt-out", "leave me alone", "no more messages",
    "not again",
)

# "Remove me" / "take me off" are genuine opt-outs in isolation, but they are also
# how an engaged buyer asks to leave one narrow thing: "please remove me from the
# waitlist for 2BHK" is a live conversation, and permanently blocking that person
# from every project is a far worse error than failing to block them. So this
# family is checked separately and stands down whenever the message also mentions
# something we sell.
#
# Note the asymmetry, which is intentional: a hard stop is NOT downgraded by
# product context. "Stop sending me 2BHK offers" still means stop.
STOP_PHRASES_AMBIGUOUS = (
    "remove me", "take me off",
)

PRODUCT_CONTEXT = (
    "waitlist", "wait list", "shortlist", "unit", "bhk", "villa", "villament",
    "apartment", "apartments", "flat", "plot", "booking", "site visit",
    "brochure", "floor plan", "price list",
)

# Wrong number / accidental enquiry -> GLOBAL, not project. If this phone is not
# the person we think it is, every project is equally wrong to message it.
WRONG_NUMBER_PHRASES = (
    "wrong number", "wrong no", "not my number", "you have the wrong",
    "by mistake", "by mistek", "sent by mistake", "clicked by mistake",
    "accidental", "accidentally", "didn't mean to", "didnt mean to",
    "who is this number", "i did not enquire", "i didnt enquire",
    "i did not enquiry", "never enquired",
)

# Softer signals -> PROJECT scope. Stops this project, record kept.
NOT_INTERESTED_PHRASES = (
    "not interested", "no longer interested", "lost interest",
    "not intrested", "not intersted",              # common misspellings
    "not looking", "not searching", "no plans to buy", "not buying",
    "already bought", "already purchased", "already booked",
    "bought elsewhere", "purchased elsewhere",
    "dropped the plan", "cancelled the plan", "no need", "not required",
)

GLOBAL = "global"
PROJECT = "project"


def _norm(text):
    """Lowercase, strip punctuation and emoji down to letters, digits, spaces."""
    t = (text or "").lower().strip()
    t = re.sub(r"[^a-z0-9\s']", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _has_phrase(norm, phrase):
    return re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", norm) is not None


def classify(text):
    """Return (scope, matched_phrase) or (None, None).

    Checked in order of severity: an explicit stop wins over "not interested",
    because a message containing both is unambiguously the stronger signal.
    """
    norm = _norm(text)
    if not norm:
        return None, None

    # Single-word stops only count when they are essentially the entire message.
    # Up to three words tolerates "stop please" and "please stop now" without
    # catching "the bus stop near the site".
    words = norm.split()
    if len(words) <= 3:
        for w in STOP_PHRASES_STANDALONE:
            if w in words:
                return GLOBAL, w

    for p in STOP_PHRASES_ANYWHERE:
        if _has_phrase(norm, p):
            return GLOBAL, p

    # Ambiguous family: only an opt-out when the message is not about a product.
    # Failing to block someone who meant it is recoverable -- they will say it
    # again, more bluntly. Permanently blocking a buyer who was mid-enquiry is not.
    if not any(_has_phrase(norm, c) for c in PRODUCT_CONTEXT):
        for p in STOP_PHRASES_AMBIGUOUS:
            if _has_phrase(norm, p):
                return GLOBAL, p

    for p in WRONG_NUMBER_PHRASES:
        if _has_phrase(norm, p):
            return GLOBAL, p

    for p in NOT_INTERESTED_PHRASES:
        if _has_phrase(norm, p):
            return PROJECT, p

    return None, None


def record(phone, scope, project=None, matched=None, source="inbound_keyword",
           note=None):
    """Add a ledger row. Idempotent; returns True if this was new.

    A 'global' row never carries a project even if one is passed -- storing one
    would imply the block is narrower than it is.
    """
    if not phone:
        return False
    if scope == GLOBAL:
        project = None
    n = db.x("""INSERT INTO optouts (phone, scope, project, matched, source, note)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING""",
             (phone, scope, project, matched, source, note))
    return n == 1


def is_blocked(phone, project=None):
    """(blocked, scope) for this phone, considering `project` if given.

    A global row blocks regardless of project. A project row blocks only its own
    project -- and only when a project is supplied: a caller that does not know
    which project it is sending for cannot be allowed to bypass a project block,
    so an unknown project is treated as blocked by ANY project row.
    """
    if not phone:
        return False, None
    rows = db.q("SELECT scope, project FROM optouts WHERE phone=%s", (phone,)) or []
    for r in rows:
        if r["scope"] == GLOBAL:
            return True, GLOBAL
    for r in rows:
        if r["scope"] == PROJECT and (project is None or r["project"] == project):
            return True, PROJECT
    return False, None


def apply_to_leads(phone, scope, project=None):
    """Mirror the ledger onto the lead rows so existing queries see it.

    The ledger is the authority -- every send consults it -- but `leads.suppressed`
    is what the dashboard and the older lead queries filter on, so keeping the two
    in step avoids a suppressed person still appearing as an active lead.
    """
    if scope == GLOBAL:
        return db.x("""UPDATE leads SET suppressed=TRUE, updated_at=now()
                       WHERE phone=%s AND NOT suppressed""", (phone,))
    return db.x("""UPDATE leads SET suppressed=TRUE, updated_at=now()
                   WHERE phone=%s AND project=%s AND NOT suppressed""",
                (phone, project))


def handle_inbound_text(phone, text, project=None):
    """Classify one inbound message and act on it. Returns (scope, matched).

    Called from `sequencer.handle_inbound` on EVERY inbound message, before
    anything else looks at the text.
    """
    scope, matched = classify(text)
    if not scope:
        return None, None
    record(phone, scope, project=project, matched=matched)
    apply_to_leads(phone, scope, project=project)
    return scope, matched
