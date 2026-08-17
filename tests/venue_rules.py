"""Which visit we ask for, and who we ask. No database, no API, ~1 second.

    python tests/venue_rules.py

Owner, 2026-08-11: "stop asking for visit to experience center - we want ppl to visit
the site if they are in chennai - if they are outside chennai - like this NRI campaign
- we have to push them for a virtual walk thru".

Lead 1016 is why this exists: a +966 number from the NRI ad asked to be phoned five
different ways -- "Call me", "Fast", "Only 2 minutes", "Confirm karo call" -- and was
answered with "just tell me a day and I'll set up the visit" every single time.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bootstrap import Results        # noqa: E402

import config                          # noqa: E402
import handoff                         # noqa: E402
import qualifier                       # noqa: E402

r = Results()

# --- who counts as overseas ---------------------------------------------------
r.eq("the NRI ad marks a lead overseas whatever the number",
     config.is_overseas({"ctwa_source_id": "52553896609352", "phone": "919876543210"}),
     True)
r.eq("a +966 number is overseas",
     config.is_overseas({"phone": "966510895539"}), True)
r.eq("a +974 number is overseas",
     config.is_overseas({"phone": "97477055094"}), True)
r.eq("a +1 number is overseas",
     config.is_overseas({"phone": "17326198865"}), True)
r.eq("an Indian number from a normal ad is not overseas",
     config.is_overseas({"phone": "919884739289",
                         "ctwa_source_id": "52552577525152"}), False)
# A 10-digit number has lost its country code somewhere. Reading it as overseas would
# offer a Chennai buyer a video call instead of the site, so it fails towards India.
r.eq("a bare 10-digit number is treated as Indian",
     config.is_overseas({"phone": "9884739289"}), False)
r.eq("no phone and no ad is not overseas", config.is_overseas({}), False)
r.eq("a missing lead is not overseas", config.is_overseas(None), False)

# --- the Experience Centre is retired ----------------------------------------
r.eq("experience_centre is gone from VISIT_VENUES",
     "experience_centre" in config.VISIT_VENUES, False)
r.eq("virtual is a venue now", "virtual" in config.VISIT_VENUES, True)
r.eq("site is still a venue", "site" in config.VISIT_VENUES, True)

# The mall strip used to be conditional on a distance objection. It is now absolute,
# so a reply that names the mall loses that sentence no matter what the buyer said.
mall = "Happy to help. If the drive feels long, you could see the Experience Centre " \
       "at Express Avenue instead. Whenever suits you."
r.check("an Experience Centre offer is stripped from a reply",
        "Express Avenue" not in qualifier._strip_mall(mall),
        detail=qualifier._strip_mall(mall))
r.check("the rest of the reply survives the strip",
        "Happy to help." in qualifier._strip_mall(mall),
        detail=qualifier._strip_mall(mall))

# --- the handoff card must not send sales to a gate for a video call ----------
conv_virtual = {"checklist": {"visit_day": "Sunday", "visit_time": "11am",
                              "visit_venue": "virtual", "purpose": "investment"}}
card = handoff.build_card({"project": "RON", "name": "Akram", "phone": "966510895539"},
                          conv_virtual)
joined = " | ".join(str(s) for s in card)
r.check("a virtual booking says VIRTUAL WALKTHROUGH, not SITE VISIT",
        "VIRTUAL WALKTHROUGH" in joined and "SITE VISIT BOOKED" not in joined,
        detail=joined[:200])
r.check("a virtual booking warns sales not to send directions",
        "DO NOT SEND DIRECTIONS" in joined, detail=joined[:200])

conv_site = {"checklist": {"visit_day": "Sunday", "visit_time": "11am",
                           "visit_venue": "site", "purpose": "weekend"}}
card_site = handoff.build_card({"project": "RON", "name": "Ravi", "phone": "919884739289"},
                              conv_site)
joined_site = " | ".join(str(s) for s in card_site)
r.check("a site booking still says SITE VISIT BOOKED",
        "SITE VISIT BOOKED" in joined_site, detail=joined_site[:200])
r.check("a site booking carries no overseas warning",
        "DO NOT SEND DIRECTIONS" not in joined_site, detail=joined_site[:200])

# --- the rulebook ------------------------------------------------------------
import answering                        # noqa: E402
visits = answering.RULES["visits"]
r.check("the rulebook forbids the Experience Centre",
        "Never offer the Experience Centre" in visits)
r.check("the rulebook names the video walkthrough",
        "video walkthrough" in visits.lower())
r.check("the rulebook still says the site is the win for Chennai buyers",
        "site" in visits.lower() and "chennai" in visits.lower())

sys.exit(0 if r.report("VENUE RULES") else 1)
