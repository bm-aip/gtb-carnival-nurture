# Phase 0 Architecture — Send Safety

**Created:** 2026-07-29. Covers BUILD-PLAN tasks 1–5.
**Status:** awaiting owner sign-off. No code written.

Read alongside `POST-CARNIVAL-DESIGN.md` (rev 2) and `BUILD-PLAN.md`.

---

## The finding that shapes everything

**There is exactly one door.** Every outbound message leaves through `sequencer.py:230` `_send()`. All five send sites route through it:

| Site | Type |
|---|---|
| `sequencer.py:327` | m3 |
| `sequencer.py:344` | m1 |
| `sequencer.py:365` | m2 |
| `sequencer.py:482` | welcome (walk-in) |
| `sequencer.py:491` | ack |

One bypass exists and must be closed: `app.py:319` `admin_test_send()` calls `wati.send_text()` directly.

Because there is one door, all four safety rules go in one gate rather than being spread across callers. Four rules × three future callers (knock engine, qualifier, ack path) would be twelve places to get right, and one miss is a compliance incident with a real person.

---

## Verified state of the four safety mechanisms

Established by inspection 2026-07-29, not assumption.

| Mechanism | Today |
|---|---|
| Opt-out | **Does not exist.** No word anywhere in the codebase stops a person permanently. |
| Fatigue cap | Caps exist but protect *our number*, not the person: `MAX_SENDS_PER_HOUR=30`, `DAILY_SEND_CAP=250`, both counted off `message_log` in aggregate. One human could receive every message we own. |
| Retry ceiling | `send_attempts` increments (`sequencer.py:259`) and `sequencer.py:252` documents that it **deliberately never stops**. Only a per-lead permanent error (`"does not exist"`, `"not a valid whatsapp"`) suppresses. Infinite retry by design. |
| Delivery tracking | Wati **does** post delivered/read/failed callbacks. `wati.py:163` discards them: any `eventType` outside `message/text/interactive/button` returns `(None, None)`. |

Carnival wiring to remove (task 1): `EVENT_DATES` at `config.py:64`, `parser.py:7`, `parser.py:13`, `sequencer.py:189`, `sequencer.py:285-286`, `sequencer.py:422`, `app.py:178`. `parser.py` is entirely the carnival day-picker and is deleted whole.

---

## The design — one bouncer at one door

```
send request
   ↓
1. opt-out ledger      → permanent, all brands  → BLOCK forever
2. per-person fatigue  → too many lately        → BLOCK until window rolls
3. per-person retries  → N failures             → BLOCK, mark unreachable
4. system state        → pause / quiet hours / number cap → HOLD, retry later
   ↓
Wati send
   ↓
5. delivery callback   → delivered / read / failed → recorded against the person
```

**All four checks key on phone number, not lead id.** `leads` is `UNIQUE (project, selldo_lead_id)` — the schema *guarantees* one human can be several rows (RON lead + reactivation lead + website form). Lead-keyed safety would be silently broken in exactly the reactivation scenario rev 2 introduces.

### Build order: 1 → 5 → 2 → 3 → 4

Not the BUILD-PLAN numeric order, deliberately. Clear the dead carnival wiring first so the new gate isn't built around code that is about to be deleted. Then **delivery ears before the three blocks**: the blocks are cheap to build but their thresholds are guesses without delivery data. Hearing first means real numbers tune them, instead of us picking a number and discovering it was wrong via a Meta restriction.

One task at a time, each pausing for owner review.

---

## Locked: replies are never predefined

Owner-confirmed 2026-07-29.

| | Predefined? |
|---|---|
| Outbound first knock | **Yes** — Meta forces pre-approved template copy for cold sends. Variable slots only (`{{name}}`, `{{brand}}`). |
| Everything after a reply | **No** — free text both directions, the agent interprets |
| Quick-reply buttons on the template | Optional, only to make replying easy. A tap arrives as inbound text and is treated identically to typed text. |

Consequences:
- **A template's only job is to earn *any* reply.** It need not ask a cleanly answerable question or offer options. "Who is this?" is a total success — window open, agent takes over. Makes the 4 templates easier to write and to get approved.
- **Task 18 simplifies:** knocking stops on *any* inbound, not on a matched keyword. No owner keyword list needed.
- This is the opposite of carnival behaviour — `parser.py` mapped replies to a fixed date list and understood nothing else. Task 1 deletes it.

**Deliberate exception — opt-out stays deterministic.** `STOP / unsubscribe / remove me` is hardcoded matching that runs *before* the agent ever sees the message. Reason: an agent judging "did they mean stop?" will sometimes judge wrong, and being wrong there means messaging someone who told us to stop — a compliance incident, not a bug. The agent handles the grey case ("not interested") only if the owner rules that grey counts as permanent.

---

## Open — owner decisions blocking Phase 0

1. **Fatigue cap number.** After 4 knocks over 25 days, a person goes quiet. A new campaign starts, or they refill a website form. Options: nothing more until a human unlocks them / new reason resets the counter / hard lifetime ceiling.
2. **What counts as "stop".** Does `"not interested"` mean *never contact me about anything*, or *not this project, this time*? Determines whether we may ever re-approach.

---

## Technical defence

**Why one gate, not per-caller checks** — `_send()` is already the sole choke point, so a single gate is a small change rather than a rewrite, and the rules become provably unbypassable.

**Why phone-keyed** — the schema guarantees one human maps to many lead rows; see above.

**Why ears before blocks** — thresholds set from real delivery data instead of guesses.

**Why now** — the send path is inert since 13 July. This is the only window in which send logic can be rewritten without a message reaching a real person, and it shuts the moment anything is switched on. If the WhatsApp number gets restricted, all three inflows die at once, including CTWA, which works fine today.
