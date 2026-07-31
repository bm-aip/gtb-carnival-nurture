# RON — what is actually for sale

**Source:** owner-supplied price sheet, 2026-07-31.
**Curated:** prices REMOVED from this file by design — see `curation-rules.md` rule 3.
This file is buyer-facing and is ingested into the corpus. The price figures from the
same sheet live in `pricing-internal.md`, which is **never ingested**.

---

## Sellable configurations

| Type | Size |
|---|---|
| Compact 2BHK apartment | 1220 sqft |
| Compact 2BHK apartment | 1250 sqft |
| 2BHK apartment | 1422 sqft |
| 3BHK apartment | 2030 sqft |
| 3BHK apartment | 2133 sqft |
| 3 bed villa | 2552 sqft |
| 3 bed villa | 2612 sqft |
| 4 bed villa | 3634 sqft |

Apartments run 1220–2133 sqft. Villas run 2552–3634 sqft.

## "Compact 2BHK" is a real, separate product

Owner-confirmed 2026-07-31: **C2BHK means Compact 2BHK.** It is *not* a synonym for
2BHK — the two appear side by side on the price sheet at different sizes, 1220/1250
against 1422 sqft.

The bot says **"Compact 2BHK"**. It never says "C2BHK", which is internal shorthand no
buyer would recognise.

**Why this matters more than vocabulary:** an earlier curation rule rewrote C2BHK to
2BHK, which silently merged two products about 200 sqft apart. A buyer asking for a
2BHK and being shown a compact one finds out at the site visit. Corrected.

## Not currently for sale

The FAQ describes a wider set than the sheet offers. Absent from what is being sold:

- 1BHK apartments
- Villaments (2/3/4BHK)
- 5BHK island villas
- Beachfront villas

These belong to later phases or are not released. **A buyer asking for any of them is
not off-category — they are asking for something we do not sell yet.** Escalate to a
human rather than rejecting them; the answer is a sales conversation, not a gate.

## Known discrepancy, unresolved

The price sheet gives the largest villa as **3634 sqft**; FAQ row 8 says **3643 sqft**.
Likely a transposed digit. Nobody has confirmed which is right, so the bot should not
volunteer the top of the villa range as an exact figure.
