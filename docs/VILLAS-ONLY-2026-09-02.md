# Villas only — what changed on 2 September 2026, and how to ship it

Owner's decision, 2026-09-02: **stop running ads, and stop selling apartments.
Villas only.**

Two follow-up decisions taken the same day, because the code needed them:

| Question | Decision |
|---|---|
| A buyer who cannot afford a villa? | **Honest, then parked.** Tell them villas start from Rs 3.94 Cr, call nobody, keep following up. |
| The people ads already brought in? | **Keep following up as normal.** Ads stopping means no new arrivals, not abandoning the ones we have. |
| Are apartments sold by anyone else? | **No.** Gone from the project entirely. |

---

## The thing that nearly went wrong

"A product we do not sell" is already a phrase in this codebase, and it leads
somewhere specific: `handoff.py`'s `dead` branch, which sets `leads.suppressed=TRUE`
and blocks **every future message to that phone, permanently**.

Apartments became a product we do not sell. The obvious edit — add apartment words
to `OFF_CATEGORY` — would therefore have silently and irreversibly muted every buyer
who types "2BHK". That is the opposite of what the owner asked for.

So an apartment word is **not** off-category. `config.classify_configuration` prices
it as the nearest villa, and `qualifier.py` tells the model, in code, to say once
that we sell villas only. The affordability arithmetic then does the rest: a buyer
who cannot reach Rs 3.94 Cr lands in the existing *"reaches NOTHING in the current
release"* branch, which already says do not hand over and do not close.

**Nothing new decides anyone's fate.** The behaviour the owner asked for falls out of
machinery that was already written, already tested and already live.

`tests/rules.py::test_the_apartment_buyer_is_told_once_and_kept` checks this from
both ends. If it ever fails, read it before changing it.

---

## Deploying — the order matters

```bash
# 1. Code. Nothing below is safe until this is running.
git push && <deploy>

# 2. Re-ingest the corpus. Look for "26 withdrawals carried forward" on the FAQ.
railway run --service gtb-carnival-nurture python scripts/ingest_kb.py

# 3. Withdraw the two apartment-only FAQ answers. AFTER the ingest, never before.
railway run --service gtb-carnival-nurture python scripts/quarantine_apartments.py
```

### Why that order

Step 3 resolves chunks on whichever FAQ version is **active**. Run before the
ingest and it withdraws chunks on the old version, which the ingest then supersedes —
printing success and changing nothing a buyer can reach.

### What step 2 must NOT print

```
REFUSED: RON FAQ (curated) has N chunks but v7 had 64, and 26 are quarantined.
```

That means a question was added to or removed from `faqs.md`. The 26 withdrawals from
the 2026-08-03 audit — the flood promise, the fire-safety line, the maintenance
provider's name — are carried forward **by ordinal**, so a changed count would
withdraw the wrong answers and free the dangerous ones. The ingest refuses outright
and writes nothing. That is correct behaviour, not a bug to force past.

**This is why apartment answers in `faqs.md` were EDITED, never deleted.**
`tests/rules.py::test_the_corpus_sells_villas_only` asserts the count is still 64.

### Rolling back

```bash
railway run --service gtb-carnival-nurture python scripts/quarantine_apartments.py --undo
```

Restores only what that script withdrew; the 2026-08-03 audit's 26 are untouched.
The corpus edits roll back with the code.

---

## What changed, by file

**What the bot sells**
- `config.py` — `CONFIG_FLOORS` drops the three apartment floors. Project entry moves
  Rs 1.28 Cr → Rs 3.94 Cr; effective entry after the 25% stretch is ~Rs 3.15 Cr.
- `config.py` — `ASKS_APARTMENT`, `asks_apartment()`, `VILLA_ONLY_FRAMING`.
  `classify_configuration` prices an apartment word as the nearest villa.
- `qualifier.py` — the villa-only correction, decided in code; `_OVERVIEW_QUERY` and
  the "which home?" question no longer offer apartments.

**What the bot says**
- `kb/RON/inventory.md`, `pricing.md` — apartments removed; `inventory.md` records
  that apartments are the one "not for sale" item that must **not** be escalated.
- `kb/RON/faqs.md` — six mixed answers edited to villa-only. Chunk count unchanged.
- `kb/RON/approved-answers.md` — pool answer, positioning line, rules table.
- `scripts/ingest_kb.py` — `APPROVED_ALWAYS` now reads *"Only 3-bed and 4-bed villas
  are on sale."* It rides on all eleven approved answers, so it is the single most
  repeated sentence in the corpus.
- `content/answering-rules.md` — the price ladder is one question now, the downsell
  section is replaced, and the marketing voice sample no longer models answering an
  apartment ask without correcting it.

**Everything else**
- `media.py` — three apartment renders retired. An apartment enquirer is shown the
  **villas**, which is the honest picture on the turn they are told.
- `reopener.py` — checklists written before today say "Compact 2BHK apartment", and
  the template quotes the topic verbatim. Those fall through to the buyer's purpose
  instead of naming a home we cannot sell.
- `conversation.py` — `wants_villa` corrected. Nothing calls it; it was left correct
  rather than left to rot.

---

## Positive framing, and why it is not fussiness

Nothing in the corpus says *"we no longer sell apartments."*

A rule reading "do not mention apartments" puts the word into retrieved context on
every turn it rides along, and the model mirrors what it reads. It is the same reason
`APPROVED_ALWAYS` has never named villaments, and the same reason a guardrail must
never quote the superseded figure it is warning about.

The corpus says what **is** sold. What is not sold is handled in code.

---

## Open, and deliberately not done

- **The marketing positioning line.** "India's First Man-Made Beach Community. Luxury
  Apartments & Signature Villas." is marketing's own copy. "Luxury Apartments &" was
  removed because it describes half a project we no longer sell — **marketing should
  re-approve the replacement.**
- **Six FAQ answers were edited**, and they are marketing's document. Each edit only
  removed an apartment clause from an answer whose villa half was kept. Worth a read:
  water supply, EV charging, driveway, main door, drying clothes, solar panels.
- **`Rs 3.94 Cr` is still quoted.** Its justification was "every live ad publishes
  it". Ads have stopped, so new buyers will not arrive knowing it. Owner's call was
  to keep saying it: it is true, it is a starting price, and refusing to name a
  number at the first question is what made the bot read as evasive before.
- **Ad plumbing is untouched.** CTWA capture, attribution, CAPI and the per-ad report
  still work; they will report zeros. Nothing was ripped out, so restarting ads needs
  no rebuild.
