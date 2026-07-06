import os
import re
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

def _get(url, params):
    r = requests.get(url, params=params, timeout=40)
    return r.json()


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


def poll_meta_leads():
    """Cache all lead-form submissions since LEADS_SINCE into meta_leads."""
    since = config.LEADS_SINCE + "T00:00:00+0000"
    for pk in config.META_TOKENS:
        try:
            for page in get_pages(pk):
                ptoken = page.get("access_token")
                if not ptoken:
                    continue
                for form in get_forms(page["id"], ptoken):
                    for lead in fetch_form_leads(form["id"], ptoken, since):
                        import parser as reply_parser
                        pd = reply_parser.parse_date_reply(
                            (lead.get("preferred_raw") or "").replace("_", " "))
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
            db.set_setting(f"meta_leads_error_{pk}", str(e)[:500])


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
    """Campaign insights, restricted to META_ACCOUNT_IDS if set; both tokens
    see the same accounts so `seen` prevents double-polling."""
    seen = set()
    for pk in config.META_TOKENS:
        try:
            for acct in discover_accounts(pk):
                if ALLOWED_ACCOUNTS and acct["id"] not in ALLOWED_ACCOUNTS:
                    continue
                if acct["id"] in seen:
                    continue
                seen.add(acct["id"])
                _poll_account(pk, acct["id"])
            db.set_setting(f"meta_error_{pk}", "")
        except Exception as e:
            db.set_setting(f"meta_error_{pk}", str(e)[:500])


def _poll_account(project_key, account_id):
    token = config.META_TOKENS[project_key]
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
        leads = 0
        for a in row.get("actions", []) or []:
            if a.get("action_type") in ("lead", "onsite_conversion.lead_grouped",
                                        "leadgen_grouped"):
                leads += int(float(a.get("value", 0)))
        db.x("""INSERT INTO campaign_mapping (campaign_id, campaign_name, account_id)
                VALUES (%s,%s,%s)
                ON CONFLICT (campaign_id) DO UPDATE SET campaign_name=EXCLUDED.campaign_name""",
             (row["campaign_id"], row.get("campaign_name"), account_id))
        db.x("""INSERT INTO campaign_stats (campaign_id, stat_date, spend, impressions, clicks, leads)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (campaign_id, stat_date) DO UPDATE SET
                  spend=EXCLUDED.spend, impressions=EXCLUDED.impressions,
                  clicks=EXCLUDED.clicks, leads=EXCLUDED.leads""",
             (row["campaign_id"], row.get("date_start"), row.get("spend", 0),
              row.get("impressions", 0), row.get("clicks", 0), leads))
