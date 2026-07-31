# RON — curation rules for the knowledge base

Owner-approved rules that apply to every answer the bot gives for Republic of
Nature. These are enforced mechanically (build-plan tasks 9, 10, 25), not left to
the language model's discretion.

**Last updated:** 2026-07-30

---

## 1. Handover and possession dates — NEVER stated

**Owner decision, 2026-07-30: "don't talk about handover dates."**

FAQ row 22 contains them (*Phase 1 Handover Dec 2027, Phase 2 Jun 2028*). They are
**excluded from the corpus** and any question about handover, possession, completion
or "when can I move in" **escalates to a human**.

This resolves what was a standing contradiction: the FAQ held the dates while the
commercial guardrail forbade possession claims. The bot now has one rule instead of
two that disagree, and it cannot state a date it does not hold.

**Ingest action:** FAQ row 22 → excluded. Add handover/possession to the escalation
trigger list.

---

## 2. Configuration vocabulary — "Compact 2BHK", never "C2BHK"

**RESOLVED 2026-07-31** by the owner's price sheet and the confirmation *"C2bhk means
compact 2 bhk."*

`C2BHK` is internal shorthand appearing once in the 111-row FAQ (row 6). It is never
spoken. But it is **not** a synonym for 2BHK:

| Source term | Bot says | Size |
|---|---|---|
| `C2BHK` | **Compact 2BHK** | 1220 / 1250 sqft |
| `2BHK` | 2BHK | 1422 sqft |

They are separate products, ~200 sqft and ~₹18 lakh apart.

**A correction worth remembering.** The rule here previously rewrote C2BHK → "2BHK",
which merged the two. A buyer asking for a 2BHK could have been shown a compact one
and discovered it at the site visit. The price sheet is what exposed it: the two are
listed side by side at different sizes.

That mistake also spawned a workaround — a de-duplicator, because the merge produced
"Apts 2BHK, 2BHK, 3BHK". The workaround has been **deleted, not fixed**: with the
correct expansion there is no duplicate, and its adjacent-repeat rule would have gone
on to delete the real 2BHK. A workaround for a wrong transform becomes a bug the
moment the transform is right.

**Apartment sizes are now IN the corpus** (`inventory.md`) — the 818 sqft figure does
not appear anywhere on the sheet of what is actually for sale, so the ambiguity that
kept them out is gone.

---

## 3. Price — nothing published; the numbers exist but live outside the corpus

No price, no offer, no payment plan, no inventory scarcity (RON commercial rule).
Every price question escalates.

The owner supplied a real price sheet on 2026-07-31. It lives in
**`pricing-internal.md`, which is deliberately NOT in the ingest sources** — so the
figures cannot be retrieved and read aloud. The bot *uses* them to qualify and can
never *say* them. That is structural, not a matter of the model behaving well: the
numbers are not in the cabinet, so they cannot be quoted.

### ⚠️ The budget floor was wrong, and the error was silent

Recorded band: **₹1.5–6 crore.** Actual entry price: **₹1.28 crore.**

Budget is a **hard gate** — failing it means Dead and permanent suppression. At a
₹1.5 Cr floor the bot would have discarded a buyer with ₹1.3 Cr while an apartment
they could afford sat on the list. No error, no complaint, no log line: you would
simply never hear from the people it threw away.

**Corrected: floor ₹1.28 Cr, ceiling ₹5.5 Cr.** Only the floor rejects — somebody
with ₹8 crore is a good problem, not an unqualified lead.

**Still an owner decision:** whether any price may ever be spoken. If approved, the
change is to move a curated table into `inventory.md` and re-run the ingest. No code
changes.

---

## 4. Beach and lagoon claims

**Never imply direct access to a natural private beach.** Only approved wording:

> a planned man-made beach and lagoon experience within the community

Applied to the location corpus already: "Covelong Private Beach" renamed to
"Covelong", "Barefoot Bay" dropped entirely. See `location.md`.

---

## 5. Distances are never converted to drive times

The location corpus holds kilometres only. The bot must not turn them into minutes —
an invented travel time on a location question destroys trust, and ECR traffic varies
by hour and season. "How long to reach?" escalates.

---

## 6. Excluded from the corpus entirely

| What | Count | Why |
|---|---|---|
| Blank FAQ rows | 6 | no answer to give |
| Written for a human, not a buyer | 15 | *"KK to come up with an answer"*, *"Question not understood"*, *"Shared on email"* — a bot with these in its corpus reads internal chatter aloud to a buyer |
| Handover dates (row 22) | 1 | rule 1 above |
| Apartment sizes (row 7) | 1 | rule 2 above, pending sales |

**Consequence, accepted:** those topics have no source, the confidence floor fires,
and they **escalate to a human by design**. That is correct behaviour, not a gap —
but it is human workload, and price plus handover plus apartment sizes will be a
large share of the questions asked.
