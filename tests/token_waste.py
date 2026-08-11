"""Not spending a model call twice on one thought. No database, no API, ~1 second.

    python tests/token_waste.py

Measured 2026-08-11 across 323 model calls: 47% fired within 90 seconds of the
previous one for the same person, and lead 1016 alone burned 31 calls -- a tenth of
all spend -- while capturing nothing.

THE RISK IN THIS CHANGE IS FALSE POSITIVES, not misses. A missed saving costs a few
paise. Misreading a real answer as noise drops a booking or a gate answer on the
floor, so most of what follows is proving that words which could be ANSWERS still
reach the model.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bootstrap import Results        # noqa: E402

import answering                       # noqa: E402
import config                          # noqa: E402

r = Results()
ack = answering.is_bare_acknowledgement

# --- what SHOULD short-circuit ------------------------------------------------
for t in ("Ok", "ok", "OK.", "okay", "k", "kk", "Hm", "hmm", "By", "bye", "Bye!",
          "Thanks", "thank you", "thx", "ty", "Noted", "got it", "Alright",
          "Nice", "great", "Cool", "super", "  ok  ", "ok!!", "👍", "🙏", "😍",
          "...", "👍👍"):
    r.check(f"acknowledgement: {t!r}", ack(t), detail="should short-circuit")

# --- what MUST NOT short-circuit ---------------------------------------------
# Every one of these can be a real answer to a question the bot just asked.
for t in ("yes", "no", "Yes", "ya", "yeah", "yup", "nope", "sure", "please",
          "done", "confirm", "Sunday", "sunday 11am", "3", "2 bhk", "4 cr",
          "villa", "investment", "weekend", "primary home", "ECR", "Chennai",
          "call me", "Call me fast", "ok so what about maintenance charges",
          "ok but is the price negotiable", "okay send me the brochure",
          "how far?", "why?", "price?", "k what about parking"):
    r.check(f"must reach the model: {t!r}", not ack(t),
            detail="wrongly classified as an acknowledgement")

r.check("empty text is not an acknowledgement", not ack(""))
r.check("None is not an acknowledgement", not ack(None))
r.check("a long message starting with ok reaches the model",
        not ack("ok so i wanted to ask about the clubhouse timings please"))

# --- the gate on WHEN it applies ---------------------------------------------
# Before handoff, "ok" is often an answer. Only once a human owns the conversation
# is there no outstanding question a one-word reply could be answering.
r.check("qualified is handed off", "qualified" in config.HANDED_OFF_OUTCOMES)
r.check("visit_booked is handed off", "visit_booked" in config.HANDED_OFF_OUTCOMES)
r.check("escalated is handed off", "escalated" in config.HANDED_OFF_OUTCOMES)
r.check("wants_sales is handed off", "wants_sales" in config.HANDED_OFF_OUTCOMES)
# nurture buyers are still being actively worked -- owner 2026-08-03, probe for room,
# never kill -- so their "ok" still deserves a real turn.
r.check("nurture is NOT handed off", "nurture" not in config.HANDED_OFF_OUTCOMES)
r.check("no outcome is not handed off", None not in config.HANDED_OFF_OUTCOMES)

# --- the fixed reply ----------------------------------------------------------
r.check("there is a fixed ack reply", bool(config.ACK_REPLY.strip()))
r.check("the ack reply promises no specific time",
        not any(d in config.ACK_REPLY.lower() for d in
                ("monday", "tuesday", "wednesday", "thursday", "friday",
                 "saturday", "sunday", "today", "tomorrow")),
        detail=config.ACK_REPLY)
r.check("the ack reply writes Rs, never the rupee sign",
        "₹" not in config.ACK_REPLY, detail=config.ACK_REPLY)

# --- coalescing is wired, and never drops a fragment -------------------------
import jobs                             # noqa: E402
r.check("enqueue_inbound exists", hasattr(jobs, "enqueue_inbound"))
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "jobs.py"), encoding="utf-8").read()
r.check("merging appends rather than replaces the pending text",
        "COALESCE(payload->>'text', '')" in src and "|| E'\\\\n' ||" in src,
        detail="a merge that overwrote the pending text would lose the earlier "
               "fragment, which is the opposite of the intent")
r.check("only a queued job is merged into, never a running one",
        "j.status = %s" in src and "QUEUED" in src)
r.check("merge count is recorded for observability", "'{merged}'" in src)

sys.exit(0 if r.report("TOKEN WASTE RULES") else 1)
