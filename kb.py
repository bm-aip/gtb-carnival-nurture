"""Knowledge base schema and fenced retrieval (task 8).

pgvector on the existing Railway Postgres. No second datastore: the corpus is tens
of documents per project, and a specialist vector store would buy scale we will
never use at the price of a system to deploy, secure, back up and keep in sync with
the leads it serves (design §11).

WHY THIS IS A SEPARATE MODULE WITH ITS OWN INIT
-----------------------------------------------
`db.init_db()` runs at import time in app.py. `CREATE EXTENSION vector` needs a
privilege the database user may not have, and the `vector` column type does not
exist until the extension does -- so putting this schema in db.SCHEMA would mean a
Railway instance without pgvector fails to BOOT THE WEB APP. The webhook would stop
answering because of a knowledge-base problem, which is absurd.

So: `init_kb()` is called separately, never raises, and records what happened in
`settings`. If pgvector is unavailable the app runs exactly as before and the ingest
(task 10) refuses to run rather than half-working.

THE BRAND FENCE
---------------
`brand_id` is denormalised onto every chunk on purpose. Retrieval is then a
single-table WHERE with no join to get wrong, and `search()` below takes brand_id as
its first positional argument and interpolates it into every query -- there is no
code path that searches without one. Task 11 exists to prove that by trying to
break it.
"""
import config
import db

BRANDS_SEED = ("RON", "ELEMENTS")


def _schema(dim):
    """DDL, parameterised by embedding dimension.

    `dim` is interpolated rather than bound because a column TYPE cannot be a query
    parameter. It comes from `int()` in config, and is re-checked here -- this string
    is executed, so an int assertion is the difference between a config typo and a
    SQL injection.
    """
    if not isinstance(dim, int) or not (1 <= dim <= 16000):
        raise ValueError(f"EMBED_DIM must be a sane int, got {dim!r}")
    return f"""
CREATE EXTENSION IF NOT EXISTS vector;

-- One row per project. Adding a project is a ROW, not a release (design §11).
CREATE TABLE IF NOT EXISTS brands (
    brand_id TEXT PRIMARY KEY,          -- 'RON' | 'ELEMENTS' | ...
    display_name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Source documents. VERSIONED, with an active flag and the uploader recorded,
-- because §10's audit guardrail requires answering "which document was live on the
-- day the bot said that" -- in property that is the defence.
CREATE TABLE IF NOT EXISTS kb_documents (
    id SERIAL PRIMARY KEY,
    brand_id TEXT NOT NULL REFERENCES brands(brand_id),
    title TEXT NOT NULL,
    source_path TEXT,                   -- where it came from
    doc_type TEXT,                      -- faq | location | brochure | spec | plan
    version INT NOT NULL DEFAULT 1,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    uploaded_by TEXT,
    content_hash TEXT,                  -- skip re-ingesting an unchanged file
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (brand_id, title, version)
);
CREATE INDEX IF NOT EXISTS idx_kbdocs_brand ON kb_documents (brand_id, active);

-- Searchable pieces. brand_id is DENORMALISED here deliberately: retrieval is then
-- one table with one WHERE and no join to get wrong.
CREATE TABLE IF NOT EXISTS kb_chunks (
    id SERIAL PRIMARY KEY,
    brand_id TEXT NOT NULL REFERENCES brands(brand_id),
    document_id INT NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
    ordinal INT NOT NULL,               -- position within the document
    content TEXT NOT NULL,
    -- Curator-supplied guard that must travel WITH the fact, not just live in
    -- template copy. e.g. the no-natural-private-beach rule on the Covelong row.
    guardrail TEXT,
    embed_model TEXT,                   -- so a model change is detectable
    embedding vector({dim}),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kbchunks_brand ON kb_chunks (brand_id);
CREATE INDEX IF NOT EXISTS idx_kbchunks_doc ON kb_chunks (document_id);

-- Vector index. Cosine, because embeddings are normalised and cosine is what the
-- providers' own similarity is defined in.
CREATE INDEX IF NOT EXISTS idx_kbchunks_embedding
    ON kb_chunks USING hnsw (embedding vector_cosine_ops);

-- One agent per project: persona, price policy, checklist, and the §7 persuasion
-- material. JSONB rather than columns because the framings are lists that sales
-- will edit, and a new framing must not be a migration.
CREATE TABLE IF NOT EXISTS agents (
    brand_id TEXT PRIMARY KEY REFERENCES brands(brand_id),
    persona TEXT,
    system_prompt TEXT,
    price_policy TEXT,                  -- 'none_published' for RON today
    budget_floor NUMERIC,
    budget_ceiling NUMERIC,
    checklist JSONB,                    -- the gates, their order and hardness
    framings JSONB,                     -- §7 reason-lines, 3 per gate, no repeats
    guardrails JSONB,                   -- claims rules enforced on every answer
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def init_kb():
    """Create the KB schema. Never raises; records the outcome in settings.

    Returns a dict describing what happened, so /api/kb can answer "is the
    knowledge base actually available" without anyone reading logs.
    """
    try:
        with db.conn() as c:
            with c.cursor() as cur:
                cur.execute(_schema(config.EMBED_DIM))
        for b in BRANDS_SEED:
            db.x("""INSERT INTO brands (brand_id, display_name)
                    VALUES (%s,%s) ON CONFLICT (brand_id) DO NOTHING""",
                 (b, {"RON": "Republic Of Nature",
                      "ELEMENTS": "Elements Senior Living"}.get(b, b)))
        db.set_setting("kb_schema_ok", "true")
        db.set_setting("kb_schema_error", "")
        db.set_setting("kb_embed_dim", str(config.EMBED_DIM))
        db.set_setting("kb_embed_model", config.EMBED_MODEL)
        return {"ok": True, "dim": config.EMBED_DIM, "model": config.EMBED_MODEL}
    except Exception as e:
        # The commonest cause is a database user without rights to CREATE
        # EXTENSION. That is a Railway/vendor action, not a code fix, so say so
        # plainly rather than retrying.
        msg = str(e)[:500]
        try:
            db.set_setting("kb_schema_ok", "false")
            db.set_setting("kb_schema_error", msg)
        except Exception:
            pass
        return {"ok": False, "detail": msg}


def available():
    return db.get_setting("kb_schema_ok", "false") == "true"


def dim_matches():
    """False when the stored dimension differs from config.

    A dimension change is a re-index, not a migration: existing vectors are
    meaningless under a new model. Detecting it is the difference between "the
    ingest refuses" and "the bot answers confidently from noise".
    """
    stored = db.get_setting("kb_embed_dim")
    return stored is None or stored == str(config.EMBED_DIM)


# --- fenced retrieval --------------------------------------------------------

def search(brand_id, embedding, k=None, doc_type=None):
    """Nearest chunks for ONE brand. `brand_id` is required and first.

    There is deliberately no variant of this function without a brand. The fence is
    not a rule the caller is trusted to remember -- it is the shape of the only
    available door.

    Over-fetches (config.RETRIEVE_OVERFETCH) then trims: with an HNSW index plus a
    WHERE filter, Postgres can filter after the index scan and hand back fewer than
    k rows for the brand. Over-fetching costs nothing at this corpus size and the
    alternative is a silently short answer.
    """
    if not brand_id:
        raise ValueError("search() requires a brand_id -- the fence is not optional")
    if embedding is None:
        raise ValueError("search() requires an embedding")
    k = k or config.RETRIEVE_K
    sql = """SELECT c.id, c.brand_id, c.content, c.guardrail, c.ordinal,
                    d.title, d.doc_type, d.version,
                    c.embedding <=> %s::vector AS distance
             FROM kb_chunks c JOIN kb_documents d ON d.id = c.document_id
             WHERE c.brand_id = %s AND d.active"""
    params = [embedding, brand_id]
    if doc_type:
        sql += " AND d.doc_type = %s"
        params.append(doc_type)
    sql += " ORDER BY distance ASC LIMIT %s"
    params.append(config.RETRIEVE_OVERFETCH)
    rows = db.q(sql, tuple(params)) or []
    # Defence in depth: the WHERE already fences, and this asserts it held. If a
    # future refactor loses the clause, this raises instead of leaking one project's
    # answers into another project's conversation.
    for r in rows:
        if r["brand_id"] != brand_id:
            raise AssertionError(
                f"brand fence breach: asked {brand_id}, got {r['brand_id']}")
    return rows[:k]


def answer_context(brand_id, question, k=None, doc_type=None):
    """Embed a buyer's question and return the chunks that may answer it.

    The only entry point the qualifier agent should use. It exists so that the
    query-side `input_type` cannot be got wrong at the call site: `embed_query`
    produces a query vector, `search` compares it against document vectors, and a
    caller who reaches for `search` directly has to supply a vector themselves.
    """
    import embed
    return search(brand_id, embed.embed_query(question), k=k, doc_type=doc_type)


def stats():
    """Corpus size per brand, for the dashboard and for task 11's proof."""
    if not available():
        return {"available": False,
                "error": db.get_setting("kb_schema_error", "")}
    docs = db.q("""SELECT b.brand_id, b.display_name,
                          count(DISTINCT d.id) AS documents,
                          count(c.id) AS chunks,
                          count(c.embedding) AS embedded
                   FROM brands b
                   LEFT JOIN kb_documents d ON d.brand_id = b.brand_id AND d.active
                   LEFT JOIN kb_chunks c ON c.document_id = d.id
                   GROUP BY b.brand_id, b.display_name
                   ORDER BY b.brand_id""") or []
    return {"available": True,
            "embed_model": db.get_setting("kb_embed_model"),
            "embed_dim": db.get_setting("kb_embed_dim"),
            "dim_matches_config": dim_matches(),
            "brands": docs}
