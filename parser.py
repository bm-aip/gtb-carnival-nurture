import re
from datetime import date
import config

# Option index -> event date
def _dates():
    return config.EVENT_DATES  # [Fri 10, Sat 11, Sun 12]

WEEKDAYS = {"friday": 0, "fri": 0, "saturday": 1, "sat": 1, "sunday": 2, "sun": 2}


def parse_date_reply(text):
    """Return a date from EVENT_DATES, or None."""
    if not text:
        return None
    t = text.strip().lower()
    dates = _dates()

    # Poll option text like "Friday 10 July" / "Fri 10"
    for wd, idx in WEEKDAYS.items():
        if wd in t:
            return dates[idx]

    # Bare option number 1/2/3
    m = re.fullmatch(r"[^\d]*([123])[^\d]*", t)
    if m:
        return dates[int(m.group(1)) - 1]

    # Day-of-month 10/11/12 anywhere ("10th", "on 11", "12 july")
    for m in re.finditer(r"\b(1[0-2])(st|nd|rd|th)?\b", t):
        d = int(m.group(1))
        for ed in dates:
            if ed.day == d:
                return ed

    # Common words
    if "tomorrow" in t:
        from datetime import timedelta
        tm = date.today() + timedelta(days=1)
        if tm in dates:
            return tm
    if "today" in t and date.today() in dates:
        return date.today()

    return None
