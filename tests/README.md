# Tests

Two layers, because the defects come in two kinds.

```bash
python tests/rules.py                                  # ~1s, no database, no API
railway run python tests/conversations.py              # real model, real corpus
railway run python tests/conversations.py downsell -v  # one scenario, full replies
```

## Why this exists

Owner, 2026-08-02, after the third round of live testing: *"why are we applying
bandaids after bandaids"*.

Every defect that day was found by a person messaging the bot from their own phone —
a wrong villa size that reached a real buyer, a duplicate lead that double-messaged
someone, a qualified buyer downsold to apartments, three separate cases of the bot
going silent mid-conversation. That is a slow and expensive way to learn that a
regex was wrong.

## `rules.py` — the guards

Pure functions: the qualification arithmetic, the configuration classifier, the
price guard, corruption detection, the locality rewrite, the mall lock, knock
spacing. **73 cases, about a second, no credentials.** Run it after every change.

Each case is a defect that actually happened. `villa @3.5cr qualifies` is there
because a real buyer was told the opposite.

A case marked `known_bug=True` is a defect we know about and have not fixed. It
prints separately and does **not** fail the run, so an open defect stays visible
instead of being forgotten or silently tolerated. There are currently two.

## `conversations.py` — what the bot actually says

`rules.py` cannot catch the defect that cost a buyer: the arithmetic was right,
`clears_the_bar` returned QUALIFIED, and the model still wrote *"that sits a little
above your band"* and offered apartments. Only a real turn shows that.

So this replays scripted buyers against the real model and asserts on the text:

| assertion | meaning |
|---|---|
| `forbid` | regex that must NOT appear |
| `require` | regex that MUST appear |
| `action_not` | the decision's action must not be this |
| `qualifies` | `clears_the_bar` on the folded checklist must equal this |

It creates no lead, writes no row and sends no WhatsApp message — the lead and
conversation are dictionaries that live for the length of the test.

### It pays for itself

Run against the code that was already "fixed", it reproduced the downsell
immediately and showed the cause: the affordability verdict is built from the
checklist, which holds the state *before* the turn, so on the very turn the buyer
names a budget there was no verdict and the model did the sum itself.

It also found that `"Need More Details"` — the template's own button, the first
thing a knocked buyer taps — escalated to a human about one turn in three. Not
disobedience: embedding that phrase retrieves nothing, so the citation rule had
nothing to match and forced an escalation. Deterministic once retrieval falls back
to an overview query. **Four for four after the fix, one in three before.**

Variance across runs is itself a finding, and a single manual test cannot see it.

## Adding a case

When something goes wrong in production, add it here before fixing it. A defect
with a test cannot come back; a defect without one already has.
