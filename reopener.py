"""The ghost lane: a conversation that started, then went quiet.

THE GAP THIS FILLS. knocks.py excludes anyone who has ever replied -- task 18,
"ANY inbound ends the sequence, permanently" -- which is right, because a
scheduled template must never talk over a live human being. But it meant that the
moment somebody answered us, nothing would ever follow up again if they then went
silent. 283 people were in that state on 2026-08-24 and not one message was owed
to any of them.

The design was written down long ago (POST-CARNIVAL-DESIGN, the ghost lane):

    Chase them. One template re-open, then two more spaced tries. If they come
    back, the bot resumes exactly where it left off -- it does not restart the
    checklist. Then dormant.

And `t7_reopener_newac` was approved in Wati and wired to config.REOPENER_TEMPLATE.
It had been sent ZERO times. Every part existed except the code that sends it.

ONLY WHERE THERE IS CONTEXT. Owner, 2026-08-24: "more than timing - we should take
the conversation into context - and fire the reopener". The template says "we were
talking about {{2}}", so it is only honest -- and only useful -- when we can name
the thing. Of the 283 quiet conversations, 266 had told us NOTHING: their last
message was an ad prefill or a bare "Hi". Those were never conversations, and they
belong to the knock ladder. 17 had given a purpose or a configuration. Those are
the ghosts, and this is for them.

WHAT STOPS A RE-OPEN, in order:

  * no context               -- nothing to put in {{2}}; not our lane
  * a terminal outcome       -- they are with a human, or we know the answer
  * they came back           -- any inbound after the last re-open ends the lane
  * REOPEN_MAX tries         -- then dormant, and dormant is silent forever
  * sendgate.check()         -- master switch, pause, opt-out, retry ceiling

The send goes through sequencer._send like everything else. One door.
"""
import logging
import os
import re
from datetime import datetime, timezone

import config
import db
import knocks
import sequencer

log = logging.getLogger("reopener")

MSG_TYPE = "reopener_t7"

# Days of silence before the first re-open, then between tries. Three tries in
# total -- the design's "one re-open, then two more spaced tries".
REOPEN_AFTER_DAYS = [int(x) for x in os.environ.get(
    "REOPEN_AFTER_DAYS", "3,7,14").split(",") if x.strip()]
REOPEN_MAX = len(REOPEN_AFTER_DAYS)

# Past this, dormant. The design is explicit that the bot may never auto-restart a
# journey, so dormant is terminal for this lane.
DORMANT_DAYS = int(os.environ.get("REOPEN_DORMANT_DAYS", "31"))


def topic_for(conv):
    """What to put in "we were talking about {{2}}". None if we cannot name it.

    Returning None is a real answer and the whole gate: with nothing to reference
    the template would read "we were talking about your requirement", which is the
    kind of sentence that tells a buyer we were not listening.
    """
    checklist = (conv or {}).get("checklist") or {}

    def tidy(s):
        # The model writes these fields freely -- "3 bedroom (villa or apartment
        # not yet decided)" and "Compact 2BHK apartment 1250 sqft; also asking
        # about villas" are both real stored values. Inside "we were talking about
        # ___" they have to read like a phrase a person would say, so drop the
        # parenthetical aside and take the first clause.
        s = re.sub(r"\([^)]*\)", " ", str(s or ""))
        s = s.split(";")[0].split(",")[0].strip(" .-")
        return " ".join(s.split())

    # A PARKED VISIT COMES FIRST. It is the strongest thing anyone tells us, and
    # it is what they will remember saying.
    #
    # 2026-08-14, lead 1154: primary residence, 4 bed villa, Besant Nagar, budget
    # "No constraint if I like it", and "Need to see if its this weekend or next.
    # Mostly next." The bot said "no rush, just ping me when you know" and waited.
    # Next weekend came and went. Nine days later he came back on his own through
    # an ad. Nobody had followed up, because nothing was built to.
    if checklist.get("visit_day") or checklist.get("visit_venue") or \
            re.search(r"visit|weekend|week|month", str(checklist.get("timeline") or ""),
                      re.I):
        cfg = tidy(checklist.get("configuration"))
        return f"coming to see the {cfg}" if cfg else "coming to see the place"

    cfg = tidy(checklist.get("configuration"))
    if cfg:
        return cfg if cfg.lower().startswith("the ") else f"the {cfg}"

    purpose = (checklist.get("purpose") or "").strip().lower()
    if purpose:
        # The model writes `primary_residence`, not `primary_home` -- the first
        # version of this map guessed and silently returned None for every one of
        # them, which the dry run caught.
        return {"weekend_home": "a weekend place",
                "weekend": "a weekend place",
                "primary_residence": "a home for the family",
                "primary_home": "a home for the family",
                "investment": "this as an investment"}.get(purpose)
    return None


def due(limit=None):
    """Conversations owed a re-open right now. Quietest first."""
    limit = limit or config.SEND_BATCH_PER_TICK
    rows = db.q("""
        SELECT c.id AS conv_id, c.checklist, c.last_turn_at,
               l.id, l.phone, l.name, l.project, l.campaign,
               (SELECT count(*) FROM message_log r
                 WHERE r.lead_id = l.id AND r.direction='out'
                   AND r.msg_type = %s AND r.ok)              AS tries,
               (SELECT max(r.ts) FROM message_log r
                 WHERE r.lead_id = l.id AND r.direction='out'
                   AND r.msg_type = %s AND r.ok)              AS last_try
        FROM conversations c
        JOIN leads l ON l.id = c.lead_id
        WHERE (
                -- THE GHOST LANE: a conversation that simply stopped.
                c.outcome IS NULL
                -- THE PARKED VISIT: they said they would come and then went
                -- quiet. Owner 2026-08-24 chose to nudge these directly even
                -- when a salesperson already has them -- the date passing with
                -- nobody in touch is worse than two voices.
                -- `visit_booked` and `dead` stay out: one is done, the other is
                -- a person we have nothing to sell.
                OR (COALESCE(c.outcome,'') NOT IN ('dead','visit_booked')
                    AND (c.checklist ? 'visit_day' OR c.checklist ? 'visit_venue'
                         OR c.checklist->>'timeline' ~* '(visit|weekend|week|month)'))
              )
          AND l.last_inbound_at IS NOT NULL
          AND l.phone IS NOT NULL
          AND NOT l.suppressed
          AND l.knock_lost_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM optouts o WHERE o.phone = l.phone)
          -- THEY CAME BACK. Any inbound after our last re-open ends this lane:
          -- the qualifier owns them again and a scheduled template would talk
          -- over a live person, which is the rule task 18 exists to protect.
          AND l.last_inbound_at <= COALESCE(
                (SELECT max(r.ts) FROM message_log r
                  WHERE r.lead_id = l.id AND r.direction='out'
                    AND r.msg_type = %s AND r.ok),
                l.last_inbound_at)
          AND c.last_turn_at < now() - (%s || ' days')::interval
          AND c.last_turn_at > now() - (%s || ' days')::interval
        ORDER BY c.last_turn_at ASC
        LIMIT %s""",
        (MSG_TYPE, MSG_TYPE, MSG_TYPE,
         REOPEN_AFTER_DAYS[0], DORMANT_DAYS, limit * 5)) or []

    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        tries = r.get("tries") or 0
        if tries >= REOPEN_MAX:
            continue
        topic = topic_for(r)
        if not topic:
            continue                       # no context -> not our lane
        # Spacing: measured from the last try if there is one, otherwise from the
        # moment the conversation went quiet.
        anchor = r.get("last_try") or r.get("last_turn_at")
        if anchor is None:
            continue
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        wait_days = REOPEN_AFTER_DAYS[tries]
        if (now - anchor).days < wait_days:
            continue
        out.append((r, tries, topic))
        if len(out) >= limit:
            break
    return out


def send_one(lead_row, tries, topic):
    """One re-open. Returns True only if it actually went out."""
    template = config.REOPENER_TEMPLATE
    if not template:
        log.warning("no REOPENER_TEMPLATE configured")
        return False
    params = [knocks._first_name(lead_row.get("name")), topic]
    lead = dict(lead_row)
    lead["id"] = lead_row["id"]
    ok = sequencer._send(lead, MSG_TYPE, template=template, params=params,
                         body=f"re-open: we were talking about {topic}")
    if ok:
        log.info("lead %s re-opened (try %s) about %s",
                 lead_row["id"], tries + 1, topic)
    return ok


def run():
    """One scheduled pass. Returns how many re-opens went out."""
    batch = due()
    if not batch:
        return 0
    sent = 0
    for lead_row, tries, topic in batch:
        try:
            if send_one(lead_row, tries, topic):
                sent += 1
        except Exception as e:                  # one bad lead must not stop the pass
            log.exception("re-open failed for lead %s: %s", lead_row.get("id"), e)
    if sent:
        db.set_setting("last_reopener_run", f"{sent} sent")
    return sent
