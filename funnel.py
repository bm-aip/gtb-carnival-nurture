"""What actually happened to the messages we sent. One place, eight words.

WHY THIS EXISTS
---------------
2026-09-05. Asked a simple question -- "is the nurture working" -- three counters
in this codebase answered 2.3%, 40% and 66% for the same lane in one afternoon.
All three were arithmetically correct. They were counting different things, and
nothing said which.

Worse, every one of them called Wati's "accepted" a delivery. It is not. Wati
accepting a message means Wati will try. Roughly one in ten is rejected by Meta
afterwards, and for template sends we had never once confirmed a delivery: Wati
returns no message id for a template, so `message_log.provider_msg_id` is NULL
for all of them and the id-join in the delivery callbacks matched nothing. 2,223
delivery confirmations and 2,124 read receipts sat in `message_delivery`
unmatched to anything.

They match on PHONE, which is what Phase 0 keys everything on anyway. Doing that
gave the first true reading this project has ever had: of 699 accepted template
sends, 499 reached a buyer and 335 were read.

THE EIGHT STATES, owner's vocabulary, 2026-09-05
------------------------------------------------
  HELD       we stopped it. It never reached Wati. Our decision, our bug to fix.
  SENT       we called Wati.
  ACCEPTED   Wati took it and queued it. PROVISIONAL -- not a delivery.
  REJECTED   refused. Either Wati said no at once, or Meta said no afterwards.
  DELIVERED  it reached the buyer's phone.
  READ       the buyer opened it. A FLOOR, never a ceiling -- anyone with read
             receipts switched off never generates one.
  REPLIED    they wrote back. Belongs to the PERSON, not to a message: people
             reply days later, to the conversation, not to your last template.
  UNKNOWN    accepted, and no callback ever came. Neither success nor failure.

UNKNOWN IS THE POINT. It is 19% of sends, and every report before this one
silently rounded it into whichever of the other two the author expected. Counting
it as failure said 45% delivery; as success, 90%. Both were "true". It gets its
own column here and it is never, ever folded.

ATTEMPT IS NOT MESSAGE. One lead took 1,183 attempts in twenty hours during the
2026-08-24 runaway. Counted by attempt the lane looks catastrophic; counted by
person it was 38 people and a normal retry ladder. Both counts are here, both
labelled, because they answer different questions -- "is the machine healthy"
against "did we reach the buyer".
"""
import config
import db

# WHY A TIME WINDOW AND NOT A JOIN KEY. Wati returns no message id for a template
# send, so there is nothing to join on but the phone number. Two sends to one
# person inside the same window would each claim the same callback, which is why
# these are deliberately tight -- a delivery lands in seconds, and no lane in this
# system sends the same person twice within half an hour.
DELIVERED_WINDOW = "30 minutes"

# Reads are slower and human: people open WhatsApp when they open it. Six hours
# is a compromise -- long enough for an evening send read at bedtime, short enough
# that tomorrow's send does not steal today's read receipt.
READ_WINDOW = "6 hours"

# Rows that are filed outbound but are bookkeeping, not messages. Kept in step
# with wati.NOT_A_SEND: the 2026-08-22 and 2026-09-04 incidents were both a
# bookkeeping row counted as a send, and the second happened because the first
# fix named one word instead of making a list.
NOT_A_MESSAGE = ("matched", "knock_gave_up", "knock_skipped", "meta_refused")

# Escaped `\_` because underscore is a single-character wildcard in LIKE, and
# `knock_t%` would also match a hypothetical `knockXt`. `%%` throughout: these
# strings are assembled with .format(), never with %-formatting, so a doubled
# percent means exactly one thing -- a literal % for psycopg2. Mixing the two
# escapes is what broke this file's first run and the verification sheet before it.
TEMPLATE_LANES = r"(m.msg_type LIKE 'knock\_t%%' OR m.msg_type LIKE 'reopener\_%%')"

_STATES = """
    count(*)                                                       AS sent,
    count(*) FILTER (WHERE m.ok)                                   AS accepted,
    count(*) FILTER (WHERE NOT m.ok)                               AS rejected,
    count(*) FILTER (WHERE m.ok AND d.delivered)                   AS delivered,
    count(*) FILTER (WHERE m.ok AND d.was_read)                    AS read_,
    count(*) FILTER (WHERE m.ok AND d.failed)                      AS failed_late,
    count(*) FILTER (WHERE m.ok AND NOT d.delivered AND NOT d.failed)
                                                                   AS unknown,
    count(DISTINCT m.lead_id)                                      AS people,
    count(DISTINCT m.lead_id) FILTER (WHERE d.delivered)           AS people_reached
"""

# The callback lookup, written once. Correlated on phone and a time window
# because there is no id to join on -- see DELIVERED_WINDOW.
_CALLBACKS = """
    LEFT JOIN LATERAL (
        SELECT
          EXISTS (SELECT 1 FROM message_delivery x
                   WHERE x.phone = l.phone AND x.status = 'delivered'
                     AND x.created_at BETWEEN m.ts
                                  AND m.ts + interval '{delivered}') AS delivered,
          EXISTS (SELECT 1 FROM message_delivery x
                   WHERE x.phone = l.phone AND x.status = 'read'
                     AND x.created_at BETWEEN m.ts
                                  AND m.ts + interval '{read}')      AS was_read,
          EXISTS (SELECT 1 FROM message_delivery x
                   WHERE x.phone = l.phone AND x.status = 'failed'
                     AND x.created_at BETWEEN m.ts
                                  AND m.ts + interval '{delivered}') AS failed
    ) d ON TRUE
""".format(delivered=DELIVERED_WINDOW, read=READ_WINDOW)

_FROM = """
      FROM message_log m
      JOIN leads l ON l.id = m.lead_id
      {callbacks}
     WHERE m.direction = 'out'
       AND m.ts > now() - (%s || ' days')::interval
       AND NOT (m.msg_type = ANY(%s))
       AND COALESCE(m.detail, '') NOT LIKE 'blocked:%%'
       AND {lanes}
""".format(callbacks=_CALLBACKS, lanes=TEMPLATE_LANES)


def _rate(part, whole):
    """A percentage, or None when the denominator is zero.

    None rather than 0.0 on purpose: "no data" and "nothing got through" look
    identical as a number and mean opposite things. A dash on a dashboard is
    honest; a zero invents a failure that was never measured.
    """
    return round(100.0 * part / whole, 1) if whole else None


# What the outcome words actually mean, spelled out on the page. `escalated`
# reads like a success and is the opposite: the bot could not answer and called a
# human. On 2026-09-05 twelve of seventeen outcomes were escalations and nobody
# had noticed, because the word flatters itself.
OUTCOME_MEANINGS = {
    "qualified": "GOAL. Cleared every gate.",
    "visit_booked": "GOAL. Agreed to come and see it.",
    "wants_sales": "Asked for a human, without a budget on record.",
    "escalated": "NOT a success. The bot got stuck and called a human.",
    "nurture": "Parked to be nudged again later.",
    "dead": "Not a buyer.",
}


def _decorate(r):
    r = dict(r)
    r["delivery_rate"] = _rate(r["delivered"], r["accepted"])
    r["read_rate"] = _rate(r["read_"], r["delivered"])
    r["unknown_rate"] = _rate(r["unknown"], r["accepted"])
    r["reach_rate"] = _rate(r["people_reached"], r["people"])

    # THE ONLY COLUMN THAT JUDGES THE TEMPLATE RATHER THAN ONE STEP OF IT.
    # Acceptance and delivery pull in opposite directions -- 2026-09-05,
    # t1_lifestyle was accepted 85% of the time and delivered only 57% of those,
    # while t6_visit was accepted 17% and delivered 91%. Reading either number
    # alone ranks them backwards. Delivered over SENT is the honest one.
    r["end_to_end"] = _rate(r["delivered"], r["sent"])
    return r


def by_template(days=7):
    """One row per template: every state, per message and per person."""
    rows = db.q("""
        SELECT COALESCE(m.template_name, m.msg_type) AS template, {states} {frm}
         GROUP BY 1 ORDER BY sent DESC""".format(states=_STATES, frm=_FROM),
        (days, list(NOT_A_MESSAGE))) or []
    return [_decorate(r) for r in rows]


def by_lane(days=7):
    """Nurture against re-opener. The two lanes have different jobs."""
    rows = db.q("""
        SELECT CASE WHEN m.msg_type LIKE 'reopener\\_%%' THEN 'reopener'
                    ELSE 'nurture' END AS lane, {states} {frm}
         GROUP BY 1 ORDER BY 1""".format(states=_STATES, frm=_FROM),
        (days, list(NOT_A_MESSAGE))) or []
    return [_decorate(r) for r in rows]


def held(days=7):
    """What WE stopped, and why. Separate because the owner is different.

    A rejection is Meta's decision and nothing we build changes it. A hold is
    ours, and every one is a message a buyer did not get because of our own
    limit, gate or bug. Mixing the two is how 2026-09-04's outage was first
    reported as Meta throttling us.
    """
    return db.q("""
        SELECT split_part(m.detail, ':', 2) AS reason,
               count(*) AS blocked, count(DISTINCT m.lead_id) AS people
          FROM message_log m
         WHERE m.direction = 'out'
           AND m.ts > now() - (%s || ' days')::interval
           AND COALESCE(m.detail, '') LIKE 'blocked:%%'
         GROUP BY 1 ORDER BY 2 DESC""", (days,)) or []


def replies(days=7):
    """Did being reached make them write back?

    Anchored on the PERSON and on delivery, not on a message: someone answers the
    conversation, often days later and rarely to the last thing sent. Counting a
    reply against the template that "caused" it invents a link WhatsApp never
    gives us.
    """
    r = db.q("""
        WITH reached AS (
            SELECT DISTINCT m.lead_id, l.phone, min(m.ts) AS first_at
              FROM message_log m
              JOIN leads l ON l.id = m.lead_id
             WHERE m.direction = 'out' AND m.ok
               AND m.ts > now() - (%s || ' days')::interval
               AND {lanes}
               AND EXISTS (SELECT 1 FROM message_delivery x
                            WHERE x.phone = l.phone AND x.status = 'delivered'
                              AND x.created_at BETWEEN m.ts
                                  AND m.ts + interval '{delivered}')
             GROUP BY 1, 2)
        SELECT count(*) AS reached,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM message_log i
                    WHERE i.lead_id = reached.lead_id AND i.direction = 'in'
                      AND i.ts > reached.first_at)) AS replied
          FROM reached""".format(lanes=TEMPLATE_LANES, delivered=DELIVERED_WINDOW),
        (days,), one=True) or {}
    out = dict(r)
    out["reply_rate"] = _rate(out.get("replied") or 0, out.get("reached") or 0)
    return out


def outcomes(days=7):
    """Where conversations ended up.

    THE NUMBER THIS PROJECT EXISTS FOR, and the one nothing was reporting. On
    2026-09-05, fourteen days produced twelve `escalated`, one `wants_sales`, and
    ZERO `qualified` or `visit_booked`. `escalated` is the bot's failure exit --
    it means the bot could not answer and called a human -- so the honest reading
    was that the bot had qualified nobody at all. Every other number on this page
    is plumbing next to this one, which is why it is on the same page.
    """
    return db.q("""
        SELECT outcome, count(*) AS n
          FROM conversations
         WHERE outcome IS NOT NULL
           AND outcome_at > now() - (%s || ' days')::interval
         GROUP BY 1 ORDER BY 2 DESC""", (days,)) or []


def report(days=7):
    lanes = by_lane(days)

    # Summed from the lane rows rather than queried again, so the headline figures
    # and the table can never disagree -- two queries for one number is how this
    # codebase ended up with three different answers for the same lane.
    totals = {k: sum(x[k] or 0 for x in lanes)
              for k in ("sent", "accepted", "rejected", "delivered", "read_",
                        "failed_late", "unknown", "people", "people_reached")}

    return {
        "days": days,
        "windows": {"delivered": DELIVERED_WINDOW, "read": READ_WINDOW},
        "totals": totals,
        "outcome_meanings": OUTCOME_MEANINGS,
        "by_lane": lanes,
        "by_template": by_template(days),
        "held": held(days),
        "replies": replies(days),
        "outcomes": outcomes(days),
        "caveats": [
            "ACCEPTED is not DELIVERED. Wati accepting a message means it will "
            "try; about one in ten is refused by Meta afterwards.",
            "Delivered and Read are matched on phone within %s / %s, because Wati "
            "returns no message id for a template send. Two sends to one person "
            "inside that window could share a callback."
            % (DELIVERED_WINDOW, READ_WINDOW),
            "READ is a floor. Anyone with read receipts switched off never "
            "generates one, so the true figure is higher.",
            "UNKNOWN is accepted-and-never-heard-of-again. It is neither success "
            "nor failure and is never folded into either.",
            "A rate of null means nothing was measured, which is not the same as "
            "zero.",
        ],
    }
