# Post-Carnival Redesign — The Qualification Machine

**Status:** Design complete and agreed. **NO CODE WRITTEN YET.**
**Date:** 2026-07-13 (carnival ran 10–12 July 2026, now over)
**What this is:** the bot replaces the presales team. It qualifies leads and hands sales only the ones that clear a bar sales themselves agreed to.

---

## 1. The reframe

The carnival system was an **invite machine**: one-shot, time-boxed, single CTA, urgency did the work.

The new system is a **qualification machine**. It is **not** a nurture drip.

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
| **Location means** | **Where they want to BUY** — the micro-market they're shopping in. A fit filter, not a reachability filter. If Elements is at Vandalur and they're hunting on OMR, they are not a fit however much money they have. |
| **Configuration** | **Soft gate.** Rejects **only if wildly off-category** (they want a plot, a shop, a rental — something we simply don't sell). A 2BHK-vs-3BHK gap does **not** reject: sales can flex inventory, upgrade, restructure the payment plan — *"they have tools in their hand."* Passed to sales flagged. Ambiguous cases → escalate. |
| **Purpose** (end-use vs investment) | **Captured, NEVER gates.** Not a filter — **a lens.** It reframes every answer the bot gives afterwards, and tells sales which pitch to open with. |
| **Timeline** | **Captured, NOT gated.** Bot asks, records it, hands it to sales as context. Sales prioritises. |
| **Ask order** | **Purpose → Location → Configuration → Budget.** (NOT the same as gate priority — see below.) |

### The gate table

| Slot | Can it reject? | Notes |
|---|---|---|
| **Budget** | **HARD — yes** | The sharpest filter. |
| **Location** (where they want to buy) | **HARD — yes** | Wrong micro-market = not a fit, however rich. |
| **Configuration** | **Only if wildly off-category** | Plot / commercial / rental → reject. 2BHK-vs-3BHK → **pass to sales, flagged.** Ambiguous → escalate to a human. |
| **Purpose** | **Never** | A **lens**, not a filter. See below. |
| **Timeline** | **Never** | Context for sales to prioritise on. |

**Only budget and location can send a lead to the dead lane.** Config needs one KB fact per brand: *what category do we actually sell?* In-category-but-not-in-stock is a sales problem, not a rejection — sales can flex inventory, upgrade, restructure the payment plan. *"They have tools in their hand."*

### Purpose is a lens, not a gate

End-use and investment are **both real buyers with real money.** Neither gets rejected. But they want opposite things, and the bot must know which before it opens its mouth:

- **End-user** → schools, commute, possession date, EMI, which floor gets the light.
- **Investor** → rental yield, appreciation, resale liquidity, exit horizon. **He does not care about the playground.**

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

**And a useful accident falls out of it.** Budget is asked *last*, so most ghosts go silent **before** the budget question — meaning the typical ghost is someone whose **purpose, location and configuration we already know.** That hands the ghost re-open template its own script:

> *"You were looking at a 3BHK in Vandalur — shall I send you the price range?"*

That message is simultaneously **(a)** the budget question in disguise and **(b)** shaped exactly like a **UTILITY** template — a follow-up to something *they* asked, not a promotion. Which is precisely the category that dodges Meta's block rate (§4b). **The ghost lane's biggest weakness has a natural answer built into the ask-order.**
| Does the bot book the site visit? | **No.** Sales calls and closes for the visit. Bot warms it, doesn't book it. |
| Has sales agreed the bar? | **Yes.** Sales agreed the definition. *(Actual thresholds still to be supplied — see §10.)* |
| Handoff destination | **Both:** write to **Sell.do** (the record) **and** ping a **WhatsApp group** (the interrupt) |
| Primary channel | **CTWA** (Click-to-WhatsApp ads) to start; other campaign types later |
| **Ghosts** (started, then went silent) | **Chase them.** One template re-open, then two more spaced tries. Bot resumes mid-checklist. Then dormant. |
| Cross-brand routing (budget/location fits the *other* project) | **No. Stay in lane.** Fail them out instead. |
| Price policy | **Range only, never exact.** |
| RAG engine | **Build our own.** Do NOT use Wati's Astra. |
| Architecture | Multi-brand, **ring-fenced KB per brand**, one qualifier agent per brand |
| Brands | **RON + Elements only for now.** Designed so a third is a config row, not a release. |
| Who owns the knowledge base | **Sales.** (They know what's true and sellable today.) |

### Why the bar is Budget · Configuration · Location — and not Timeline

All three gates are **facts**: checkable, stable, hard to fudge. **Timeline is a mood.** Everyone says "soon." Gating on the one slot buyers routinely misreport is how you reject real buyers who were merely being honest about being six months out.

So timeline is captured as **intelligence for sales**, not as a gate. Sales sees "ready in 3 months" vs "ready in 14" and prioritises accordingly.

**Consequence:** the earlier "not now / park with a date" lane is **gone** — nobody fails on timing anymore, so a far-out buyer with the right money, config and location goes straight to sales with his timeline attached. **Reversible:** if sales later complains the queue is full of far-out browsers, timeline can be promoted to a gate.

### Why we rejected Astra (Wati's own RAG/agent tool)

Astra is real and capable — RAG over synced docs, webhook tool-calling, escalation to the Wati Team Inbox (https://support.wati.io/en/articles/13193160). Rejected because it would put the **price policy, the brand isolation, and the escalation rules in a browser tool outside git** — unversioned, unreviewed, editable by anyone, no audit trail. For a multi-brand setup that is a cross-brand leak waiting to happen.

### Recorded cost of "no cross-brand routing"

Choosing clean attribution and a clear buyer experience over recovering mismatched leads. A buyer too rich for Elements but right for RON — or hunting in RON's micro-market having clicked an Elements ad — is failed out rather than transferred. **Reversible call** — if volume shows that pile is large, revisit.

---

## 3. The political mechanism — why this one might actually survive

The standard way a marketing-built qualification system dies: **sales doesn't trust the filter, so sales ignores it and calls everyone anyway.** The machine gets built, presales gets removed, and nothing changes — except now there's no fallback.

The defence here is structural, and it's already in place:

**Sales owns the bar. Sales owns the knowledge base.**

They set the definition of qualified. They feed the bot its facts. So when a lead lands in their WhatsApp group, they cannot say *"marketing's bot sent me rubbish"* — it filtered by **their** bar and said what **they** told it to say.

**This must be protected in the build.** Whatever we ship must keep sales' hands visibly on both. Which means: sales are not engineers, so "sales owns the corpus" requires **a screen they can drop a PDF into and see what changed** — not a script, not a CLI. That is real scope, and it decides whether the bot's knowledge is fresh in six months or quietly two quarters stale.

---

## 4. CTWA changes the front door — but not as completely as it first looks

**Click-to-WhatsApp means the buyer messages us first.** That deletes the hardest problem in the earlier design:

- The 24-hour free-text window **opens on arrival**, free.
- **No opener template. No Meta MARKETING gate. No 44% block rate** — *on first contact.*
- **Wati surfaces the ad**: it auto-creates `source_id` (the Ad ID) and `source_url` on the contact. So brand stamping is *exact* — we know the ad, therefore the brand, at first contact. No guessing from message text.

**But CTWA moves two problems rather than solving them.**

**(a) It moves the sorting problem.** With lead forms, the reply *was* the filter — junk never replied, and we got that sort for free. **With CTWA, everyone at the door has already "replied."** Tyre-kickers and buyers look identical on arrival. There is no reply-fork to hide behind: **the bot itself is now the entire filter.**

**(b) It moves the template problem to the recovery path.** See §5 — the ghost lane. The window shuts after 24 hours of silence, and re-opening it **costs a template.** Meta's approval gate comes straight back. CTWA didn't remove the template tax; it moved it from the front door to the second knock. **The UTILITY-vs-MARKETING category lever therefore still matters — just later in the funnel.**

### The flywheel (do not miss this at ingestion)

Meta puts a **click ID (`ctwa_clid`)** in the `referral` object of its webhook. Capture it, and when a lead qualifies you can fire a Conversions API event back to Meta — `action_source: "business_messaging"`, `messaging_channel: "whatsapp"`, carrying the `ctwa_clid`.

Meaning: **you tell Meta "this one qualified," not "this one clicked."** The algorithm stops optimising for cheap clicks and starts hunting people who actually qualify. Ad spend gets smarter every week, automatically, with nobody touching a dial.

**⚠️ BLOCKING UNKNOWN:** Meta puts `ctwa_clid` in *its* webhook. **Wati sits in between, and whether Wati forwards that field to us is undocumented.** Must be verified by eye: fire one CTWA click, catch the webhook, read the payload. If Wati strips it, the flywheel needs another path (direct Meta webhook, or Wati's own CAPI feature). **Everything about attribution hangs on this, and if we don't capture the click ID at ingestion it is gone forever.**

Sources: [Wati — identifying & tracking CTWA conversations](https://support.wati.io/en/articles/11463601-how-to-identify-and-track-click-to-whatsapp-ad-ctwa-conversations-and-track-performance) · [WOZTELL — CAPI for WhatsApp Ads](https://support.woztell.com/portal/en/kb/articles/wa-conversion-flow)

---

## 5. The flow

```
CTWA ad click ──────► THEY MESSAGE US FIRST
 (primary)            • 24h window already open, free
                      • ad_id → brand, stamped instantly, exactly
                      • ctwa_clid captured (the flywheel)
                      • no template on first contact
                      • bot starts qualifying IMMEDIATELY
                              │
                              ├──────────────┐
                              │              │
Lead form ──────────► opener template        │
 (later campaigns)    → did they reply? ─────┤
                      → no reply: slow lane  │
                                             ▼
                    ┌──────────────────────────────────────┐
                    │        THE QUALIFIER AGENT           │
                    │        (that brand's agent)          │
                    │                                      │
                    │  ASKS IN THIS ORDER:                 │
                    │   1. PURPOSE ..... never gates       │
                    │      (end-use vs investment)         │
                    │      → a LENS. Reframes every        │
                    │        answer from here on.          │
                    │   2. LOCATION .... HARD GATE         │
                    │      (where they want to BUY)        │
                    │   3. CONFIG ...... soft gate         │
                    │      (rejects only if wildly         │
                    │       off-category)                  │
                    │   4. BUDGET ...... HARD GATE         │
                    │      (asked last — it's earned)      │
                    │                                      │
                    │  + TIMELINE, opportunistically       │
                    │    → context for sales, never gates  │
                    │                                      │
                    │  Answers from that brand's KB only.  │
                    │  Ranges, never exact. Warms the      │
                    │  site visit — does not book it.      │
                    └──────────────────────────────────────┘
                                             │
     ┌──────────────┬────────────────────────┼──────────────────┬────────────────┐
     ▼              ▼                        ▼                  ▼                ▼
 QUALIFIED       GHOST                    DEAD           OUT OF DEPTH
 budget +      started, then           fails BUDGET      objection, price
 location      went silent.            or LOCATION.      pushback, hard Q
 clear;        Window shut.            Or config         with no good answer
 config ok     ⚠ LARGEST BUCKET        wildly off-               │
 or flagged         │                  category. Or              │
     │              │                  junk, STOP.               │
     ▼              ▼                        │                   ▼
• WRITE TO     Re-open with a               ▼            Bot STOPS TALKING.
  SELL.DO      TEMPLATE (Meta's        Suppressed        Escalates to a human,
  (the record) gate is BACK here)      permanently.      flagged. Does not
• PING THE     ×1, then 2 more         Never touched     improvise.
  WHATSAPP     spaced tries.           again, by         ⚠ These are
  GROUP        Bot RESUMES             anything.           disproportionately
  (the         mid-checklist                               the REAL BUYERS.
  interrupt)   if they return.
• Purpose +          │
  timeline +         ▼
  config note    Dormant
  ride along
     │
     ▼
 Sales calls,
 closes for the
 SITE VISIT ✓
```

### The ghost lane — the bucket nobody plans for

A CTWA lead clicks the ad, sends "hi", the bot asks about budget, and they stop. They gave us one slot out of three.

They are not qualified. They are not dead — **they clicked a property ad and started a conversation.** They are **incomplete**, and on a click-to-WhatsApp campaign **this will be the single largest bucket in the system.** Bigger than qualified. Bigger than dead. **This is where most of the ad spend lives.**

**Decision: chase them.** One template re-open ("you asked about the 3BHK — shall I send the layout?"), then two more spaced tries. If they come back, **the bot resumes exactly where it left off** — it does not restart the checklist. Then dormant.

**This is why the template-category work (MARKETING → UTILITY) is still core, not optional.** It just applies to the second knock rather than the first.

### Why both Sell.do *and* the WhatsApp group

They do different jobs and you need both:
- **Sell.do is the record** — durable, searchable, where sales already lives.
- **The WhatsApp ping is the interrupt** — fast, gets someone moving today.

Record without interrupt = leads rot in a CRM tab. Interrupt without record = leads get lost in a group-chat scroll.

**⚠️ DEPENDENCY:** today this system only ever **reads** from Sell.do — a read-only connection into their reporting database (`selldo.py:28-35`). **There is no write path.** Pushing a qualified lead *into* Sell.do needs their write API, which means credentials and access we do not currently have. Not hard, but it's a request to someone outside the team, and those take days. **Start this now, not in week three.**

---

## 6. Inside one bot turn — where the ring-fence lives

```
1. Lead sends a message
2. LOCK BRAND — read brand_id FROM THE LEAD RECORD (stamped from the ad at ingestion),
   never from the message text
3. RETRIEVE — WHERE brand_id = lead.brand   ← the fence. A DB filter, not a prompt.
4. CONFIDENCE FLOOR — nothing solid retrieved? DO NOT answer from general
   knowledge (that's where invented possession dates come from). Escalate.
5. GENERATE — answer from retrieved chunks only; ranges never exact figures;
   advance the budget/config/location checklist; capture timeline if it surfaces;
   warm the site visit
6. LOG — store the reply WITH the exact chunk ids and document versions that produced it
```

---

## 7. Guardrails — all mechanical, none prompt-only

| Guardrail | How it's enforced |
|---|---|
| **Brand fence** | `WHERE brand_id = %s`, sourced from the lead record. Cross-brand leakage is *impossible*, not merely unlikely. **Never a prompt instruction** — models drift, buyers ask weird things. Reinforced by the "no cross-brand routing" decision: a lead lives and dies in its brand. |
| **Price = range only** | **By curation.** Ranges go into the corpus; exact cost sheets stay **OUT**. You cannot reliably instruct a retrieval bot to withhold a number sitting in its own knowledge base — eventually, to someone, it says it. **What isn't in the corpus cannot be said. Policy becomes physics.** |
| **No source, no answer** | Confidence floor. Below it → refuse + escalate. Never fall back on the model's own knowledge. |
| **Retry ceiling** | Replaces today's loop, which retries a failed send **every 5 minutes, forever**. There is no retry scheduler — a failed send leaves `wa_state` unchanged so the next tick re-picks it (`sequencer.py:252-268`). **The event ending is the ONLY thing that ever stopped it. This is a live landmine.** |
| **Opt-out** | "STOP" honoured permanently, across every brand. **Does not exist today.** |
| **Fatigue cap** | Hard lifetime limit on messages per person. Bounds the ghost-chase too. |
| **Scheduler stand-down** | While a lead is in a live conversation, the scheduled-message system never touches them. Otherwise a warm bot reply and a cold template land the same afternoon. |
| **Never commits** | No discounts, no promises, no dates it wasn't given. **And no site-visit slot** — the bot doesn't book. |
| **Full audit** | Every answer logged with source chunks + document version. When a buyer says *"your bot told me ₹X"* — and one day one will — you can pull the conversation and the exact document that was live that day. In property, that's your defence. |

---

## 8. RAG infra

**pgvector on the existing Railway Postgres.** No new database, no new vendor, no new bill. The KB lives next to the leads, backed up with them, deployed with them.

Proposed tables:
- `brands` — RON, Elements. Adding a third = a row, not a release.
- `kb_documents` — brochures, floor plans, FAQs. **Versioned**, `active` flag, uploader recorded.
- `kb_chunks` — searchable pieces, each stamped with `brand_id` + embedding. **`brand_id` denormalised onto the chunk deliberately**, so retrieval is a single-table `WHERE` with no join to get wrong.
- `agents` — one row per brand: persona, system prompt, price policy, qualifying checklist.
- `conversations` / `conv_turns` — state, the 24h window expiry, checklist progress (so a returning ghost resumes rather than restarts), and every turn with its retrieved chunk ids.

**One engine, brands as configuration.** There is no "RON codebase" and "Elements codebase."

**Plus: a corpus upload screen for sales.** Non-negotiable given they own it (§3).

### Technical defense — pgvector over Pinecone/Weaviate
The corpus is tens of documents per brand, not millions. A specialist vector store buys scale we will never use, at the cost of a second system to deploy, secure, back up, and keep in sync with the leads it serves. The brand fence is a `WHERE` clause on the same database that already knows which brand a lead belongs to; split them across two systems and the fence becomes a distributed-consistency problem instead of a join. If a brand ever exceeds ~100k chunks, pgvector's HNSW index still handles it. Migrate when that's real, not in anticipation.

---

## 9. The structural change required underneath

**The current app cannot host a real-time bot.**

It is a single process, one gunicorn worker, four threads, with APScheduler running *inside* the web server (`Procfile`, `app.py:367-369`). `workers=1` is load-bearing — a second worker would double every send.

That was a smart, tight choice for a 3-day event. But an LLM turn takes seconds, and Wati's webhook wants an instant `200 OK`. Four threads + a burst of inbound CTWA messages = timeouts and dropped replies, which look to Wati like a broken integration.

**Fix:** the webhook writes the message to a queue and returns `200` immediately. A separate worker picks it up, thinks, and replies. Same box, same Postgres, one new process.

**This changes the process model. Owner nod required before touching it.**

---

## 10. STILL NEEDED — the actual bar

Sales has agreed a definition. **We do not yet have the numbers.** These answers *are* the bot; everything else is plumbing around them.

- **BUDGET** *(hard gate)* — what's the floor, per brand? What must a RON buyer have? An Elements buyer? Hard cutoff or a band?
- **LOCATION** *(hard gate)* — which micro-markets count as a fit, per brand? (e.g. Elements = Vandalur — does "Tambaram" count? "OMR"?) **Where exactly do we draw the line?**
- **CONFIGURATION** *(soft gate)* — two separate facts needed: **(a)** what's sellable *right now* per brand (also goes in the KB), and **(b)** what counts as **"wildly off-category"** — i.e. what do we simply not sell, at all? (Plots? Commercial? Rentals?) Only (b) rejects.

Also still open:
- **Where does an escalation land?** ("Out of depth" — objection, price pushback.) Same WhatsApp group, or a separate one? **These are disproportionately the real buyers, so this queue cannot be unwatched.**
- **Sell.do write API** — credentials and access. Start the request now (§5).
- **`ctwa_clid` pass-through** — verify by eye with a real CTWA webhook payload (§4).

---

## 11. Build order

| Phase | What | Notes |
|---|---|---|
| **0** | **Safety.** Retry ceiling, opt-out, fatigue cap, delivery-status tracking, remove dead carnival wiring. | Do it **now**: the send path is currently inert (§12), so send logic can be rewritten **without a single message reaching a real person.** That window closes the moment anything is switched back on. |
| **1** | **CTWA ingestion.** Verify the webhook payload by eye. Capture `ad_id` → brand, and `ctwa_clid` → the flywheel. | Blocking unknown. Cheap to answer. |
| **2** | **KB infra.** pgvector, the tables, ingest pipeline. Load RON + Elements. **Prove the fence** — ask the RON agent an Elements question and watch it come back empty. | Nothing downstream is safe until the fence is proven. |
| **3** | **The queue.** Webhook/worker split (§9). | Prerequisite for any real-time bot. |
| **4** | **The qualifier agent.** Three gates + timeline capture, guardrails, four exits. | Needs §10 first. |
| **5** | **The handoff.** Sell.do write + WhatsApp group ping. | Needs Sell.do API access. |
| **6** | **The ghost lane.** UTILITY template re-open ×3, resume mid-checklist. | **Core, not optional** — this is the largest bucket. Brings the MARKETING→UTILITY template work back in scope. |
| **7** | **Corpus upload screen for sales.** | Protects the buy-in mechanism (§3). |
| **8** | **The flywheel.** CAPI conversion events back to Meta. | Only possible if Phase 1 found `ctwa_clid`. |
| **9** | **Lead-form opener** (UTILITY template) — only when non-CTWA campaigns start. | Reuses Phase 6's template work. |

---

## 12. Facts about the current codebase

- **The send path is currently INERT.** Every send loop has an `if today > last_event_day: skip` guard (`sequencer.py:337`, `:363`, `:185-189`). The carnival ended 12 July; on 13 July the system turned itself off. Nothing is sending. **No fire to put out — and a free window to rewrite the send logic safely.**
- **`config.EVENT_DATES` is the load-bearing constant of the entire lifecycle.** Reply parsing indexes into it *by position* (`parser.py:25-27` — a reply of "1" means "carnival day one"), and three separate send guards compare against `EVENT_DATES[-1]`. **`parser.py` is a rewrite, not a config change.**
- **No delivery tracking exists.** `wati.parse_inbound` explicitly discards all status callbacks (`wati.py:161-165`). The "44% blocked" figure came off the Wati dashboard, **not from this system.**
- **No retry ceiling exists.** `send_attempts` is incremented but deliberately never used to suppress (`sequencer.py:252-258`) — a decision made when templates were pending approval and every send failed for reasons unrelated to the lead.
- **No opt-out path exists.**
- **No lead scoring/tiering exists.** "Tier" in this codebase refers only to the WhatsApp number's daily messaging tier (250/day), not lead quality.
- **`PROMOTE_ENABLED` defaults to `false`** (`config.py:109`); **`WALKIN_ENABLED` defaults to `false`** (`config.py:128`).
- Message copy, day-picker strings, and the ads time-range are **hardcoded literals that do not derive from `EVENT_DATES`** and will not follow a date change.
- Docs `RUNBOOK.md` and `GTB-Carnival-System-Documentation.md` are **stale** — they describe Wasender and a generic-M3 blast, neither of which is true of the current code.

---

## 13. The trap to keep re-reading

**A good answering engine will kill your numbers.**

Build great RAG and the bot will cheerfully answer twenty questions — price, floor plan, possession date, distance to the metro — and the lead will finish the conversation **fully satisfied, fully informed, and never qualified.** You will have built a magnificent, expensive brochure.

**The agent's goal is a qualified lead, not a satisfied customer.** It answers *enough* to earn the right to ask its three questions. **Every turn must advance the checklist.** An answer that doesn't move budget, configuration or location forward is a turn the bot wasted — and the 24-hour window is finite.

---

## 14. Artifacts

- **Flow chart (visual):** published to the **aiprojects@bharathimeraki.com** Claude account —
  https://claude.ai/code/artifact/7a8e285e-a2c3-4cd2-abe3-72001a797c7c
  **A different Claude account cannot open or update it.** Source is preserved at
  `docs/lead-flow.html` — republish from there.
