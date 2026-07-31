# Build Plan — 29 tasks

**Created:** 2026-07-28. Derived from `POST-CARNIVAL-DESIGN.md` §14 (rev 2).
**Status (2026-07-31):** **15 of 29 done.** The conversation loop is complete and tested end to end. ~3,400 lines, 21 modules.

Nothing is deployed. `SEND_ENABLED` defaults `false` in code and is set `false` in Railway, so merging this cannot message anyone.

---

## ▶ READY TO MERGE — PR #4

`https://github.com/bm-aip/gtb-carnival-nurture/pull/4` — 7 commits, 37 files.

### What works today

```
message arrives -> recorded + opt-out checked   (synchronous, milliseconds)
                -> queued                        (Postgres, per-phone ordering)
                -> retrieve from the brand-fenced corpus
                -> answer first, then ONE ask carrying an unused framing
                -> reply sent, logged with the chunk ids that produced it
                -> checklist advanced
                -> qualified / dead / escalated -> card to the two numbers
```

Verified against the live corpus and database, not mocks.

### Merge checklist

| | Step | Note |
|---|---|---|
| 1 | Confirm `SEND_ENABLED` is `false` in Railway | It already is. This is the only thing between the code and real phone numbers |
| 2 | Merge PR #4 | Railway builds automatically |
| 3 | Watch the deploy | `db.init_db()` + `kb.init_kb()` run at boot; both are additive and idempotent |
| 4 | Check `/admin/config-check` | Confirms which build is serving and every limit it is running with |
| 5 | Check `/api/kb` | Should report the corpus already loaded (it was loaded out-of-band) |

**Do not flip `SEND_ENABLED` in the same sitting as the merge.** Deploy, confirm the app is healthy, then decide separately.

### Before the first real conversation

**Both handoff recipients must message the bot number once.** WhatsApp only allows free-text to someone who contacted the business number in the last 24 hours — that rule applies to staff too. A card sent to a quiet handset is accepted by us and dropped by WhatsApp, silently from our side. Watch `/api/delivery` on the first card rather than assuming it arrived.

The durable fix is a utility template for cards, or Wati Team Inbox assignment. Both are small; neither is built.

### What is deliberately NOT in this PR

- **Knock engine (17)** — cold outbound. Blocked on the suppression list (`docs/SUPPRESSION-LIST.md`, waiting on ticks). Until it exists the bot can only answer, never start a conversation. For a first switch-on that is the right shape.
- **Intake for the three inflows (14)** — a lead must already exist in the database. A CTWA click from a stranger is logged as unattributed rather than becoming a conversation.
- **No schema columns dropped.** `leads.selected_date` and the carnival `wa_state` values still hold real attendance data.
- **`/webhook/wasender`** is still unauthenticated and can mutate lead records. Dead provider, live door — close it when task 14 reworks intake.

### Known and accepted

- **`kb/RON/pricing-internal.md` is gitignored.** The price sheet is never in git history; the budget band lives in `BUDGET_FLOOR` / `BUDGET_CEILING` because the gate is incomprehensible without it.
- **Handoff numbers are defaults in `config.py`.** Business contact numbers in a private repo, overridable by `HANDOFF_PHONES` / `ESCALATION_PHONES`. Move them to env-only if that is not wanted.
- **Flags accumulate across turns** rather than being per-turn — cosmetic on the card, noted rather than fixed.

---

## Build log

**Task 1 — split into 1a and 1b, in that order, for a load-bearing reason.**

The date guards *were* the off switch. `sequencer.py:185`, `:337`, `:363` skipped every send because today was past the last carnival day — that accident is what made the system inert. Deleting the carnival wiring therefore **re-arms the sender as a side effect**. So an explicit switch had to exist first.

| | Landed | Files |
|---|---|---|
| **1a** | `sendgate.py` — the one door. `SEND_ENABLED` env switch, default **false**. `_send()` and the admin test-send bypass both routed through it. | `sendgate.py` (new), `config.py`, `sequencer.py`, `app.py` |
| **1b** | Carnival lifecycle removed: `EVENT_*` constants, all six carnival templates, the M1/M2/M3 loops, copy banks, day-picker parsing, `_detect_project`. `tick()` is a documented no-op. Inbound is still recorded. | `config.py`, `sequencer.py` (rewritten), `meta.py`, `match.py`, `app.py`, `db.py` (comment), `templates/dashboard.html` |

Verified: all ten modules compile, the Flask app boots with 17 routes, 12 carnival symbols confirmed absent, `parser.py` imported by nothing, and the gate returns `(False, 'send_disabled')` with no env set.

**Task 5 — delivery ears (DONE 2026-07-30).**

The trap here: `mark_webhook_new(msg_id)` claims each message id once, and `sent` / `delivered` / `read` callbacks **all carry the same message id**. Routing statuses through the existing dedup unchanged would have kept the first and discarded the rest — the exact data loss the task exists to fix. Status events therefore dedup on `id + status`, and the table's uniqueness constraint is `(provider_msg_id, status)`.

Second principle: **an event we cannot classify is stored, not dropped.** Wati's event names are not stable across its own versions, so the parser matches keywords rather than an exact list, and anything unrecognised lands as `status='unknown'` with its full payload in `raw`. A parser that silently drops what it does not recognise repeats the original mistake in a more sophisticated form. `/api/delivery` reports the unknown count precisely so those shapes get fixed rather than forgotten.

| Landed | Where |
|---|---|
| `message_delivery` table, phone-keyed, `+ message_log.provider_msg_id` | `db.py` |
| `parse_status()`, `is_status_event()`, `extract_msg_id()`, `_parse_ts()` | `wati.py` |
| Status branch ahead of the inbound dedup | `app.py` `_wati_inbound` |
| Provider message id stored against every send | `sequencer.py` `_send` |
| `/api/delivery` — rollup, failure rate, unknown count, last 200 events | `app.py` |

Tested against 8 payload shapes (epoch-seconds, epoch-millis and ISO timestamps; nested under `data`; `status` field only; a failure carrying a reason; an invented future event name) plus 2 real customer messages that must **not** be claimed as statuses. Send-response id extraction covers flat, nested and non-JSON bodies.

Send-response truncation raised 300 → 1000 chars in `wati.py`, because the provider message id lives in that body and could truncate first.

**Task 2 — opt-out ledger (DONE 2026-07-30).**

Owner decision 2026-07-30: **"not interested" stops this project and keeps the record; it is not a permanent all-projects block.** So the ledger has two scopes.

| Scope | Set by | Effect |
|---|---|---|
| `global` | explicit stop words · the Stop-updates button · **wrong-number / by-mistake replies** | permanent, every project, forever |
| `project` | "not interested", "already bought", "not looking" | stops this project, record kept, another project may still reach them |

Wrong-number is `global` rather than `project` on purpose: it means this phone is not the lead at all, so no project has any business contacting it.

**Detection is deliberately dumb and must stay that way.** Phrase matching, no language model, running *before* any agent sees the message. An agent judging "did they mean stop?" will sometimes judge wrong, and wrong here means messaging someone who told us to stop. The qualifier (task 20) may later classify ambiguous cases into `project` scope, where a wrong call costs one project; it may **never** write a `global` row.

**The false positive that mattered.** First test run classified *"Please remove me from the waitlist for 2BHK"* as a permanent all-projects block — an engaged buyer, silenced forever. Fixed by splitting an ambiguous family (`remove me`, `take me off`) that stands down when the message mentions something we sell. The asymmetry is intentional: a hard stop is **not** downgraded by product context — *"stop sending me 2BHK offers"* still means stop. Failing to block someone who meant it is recoverable; they will say it again more bluntly. Permanently blocking a live buyer is not.

Final: **32/32 cases, 0 misses, 0 false positives** — including "bus stop", "non-stop drive", "stop by the office" and "block a unit".

Landed: `optout.py` (new), `optouts` table with a `COALESCE`-based uniqueness index, `sendgate.check()` now consults the ledger, detection runs on every inbound **even when the sender matches no lead**, `/api/optouts`, `/admin/optout`.

**No route removes an opt-out.** Lifting one is a manual database action by a human who has thought about it.

**Correction to task 1a's claim:** `sendgate.check()` gained a `project` parameter, so that signature was not frozen after all. Opt-out has a per-project scope and a caller that cannot name its project is treated as blocked by **any** project row rather than waved through — passing `None` is never permissive.

**Task 3 — fatigue cap (DONE 2026-07-30).**

Owner chose **"a new reason resets the counter"** over a hard lifetime ceiling. That recovers buyers who resurface, but "a new reason" is loose and easy to define generously, so on its own it is a way to message somebody forever. Made safe with **two ceilings, one of which nothing can reset**:

| Ceiling | Value | Resettable |
|---|---|---|
| Journey — the day 0/3/10/25 sequence | `KNOCK_MAX_PER_JOURNEY` = 4, per (phone, project) | **yes**, by a defined reason |
| Window — rolling, per person, all projects | `FATIGUE_MAX_PER_WINDOW` = 2 per `FATIGUE_WINDOW_DAYS` = 7 | **never** |

The window ceiling is what makes a generous reset harmless: reset as often as you like, nobody gets a burst. Verified in test — a lead at 4 knocks and 2-this-week is still blocked *after* a reset. It is also the RON plan's own guardrail ("never more than two nurture messages in the same week"), so it is not new policy.

**Neither count is stored as a number.** Both are computed from `message_log`. A stored counter can be zeroed by a bug, a migration or a well-meaning manual edit, and the failure is silent — the system carries on believing it messaged someone twice when it messaged them nine times. The only mutable fact is where a journey *starts*, and every move of it writes to `journey_resets` with a reason and the count it superseded.

**Reset reasons are a fixed list** — `form_fill`, `ctwa_click`, `human`, `import`. Free text would make the audit trail unqueryable and let "a new reason" mean whatever the caller felt like.

**Replies are never fatigue.** Only `msg_type` starting `knock` counts. A reply inside a window the customer opened costs no messaging tier and throttling it would mean going silent mid-conversation — worse than the problem being prevented. Verified: non-proactive types pass at 99 prior sends.

**Lifetime total is reported, not enforced** (`/api/fatigue`). The owner picked resettable over a lifetime ceiling; this makes a person who accumulated twenty messages across four resets visible rather than invisible.

Landed: `fatigue.py` (new), `knock_journeys` + `journey_resets` tables, fatigue check wired into `sendgate.check()` after opt-out, `/api/fatigue`.

Gate order is now: **1** master switch → **2** operator pause → **3** opt-out → **4** fatigue. Opt-out is evaluated before fatigue so the logged reason records the permanent fact rather than one a reset would clear.

**Task 4 — retry ceiling (DONE 2026-07-30). PHASE 0 COMPLETE.**

A naive three-strike rule would have been wrong, and the deleted comment at the old `sequencer.py:252` said why: while templates were pending approval **every** send failed for reasons unrelated to the lead, so strikes would have killed off good buyers. The ceiling therefore classifies whose fault the failure was.

| Class | Examples | Ceiling |
|---|---|---|
| `recipient` | not on WhatsApp · blocked us · WhatsApp code 131050 | `RETRY_MAX_RECIPIENT` = 3 |
| `transient` | timeout · 5xx · 429 · **anything unrecognised** | `RETRY_MAX_TRANSIENT` = 6 |
| `system` | template unapproved · bad token · 24h window shut · account restricted | **none, by design** |

**Unrecognised failures class as `transient`, never `recipient`.** If Wati invents new wording the failure mode is "we keep trying a bit longer", not "we silently discard a buyer".

**Suppress-on-first is narrower than the class.** Only *"this number has no WhatsApp at all"* suppresses immediately — retrying it will never succeed, so spending two more attempts only buries real failures in noise. A block waits for the ceiling.

**Reset-on-success falls out of the query.** Only failures since the last **delivered** message count, so there is no counter to clear. "Delivered", not "accepted by the API" — an accepted-then-failed message proves nothing about the number.

**Async failures are counted.** WhatsApp usually accepts a message and fails it later, so the most common recipient failure — a block — arrives as a delivery callback, not a send error. Both `message_log` and `message_delivery` are consulted; without that, the commonest failure would never have reached the ceiling.

**Considered and rejected: turning a block into a permanent opt-out.** Tempting — a block is an opt-out expressed by action. But the ledger is cross-project and nothing in code can undo it, and the only evidence would be a keyword match against provider error text that changes without notice and can carry the word "blocked" for reasons unrelated to the recipient (a blocked template, a blocked account). Suppressing the lead is reversible and proportionate. A human can still escalate via `/admin/optout`.

One classification miss found and fixed in test: *"Your account has been restricted"* fell to `transient` because the phrase list held `account restricted`. Now stems on `restrict`. A restriction is the single most important failure to class correctly — it means the WhatsApp number itself is in trouble, which would otherwise read as a wave of ordinary timeouts. Final: **21/21 real failure strings correct.**

Landed: `failures.py` (new), `fail_class` on both `message_log` and `message_delivery`, ceiling wired into `sendgate.check()`, `/api/delivery` now reports failures by fault.

### Phase 0 gate, final order

**1** master switch → **2** operator pause → **3** opt-out → **4** fatigue → **5** retry ceiling

Cheapest and most absolute first: with the master switch off, a blocked send costs one boolean and touches no database.

**Task 8 — pgvector + KB tables (DONE 2026-07-30).**

Four tables per design §11: `brands`, `kb_documents` (versioned, `active`, uploader, content hash), `kb_chunks` (`brand_id` denormalised, `guardrail` column, `embed_model`, `vector(EMBED_DIM)`, HNSW cosine index), `agents` (persona, price policy, budget band, checklist/framings/guardrails as JSONB).

**The KB schema runs separately from `db.init_db()` and never raises.** `CREATE EXTENSION vector` needs a privilege the database user may not have, and the `vector` column type does not exist until the extension does. Folding this into `db.SCHEMA` would mean a Railway instance without pgvector **fails to boot the web app** — the webhook would stop answering because of a knowledge-base problem. `kb.init_kb()` records the outcome in `settings`, readable at `/api/kb`. Verified: a database that refuses the extension returns `{ok: False, detail: ...}` and the app still boots.

**The brand fence is the shape of the door, not a rule to remember.** `kb.search(brand_id, embedding, ...)` takes brand first and positionally required; there is no variant without a brand, and an empty one raises. Plus a post-query assertion that every returned row carries the requested brand — so if a future refactor drops the `WHERE`, it raises instead of leaking one project's answers into another project's conversation. Task 11 exists to attack this.

**Over-fetch then trim.** With HNSW plus a `WHERE brand_id`, Postgres can filter *after* the index scan and return fewer than k rows for the brand. `RETRIEVE_OVERFETCH=40` → trim to `RETRIEVE_K=6`. Free at this corpus size; the alternative is a silently short answer.

**`EMBED_DIM` is validated before interpolation** — it goes into executed DDL because a column type cannot be a bound parameter, so `_schema()` re-checks it is a sane int. Verified: `"1024; DROP TABLE leads"`, `0`, `-1`, `99999`, `None`, `3.5` all rejected.

**A model change is a re-index, not a migration.** `embed_model` is stored per chunk and the dimension in `settings`; `kb.dim_matches()` detects a mismatch so the ingest can refuse rather than let the bot answer confidently from noise. Provider still open (default `voyage-3-large` / 1024) — the qualifier agent does not exist yet and nothing here depends on the choice.

Also landed: `/admin/config-check` now reports every Phase 0 limit and the embedding config, so a container running unexpected settings is visible without reading logs.

**Task 9 — curate the RON FAQ (DONE 2026-07-30).**

`scripts/curate_faq.py` → `kb/RON/faqs.md`. **78 of 110 rows included, 32 excluded**, every exclusion attributable to a named rule and printed in an audit.

| Rule | Out | Why |
|---|---|---|
| `internal_chatter` | 16 | *"KK to come up with an answer"*, *"Question not understood"*, *"Not to be answered"*, *"Shared on email"* — a bot holding these reads staff conversation aloud to a buyer |
| `blank` | 6 | no answer in the source |
| `price` | 5 | nothing commercial is published; escalates by design |
| `handover_dates` | 3 | owner: never state handover or possession |
| `apartment_sizes` | 1 | two conflicting floors (818 vs 1220), open with sales |
| `truncated` | 1 | answer cut off mid-sentence in the source |

**A script, not a hand-curated file.** Sales owns this content and will update the spreadsheet. A hand-copied corpus would drift from the source the first time somebody fills a blank row, and nobody would know which version the bot was answering from.

**Five wrong exclusions were found by running the audit repeatedly**, not by reading the regexes:

1. carpet-area answer — killed by a stray `rera` token in the handover rule. RERA is a regulator, not a date.
2. construction warranty (*"as per RERA norms"*) — same cause.
3. *who* maintains the common areas — bare `maintenance` treated a responsibility question as a price question.
4. water source — question mentions charges in passing; the answer is only *"Panchayat supply, Bore, tankers"*. Now an explicit override with its reason.
5. **villa** sizes — the apartment-size rule matched them too. Villa figures (2552–3643 sqft) are unambiguous and safe.

**Two bugs the verification caught after that, both created by the fix itself:**

- The provenance comment read `transformed:C2BHK->2BHK` — so a naive chunker would have pulled the internal term into the corpus **through the marker recording its removal.** Now `transformed:config-vocab`.
- Renaming C2BHK to 2BHK made FAQ row 6's Phase 2 read **"Apts 2BHK, 2BHK, 3BHK"**. The first dedupe fixed that and **silently deleted a real product** — Phase 3's 4BHK villament, because the beachfront villa had already claimed that token on the same line. Dedupe is now restricted to *adjacent* bare repeats, which is the only collision the rename can produce. Verified token-by-token against the source: Phase 1 3→3, Phase 2 5→4 (duplicate only), Phase 3 5→5.

Final leak check on the buyer-facing section: no `C2BHK`, no handover dates, no 818/1220, no *"For Sales Person"*, no internal chatter, no adjacent duplicates.

**Task 10 — ingest pipeline (BUILT 2026-07-31, not yet executed).**

Embedding provider: **Voyage**, owner has a key from another project. `voyage-4-large`, 1024 dims.

**Chosen over Supabase's built-in embeddings for one concrete reason.** Supabase hosts `gte-small` free inside Edge Functions, but their docs state it "exclusively caters to English" and it is 384 dims. This audience writes Tanglish on WhatsApp — *"2bhk irukka"*, *"price enna"*, *"ECR la epdi"* — and matching those to the right FAQ row **is** the retrieval step. An English-only model would raise the escalation rate to save a cost that does not exist: the corpus is 79 chunks, so embedding it is a rounding error on any provider. Using Supabase would also have meant standing up a second vendor purely to call one function, while the database stays on Railway.

⚠️ **`voyage-3-large` was my config default and it is superseded** — the current line is the 4-series. Corrected; do not reintroduce 3-series names.

**The input_type trap.** Voyage returns *different vectors for the same string* depending on `input_type` (`document` for stored text, `query` for a buyer's question). Getting it backwards or omitting it does not error — it just answers worse, invisibly. So `embed.py` exposes two separate functions rather than one flag with a default, and `kb.answer_context()` is the only path the qualifier should use, so the query side cannot be got wrong at the call site.

**Dimension is verified, never trusted.** Every response is checked against `EMBED_DIM` before returning, because a mismatched model would either fail at insert or silently fill the corpus with incomparable vectors. `/admin/embed-check` probes the key, the model name and the dimension without touching the corpus — a wrong model name is otherwise discovered halfway through an ingest.

**Batch order is sorted by the provider's `index`, not assumed.** A silently reordered batch would attach every vector to the wrong chunk, and nothing downstream could detect it.

**Chunking is per-file and deliberate:**

| Source | Chunks | Strategy |
|---|---|---|
| `kb/RON/faqs.md` | 78 | one per `##` Q&A — already the unit of meaning; fixed-size splitting would cut answers in half and pair one tail with the next head |
| `kb/RON/location.md` | 1 | the whole 18-row distance table as ONE chunk (657 chars). One chunk per row would make *"what's nearby?"* retrieve three landmarks out of eighteen and look ignorant |

**79 chunks total.** The beach guardrail is attached to the location chunk, so it is retrieved alongside any distance answer and cannot be separated from the facts it constrains.

**Dry-run verified with no key and no database** (`--dry-run --show`). Leak check over all 79 chunks: no `C2BHK`, no handover dates, no 818/1220, no *"For Sales Person"*, no staff chatter, no "Barefoot", no "Private Beach", **and nothing addressed to us** — no markdown headings, no HTML comments, no curation notes, no "escalates to a human" instructions. A bot that ingests its own instructions reads them aloud. Shortest chunk 46 chars, longest 657, none under 30.

**Re-running is safe.** Documents are content-hashed; unchanged files skip. A changed file becomes a **new version** and the old one is marked inactive rather than deleted, so an answer given last week is still traceable to the text that produced it (§10 audit guardrail).

**Blocked on execution, not code:** needs `VOYAGE_API_KEY` in the environment and a live database. Run `python scripts/ingest_kb.py --brand RON`.

**Not done, deliberately:** no schema columns dropped. `leads.selected_date` and the carnival `wa_state` values stay — dropping them would destroy the carnival's real attendance data for no benefit, and it is irreversible. Schema cleanup is its own deliberate migration, if ever.

**Orphaned, safe for you to delete by hand:** `parser.py` and `scripts/smoke_test.py` (tests carnival flows that no longer exist). Left on disk rather than deleted. Snapshot of all ten original modules in `backup-2026-07-30-phase0/`.

**Carried forward:** `wasender.py` and the unauthenticated `/webhook/wasender` route still exist. Not carnival wiring, so out of task 1's scope — but that route can mutate leads without authentication and should be closed when task 12 reworks the process model.

Full task detail (acceptance criteria, doc references, rationale) is in the session task list. This file is the durable index — it survives the session.

---

## The tasks

| # | Task | Phase | Blocked by | Also waiting on |
|---|---|---|---|---|
| 1 | ~~Rip out dead carnival wiring, break the `EVENT_DATES` dependency~~ **DONE** | 0 Safety | — | |
| 2 | ~~Permanent cross-project opt-out~~ **DONE** | 0 Safety | — | |
| 3 | ~~Shared fatigue cap (one counter, all lanes)~~ **DONE** | 0 Safety | — | |
| 4 | ~~Retry ceiling~~ **DONE** | 0 Safety | — | |
| 5 | ~~Capture Wati delivery-status callbacks~~ **DONE** | 0 Safety | — | |
| 6 | Verify the CTWA webhook payload by eye | 1 Ingestion | — | |
| 7 | Stamp project + `ctwa_clid` at ingestion | 1 Ingestion | 6 | |
| 8 | ~~Stand up pgvector + KB tables~~ **DONE** | 2 KB | — | |
| 9 | ~~Curate the RON FAQ~~ **DONE** — 78 in / 32 out | 2 KB | — | 2BHK size parked by owner (fails safe: sizes excluded, escalate) |
| 10 | Ingest pipeline + load RON — **BUILT**, dry-run verified; load needs `VOYAGE_API_KEY` + live DB | 2 KB | 8, 9 | — |
| 11 | **Prove the brand fence** | 2 KB | 10 | |
| 12 | ~~Split webhook from worker via a queue~~ **DONE** | 3 Queue | 1 | **owner nod:** changes process model |
| 13 | ~~Build the worker process~~ **DONE** | 3 Queue | 12 | |
| 14 | Lead intake for all three inflows | 4 Intake | 7 | **owner:** campaign/source specifics |
| 15 | ~~Verify Sell.do labels exist in the mirror~~ **DONE** | 4 Intake | — | |
| 16 | The suppression gate | 4 Intake | 14, 15 | **owner:** stage + label list |
| 17 | Knock engine scheduler (day 0/3/10/25) | 5 Knock | 2, 3, 4, 13, 16 | **owner:** 4 approved templates |
| 18 | Stop-on-reply + scheduler stand-down | 5 Knock | 17 | |
| 19 | Dormancy + no-auto-restart guard | 5 Knock | 17 | |
| 20 | ~~Qualifier agent turn loop~~ **DONE** | 6 Agent | 11, 13 | |
| 21 | ~~Answer-before-ask turn structure~~ **DONE** | 6 Agent | 20 | |
| 22 | Three gates + purpose lens + timeline | 6 Agent | 20 | **owner:** location line, config list |
| 23 | ~~The persuasion ladder~~ **DONE** | 6 Agent | 22 | |
| 24 | ~~Exit router — 3 terminals + 1 loop-back~~ **DONE** | 6 Agent | 17, 22 | |
| 25 | ~~Claims + commitment guardrails~~ **DONE** | 6 Agent | 20 | |
| 26 | Qualified-lead card + group ping | 7 Handoff | 24 | **owner:** escalation destination |
| 27 | ~~Transcript storage for audit~~ **DONE** | 7 Handoff | 20 | |
| 28 | Corpus upload screen for sales | 8 | 10 | |
| 29 | CAPI flywheel back to Meta | 9 | 6, 26 | only if 6 finds `ctwa_clid` |

**Not on this list:** Sell.do write (parked by owner — becomes an extra handoff sink whenever credentials arrive; nothing is lost meanwhile because task 27 stores the transcript).

---

## Startable right now — nothing blocks these

**1, 2, 3, 4, 5** (all of Phase 0) · **6** · **8** · **9** · **15**

Phase 0 is where to start, and not merely because it's first:
- The send path is **inert today** (§15), so send logic can be rewritten without a single message reaching a real person. That window closes the moment anything is switched on.
- Rev 2 loads the system far harder than rev 1 — 4 templates against a cold reactivation list is heavier than the carnival blast that produced 44% blocked.
- **None of retry ceiling, opt-out, fatigue cap or delivery tracking exists today.**
- If the WhatsApp number gets restricted, **all three inflows die at once** — including CTWA, which works fine today and would be collateral damage.

**6, 15** are cheap verification tasks with expensive consequences if skipped, and both can run in parallel with Phase 0:
- **6** — `ctwa_clid` is unrecoverable. Not captured at ingestion means gone forever, and task 29 dies with it.
- **15** — decides whether the owner's suppression list is implementable as specified at all.

---

## Two ordering decisions worth not undoing

**The interlock precedes the thing it interlocks.** Task 16 (suppression gate) blocks task 17 (knock engine), not the reverse. No lead may be knocked before it has been checked against Sell.do — building the knocker first means a window where it can reach someone a salesperson already owns.

**Phase 0 blocks the knock engine.** Task 17 is blocked by 2, 3 and 4. The knock engine is the highest-volume sender in the system and it must not exist before opt-out, the fatigue cap and the retry ceiling do.

---

## Owner dependencies, gathered

| Needed | Blocks | Status |
|---|---|---|
| 4 approved templates (utility framing for reactivation) | 17 | in progress |
| Location file — "a data point from every possible angle" | 10, 22 | promised |
| Config / off-category list (and the 1BHK / villament / 5BHK conflict) | 22 | promised |
| Suppression stage + label list | 16 | promised |
| Campaign + lead-source specifics | 14 | promised |
| Escalation destination — same group as qualified, or separate | 26 | open |
| FAQ row 7 — which size range may the bot say | 9 | open |
| Nod on the process-model change | 12 | open |
