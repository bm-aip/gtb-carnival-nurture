# RON bot — questions that still need real answers

**For:** whoever owns the FAQ content (Bhargavi / KK / sales)
**From:** the WhatsApp qualification bot build
**Date:** 2026-07-31

The bot now answers **64 questions** from the FAQ. **46 rows could not be used.**

Nothing here is a criticism of the FAQ — it was written as an internal working
document, staff answering staff. That is exactly what it should have been. It just
means a chunk of it can't be read aloud to a customer without a rewrite.

**Every question below currently escalates to a human.** So each one you fix is a
customer answered in seconds instead of waiting for a callback.

---

## GROUP 1 — Quick rewrites. The answer already exists, it just reads like an email.

**These are the cheapest wins. Most are a one-line edit.** In several cases the real
fact is sitting inside the answer with staff conversation wrapped around it.

| # | Question | What's in the FAQ today | What's needed |
|---|---|---|---|
| 30 | Piles — how many, what depth? | *"Is this Question really necessary - We have isolated footings - not Pile Foundation"* | Just: **"Isolated footings, not a pile foundation."** Delete the first six words. |
| 111 | What % of land is green? | *"Already answered in Question 4th"* | Copy the real answer from Q4: **55% plot coverage, 25% green cover.** |
| 92 | Ceiling height? | *"Already Given"* | Copy the real answer from Q17. |
| ~~44~~ | ~~What buildings are nearby?~~ | — | **RESOLVED by your location file** — the bot now answers with Sheraton 800 m, crocodile bank 900 m and the rest |
| 26 | Any commercial plans? | *"There is no such plans from the firm on this, If then we will updated"* | **"No commercial component is planned."** |
| 49 | Rainwater harvesting? | *"Yes we are doing - exact system to be understood via MEP - Rainwater from the roofs..."* | Keep the real part, drop "to be understood via MEP". |
| 39 | How many lounges? | *"As in the spec email we have one lounge and library in Phase 1 & 2..."* | Same — drop "as in the spec email", keep the facts. |
| 74 | Groundwater TDS? | *"Not to be disclosed, We are taking measure to treat the water and make it potable."* | Drop "not to be disclosed" — the rest is a good answer. **Is there a reason we don't state TDS?** |
| 56 | Where are the gas stations? | *"Not Applicable"* | **"No piped gas connection is planned."** |
| 75 | Solar panels and capacity? | Real answer, but trails off into another question | Finish the sentence. **What capacity?** |
| 67 | Car parking? | Phase 1 & 2 given; **Phase 3 is blank** | Phase 3 parking numbers. |
| 79 | Party area? | *"Phase 2 - has a comm"* — cut off | Finish it. |
| 34 | Lagoon depth? | *"Awaiting details from Fluidra. Tentative depth 1200mm"* | Confirm with Fluidra, then state it. |

---

## GROUP 2 — Genuinely missing. Someone has to find these out.

### The ones buyers ask most

| # | Question | Note |
|---|---|---|
| 37 | **The full amenities list** | *"Shared on email"*. This is one of the most-asked questions in the entire funnel and the bot currently has nothing. **Highest priority on this page.** |
| 23 | **Full specification of apartments and villas** | *"Refer to the spec sheets shared on email"*. Second most valuable. If the spec sheet can be shared, most of Group 2 is solved at once. |
| 25 | **RERA numbers, phase-wise** | Currently points at the price sheet. RERA numbers are public and buyers do ask. Should be easy. |
| 103 | What concierge services? | Answer is currently just *"Yes"* — which tells a buyer nothing. Need the actual list. |
| 101 | Business centre, and capacity? | *"Covered in the Amenities List"* — need the list (see Q37). |

### Location — what the file you sent already covers, and what it doesn't

**Already answered, no action needed.** The location file handles *"what's near the
project"* well — Sheraton 800 m, crocodile bank 900 m, Mahabalipuram 15 km — and
covers **public transport** via Central Station (43 km) and the airport (39 km).
It also answers Q44 "nearby buildings" far better than the old *"surrounded by land"*.

**Still genuinely absent:**

| # | Question | Note |
|---|---|---|
| 41 | Named **schools** and **hospitals** with distances | The file has neither |
| 41 | IT corridors / office campuses | Kelambakkam (8 km) and Thiruporur (9.5 km) are on that belt, but the file doesn't say so and the bot must not infer it |
| 42 | Metro — existing or upcoming, and how honestly "upcoming" may be described | |

⚠️ **Why these matter more than their priority suggests.** Asked *"which schools are
nearby?"*, the bot retrieves the location list — correctly, it's the closest thing it
has — but that list contains no school. Anything answering from it could invent a
plausible-sounding school near ECR. The chunk now carries an explicit instruction to
escalate rather than guess, but **two or three real names would remove the risk
entirely.** Hospitals especially: emergencies, and some of this audience is older.

### Specification detail

| # | Question | Currently |
|---|---|---|
| 57 | Lift brands | *"Refer Spec sheet"* |
| 58 | Lift capacity (RL / RSL / RFL) | *"Question not understood"* |
| 62 | Civil / wood / plumbing warranty — how many years? | *"Not sure of this"* |
| 86 | Kitchen sink type | *"Refer Spec"* |
| 96 | Bollard system? | *"Cant understand j"* |
| 54 | Eco-friendly waste disposal | *"To be Discussed"* |
| 78 | Design life of the building | blank |
| 106 | Laundry space in every unit? | blank |
| 98 | How many borewells? | *"Not to be answered"* — **is that deliberate?** If yes, fine, it stays an escalation. |
| 69 | STC-rated walls in row villas? | *"Even Bhargavi could not understand"* — the question may need rewording before it can be answered |

---

## GROUP 3 — Not for sourcing. These are decisions for you.

The bot deliberately says nothing on these. Each is a business or legal call, not a
missing fact.

| # | Topic | The decision |
|---|---|---|
| 22, 110 | **Handover / possession dates** | You've said don't discuss. Recorded and enforced. |
| 16, 20, 93, 9 | **Taj membership cost · rental assurance · customisation charges · UDS** | All contain commercial commitments. Currently silent. |
| 15, 21 | **ROI and resale value** | *"KK to come up with an answer"*. These are investment claims — worth a considered position, or a permanent decision to always route to a human. |
| — | **Whether the bot may ever state a price** | Your commercial rule says no. On a ₹1.28–5.5 crore product this is one of the first three questions everyone asks, so it is the single biggest driver of escalation volume. |

---

## Two rows that should just be deleted

| # | Question | Why |
|---|---|---|
| 19 | *"How many projects are we entertaining?"* | Nobody understood it, including the person answering. Not a buyer question. |
| — | *"doors ?"* | A fragment with no answer. |

---

## If you only do three things

1. **Share the spec sheet and the amenities list** (Q23, Q37). Between them they answer
   the most-asked questions and probably unlock six or seven rows in Group 2.
2. **Spend an hour on Group 1.** Thirteen rows, mostly one-line edits, and the facts are
   already written down.
3. **Decide the price question.** Not a fact to source — a position to take. It governs
   how much human time the bot saves you.
