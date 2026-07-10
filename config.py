import os
from datetime import date

def _b(v): return str(v).lower() in ("1", "true", "yes", "on")

DATABASE_URL = os.environ["DATABASE_URL"]

SELLDO = {
    "RON": {
        "db_url": os.environ["SELLDO_DB_URL_RON"],
        "project": "Republic Of Nature",
        "campaign": "RON_Carnival",
    },
    "ELEMENTS": {
        "db_url": os.environ["SELLDO_DB_URL_ELEMENTS"],
        "project": "Elements Common",
        "campaign": "Meta",
    },
}

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

# Template names as approved in Wati. Defaults match the copy handed to the
# owner; override per-env if the approved names differ -- no code change needed.
WATI_TEMPLATES = {
    # Defaults = the correctly-variable-tagged templates owner confirmed in Wati
    # (the *_final / *_1 versions). Override via env only if a different template
    # gets approved instead.
    "m1_ron":      os.environ.get("WATI_TPL_M1_RON", "gtb_m1_ron_final"),
    "m1_elements": os.environ.get("WATI_TPL_M1_ELEMENTS", "gtb_m1_elements_1"),
    "m2":          os.environ.get("WATI_TPL_M2", "gtb_m2_followup_final"),
    "m3":          os.environ.get("WATI_TPL_M3", "gtb_m3_reminder"),
    "m3_generic":  os.environ.get("WATI_TPL_M3_GENERIC", "gtb_m3_generic"),
    "ack":         os.environ.get("WATI_TPL_ACK", "gtb_ack"),
}

EVENT_NAME = os.environ.get("EVENT_NAME", "GTB Carnival")
EVENT_VENUE = os.environ.get("EVENT_VENUE", "GTB Lounge, EA Mall, Chennai")
EVENT_MAPS_LINK = os.environ.get("EVENT_MAPS_LINK", "")
EVENT_TIMING = os.environ.get("EVENT_TIMING", "All day")
EVENT_DATES = [date.fromisoformat(d) for d in
               os.environ.get("EVENT_DATES", "2026-07-10,2026-07-11,2026-07-12").split(",")]

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

MAX_SENDS_PER_HOUR = int(os.environ.get("MAX_SENDS_PER_HOUR", "30"))
# Rolling-24h cap on PROACTIVE sends (m1/m2/m3) to respect the WhatsApp number's
# messaging tier. New number = 250/day; raise this as Meta bumps the tier
# (250 -> 1K -> 10K). Acks don't count -- they're replies inside an open
# conversation, not new business-initiated conversations.
DAILY_SEND_CAP = int(os.environ.get("DAILY_SEND_CAP", "250"))
GLOBAL_PAUSE_ENV = _b(os.environ.get("GLOBAL_PAUSE", "false"))

# Anti-bulk-blast pacing (Balanced profile). Random pause between each bulk
# outbound send; batch cap keeps a single tick bounded and spreads sends across
# ticks. Acks (1:1 replies) are never jittered.
SEND_JITTER_MIN_SEC = float(os.environ.get("SEND_JITTER_MIN_SEC", "5"))
SEND_JITTER_MAX_SEC = float(os.environ.get("SEND_JITTER_MAX_SEC", "20"))
SEND_BATCH_PER_TICK = int(os.environ.get("SEND_BATCH_PER_TICK", "10"))

DASH_USER = os.environ.get("DASH_USER", "admin")
DASH_PASS = os.environ.get("DASH_PASS", "change-me")

SELLDO_POLL_MIN = int(os.environ.get("SELLDO_POLL_MIN", "10"))
META_ADS_POLL_MIN = int(os.environ.get("META_ADS_POLL_MIN", "30"))
SEQUENCER_TICK_MIN = int(os.environ.get("SEQUENCER_TICK_MIN", "5"))

IST_OFFSET_HOURS = 5.5
