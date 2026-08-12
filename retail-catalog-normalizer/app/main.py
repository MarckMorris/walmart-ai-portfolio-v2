"""retail-catalog-normalizer.

Turns messy supplier product feeds into canonical records that downstream
matching, pricing and replenishment can actually join on.

The normalisation itself lives in app/normalizer.py and has no web dependency,
so it can be imported into a batch job or a notebook without starting a server.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

from app.normalizer import group_duplicates, normalise, normalise_batch

SERVICE = "retail-catalog-normalizer"
PORT = 8003
MAX_BATCH = 5_000

app = FastAPI(
    title=SERVICE,
    description=(
        "Canonicalises supplier product records: units converted to one base unit, "
        "pack counts extracted, brand spelling folded, GTINs validated."
    ),
    version="2.0.0",
)

# Wide open for local development. Narrow to your own origins before exposing
# this outside a trusted network.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

REQUEST_COUNT = Counter(
    "app_requests_total", "Total HTTP requests", ["method", "endpoint", "status"]
)
REQUEST_LATENCY = Histogram(
    "app_request_latency_seconds", "Request latency in seconds", ["endpoint"]
)
RECORDS_NORMALISED = Counter(
    "catalog_records_normalised_total", "Product records normalised"
)
RECORDS_WITH_WARNINGS = Counter(
    "catalog_records_with_warnings_total", "Records that produced at least one warning"
)


@app.middleware("http")
async def metrics_middleware(request, call_next):
    started = time.time()
    response = await call_next(request)
    REQUEST_COUNT.labels(
        method=request.method, endpoint=request.url.path, status=response.status_code
    ).inc()
    REQUEST_LATENCY.labels(endpoint=request.url.path).observe(time.time() - started)
    return response


class ProductRecord(BaseModel):
    """One raw supplier row. Extra keys are ignored, not rejected."""

    name: str | None = None
    title: str | None = None
    description: str | None = None
    brand: str | None = None
    gtin: str | None = None
    upc: str | None = None
    ean: str | None = None

    model_config = {"extra": "allow"}


class NormaliseRequest(BaseModel):
    records: list[ProductRecord] = Field(..., description="Raw supplier records")


class NormaliseResponse(BaseModel):
    count: int
    normalised: list[dict[str, Any]]
    duplicate_groups: dict[str, list[int]]
    records_with_warnings: int


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": SERVICE,
        "domain": "RETAIL",
        "status": "running",
        "version": "2.0.0",
        "port": PORT,
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "status": "healthy",
        "service": SERVICE,
        "domain": "RETAIL",
        "port": PORT,
    }


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/normalize", response_model=NormaliseResponse)
async def normalize(request: NormaliseRequest) -> NormaliseResponse:
    """Canonicalise a batch and report which records collapse to the same key."""
    if not request.records:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No records supplied"
        )
    if len(request.records) > MAX_BATCH:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Batch limit is {MAX_BATCH} records",
        )

    products = normalise_batch([r.model_dump(exclude_none=True) for r in request.records])

    RECORDS_NORMALISED.inc(len(products))
    with_warnings = sum(1 for p in products if p.warnings)
    RECORDS_WITH_WARNINGS.inc(with_warnings)

    return NormaliseResponse(
        count=len(products),
        normalised=[p.to_dict() for p in products],
        duplicate_groups=group_duplicates(products),
        records_with_warnings=with_warnings,
    )


@app.post("/normalize/one")
async def normalize_one(record: ProductRecord) -> dict[str, Any]:
    """Single record, for interactive use and debugging a supplier feed."""
    return normalise(record.model_dump(exclude_none=True)).to_dict()


@app.get("/info")
async def info() -> dict[str, Any]:
    return {
        "service": SERVICE,
        "domain": "RETAIL",
        "port": PORT,
        "endpoints": [
            {"path": "/", "method": "GET", "description": "Service identity"},
            {"path": "/health", "method": "GET", "description": "Health check"},
            {"path": "/metrics", "method": "GET", "description": "Prometheus metrics"},
            {"path": "/normalize", "method": "POST", "description": "Canonicalise a batch of records"},
            {"path": "/normalize/one", "method": "POST", "description": "Canonicalise a single record"},
            {"path": "/info", "method": "GET", "description": "Service information"},
        ],
        "description": (
            "Reduces supplier product records to a canonical form: units converted to a "
            "single base unit, pack counts extracted, brand spelling folded, GTINs "
            "validated against their check digit."
        ),
        "batch_limit": MAX_BATCH,
    }
