"""The hand-raiser lane: they tapped a button, then went quiet, and no lane owns them.

WHY THIS EXISTS. On 2026-09-06 the daily report said "373 conversations stalled
3d+" for the fourth week running. Bucketing all 373 against the real predicates
found four different populations, and only one of them was a defect:

     9  opted out or suppressed        -- correctly silent
   118  knock_lost_at set             -- ten refusals each; WhatsApp will not
                                          deliver to them at all. See /admin/unreachable
   144  prefill only, empty checklist  -- the knock ladder owns them and is working;
                                          49 were already three knocks deep
    27  a nameable topic              -- the re-opener owns them, inside its spacing
    74  typed a real word, no topic    -- NOBODY OWNS THEM

Of those 74, **fourteen had pressed a button on one of our own templates** --
thirteen "Need More Details" and one "Plan a Site Visit". Every one of them got an
answer from the bot, said nothing more, and was then never contacted again by
anything.

THE CAUSE IS TWO RULES THAT ARE EACH CORRECT ON THEIR OWN.

  * knocks.py, task 18: any inbound ends the knock sequence permanently. PR #62
    extended it so that a pressed button counts as a real inbound -- because
    WhatsApp returns a button label as an ordinary message, and without that a
    person tapping "Need More Details" would be answered with another marketing
    blast. That is the strongest buy signal we get, and blasting it is the worst
    possible reply.
  * reopener.py: a re-open must name what we were talking about, or the template
    reads "we were talking about your requirement" and tells a buyer we were not
    listening. Someone who only ever pressed a button has told us nothing to name.

So the rule that protects a hand-raiser from being blasted is the same rule that
removes them from every follow-up lane. Owner's call, 2026-09-06: a salesperson
gets them.

WHAT THIS LANE DOES NOT DO. It never messages the buyer. The card goes to staff,
on a number where 18% of buyer sends are already bouncing, and adding marketing
load to it makes that worse -- so the answer to "this person raised their hand"
is a human, not another template.

OWNERSHIP IS EXCLUSIVE BY CONSTRUCTION. Requiring reopener.topic_for() to return
None means this lane can only ever pick up somebody the re-opener has already
declined. The two cannot chase the same person.
"""
import logging
import os
from datetime import datetime, timezone

import config
import db
import handoff
import picker
import reopener
import sequencer

log = logging.getLogger("handraiser")

MSG_TYPE = "handoff_handraiser"

# How long a hand stays up before it needs a human. Three days matches the
# re-opener's first rung: long enough that somebody mid-conversation is not
# reported as gone quiet, short enough that a live buying signal is still warm.
QUIET_DAYS = int(os.environ.get("HANDRAISER_QUIET_DAYS", "3"))

# Cards per pass. Deliberately small: this is a person's morning, not a queue to
# drain. The backlog is 14 and shrinks by this much a minute if it ever needs to.
BATCH = int(os.environ.get("HANDRAISER_BATCH", "5"))


def _fetch(page, offset):
    """One page of candidates, longest-quiet first.

    THE SQL DOES WHAT SQL IS GOOD AT and nothing more. The button test and the
    topic test are both done in Python by `_pick`, against the same helpers the
    other lanes use, because a second copy of either rule in SQL is a copy that
    drifts. What is filtered here is only what would otherwise drag whole tables
    into memory.

    `handoff_%` covers both the template card and its free-text fallback: if
    either reached a salesperson, this person has been reported and must not be
    reported again. Keyed on PHONE, not lead id -- one human is routinely several
    lead rows and a card is about the person.
    """
    return db.q("""
        SELECT c.id AS conv_id, c.checklist, c.last_turn_at,
               l.id, l.phone, l.name, l.project, l.campaign, l.last_inbound_at,
               (SELECT im.body FROM message_log im JOIN leads l2 ON l2.id = im.lead_id
                 WHERE l2.phone = l.phone
                   AND im.direction = 'in' AND im.msg_type = 'inbound'
                 ORDER BY im.ts DESC LIMIT 1)                       AS last_in
        FROM conversations c
        JOIN leads l ON l.id = c.lead_id
        WHERE c.outcome IS NULL
          AND l.phone IS NOT NULL
          AND NOT l.suppressed
          -- Ten refusals and we stopped. A number WhatsApp will not deliver to is
          -- not a lead a salesperson should be handed as a chat; it belongs on
          -- the call list at /admin/unreachable.
          AND l.knock_lost_at IS NULL
          AND l.last_inbound_at IS NOT NULL
          AND l.last_inbound_at < now() - (%s || ' days')::interval
          AND NOT EXISTS (SELECT 1 FROM optouts o WHERE o.phone = l.phone)
          -- ONCE PER PERSON, EVER. A card that repeats is a card people stop
          -- reading -- the same reasoning as the escalation guard in handoff.py.
          AND NOT EXISTS (
                  SELECT 1 FROM message_log h JOIN leads l3 ON l3.id = h.lead_id
                   WHERE l3.phone = l.phone AND h.direction = 'out'
                     AND h.msg_type LIKE %s)
        ORDER BY l.last_inbound_at ASC
        LIMIT %s OFFSET %s
    """, (QUIET_DAYS, MSG_TYPE + "%", page, offset))


def _is_button(body):
    """Did they press one of our own template buttons?

    WhatsApp returns a pressed button's label as an ordinary inbound message, so
    this is a string comparison and there is nothing else to go on. The list is
    config.TEMPLATE_BUTTON_LABELS -- the same list knocks.py tests against, for
    the reason PR #62 recorded: a pattern edit in one place must not be able to
    reclassify a hand-raiser somewhere else.
    """
    return str(body or "").strip().lower() in set(config.TEMPLATE_BUTTON_LABELS)


def _pick(row):
    """This lane's own rules. Side-effect free -- a monitor may call due()."""
    if not _is_button(row.get("last_in")):
        return None
    # THE RE-OPENER GETS FIRST REFUSAL. If it can name what we were talking about
    # then it owns this person and will nudge them itself; two lanes chasing one
    # buyer is worse than neither.
    if reopener.topic_for(row):
        return None
    return row


def due(limit=None):
    """Hand-raisers owed a card right now. Longest-quiet first.

    Pages through candidates with the shared walker rather than a fixed window.
    A fixed `limit * 5` is what starved the re-opener for 90 hours: the oldest
    rows were all rejects and the live buyer sat on page three. See #81.
    """
    return picker.scan(_fetch, _pick, limit or BATCH)


def _card(row):
    """What the salesperson reads. Five slots, same template as every other card."""
    tapped = " ".join(str(row.get("last_in") or "").split())[:60]
    when = str(row.get("last_inbound_at"))[:16]
    quiet_days = ""
    last = row.get("last_inbound_at")
    if isinstance(last, datetime):
        now = datetime.now(last.tzinfo or timezone.utc)
        quiet_days = " (%d days ago)" % max(0, (now - last).days)
    return [
        handoff._slot("Tapped a button then went quiet -- %s"
                      % (row.get("project") or "RON")),
        handoff._slot(row.get("name"), "name not given"),
        handoff._slot(row.get("phone")),
        handoff._slot('They tapped "%s" on %s%s' % (tapped, when, quiet_days)),
        # WHAT THE BOT KNOWS, WHICH IS NOTHING. Saying so plainly is the point of
        # the card: this person raised their hand and never told us what they
        # want, so there is nothing to prepare and the call is the discovery.
        handoff._slot("They have answered no questions, so we know nothing about "
                      "what they want. The whole conversation is in the Wati Team "
                      "Inbox."),
    ]


def run():
    """One scheduled pass. Returns how many cards went out."""
    # THE CLOCK, FOR THE SAME REASON THE OTHER TWO LANES HAVE IT. These cards are
    # a batch sweep, not a live escalation: fourteen of them arriving on a
    # salesperson's phone at 3am is worse service than the same fourteen at 8am.
    # An escalation raised by a buyer mid-conversation still goes straight out --
    # that path does not come through here.
    if sequencer.quiet_now():
        log.info("handraiser pass skipped: quiet hours until %02d:%02d IST",
                 *sequencer.QUIET_END)
        return 0

    sent = 0
    for row in due():
        # No daily-cap check: a card to our own team is not a buyer-facing
        # message and must never consume the messaging tier. sequencer._daily_sends()
        # counts `knock%` and the cold first touches, and `handoff_%` is neither.
        if handoff._notify(config.STAFF_PHONES, _card(row), "handraiser",
                           lead_id=row["id"]):
            sent += 1
            log.info("lead %s handed to a human: tapped %r and went quiet",
                     row["id"], str(row.get("last_in"))[:40])
    return sent
