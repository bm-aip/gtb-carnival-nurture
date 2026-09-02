# RON — publishable starting prices

**This file IS ingested.** It is the only pricing the bot may quote. The exact
per-unit sheet stays in `pricing-internal.md`, which is gitignored and deliberately
absent from `SOURCES`.

Owner decision 2026-08-02: *"we should be able to talk about price - always say
starting or onwards - so that we are safe."*

## Why starting prices only, never per-unit

Every live ad already publishes the villa entry price — `3.94 Cr* Onwards` — so a
buyer arrives knowing it. Refusing to repeat our own advertised number reads as
evasion at the first question, and the earlier blanket ban produced exactly that.

Prices are written **`Rs`**, not the rupee symbol, throughout this file. The ad uses
the symbol; we do not, because free session text reaches Wati as a URL query
parameter rather than a JSON body, so a non-ASCII character depends on their
decoder to survive. `Rs 3.94 Cr` is ordinary Indian property language and renders
identically everywhere. Same number, one fewer thing to go wrong.

But an exact figure against an exact unit — "2552 sqft is Rs 3.9 Cr" — invites a
negotiation the bot cannot hold, and prices move. A starting price is a true,
durable, non-negotiable statement. Anything more precise is a human's job.

## The prices

| Configuration | Starting price |
|---|---|
| Villas (overall entry) | Rs 3.94 Cr onwards |
| 3 bed villa | Rs 3.94 Cr onwards |
| 4 bed villa | Rs 5.5 Cr onwards |

The villa entry is quoted as **Rs 3.94 Cr**. That was the figure in every live ad, so
buyers arrived already knowing it. **Ads stopped on 2026-09-02** and new buyers will
not have read it anywhere — the number stays anyway, on the owner's decision. It is
true, it is a starting price, and the earlier blanket ban on saying it is what made
the bot read as evasive at the very first question. The internal sheet rounds the same
unit to Rs 3.9 Cr; they are the same price.

**Villas only from 2026-09-02.** There is no cheaper configuration to fall back on, so
there is nothing below this table to offer. A buyer who cannot reach Rs 3.94 Cr is not
handed to a colleague and not turned away — see `inventory.md`.

## Rules that travel with these numbers

- Always say **from**, **starting** or **onwards**. Never a flat price, never a
  range with a top, never a per-square-foot rate.
- Never quote a price against a specific unit or a specific size.
- Never discuss discounts, offers, payment plans, pre-EMI or registration charges.
- If asked "what will THIS unit cost", or for anything beyond a starting figure,
  a colleague follows up. That is not evasion, it is the honest answer: the exact
  number depends on the unit, the floor and the day.
