# Post-Carnival Redesign — The Qualification Machine

**Status:** Design agreed. **NO CODE WRITTEN YET.**
**Date:** 2026-07-28 (rev 2). Original 2026-07-13 preserved at `POST-CARNIVAL-DESIGN.2026-07-13.md`.
**Active project:** **Republic of Nature (RON)** — Vadanemmeli, ECR.
**What this is:** the bot replaces the presales team. It qualifies leads and hands sales only the ones that clear a bar sales themselves agreed to.

**What changed in rev 2:** the qualifier now has **three inflows**, not one — CTWA, website/form leads, and reactivation of old leads. That single change rewrote the front door (§4), the flow (§5), added the knock engine (§6) and the persuasion ladder (§7), and reordered the build (§14). Wati's role was also pinned down: **Wati is the messenger, our app is the engine.**

---

## 1. The reframe

The carnival system was an **invite machine**: one-shot, time-boxed, single CTA, urgency did the work.

The new system is a **qualification machine**.

Rev 1 said flatly: *"it is not a nurture drip."* Rev 2 corrects that, because we now nurture old and cold leads over 30 days. But the distinction still holds, and it matters:

> **The drip is the doorbell, not the destination.**

Templates exist to get someone to open a conversation. The moment they do, a **qualifying agent** takes over — not another scheduled message. Nothing in this system sends content for its own sake, and no lead reaches sales because they watched a video. They reach sales because they cleared the bar.

Owner's stated facts that drive everything:
- **Win = a site visit happened.**
- **Most leads are junk.** Only a small slice deserve a human.
- **The bot replaces presales.** Marketing is building this to filter and send only qualified leads *directly to sales*, bypassing the presales team entirely.

So the bot's job description is precise: **do what a presales caller does.** Qualify, filter, hand over.

---

## 2. Decisions locked (owner)

| Question | Decision |
|---|---|
| Success metric | **Site visit happened** |
| Lead quality | Small slice worth a human; most are junk |
| What the bot is | **Presales.** It replaces the presales team. |
| Who answers leads | **The bot.** Human (sales) only after qualification. |
| **QUALIFIED =** | **Budget (hard) · Location (hard) · Configuration (soft).** See the gate table below. |
| **Location means** | **Where they want to BUY** — the micro-market they're shopping in. A fit filter, not a reachability filter. RON is on ECR at Vadanemmeli; someone hunting on OMR is not a fit however much money they have. |
| **Configuration** | **Soft gate.** Rejects **only if wildly off-category** (they want a plot, a shop, a rental — something we simply don't sell). A 2BHK-vs-3BHK gap does **not** reject: sales can flex inventory, upgrade, restructure the payment plan — *"they have tools in their hand."* Passed to sales flagged. Ambiguous cases → escalate. |
| **Purpose** (end-use vs investment) | **Captured, NEVER gates.** Not a filter — **a lens.** It reframes every answer the bot gives afterwards, and tells sales which pitch to open with. |
| **Timeline** | **Captured, NOT gated.** Bot asks, records it, hands it to sales as context. Sales prioritises. |
| **Ask order** | **Purpose → Location → Configuration → Budget.** (NOT the same as gate priority — see below.) |
| Does the bot book the site visit? | **YES — reversed 2026-07-31.** It takes a day and time and acknowledges the visit as booked, then says the team will call to confirm timing and share directions. It must not say a bare "confirmed": there is no calendar behind it. Tuesday never offered (team's day off); Monday afternoon only. Experience Centre at Express Avenue offered **only** on a distance objection, never instead of the site. See §8. |
| Does the bot answer open-ended questions? | **Yes — this is core, not a bonus.** A RAG agent answers from that project's curated corpus. **It answers before it asks** (§7, §9). |
| Has sales agreed the bar? | **Yes.** Sales agreed the definition. Budget band and off-category now supplied; location and config lines pending (§13). |
| Handoff destination | **WhatsApp group ping only.** Sell.do write is **parked** (owner, 2026-07-28: *"that is slow the progress"*). Transcript still stored our side — nothing is lost, the CRM push becomes an extra sink later. See §8. |
| **Inflows** | **Three:** CTWA · website/form leads · reactivation of old leads. **This is the crux of rev 2.** See §4. |
| **Wati's role** | **Messenger only. Our app is the engine.** No Wati keyword routing, no Wati chatbot, no Wati journeys. See §4c. |
| **Nurture cadence** | **4 touches — day 0 · 3 · 10 · 25.** Day 31 silence → dormant. See §6. |
| **Reply during nurture** | **Stops the knocking. Does NOT skip the bar.** Bot qualifies, then sales. |
| **Day 31 silence** | **Dormant** — record kept, re-entry only on a genuinely new reason (new phase, new launch). **The bot may never auto-restart a journey.** |
| **Old-lead safety** | **Suppression gate before any knock**, reading Sell.do stage + label against a **config list** (owner supplying). See §5. |
| **Ghosts** (started, then went silent) | **Chase them — through the same knock engine.** Ghost is not a fourth exit, it's a loop-back. Bot resumes mid-checklist, never restarts it. |
| Cross-brand routing (budget/location fits another project) | **No. Stay in lane.** Fail them out instead. |
| Price policy | **Range only, never exact — and for RON today, no price at all.** The nurture plan forbids publishing price until approved, so no figure enters the corpus. |
| RAG engine | **Build our own.** Do NOT use Wati's Astra. |
| Architecture | Multi-brand, **ring-fenced KB per brand**, one qualifier agent per brand |
| Brands | **RON is the live build.** Engine stays multi-brand by design — a second project is a config row, not a release. |
| Who owns the knowledge base | **Sales.** (They know what's true and sellable today.) |
| Who owns the persuasion wording | **Sales.** The reason-lines and framings are config, not code. See §7. |

### The gate table

| Slot | Can it reject? | Notes |
|---|---|---|
| **Budget** | **HARD — yes** | The sharpest filter. RON band: **₹1.5–6 crore.** |
| **Location** (where they want to buy) | **HARD — but WIDE** (amended 2026-07-30) | Fails only on a **different city/region**, or an **explicit standing exclusion of ECR / the south side**. Anywhere in or around Chennai — OMR included — **passes**. See below. |
| **Configuration** | **Only if wildly off-category** | Plot / commercial / rental → reject. 2BHK-vs-3BHK → **pass to sales, flagged.** Ambiguous → escalate to a human. |
| **Purpose** | **Never** — but now **flags** (amended 2026-07-30) | A **lens**, not a filter. Never rejects. A primary-residence / daily-commuter answer rides on the handoff card as a fit warning. See below. |
| **Timeline** | **Never** | Context for sales to prioritise on. |

#### ⚠️ AMENDED 2026-07-30 (owner) — the location gate is wide, and purpose carries the fit signal

Owner: *"right now if someone says OMR we will pass it as long as the budget is fine — this is a bit of a weekend home / resort style, so the regular distance to schools / workplace may not matter so much."*

**RON is a weekend-home / resort-style product.** That reframes the whole location question. Location fails on only two conditions:

1. They want to buy in **a different city or region** — Bangalore, Hyderabad, Coimbatore, anywhere unconnected to Chennai.
2. They **explicitly and finally rule out ECR / the south side.**

**Condition 2 carries a trap, and the trap is handled mechanically.** *"ECR is too far"* is not an exclusion, it is **an objection the bot exists to answer** — nurture touch 2 is entirely about location reassurance, and the FAQ objection library has a dedicated entry for it. Rejecting on it would kill leads for voicing the single most common doubt about the project.

So: **the bot must answer the doubt before the gate may reject on it.** The location-exclusion rejection is gated on `location_objection_answered = true` — the bot makes its case once, and only a *standing* exclusion after that counts as a fail. Same shape as the persuasion ladder (§7): answer first, then ask again, then honour the answer.

**Three consequences, stated so they are not discovered later:**

**(a) The bar is now effectively budget-only.** Almost nobody in Chennai fails on location, and config only rejects off-category. That is a legitimate call for this product, but the three gates were partly the **political mechanism of §3** — sales trusts the queue because the bot rejects people. A budget-only bar means sales receives more leads, and the first bad one costs trust. **Tell sales this rather than let them discover it.**

**(b) Purpose is promoted from framing to fit signal.** With location no longer catching mismatches, purpose is the sharpest fit indicator left. A buyer saying *"primary home, kids need school nearby, I commute to Guindy daily"* is a genuine mismatch with a resort-style property — 39 km from the airport, no school in the location file — **regardless of budget**. Owner's rule: **pass them, but flag it on the handoff card** so the salesperson can reset expectations on the call instead of on arrival at a site visit. Purpose still never rejects.

**(c) FAQ rows 40/41/42 drop off the critical path.** Schools, hospitals, IT corridors and metro were blocking because location was a hard gate. For a weekend-home buyer they are secondary, so the confidence floor escalating them to a human is **acceptable behaviour rather than a hole**. Hospitals remain worth having — emergencies, and older buyers — at lower priority.

**Only budget and location can send a lead to the dead lane** (and location now rarely does). Config needs one KB fact per project: *what category do we actually sell?* In-category-but-not-in-stock is a sales problem, not a rejection — sales can flex inventory, upgrade, restructure the payment plan. *"They have tools in their hand."*

### The budget gate needs no price in the corpus

Two rules that look like a conflict and aren't:

- Our guardrail: **price is a range, never an exact figure.**
- RON's commercial rule: **do not publish price, offers, payment plans, possession claims or inventory scarcity until approved.**

Resolution: **the budget gate is internal arithmetic.** The bot compares what the buyer says against a floor it holds privately, and decides. It never quotes a number, because **no number is in the cabinet to quote.** Policy becomes physics — the same trick as the brand fence.

Consequence to accept knowingly: **every price question escalates to a human** until commercial info is approved. On a ₹1.5–6cr product that will be a lot of escalations. Budget the human capacity for it.

### Purpose is a lens, not a gate

End-use and investment are **both real buyers with real money.** Neither gets rejected. But they want opposite things, and the bot must know which before it opens its mouth:

- **End-user** → schools, commute, possession date, EMI, which floor gets the light.
- **Investor** → rental yield, appreciation, resale liquidity, exit horizon. **He does not care about the playground.**

**Third purpose, and for RON the most important one (added 2026-07-30): the weekend-home buyer.** RON is resort-style, so this is the product's natural buyer — they want the beach and lagoon experience, the low density, the drive from the city, the temples and the quiet, and they are indifferent to schools and commute. The location file's landmark list (Sheraton 800 m, Barefoot Bay 400 m, Mahabalipuram 15 km, Muttukadu boat house 8 km) is **weekend-buyer material almost in its entirety** — retrieval should favour it heavily once purpose is known to be weekend or second home.

The three purposes now split retrieval three ways, and the **primary-residence answer additionally raises a fit flag on the handoff card** (see the gate table amendment).

**Implementation consequence:** purpose is a **conversation-level variable** that biases retrieval and framing for every subsequent turn. Same knowledge base, different half of it. The moment the bot learns "investment," it should stop retrieving amenity copy and start retrieving yield and appreciation material.

### Ask order ≠ gate priority

Budget is the sharpest filter, so by *value* it ranks first. But **"what's your budget?" is a brutal opening line to a stranger who just tapped an Instagram ad.** Presales people don't lead with it — they **earn** it. Purpose, location and configuration are warm-up questions: they feel like the bot is *helping*, not screening.

**Decision: Purpose → Location → Configuration → Budget.**

Note the shape — **the ask order is the exact inverse of the gate hardness:**

```
ASK:     Purpose  →  Location  →  Config  →  Budget
GATE:    never        HARD         soft       HARD
```

That's not an accident, it's good presales. Open with the question that costs them nothing and makes you useful; spend the trust you earn on the question that decides everything.

**And a useful accident falls out of it.** Budget is asked *last*, so most ghosts go silent **before** the budget question — meaning the typical ghost is someone whose **purpose, location and configuration we already know.** That hands the re-open template its own script:

> *"You were looking at a 3BHK at Republic of Nature — shall I send you the layout?"*

That message is simultaneously **(a)** a step toward the budget question and **(b)** shaped exactly like a **UTILITY** template — a follow-up to something *they* asked, not a promotion. Which is precisely the category that dodges Meta's block rate (§4d). **The knock engine's biggest weakness has a natural answer built into the ask-order.**

### Why the bar is Budget · Configuration · Location — and not Timeline

All three gates are **facts**: checkable, stable, hard to fudge. **Timeline is a mood.** Everyone says "soon." Gating on the one slot buyers routinely misreport is how you reject real buyers who were merely being honest about being six months out.

So timeline is captured as **intelligence for sales**, not as a gate. Sales sees "ready in 3 months" vs "ready in 14" and prioritises accordingly.

**Consequence:** nobody fails on timing, so a far-out buyer with the right money, config and location goes straight to sales with his timeline attached. **Reversible:** if sales later complains the queue is full of far-out browsers, timeline can be promoted to a gate.

### Why we rejected Astra (Wati's own RAG/agent tool)

Astra is real and capable — RAG over synced docs, webhook tool-calling, escalation to the Wati Team Inbox (https://support.wati.io/en/articles/13193160). Rejected because it would put the **price policy, the brand isolation, and the escalation rules in a browser tool outside git** — unversioned, unreviewed, editable by anyone, no audit trail. For a multi-brand setup that is a cross-brand leak waiting to happen.

### Recorded cost of "no cross-brand routing"

Choosing clean attribution and a clear buyer experience over recovering mismatched leads. A buyer too rich for one project but right for another — or hunting in the wrong micro-market — is failed out rather than transferred. **Reversible call** — if volume shows that pile is large, revisit.

---

## 3. The political mechanism — why this one might actually survive

The standard way a marketing-built qualification system dies: **sales doesn't trust the filter, so sales ignores it and calls everyone anyway.** The machine gets built, presales gets removed, and nothing changes — except now there's no fallback.

The defence here is structural, and it's already in place:

**Sales owns the bar. Sales owns the knowledge base. Sales owns the persuasion wording.**

They set the definition of qualified. They feed the bot its facts. They write the lines it uses to ask for a budget. So when a lead lands in their WhatsApp group, they cannot say *"marketing's bot sent me rubbish"* — it filtered by **their** bar and said what **they** told it to say.

**This must be protected in the build.** Whatever we ship must keep sales' hands visibly on all three. Which means: sales are not engineers, so "sales owns the corpus" requires **a screen they can drop a PDF into and see what changed** — not a script, not a CLI. That is real scope, and it decides whether the bot's knowledge is fresh in six months or quietly two quarters stale.

**Rev 2 adds a second political interlock: the suppression gate (§5).** A bot that knocks on a lead a salesperson is already working destroys goodwill faster than a bad filter does — because it embarrasses that salesperson in front of their own customer. Suppression is not hygiene, it's the same survival mechanism.

---

## 4. The front door — three inflows, one machine

Rev 1 assumed CTWA was the only door. It isn't. **Three inflows now feed one qualifier**, and the qualifier does not care which door a lead came through — it only ever reads `brand_id` off the lead record.

### (a) The real dividing line is the 24-hour window, not the campaign

| Inflow | 24h window | What the bot may do |
|---|---|---|
| **CTWA** (click-to-WhatsApp) | **OPEN** — they messaged first | Free text. Qualify immediately. Costs nothing. |
| **Website / form leads** | **SHUT** | Approved template only. Meta's gate applies. |
| **Old-lead reactivation** | **SHUT**, and cold — last contact months ago | Approved template only. Highest block risk of the three. |

**Design consequence: two lanes, one qualifier.**

- **Live lane** — window open. Bot talks now.
- **Knock lane** — window shut. Bot knocks with a template and waits. **A reply opens the window**, and only then does the qualifier take over.

Campaign source is still captured (it's how the flywheel and attribution work), but it is **not** what decides behaviour. The window is. Sorting the design by campaign type instead of window state is how you end up with three half-duplicated senders.

### (b) CTWA moves two problems rather than solving them

**It moves the sorting problem.** With lead forms, the reply *was* the filter — junk never replied, and we got that sort for free. **With CTWA, everyone at the door has already "replied."** Tyre-kickers and buyers look identical on arrival. There is no reply-fork to hide behind: **the bot itself is now the entire filter.**

**Wati surfaces the ad**: it auto-creates `source_id` (the Ad ID) and `source_url` on the contact. So brand stamping is *exact* — we know the ad, therefore the project, at first contact. No guessing from message text.

### (c) Wati is the messenger. Our app is the engine.

Locked by owner, 2026-07-28: *"Wati is the messenger — our bot app is the engine driving it — no confusion here."*

The RON nurturing plan (§16) was written as a **Wati-native automation**: Wati keyword routing (`RON_VISIT`, `RON_MAP`, `RON_CALL`…), a Wati chatbot for site-visit booking, Wati journey suppression rules. **All of that moves to our side.**

| Concern | Owner |
|---|---|
| Delivering a message; template rendering; media headers | **Wati** |
| Receiving inbound messages via webhook | **Wati** (we consume it) |
| Deciding what to send, to whom, when | **Us** |
| Interpreting a reply — including quick-reply button taps | **Us** |
| Checklist state, journey state, suppression, fatigue, opt-out | **Us** |
| Booking a visit | **Nobody.** The bot doesn't book (§8). |

Quick-reply buttons stay in the templates and stay useful — a tap arrives at our webhook as ordinary inbound text, and our engine reads it. What we do **not** do is let Wati hold any state or make any routing decision.

**Why this matters mechanically:** two systems that both believe they own a conversation will both message the same person. That's the exact failure the single knock engine (§6) exists to make impossible, and it would be reintroduced the moment a Wati journey ran in parallel with ours.

### (d) The template tax, and where it now lands

Rev 1 noted CTWA dodges Meta's MARKETING gate *on first contact* and moved the template tax to the second knock. **Rev 2 makes it worse and earlier:** two of three inflows are template-first by physics. Form leads and old leads cannot receive a single message without an approved template.

**Every template in the current codebase is useless here.** `config.py:48` holds six carnival templates (`gtb_m1_ron_final`, `gtb_m3_reminder`, …) — all event copy, all dead. The four RON nurture templates are net-new.

**⚠️ Category risk:** the RON plan specifies six **MARKETING** templates with media headers and quick-reply buttons. Marketing-category templates to a *cold reactivation list* is the highest block-risk configuration in this system — the same shape as the carnival blast that produced the 44%-blocked figure, except those recipients were two weeks warm and these are months cold. For CTWA and fresh form leads, marketing category is fine. **For old-lead reactivation specifically, utility-framed variants are strongly preferred.** Owner is handling approval.

### The flywheel (do not miss this at ingestion)

Meta puts a **click ID (`ctwa_clid`)** in the `referral` object of its webhook. Capture it, and when a lead qualifies you can fire a Conversions API event back to Meta — `action_source: "business_messaging"`, `messaging_channel: "whatsapp"`, carrying the `ctwa_clid`.

Meaning: **you tell Meta "this one qualified," not "this one clicked."** The algorithm stops optimising for cheap clicks and starts hunting people who actually qualify. Ad spend gets smarter every week, automatically, with nobody touching a dial.

**⚠️ BLOCKING UNKNOWN:** Meta puts `ctwa_clid` in *its* webhook. **Wati sits in between, and whether Wati forwards that field to us is undocumented.** Must be verified by eye: fire one CTWA click, catch the webhook, read the payload. If Wati strips it, the flywheel needs another path (direct Meta webhook, or Wati's own CAPI feature). **Everything about attribution hangs on this, and if we don't capture the click ID at ingestion it is gone forever.**

Sources: [Wati — identifying & tracking CTWA conversations](https://support.wati.io/en/articles/11463601-how-to-identify-and-track-click-to-whatsapp-ad-ctwa-conversations-and-track-performance) · [WOZTELL — CAPI for WhatsApp Ads](https://support.woztell.com/portal/en/kb/articles/wa-conversion-flow)

---

## 5. The flow

```
        ┌── CTWA click ────────► they message first ──► window OPEN
        │
INFLOW ─┼── Website / form lead ─────────────────────► window SHUT
        │
        └── Old-lead reactivation ───────────────────► window SHUT + cold
                            │
                            ▼
        ┌───────────────────────────────────────────┐
        │  INTAKE                                   │
        │  stamp brand_id (from the ad / the list,  │
        │  NEVER from message text), source,         │
        │  campaign, window_state, checklist = 0     │
        └───────────────────────────────────────────┘
                            │
                            ▼
        ┌───────────────────────────────────────────┐
        │  SUPPRESSION GATE                         │
        │  read Sell.do stage + label, compare      │
        │  against a CONFIG list                    │
        │  → blocked ⇒ never knocked, ever           │
        │  (sales already owns them / dead / bought  │
        │   / ever said stop / invalid number)       │
        └───────────────────────────────────────────┘
                            │
            ┌───────────────┴────────────────┐
            ▼                                ▼
     WINDOW OPEN                      WINDOW SHUT
     (live lane)                      (knock lane)
            │                                │
            │              ┌─────────────────────────────────┐
            │              │  KNOCK ENGINE          (§6)     │
            │              │  day 0 · 3 · 10 · 25            │
            │              │  any inbound reply → STOP        │
            │              │  bounded by fatigue cap,         │
            │              │  opt-out, scheduler stand-down   │
            │              └─────────────────────────────────┘
            │                     │                  │
            │                reply│                  │day 31, silence
            │                     │                  ▼
            │                     │             DORMANT
            │                     │      record kept. Re-entry ONLY
            │                     │      on a genuinely new reason,
            │                     │      decided by a human.
            │                     │      The bot may NEVER
            │                     │      auto-restart a journey.
            ▼                     ▼
     ┌──────────────────────────────────────────────┐
     │        THE QUALIFIER AGENT                   │
     │        (that project's agent)                │
     │                                              │
     │  SOURCE-AGNOSTIC. Resumes at the saved       │
     │  checklist position — never restarts.        │
     │                                              │
     │  ANSWERS FIRST, THEN ASKS         (§7, §9)   │
     │                                              │
     │  ASKS IN THIS ORDER:                         │
     │   1. PURPOSE ..... never gates (a LENS)      │
     │   2. LOCATION .... HARD GATE                 │
     │   3. CONFIG ...... soft gate                 │
     │   4. BUDGET ...... HARD GATE (earned, last)  │
     │                                              │
     │  + TIMELINE, opportunistically               │
     │                                              │
     │  Answers from that project's KB only.        │
     │  Ranges, never exact. For RON today:         │     │  no price at all. BOOKS the visit — takes    │
     │  day + time, team confirms details.          │
     └──────────────────────────────────────────────┘
            │            │            │            │
            ▼            ▼            ▼            ▼
      QUALIFIED     GOES SILENT     DEAD      OUT OF DEPTH
      budget +      window shuts   fails      objection, price
      location      mid-checklist  BUDGET or  pushback, hard Q
      clear;             │         LOCATION.  with no good answer
      config ok          │         Or config       │
      or flagged         │         wildly off-      ▼
            │            │         category.   Bot STOPS TALKING.
            ▼            │         Or junk.    Escalates to a human,
   • PING THE WHATSAPP   │             │       flagged. Does not
     GROUP (the card)    │             ▼       improvise.
   • transcript stored   │        Suppressed   ⚠ These are
     our side (audit +   │        permanently. disproportionately
     later CRM push)     │        Never        the REAL BUYERS.
   • purpose, timeline,  │        touched
     config note and     │        again, by
     topics-asked ride   │        anything.
     along               │
            │            └──────► BACK INTO THE KNOCK ENGINE
            │                     (mid-checklist, NOT a restart)
            ▼
      Sales calls,
      closes for the
      SITE VISIT ✓
```

### Three terminal exits and one loop-back

Rev 1 called this "four exits." That was wrong, and the correction is load-bearing.

| State | Kind |
|---|---|
| **Qualified** | terminal |
| **Dead** | terminal |
| **Out of depth / escalated** | terminal (for the bot — a human owns it now) |
| **Goes silent** | **loop-back into the knock engine.** Its own terminal is **Dormant.** |

Going silent mid-qualification and being a cold old lead are **the same condition**: window shut, checklist incomplete, knocks remaining. Treating them as one state is what lets one engine serve both (§6).

### Why the suppression gate exists

An old lead is not a blank contact. It already exists in Sell.do with a stage and possibly labels. Some of those people are **already assigned to a salesperson**, already rejected, already bought, or already told us to stop.

**Nothing may be knocked before it has been checked.** The suppression list is **config, not code** — owner supplies which stages and labels block a knock, and changing it is a data edit, not a release. Same pattern as brands-as-config-rows.

**⚠️ UNVERIFIED:** our Sell.do connection is a read-only reporting mirror, and today's query (`sql/selldo_leads.sql`) reads **stage** only — it joins `reporting_lead_stages` and nothing else. **Whether labels are exposed in that mirror at all is unknown.** If they aren't, label-based suppression needs a vendor conversation. Verify before promising label-level rules.

---

## 6. The knock engine — one machine, not two

**The nurture drip and the ghost-recovery chase are the same machine.** This is the biggest simplification in rev 2.

Both are the state *window shut, checklist incomplete, N knocks remaining.* They differ only in **where the checklist starts** and **which templates get used**:

| Entering as | Checklist starts at | Template set |
|---|---|---|
| Old-lead reactivation | 0 (nothing known) | RON nurture T1, T2, T3, T6 (amended 2026-07-30) |
| Website / form lead | 0 | opener, then straight to live lane on reply |
| **Ghost** (went silent mid-qualification) | **wherever they stopped** | re-open shaped around what we already know |

### Cadence

**4 knocks: day 0 · 3 · 10 · 25.** Silence at day 31 → **Dormant**.

#### ⚠️ AMENDED 2026-07-30 (owner) — the template set changed

**`touch_05_masterplan` is OUT. `touch_06_visit` is IN, taking the day-25 slot. `touch_04_wellness` drops out to make room.** Knock count stays 4.

| Knock | Plan touch | Job |
|---|---|---|
| day 0 | `touch_01_lifestyle` | earn a reply |
| day 3 | `touch_02_location` | earn a reply |
| day 10 | `touch_03_low_density` | earn a reply |
| day 25 | `touch_06_visit` | earn a reply — **rewritten, see below** |

Dropping touch 5 removes a genuine duplicate: touch 3 is *already* a master-plan video. Touch 5 was a second one.

**The KPI of a template is now REPLY RATE, not visits booked.** Owner, 2026-07-30: the 24-hour window is the crucial asset, so the ask must be low-friction and must not induce pressure. This supersedes the RON plan's stated Primary KPI ("site visits booked — delivered WhatsApp nurture journeys", plan §7), which measured the wrong end of the funnel for our design. A template's only job is to earn **any** inbound. "Who is this?" is a complete success.

**Template 6 is APPROVED and needs no rewrite** (verified against the live Wati account, 2026-07-31: `ron_nurture_06_visit`). An earlier note here said it must be rewritten because the RON plan attached a booking chatbot to it. That was wrong on two counts: the approved copy *offers* a visit rather than booking one (*"we can arrange a guided visit at a convenient time"*), and §8 has since been reversed — the bot now does book. The buttons are `Need More Details` / `Plan a Site Visit` / `Stop updates`.

**Button design rule, from the same decision.** Buttons must **request information, not commitment** — "Send the map", "What's nearby", "See the layout" — or pose **one either-or question**. The strongest either-or is **purpose** ("a weekend place, or somewhere to live full time?"): purpose is the only one of the four questions that *never rejects anybody* (§2), so a vague or wrong answer costs nothing, it is already first in the locked ask order, and the reply therefore both opens the window and starts the checklist. **Location and budget must never appear on a cold template** — both can end a conversation. The plan's own guardrail against *"Are you interested?"* (plan §8) stands.

Two constraints this creates: **button labels are frozen at template-approval time**, so they cannot be tailored per lead — they are chosen once for the whole campaign. And **"Stop updates" is a real opt-out, not a Wati journey pause** — it must wire to the permanent cross-project ledger (`PHASE-0-ARCHITECTURE.md`, task 2), or a lead who taps it gets knocked again by the next campaign.

**⚠️ What the dropped touches took with them.** Plan touch 6's *booking* job does not return — **no template asks for or confirms a visit; the agent warms it conversationally and a human closes it.** Touch 4 (wellness / "Designed with Nature" evidence film) is out of the knock set; its material is still valid corpus content for the qualifier to draw on when asked. Touch 5 carried the beach/lagoon wording guardrail — **that rule survives the deletion of the template**, since it is a claims rule rather than copy, and it lives in §10.

### Rules

| Rule | Why |
|---|---|
| **Any inbound reply stops the knocking immediately** | A scheduled template landing on top of a live human conversation is the single most damaging thing this system can do. |
| **A reply does NOT skip the bar** | It opens the window and hands to the qualifier at the saved checklist position. Sales still receives nobody unqualified. |
| **Resume, never restart** | A returning ghost is never asked a question we already have the answer to. |
| **One fatigue counter, shared** | See the technical defence below. |
| **Max two nurture messages in any week** | From the RON plan's own guardrails. Our cadence already satisfies it. |
| **Scheduler stand-down** | While a lead is in a live conversation, nothing scheduled may touch them. |
| **Day 31 → Dormant, and the bot may never auto-restart** | Re-entry requires a human and a genuinely new reason — a new phase, a new launch. This is the line between nurture and harassment, and it is enforced in code, not in policy. |

### Technical defence — one engine over two

The tempting build is a nurture module for old leads and a separate re-engagement module for ghosts. Rejected.

Two implementations mean **two fatigue counters**. The moment a lead is legitimately in both populations — an old lead who woke up, answered two questions, then went quiet again — they get messaged by two systems that don't know about each other. One engine with one counter makes that **arithmetically impossible** rather than merely unlikely.

Same reasoning as the brand fence: make the bad outcome physically unavailable instead of instructing code not to cause it.

---

## 7. The persuasion ladder — gentle, and mechanical

Owner requirement, 2026-07-28: *"the agent should be able to persuade them to answer in a gentle, persuasive manner — this is important."*

Built as three mechanisms, because **"be persuasive" written into a system prompt drifts** — within a few turns, and differently for every buyer.

### (a) Every ask carries a reason that benefits them

Not *"what's your budget?"* but *"so I only send you layouts that actually fit — what range are you working with?"*

The reason-line is **config per project per gate, editable by sales** (§3). They write the persuasion; engineering doesn't.

### (b) Three distinct framings per gate — never a repeat

| Attempt | Shape |
|---|---|
| 1 | ask + reason ("so I show you the right thing") |
| 2 | offer a range to choose from — far easier to answer than a blank page |
| 3 | name the cost of not answering, softly ("I don't want to waste your time on something that won't fit") |

Repeating the same question in the same words is what makes a bot feel like a form. Three angles feels like a person who cares.

### (c) A counter, not a wall

The bot tracks **answers given vs answers received.** After **3 unreciprocated answers** it **keeps answering** and quietly flags a human. The buyer never feels a door close.

Rationale is already in this document: heavy question-askers who dodge the budget question skew toward **real buyers being cautious** (§5, out-of-depth).

### Never

- Ask two things in one turn.
- Ask without first answering what they asked.
- Repeat a gate's phrasing.
- Ask *"are you interested?"* (explicitly forbidden by the RON plan's guardrails, and it's a question that teaches the buyer nothing and earns us nothing.)

---

## 8. The handoff

**WhatsApp group ping only.** Owner chose this over building a lead page. Sell.do write is parked.

The ping carries the **card**, not the transcript:

```
🟢 QUALIFIED — <project>
<name> · <phone>
Purpose:      <captured>
Location:     <captured> ✓
Config:       <captured>
Budget:       <captured> ✓
Timeline:     <captured>
Source:       <campaign / inflow / knock #>
Asked about:  <topics they raised>
Volunteered:  <any commitment they offered, verbatim>
```

**`Asked about` earns its line.** It tells the salesperson what this buyer actually cares about before they dial — the difference between a cold opener and a warm one.

### ~~Capture, never confirm~~ → **The bot books the visit** (REVERSED 2026-07-31)

**Superseded.** The 2026-07-28 rule was *"we record and pass on — telling the system to do the full booking is not ideal."* The owner reversed it on 2026-07-31:

> *"the right behaviour for plan a site visit is to schedule the visit — date and time — and then offer to get our team to coordinate for next steps... the user is able to converse fully and get acknowledged that their site visit is booked — it should be able to persuade those who may hesitate."*

The reasoning is sound: a buyer who names a day and gets a neutral non-answer is left at a dead end, and that dead end costs more than the risk the old rule avoided.

#### What the bot may say, and the boundary that stays

**"Booked — our team will call to confirm the timing and share directions."**

Not a bare *"confirmed."* The bot has no calendar and no advisor roster, so an unqualified confirmation is a promise the company never agreed to keep, and the failure mode is a buyer standing at a gate in Vadanemmeli on a Saturday holding a WhatsApp message that says it was confirmed. This wording gives them a real commitment and leaves the team room to move an hour. Encoded as `config.VISIT_CONFIRMATION`.

#### Availability (`config.VISIT_DAYS`)

| Day | Available |
|---|---|
| Mon | **afternoon only** — team's weekly meeting runs the first half |
| Tue | **never** — team's weekly off |
| Wed–Sun | full |

#### Two venues, and the order is load-bearing (`config.VISIT_VENUES`)

| | Venue | When offered |
|---|---|---|
| 1 | **The site at Vadanemmeli, ECR** | **always first** |
| 2 | The Experience Centre at Express Avenue mall | **only after the buyer raises distance** |

The Experience Centre has a miniature model and a walkthrough of the RON experience, and runs the same schedule. It is **not an equal option**. A site visit *is* the definition of a win (§2), so a bot that volunteers the mall unprompted would quietly convert site visits into mall visits — a downgrade it must never make on its own.

The intended ladder, owner's words: *"within the week they can see the Experience Centre — and plan the real site visit in the weekend."* **The EC visit is a milestone, not the outcome**, and the handoff card must distinguish the two or the pipeline will read as healthier than it is.

#### Persuading the hesitant

Same mechanics as the §7 ladder — a reason with every ask, three framings, never the same one twice:

| Hesitation | Response |
|---|---|
| *"Let me check and revert"* | Offer two concrete options, not an open question. "This Saturday or next?" is easier to answer than a blank. |
| *"It's too far"* | Answer the distance honestly first, **then** offer the Experience Centre as a first look this week |
| *"I'm not in Chennai"* | Video walkthrough — already in the approved `ron_nurture_06_visit` copy, and costs them nothing |
| *"Just send details"* | Send them. Ask again later. **Never trade the visit for the brochure.** |
| Goes quiet after picking a day | The in-window nudge — free, no template, no tier cost |

Never *"are you interested?"* (plan guardrail), and never the same framing twice.

### The transcript is still stored

Every conversation is stored our side regardless of the handoff channel, because §10's audit guardrail requires it — the day a buyer says *"your bot told me ₹X"*, you need the conversation and the document version that was live that day.

**Consequence: parking the Sell.do write delays where the record lands, it does not lose the record.** When write access eventually arrives it becomes an **additional handoff sink**, not a rewrite.

### ⚠️ Still open: where does an escalation land?

Qualified leads and out-of-depth escalations are **opposite urgencies** — one is *"good news, call them,"* the other is *"we're stuck, rescue this."* Mixed into one group, the rescues get missed. And §5 records that escalations skew toward the real buyers. **Same group or a separate one? Owner decision.**

---

## 9. Inside one bot turn — where the ring-fence lives

```
1. Lead sends a message
2. LOCK BRAND — read brand_id FROM THE LEAD RECORD (stamped at intake),
   never from the message text
3. RETRIEVE — WHERE brand_id = lead.brand   ← the fence. A DB filter, not a prompt.
4. CONFIDENCE FLOOR — nothing solid retrieved? DO NOT answer from general
   knowledge (that's where invented possession dates come from). Escalate.
5. ANSWER FIRST — if they asked something, answer it from the retrieved chunks.
   Ranges never exact figures. For RON today: no price at all.
6. THEN ASK — exactly one ask, carrying its reason-line (§7). Advance the
   checklist by one. Capture timeline if it surfaced. Warm the site visit.
7. LOG — store the reply WITH the exact chunk ids and document versions that
   produced it.
```

**Step 5 before step 6 is not a style preference.** A buyer who feels interrogated goes silent, and silence is already the largest bucket in this system. Answering is how the bot earns the right to ask. It is also why budget sits last: by the time it's asked, the bot has been useful three times.

---

## 10. Guardrails — all mechanical, none prompt-only

| Guardrail | How it's enforced |
|---|---|
| **Brand fence** | `WHERE brand_id = %s`, sourced from the lead record. Cross-brand leakage is *impossible*, not merely unlikely. **Never a prompt instruction** — models drift, buyers ask weird things. |
| **Price = range only; for RON, no price at all** | **By curation.** Ranges may go into a corpus; exact cost sheets stay **OUT**. For RON today the commercial rule forbids publishing price entirely, so **no figure enters the corpus.** You cannot reliably instruct a retrieval bot to withhold a number sitting in its own knowledge base — eventually, to someone, it says it. **What isn't in the corpus cannot be said. Policy becomes physics.** |
| **The beach claims rule** | Always: *"a planned man-made beach and lagoon experience within the community."* **Never** "private beach access", "beachfront project", or anything implying direct access to a natural private beach. This is legal exposure, not copywriting, and the agent **will** be asked about the beach constantly. Enforced by curation of the corpus plus a hard refusal pattern. |
| **No unapproved claims** | No offers, payment plans, possession schedules or inventory scarcity — RON commercial rule. Possession dates are exactly the thing a confident model invents. |
| **No source, no answer** | Confidence floor. Below it → refuse + escalate. Never fall back on the model's own knowledge. |
| **Suppression interlock** | No lead may be knocked before its Sell.do stage/label has been checked against the config list (§5). Protects sales' existing relationships — a political guardrail as much as an operational one. |
| **Books, but does not over-promise** | REVERSED 2026-07-31. The bot takes a day and time and acknowledges it as booked, then says the team will call to confirm timing and share directions. It must not say a bare "confirmed" — there is no calendar behind it (§8). Tuesday is never offered; Monday afternoon only. |
| **Retry ceiling** | Replaces today's loop, which retries a failed send **every 5 minutes, forever**. There is no retry scheduler — a failed send leaves `wa_state` unchanged so the next tick re-picks it (`sequencer.py:252-268`). **The event ending is the ONLY thing that ever stopped it. This is a live landmine.** |
| **Opt-out** | "STOP" honoured permanently, across every project. **Does not exist today.** |
| **Fatigue cap** | Hard lifetime limit on messages per person. One counter, shared by every lane (§6). |
| **Scheduler stand-down** | While a lead is in a live conversation, the scheduled-message system never touches them. Otherwise a warm bot reply and a cold template land the same afternoon. |
| **No auto-restart** | A dormant lead can only re-enter a journey by human decision, on a new reason. The bot cannot reawaken anyone on its own. |
| **Never commits** | No discounts, no promises, no dates it wasn't given. **And no site-visit slot.** |
| **Never asks "are you interested?"** | RON plan guardrail. It's a question that earns nothing and signals a script. |
| **Full audit** | Every answer logged with source chunks + document version. When a buyer says *"your bot told me ₹X"* — and one day one will — you can pull the conversation and the exact document that was live that day. In property, that's your defence. |

---

## 11. RAG infra

**pgvector on the existing Railway Postgres.** No new database, no new vendor, no new bill. The KB lives next to the leads, backed up with them, deployed with them.

### Tables

**Knowledge:**
- `brands` — one row per project. Adding one = a row, not a release.
- `kb_documents` — brochures, floor plans, FAQs, location data. **Versioned**, `active` flag, uploader recorded.
- `kb_chunks` — searchable pieces, each stamped with `brand_id` + embedding. **`brand_id` denormalised onto the chunk deliberately**, so retrieval is a single-table `WHERE` with no join to get wrong.
- `agents` — one row per project: persona, system prompt, price policy, qualifying checklist, **and the §7 reason-lines and framings.**

**Conversation:**
- `conversations` / `conv_turns` — state, 24h window expiry, checklist progress (so a returning ghost resumes rather than restarts), and every turn with its retrieved chunk ids.

**Rev 2 additions:**
- `lead_intake` — one row per lead per inflow: `brand_id`, `source` (ctwa / form / reactivation), campaign, `ctwa_clid`, `window_state`, and the **suppression verdict** with the stage/label it was checked against.
- `journeys` — knock-engine state per lead: which template set, which knock number, next-knock-due, stop reason (replied / opted out / suppressed / dormant), and the shared **fatigue counter**.
- `suppression_rules` — the config list of blocking Sell.do stages and labels. Data, not code.

**One engine, projects as configuration.** There is no "RON codebase."

**Plus: a corpus upload screen for sales.** Non-negotiable given they own it (§3).

### Technical defence — pgvector over Pinecone/Weaviate

The corpus is tens of documents per project, not millions. A specialist vector store buys scale we will never use, at the cost of a second system to deploy, secure, back up, and keep in sync with the leads it serves. The brand fence is a `WHERE` clause on the same database that already knows which project a lead belongs to; split them across two systems and the fence becomes a distributed-consistency problem instead of a join. If a project ever exceeds ~100k chunks, pgvector's HNSW index still handles it. Migrate when that's real, not in anticipation.

---

## 12. The structural change required underneath

**The current app cannot host a real-time bot.**

It is a single process, one gunicorn worker, four threads, with APScheduler running *inside* the web server (`Procfile`, `app.py:367-369`). `workers=1` is load-bearing — a second worker would double every send.

That was a smart, tight choice for a 3-day event. But an LLM turn takes seconds, and Wati's webhook wants an instant `200 OK`. Four threads + a burst of inbound CTWA messages = timeouts and dropped replies, which look to Wati like a broken integration.

**Fix:** the webhook writes the message to a queue and returns `200` immediately. A separate worker picks it up, thinks, and replies. Same box, same Postgres, one new process.

**Rev 2 raises the stakes.** With three inflows and a knock engine firing scheduled sends, the queue is no longer just about LLM latency — it's the only thing keeping scheduled sends and live conversation from colliding inside one thread pool.

**This changes the process model. Owner nod required before touching it.**

---

## 13. STILL NEEDED — the actual bar

### Now answered

| Slot | Answer | Source |
|---|---|---|
| **BUDGET** | **₹1.5–6 crore** band. Floor = ₹1.5cr. | RON nurturing plan |
| **Off-category (config rejects)** | Commercial ruled out — *"There is no such plans from the firm"* | RON FAQ row 26 |
| **Price publication** | **Nothing published** until approved → budget gate is internal arithmetic, every price question escalates | RON plan, commercial rule |
| **Cadence** | 4 knocks, day 0/3/10/25 | owner |
| **Escalation behaviour** | Bot stops talking, flags a human, never improvises | rev 1 |

### Still open

- **LOCATION line** *(hard gate)* — we know where RON **is** (Vadanemmeli, ECR, under 10 min from Kovalam, ~2 min before Sheraton Grand). We do **not** know which buyer-stated locations count as a fit. Does an OMR hunter reject? Tambaram? **Owner owns this and is supplying a dedicated location file — "a data point from every possible angle."** Blocks the location gate and fills the three blank FAQ rows below.
- **CONFIG list** *(soft gate)* — **owner owns this.** ⚠ And there is a **live conflict to resolve**: the nurturing plan scopes *2 & 3BHK apartments + 3 & 4BHK villas*, but the FAQ describes 1BHK and C2BHK apartments (Phase 1), villaments in 2/3/4BHK, 5BHK island villas and beachfront villas (Phase 3). **A buyer asking for a 1BHK or a 5BHK island villa — in scope, or off-category?** The two source documents disagree.
- **Escalation destination** — same WhatsApp group as qualified leads, or separate? (§8)
- **Campaign + lead-source specifics** — which campaigns feed which inflow. Owner supplying.
- **Suppression stage/label list** — which Sell.do stages and labels block a knock. Owner supplying. **Verify labels exist in the read-only mirror first** (§5).
- **Template approval** — 4 templates, owner handling. Utility framing preferred for reactivation (§4d).
- **`ctwa_clid` pass-through** — verify by eye with a real CTWA webhook payload (§4).
- **FAQ row 7 must be resolved before ingestion** — it holds two size ranges: *"1220–2133 Sqft (For Sales Person)"* and *"818–2133 Sqft (Actual)."* An internal-only distinction inside an otherwise usable row. **Which range may the bot say out loud?** One pair of numbers, please, before this reaches a corpus.
- **Sell.do write API** — **parked** by owner. The read side is now load-bearing for suppression.

---

## 14. Build order

| Phase | What | Notes |
|---|---|---|
| **0** | **Safety.** Retry ceiling, permanent opt-out, fatigue cap, delivery-status tracking, remove dead carnival wiring. | **Do it now, and it is no longer hygiene.** The send path is currently inert (§15), so send logic can be rewritten **without a single message reaching a real person** — that window closes the moment anything is switched on. And rev 2 loads this system far harder than rev 1: 4 templates × a cold reactivation list is heavier than the carnival blast that produced 44% blocked. **If the number gets restricted, all three inflows die at once — including CTWA, which works fine today and would be collateral damage.** |
| **1** | **CTWA ingestion.** Verify the webhook payload by eye. Capture `ad_id` → project, `ctwa_clid` → the flywheel. | Blocking unknown, cheap to answer, unrecoverable if missed. |
| **2** | **KB infra + curation.** pgvector, the tables, the ingest pipeline. Load RON. **Prove the fence** — ask the RON agent a question about another project and watch it come back empty. | Nothing downstream is safe until the fence is proven. Curation is real work here, not a formality — see §16. |
| **3** | **The queue.** Webhook/worker split (§12). | Prerequisite for any real-time bot, and now for send/receive isolation too. |
| **4** | **Intake + suppression gate.** Three inflows land in `lead_intake`; nothing may be knocked before it's checked. | **Needs the suppression list.** Sequence it before the knock engine — the interlock must exist before the thing it interlocks. |
| **5** | **The knock engine.** 4 knocks, day 0/3/10/25, one fatigue counter, resume-not-restart, dormant at 31. | **Moved up from last place in rev 1.** It is now the only way form leads and old leads ever receive a first message. **Needs approved templates.** |
| **6** | **The qualifier agent.** Three gates + timeline capture, answer-before-ask, the persuasion ladder, guardrails, three exits + loop-back. | **Needs §13** — location line and config list. |
| **7** | **The handoff.** WhatsApp group ping + card. Transcript stored. | Simple now that Sell.do write is parked. |
| **8** | **Corpus upload screen for sales.** | Protects the buy-in mechanism (§3). |
| **9** | **The flywheel.** CAPI conversion events back to Meta. | Only possible if Phase 1 found `ctwa_clid`. |
| **10** | **Sell.do write** — add as a second handoff sink. | **Parked.** Whenever credentials arrive. Nothing is lost meanwhile — the transcript is stored from Phase 7 onward. |

**External dependencies, all outside engineering's control, all needed before the phase that uses them:**

| Dependency | Blocks | Owner |
|---|---|---|
| 4 approved templates | Phase 5 | owner (in progress) |
| Location file | Phase 6 | owner |
| Config / off-category list | Phase 6 | owner |
| Suppression stage + label list | Phase 4 | owner |
| Escalation destination | Phase 7 | owner |
| FAQ row 7 decision | Phase 2 | owner |
| Sell.do write credentials | Phase 10 | vendor (parked) |

---

## 15. Facts about the current codebase

- **The send path is currently INERT.** Every send loop has an `if today > last_event_day: skip` guard (`sequencer.py:337`, `:363`, `:185-189`). The carnival ended 12 July; on 13 July the system turned itself off. Nothing is sending. **No fire to put out — and a free window to rewrite the send logic safely.**
- **`config.EVENT_DATES` is the load-bearing constant of the entire lifecycle.** Reply parsing indexes into it *by position* (`parser.py:25-27` — a reply of "1" means "carnival day one"), and three separate send guards compare against `EVENT_DATES[-1]`. **`parser.py` is a rewrite, not a config change.**
- **Every existing template is useless.** All six in `config.py:48` are carnival copy (`gtb_m1_ron_final`, `gtb_m2_followup_final`, `gtb_m3_reminder`, …). The 4 nurture templates are net-new and need Meta approval.
- **No delivery tracking exists.** `wati.parse_inbound` explicitly discards all status callbacks (`wati.py:161-165`). The "44% blocked" figure came off the Wati dashboard, **not from this system.**
- **No retry ceiling exists.** `send_attempts` is incremented but deliberately never used to suppress (`sequencer.py:252-258`) — a decision made when templates were pending approval and every send failed for reasons unrelated to the lead.
- **No opt-out path exists.**
- **No fatigue cap exists.**
- **Sell.do is read-only by construction** — `selldo.py:30` sets `readonly=True`; it's a reporting-DB mirror, not the CRM API. `sql/selldo_leads.sql` reads **stage only**; labels unverified.
- **No lead scoring/tiering exists.** "Tier" in this codebase refers only to the WhatsApp number's daily messaging tier (250/day), not lead quality.
- **`PROMOTE_ENABLED` defaults to `false`** (`config.py:109`); **`WALKIN_ENABLED` defaults to `false`** (`config.py:128`).
- Message copy, day-picker strings, and the ads time-range are **hardcoded literals that do not derive from `EVENT_DATES`** and will not follow a date change.
- Docs `RUNBOOK.md` and `GTB-Carnival-System-Documentation.md` are **stale** — they describe Wasender and a generic-M3 blast, neither of which is true of the current code.

---

## 16. Source material — and its condition

### `RON Faqs.xlsx` — 111 rows

Triaged 2026-07-28:

| Bucket | Count | Disposition |
|---|---|---|
| Usable as buyer-facing answers | **90** | ingest |
| Blank — no answer at all | **6** | **excluded** (owner: ignore) |
| Written for a human, not a buyer | **15** | **excluded** (owner: ignore) |

The 15 excluded include *"KK to come up with an answer"* (ROI, resale value), *"Question not understood"*, *"To be Discussed"*, *"Is this Question really necessary"*, and pointers like *"Shared on email"* / *"Refer to the spec sheets."* **These must not be ingested** — a bot with them in its corpus reads internal chatter aloud to a buyer.

**Consequence to accept:** those 21 topics have no source, so the confidence floor fires and they **escalate to a human by design** (§10). That is correct behaviour, not a gap — but it is human workload.

⚠️ **The blanks are the questions buyers ask first.** Rows 40, 41, 42 — distance to schools / offices / hospitals / public transport, nearby IT corridors, metro stations. All blank. Meanwhile **location is a hard gate** and **nurture touch 2 is entirely about location reassurance.** The most-asked topic in the funnel currently has an empty folder. **The owner's forthcoming location file is the fix, and it is on the critical path for Phase 6.**

⚠️ **Row 7 needs a decision before ingestion** — see §13.

### `Republic_of_Nature_WhatsApp_Nurturing_Plan (2).docx`

Marketing's 4-week journey. **Adopted:** the four touch themes and their copy, the objection-response library, the claims guardrails (beach/lagoon, no unapproved commercials, never ask *"are you interested?"*, max two per week), and the suppression principles.

**Not adopted:** every Wati-native mechanism in it — keyword routing, the booking chatbot, Wati journey state. Those move to our engine (§4c). And touches 5 and 6 are dropped (§6).

**Also useful from it:** the measurement plan (delivery rate, positive-reply rate, location-request rate, stop-update rate, visits booked, show-up rate) is a reasonable instrumentation spec for Phase 7 onward, and stop-update rate in particular is the early-warning signal for the block risk in §4d.

---

## 17. The trap to keep re-reading

**A good answering engine will kill your numbers.**

Build great RAG and the bot will cheerfully answer twenty questions — floor plan, possession date, distance to the metro, depth of the lagoon — and the lead will finish the conversation **fully satisfied, fully informed, and never qualified.** You will have built a magnificent, expensive brochure.

**The agent's goal is a qualified lead, not a satisfied customer.**

Rev 2 has to hold this together with two decisions that pull the other way — *answer before you ask* (§9) and *persuade gently, never stop being useful* (§7). The reconciliation is precise, and it is not "answer less":

> **Every turn must carry an ask. The ask rides on top of the answer.**

Answering is not the concession — answering is how the ask gets earned. What is forbidden is a turn that answers and **doesn't** ask, because the 24-hour window is finite and a bot that only answers is a brochure with a typing indicator.

And the counter in §7c is what stops the opposite failure: after three unreciprocated answers, a human gets flagged. **The bot never wins the argument by refusing to help — it hands over.**

---

## 18. Artifacts

- **Flow chart (visual):** published to the **aiprojects@bharathimeraki.com** Claude account —
  https://claude.ai/code/artifact/7a8e285e-a2c3-4cd2-abe3-72001a797c7c
  **A different Claude account cannot open or update it.** Source is preserved at
  `docs/lead-flow.html` — republish from there.
  **⚠️ Now stale** — it shows the rev 1 single-inflow flow and four exits. Needs redrawing for §5.
- **Rev 1 of this document:** `docs/POST-CARNIVAL-DESIGN.2026-07-13.md`
- **Source material:** `../../RON Faqs.xlsx` · `../../Republic_of_Nature_WhatsApp_Nurturing_Plan (2).docx`
