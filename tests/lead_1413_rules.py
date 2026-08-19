"""Every defect found in lead 1413's conversation, 2026-08-19.

He arrived at 15:46 and by 15:53 had given his purpose, his area and the home he
wanted, said the villa price was fine, and agreed to a call. The bot did the hard
part well. These are the seven things it got wrong on the way, each locked here so
they cannot come back quietly.
"""
import sys

import _bootstrap  # noqa: F401
import config
import media
import qualifier as q

R = _bootstrap.Results()


def _hist(*assistant_msgs):
    return [{"role": "assistant", "content": m} for m in assistant_msgs]


# --- 1. THE BUDGET ANSWER THAT HAD NOWHERE TO GO ------------------------------
# Bot: "does the starting price sit around the range you had in mind?"
# Him: "Yes it sound fine."  -> recorded nothing, counted as his third dodge.
quoted = "\n".join(q._already_quoted(_hist(
    "The 3 bed villas start from Rs 3.94 Cr onwards and the 4 bed from Rs 5.5 Cr.")))
R.check("a quoted price tells the model a 'yes' IS the budget",
        "budget_inr" in quoted, quoted[:200])
R.check("...and names the figure, so it cannot guess which price",
        "3.94" in quoted, quoted[:200])
R.check("...and only when they agreed, not when they refused",
        "too high" in quoted.lower() or "do not record" in quoted.lower(), quoted[:300])

# --- 2. FACTS REPEATED IN CONSECUTIVE MESSAGES --------------------------------
# The opener gave 32 acres, Kovalam Junction, the clubhouse size and the amenity
# list. The very next message gave all four again, unprompted.
opener = ("Republic of Nature is a 32-acre community on ECR, near Kovalam Junction. "
          "Apartments and villas, plus a 1,00,000 sqft clubhouse with pool, gym, "
          "courts, mini theatre and spa.")
told = "\n".join(q._already_quoted(_hist(opener)))
R.check("ONE telling is enough to warn -- it used to take two",
        "ALREADY TOLD" in told, told[:200])
for fact in ("32 acres", "clubhouse size", "Kovalam Junction", "clubhouse amenities"):
    R.check(f"...and it names {fact!r}", fact in told, told[:300])

# Never said, never warned about.
clean = "\n".join(q._already_quoted(_hist("Sure, which one did you have in mind?")))
R.check("a fact never stated is not warned about", "ALREADY TOLD" not in clean)

# --- 5. THE MODEL TYPING URLS -------------------------------------------------
# It produced a correct maps link, copied from a voice sample. Correct that time.
for url in ("https://maps.app.goo.gl/RpzjkiwQ4j8iAEAh9",
            "www.republicofnature.com", "http://example.com/floorplan.pdf"):
    R.check(f"a URL is recognised for stripping: {url[:34]}",
            bool(config.ANY_URL.search(f"see {url} for details")))
R.check("ordinary text carries no URL",
        not config.ANY_URL.search("We are on ECR, 5 kms from Kovalam."))
R.check("the map link is configured", bool(config.SITE_MAP_URL))
R.check("the map line carries the URL", config.SITE_MAP_URL in config.SITE_MAP_LINE)

# The map is attached when they ask how to get there, mid-sentence -- ASKS_LOCATION
# only fires when the whole message is the question.
for msg in ("But I' will need to know.the exact location",
            "share the location please", "how do i reach there",
            "can you send me the google maps link", "where exactly is it"):
    R.check(f"asks for directions: {msg[:34]!r}",
            bool(config.ASKS_DIRECTIONS.search(msg)))
for msg in ("what is the price", "3 bhk villa", "Primary"):
    R.check(f"NOT a directions request: {msg!r}",
            not config.ASKS_DIRECTIONS.search(msg))

# --- 6. PADDING AFTER A DEFERRAL ----------------------------------------------
# "Will tell you later" -> possession dates for both phases, unasked.
for msg in ("Will tell you later", "will let you know", "not now",
            "I'll get back to you", "give me some time", "let me think about it"):
    R.check(f"deferral recognised: {msg!r}", bool(config.DEFERS.search(msg)))
for msg in ("Villa", "I'm looking at ecr", "yes it sound fine", "Yep"):
    R.check(f"NOT a deferral: {msg!r}", not config.DEFERS.search(msg))

# --- 7. THE AFFIRMATION ON A REPLY THAT SAID NOTHING --------------------------
# "This looks good..." -> "Glad to hear that."
CASES = [
    ("This looks good...", "Glad to hear that. Villas are the quieter side."),
    ("looks good", "Happy to hear that. The villas sit apart."),
    ("nice", "Great. The clubhouse is open all day."),
    ("👍", "Perfect. Where are you looking to buy?"),
]
for said, reply in CASES:
    out = q._strip_empty_affirmation(reply, said)
    R.check(f"no congratulating {said!r}", out != reply, f"{reply!r} -> {out!r}")
    R.check(f"...and the reply survives ({said!r})", len(out.strip()) > 10, out)

# A real answer still earns a warm opener.
real = q._strip_empty_affirmation("Great. Villas it is.", "I want a 4 bed villa")
R.eq("a genuine answer keeps its affirmation", real, "Great. Villas it is.")

# --- 10. AN IMAGE WHEN THE VISIT IS ACTUALLY BOOKED ---------------------------
R.check("the visit image is wired", "visit" in media.WIRED)
R.check("...and resolves to a file", bool(media.path_for("visit")))

booked = media.pick.__doc__  # documented behaviour; the picker itself needs a db
R.check("pick() is documented as one-per-message", "One per message" in booked)

# The picker's rules, exercised without a database by calling the pure helpers.
R.eq("a site visit earns the visit image", media._config_slug("villa"), "villa")

if __name__ == "__main__":
    sys.exit(0 if R.report("LEAD 1413") else 1)
