"""Import the app's modules without a database, an API key or a network.

config.py reads a dozen environment variables at import time and raises KeyError
without them. Every one stubbed here is a credential the guard tests never use --
they exercise pure functions. If a test needs the real corpus or the real model it
belongs in conversations.py, which runs under `railway run`.
"""
import io
import os
import sys

# The Windows console is cp1252 and dies on the rupee sign. That cost real time on
# 2026-08-02: twice I reported "encoding corruption" in the bot's replies that was
# only the terminal mangling the display, and once a script crashed mid-run on a
# buyer's name. A test tool must never lie about the text it is testing.
for _stream in ("stdout", "stderr"):
    _s = getattr(sys, _stream)
    if hasattr(_s, "buffer") and (_s.encoding or "").lower() not in ("utf-8", "utf8"):
        setattr(sys, _stream, io.TextIOWrapper(_s.buffer, encoding="utf-8",
                                               errors="replace", line_buffering=True))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Under `railway run`, DATABASE_URL points at postgres.railway.internal, which only
# resolves inside Railway's network. The public URL is injected alongside it and is
# what a laptop can actually reach.
if os.environ.get("DATABASE_PUBLIC_URL"):
    os.environ["DATABASE_URL"] = os.environ["DATABASE_PUBLIC_URL"]

for _k in ("DATABASE_URL", "SELLDO_DB_URL_RON", "SELLDO_DB_URL_ELEMENTS",
           "META_TOKEN_RON", "META_TOKEN_ELEMENTS", "WATI_TOKEN", "WATI_BASE_URL",
           "ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "META_APP_SECRET",
           "VERIFY_TOKEN", "ADMIN_PASSWORD"):
    os.environ.setdefault(_k, "test-stub")
os.environ.setdefault("DISABLE_SCHEDULER", "1")


class Results:
    """A deliberately tiny assert runner.

    No pytest. This must be runnable by anyone who can run python, including in a
    hurry on a phone-tethered laptop, without installing anything.
    """

    def __init__(self):
        self.passed = 0
        self.failures = []
        self.known = []

    def check(self, name, ok, detail="", known_bug=False):
        if ok:
            self.passed += 1
            return True
        (self.known if known_bug else self.failures).append((name, detail))
        return False

    def eq(self, name, got, want, known_bug=False):
        return self.check(name, got == want, f"got {got!r}, want {want!r}", known_bug)

    def report(self, title):
        print(f"\n{title}")
        print("-" * len(title))
        print(f"  passed: {self.passed}")
        if self.known:
            print(f"  KNOWN OPEN DEFECTS (expected to fail): {len(self.known)}")
            for n, d in self.known:
                print(f"     ~ {n}\n         {d}")
        if self.failures:
            print(f"  FAILED: {len(self.failures)}")
            for n, d in self.failures:
                print(f"     x {n}\n         {d}")
        else:
            print("  no regressions")
        return not self.failures
