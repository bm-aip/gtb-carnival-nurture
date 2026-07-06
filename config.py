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

WASENDER_API_KEY = os.environ["WASENDER_API_KEY"]
WASENDER_SESSION_ID = os.environ.get("WASENDER_SESSION_ID", "")
WASENDER_BASE = "https://wasenderapi.com/api"
WASENDER_WEBHOOK_SECRET = os.environ.get("WASENDER_WEBHOOK_SECRET", "")

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

MAX_SENDS_PER_HOUR = int(os.environ.get("MAX_SENDS_PER_HOUR", "30"))
GLOBAL_PAUSE_ENV = _b(os.environ.get("GLOBAL_PAUSE", "false"))

DASH_USER = os.environ.get("DASH_USER", "admin")
DASH_PASS = os.environ.get("DASH_PASS", "change-me")

SELLDO_POLL_MIN = int(os.environ.get("SELLDO_POLL_MIN", "10"))
META_ADS_POLL_MIN = int(os.environ.get("META_ADS_POLL_MIN", "30"))
SEQUENCER_TICK_MIN = int(os.environ.get("SEQUENCER_TICK_MIN", "5"))

IST_OFFSET_HOURS = 5.5
