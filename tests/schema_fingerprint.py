"""The database's shape is checked when it changes, not on every boot.

    railway run --service gtb-carnival-nurture python tests/schema_fingerprint.py

Needs a real Postgres, because the whole point is DDL behaviour and a fresh
install, and neither is provable against a fake. Every test works inside its own
throwaway schema and drops it afterwards, so it never touches live tables.

WHY THIS EXISTS
---------------
2026-09-05. `init_db()` ran ~400 statements in one transaction on every boot, in
both the web process and the worker, which a deploy restarts together. That
transaction holds an AccessExclusiveLock on fifteen tables at once; an ordinary
query touching two of them the other way round is a deadlock waiting for the
timing to line up. Two of three boots died and the bot was down ten minutes --
the watchdog missed a run by thirteen, so the thing that would have raised the
alarm was the thing that was off.

The schema had not changed in twenty-three deploys. All four hundred statements,
every time, to confirm nothing had moved.

The rule these tests hold down: only proven success is trusted, and everything
else is retried. A fingerprint is written ONLY after a full successful apply, so
a migration that dies halfway leaves no note and the next boot does it again.
"""
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(__file__))
from _bootstrap import Results        # noqa: E402

import psycopg2                        # noqa: E402
import db                              # noqa: E402

R = Results()
SANDBOX = "fingerprint_test"


def raw(sql, fetch=False):
    """One statement outside the sandbox indirection, for setup and inspection."""
    c = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with c.cursor() as cur:
            cur.execute("SET search_path TO %s, public" % SANDBOX)
            cur.execute(sql)
            out = cur.fetchone()[0] if fetch else None
        c.commit()
        return out
    finally:
        c.close()


@contextmanager
def sandboxed():
    """db.conn(), but every unqualified table name resolves inside the sandbox.

    search_path is set on the connection, so `CREATE TABLE leads` lands in the
    sandbox and the live `leads` is never in reach.
    """
    c = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with c.cursor() as cur:
            cur.execute("SET search_path TO %s" % SANDBOX)
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def boot(**kw):
    """Run init_db() as though the sandbox were the whole database."""
    real = db.conn
    db.conn = sandboxed
    try:
        return db.init_db(**kw)
    finally:
        db.conn = real


def tables(name=None):
    where = " AND table_name='%s'" % name if name else ""
    return raw("SELECT count(*) FROM information_schema.tables "
               "WHERE table_schema='%s'%s" % (SANDBOX, where), fetch=True)


# Every DDL statement bumps the catalog, so a frozen catalog proves no DDL ran.
# That is the difference between "said unchanged" and "took no locks".
def catalog():
    return raw("SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
               "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='%s'"
               % SANDBOX, fetch=True)


def recorded():
    return raw("SELECT count(*) FROM settings WHERE key='%s'" % db.SCHEMA_KEY, fetch=True)


def recorded_is(value):
    return raw("SELECT count(*) FROM settings WHERE key='%s' AND value='%s'"
               % (db.SCHEMA_KEY, value), fetch=True) == 1


@contextmanager
def schema_edited_to(text):
    real = db.SCHEMA
    db.SCHEMA = text
    try:
        yield
    finally:
        db.SCHEMA = real


# --- the fingerprint itself ---------------------------------------------------
R.check("a fingerprint is short and stable",
        db.schema_fingerprint() == db.schema_fingerprint()
        and len(db.schema_fingerprint()) == 16)
R.check("changing one character changes it",
        db.schema_fingerprint("a") != db.schema_fingerprint("a "),
        detail="taken over the SCHEMA text, so nobody has to remember to bump a version")

raw_setup = psycopg2.connect(os.environ["DATABASE_URL"])
with raw_setup.cursor() as _cur:
    _cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SANDBOX)
    _cur.execute("CREATE SCHEMA %s" % SANDBOX)
raw_setup.commit()
raw_setup.close()

try:
    # --- a database we have never seen ----------------------------------------
    # THE PATH THAT NEARLY SHIPPED BROKEN. On a fresh database the `settings`
    # lookup fails, and a failed statement aborts the whole transaction -- so
    # without the savepoint the very first install dies on InFailedSqlTransaction
    # before it creates a single table.
    R.eq("a fresh database gets the full schema", boot(), "applied")
    R.check("and the tables really exist", tables() >= 15,
            detail="SCHEMA has 15 CREATE TABLE statements, found %d" % tables())
    R.check("and it wrote down what it built", recorded_is(db.schema_fingerprint()))

    # --- the boot that should do nothing --------------------------------------
    # 23 of the last 23 deploys took this path. This is the whole benefit.
    R.eq("a second boot with the same schema applies nothing", boot(), "unchanged")

    before = catalog()
    boot()
    R.check("and provably ran no DDL at all", catalog() == before,
            detail="the deadlock came from DDL locks, so zero DDL is the fix; "
                   "catalog went %d -> %d" % (before, catalog()))

    # --- a real change is noticed ---------------------------------------------
    with schema_edited_to(db.SCHEMA + "\nCREATE TABLE IF NOT EXISTS a_new_table (id INT);"):
        R.eq("an edited schema is applied", boot(), "applied")
        R.eq("and the new table is there", tables("a_new_table"), 1)
        R.eq("and the boot after that is quiet again", boot(), "unchanged")

    # Reverting SCHEMA is itself a change, so the next boot must notice.
    R.eq("going back to the old schema is also a change", boot(), "applied")

    # --- THE SAFETY PROPERTY --------------------------------------------------
    # A migration that dies halfway must leave NO note, so the next boot retries.
    # Without this the database could sit half-built while every later boot
    # cheerfully skipped the repair.
    good = db.schema_fingerprint()
    R.check("precondition: the good fingerprint is on record", recorded_is(good))

    with schema_edited_to(db.SCHEMA + "\nCREATE TABLE this_is_not_valid_sql ((;"):
        failed = False
        try:
            boot()
        except psycopg2.Error:
            failed = True
        R.check("a broken migration raises rather than pretending it worked", failed)
        R.check("and leaves the OLD fingerprint, so the next boot tries again",
                recorded_is(good),
                detail="only proven success is ever trusted")

    # --- contention is retried, a broken migration is not ---------------------
    # THE HALF I FIRST FORGOT. A lock_timeout without a retry is still a failed
    # boot -- a tidier error than a deadlock, but the bot is equally down. Proved
    # by counting attempts rather than by reading the code.
    real_conn = db.conn
    attempts = []

    def busy_then_free(fail_times):
        @contextmanager
        def wrapper():
            attempts.append(1)
            if len(attempts) <= fail_times:
                raise psycopg2.errors.LockNotAvailable(
                    "canceling statement due to lock timeout")
            with sandboxed() as c:
                yield c
        return wrapper

    real_sleep = db.time.sleep
    db.time.sleep = lambda _s: None          # do not actually wait 2+4+8 seconds
    try:
        attempts[:] = []
        db.conn = busy_then_free(3)
        try:
            R.eq("a busy database is retried, not given up on",
                 db.init_db(force=True), "applied (forced)")
        finally:
            db.conn = real_conn
        R.eq("and it took exactly the failed tries plus one", len(attempts), 4)

        # A deadlock is contention too -- that is the error we actually saw.
        attempts[:] = []
        db.conn = busy_then_free(1)
        try:
            db.init_db(force=True)
        finally:
            db.conn = real_conn
        R.eq("a deadlock is retried the same way", len(attempts), 2)

        # But a migration that is simply wrong must fail now, not in four minutes.
        attempts[:] = []
        db.conn = busy_then_free(0)
        with schema_edited_to(db.SCHEMA + "\nCREATE TABLE bad ((;"):
            broke = False
            try:
                db.init_db(force=True)
            except psycopg2.Error:
                broke = True
            finally:
                db.conn = real_conn
        R.check("a broken migration is NOT retried", broke and len(attempts) == 1,
                detail="retrying a syntax error six times only delays the truth; "
                       "attempts=%d" % len(attempts))

        attempts[:] = []
        db.conn = busy_then_free(db.SCHEMA_APPLY_TRIES)
        gave_up = False
        try:
            db.init_db(force=True)
        except psycopg2.Error:
            gave_up = True
        finally:
            db.conn = real_conn
        R.check("and retrying is bounded, it does not loop forever",
                gave_up and len(attempts) == db.SCHEMA_APPLY_TRIES,
                detail="attempts=%d, limit=%d" % (len(attempts), db.SCHEMA_APPLY_TRIES))
    finally:
        db.time.sleep = real_sleep
        db.conn = real_conn

    boot(force=True)   # leave the sandbox whole for the checks below

    # --- the escape hatch -----------------------------------------------------
    # /admin/schema-check. A database somebody edited by hand is no longer
    # repaired at boot, so there has to be a way to force the re-apply.
    before = catalog()
    R.eq("force re-applies even when the fingerprint matches",
         boot(force=True), "applied (forced)")
    R.check("and SCHEMA is safe to run twice", catalog() == before,
            detail="every statement is IF NOT EXISTS or ON CONFLICT DO NOTHING")

    raw("DROP TABLE IF EXISTS meta_form_polls CASCADE")
    R.eq("precondition: the table is gone", tables("meta_form_polls"), 0)
    boot(force=True)
    R.eq("force repairs a table somebody dropped by hand",
         tables("meta_form_polls"), 1)

    # And an ordinary boot does NOT, which is the cost we accepted knowingly.
    raw("DROP TABLE IF EXISTS meta_form_polls CASCADE")
    R.eq("an ordinary boot leaves it broken, by design", boot(), "unchanged")
    R.eq("which is the trade: it fails loudly at first use, not silently at boot",
         tables("meta_form_polls"), 0)
    boot(force=True)
finally:
    raw_cleanup = psycopg2.connect(os.environ["DATABASE_URL"])
    with raw_cleanup.cursor() as _cur:
        _cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SANDBOX)
    raw_cleanup.commit()
    raw_cleanup.close()

if __name__ == "__main__":
    sys.exit(0 if R.report("SCHEMA FINGERPRINT") else 1)
