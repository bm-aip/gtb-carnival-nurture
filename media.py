"""The image library: which picture belongs to which moment, and its caption.

WHY A FIXED TABLE AND NOT A CHOICE
----------------------------------
An image is a claim, made as loudly as a sentence and with nothing for a text guard
to read. The claims guardrail forbids implying a private natural beach; a model free
to pick pictures would eventually pick sand and water and make that claim silently.
So the model never chooses. Every image hangs off a fact the bot has ALREADY written
into the checklist, and the mapping lives here in code.

WHY THE FILES ARE IN THE REPO
-----------------------------
`wati.send_file` uploads the bytes. No bucket to keep alive, no link to rot, no
second place for the set to drift out of step with the code that references it.
~6MB for fourteen images.

NAMES DESCRIBE THE PICTURE, AND THE PREFIX IS PROVENANCE
--------------------------------------------------------
`ron_photo_*` is a photograph. `ron_render_*` is CGI, because the apartments are not
built and nothing photographable exists -- so no render may ever be captioned as the
site as it stands today.

The originals were named by marketing theme and it did not survive contact:
`smart-investment` was a family in a living room, `quality-of-life` a pond with
swans. Wiring by those names would have sent an investment buyer a toddler and a
dog. Three more were mislabelled `render` while being renamed, from those same old
names, and turned out to be photographs. Look at the file, never the label.
"""
import os
import re

import db

MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "content", "media")

# How a picture is recorded in message_log. Its own msg_type so it never pollutes
# the conversation history the model is shown -- a line reading "hero" in the
# transcript would be read as something the bot said.
MSG_TYPE = "media"

# slug -> (filename, caption)
#
# CAPTIONS TRAVEL AS A URL QUERY PARAMETER (see wati.send_file), which is the path
# that mangled a rupee sign on 2026-08-05. Keep them plain; write money as "Rs".
# Keep them short: the caption sits under the picture, it is not the message.
LIBRARY = {
    # --- the opener. One image, the best one, at the moment of most attention.
    "hero": ("ron_photo_living_doubleheight.jpg",
             "A little of what it feels like inside."),

    # --- purpose. This is what the purpose question is FOR: an investment buyer
    #     and a weekend buyer are not sold the same thing, so they should not be
    #     shown the same thing either.
    "purpose_weekend": ("ron_photo_terrace_couple.jpg",
                        "Weekends here look a bit like this."),
    "purpose_primary": ("ron_photo_bedroom.jpg",
                        "Morning light in one of the bedrooms."),
    "purpose_investment": ("ron_photo_homes_goldenhour.jpg",
                           "The homes, late afternoon."),

    # --- configuration. He has told us what he wants; show him that, not the other.
    "villa": ("ron_photo_villa_row.jpg",
              "The villas."),
    "apartment": ("ron_render_apartment_courtyard.jpg",
                  "The apartment courtyards. A render - they are still being built."),

    # --- held back deliberately. Available to send by hand from /admin/test-media,
    #     not wired to any trigger yet.
    "villa_exterior": ("ron_photo_villa_exterior.jpg", "Evening, by the garden."),
    "balcony_couple": ("ron_photo_balcony_couple.jpg", "One of the balconies."),
    "balcony_coffee": ("ron_photo_balcony_coffee.jpg", "Coffee outside."),
    "terrace_cat": ("ron_photo_terrace_cat.jpg", "A quiet corner."),
    "apartment_greens": ("ron_render_apartment_greens.jpg",
                         "The apartments, as designed. A render."),
    "apartment_family": ("ron_render_apartment_family.jpg",
                         "The apartments, as designed. A render."),
    "living_room": ("ron_render_living_room_family.jpg",
                    "Inside a home, as designed. A render."),
    # NOT wired and worth a second look before it ever is: it shows a body of water,
    # and we are not allowed to imply a private natural beach.
    "water": ("ron_render_water_family.jpg",
              "The water inside the community. A render."),
}

SLUGS = set(LIBRARY)

# What actually fires today. Everything else in LIBRARY is reachable only by hand.
WIRED = ("hero", "purpose_weekend", "purpose_primary", "purpose_investment",
         "villa", "apartment")


def path_for(slug):
    """Absolute path, or None if the slug is unknown or the file is missing.

    None is a normal answer, not an error: the caller sends its text reply anyway.
    The failure mode of this whole feature is "no picture", never "wrong picture"
    and never "no reply".
    """
    entry = LIBRARY.get(slug)
    if not entry:
        return None
    p = os.path.join(MEDIA_DIR, entry[0])
    return p if os.path.exists(p) else None


def caption_for(slug):
    entry = LIBRARY.get(slug)
    return entry[1] if entry else None


# --- reading the buyer's own words back ---------------------------------------
#
# `purpose` is stored as the model wrote it down -- "weekend getaway", "for my
# parents to live in", "purely investment" -- so it has to be read, not looked up.
# Order matters: the first pattern that matches wins.
#
# This is a THREE-WAY sort with a safe default of nothing, not a guess. An
# unrecognised purpose sends no image at all, which is the right outcome: a buyer
# who said something we did not understand should not be shown a picture chosen on
# a coin flip.
_PURPOSE = (
    ("purpose_investment",
     re.compile(r"invest|appreciat|rental|yield|portfolio|resale|returns?\b", re.I)),
    ("purpose_weekend",
     re.compile(r"weekend|holiday|vacation|getaway|retreat|second home|"
                r"farm ?house|leisure", re.I)),
    ("purpose_primary",
     re.compile(r"primary|permanent|full.?time|end.?use|shift|relocat|"
                r"live (there|here|in)|stay|family home|own use|retire", re.I)),
)


def _purpose_slug(value):
    for slug, pattern in _PURPOSE:
        if pattern.search(str(value or "")):
            return slug
    return None


def _config_slug(value):
    """Villa or apartment. Anything naming both, or neither, sends nothing."""
    v = str(value or "").lower()
    villa, apt = "villa" in v, ("apartment" in v or "flat" in v)
    if villa and not apt:
        return "villa"
    if apt and not villa:
        return "apartment"
    return None


def already_sent(lead_id):
    """Slugs this lead has already been shown.

    Read from message_log rather than a new column: the log is already the record
    of what reached this person, and one source of truth beats two that can drift.

    The knock videos are a SEPARATE set of files (Wati_01-04.mp4, ron.jpg) that
    Wati delivers as template headers, so nothing here can collide with them. If an
    answer image is ever added that also appears in a knock template, dedup has to
    span both -- a buyer receiving the same film twice by two different routes reads
    as broken.
    """
    rows = db.q("""SELECT body FROM message_log
                   WHERE lead_id=%s AND msg_type=%s""",
                (lead_id, MSG_TYPE)) or []
    return {r["body"] for r in rows}


def _first_reply(lead_id):
    """Is the message we just sent this lead's FIRST answer from the bot?

    Counted from the log, after the send, so exactly one row means this was it.
    Deliberately not "have we sent the hero yet" -- that would fire the opener image
    halfway down a conversation if an early turn had failed, where it makes no sense.
    """
    r = db.q("""SELECT count(*) AS n FROM message_log
                WHERE lead_id=%s AND direction='out' AND msg_type='qualifier_turn'""",
             (lead_id,), one=True)
    return (r or {}).get("n") == 1


def pick(lead_id, before, after):
    """Which image this turn has earned, or None. One per message, never repeated.

    Every candidate comes from something the bot has ALREADY written into the
    checklist -- the model is not asked, and cannot be. `before` and `after` are the
    checklist either side of this turn, so an image fires on the turn a fact is
    LEARNED, never again afterwards.

    None is the ordinary answer. Most turns carry no picture, and that is the point:
    three small lifts across a conversation, not a slideshow.
    """
    before, after = before or {}, after or {}
    candidates = []

    if _first_reply(lead_id):
        candidates.append("hero")

    # Fires only on the turn the fact arrives -- `not before.get(...)`.
    if not before.get("purpose") and after.get("purpose"):
        candidates.append(_purpose_slug(after["purpose"]))
    if not before.get("configuration") and after.get("configuration"):
        candidates.append(_config_slug(after["configuration"]))

    sent = already_sent(lead_id)
    for slug in candidates:
        if slug and slug in WIRED and slug not in sent and path_for(slug):
            return slug
    return None


def record(lead_id, slug, ok, detail=None):
    """Note that this lead has been shown this image. Failures are logged too --
    a picture that did not send must not be silently retried on a later turn."""
    db.log_msg(lead_id, "out", MSG_TYPE, slug, ok=ok,
               detail=detail[:400] if detail else None)


def stats():
    """Every slug, whether its file is actually there, and its size."""
    out = []
    for slug, (fn, caption) in sorted(LIBRARY.items()):
        p = os.path.join(MEDIA_DIR, fn)
        exists = os.path.exists(p)
        out.append({
            "slug": slug,
            "file": fn,
            "kind": "photo" if fn.startswith("ron_photo_") else "render",
            "wired": slug in WIRED,
            "caption": caption,
            "exists": exists,
            "kb": round(os.path.getsize(p) / 1024) if exists else None,
        })
    missing = [r["slug"] for r in out if not r["exists"]]
    return {"dir": MEDIA_DIR, "count": len(out), "wired": list(WIRED),
            "missing": missing, "media": out}
