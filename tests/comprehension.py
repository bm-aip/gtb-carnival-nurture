"""Answering what the buyer actually asked. No database, no API, ~1 second.

    python tests/comprehension.py

Two failures found by reading real replies on 2026-08-17. Both are comprehension,
not tone -- the buyer asked something plainly and the answer did not address it.

  CALL ME. Six buyers asked to be phoned. FOUR replies never mentioned a call or a
  person; one answered "Call me" with apartment prices and a site visit. Three of
  those conversations were `escalated`, so a colleague HAD been told while the buyer
  read about the clubhouse. From the buyer's side that is being ignored.

  LOCATION. A buyer sent "Location". The reply opened "Ha, thanks." and described the
  beach and the clubhouse without ever saying where the project is. The collision is
  structural: our own gate is CALLED location, so a bare "location" reads as the
  buyer ANSWERING it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bootstrap import Results        # noqa: E402

import config                          # noqa: E402

r = Results()

# --- they asked to be phoned --------------------------------------------------
for msg in ("Call me", "call me", "Can you call me", "Could you call me please",
            "Please call", "call back", "Give me a call", "Ring me",
            "Only 1 minutes call me video", "Can you share his contact number",
            "I want to talk to someone", "talk to sales", "phone me",
            "call kar do", "I need to speak"):
    r.check(f"detected as a call request: {msg!r}",
            bool(config.WANTS_CALL.search(msg)), detail=msg)

# Must NOT fire on ordinary talk about calls, or the acknowledgement gets prepended
# to messages where nobody asked for anything.
for msg in ("What is the call charge", "I called yesterday and no one picked up",
            "Recalling my earlier question", "so called luxury",
            "Do you have a clubhouse", "3 BHK villa please"):
    r.check(f"NOT a call request: {msg!r}",
            not config.WANTS_CALL.search(msg), detail=msg)

r.check("there is an approved acknowledgement", bool(config.CALL_ACK_FRAMING.strip()))
r.check("it promises a call", "call" in config.CALL_ACK_FRAMING.lower(),
        detail=config.CALL_ACK_FRAMING)
r.check("it commits to no time",
        not any(w in config.CALL_ACK_FRAMING.lower() for w in
                ("today", "tomorrow", "hour", "minute", "morning", "shortly",
                 "immediately", "right away")),
        detail=f"nothing here knows the team's diary: {config.CALL_ACK_FRAMING}")

# --- they asked where it is ---------------------------------------------------
for msg in ("Location", "location", "  Location  ", "Address", "Where", "where?",
            "Where is it", "Where is this", "Where is the site", "How far",
            "Which area", "exact location", "site location"):
    r.check(f"detected as a location question: {msg!r}",
            bool(config.ASKS_LOCATION.match(msg.strip())), detail=msg)

# A buyer ANSWERING the location gate names a place. That must not be mistaken for a
# question, or we would tell somebody in Adyar where the project is instead of
# recording that they are in Adyar.
for msg in ("Adyar", "ECR", "Chennai", "I am in Velachery",
            "Looking around OMR", "Where can I see the floor plan",
            "Where are the amenities listed"):
    r.check(f"NOT a bare location question: {msg!r}",
            not config.ASKS_LOCATION.match(msg.strip()), detail=msg)

# The answer has to come from approved config text, not a retrieved claim, or the
# citation floor would reject a reply that states it.
site = config.VISIT_VENUES["site"]["name"]
r.check("the site name is available to state", bool(site))
r.check("and it names ECR and Kovalam", "ECR" in site and "Kovalam" in site,
        detail=site)
r.check("and never says Vadanemmeli to a buyer", "Vadanemmeli" not in site,
        detail=site)

# --- the guards actually fire -------------------------------------------------
# Detecting the request proves nothing on its own; the reply has to change. These
# call _enforce the way the worker does.
import qualifier as q                   # noqa: E402

LEAD = {"id": 1, "phone": "919876543210", "project": "RON", "name": "Ravi"}


def decision(reply, **kw):
    d = {"reply": reply, "action": "answer", "sources": [], "purpose": None,
         "location": None, "configuration": None, "budget_inr": None,
         "timeline": None, "visit_day": None, "visit_time": None,
         "visit_venue": None, "flags": [], "internal_note": "",
         "gate_asked": None, "framing_used": None}
    d.update(kw)
    return d


# A reply that ignores the call request gets the acknowledgement put FIRST.
def enforced(reply, message, **kw):
    """The full path the worker takes: _enforce, then _answer_the_question."""
    return q._answer_the_question(
        q._enforce(decision(reply, **kw), [], LEAD, message=message), message)


out = enforced("Whenever you want to come by, just tell me a day.", "Call me")
r.check("the acknowledgement is prepended",
        out["reply"].startswith(config.CALL_ACK_FRAMING.split(",")[0]),
        detail=out["reply"])
r.check("and it is recorded in the note", "call request" in out["internal_note"],
        detail=out["internal_note"])

# A reply that ALREADY acknowledges is left alone -- no doubling up.
already = "Sure, I'll have a colleague call you."
out = enforced(already, "Call me")
r.eq("an acknowledged reply is untouched", out["reply"], already)

# Nobody asked for a call, so nothing is prepended.
plain = "Happy to help with that."
out = enforced(plain, "Any other amenities")
r.eq("no call request means no prepend", out["reply"], plain)

# "Location" gets told where it is.
out = enforced("Ha, thanks. It is a big community.", "Location")
r.check("the location is prepended", "ECR" in out["reply"], detail=out["reply"])
r.check("and Kovalam Junction is named", "Kovalam" in out["reply"],
        detail=out["reply"])
r.check("and never Vadanemmeli", "Vadanemmeli" not in out["reply"],
        detail=out["reply"])

# A reply that already gives the location is left alone.
told = "We're on ECR, near Kovalam Junction."
out = enforced(told, "Where is it")
r.eq("an answered location question is untouched", out["reply"], told)

# A bare location question must NOT be recorded as their answer to the gate --
# that is the collision that caused the failure in the first place.
out = enforced("It is a big community.", "Location", location="ECR",
               gate_asked="location", framing_used=0)
r.check("their location is not captured from a question",
        out.get("location") is None, detail=repr(out.get("location")))
r.check("and the gate is not marked as asked",
        out.get("gate_asked") is None, detail=repr(out.get("gate_asked")))

sys.exit(0 if r.report("COMPREHENSION RULES") else 1)
