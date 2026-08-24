import json
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id SERIAL PRIMARY KEY,
    project TEXT NOT NULL,                    -- RON | ELEMENTS
    selldo_lead_id TEXT NOT NULL,
    meta_lead_id TEXT,
    name TEXT,
    phone TEXT,
    selldo_status TEXT,
    selldo_response_at TIMESTAMPTZ,
    wa_state TEXT NOT NULL DEFAULT 'queued',  -- queued|unmatched|m1_sent|m2_sent|date_selected|suppressed|done
    selected_date DATE,
    m1_sent_at TIMESTAMPTZ,
    m2_sent_at TIMESTAMPTZ,
    m3_sent_at TIMESTAMPTZ,
    last_inbound_at TIMESTAMPTZ,
    last_inbound_text TEXT,
    suppressed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project, selldo_lead_id)
);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads (phone);
CREATE INDEX IF NOT EXISTS idx_leads_state ON leads (wa_state);

CREATE TABLE IF NOT EXISTS message_log (
    id SERIAL PRIMARY KEY,
    lead_id INT REFERENCES leads(id),
    direction TEXT NOT NULL,          -- out | in
    msg_type TEXT,                    -- knock_t1|knock_t2|knock_t3|knock_t6|inbound
                                      -- (legacy carnival rows: m1|m2|m3|ack)
    body TEXT,
    ok BOOLEAN,
    detail TEXT,
    ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS campaign_mapping (
    campaign_id TEXT PRIMARY KEY,
    campaign_name TEXT,
    account_id TEXT,
    objective TEXT,
    project TEXT                      -- RON | ELEMENTS | NULL (unmapped)
);
ALTER TABLE campaign_mapping ADD COLUMN IF NOT EXISTS objective TEXT;

CREATE TABLE IF NOT EXISTS campaign_stats (
    campaign_id TEXT,
    stat_date DATE,
    spend NUMERIC,
    impressions BIGINT,
    clicks BIGINT,
    leads INT,
    PRIMARY KEY (campaign_id, stat_date)
);

CREATE TABLE IF NOT EXISTS meta_leads (
    meta_lead_id TEXT PRIMARY KEY,
    project TEXT,
    page_id TEXT,
    form_id TEXT,
    form_name TEXT,
    name TEXT,
    phone TEXT,
    created_time TIMESTAMPTZ,
    preferred_date DATE,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE meta_leads ADD COLUMN IF NOT EXISTS preferred_date DATE;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS selldo_response_at TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS send_attempts INT NOT NULL DEFAULT 0;
-- Which campaign this lead came from. The bot's allow-list is checked against it,
-- so a lead with no campaign is never messaged -- the gate fails closed.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS campaign TEXT;
CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads (campaign);

-- 2026-08-10: which door the lead walked through, and Meta's click id.
--
-- WHY THESE ARE COLUMNS AND NOT A LOG LINE
-- parse_source() has been recording ad evidence into message_log.detail as free
-- text since task 6. That was right for answering "does the field exist at all",
-- and useless for anything else: the flywheel has to JOIN a lead to its click id
-- at the moment that lead qualifies, and you cannot join on a sentence.
--
-- `ctwa_clid` IS THE ONLY KEY META MATCHES CONVERSIONS ON. It is not in Wati's
-- webhook -- verified across 90 arrivals on 2026-08-10, all of which carried
-- sourceType/sourceId/sourceUrl and none of which carried the click id. It IS in
-- Wati's REST API, at messageReferral.ctwaClid on the FIRST inbound message of the
-- conversation. So it is fetched, not received; see wati.fetch_referral().
--
-- The referral rides that first message only. Miss it and it is gone for that
-- person permanently, which is why capture is wired to run on every inbound that
-- looks like an ad click rather than only at lead creation.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS inflow TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS ctwa_clid TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS ctwa_source_id TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS ctwa_source_url TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS ctwa_headline TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS ctwa_captured_at TIMESTAMPTZ;
-- `ctwa_looked_at` marks "we asked Wati and it had nothing", which is a different
-- state from "we never asked". Without it the backfill re-asks about the same
-- landing-page walk-ins on every run, forever.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS ctwa_looked_at TIMESTAMPTZ;

-- 2026-08-11: the knock that Meta refused, and when we stopped trying.
--
-- Meta refuses individual template sends per RECIPIENT, not per sender. Proven on
-- 2026-08-11: numbers 919884739289 and 919841071005 each received two knocks the
-- same day from the same number -- one delivered and read, the other refused. A
-- sender-wide penalty cannot do that, so a refusal is temporary and the same person
-- can be reached later with a different template.
--
-- `knock_lost_at` is set when the retry ceiling is reached. Owner 2026-08-11: after
-- ten attempts the person is "left alone permanently, marked as lost". Separate
-- from `suppressed`, which means they asked us to stop -- these two must never be
-- confused, because one is our failure and the other is their instruction.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS knock_lost_at TIMESTAMPTZ;
-- Which VARIANT actually went out. Without it a retry cannot know which wording
-- has already been spent on this person, and the whole rotation is guesswork.
ALTER TABLE message_log ADD COLUMN IF NOT EXISTS template_name TEXT;

-- 2026-08-17: how many turns since we last asked a qualifying question.
--
-- The bot asked one in 81% of its turns, and turns carrying a question were replied
-- to at 47.9% against 70.6% for turns carrying none. Owner: ask a gate only every
-- second or third turn and let the buyer lead in between.
--
-- A COUNTER, not a timestamp, because the rule is "turns", and a conversation can
-- go quiet for a day between two turns without that meaning anything. `asked` cannot
-- answer this -- it records WHICH framings were spent, never when.
--
-- Starts at 99 so a brand-new conversation is immediately eligible: the first turn
-- should still ask, it is the rest that should breathe.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS turns_since_gate INT NOT NULL DEFAULT 99;
-- Per-ad reporting is the whole point of storing source_id, and it is always a
-- GROUP BY over the full table.
CREATE INDEX IF NOT EXISTS idx_leads_ctwa_source ON leads (ctwa_source_id);
CREATE INDEX IF NOT EXISTS idx_meta_leads_proj_time ON meta_leads (project, created_time);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS processed_webhooks (
    msg_id TEXT PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phase 0 task 5: delivery ears.
-- Wati posts a callback per state change of every outbound message. The old code
-- discarded all of them (wati.parse_inbound returned (None, None) for anything
-- that was not a customer message), which is why the "44% blocked" figure had to
-- be read off Wati's own dashboard instead of this system.
--
-- UNIQUE (provider_msg_id, status) rather than (provider_msg_id): sent,
-- delivered and read callbacks all carry the SAME message id, so a per-id
-- constraint would keep only the first and silently discard the rest -- the exact
-- failure this table exists to prevent. A NULL provider_msg_id never conflicts in
-- Postgres, which is deliberate: an event we could not identify is still kept.
CREATE TABLE IF NOT EXISTS message_delivery (
    id SERIAL PRIMARY KEY,
    phone TEXT,                        -- keyed on the person, per Phase 0
    provider_msg_id TEXT,
    lead_id INT REFERENCES leads(id),
    status TEXT NOT NULL,              -- sent|delivered|read|failed|unknown
    reason TEXT,                       -- failure text / error code when supplied
    event_type TEXT,                   -- Wati's raw eventType, unmapped
    event_ts TIMESTAMPTZ,
    raw TEXT,                          -- kept only for status='unknown'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider_msg_id, status)
);
CREATE INDEX IF NOT EXISTS idx_delivery_phone ON message_delivery (phone);
CREATE INDEX IF NOT EXISTS idx_delivery_status ON message_delivery (status, created_at);

-- Tasks 23/24: per-conversation state.
--
-- The checklist lives here rather than on `leads` because a returning ghost must
-- RESUME mid-checklist, never restart -- a buyer who already told us their budget
-- must never be asked for it twice.
--
-- `asked` records which persuasion framing was used for each gate, so the bot can
-- rephrase rather than repeat. `unreciprocated` counts consecutive turns where the
-- bot asked and got no answer; at the limit a human is flagged WHILE THE BOT KEEPS
-- ANSWERING -- flagging is not the same as giving up.
CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY,
    lead_id INT NOT NULL REFERENCES leads(id),
    brand_id TEXT NOT NULL,
    checklist JSONB NOT NULL DEFAULT '{}'::jsonb,
    asked JSONB NOT NULL DEFAULT '{}'::jsonb,
    unreciprocated INT NOT NULL DEFAULT 0,
    human_flagged_at TIMESTAMPTZ,
    -- qualified | visit_booked | nurture | dead | escalated | null
    -- `nurture` is the only PROVISIONAL one: below everything we sell today, and
    -- allowed to become qualified later if the budget moves (conversation.UPGRADABLE).
    -- No value here silences the bot -- see worker._handle_inbound.
    outcome TEXT,
    outcome_at TIMESTAMPTZ,
    handoff_sent_at TIMESTAMPTZ,
    last_turn_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (lead_id)
);
CREATE INDEX IF NOT EXISTS idx_conv_outcome ON conversations (outcome);

-- The §10 audit guardrail: the day a buyer says "your bot told me X", you need the
-- exact chunks and document versions that produced the reply. Stored per outbound
-- turn rather than reconstructed later, because the corpus is versioned and will
-- have moved on by the time anyone asks.
ALTER TABLE message_log ADD COLUMN IF NOT EXISTS sources JSONB;

-- Task 12: the job queue. The webhook writes here and returns; a worker reads.
--
-- `dedup_key` UNIQUE is the idempotency guarantee, enforced by the database rather
-- than by code, so two webhook deliveries racing each other cannot both enqueue.
--
-- Failed jobs are LEFT IN THE TABLE. Deleting them would hide the one failure in
-- this system that a real person is waiting on: a customer message never answered.
CREATE TABLE IF NOT EXISTS job_queue (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL,
    dedup_key TEXT UNIQUE,
    phone TEXT,                        -- ordering key: one in-flight job per person
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 5,
    last_error TEXT,
    run_after TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_jobs_claimable ON job_queue (status, run_after, id);
-- Partial index: the claim query's NOT EXISTS only ever looks at running rows.
CREATE INDEX IF NOT EXISTS idx_jobs_running_phone ON job_queue (phone)
    WHERE status = 'running';

-- Phase 0 task 2: the opt-out ledger. Keyed on PHONE, never on lead id -- `leads`
-- is UNIQUE (project, selldo_lead_id), so one human is routinely several rows and
-- a lead-keyed opt-out would leak between them.
--
-- TWO SCOPES, per owner decision 2026-07-30:
--   'global'  -- permanent, every project, forever. Set by an explicit stop
--               ("STOP", "unsubscribe", the Stop-updates button) or by a
--               wrong-number / sent-by-mistake reply, which means this phone is
--               not the lead at all and no project may contact it.
--   'project' -- "not interested" and friends. Stops this project only; the
--               record is kept and another project may still reach them.
-- Precedence is global over project, enforced in optout.is_blocked().
--
-- Rows are INSERT-only and nothing in the codebase deletes them. Removing a
-- global opt-out is a deliberate manual database action by a human, on purpose:
-- an automated un-opt-out is the one bug in this system that cannot be apologised
-- for afterwards.
CREATE TABLE IF NOT EXISTS optouts (
    id SERIAL PRIMARY KEY,
    phone TEXT NOT NULL,
    scope TEXT NOT NULL,               -- global | project
    project TEXT,                      -- NULL when scope='global'
    matched TEXT,                      -- the phrase that triggered it
    source TEXT,                       -- inbound_keyword | button | human | import
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- COALESCE in the index because a NULL project would not collide in Postgres,
-- so the same global opt-out could otherwise be inserted repeatedly.
CREATE UNIQUE INDEX IF NOT EXISTS idx_optouts_unique
    ON optouts (phone, scope, COALESCE(project, ''));
CREATE INDEX IF NOT EXISTS idx_optouts_phone ON optouts (phone);

-- Phase 0 task 3: the fatigue cap.
--
-- This table holds only WHERE A JOURNEY STARTS. The knock count itself is never
-- stored -- it is counted from message_log against started_at, so no bug and no
-- manual edit can forge a lower count. The single mutable fact is the start
-- marker, and every move of it is written to journey_resets with a reason.
--
-- Keyed (phone, project): one human is routinely several lead rows, and the
-- 4-knock sequence is per project while the weekly ceiling below is per person.
CREATE TABLE IF NOT EXISTS knock_journeys (
    id SERIAL PRIMARY KEY,
    phone TEXT NOT NULL,
    project TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reset_count INT NOT NULL DEFAULT 0,
    last_reset_at TIMESTAMPTZ,
    last_reason TEXT,
    dormant_at TIMESTAMPTZ,            -- set by task 19 at day 31 of silence
    UNIQUE (phone, project)
);
CREATE INDEX IF NOT EXISTS idx_journeys_phone ON knock_journeys (phone);

-- Audit trail for resets. The owner chose a resettable counter over a hard
-- lifetime ceiling; the safeguard against that being abused is that every reset is
-- attributable, not that resets are rare.
CREATE TABLE IF NOT EXISTS journey_resets (
    id SERIAL PRIMARY KEY,
    phone TEXT NOT NULL,
    project TEXT,
    reason TEXT NOT NULL,              -- form_fill | ctwa_click | human | import
    note TEXT,
    knocks_before INT,                 -- how many they had already had
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_resets_phone ON journey_resets (phone);

-- Sell.do stage movement, so "did our knock move this lead forward" is answerable.
--
-- selldo.poll_project OVERWRITES leads.selldo_status on every change, which meant
-- the before-state was destroyed the instant presales advanced someone. The counts
-- on 2026-08-22 were unreadable for exactly this reason: we could see 94 leads
-- sitting in a qualified stage and had no way to know which of them got there
-- after we knocked.
--
-- knocks_before and last_knock_at are SNAPSHOTTED HERE rather than joined later.
-- Two reasons: the attribution query stays a simple read of one table, and the
-- answer survives message_log being pruned or wiped (/admin/reset-test deletes a
-- handset's whole history, which would silently rewrite the past otherwise).
CREATE TABLE IF NOT EXISTS selldo_stage_history (
    id BIGSERIAL PRIMARY KEY,
    lead_id INT NOT NULL REFERENCES leads(id),
    project TEXT NOT NULL,
    selldo_lead_id TEXT,
    from_stage TEXT,                   -- NULL on the first sighting of a lead
    to_stage TEXT,
    knocks_before INT NOT NULL DEFAULT 0,
    last_knock_at TIMESTAMPTZ,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_stage_hist_lead ON selldo_stage_history (lead_id);
CREATE INDEX IF NOT EXISTS idx_stage_hist_changed ON selldo_stage_history (changed_at);

-- Joins a delivery callback back to the send that caused it.
ALTER TABLE message_log ADD COLUMN IF NOT EXISTS provider_msg_id TEXT;
CREATE INDEX IF NOT EXISTS idx_msglog_provider ON message_log (provider_msg_id);

-- Phase 0 task 4: why a send failed, in one canonical word.
--   recipient -- their number is not on WhatsApp, or they have blocked us
--   transient -- timeout, 5xx, rate limit, or anything unrecognised
--   system    -- OUR fault: template unapproved, bad token, 24h window shut
-- Only 'recipient' and 'transient' have ceilings. 'system' has none, because a
-- lead must never be killed off by our own misconfiguration.
ALTER TABLE message_log ADD COLUMN IF NOT EXISTS fail_class TEXT;
CREATE INDEX IF NOT EXISTS idx_msglog_failclass ON message_log (fail_class, ts);

-- WhatsApp frequently ACCEPTS a message and fails it later, so the most common
-- recipient failure (a block) arrives as a delivery callback rather than as a send
-- error. Without classifying it here it would never reach the retry ceiling.
ALTER TABLE message_delivery ADD COLUMN IF NOT EXISTS fail_class TEXT;
CREATE INDEX IF NOT EXISTS idx_delivery_failclass ON message_delivery (fail_class);

-- 2026-08-07: which lead forms are worth checking, and when we last looked.
--
-- poll_meta_leads walked EVERY form Meta lists -- 103 of them, 94 of which had
-- produced nothing in a week. With a 0.4s throttle and a Graph call each, one pass
-- took ~3 minutes against a 1-minute schedule, so the scheduler refused two fires
-- in every three and logged a skip for each. Leads still arrived; the cost was that
-- the log became one repeated line, which is the same blindness that let the credit
-- outage run for eight hours.
--
-- THIS TABLE RECORDS WHAT WE LOOKED AT, not what we found. That distinction is the
-- whole point: `meta_leads` only knows forms that produced a lead, so 66 forms were
-- indistinguishable from forms we had never checked, and a rule built on it would
-- have polled them forever.
--
-- `last_lead_at` decides the fast lane (META_FORM_ACTIVE_DAYS). `last_polled_at`
-- exists so a form that yields nothing still counts as known. A form absent from
-- this table is ALWAYS polled -- the unknown case fails towards doing too much
-- work, never towards missing a lead.
--
-- `leads_seen` is what the MOST RECENT poll returned, not a running total. The poll
-- re-reads the same window every pass (inserts are ON CONFLICT DO NOTHING), so a
-- cumulative count would climb without meaning anything.
CREATE TABLE IF NOT EXISTS meta_form_polls (
    form_id TEXT PRIMARY KEY,
    project TEXT,
    page_id TEXT,
    form_name TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_polled_at TIMESTAMPTZ,
    last_lead_at TIMESTAMPTZ,
    leads_seen INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_formpolls_lead ON meta_form_polls (last_lead_at);

-- Seed from the leads already cached, so the first pass after deploy does not treat
-- 37 known-good forms as strangers and drag every one of them into the fast lane.
-- ON CONFLICT DO NOTHING makes this a no-op on every boot after the first, and it
-- can never overwrite a row the poller has since maintained.
INSERT INTO meta_form_polls (form_id, project, page_id, form_name,
                             first_seen_at, last_lead_at)
SELECT form_id, min(project), min(page_id), min(form_name),
       min(created_time), max(created_time)
FROM meta_leads
WHERE form_id IS NOT NULL
GROUP BY form_id
ON CONFLICT (form_id) DO NOTHING;
"""


@contextmanager
def conn():
    c = psycopg2.connect(config.DATABASE_URL)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(SCHEMA)


def q(sql, params=None, one=False):
    with conn() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            if cur.description is None:
                return None
            rows = cur.fetchall()
            return (rows[0] if rows else None) if one else rows


def x(sql, params=None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.rowcount


def get_setting(key, default=None):
    r = q("SELECT value FROM settings WHERE key=%s", (key,), one=True)
    return r["value"] if r else default


def set_setting(key, value):
    x("""INSERT INTO settings (key, value) VALUES (%s,%s)
         ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""", (key, str(value)))


def mark_webhook_new(msg_id):
    """Return True if this Wasender msg_id is new (and claim it); False if it
    was already processed. Wasender delivers the same message via multiple
    events (messages.upsert + messages.received), so dedup on the message id to
    avoid double-processing (double acks). No id -> can't dedup, process it."""
    if not msg_id:
        return True
    return x("INSERT INTO processed_webhooks (msg_id) VALUES (%s) ON CONFLICT DO NOTHING",
             (msg_id,)) == 1


def log_msg(lead_id, direction, msg_type, body, ok=True, detail=None,
            provider_msg_id=None, fail_class=None, sources=None,
            template_name=None):
    import json as _json
    x("""INSERT INTO message_log (lead_id, direction, msg_type, body, ok, detail,
                                  provider_msg_id, fail_class, sources, template_name)
         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
      (lead_id, direction, msg_type, body, ok, detail, provider_msg_id, fail_class,
       _json.dumps(sources) if sources else None, template_name))


def record_delivery(ev):
    """Store one delivery callback. Idempotent on (provider_msg_id, status).

    Resolves the lead by PHONE, not by the message id: a status can arrive for a
    send we failed to record an id for, and the phone is the durable link. Returns
    True if this was a new event, False if it was a duplicate callback.
    """
    # Wati's delivery events drop the phone. Verified against a live callback on
    # 2026-08-02:
    #
    #   templateMessageSent       waId=919789988124   wamid.HBgMOTE5...
    #   sentMessageDELIVERED_v2   waId ABSENT         wamid.HBgMOTE5...  <- same id
    #
    # The message id is the thread between them, so a phone-less event inherits the
    # phone from the send we already recorded. Without this every delivered/read/
    # failed row is orphaned and the report can never say who a failure belongs to.
    if not ev.get("phone") and ev.get("provider_msg_id"):
        prior = q("""SELECT phone, lead_id FROM message_delivery
                     WHERE provider_msg_id=%s AND phone IS NOT NULL
                     ORDER BY id LIMIT 1""", (ev["provider_msg_id"],), one=True)
        if prior:
            # IN PLACE, NOT A REBIND. This used to be `ev = dict(ev, phone=...)`,
            # which fixed the phone for the INSERT below and left the caller's dict
            # untouched -- and the caller needs it.
            #
            # app.py calls mark_meta_refused(ev.get("phone"), ...) immediately after
            # this function returns, and mark_meta_refused opens with
            # `if not phone: return None`. Wati drops the phone on every delivery
            # callback, so that argument was None every single time and the refusal
            # was never recorded. Measured 2026-08-24: 1,793 templateMessageFailed
            # events in seven days, 0 knocks ever marked meta_refused, 0 retries.
            # The whole retry-with-variants ladder was dead behind one rebind.
            ev["phone"] = prior["phone"]

    lead = q("SELECT id FROM leads WHERE phone=%s ORDER BY updated_at DESC LIMIT 1",
             (ev.get("phone"),), one=True) if ev.get("phone") else None
    n = x("""INSERT INTO message_delivery
                (phone, provider_msg_id, lead_id, status, reason, event_type,
                 event_ts, raw, fail_class)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON CONFLICT (provider_msg_id, status) DO NOTHING""",
          (ev.get("phone"), ev.get("provider_msg_id"), lead["id"] if lead else None,
           ev.get("status"), ev.get("reason"), ev.get("event_type"),
           ev.get("event_ts"), ev.get("raw"), ev.get("fail_class")))
    return n == 1


def mark_meta_refused(phone, event_ts=None, window_minutes=120):
    """Meta refused a template we had already logged as sent. Correct the record.

    Returns the message_log id we corrected, or None.

    WHY FLIP `ok` RATHER THAN ADD A RETRY TABLE
    -------------------------------------------
    `ok=TRUE` currently means "Wati accepted it", and when Meta then refuses the
    message that record is simply WRONG -- nothing reached the handset. Three
    separate counters already read `ok`, each with the same intent:

        knocks.knock_state   -- "ok=TRUE only: a template that never reached the
                                handset has not been spent"
        fatigue.window_count -- proactive sends this person RECEIVED in 7 days
        fatigue.journey_count-- knocks RECEIVED in the current journey

    So correcting the one flag makes all three right at once, and it delivers the
    owner's rule of 2026-08-11 -- "never arrived so don't count" -- without adding a
    parallel bookkeeping system that could disagree with the log.

    It also produces the retry for free: with the row no longer counted, the step
    becomes due again and the engine picks the next variant.

    MATCHED BY PHONE AND TIME. Wati's sendTemplateMessage response carries no
    message id, so a callback can never be joined to its send by id -- the same
    reason knock_delivery() matches this way. The most recent still-ok knock to that
    number inside the window is the one that was refused.
    """
    if not phone:
        return None
    row = q("""SELECT ml.id
                 FROM message_log ml JOIN leads l ON l.id = ml.lead_id
                WHERE l.phone = %s
                  AND ml.direction = 'out' AND ml.ok
                  AND ml.msg_type LIKE 'knock\\_%%'
                  AND ml.ts <= COALESCE(%s, now())
                  AND ml.ts > COALESCE(%s, now()) - (%s * interval '1 minute')
                ORDER BY ml.ts DESC LIMIT 1""",
            (phone, event_ts, event_ts, window_minutes), one=True)
    if not row:
        return None
    x("""UPDATE message_log
            SET ok = FALSE,
                fail_class = 'meta_refused',
                detail = COALESCE(detail, '') || ' | meta refused after accept'
          WHERE id = %s""", (row["id"],))
    return row["id"]


def knock_delivery(hours=72):
    """Did our knocks actually ARRIVE? One row per send.

    Template sends go through Wati's sendTemplateMessage, whose response carries
    no whatsappMessageId -- so a delivery callback can never be matched to the send
    by id. It is matched by PHONE and TIME instead: the outcome of a send is the
    best delivery event for that phone in the hour after it went out.

    `ok=TRUE` in message_log means Wati ACCEPTED the send. It is not delivery, and
    conflating the two is how a 62% failure rate went unnoticed on 2026-08-02.
    """
    return q("""
        WITH sends AS (
            SELECT ml.id, ml.lead_id, ml.msg_type, ml.ts, l.phone, l.name
            FROM message_log ml JOIN leads l ON l.id = ml.lead_id
            WHERE ml.direction='out' AND ml.ok
              AND ml.msg_type LIKE 'knock\\_%%'
              AND ml.ts > now() - (%s || ' hours')::interval
        )
        SELECT s.id, s.lead_id, s.name, s.phone, s.msg_type, s.ts,
               (SELECT md.status FROM message_delivery md
                 WHERE md.phone = s.phone
                   AND md.created_at BETWEEN s.ts AND s.ts + interval '1 hour'
                   AND md.status <> 'unrecognised'
                 ORDER BY CASE md.status WHEN 'read' THEN 1 WHEN 'delivered' THEN 2
                                         WHEN 'sent' THEN 3 WHEN 'failed' THEN 4
                                         ELSE 5 END
                 LIMIT 1) AS outcome,
               (SELECT md.reason FROM message_delivery md
                 WHERE md.phone = s.phone AND md.status='failed'
                   AND md.created_at BETWEEN s.ts AND s.ts + interval '1 hour'
                 LIMIT 1) AS fail_reason
        FROM sends s ORDER BY s.ts DESC""", (str(hours),)) or []


def knock_delivery_summary(hours=72):
    """Counts by outcome. `None` means no callback ever arrived for that send --
    which is itself the finding, not a gap to hide."""
    rows = knock_delivery(hours)
    out = {}
    for r in rows:
        out[r["outcome"] or "no callback"] = out.get(r["outcome"] or "no callback", 0) + 1
    return {"sends": len(rows), "by_outcome": out}


def delivery_rollup(hours=24):
    """Counts per status over a rolling window, plus the failure rate.

    This is the number the carnival could only read off Wati's dashboard: 44% of
    cold sends blocked. Having it in our own database is what lets the fatigue cap
    (task 3) and the retry ceiling (task 4) be set from evidence rather than from
    a guess.
    """
    rows = q("""SELECT status, count(*) AS n FROM message_delivery
                WHERE created_at > now() - (%s * interval '1 hour')
                GROUP BY status""", (hours,)) or []
    counts = {r["status"]: r["n"] for r in rows}
    total = sum(counts.values())
    failed = counts.get("failed", 0)
    return {"window_hours": hours, "counts": counts, "total": total,
            "failure_rate": round(failed / total, 4) if total else None}
