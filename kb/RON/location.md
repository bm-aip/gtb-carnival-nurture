<!-- Sibling file: curation-rules.md holds the project-wide answer rules. -->

# RON — Location and distances

**Project:** Republic of Nature · Vadanemmeli, ECR (East Coast Road), Chennai
**Source:** `RON Location (1).docx`, supplied by owner 2026-07-30
**Curated:** 2026-07-30. Owner-approved for buyer-facing use.
**Status:** staging file for the KB ingest (build-plan task 10). Sales owns this content.

---

## Curation applied to the source file

| Source entry | Action | Why |
|---|---|---|
| `Covelong Private Beach — 3.5 KM` | **kept, renamed to "Covelong"** | The words "Private Beach" in the corpus would make the bot answer a beach question in a way that breaches the claims guardrail below. The distance is a true and useful landmark fact; the phrasing was the problem. |
| `Barefoot Bay — 400 M` | **DROPPED entirely** | Beach club at 400 m. Anything this close, described this way, reads to a buyer as the project's own beach access. Owner decision: remove. |

## ⚠️ Claims guardrail — travels with this data, not just with template copy

**Never imply direct access to a natural private beach.** The only approved wording is:

> a planned man-made beach and lagoon experience within the community

No entry in this file may be retrieved to support a claim of private or natural
beach access. Covelong is a nearby *place*, not an amenity of the project.

---

## Distances from the project

| Landmark | Distance |
|---|---|
| Sheraton Grand Chennai Resort | 800 m |
| Madras Crocodile Bank | 900 m |
| Nithya Kalyana Perumal Temple | 2 km |
| Covelong | 3.5 km |
| InterContinental Resort, Chennai | 6 km |
| Shri Munisuvratswami Jain Navgraha Temple | 6.5 km |
| Nemmeli Seawater Desalination Plant | 7 km |
| Boat House, Muttukadu | 8 km |
| Kelambakkam | 8 km |
| MGM Dizzee World | 9 km |
| Thiruporur | 9.5 km |
| Mayajaal Multiplex | 12 km |
| Uthandi Toll Plaza | 13.5 km |
| PVR Heritage | 14.5 km |
| Mahabalipuram | 15 km |
| Neelankarai | 23 km |
| Chennai International Airport | 39 km |
| Central Railway Station | 43 km |

18 entries. Source had 19; Barefoot Bay removed.

---

## What this file does NOT contain

Deliberate, and safe as of the 2026-07-30 positioning decision — RON is a
**weekend-home / resort-style** product, so these are secondary rather than
gating (see `POST-CARNIVAL-DESIGN.md` §2, amended):

- **No schools** — FAQ row 41 blank
- **No hospitals** — FAQ row 41 blank. *Still worth 2–3 names eventually: weekend
  buyers have emergencies, and some of this audience is older.*
- **No offices / IT corridors** — FAQ row 41 blank
- **No metro, current or planned** — FAQ row 42 blank
- **No drive times.** Distances only. The bot must not convert kilometres into
  minutes — a made-up travel time about a location question is exactly the kind of
  invention that loses trust, and ECR traffic varies by season and hour.

Questions on any of the above hit the confidence floor and **escalate to a human
by design** (§10). That is correct behaviour, not a gap.

## Retrieval note

This list is **weekend-buyer material almost in its entirety** — resorts, beaches,
a boat house, temples, Mahabalipuram, a crocodile bank. Once purpose is known to be
a weekend or second home, retrieval should favour it heavily. For a stated
primary-residence buyer it is much weaker evidence, and that buyer is flagged on
the handoff card rather than answered from this file alone.
