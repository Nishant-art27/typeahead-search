"""FastAPI application: HTTP surface + startup ingestion + static UI.

Endpoints (see docs/API.md for full contracts):
    GET  /                      -> the typeahead web UI
    GET  /suggest?q=&recency=   -> up to 10 prefix suggestions (cache -> index)
    POST /search                -> dummy "Searched" response + records the query
    GET  /trending?k=           -> recency-ranked trending queries
    GET  /cache/debug?prefix=   -> which cache node owns a prefix + hit/miss
    GET  /stats                 -> latency p95, cache hit-rate, DB writes, etc.
    GET  /health                -> liveness probe

On startup the durable store is populated from the dataset CSV (generated on
first run if absent), the Trie index is built from the store, and the batch
writer thread is started.
"""

from __future__ import annotations

import importlib.util
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings, STATIC_DIR, BASE_DIR
from .service import TypeaheadService


def _ensure_dataset() -> None:
    """Generate the dataset CSV on first run if it does not already exist."""
    if os.path.exists(settings.dataset_path):
        return
    gen_path = os.path.join(BASE_DIR, "scripts", "generate_dataset.py")
    spec = importlib.util.spec_from_file_location("generate_dataset", gen_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    print(f"[startup] No dataset found — generating {settings.dataset_min_rows:,} queries...")
    n = module.generate(settings.dataset_min_rows, settings.dataset_path)
    print(f"[startup] Generated {n:,} queries at {settings.dataset_path}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    service = TypeaheadService(settings)
    if service.store.is_empty():
        _ensure_dataset()
        print("[startup] Loading dataset into primary store (SQLite)...")
        loaded = service.store.bulk_load_csv(settings.dataset_path)
        print(f"[startup] Loaded {loaded:,} rows into the store.")
    print("[startup] Building in-memory Trie index from the store...")
    service.build_index_from_store()
    print(f"[startup] Indexed {service.trie.size:,} queries.")
    service.start()
    app.state.service = service
    print(f"[startup] Ready. Cache nodes: {service.cache.ring.nodes}")
    try:
        yield
    finally:
        print("[shutdown] Flushing batch writer and closing store...")
        service.shutdown()


app = FastAPI(title="Search Typeahead", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def _svc(app: FastAPI) -> TypeaheadService:
    return app.state.service


# --- API ------------------------------------------------------------------
class SearchBody(BaseModel):
    query: str


@app.get("/suggest")
def suggest(
    q: str = Query("", description="prefix the user has typed"),
    recency: bool = Query(True, description="recency-aware ranking (trending)"),
):
    return _svc(app).suggest(q, recency)


@app.post("/search")
def search(body: SearchBody):
    # Dummy search API: returns "Searched" and records the query (batched).
    return _svc(app).search(body.query)


@app.get("/trending")
def trending(k: int = Query(None, description="how many trending queries")):
    return {"trending": _svc(app).trending(k)}


@app.get("/cache/debug")
def cache_debug(
    prefix: str = Query("", description="prefix key to inspect"),
    recency: bool = Query(False, description="inspect the recency-mode key"),
):
    return _svc(app).cache_debug(prefix, recency)


@app.get("/stats")
def stats():
    return _svc(app).stats()


@app.get("/health")
def health():
    return {"status": "ok"}


# --- UI / static ----------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/favicon.ico")
def favicon():
    return JSONResponse(status_code=204, content=None)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
