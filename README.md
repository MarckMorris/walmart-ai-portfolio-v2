# Retail AI Services

**Twenty independent FastAPI microservices for retail and merchandising operations, behind one docker compose stack, with Prometheus metrics and a React dashboard.**

[![CI](https://github.com/MarckMorris/AI-portfolio-v2/actions/workflows/ci.yml/badge.svg)](https://github.com/MarckMorris/AI-portfolio-v2/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## Read this first

**One service is implemented. Nineteen are scaffolds.** They share a service
template — health check, Prometheus metrics, OpenAPI schema, containerisation,
compose wiring — and their `/predict` endpoint returns a placeholder that says so
in the response body.

That is stated here rather than buried, because the alternative is nineteen
endpoints that look finished and are not. What this repository actually
demonstrates is the platform layer: a consistent service contract, per-service
dependency isolation, observability wired in from the start, and CI that tests
all twenty independently.

| Service | Status | What it does |
| --- | --- | --- |
| `retail-catalog-normalizer` | **Implemented** | Canonicalises messy supplier product feeds |
| the other 19 | Scaffold | Service template with health, metrics and OpenAPI |

## The implemented one: retail-catalog-normalizer

Supplier feeds describe the same product five different ways:

```
"Coca-Cola 2 LTR"     brand: Coca-Cola     upc: 036000291452
"coca cola 2l"        brand: coca cola
"COCA COLA 2000ML"    brand: Coca Cola
```

Those are one product. Every downstream join — pricing, replenishment,
assortment — breaks on them. This service reduces each record to a canonical
form and reports which records collapse together:

```bash
curl -X POST http://localhost:8003/normalize \
  -H "Content-Type: application/json" \
  -d '{"records": [
        {"name": "Coca-Cola 2 LTR", "brand": "Coca-Cola", "upc": "036000291452"},
        {"name": "coca cola 2l", "brand": "coca cola"},
        {"name": "COCA COLA 2000ML", "brand": "Coca Cola"}
      ]}'
```

```json
{
  "count": 3,
  "duplicate_groups": {"coca cola|2000ml": [0, 1, 2]},
  "records_with_warnings": 0
}
```

**What it handles.** Units converted to one base unit per dimension — millilitres
for volume, grams for weight — so `2 LTR`, `2l` and `2000ML` all become 2000 ml, and
`12 oz` becomes 340.194 g. Multipacks extracted from `6 x 330ml`, `pack of 12`, `24ct`.
Accents folded, hyphens and case normalised, stop words dropped. Barcodes
validated against their GS1 check digit and padded to GTIN-14.

**Three decisions worth naming:**

- **`fl oz` is matched before `oz`.** Otherwise every drink in the catalogue silently becomes a weight.
- **A barcode that fails its own checksum is rejected, not stored.** A bad check digit is a transcription error, and accepting it is how two different products get merged into one.
- **Nothing is guessed silently.** Every record carries a `warnings` list, and the batch response counts how many records produced one. A pipeline that cannot explain why it changed a value is one nobody will run in production.

No machine learning. Deterministic rules, 67 tests.

The normalisation logic lives in `retail-catalog-normalizer/app/normalizer.py` and
imports nothing web-related, so it can be dropped into a batch job or a notebook
without starting a server.

## The service template

Every service exposes the same contract, which is what makes twenty of them
manageable:

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness, used by compose and the verification script |
| `GET /` | Service identity and version |
| `GET /info` | Its own endpoint list, so the dashboard can discover it |
| `GET /metrics` | Prometheus: request count and latency histogram per endpoint |
| `GET /docs` | OpenAPI schema |

Each service owns its `requirements.txt` and its `Dockerfile`, so one service
adding a heavy dependency does not slow the other nineteen.

## Running it

```bash
git clone https://github.com/MarckMorris/AI-portfolio-v2.git
cd AI-portfolio-v2
docker compose up
```

| | |
| --- | --- |
| Dashboard | http://localhost:3000 |
| Catalog normalizer | http://localhost:8003/docs |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3001 |

One service on its own:

```bash
cd retail-catalog-normalizer
pip install -r requirements.txt
python -m pytest -v          # 67 tests
uvicorn app.main:app --reload --port 8003
```

## CI

Each of the twenty services is installed and tested in isolation, in its own
matrix job. The React dashboard is built, and the compose file is validated —
which catches the failure that actually happens in a repository this shape: a
service renamed in one place and not the other.

Building twenty container images on every push was the previous workflow. It
cost more than it caught and has been removed.

## Known limitations

- Nineteen services return a placeholder from `/predict`. The response body says so.
- No persistence layer. Nothing is stored between requests.
- No authentication. Every service is open, which is fine on a compose network and not fine anywhere else.
- CORS is wide open for local development.
- Grafana ships with the default credentials.

## License

MIT — see [LICENSE](LICENSE).

## Author

**Marcos Morris** — Cloud Infrastructure Engineer, Bentonville, AR

[LinkedIn](https://www.linkedin.com/in/marck-morris/) · [Portfolio](https://marckmorris.github.io/) · marck.morris.pro@gmail.com
