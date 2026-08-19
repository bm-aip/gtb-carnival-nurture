"""The image library: every wired slug must have a real, sendable file.

An image is a claim made with nothing for a text guard to read, so the mapping is
fixed in code and the model never chooses. These assertions guard the table itself.
"""
import os
import sys

import _bootstrap  # noqa: F401
import media

R = _bootstrap.Results()

# WhatsApp refuses an image over 5MB. Five of the originals were over it -- the
# largest was 9.9MB -- and would simply have failed to send.
MAX_KB = 5 * 1024

s = media.stats()

R.eq("no wired slug is missing its file", s["missing"], [])

for row in s["media"]:
    if row["exists"]:
        R.check(f"{row['slug']}: under WhatsApp's 5MB cap ({row['kb']}K)",
                row["kb"] <= MAX_KB, f"{row['file']} is {row['kb']}K")

for slug in media.WIRED:
    R.check(f"wired slug {slug!r} resolves to a file", bool(media.path_for(slug)))
    cap = media.caption_for(slug)
    R.check(f"wired slug {slug!r} has a caption", bool(cap))
    # Captions ride a URL query parameter (see wati.send_file). 2026-08-05 a rupee
    # sign went out mangled on exactly that path, which is why money is "Rs".
    R.check(f"{slug!r} caption has no rupee sign", "₹" not in (cap or ""))
    R.check(f"{slug!r} caption stays short", len(cap or "") <= 90, cap)

# PROVENANCE. The apartments are not built, so their images are CGI. A render may
# never be captioned as the site as it stands today.
for row in s["media"]:
    if row["kind"] == "render" and row["wired"]:
        R.check(f"wired render {row['slug']!r} admits it is a render",
                "render" in (row["caption"] or "").lower(), row["caption"])

# An unknown slug is a normal answer, not a crash: the text reply still goes out.
R.check("an unknown slug resolves to None", media.path_for("no_such_thing") is None)
R.check("an empty slug resolves to None", media.path_for("") is None)
R.check("a None slug resolves to None", media.path_for(None) is None)

# The one held back on purpose: it shows water, and we may not imply a private
# natural beach. If this ever fails, someone wired it without that conversation.
R.check("the water image is NOT wired", "water" not in media.WIRED)

# Every file in the folder should be reachable, or it is dead weight in the repo.
if os.path.isdir(media.MEDIA_DIR):
    on_disk = {f for f in os.listdir(media.MEDIA_DIR) if f.endswith(".jpg")}
    referenced = {fn for fn, _ in media.LIBRARY.values()}
    R.eq("no file sits in the folder unreferenced", sorted(on_disk - referenced), [])

# --- reading the buyer's stated purpose ---------------------------------------
# Stored as the model wrote it, so it is read rather than looked up. An
# unrecognised purpose must send NOTHING -- never a picture chosen on a coin flip.
PURPOSE_CASES = [
    ("investment", "purpose_investment"),
    ("purely an investment", "purpose_investment"),
    ("for rental income", "purpose_investment"),
    ("looking at appreciation", "purpose_investment"),
    ("weekend home", "purpose_weekend"),
    ("weekend getaway", "purpose_weekend"),
    ("holiday home", "purpose_weekend"),
    ("second home", "purpose_weekend"),
    ("primary home", "purpose_primary"),
    ("primary residence", "purpose_primary"),
    ("to live there full time", "purpose_primary"),
    ("shifting from Bangalore", "purpose_primary"),
    ("for my parents to retire", "purpose_primary"),
    # Not understood -> no image.
    ("", None),
    (None, None),
    ("not sure yet", None),
    ("maybe", None),
]
for value, want in PURPOSE_CASES:
    got = media._purpose_slug(value)
    R.eq(f"purpose {value!r} -> {want}", got, want)

# --- villa or apartment -------------------------------------------------------
CONFIG_CASES = [
    ("villa", "villa"), ("3 bed villa", "villa"), ("4BHK Villa", "villa"),
    ("apartment", "apartment"), ("2BHK apartment", "apartment"),
    ("3 bhk flat", "apartment"),
    # Both named, or neither: send nothing rather than pick.
    ("villa or apartment", None), ("apartments and villas", None),
    ("3 bhk", None), ("", None), (None, None),
]
for value, want in CONFIG_CASES:
    got = media._config_slug(value)
    R.eq(f"configuration {value!r} -> {want}", got, want)

# The apartment image is the one wired render, so a buyer who says "apartment" is
# shown CGI. Its caption has to say so -- checked above, restated here because this
# is the pairing that would mislead someone.
R.check("the apartment slug is a render and admits it",
        "render" in (media.caption_for("apartment") or "").lower())
R.check("the villa slug is a real photograph",
        media.LIBRARY["villa"][0].startswith("ron_photo_"))

if __name__ == "__main__":
    sys.exit(0 if R.report("MEDIA RULES") else 1)
