"""Failure classification and the retry ceiling (Phase 0, task 4).

THE MISTAKE THIS AVOIDS
-----------------------
Before Phase 0 the code incremented `send_attempts` and deliberately never used
it. The comment at the old `sequencer.py:252` explained why, and it was right:
while the WhatsApp templates were still pending approval, EVERY send failed for a
reason that had nothing to do with the lead. A three-strike rule would have
quietly killed off perfectly good buyers.

So a retry ceiling cannot simply count failures. It has to know whose fault the
failure was:

  RECIPIENT -- their number is not on WhatsApp, or they have blocked us. Real
               evidence about this person. Low ceiling.
  TRANSIENT -- timeout, 5xx, rate limit, and anything we do not recognise. Says
               nothing about the lead. Higher ceiling.
  SYSTEM    -- our own fault: template not approved, bad token, 24-hour window
               shut. NO CEILING AT ALL. A lead must never be killed off by our
               misconfiguration, which is exactly the trap above.

Unrecognised failures are classed TRANSIENT, never RECIPIENT. If the provider
invents new wording, the failure mode is "we keep trying a bit longer", not "we
silently discard a buyer".

WHY A BLOCK DOES NOT BECOME A PERMANENT OPT-OUT
-----------------------------------------------
It is tempting: a delivery callback saying the recipient blocked us is an opt-out
expressed by action rather than words, and wiring it into the permanent ledger
would be neat. Rejected. That ledger is cross-project and nothing in the code can
undo it, and the only evidence would be a keyword match against a provider's error
string -- text that changes without notice and can carry the word "blocked" for
reasons that have nothing to do with the recipient (a blocked template, a blocked
account). Suppressing the lead is reversible and proportionate; a permanent
all-projects block on that basis is not. A human can always escalate it via
/admin/optout.
"""
import config
import db

RECIPIENT = "recipient"
TRANSIENT = "transient"
SYSTEM = "system"

CEILING_RECIPIENT = "retry_ceiling_recipient"
CEILING_TRANSIENT = "retry_ceiling_transient"

# Order of evaluation matters and is not alphabetical. SYSTEM is checked before
# TRANSIENT because "Message failed to send because more than 24 hours have
# passed" is our own error and must not consume anyone's retry budget.
_RECIPIENT_PHRASES = (
    "does not exist", "not a valid whatsapp", "invalid whatsapp number",
    "not a whatsapp", "no whatsapp account", "unregistered",
    "not registered", "recipient not found", "invalid recipient",
    "number does not have whatsapp",
)

# Recipient-attributable refusal. Counts toward the recipient ceiling; explicitly
# does NOT create a permanent opt-out -- see the module docstring.
_REFUSED_PHRASES = (
    "recipient has blocked", "user has blocked", "blocked by the user",
    "not opted in", "131050", "user is not opted in",
)

_SYSTEM_PHRASES = (
    "24 hour", "24 hours", "no active session", "session expired",
    "outside the session", "template", "not approved", "template paused",
    "unauthorized", "unauthorised", "invalid token", "authentication",
    # "restrict" as a stem, not "account restricted": the live wording is "Your
    # account has been restricted", and matching the exact phrase missed it. A
    # restriction is the single most important failure to classify correctly --
    # it means the WhatsApp number is in trouble, which would otherwise look like
    # a wave of ordinary transient errors.
    "forbidden", "quality rating", "restrict", "tier limit",
    "messaging limit", "not registered for cloud api", "parameter",
    "bad request",
)

_TRANSIENT_PHRASES = (
    "timeout", "timed out", "connection", "connectionerror", "read timed out",
    "rate limit", "too many requests", "429", "500", "502", "503", "504",
    "temporar", "try again", "unavailable", "gateway",
)


def classify(detail):
    """Return one of RECIPIENT / TRANSIENT / SYSTEM for a failure string.

    Never returns None: an unclassifiable failure is still a failure, and the safe
    default is TRANSIENT, which delays rather than discards.
    """
    d = (detail or "").lower()
    if not d:
        return TRANSIENT
    if any(p in d for p in _RECIPIENT_PHRASES):
        return RECIPIENT
    if any(p in d for p in _REFUSED_PHRASES):
        return RECIPIENT
    if any(p in d for p in _SYSTEM_PHRASES):
        return SYSTEM
    if any(p in d for p in _TRANSIENT_PHRASES):
        return TRANSIENT
    return TRANSIENT


def is_hard_recipient_failure(detail):
    """True only for "this number cannot receive WhatsApp at all".

    Kept separate from classify() because this one justifies suppressing the lead
    on the FIRST occurrence -- retrying a number that is not on WhatsApp will never
    succeed, so a ceiling of 3 would just waste two more sends.
    """
    d = (detail or "").lower()
    return any(p in d for p in _RECIPIENT_PHRASES)


def last_delivered_at(phone):
    """When we last got a message THROUGH to this person.

    "Delivered" rather than "accepted by the API": an accepted-then-failed message
    proves nothing about the number. Falls back to a successful send when no
    delivery callbacks exist yet, so the ceiling still resets sensibly before the
    delivery feed is live.
    """
    r = db.q("""SELECT max(ts) AS ts FROM (
                  SELECT created_at AS ts FROM message_delivery
                   WHERE phone=%s AND status IN ('delivered','read')
                  UNION ALL
                  SELECT ml.ts FROM message_log ml JOIN leads l ON l.id=ml.lead_id
                   WHERE l.phone=%s AND ml.direction='out' AND ml.ok
                ) s""", (phone, phone), one=True)
    return (r or {}).get("ts")


def counts(phone, days=None):
    """Failures per class for this person, since the last message that got through.

    Counted from history rather than a stored counter, same reasoning as the
    fatigue cap: a stored number can be silently zeroed, a query cannot. That also
    gives "reset on success" for free -- one delivered message means the number
    works, so earlier failures stop being evidence about it.

    Both sources are consulted. A send that failed outright appears in
    message_log; a send WhatsApp accepted and then failed appears only as a
    delivery callback, and that is the common shape of a block.
    """
    days = days if days is not None else config.RETRY_WINDOW_DAYS
    since = last_delivered_at(phone)
    out = {}

    rows = db.q("""SELECT ml.fail_class, count(*) AS n
                   FROM message_log ml JOIN leads l ON l.id = ml.lead_id
                   WHERE l.phone = %s AND ml.direction='out' AND ml.ok = FALSE
                     AND ml.fail_class IS NOT NULL
                     AND ml.ts > now() - (%s * interval '1 day')
                     AND (%s IS NULL OR ml.ts > %s)
                   GROUP BY ml.fail_class""",
                (phone, days, since, since)) or []
    for r in rows:
        out[r["fail_class"]] = out.get(r["fail_class"], 0) + r["n"]

    rows = db.q("""SELECT fail_class, count(*) AS n FROM message_delivery
                   WHERE phone = %s AND status='failed' AND fail_class IS NOT NULL
                     AND created_at > now() - (%s * interval '1 day')
                     AND (%s IS NULL OR created_at > %s)
                   GROUP BY fail_class""",
                (phone, days, since, since)) or []
    for r in rows:
        out[r["fail_class"]] = out.get(r["fail_class"], 0) + r["n"]

    return out


def check(phone, msg_type=None, project=None):
    """(allowed, reason) for the retry ceiling.

    Only failures SINCE THE LAST SUCCESSFUL SEND count -- that is the "reset on
    success" rule, and it falls out of the query rather than needing a counter to
    be cleared. One good delivery means the number works, so old failures stop
    being evidence about it.
    """
    if not phone:
        return True, None
    c = counts(phone)
    if c.get(RECIPIENT, 0) >= config.RETRY_MAX_RECIPIENT:
        return False, CEILING_RECIPIENT
    if c.get(TRANSIENT, 0) >= config.RETRY_MAX_TRANSIENT:
        return False, CEILING_TRANSIENT
    # SYSTEM deliberately absent. No ceiling, by design.
    return True, None


def rollup(days=7):
    """Failure classes across the whole system, for the dashboard.

    A spike in `system` is an alarm about US -- an expired token, an unapproved
    template -- and it is the one class that will never show up as a blocked lead,
    so it needs somewhere to be visible.
    """
    rows = db.q("""SELECT fail_class, count(*) AS n FROM message_log
                   WHERE direction='out' AND ok = FALSE AND fail_class IS NOT NULL
                     AND ts > now() - (%s * interval '1 day')
                   GROUP BY fail_class""", (days,)) or []
    out = {r["fail_class"]: r["n"] for r in rows}
    return {"window_days": days, "counts": out, "total": sum(out.values()),
            "limits": {"recipient": config.RETRY_MAX_RECIPIENT,
                       "transient": config.RETRY_MAX_TRANSIENT,
                       "system": None}}
