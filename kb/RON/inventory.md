# RON — what is actually for sale

**Source:** owner-supplied price sheet, 2026-07-31.
**Curated:** prices REMOVED from this file by design — see `curation-rules.md` rule 3.
This file is buyer-facing and is ingested into the corpus. The price figures from the
same sheet live in `pricing-internal.md`, which is **never ingested**.

---

## Sellable configurations

| Type | Size |
|---|---|
| 3 bed villa | 2552 sqft |
| 3 bed villa | 2612 sqft |
| 4 bed villa | 3634 sqft |

Villas run 2552 to 3634 sqft. Villas are the only homes on sale.

## Not currently for sale

The FAQ describes a wider set than the sheet offers. Absent from what is being sold:

- **All apartments** - Compact 2BHK, 2BHK and 3BHK
- 1BHK apartments
- Villaments (2/3/4BHK)
- 5BHK island villas
- Beachfront villas

These belong to later phases or are not released. **A buyer asking for any of them is
not off-category — they are asking for something we do not sell yet.** Escalate to a
human rather than rejecting them; the answer is a sales conversation, not a gate.

### Apartments are the exception to that (owner, 2026-09-02)

Apartments left the list on 2026-09-02: **villas only from here.** They do NOT get the
escalation above, and they must never be treated as off-category:

- Say once, plainly, that we are selling villas only now and what they start from.
- Then carry on. Do **not** call a colleague, and do **not** close the conversation.
- Somebody who cannot reach a villa is nurtured, exactly as anyone below the entry
  price always has been — the bot keeps helping and probes gently for room.

The reasoning is the owner's: the number a buyer gives today is not the number they
will have in two months, and an apartment enquirer is the person most likely to move.
Handing them to sales wastes a salesperson; closing the conversation loses them for
good. See `content/answering-rules.md`, "When their budget is below anything we sell".

## Villa top size — RESOLVED 2026-08-02

The largest villa is **3634 sqft**. The FAQ sheet said 3643, a transposed digit;
the owner confirmed the price sheet is final. Both documents now agree.

Worth remembering how this surfaced: the conflict was spotted when the corpus was
built and written down here as a note saying the bot "should not volunteer the top
of the villa range". Nothing enforced that, because it was prose in a document the
bot only ever reads as retrieved text. The FAQ chunk was retrieved on its own, cited
correctly, and 3643 went to a real buyer. **A caveat recorded next to a fact does
not travel with the fact** — if a figure must not be used, remove or fix the figure.
