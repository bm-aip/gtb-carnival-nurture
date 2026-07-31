"""Fatigue cap (Phase 0, task 3) -- how much is too much for one person.

TWO CEILINGS, ONE OF WHICH CANNOT BE RESET
------------------------------------------
Owner decision 2026-07-30: a new reason resets the knock counter. That recovers
real buyers who resurface, but "a new reason" is a loose idea and easy to define
generously, so on its own it is a way to message somebody forever.

So there are two:

  1. JOURNEY CEILING -- KNOCK_MAX_PER_JOURNEY (4). Resettable. This is the day
     0/3/10/25 sequence. A reset moves the journey's start marker; it never
     deletes history.

  2. WINDOW CEILING -- FATIGUE_MAX_PER_WINDOW (2) per FATIGUE_WINDOW_DAYS (7).
     NOT resettable by anything, because there is nothing to reset: it is a
     count of what we actually sent in the last seven days. This is what makes a
     generous reset safe -- reset as often as you like, nobody gets a burst.

Ceiling 2 also happens to be the RON nurture plan's own guardrail ("never more
than two nurture messages in the same week"), so it is not new policy.

WHY NOTHING IS STORED AS A NUMBER
---------------------------------
Both counts are computed from `message_log`. A stored counter can be zeroed by a
bug, a bad migration or a well-meaning manual edit, and the failure is silent --
the system carries on believing it has messaged someone twice when it has messaged
them nine times. Counting from the send record means the only mutable fact in the
system is where a journey starts, and every move of that is written to
`journey_resets` with a reason and the count it superseded.

WHAT COUNTS
-----------
Proactive knocks only. A reply inside a conversation the customer opened is not
fatigue -- they are actively talking to us, it costs no messaging tier, and
throttling it would mean going silent mid-conversation, which is worse than the
problem this module exists to prevent.
"""
import config
import db

CAP_JOURNEY = "fatigue_journey"
CAP_WINDOW = "fatigue_window"

# Proactive sends carry a msg_type prefixed 'knock' (see sequencer._send callers,
# task 17). Everything else -- session replies, acks, admin test sends -- is either
# inside an open window or one-off, and is not fatigue.
PROACTIVE_PREFIX = "knock"


def is_proactive(msg_type):
    return bool(msg_type) and str(msg_type).startswith(PROACTIVE_PREFIX)


def window_count(phone, days=None):
    """Proactive sends to this person in the rolling window, across ALL projects.

    Cross-project on purpose: the ceiling protects a human being, and being
    messaged twice by Republic of Nature and twice by another project in one week
    is four messages to one person.
    """
    days = days if days is not None else config.FATIGUE_WINDOW_DAYS
    r = db.q("""SELECT count(*) AS n
                FROM message_log ml JOIN leads l ON l.id = ml.lead_id
                WHERE l.phone = %s AND ml.direction = 'out' AND ml.ok
                  AND ml.msg_type LIKE %s
                  AND ml.ts > now() - (%s * interval '1 day')""",
             (phone, PROACTIVE_PREFIX + "%", days), one=True)
    return (r or {}).get("n", 0) or 0


def journey_started_at(phone, project):
    r = db.q("""SELECT started_at FROM knock_journeys
                WHERE phone=%s AND project=%s""", (phone, project), one=True)
    return (r or {}).get("started_at")


def journey_count(phone, project):
    """Knocks sent in the CURRENT journey.

    No journey row means no journey has started, which is zero knocks -- not an
    error. The knock engine (task 17) opens the row when it sends the first knock.
    """
    started = journey_started_at(phone, project)
    if not started:
        return 0
    r = db.q("""SELECT count(*) AS n
                FROM message_log ml JOIN leads l ON l.id = ml.lead_id
                WHERE l.phone = %s AND l.project = %s
                  AND ml.direction = 'out' AND ml.ok
                  AND ml.msg_type LIKE %s AND ml.ts >= %s""",
             (phone, project, PROACTIVE_PREFIX + "%", started), one=True)
    return (r or {}).get("n", 0) or 0


def lifetime_count(phone):
    """Every proactive message this person has ever received, all projects.

    Recorded and surfaced but NOT enforced: the owner chose a resettable counter
    over a hard lifetime ceiling. This exists so that a person who has quietly
    accumulated twenty messages across four resets is visible rather than
    invisible -- a reporting number, not a block.
    """
    r = db.q("""SELECT count(*) AS n
                FROM message_log ml JOIN leads l ON l.id = ml.lead_id
                WHERE l.phone = %s AND ml.direction='out' AND ml.ok
                  AND ml.msg_type LIKE %s""",
             (phone, PROACTIVE_PREFIX + "%"), one=True)
    return (r or {}).get("n", 0) or 0


def check(phone, msg_type, project=None):
    """(allowed, reason) for fatigue. Non-proactive sends always pass.

    Window ceiling is evaluated before the journey ceiling: it is the one that
    cannot be reset, so a caller reading the reason gets the more fundamental
    answer rather than one a reset would clear.
    """
    if not phone or not is_proactive(msg_type):
        return True, None

    if window_count(phone) >= config.FATIGUE_MAX_PER_WINDOW:
        return False, CAP_WINDOW

    if project and journey_count(phone, project) >= config.KNOCK_MAX_PER_JOURNEY:
        return False, CAP_JOURNEY

    return True, None


def start_journey(phone, project):
    """Open a journey if one is not already open. Idempotent.

    Called by the knock engine before its first knock (task 17). Deliberately not
    called from the gate: the gate decides whether a message may go, and a check
    that quietly creates state is a check that cannot be run twice safely.
    """
    return db.x("""INSERT INTO knock_journeys (phone, project)
                   VALUES (%s,%s) ON CONFLICT (phone, project) DO NOTHING""",
                (phone, project)) == 1


VALID_REASONS = ("form_fill", "ctwa_click", "human", "import")


def reset_journey(phone, project, reason, note=None):
    """Move the journey start to now, so the 4-knock sequence may run again.

    `reason` must be one of VALID_REASONS -- a free-text reason would make the
    audit trail unqueryable, and "a new reason" then means whatever the caller
    felt like. The bot may never call this with reason='human'.

    The weekly ceiling is untouched by this and remains in force, so a reset can
    never produce a burst. Returns (ok, knocks_before).
    """
    if reason not in VALID_REASONS:
        return False, None
    before = journey_count(phone, project)
    db.x("""INSERT INTO knock_journeys (phone, project, started_at, reset_count,
                                        last_reset_at, last_reason)
            VALUES (%s,%s,now(),0,now(),%s)
            ON CONFLICT (phone, project) DO UPDATE
              SET started_at = now(),
                  reset_count = knock_journeys.reset_count + 1,
                  last_reset_at = now(),
                  last_reason = EXCLUDED.last_reason,
                  dormant_at = NULL""",
         (phone, project, reason))
    db.x("""INSERT INTO journey_resets (phone, project, reason, note, knocks_before)
            VALUES (%s,%s,%s,%s,%s)""", (phone, project, reason, note, before))
    return True, before


def snapshot(phone, project=None):
    """Everything the fatigue rules know about one person, for the dashboard."""
    row = db.q("""SELECT project, started_at, reset_count, last_reset_at,
                         last_reason, dormant_at
                  FROM knock_journeys WHERE phone=%s""", (phone,)) or []
    return {
        "phone": phone,
        "window_days": config.FATIGUE_WINDOW_DAYS,
        "window_used": window_count(phone),
        "window_max": config.FATIGUE_MAX_PER_WINDOW,
        "journey_max": config.KNOCK_MAX_PER_JOURNEY,
        "journey_used": journey_count(phone, project) if project else None,
        "lifetime": lifetime_count(phone),
        "journeys": row,
    }
