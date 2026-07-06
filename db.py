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
    msg_type TEXT,                    -- m1|m2|m3|m3_generic|ack|inbound
    body TEXT,
    ok BOOLEAN,
    detail TEXT,
    ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS campaign_mapping (
    campaign_id TEXT PRIMARY KEY,
    campaign_name TEXT,
    account_id TEXT,
    project TEXT                      -- RON | ELEMENTS | NULL (unmapped)
);

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
CREATE INDEX IF NOT EXISTS idx_meta_leads_proj_time ON meta_leads (project, created_time);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
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


def log_msg(lead_id, direction, msg_type, body, ok=True, detail=None):
    x("INSERT INTO message_log (lead_id, direction, msg_type, body, ok, detail) VALUES (%s,%s,%s,%s,%s,%s)",
      (lead_id, direction, msg_type, body, ok, detail))
