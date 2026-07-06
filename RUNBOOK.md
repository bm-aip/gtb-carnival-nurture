# GTB Carnival — Lead Confirmation & Event Dashboard
## Runbook (deploy by July 6 EOD, test July 6–7, live July 7)

### What this is
Single Flask app. Polls both Sell.do reporting DBs every 10 min for leads in
status **Interested** / **Site Visit Scheduled** on the mapped project+campaign,
resolves each lead's phone from the Meta lead form via its lead ID, runs the
3-message WhatsApp sequence over Wasender, and serves the dashboard
(day counts, pipeline, campaign spend/CPL, unmatched queue).

Sequence: **M1** date-ask on qualification → **M2** at +24h if silent →
**M3** reminder 6pm evening before selected date. From July 9 onward, M1 ships
combined with venue + maps link. July 9 6pm, non-responders get a generic
"walk in any day" M3. Sell.do status flipping out of the two qualifying
statuses suppresses all pending messages. Global pause button and a
30 sends/hour cap protect the WhatsApp number.

---

### DESIGN NOTE (post-verification, July 6)
Sell.do's reporting DB never carries the Meta lead ID (utm_lead_id = 0/864
checked rows). Phone resolution therefore pulls ALL lead-form submissions
from Meta (page tokens -> forms -> leads) into a meta_leads cache and matches
Sell.do leads by normalized name + creation-time proximity (±6h window).
Unambiguous matches auto-queue; ambiguous ones surface in the Unmatched tab
after 24h. Stage matching normalizes the '(Pre Sales) ' prefix.

### Step 0 — DONE: Sell.do query calibrated (~15 min)
The only untested integration is the Sell.do reporting schema. On your machine:

```
pip install psycopg2-binary
SELLDO_DB_URL="<RON url from your message>" python scripts/discover_selldo.py
SELLDO_DB_URL="<Elements url>" python scripts/discover_selldo.py
```

Paste both outputs back to Claude. `sql/selldo_leads.sql` gets fixed in one
edit. **Do not deploy before this** — the guessed column names will error
(harmlessly — errors surface on the dashboard error bar — but nothing flows).

### Step 1 — Verify Meta tokens (~5 min)
```
META_TOKEN="<RON token>" python scripts/test_meta.py
META_TOKEN="<Elements token>" python scripts/test_meta.py
```
Confirm `leads_retrieval` appears in granted scopes for BOTH (verified July 6).
Then run the FORM-PULL proof — this is now the critical Meta test:

```
$env:META_TOKEN = "<token>"
python scripts/test_meta_forms.py
```

It must list your Carnival lead forms with recent leads showing masked phone
numbers. A 'LEADS ERROR' on a form means Leads Access Manager is blocking the
user — fix in Page Settings -> Leads Access before deploy.

### Step 2 — Deploy (~20 min)
1. Push this folder to a new repo under `bm-aip` (suggest `gtb-carnival-nurture`).
2. New Railway project → deploy from repo → add **Postgres plugin**
   (Railway injects `DATABASE_URL`; no Supabase needed — you said "new",
   Railway's plugin is one click and keeps everything in one project).
3. Set all remaining env vars from `.env.example` using the credentials you
   sent. Set `DASH_PASS` to something real.
4. Deploy runs `web: gunicorn app:app --workers 1 ...`. **Keep workers=1** —
   the scheduler runs in-process; two workers = duplicate messages.

### Step 3 — Wire the Wasender webhook (~5 min)
Wasender dashboard → session 67860 → webhook URL:
`https://<railway-domain>/webhook/wasender` — enable message-received
(and poll-vote if listed) events.

### Step 4 — Test protocol (July 6)
1. Dashboard → confirm no errors in the red bar.
2. `POST /admin/test-send` with your own number (or use curl) → message arrives.
3. Insert yourself as a fake lead **in Sell.do** (status Interested, campaign
   mapped) → within 10 min you get M1 → reply "2" → ack arrives, dashboard
   shows you under Sat 11.
4. Reply with garbage text → no date recorded, no crash.
5. Have 2–3 team members repeat. Then flip one test lead's Sell.do status to
   Not Interested → confirm suppression on dashboard.
6. Delete/suppress test leads before go-live.

### Step 4.5 — RON ONLY: connect Sell.do to Meta + backfill (BLOCKING for RON)
The Meta->Sell.do integration for RON carnival forms was never connected;
leads since July 3 exist only in Meta. Two actions:
1. In Sell.do, connect the Facebook Lead Ads integration to page 'GT Bharathi'
   and subscribe ALL carnival forms, mapping to campaign 'RON_Carnival' +
   project 'Republic Of Nature' (exact strings — the poller filters on them).
2. Backfill: download each carnival form's leads CSV from Meta Leads Center
   and import into Sell.do under the same campaign/project. Presales must
   qualify the July 3-6 backlog immediately — M1s only fire on qualification.

### Step 5 — Go-live (July 7)
Nothing to switch on — once the Sell.do query is calibrated and the app is up,
every newly qualified lead flows automatically. Watch the Unmatched tab the
first few hours; match-failure rate above ~15% means the Meta lead ID column
mapping is wrong — stop and recheck.

### Controls
- **Pause sending** (header button) — stops all outbound instantly, polling continues.
- **Poll now** — forces immediate Sell.do + sequencer run.
- **Rate cap** — `MAX_SENDS_PER_HOUR` env (default 30). If leads outpace it,
  messages queue to the next tick; nothing is lost.

### After July 13
- Rotate BOTH Sell.do reporting passwords, BOTH Meta tokens, and the Wasender
  key. All four were shared in plaintext chat.
- Export final numbers from the dashboard before tearing anything down.

### Known limits (accepted trade-offs)
- Poll-vote webhook shapes vary by Wasender version; text replies are the
  reliable path (M1 asks for "Reply 1/2/3" for exactly this reason). If poll
  votes don't register on test day, nothing breaks — text still works.
- One phone matched to one lead: if the same number exists as a lead in both
  projects, inbound replies attach to the most recently updated lead.
- Feedback message (M4) was dropped per your instruction.
