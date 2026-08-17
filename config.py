import os
import re

def _b(v): return str(v).lower() in ("1", "true", "yes", "on")

DATABASE_URL = os.environ["DATABASE_URL"]

# CAMPAIGN ALLOW-LIST. The bot only ever touches leads from these campaigns.
#
# Owner decision 2026-07-31: "we use this bot only for the leads from one campaign
# - not all", and old-lead reactivation is on hold until the conversation flow has
# been tested for real.
#
# An allow-list is safer BY CONSTRUCTION than the block-list the design assumed. A
# suppression list over 48,354 leads is only as good as its completeness -- one
# stage nobody thought of and the bot messages the wrong person. "Only these
# campaigns" has no such failure mode: everyone else is unreachable whatever their
# status. The suppression list still gets built (task 16) for when reactivation is
# switched on; it is no longer the thing standing between us and a first
# conversation.
#
# Matched case-insensitively against Sell.do's campaign name.
SELLDO = {
    "RON": {
        "db_url": os.environ["SELLDO_DB_URL_RON"],
        "project": "Republic Of Nature",
        "campaigns": [c.strip() for c in os.environ.get(
            "RON_CAMPAIGNS", "RON_Meta_BM,GTB RON BM website").split(",") if c.strip()],
    },
    "ELEMENTS": {
        "db_url": os.environ["SELLDO_DB_URL_ELEMENTS"],
        "project": "Elements Common",
        # No live campaign. Empty list = the bot touches no Elements lead at all.
        "campaigns": [c.strip() for c in os.environ.get(
            "ELEMENTS_CAMPAIGNS", "").split(",") if c.strip()],
    },
}


# DIRECT INBOUND -- a stranger messaging the business number.
#
# 58 people did this and got silence, most recently at 05:52 on 2026-08-02. They
# arrive from a click-to-WhatsApp ad, so THEY started the conversation: the 24-hour
# window is open, no template is needed, and intent is higher than anything sitting
# in a form list.
#
# Lead creation from an inbound was removed in task 1b because it guessed the brand
# from the customer's own message text, which rev 2 forbids. The project is now
# stamped from CONFIG, not from anything the customer typed -- only one project runs
# on this number. That is the "correct basis" task 14 was waiting for.
#
# The hard rule (owner, 2026-08-02): adopt ONLY a phone with no lead of any kind.
# A phone already attached to a GT Bharathi lead is left alone -- we no longer have
# rights to that audience. See [[phone-number-blocker]].
DIRECT_INBOUND_ENABLED = _b(os.environ.get("DIRECT_INBOUND_ENABLED", "true"))
DIRECT_INBOUND_PROJECT = os.environ.get("DIRECT_INBOUND_PROJECT", "RON")
DIRECT_INBOUND_CAMPAIGN = os.environ.get("DIRECT_INBOUND_CAMPAIGN", "direct_whatsapp")

# OUR OWN LEAD FORMS -- the live BM ones, verified against the Meta page
# "Republic of Nature by GTB" (1144824778724398) on 2026-08-02. ONE list, THREE
# uses, so they cannot drift apart:
#
#   1. the promote path stamps a lead's campaign with its form name
#   2. the allow-list must therefore contain those names, or the gate we built in
#      #6 silences every lead the promote path creates -- exactly the trap left
#      open in #7
#   3. the leadgen webhook refuses any form not on this list
RON_FORMS = [f.strip() for f in os.environ.get(
    "RON_FORMS", "RON_Villa_BM,RON_Villa_HI_BM").split(",") if f.strip()]

# THE ONE PRICE THE BOT MAY SAY. Every live ad publishes it -- "Luxury Villas on
# ECR | ₹3.94 Cr* Onwards" -- so a buyer arrives already knowing it. Refusing to
# repeat your own advertised number reads as evasion at the first question
# (owner, 2026-08-02: "yes the bot can say 3.94 cr starting price").
#
# It matches the price sheet, which rounds the cheapest villa to ₹3.9 Cr. The ad
# figure is the precise one and the one the buyer saw, so it is the one we quote.
#
# NOTHING ELSE. Apartment prices, the Rs 5.5 Cr four-bed, per-unit figures: still a
# human's job. _price_problem requires every figure to be traceable to a cited
# chunk, so the carve-out cannot widen by accident.
#
# Written "Rs", not the rupee sign: free session text reaches Wati as a URL query
# parameter rather than a JSON body, so a non-ASCII character is percent-encoded
# and depends on their decoder. The env override is stripped the same way, because
# the value set in Railway predates this rule. See _PUNCT in qualifier.py.
VILLA_PRICE_TEXT = os.environ.get(
    "VILLA_PRICE_TEXT", "Rs 3.94 Cr").replace("₹", "Rs ").replace("  ", " ").strip()
VILLA_FLOOR = int(os.environ.get("VILLA_FLOOR", "39400000"))     # Rs 3.94 crore

# --- price and configuration qualify TOGETHER (owner, 2026-08-02) -------------
#
# "each configuration and price has to be tied together - we cant qualify someone
# who says 1.2 without we confirming that config is apartment - so both go hand in
# hand - our job is to qualify for the price and unit configuration".
#
# One floor per configuration, not one floor for the project. A 1.5 Cr buyer is
# qualified for a 2BHK and NOT for the 3BHK they asked about, and the difference
# matters: sales receiving "qualified, wants 3BHK" for someone 6 lakh short of a
# 2BHK is the handoff that loses their trust in the queue.
CONFIG_FLOORS = (
    # (label, floor) -- ordered cheapest first; the label is what a card shows.
    ("Compact 2BHK apartment", 12800000),
    ("2BHK apartment",         14600000),
    ("3BHK apartment",         21000000),
    ("3 bed villa",            39400000),
    ("4 bed villa",            55000000),
)

# Buyers understate, and they stretch. Owner: "buyers will be able to strecch -
# 20% to 25% more is usually fine". So a stated budget is compared to the floor
# AFTER stretching, which is why 1.2 Cr qualifies for a 1.28 Cr apartment.
# Effective entry becomes about 1.02 Cr.
BUDGET_STRETCH = float(os.environ.get("BUDGET_STRETCH", "1.25"))


def classify_configuration(text):
    """Free text -> (label, floor). (None, None) when we cannot tell.

    Cannot-tell is not a failure to paper over: configuration is a hard gate, so
    an unrecognised answer means the bot keeps asking rather than guessing a floor.
    """
    t = (text or "").lower()
    if not t.strip():
        return None, None
    # OFF-CATEGORY FIRST. These appear in older material and are not currently
    # sold (see kb/RON/inventory.md), so they have no floor. Checked before
    # anything else because "island villa" contains "villa" and "1BHK" contains
    # "bhk" -- both would otherwise be priced as a product we cannot sell them.
    if re.search(r"\b1\s*bhk\b|\bone\s*bhk\b|villament|island\s*villa|"
                 r"beach\s*front|beachfront|\b5\s*bhk\b", t):
        return None, None
    if "villa" in t:
        if "4" in t or "four" in t:
            return "4 bed villa", 55000000
        if "3" in t or "three" in t:
            return "3 bed villa", 39400000
        return "3 bed villa", 39400000          # villas start at the 3 bed
    if "apartment" in t or "bhk" in t or "flat" in t:
        if "compact" in t:
            return "Compact 2BHK apartment", 12800000
        if "3" in t or "three" in t:
            return "3BHK apartment", 21000000
        if "2" in t or "two" in t:
            return "2BHK apartment", 14600000
        return "Compact 2BHK apartment", 12800000   # apartments start here
    return None, None


def budget_reaches(budget, floor):
    """Can this budget reach that floor once stretched?"""
    if not isinstance(budget, int) or budget <= 0 or not floor:
        return False
    return budget * BUDGET_STRETCH >= floor


def affordable_configs(budget):
    """Every configuration this budget can reach, cheapest first."""
    return [(label, floor) for label, floor in CONFIG_FLOORS
            if budget_reaches(budget, floor)]

# LEADGEN WEBHOOK. Meta pushes a lead the moment the form is submitted, so the
# first template goes out in seconds instead of waiting up to 15 minutes for the
# next poll. Polling stays on as the safety net for anything a webhook misses.
#
# META_VERIFY_TOKEN is any random string; the SAME value goes in the Meta app's
# webhook configuration. META_APP_SECRET signs every delivery -- without it we
# would accept a lead from anyone who guessed the URL, so an unset secret means
# the endpoint refuses everything rather than trusting it.
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN") or os.environ.get("VERIFY_TOKEN")
META_APP_SECRET = os.environ.get("META_APP_SECRET")
LEADGEN_WEBHOOK_ENABLED = _b(os.environ.get("LEADGEN_WEBHOOK_ENABLED", "true"))

# Appended in code rather than in the RON_CAMPAIGNS default so that overriding the
# campaign list in Railway can never silently strip it and mute every walk-up.
if DIRECT_INBOUND_ENABLED and DIRECT_INBOUND_PROJECT in SELLDO:
    SELLDO[DIRECT_INBOUND_PROJECT]["campaigns"].append(DIRECT_INBOUND_CAMPAIGN)

# THE GAP FROM #7, CLOSED. A lead created from a Meta form is stamped with the
# FORM's name, not a Sell.do campaign name. Without these entries the allow-list
# gate fails closed on every one of them -- the promote path and the leadgen
# webhook would both create leads correctly and then be unable to say a word to
# them. Same reasoning as above: appended in code so an env override cannot
# silently remove them.
if "RON" in SELLDO:
    SELLDO["RON"]["campaigns"].extend(RON_FORMS)


def campaign_allowed(project_key, campaign):
    """Is this lead's campaign one the bot may talk to? Case-insensitive.

    Unknown project or missing campaign -> False. The gate fails CLOSED: a lead we
    cannot attribute is a lead we do not message.
    """
    if not campaign:
        return False
    allowed = SELLDO.get(project_key, {}).get("campaigns") or []
    return campaign.strip().lower() in {a.lower() for a in allowed}

META_TOKENS = {
    "RON": os.environ["META_TOKEN_RON"],
    "ELEMENTS": os.environ["META_TOKEN_ELEMENTS"],
}
GRAPH = "https://graph.facebook.com/v19.0"

WASENDER_API_KEY = os.environ.get("WASENDER_API_KEY", "")
WASENDER_SESSION_ID = os.environ.get("WASENDER_SESSION_ID", "")
WASENDER_BASE = "https://wasenderapi.com/api"
WASENDER_WEBHOOK_SECRET = os.environ.get("WASENDER_WEBHOOK_SECRET", "")

# --- WhatsApp provider switch ---
# Which engine the sequencer sends through: "wasender" (legacy, default) or
# "wati" (official). Deploy stays on wasender until this is flipped to wati in
# the Railway env -- lets the new code ship dark and roll back in one flip.
WHATSAPP_PROVIDER = os.environ.get("WHATSAPP_PROVIDER", "wasender").lower()

# --- Wati (official WhatsApp Cloud API) ---
# WATI_BASE example: https://live-server-12345.wati.io   (no trailing slash)
WATI_BASE = os.environ.get("WATI_BASE", "").rstrip("/")
# Store token WITHOUT the "Bearer " prefix; wati.py adds exactly one. Strip it
# here so a pasted "Bearer xxx" doesn't become "Bearer Bearer xxx".
WATI_TOKEN = os.environ.get("WATI_TOKEN", "").replace("Bearer ", "").strip()
WATI_WEBHOOK_SECRET = os.environ.get("WATI_WEBHOOK_SECRET", "")

# --- Knock templates ---
# The six carnival templates (gtb_m1_ron_final, gtb_m2_followup_final,
# gtb_m3_reminder, ...) are REMOVED, Phase 0 task 1b. All six were carnival copy
# -- an event that ended on 12 July -- so none could be reused for a nurture
# knock. They are also unrecoverable as copy: WhatsApp templates are approved
# per exact body text, so new copy needs new approval regardless.
#
# The replacement set is the amended 4-knock sequence (POST-CARNIVAL-DESIGN §6,
# amended 2026-07-30): T1 lifestyle (day 0) · T2 location (day 3) · T3
# low-density (day 10) · T6 visit invitation (day 25, rewritten to drop the
# booking flow). Populated by task 17 once Meta approves them; env-overridable
# so an approval-name change is not a code change.
# Verified against the live Wati account 2026-07-31: all four are APPROVED.
# Defaults are the real approved names, so the knock engine needs no env config to
# work; override only if a template is replaced.
#
# --- THE FACEBOOK BUSINESS MOVE, 2026-08-06 -------------------------------
# The WhatsApp number did NOT change; the Facebook business it sits under did.
# Templates are approved per business, so every one was resubmitted under a new
# name. All four below were compared against their predecessors on the live
# account -- body, variable count and quick-reply buttons identical -- so this is
# a rename and nothing else. That check is not optional: a template whose
# variable count differs is a send that fails outright, and the day-25 message
# declares no variables at all.
#
# ⚠️ THE SUFFIX IS NOT CONSISTENT. Three are `_newac`, the visit invitation is
# `_new_acc`. Do not "correct" one to match the others and do not pattern-match
# on the suffix -- these are the exact strings Meta approved, and a name that is
# nearly right is a name that does not exist.
#
# The first attempt at the visit template carried the GHOST RE-OPENER's copy
# under the visit template's name. It has since been deleted and both were
# resubmitted correctly; tests/rules.py keeps the two apart so the crossing
# cannot come back quietly.
KNOCK_TEMPLATES = {
    "t1_lifestyle":   os.environ.get("WATI_TPL_T1", "ron_nurture_01_lifestyle_newac"),
    "t2_location":    os.environ.get("WATI_TPL_T2", "ron_nurture_02_location_newac"),
    "t3_low_density": os.environ.get("WATI_TPL_T3", "ron_nurture_03_low_density_newac"),
    "t6_visit":       os.environ.get("WATI_TPL_T6", "ron_nurture_06_visit_new_acc"),
}


def _variants(*names):
    """Approved templates carrying the SAME message, in the order they are tried.

    Empty slots are dropped, so a step with no alternates written yet degrades to
    the single template it already had rather than to nothing.
    """
    seen, out = set(), []
    for n in names:
        n = (n or "").strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# Up to three wordings per knock, cycled on refusal (owner, 2026-08-11: "3 variants
# - cycling upto 10 tries").
#
# ⚠️ EVERY VARIANT OF A STEP MUST DECLARE THE SAME VARIABLES. The parameter list is
# chosen per STEP in knocks.TEMPLATE_TAKES_NAME, not per template, because a knock
# is one message with three phrasings. A variant that adds or drops a {{1}} fails on
# send with a parameter-count error -- which the retry loop would then treat as a
# Meta refusal and spend ten attempts on. Marketing must keep the variables
# identical across the variants of a step.
#
# The _B and _C slots are unset until marketing has approved alternates, so today
# every step has exactly one variant and the rotation is a no-op.
KNOCK_TEMPLATE_VARIANTS = {
    "t1_lifestyle": _variants(KNOCK_TEMPLATES["t1_lifestyle"],
                              os.environ.get("WATI_TPL_T1_B"),
                              os.environ.get("WATI_TPL_T1_C")),
    "t2_location": _variants(KNOCK_TEMPLATES["t2_location"],
                             os.environ.get("WATI_TPL_T2_B"),
                             os.environ.get("WATI_TPL_T2_C")),
    "t3_low_density": _variants(KNOCK_TEMPLATES["t3_low_density"],
                                os.environ.get("WATI_TPL_T3_B"),
                                os.environ.get("WATI_TPL_T3_C")),
    "t6_visit": _variants(KNOCK_TEMPLATES["t6_visit"],
                          os.environ.get("WATI_TPL_T6_B"),
                          os.environ.get("WATI_TPL_T6_C")),
}

# --- Retrying a knock Meta refused (2026-08-11) -------------------------------
#
# Meta refuses template sends per RECIPIENT and temporarily, not per sender: on
# 2026-08-11 two numbers each got two knocks the same day, one read and one refused.
# So the same person can be reached later with a different wording.
#
# OFF BY DEFAULT. It changes the meaning of a send already recorded as ok, and it
# is the first thing in this system that deliberately messages someone again after
# a failure. That deserves a switch someone has to turn on.
KNOCK_RETRY_ENABLED = os.environ.get("KNOCK_RETRY_ENABLED", "false").lower() == "true"
# Owner: retry ten times, then mark the person lost and leave them alone forever.
KNOCK_RETRY_MAX = int(os.environ.get("KNOCK_RETRY_MAX", "10"))
# "another version of the same message next day".
KNOCK_RETRY_GAP_HOURS = int(os.environ.get("KNOCK_RETRY_GAP_HOURS", "24"))

# Ghost re-opener (task 18/19). Someone who talked and then went quiet cannot be
# sent the COLD sequence -- those templates introduce the project from scratch to a
# person who already told us their budget, which reads as broken at exactly the
# moment they are most likely to leave.
#
# ⚠️ PENDING approval as of 2026-07-31, and submitted as MARKETING rather than
# UTILITY. Marketing carries the category gate that blocked ~44% of the carnival's
# cold sends, so re-opener delivery will be worse than it needs to be. Worth
# resubmitting as utility later -- it continues a conversation the customer started.
#
# Moved to the new Facebook business 2026-08-06 along with the knock set. Verified
# on the live account: two variables (name, topic), no buttons, body unchanged.
REOPENER_TEMPLATE = os.environ.get("WATI_TPL_T7", "t7_reopener_newac")

# --- Staff card template (2026-08-06) ---
# Staff notifications went out as free session text until this existed, so they
# only delivered to a salesperson who happened to have messaged the business
# number in the previous 24 hours. Measured over 30 days: 5 of 24 cards (21%)
# were rejected by WhatsApp with "Ticket has been expired." -- four of them
# escalations, where a buyer had asked for a human. Nothing reported it.
#
# A template ignores the 24h window; that is what templates are for. Submitted
# as UTILITY: it is an internal operational notice, not marketing. If Meta
# reclassifies it to MARKETING it still delivers outside the window -- marketing
# only adds a per-recipient frequency cap, which a handful of cards a day to
# four staff phones will not reach.
#
# Five numbered slots, and NONE of them may contain a newline: WhatsApp rejects
# the whole send if a parameter carries one. handoff._slot() enforces that.
STAFF_TEMPLATE = os.environ.get("WATI_TPL_STAFF", "ron_staff_card_01")

# The `topic` variable is filled from a CLOSED LIST, never from the agent's own
# words. An approved template plus a freely-generated variable is still a message we
# are accountable for, and it is the one place a stray price or claim cannot be
# retracted. Values are short, neutral, and contain no figure, date or commitment.
#
# "your enquiry" is the default and will be the commonest value: most ghosts go
# quiet BEFORE saying anything specific.
REOPENER_TOPICS = {
    "apartments":   "the apartments",
    "compact_2bhk": "the compact 2BHK apartments",
    "2bhk":         "the 2BHK apartments",
    "3bhk":         "the 3BHK apartments",
    "villas":       "the villas",
    "location":     "the location on ECR",
    "sizes":        "the layouts and sizes",
    "amenities":    "the amenities",
    "lagoon":       "the lagoon and beach experience",
    "default":      "your enquiry",
}

# --- Carnival event constants: REMOVED, Phase 0 task 1b (2026-07-30) ---
# EVENT_NAME / EVENT_VENUE / EVENT_MAPS_LINK / EVENT_TIMING / EVENT_DATES are
# gone. EVENT_DATES was the load-bearing constant of the whole old lifecycle:
# reply parsing indexed into it BY POSITION (a reply of "1" meant carnival day
# one) and three send guards compared against EVENT_DATES[-1]. Those guards were
# what kept the system inert after 12 July -- SEND_ENABLED (below) now does that
# job deliberately, which is why task 1a had to land before this deletion.
#
# `parser.py` is orphaned by this change: nothing imports it any more. Left on
# disk rather than deleted so the old day-picker logic stays readable; safe to
# remove whenever you like. Same for `scripts/smoke_test.py`, which exercises
# carnival flows that no longer exist.

import re as _re

def status_qualifies(raw):
    """Stage names arrive as '(Pre Sales) Interested', 'Interested',
    '(Pre Sales) Site Visit Scheduled', etc. Normalize and match."""
    if not raw:
        return False
    s = _re.sub(r"^\(\s*pre[\s_-]*sales?\s*\)\s*", "", raw.strip().lower())
    return s in {"interested", "site visit scheduled"}

# Optional page restriction for Meta lead-form polling (comma-separated page IDs)
META_PAGE_IDS = {
    "RON": [p for p in os.environ.get("META_PAGE_IDS_RON", "").split(",") if p],
    "ELEMENTS": [p for p in os.environ.get("META_PAGE_IDS_ELEMENTS", "").split(",") if p],
}
LEADS_SINCE = "2026-06-25"

# Direct Meta -> sequencer promotion. A lead that lands on one of these forms is
# invited straight away, without waiting for Sell.do to stage it as Interested.
# The list is an EXACT form-name allow-list, not a keyword: meta_leads still
# holds rows from other projects (Rising_Palms, Central_park, Uptown, Madhuram)
# pulled before FORM_FILTER was narrowed, and substring matching on "carnival"
# would also sweep in the broad-audience RON forms (~655 people) that we
# deliberately do not message.
# RON side is restricted to the _BM forms only. The other RON carnival forms
# (_Apt, _Villa, _Villa 1, Ron_carnival_*) and the broad-audience forms are
# deliberately excluded.
PROMOTE_FORMS = list(RON_FORMS)
# Only promote recent form fills. Without this the first run would sweep the
# entire LEADS_SINCE backlog (729 people) into the send queue at once -- three
# days of sending on a 250/day tier, for an event that ends in two.
PROMOTE_WINDOW_HOURS = int(os.environ.get("PROMOTE_WINDOW_HOURS", "24"))
PROMOTE_ENABLED = _b(os.environ.get("PROMOTE_ENABLED", "false"))

# M2 is the cold follow-up to people who never answered M1. On event day it
# competes with venue reminders for the hourly allowance, so it can be held.
M2_ENABLED = _b(os.environ.get("M2_ENABLED", "true"))

# Shared secret carried in the Wati webhook PATH (/webhook/wati/<token>).
# Wati sends no custom headers -- that is why WATI_WEBHOOK_SECRET must stay blank
# or every real post 403s -- but it will POST to any URL you give it. Only the
# secret path is allowed to CREATE leads from an inbound message; the legacy
# unauthenticated route can merely update leads that already exist. Blank token
# disables the secret route entirely (it 403s), so walk-ins are off by default.
WATI_PATH_TOKEN = os.environ.get("WATI_PATH_TOKEN", "").strip()

# Landing-page WhatsApp button. A visitor messages us first, which opens a 24h
# WhatsApp service window: we may reply with free text, no approved template and
# no MARKETING-category gate (the gate that blocks ~44% of our cold sends and
# every US recipient outright). Off by default -- creating leads from an inbound
# message is only safe on the authenticated webhook route.
WALKIN_ENABLED = _b(os.environ.get("WALKIN_ENABLED", "false"))

# --- Site visits (owner decision 2026-07-31, REVERSES "capture, never confirm") ---
#
# The bot now takes a day and a time and acknowledges it. The earlier rule was that
# it must never confirm a slot; the owner's reasoning for changing it is that a
# buyer who picks a day and gets no acknowledgement is left at a dead end, which is
# a worse experience than the risk it was avoiding.
#
# WHAT THE BOT MAY SAY is bounded, and the boundary is the whole point: "booked, and
# our team will call to confirm the timing and share directions." The bot has no
# calendar, so an unqualified "confirmed" is a promise the company has not agreed to
# keep -- and the failure mode is a buyer standing at a gate in Vadanemmeli on a
# Saturday. This wording gives the buyer a real commitment while leaving the team
# room to move an hour.
VISIT_CONFIRMATION = "booked_team_confirms"

# Availability. Tuesday is the sales team's weekly off; Monday mornings they are in
# their weekly meeting, so Monday is afternoon-only.
VISIT_DAYS = {
    "mon": "afternoon",   # first half is the team's weekly meeting
    "tue": None,          # weekly off -- never offer, and never accept
    "wed": "full",
    "thu": "full",
    "fri": "full",
    "sat": "full",
    "sun": "full",
}

# Two venues, and the ORDER MATTERS.
#
# The site is always offered first, because a site visit is the
# definition of a win (design §2). The Experience Centre is NOT an equal option: it
# is a distance-objection handler and a stepping stone. Offering it unprompted would
# quietly convert site visits into mall visits, which is a downgrade the bot must
# never make on its own.
#
# The intended ladder: someone worried about the drive sees the miniature model at
# the Experience Centre during the week, and books the real site visit for the
# weekend. The EC visit is a milestone, not the outcome.
# REWRITTEN 2026-08-11. Owner: "stop asking for visit to experience center - we want
# ppl to visit the site if they are in chennai - if they are outside chennai - like
# this NRI campaign - we have to push them for a virtual walk thru".
#
# The Experience Centre is RETIRED. It was the answer to a distant buyer, and a live
# video walkthrough is a better one: the mall showed a miniature model, the call shows
# the actual site, and nobody has to fly. So the venue now follows WHERE THE BUYER IS
# rather than how strongly they objected to the drive.
VISIT_VENUES = {
    "site": {
        # Owner 2026-08-01: never say "Vadanemmeli" to a buyer -- it does not help
        # the positioning and nobody knows where it is. qualifier._rename_locality
        # enforces this on every outbound reply; this is the phrasing it uses.
        "name": "the site on ECR, near Kovalam Junction",
        "priority": 1,
        "offer": "in_chennai",
    },
    "virtual": {
        "name": "a live video walkthrough with one of our team",
        "priority": 1,
        "offer": "outside_chennai",
        # Booked exactly like a site visit -- a day and a time -- because it IS an
        # appointment with a person, not a link. The handoff card must say so, or
        # sales turns up expecting someone at the gate.
        "note": ("A salesperson walks them through the site live on a call. Same day "
                 "rules as the site. Never send directions for this one."),
    },
}

# The ads that target buyers abroad. Env-driven because ad ids churn every campaign
# and marketing must be able to add one without a deploy.
#
# 52553896609352 is the NRI campaign targeting the Middle East (owner, 2026-08-11).
# It produced lead 1016 -- a +966 number who asked to be phoned five different ways
# and was told "just tell me a day and I'll set up the visit" each time.
NRI_AD_IDS = [a.strip() for a in os.environ.get(
    "NRI_AD_IDS", "52553896609352").split(",") if a.strip()]

# --- not spending a model call on the word "Ok" (2026-08-11) -------------------
#
# Outcomes where a human already owns the conversation. Only in these does a bare
# acknowledgement get the fixed reply instead of a model turn: before handoff, "ok"
# and "sure" are often real answers to "shall I pencil in Sunday?" and must be heard.
#
# `nurture` is NOT here. A nurtured buyer is one the bot is still actively working --
# owner 2026-08-03, probe for room, never kill -- so their "ok" still deserves a turn.
HANDED_OFF_OUTCOMES = ("qualified", "visit_booked", "escalated", "wants_sales", "dead")

# ⚠️ SALES OWNS THIS WORDING, like FRAMINGS and SALES_OFFER_FRAMING. It is the one
# sentence a buyer gets when they say "ok" after being handed over, so it has to close
# warmly without promising a time nobody has committed to.
ACK_REPLY = os.environ.get(
    "ACK_REPLY", "Sure - someone from our team will be in touch shortly.")

# --- shorter, warmer, less interrogative (owner, 2026-08-17) -------------------
#
# Measured across 623 turns that day, alongside a competitor conversation the owner
# supplied as the reference for tone:
#
#                      that bot    ours     our reply rate
#   median length      171 ch      304 ch   120-240ch: 71.6%  240-400: 42.8%
#   carries a question    30%       81%     no question: 70.6%  one: 47.9%
#   uses the name         61%       <1%
#
# Our median message sat in our own worst-performing length bucket.

# Ask a qualifying question at most this often. 2 = ask, let two turns breathe, ask.
# Turned down to 1 restores the old behaviour of asking almost every turn.
GATE_EVERY_N_TURNS = int(os.environ.get("GATE_EVERY_N_TURNS", "2"))

# HARD CEILING on a reply, enforced in qualifier._enforce rather than requested in
# the rulebook. The rulebook has said "two or three lines is usually plenty" since
# it was written and the median came out at 304 characters anyway -- a prompt is a
# request, and this is the guarantee. The reference bot's longest message was 282.
MAX_REPLY_CHARS = int(os.environ.get("MAX_REPLY_CHARS", "300"))

# Junk profile names. The buyer controls this string, so it is display-only -- but
# now that it is spoken back to them, "Hi Muna💞💞💞" is worse than no name at all.
_NAME_JUNK = re.compile(r"^(test|testing|abc|xyz|na|n/?a|none|null|user|guest|"
                        r"customer|admin|hi|hello|sir|madam|unknown)$", re.I)


def clean_first_name(raw):
    """A first name safe to say out loud, or None.

    Returns None rather than a cleaned-up guess whenever there is doubt: a message
    addressed to nobody reads fine, and one addressed to "Hi 9" does not.
    """
    if not raw:
        return None
    first = str(raw).strip().split()[0] if str(raw).strip() else ""
    # Keep letters and internal apostrophes/hyphens; drop emoji, digits, symbols.
    first = re.sub(r"[^A-Za-zÀ-ɏ'\-]", "", first).strip("'-")
    if len(first) < 2 or len(first) > 20:
        return None
    if _NAME_JUNK.match(first):
        return None
    # A name that was mostly decoration -- "💞💞Mu💞" -> "Mu" -- is not a name.
    if len(first) < len(re.sub(r"\s", "", str(raw).split()[0])) / 2:
        return None
    return first[:1].upper() + first[1:]


def is_overseas(lead):
    """Is this buyer outside India, so a site visit is the wrong ask?

    TWO SIGNALS, EITHER IS ENOUGH.
      * the ad they came from targets buyers abroad (NRI_AD_IDS)
      * their number is not Indian

    WHAT THIS DELIBERATELY DOES NOT DO is guess at "outside Chennai but inside
    India". A Bangalore buyer should also be offered the walkthrough, but nothing we
    store tells us where a person IS -- the location gate asks where they want to
    BUY, which is a different question, and it was rewritten on 2026-08-02 precisely
    because the model kept merging the two. So the code claims only what it can
    prove, and the rulebook handles the rest: if a buyer SAYS they are not in
    Chennai, the model offers the walkthrough on that basis.
    """
    if not lead:
        return False
    ad = str(lead.get("ctwa_source_id") or "")
    if ad and ad in NRI_AD_IDS:
        return True
    phone = re.sub(r"\D", "", str(lead.get("phone") or ""))
    if not phone:
        return False
    # A bare 10-digit number is Indian -- meta.normalize_phone() adds the 91, but not
    # every path through the database has been through it, and reading an Indian
    # mobile as overseas would offer a Chennai buyer a video call instead of the site.
    # Fails towards "in India", which is the safer error: the site is the better ask
    # when we are unsure, and the buyer can always say they are abroad.
    if len(phone) <= 10:
        return False
    return not phone.startswith("91")

# --- Persuasion ladder (design §7, task 23) ---
#
# Owner requirement: "the agent should be able to persuade them to answer in a
# gentle, persuasive manner -- this is important."
#
# Three framings per gate, each carrying a reason that benefits THEM. The bot never
# reuses a framing inside one conversation: asking the same question the same way
# twice is what makes a bot feel like a form.
#
# ⚠️ SALES OWNS THIS WORDING, not engineering (design §7). It lives in config so it
# is data rather than code; it moves to the `agents.framings` column when the
# corpus-upload screen exists (task 28).
FRAMINGS = {
    "purpose": [
        "so I can show you the homes that suit how you'd actually use the place",
        "because a weekend home and a primary home are very different picks here",
        "so I don't waste your time on the wrong side of the project",
    ],
    # REWRITTEN 2026-08-02. The previous three were "whether this stretch of ECR
    # works for you", "the drive matters differently depending on where you're
    # coming from" and "whether we're the right fit". Every one planted doubt
    # before the buyer had any, and the bot came across as apologetic about a
    # premium coastal project -- owner: "we are apologetic about someone who wants
    # a full time home and we are already defensive if that works out for them".
    #
    # They also asked two different questions. "Where are you coming from" is where
    # they LIVE; the gate wants where they want to BUY, and the model merged them
    # into "based in or looking to buy around?", which a real buyer answered "Yes".
    # One meaning now, and a reason that gives them something.
    "location": [
        "so I can show you how it connects to the places you already go",
        "so I can line up the right homes and the right views before you come",
        "so I can tell you what the drive actually looks like from your side of town",
    ],
    "configuration": [
        "so I can tell you what's actually available rather than everything at once",
        "because the apartments and the villas are quite different experiences",
        "so I can point you at the two or three worth seeing",
    ],
    "budget": [
        "only so I show you homes that are genuinely in range — nothing above it",
        "so our team doesn't put you in front of the wrong homes on a site visit",
        "just a rough band is plenty — it saves you being shown things you'd rule out",
    ],
}

# After this many consecutive asks with no answer, flag a human. The bot KEEPS
# ANSWERING -- this is a flag, not a hand-off. A buyer asking good questions while
# dodging the checklist is engaged, not obstructive, and cutting them off would be
# the wrong read.
UNRECIPROCATED_LIMIT = int(os.environ.get("UNRECIPROCATED_LIMIT", "3"))

# --- The buyer who will not name a budget (owner, 2026-08-06) ---
#
# "if someone is not giving budget - we should ask them - if they want to speak to
# sales team and take it forward - that is good enough test of their seriousness".
#
# A buyer who gives purpose, location and configuration but keeps stepping around
# the money question used to sit forever: budget is a HARD gate in
# clears_the_bar(), so no budget meant no card, ever. Agreeing to meet a
# salesperson is the owner's substitute signal -- harder to fake than a number
# typed into WhatsApp in ten seconds.
#
# ⚠️ THIS DOES NOT LOOSEN THE BUDGET GATE. It is a separate exit with its own
# outcome (`wants_sales`) and its own card headline, so the qualified queue still
# means what it meant last week and the salesperson knows before dialling that
# there is no figure. Anyone who STATES a low number is captured and nurtured
# instead -- this path only opens when the number is genuinely absent.
#
# Two asks, not three: the budget gate has three framings, and the owner's read is
# that a buyer who has stepped around it twice has already decided not to answer.
SALES_OFFER_AFTER_ASKS = int(os.environ.get("SALES_OFFER_AFTER_ASKS", "2"))

# ⚠️ SALES OWNS THIS WORDING, like FRAMINGS above. Chosen by the owner 2026-08-06
# from three drafts: offer the call, not the site visit. The visit is a bigger ask
# of somebody who is still guarding what they will spend.
SALES_OFFER_FRAMING = os.environ.get(
    "SALES_OFFER_FRAMING",
    "would it be easier if someone from our team gave you a call and took this "
    "forward")

# --- Handoff (design §8, task 24) ---
#
# ⚠️ THE DESIGN SAYS "WhatsApp group ping". THE OFFICIAL WHATSAPP CLOUD API CANNOT
# SEND TO A GROUP -- it addresses individual numbers only. So the handoff goes to a
# designated number, and the Wati Team Inbox is where the conversation itself gets
# picked up. Flagged for the owner; the card content is unaffected either way.
# Owner-supplied 2026-07-31: both qualified leads and escalations go to these two
# numbers. Comma-separated, so adding or removing a recipient is an env change.
#
# The design flags qualified and escalated as OPPOSITE urgencies -- "good news, call
# them" versus "we're stuck, rescue this" -- and warns that mixed into one
# destination the rescues get missed. The owner has chosen one destination for now;
# these are two separate variables precisely so they can be split later without a
# code change.
_DEFAULT_RECIPIENTS = "6374393030,9789988124"


def _phones(raw):
    """Comma-separated numbers -> normalised E.164-ish digits, deduped, order kept."""
    import re as _r
    out = []
    for part in (raw or "").split(","):
        d = _r.sub(r"\D", "", part)
        if not d:
            continue
        if len(d) == 10:
            d = "91" + d          # bare Indian mobile
        elif d.startswith("0") and len(d) == 11:
            d = "91" + d[1:]
        if d not in out:
            out.append(d)
    return out


HANDOFF_PHONES = _phones(os.environ.get("HANDOFF_PHONES", _DEFAULT_RECIPIENTS))
ESCALATION_PHONES = _phones(os.environ.get("ESCALATION_PHONES", _DEFAULT_RECIPIENTS))

# ONE STAFF LIST, not two. Owner 2026-08-06: the same people receive qualified
# cards and escalations, so the split above was a distinction nobody was making.
# The two old variables remain as the FALLBACK because they are already set in
# Railway -- deleting them here would empty the recipient list on deploy and the
# only symptom would be silence, which is exactly the failure mode this whole
# change exists to remove.
STAFF_PHONES = _phones(os.environ.get(
    "STAFF_PHONES", ",".join(HANDOFF_PHONES + ESCALATION_PHONES)))

# WHO HEARS THAT THE SYSTEM IS BROKEN -- deliberately NOT STAFF_PHONES.
# Owner 2026-08-08, asked whose phone should ring when a buyer gets silence:
# himself only. A salesperson cannot act on "the queue is stalled", and putting
# it in the channel they rely on for hot leads teaches them to skim that channel.
# The one number they must always trust stays uncontaminated.
#
# Defaults to the owner's number, already present above, so the watchdog works
# without a Railway change. An alerting system whose default is "nobody" fails
# exactly like the silence it exists to detect.
ALERT_PHONES = _phones(os.environ.get("ALERT_PHONES", "9789988124"))

# How often the watchdog looks. Fifteen minutes is the gap between a buyer being
# ignored and somebody knowing -- the incident that prompted this ran for eight
# hours. Cheap: three indexed counts.
WATCHDOG_CHECK_MIN = int(os.environ.get("WATCHDOG_CHECK_MIN", "15"))

# When the daily heartbeat goes out (IST, 24h). Off the hour on purpose -- the
# scheduler already has four jobs and there is no reason to bunch them.
WATCHDOG_DAILY_HOUR = int(os.environ.get("WATCHDOG_DAILY_HOUR", "9"))
WATCHDOG_DAILY_MIN = int(os.environ.get("WATCHDOG_DAILY_MIN", "7"))

# --- Budget gate (design §2) ---
# The bot compares what a buyer says against these privately. It never quotes them,
# and no price is in the corpus to quote -- the gate is internal arithmetic.
#
# Rupees, not crores, so there is no unit ambiguity at a comparison site.
#
# ⚠️ The floor was ₹1.5cr until 2026-07-31, when the owner's price sheet showed the
# real entry price is ₹1.28cr. Budget is a HARD gate: a wrong floor sends qualified
# buyers to Dead and suppresses them permanently, and produces no error and no
# complaint -- you simply never hear from the people it discarded. Re-check this
# whenever pricing moves.
#
# ⚠️ THERE IS EXACTLY ONE FLOOR AND IT IS DERIVED. Until 2026-08-03 a separate
# BUDGET_FLOOR env var held ₹1.28 cr as its own number, and it was compared RAW
# while clears_the_bar compared the same figure STRETCHED. Two floors, one stretched
# and one not, and the un-stretched one fired first -- so a ₹1.1 cr buyer, who
# stretched reaches ₹1.375 cr and can genuinely afford the entry apartment, was
# marked dead and suppressed permanently. Exactly the failure the comment above
# warns about: no error, no complaint, you simply never hear from them again.
#
# So the entry price is now READ OFF CONFIG_FLOORS, and every comparison goes
# through budget_reaches(). One number cannot disagree with itself.
ENTRY_FLOOR = CONFIG_FLOORS[0][1]                                   # ₹1.28 cr

# A signal for sales, not a filter: somebody with more money than the top unit is a
# good problem, not an unqualified lead.
BUDGET_CEILING = int(os.environ.get("BUDGET_CEILING", "55000000"))  # ₹5.5 cr

# --- Knowledge base / RAG (task 8) ---
# pgvector on the existing Railway Postgres -- no second datastore. The brand fence
# is then a WHERE clause on the same database that already knows which project a
# lead belongs to, rather than a distributed-consistency problem (design §11).
#
# EMBED_DIM must match the embedding model. It is baked into the column type
# because HNSW indexing requires a fixed dimension, so changing model is a
# RE-INDEX (drop chunks, re-embed), never an in-place edit. The model name is
# stored on every chunk so a mismatch is detectable instead of silently wrong.
#
# Provider: VOYAGE (owner has a key from another project, 2026-07-31).
# Chosen over Supabase's built-in gte-small for one specific reason: gte-small is
# English-only (384 dims) and this audience writes Tanglish on WhatsApp -- "2bhk
# irukka", "price enna", "ECR la epdi". Matching those to the right FAQ row IS the
# retrieval step, so an English-only model would raise the escalation rate for no
# saving; the corpus is ~100 chunks, so embedding cost is a rounding error either way.
#
# voyage-4-large, 1024 dims native. NOTE: the 3-series names (voyage-3-large) are
# superseded -- do not reintroduce them.
EMBED_MODEL = os.environ.get("EMBED_MODEL", "voyage-4-large")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")
VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
# Provider caps a request at 1000 texts; stay well under so a retry is cheap.
EMBED_BATCH = int(os.environ.get("EMBED_BATCH", "100"))

# Retrieval over-fetch. With an HNSW index plus a `WHERE brand_id` filter, Postgres
# can filter AFTER the index scan and return fewer than k rows for the brand. Ask
# for more than we need, then trim. Corpora here are tens of documents, so the cost
# is nil and the alternative is a silently short answer.
RETRIEVE_K = int(os.environ.get("RETRIEVE_K", "6"))
RETRIEVE_OVERFETCH = int(os.environ.get("RETRIEVE_OVERFETCH", "40"))

# --- Retry ceiling (Phase 0, task 4) ---
# Two ceilings, because not every failure is the recipient's fault.
#
# RETRY_MAX_RECIPIENT -- failures attributable to THIS PERSON (their number is not
#     on WhatsApp, they have blocked us). Low, because each one is real evidence.
# RETRY_MAX_TRANSIENT -- timeouts, 5xx, rate limits, and anything unrecognised.
#     Higher, because these say nothing about the lead.
#
# Failures classed as SYSTEM (template not approved, bad token, 24h window shut)
# count toward NEITHER. That preserves the original reasoning at sequencer.py's
# old retry comment: while templates were pending approval every send failed for
# reasons unrelated to the lead, and a strike rule would have killed good leads.
RETRY_MAX_RECIPIENT = int(os.environ.get("RETRY_MAX_RECIPIENT", "3"))
RETRY_MAX_TRANSIENT = int(os.environ.get("RETRY_MAX_TRANSIENT", "6"))
RETRY_WINDOW_DAYS = int(os.environ.get("RETRY_WINDOW_DAYS", "30"))

# --- Fatigue cap (Phase 0, task 3) ---
# Owner decision 2026-07-30: a new reason RESETS the knock counter. Because "a new
# reason" is loose and hard to police, that generosity is made safe by a second
# ceiling that NOTHING can reset.
#
#   KNOCK_MAX_PER_JOURNEY -- resettable. The 4-knock sequence (day 0/3/10/25).
#   FATIGUE_MAX_PER_WINDOW -- NOT resettable, ever. A rolling ceiling, so even an
#       immediate reset cannot produce a burst. Matches the RON nurture plan's own
#       guardrail: never more than two nurture messages in the same week.
#
# Both are counted from send history rather than held as stored numbers, so a
# count cannot be forged -- only a journey's start marker moves, and that is
# logged with a reason in journey_resets.
KNOCK_MAX_PER_JOURNEY = int(os.environ.get("KNOCK_MAX_PER_JOURNEY", "4"))
FATIGUE_WINDOW_DAYS = int(os.environ.get("FATIGUE_WINDOW_DAYS", "7"))
FATIGUE_MAX_PER_WINDOW = int(os.environ.get("FATIGUE_MAX_PER_WINDOW", "2"))

# --- Master send switch (Phase 0, task 1a) ---
# Default FALSE. Nothing sends until this is deliberately turned on in the env.
#
# This exists because the system's current inertness is ACCIDENTAL: every send
# loop skips because today is past the last carnival day (sequencer.py:185,
# :337, :363). Removing that dead carnival wiring -- which task 1b does -- would
# otherwise re-arm the sender as a side effect. The explicit switch has to be in
# place before the accidental one is taken out.
#
# Distinct from GLOBAL_PAUSE: pause is an operator brake pulled during an
# incident and expected to be toggled; this is a build-state assertion, flipped
# once, when the new engine is ready to talk to real people.
SEND_ENABLED = _b(os.environ.get("SEND_ENABLED", "false"))

MAX_SENDS_PER_HOUR = int(os.environ.get("MAX_SENDS_PER_HOUR", "30"))
# Rolling-24h cap on PROACTIVE sends (m1/m2/m3) to respect the WhatsApp number's
# messaging tier. New number = 250/day; raise this as Meta bumps the tier
# (250 -> 1K -> 10K). Acks don't count -- they're replies inside an open
# conversation, not new business-initiated conversations.
DAILY_SEND_CAP = int(os.environ.get("DAILY_SEND_CAP", "250"))
GLOBAL_PAUSE_ENV = _b(os.environ.get("GLOBAL_PAUSE", "false"))

# Batch cap per scheduler tick. Keeps one tick bounded and spreads a backlog across
# ticks so /admin/poll-now returns promptly. Send JITTER was removed 2026-07-31: it
# guarded against Wasender's ban-on-burst behaviour, which does not apply to the
# official Cloud API.
SEND_BATCH_PER_TICK = int(os.environ.get("SEND_BATCH_PER_TICK", "10"))

DASH_USER = os.environ.get("DASH_USER", "admin")
DASH_PASS = os.environ.get("DASH_PASS", "change-me")

# HOW FAST A NEW LEAD IS REACHED. Owner 2026-08-02: "can u reduce this to 1 min".
#
# The old chain was Sell.do (10) -> Meta forms (10) -> tick (5), so 5-15 minutes
# before the first template, plus however long Sell.do took to receive the lead.
# Speeding up the tick alone would not have helped: the lead still waited on a
# partner database.
#
# So the fast path stops going through Sell.do. Meta form leads are polled every
# minute and promoted straight into `leads` with their phone already attached, and
# the tick that knocks them also runs every minute. Sell.do stays on a slow poll --
# it is somebody else's database, it is no longer on the critical path, and
# hammering it every minute buys nothing.
SELLDO_POLL_MIN = int(os.environ.get("SELLDO_POLL_MIN", "10"))
META_LEADS_POLL_MIN = int(os.environ.get("META_LEADS_POLL_MIN", "1"))
META_ADS_POLL_MIN = int(os.environ.get("META_ADS_POLL_MIN", "30"))
SEQUENCER_TICK_MIN = int(os.environ.get("SEQUENCER_TICK_MIN", "1"))

IST_OFFSET_HOURS = 5.5
