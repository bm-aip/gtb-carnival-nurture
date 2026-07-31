"""Embedding client (Voyage).

One job: turn text into vectors, in batches, with the right input_type.

THE INPUT_TYPE TRAP
-------------------
Voyage produces DIFFERENT vectors for the same string depending on `input_type`:

    document -> for text being stored in the corpus
    query    -> for a buyer's question being matched against it

That is deliberate on the provider's side and it materially improves retrieval. Get
it backwards, or omit it, and the system still works -- it just answers worse, with
no error anywhere to explain why. So the two are separate functions here rather than
a flag with a default, because a defaulted flag is a thing people forget.

DIMENSION IS VERIFIED, NOT TRUSTED
----------------------------------
`EMBED_DIM` is baked into the `kb_chunks` column type, so a model whose native
dimension differs would fail at insert -- or worse, succeed against a stale column
and leave a corpus of vectors that cannot be compared. Every response is checked
against config before it is returned.
"""
import time

import requests

import config


class EmbedError(RuntimeError):
    pass


def configured():
    return bool(config.VOYAGE_API_KEY)


def _post(texts, input_type, attempt=1):
    if not configured():
        raise EmbedError(
            "VOYAGE_API_KEY is not set. Set it in the environment -- never paste a "
            "key into a file or a chat transcript.")
    try:
        r = requests.post(
            config.VOYAGE_URL,
            headers={"Authorization": f"Bearer {config.VOYAGE_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": config.EMBED_MODEL,
                  "input": texts,
                  "input_type": input_type,
                  "output_dimension": config.EMBED_DIM},
            timeout=60)
    except Exception as e:
        if attempt < 3:
            time.sleep(2 ** attempt)
            return _post(texts, input_type, attempt + 1)
        raise EmbedError(f"embedding request failed: {e}")

    # 429 and 5xx are worth retrying; a 400 means our request is wrong and retrying
    # it just burns quota to get the same answer.
    if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
        time.sleep(2 ** attempt)
        return _post(texts, input_type, attempt + 1)
    if r.status_code != 200:
        raise EmbedError(f"embedding HTTP {r.status_code}: {r.text[:300]}")

    try:
        data = r.json()["data"]
    except Exception:
        raise EmbedError(f"unexpected embedding response: {r.text[:300]}")

    # Provider returns an `index` per item. Sort by it rather than assuming order --
    # a silently reordered batch would attach every vector to the wrong chunk, and
    # nothing downstream could detect it.
    data.sort(key=lambda d: d.get("index", 0))
    vectors = [d["embedding"] for d in data]

    if len(vectors) != len(texts):
        raise EmbedError(
            f"asked for {len(texts)} embeddings, got {len(vectors)}")
    for v in vectors:
        if len(v) != config.EMBED_DIM:
            raise EmbedError(
                f"model {config.EMBED_MODEL} returned {len(v)} dims but EMBED_DIM "
                f"is {config.EMBED_DIM}. Fix the config and RE-INDEX -- existing "
                f"vectors are not comparable across dimensions.")
    return vectors


def _batched(texts, input_type):
    out = []
    for i in range(0, len(texts), config.EMBED_BATCH):
        out.extend(_post(texts[i:i + config.EMBED_BATCH], input_type))
    return out


def embed_documents(texts):
    """Vectors for corpus text being stored."""
    if not texts:
        return []
    return _batched(list(texts), "document")


def embed_query(text):
    """Vector for one buyer question being matched against the corpus."""
    if not text or not text.strip():
        raise EmbedError("embed_query called with empty text")
    return _post([text], "query")[0]


def probe():
    """Cheap liveness + dimension check. Used by /admin/embed-check.

    Confirms the key works, the model name is real and its dimension matches config
    -- without touching the corpus. A wrong model name is otherwise discovered
    halfway through an ingest.
    """
    if not configured():
        return {"ok": False, "detail": "VOYAGE_API_KEY not set"}
    try:
        v = embed_query("test")
        return {"ok": True, "model": config.EMBED_MODEL, "dim": len(v)}
    except EmbedError as e:
        return {"ok": False, "model": config.EMBED_MODEL, "detail": str(e)}
