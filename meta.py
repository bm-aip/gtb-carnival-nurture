import os
import re
from datetime import datetime, timedelta, timezone
import requests
import config
import db


def resolve_phone(project_key, meta_lead_id):
    """Legacy direct lookup — kept for the manual path; utm_lead_id proved
    unpopulated in Sell.do reporting DB, so bulk resolution now goes through
    poll_meta_leads() + match.run_matching()."""
    token = config.META_TOKENS[project_key]
    try:
        r = requests.get(f"{config.GRAPH}/{meta_lead_id}",
                         params={"access_token": token, "fields": "field_data"},
                         timeout=20)
        j = r.json()
        if "error" in j:
            return None, j["error"].get("message", "graph error")
        for f in j.get("field_data", []):
            if "phone" in f.get("name", "").lower():
                raw = (f.get("values") or [""])[0]
                return normalize_phone(raw), None
        return None, "no phone field in lead form data"
    except Exception as e:
        return None, str(e)


# ---------- lead-form polling (primary phone source) ----------

import time

def _get(url, params, retries=3):
    """GET with retry/backoff — Meta resets connections under burst load."""
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=40)
            return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            time.sleep(2 ** attempt)  # 1s, 2s, 4s
    raise last


def get_pages(project_key):
    """Pages this token manages, WITH page access tokens."""
    j = _get(f"{config.GRAPH}/me/accounts",
             {"access_token": config.META_TOKENS[project_key],
              "fields": "id,name,access_token", "limit": 100})
    pages = j.get("data", [])
    allowed = config.META_PAGE_IDS.get(project_key) or []
    if allowed:
        pages = [p for p in pages if p["id"] in allowed]
    return pages


def get_forms(page_id, page_token):
    j = _get(f"{config.GRAPH}/{page_id}/leadgen_forms",
             {"access_token": page_token, "fields": "id,name,status", "limit": 100})
    return j.get("data", [])


def _extract_name_phone(field_data):
    """Returns (name, phone, preferred_date_str). Some Carnival forms ask
    'preferred_carnival_visit_date' / 'preferred_day_for_visiting...' —
    capture the raw answer; parser.parse_date_reply turns it into a date."""
    name_parts, phone, pref = {}, None, None
    for f in field_data or []:
        fname = (f.get("name") or "").lower()
        val = (f.get("values") or [""])[0]
        if "phone" in fname or "whatsapp" in fname:
            phone = phone or normalize_phone(val)
        elif fname in ("full_name", "name"):
            name_parts["full"] = val
        elif "first" in fname:
            name_parts["first"] = val
        elif "last" in fname:
            name_parts["last"] = val
        elif "visit_date" in fname or "preferred_day" in fname or "visit_day" in fname:
            pref = val
    name = name_parts.get("full") or " ".join(
        x for x in (name_parts.get("first"), name_parts.get("last")) if x)
    return name or None, phone, pref


def fetch_form_leads(form_id, page_token, since_iso):
    """All leads on a form since `since_iso` (paginates)."""
    out = []
    url = f"{config.GRAPH}/{form_id}/leads"
    params = {"access_token": page_token, "fields": "id,created_time,field_data",
              "limit": 100}
    while url:
        j = _get(url, params)
        for lead in j.get("data", []):
            if lead.get("created_time", "") < since_iso:
                return out
            name, phone, pref = _extract_name_phone(lead.get("field_data"))
            out.append({"id": lead["id"], "created_time": lead.get("created_time"),
                        "name": name, "phone": phone, "preferred_raw": pref})
        url = (j.get("paging") or {}).get("next")
        params = {}  # next url carries everything
    return out


FORM_FILTER = [k.strip().lower() for k in
               os.environ.get("FORM_FILTER", "").split(",") if k.strip()]


def _form_wanted(form_name):
    if not FORM_FILTER:
        return True
    n = (form_name or "").lower()
    return any(k in n for k in FORM_FILTER)


# WHICH FORMS ARE WORTH A GRAPH CALL. Added 2026-08-07.
#
# Meta lists 103 lead forms across the two pages; 9 produced a lead in the last week
# and 94 are the back catalogue. Walking all of them cost ~3 minutes against a
# 1-minute schedule, so APScheduler refused two fires in three and wrote a skip line
# for each. Nothing was lost -- but the log became one repeated sentence, and a log
# nobody can read is how the credit outage ran for eight hours unnoticed.
#
# A form earns a call if it is UNKNOWN (never polled -- so a form marketing launched
# five minutes ago is always picked up) or PRODUCTIVE (a lead inside the window).
FORM_ACTIVE_DAYS = int(os.environ.get("META_FORM_ACTIVE_DAYS", "14"))

# THE SAFETY NET, and it is not optional. The longest real gap between two
# consecutive leads on a live form in this account's history is 19 DAYS -- longer
# than the window above. Without a periodic sweep of everything, that form would
# fall out of the fast lane and its next lead would never be fetched at all. With
# it, the worst case is an hour late, and one lead puts the form straight back into
# the fast lane on its own.
FULL_SWEEP_MIN = int(os.environ.get("META_FULL_SWEEP_MIN", "60"))
_SWEEP_KEY = "meta_full_sweep_at"


def _full_sweep_due(now=None):
    """True when every form should be polled, not just the active ones."""
    now = now or datetime.now(timezone.utc)
    last = db.get_setting(_SWEEP_KEY)
    if not last:
        return True                      # never swept -> sweep
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True                      # unreadable marker -> sweep, don't guess
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when) >= timedelta(minutes=FULL_SWEEP_MIN)


def _worth_polling(forms, sweep, now=None):
    """Filter Meta's form list down to the ones this pass should fetch leads for.

    Unknown forms are ALWAYS kept. That is the direction this must fail in: the
    mistake it can make is doing too much work, never missing a lead."""
    if sweep:
        return list(forms)
    ids = [f["id"] for f in forms if f.get("id")]
    if not ids:
        return []
    known = db.q("""SELECT form_id, last_lead_at FROM meta_form_polls
                    WHERE form_id = ANY(%s)""", (ids,)) or []
    last_lead = {r["form_id"]: r["last_lead_at"] for r in known}
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=FORM_ACTIVE_DAYS)
    keep = []
    for f in forms:
        if f.get("id") not in last_lead:
            keep.append(f)                                   # never polled
        elif last_lead[f["id"]] and last_lead[f["id"]] >= cutoff:
            keep.append(f)                                   # productive lately
    return keep


def _record_poll(form, project_key, page_id, leads):
    """Write down that we looked, and the newest lead we saw if there was one."""
    newest = max((l.get("created_time") for l in leads if l.get("created_time")),
                 default=None)
    db.x("""INSERT INTO meta_form_polls (form_id, project, page_id, form_name,
                                         last_polled_at, last_lead_at, leads_seen)
            VALUES (%s,%s,%s,%s, now(), %s::timestamptz, %s)
            ON CONFLICT (form_id) DO UPDATE SET
                project        = EXCLUDED.project,
                page_id        = EXCLUDED.page_id,
                form_name      = EXCLUDED.form_name,
                last_polled_at = now(),
                -- GREATEST ignores NULLs in Postgres, so a poll that found nothing
                -- leaves the existing date alone instead of erasing it. Erasing it
                -- would drop a live form out of the fast lane on its first quiet
                -- pass, which is every pass between leads.
                last_lead_at   = GREATEST(meta_form_polls.last_lead_at,
                                          EXCLUDED.last_lead_at),
                leads_seen     = EXCLUDED.leads_seen""",
         (form["id"], project_key, page_id, form.get("name"), newest, len(leads)))


def poll_meta_leads():
    """Cache lead-form submissions since LEADS_SINCE into meta_leads.

    Only forms that are unknown or recently productive are fetched, with a full
    sweep every FULL_SWEEP_MIN minutes -- see _worth_polling and _full_sweep_due.
    FORM_FILTER (comma keywords, case-insensitive substring on form name) is a
    manual override on top of that; throttled to avoid Meta connection resets."""
    since = config.LEADS_SINCE + "T00:00:00+0000"
    sweep = _full_sweep_due()
    swept_clean = True
    for pk in config.META_TOKENS:
        try:
            for page in get_pages(pk):
                ptoken = page.get("access_token")
                if not ptoken:
                    continue
                forms = [f for f in get_forms(page["id"], ptoken)
                         if _form_wanted(f.get("name"))]
                for form in _worth_polling(forms, sweep):
                    time.sleep(0.4)
                    found = fetch_form_leads(form["id"], ptoken, since)
                    _record_poll(form, pk, page["id"], found)
                    for lead in found:
                        # preferred_date is no longer derived. It used to parse
                        # the form's free-text answer into one of the three
                        # carnival days (Phase 0 task 1b removed that parser).
                        # A nurture lead has no event day to prefer; if a future
                        # form asks for a visit day it is captured, never
                        # confirmed (POST-CARNIVAL-DESIGN §8), so it does not
                        # belong in an automated date column.
                        pd = None
                        db.x("""INSERT INTO meta_leads
                                (meta_lead_id, project, page_id, form_id, form_name,
                                 name, phone, created_time, preferred_date)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                ON CONFLICT (meta_lead_id) DO NOTHING""",
                             (lead["id"], pk, page["id"], form["id"],
                              form.get("name"), lead["name"], lead["phone"],
                              lead["created_time"], pd))
            db.set_setting(f"meta_leads_error_{pk}", "")
        except Exception as e:
            swept_clean = False
            db.set_setting(f"meta_leads_error_{pk}", str(e)[:500])

    # The marker moves ONLY when a sweep actually finished every project. The
    # per-project `except` above deliberately swallows one broken token so the other
    # project's leads still arrive -- but a swallowed error means forms went
    # unchecked, and recording a sweep that did not happen would hide exactly the
    # dormant form the sweep exists to catch. Leave it stale and sweep again.
    if sweep and swept_clean:
        db.set_setting(_SWEEP_KEY, datetime.now(timezone.utc).isoformat())

    # HEARTBEAT. Written only on reaching the end of a run, so a hung run writes
    # nothing and goes stale -- which is the whole point.
    #
    # 2026-08-21: one run hung and held the only slot for 24 hours. APScheduler
    # logged "maximum number of running instances reached (1)" every minute and
    # the system looked healthy from every angle we were watching: no exception,
    # no failed job, meta_leads_error empty. Form submissions simply stopped
    # arriving. watchdog._check_poller_wedged reads this timestamp.
    #
    # Deliberately NOT gated on swept_clean. A partial sweep still proves the
    # poller is alive and cycling, which is the only thing this claims; a broken
    # token is already reported through meta_leads_error_<project>.
    db.set_setting("meta_leads_last_ok", datetime.now(timezone.utc).isoformat())


def fetch_lead(project_key, leadgen_id):
    """One lead, by the id Meta hands us in a leadgen webhook.

    Uses the PAGE token, not the user token: lead retrieval is a page-scoped
    permission and the user token is refused on some accounts.

    Returns {"meta_lead_id","name","phone","created_time","form_id","form_name"}
    or None. Never raises -- a webhook that 500s is a webhook Meta retries.
    """
    try:
        pages = get_pages(project_key)
    except Exception:
        return None
    if not pages:
        return None
    ptoken = pages[0].get("access_token")
    if not ptoken:
        return None

    try:
        j = _get(f"{config.GRAPH}/{leadgen_id}",
                 {"access_token": ptoken,
                  "fields": "id,created_time,field_data,form_id"})
    except Exception:
        return None
    if not j or "error" in j:
        return None

    name, phone, _pref = _extract_name_phone(j.get("field_data"))
    form_id = j.get("form_id")
    form_name = None
    if form_id:
        try:
            f = _get(f"{config.GRAPH}/{form_id}",
                     {"access_token": ptoken, "fields": "name"})
            form_name = (f or {}).get("name")
        except Exception:
            pass
    return {"meta_lead_id": str(j.get("id") or leadgen_id),
            "name": name, "phone": phone,
            "created_time": j.get("created_time"),
            "form_id": form_id, "form_name": form_name,
            "page_id": pages[0]["id"]}


def promote_meta_leads():
    """Queue a cached Meta form lead straight into the sequencer, skipping the
    Sell.do 'Interested' gate.

    Rationale: Sell.do only stages a lead once presales has phoned them, which
    on event day is far slower than the event itself. A lead who filled a
    carnival form already declared intent, so the form fill IS the qualification.

    Three hard bounds, because this bypasses the human check:
      - form_name must be on config.PROMOTE_FORMS (exact match, never substring)
      - created within config.PROMOTE_WINDOW_HOURS
      - phone not already attached to any lead (a Sell.do-sourced row may have
        matched the same person, and DISTINCT ON collapses same-phone refills)

    The form's own preferred_date rides along as selected_date, so someone who
    already picked a day gets the reminder track, not another invite. Rows land
    as 'queued' with a phone already set -- match.run_matching() never sees them.
    """
    if not config.PROMOTE_ENABLED or not config.PROMOTE_FORMS:
        return 0
    n = db.x("""
        INSERT INTO leads (project, selldo_lead_id, meta_lead_id, name, phone,
                           selldo_status, selldo_response_at, campaign, wa_state,
                           selected_date)
        SELECT DISTINCT ON (m.phone)
               m.project, 'meta:' || m.meta_lead_id, m.meta_lead_id, m.name, m.phone,
               'meta_direct', m.created_time,
               -- The form name IS the campaign for a Meta-sourced lead. Without this
               -- the row lands with campaign NULL and the allow-list gate in
               -- worker.py silences it -- the gate fails closed, so an untagged lead
               -- is an unanswerable lead. The form names are allow-listed alongside
               -- the Sell.do campaign names in config.SELLDO[*]["campaigns"].
               m.form_name,
               'queued', m.preferred_date
        FROM meta_leads m
        WHERE m.phone IS NOT NULL
          AND m.form_name = ANY(%s)
          AND m.created_time > now() - (%s || ' hours')::interval
          AND NOT EXISTS (SELECT 1 FROM leads l WHERE l.meta_lead_id = m.meta_lead_id)
          AND NOT EXISTS (SELECT 1 FROM leads l WHERE l.phone = m.phone)
        ORDER BY m.phone, m.created_time DESC
        ON CONFLICT (project, selldo_lead_id) DO NOTHING""",
        (config.PROMOTE_FORMS, str(config.PROMOTE_WINDOW_HOURS)))
    if n:
        db.set_setting("last_promoted_at", f"{n} leads")
    return n


def normalize_phone(raw):
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 10:
        d = "91" + d
    if d.startswith("0") and len(d) == 11:
        d = "91" + d[1:]
    return d or None


def discover_accounts(project_key):
    token = config.META_TOKENS[project_key]
    r = requests.get(f"{config.GRAPH}/me/adaccounts",
                     params={"access_token": token, "fields": "id,name", "limit": 100},
                     timeout=20)
    return r.json().get("data", [])


ALLOWED_ACCOUNTS = [a.strip() for a in
                    os.environ.get("META_ACCOUNT_IDS", "").split(",") if a.strip()]


def poll_campaign_stats():
    """Campaign insights. If META_ACCOUNT_IDS is set, those accounts are polled
    directly (authoritative list) — each with the first token that works;
    per-account failures are recorded, not fatal. If unset, falls back to
    discovering accounts per token."""
    seen = set()
    if ALLOWED_ACCOUNTS:
        errs = []
        for acct_id in ALLOWED_ACCOUNTS:
            if acct_id in seen:
                continue
            last_err = None
            for pk in config.META_TOKENS:
                try:
                    _poll_account(pk, acct_id)
                    seen.add(acct_id)
                    last_err = None
                    break
                except Exception as e:
                    last_err = str(e)[:200]
            if last_err:
                errs.append(f"{acct_id}: {last_err}")
        db.set_setting("meta_error_RON", "")
        db.set_setting("meta_error_ELEMENTS", " | ".join(errs) if errs else "")
        return
    for pk in config.META_TOKENS:
        try:
            for acct in discover_accounts(pk):
                if acct["id"] in seen:
                    continue
                seen.add(acct["id"])
                _poll_account(pk, acct["id"])
            db.set_setting(f"meta_error_{pk}", "")
        except Exception as e:
            db.set_setting(f"meta_error_{pk}", str(e)[:500])


LEADGEN_OBJECTIVES = {"OUTCOME_LEADS", "LEAD_GENERATION"}


def _campaign_objectives(token, account_id):
    """campaign_id -> objective for an account."""
    out = {}
    j = _get(f"{config.GRAPH}/{account_id}/campaigns",
             {"access_token": token, "fields": "id,objective", "limit": 200})
    for c in j.get("data", []):
        out[c["id"]] = c.get("objective")
    return out


def _poll_account(project_key, account_id):
    token = config.META_TOKENS[project_key]
    objectives = _campaign_objectives(token, account_id)
    if not objectives:
        # distinguish "no campaigns" from "no access": probe the account node
        probe = _get(f"{config.GRAPH}/{account_id}", {"access_token": token,
                                                      "fields": "id"})
        if "error" in probe:
            raise RuntimeError(probe["error"].get("message", "no access"))
    r = requests.get(
        f"{config.GRAPH}/{account_id}/insights",
        params={
            "access_token": token,
            "level": "campaign",
            "fields": "campaign_id,campaign_name,spend,impressions,clicks,actions",
            "time_range": '{"since":"2026-06-25","until":"2026-07-13"}',
            "time_increment": 1,
            "limit": 200,
        }, timeout=40)
    for row in r.json().get("data", []):
        obj = objectives.get(row["campaign_id"])
        if obj not in LEADGEN_OBJECTIVES:
            continue
        leads = 0
        for a in row.get("actions", []) or []:
            if a.get("action_type") in ("lead", "onsite_conversion.lead_grouped",
                                        "leadgen_grouped"):
                leads += int(float(a.get("value", 0)))
        db.x("""INSERT INTO campaign_mapping (campaign_id, campaign_name, account_id, objective)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (campaign_id) DO UPDATE SET
                  campaign_name=EXCLUDED.campaign_name, objective=EXCLUDED.objective""",
             (row["campaign_id"], row.get("campaign_name"), account_id, obj))
        db.x("""INSERT INTO campaign_stats (campaign_id, stat_date, spend, impressions, clicks, leads)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (campaign_id, stat_date) DO UPDATE SET
                  spend=EXCLUDED.spend, impressions=EXCLUDED.impressions,
                  clicks=EXCLUDED.clicks, leads=EXCLUDED.leads""",
             (row["campaign_id"], row.get("date_start"), row.get("spend", 0),
              row.get("impressions", 0), row.get("clicks", 0), leads))
