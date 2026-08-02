import os

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

# Appended in code rather than in the RON_CAMPAIGNS default so that overriding the
# campaign list in Railway can never silently strip it and mute every walk-up.
if DIRECT_INBOUND_ENABLED and DIRECT_INBOUND_PROJECT in SELLDO:
    SELLDO[DIRECT_INBOUND_PROJECT]["campaigns"].append(DIRECT_INBOUND_CAMPAIGN)


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
KNOCK_TEMPLATES = {
    "t1_lifestyle":   os.environ.get("WATI_TPL_T1", "ron_nurture_01_lifestyle"),
    "t2_location":    os.environ.get("WATI_TPL_T2", "ron_nurture_02_location"),
    "t3_low_density": os.environ.get("WATI_TPL_T3", "ron_nurture_03_low_density"),
    "t6_visit":       os.environ.get("WATI_TPL_T6", "ron_nurture_06_visit"),
}

# Ghost re-opener (task 18/19). Someone who talked and then went quiet cannot be
# sent the COLD sequence -- those templates introduce the project from scratch to a
# person who already told us their budget, which reads as broken at exactly the
# moment they are most likely to leave.
#
# ⚠️ PENDING approval as of 2026-07-31, and submitted as MARKETING rather than
# UTILITY. Marketing carries the category gate that blocked ~44% of the carnival's
# cold sends, so re-opener delivery will be worse than it needs to be. Worth
# resubmitting as utility later -- it continues a conversation the customer started.
REOPENER_TEMPLATE = os.environ.get("WATI_TPL_T7", "t7_reopener")

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
PROMOTE_FORMS = [f.strip() for f in os.environ.get(
    "PROMOTE_FORMS",
    "GTB_Carnival_RON_2BHK_BM,"
    "GTB_Carnival_RON_3BHK_BM,"
    "GTB_Carnival_RON_3BHK_Villa BM,"
    "GTB_Carnival_RON_4BHK_Villa BM,"
    "GTB_Carnival_RON_Villa_BM,"
    "Elements Carnival,"
    "Elements Carnival - E4 New,"
    "Elements- 3 Carnival"
).split(",") if f.strip()]
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
VISIT_VENUES = {
    "site": {
        # Owner 2026-08-01: never say "Vadanemmeli" to a buyer -- it does not help
        # the positioning and nobody knows where it is. qualifier._rename_locality
        # enforces this on every outbound reply; this is the phrasing it uses.
        "name": "the site on ECR, near Kovalam Junction",
        "priority": 1,
        "offer": "always",
    },
    "experience_centre": {
        "name": "the Experience Centre at Express Avenue mall",
        "priority": 2,
        # ONLY after the buyer raises distance or travel as a concern.
        "offer": "on_distance_objection",
        "note": ("Has a miniature model and a walkthrough of the RON experience. "
                 "Same day availability rules as the site. Position it as a first "
                 "look during the week, with the site visit at the weekend -- never "
                 "as a replacement for seeing the site."),
    },
}

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
        "because a weekend home and a full-time home are very different picks here",
        "so I don't waste your time on the wrong side of the project",
    ],
    "location": [
        "so I can tell you honestly whether this stretch of ECR works for you",
        "because the drive matters differently depending on where you're coming from",
        "so I can be straight with you about whether we're the right fit",
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
# Only the FLOOR rejects. The ceiling is a signal for sales, not a filter: somebody
# with more money than the top unit is a good problem, not an unqualified lead.
BUDGET_FLOOR = int(os.environ.get("BUDGET_FLOOR", "12800000"))      # ₹1.28 cr
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

SELLDO_POLL_MIN = int(os.environ.get("SELLDO_POLL_MIN", "10"))
META_ADS_POLL_MIN = int(os.environ.get("META_ADS_POLL_MIN", "30"))
SEQUENCER_TICK_MIN = int(os.environ.get("SEQUENCER_TICK_MIN", "5"))

IST_OFFSET_HOURS = 5.5
