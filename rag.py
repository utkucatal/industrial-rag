"""RAG core: build the index, do hybrid retrieval, generate grounded answers."""
import os
import json
from pathlib import Path

import psycopg2
from pgvector.psycopg2 import register_vector
from sentence_transformers import SentenceTransformer
import anthropic
from dotenv import load_dotenv

load_dotenv(override=True) # let .env win even if the shell already exported these vars

SRC = Path(__file__).parent
DSN = os.environ["DATABASE_URL"]
EMBED_MODEL = "BAAI/bge-m3"
DIM = 1024
GEN_MODEL = "claude-sonnet-4-6"
TOP_K = 5
MIN_SIM = 0.45 # cosine similarity floor (0-1); below this we treat it as "not in catalog"

_embedder = None


def embedder():
    # load the model once and reuse it; it's a ~2.3GB download on first run
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(EMBED_MODEL)
    return _embedder


def embed(texts):
    # normalize so cosine distance behaves
    return embedder().encode(texts, normalize_embeddings=True)


def product_text(p):
    # one searchable blob per product
    parts = [p.get("title"), p.get("manufacturer"), p.get("oem_pn"),
             p.get("category"), p.get("condition"), p.get("description")]
    return " | ".join(x for x in parts if x)


def connect():
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")   # so register_vector works pre-index
    conn.commit()
    register_vector(conn)
    return conn


def index_ready(conn):
    """True once the products table exists and has rows."""
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('products')")
    if cur.fetchone()[0] is None:
        return False
    cur.execute("SELECT count(*) FROM products")
    return cur.fetchone()[0] > 0


def build_index():
    """Embed catalog.json into pgvector. Run once, or again whenever the catalog changes."""
    products = json.loads((SRC / "catalog.json").read_text(encoding="utf-8"))

    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.commit()
    register_vector(conn)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS products (
            url           text PRIMARY KEY,
            id            text,
            title         text,
            manufacturer  text,
            oem_pn        text,
            condition     text,
            price_eur     double precision,
            weight_kg     double precision,
            category      text,
            description   text,
            embedding     vector({DIM})
        )
    """)
    conn.commit()

    print(f"Embedding {len(products)} products with bge-m3...")
    vecs = embed([product_text(p) for p in products])

    for p, v in zip(products, vecs):
        cur.execute("""
            INSERT INTO products
                (url,id,title,manufacturer,oem_pn,condition,price_eur,weight_kg,category,description,embedding)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (url) DO UPDATE SET
                id=EXCLUDED.id, title=EXCLUDED.title, manufacturer=EXCLUDED.manufacturer,
                oem_pn=EXCLUDED.oem_pn, condition=EXCLUDED.condition, price_eur=EXCLUDED.price_eur,
                weight_kg=EXCLUDED.weight_kg, category=EXCLUDED.category,
                description=EXCLUDED.description, embedding=EXCLUDED.embedding
        """, (p["url"], p.get("id"), p.get("title"), p.get("manufacturer"), p.get("oem_pn"),
              p.get("condition"), p.get("price_eur"), p.get("weight_kg"),
              p.get("category"), p.get("description"), v))
    conn.commit()

    cur.execute("""
        CREATE INDEX IF NOT EXISTS products_emb_idx
        ON products USING hnsw (embedding vector_cosine_ops)
    """)
    conn.commit()
    conn.close()
    print("Index ready.")


# Ask Sonnet for plain JSON instead of the newer structured-output API,
# so this works on any anthropic SDK version.
FILTER_SYSTEM = (
    "Extract structured search filters from the user's product query and return ONLY this JSON "
    "(no markdown, no prose):\n"
    '{"semantic_query": str, "manufacturer": str|null, "category": str|null, '
    '"max_price": number|null, "min_weight": number|null}\n'
    "manufacturer: brand name (Becker, Siemens, Rietschle...) or null. "
    "category: product-type keyword (motor, pump, sensor, gear...) or null. "
    "max_price: price ceiling in euros or null. min_weight: weight floor in kg or null. "
    "semantic_query: what's left of the query for semantic search once filters are pulled out."
)

EMPTY_FILTERS = {"semantic_query": None, "manufacturer": None,
                 "category": None, "max_price": None, "min_weight": None}


def extract_filters(client, query):
    try:
        resp = client.messages.create(
            model=GEN_MODEL,
            max_tokens=300,
            system=FILTER_SYSTEM,
            messages=[{"role": "user", "content": query}],
        )
        text = next(b.text for b in resp.content if b.type == "text").strip()
        if text.startswith("```"): # strip a ```json ... ``` fence if the model adds one
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return {**EMPTY_FILTERS, **json.loads(text.strip())}
    except Exception:
        return {**EMPTY_FILTERS, "semantic_query": query} # fall back to pure semantic search


def retrieve(conn, client, query):
    f = extract_filters(client, query)
    qvec = embed([f.get("semantic_query") or query])[0]

    # structured half of the hybrid search: turn the filters into SQL predicates
    where, params = [], []
    if f.get("manufacturer"):
        where.append("manufacturer ILIKE %s"); params.append(f"%{f['manufacturer']}%")
    if f.get("category"):
        where.append("(category ILIKE %s OR title ILIKE %s)")
        params += [f"%{f['category']}%", f"%{f['category']}%"]
    if f.get("max_price") is not None:
        where.append("price_eur <= %s"); params.append(f["max_price"])
    if f.get("min_weight") is not None:
        where.append("weight_kg >= %s"); params.append(f["min_weight"])
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    # semantic half: order the filtered rows by cosine distance to the query vector
    sql = f"""
        SELECT id, title, manufacturer, oem_pn, condition, price_eur, weight_kg,
               category, url, description,
               1 - (embedding <=> %s) AS sim
        FROM products
        {clause}
        ORDER BY embedding <=> %s
        LIMIT %s
    """
    cur = conn.cursor()
    cur.execute(sql, [qvec, *params, qvec, TOP_K])
    cols = [d[0] for d in cur.description]
    results = [dict(zip(cols, row)) for row in cur.fetchall()]
    results = [r for r in results if r["sim"] >= MIN_SIM]   # drop weak matches
    return f, results


def build_prompt(results):
    blocks = []
    for i, r in enumerate(results, 1):
        desc = (r.get("description") or "")[:800]
        blocks.append(
            f"[{i}] {r['title']}\n"
            f"  id: {r['id']} | manufacturer: {r['manufacturer']} | OEM: {r['oem_pn']} | condition: {r['condition']}\n"
            f"  price: €{r['price_eur']} | weight: {r['weight_kg']} kg | category: {r['category']}\n"
            f"  url: {r['url']}\n"
            f"  description: {desc}"
        )
    return "\n\n".join(blocks)


GEN_SYSTEM = (
    "You are an industrial product catalog assistant. Answer ONLY from the PRODUCTS provided in the "
    "message. If none of them are relevant, say \"I couldn't find a relevant product in the catalog.\" "
    "Never invent products or specs, and don't answer from general knowledge. Politely refuse general "
    "chat or anything unrelated to the catalog: \"I can only answer questions about products in this "
    "catalog.\" Always show the id and url of every product you use in your answer."
)


def answer(client, query, results):
    # nothing cleared the threshold -> don't even call the model
    if not results:
        return "I couldn't find a relevant product in the catalog."
    prompt = f"PRODUCTS:\n{build_prompt(results)}\n\nQUESTION: {query}"
    resp = client.messages.create(
        model=GEN_MODEL,
        max_tokens=1024,
        system=GEN_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return next(b.text for b in resp.content if b.type == "text")


if __name__ == "__main__":
    build_index()