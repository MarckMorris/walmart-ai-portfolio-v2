"""HTTP surface of retail-catalog-normalizer."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import MAX_BATCH, SERVICE, app

client = TestClient(app)


class TestServiceEndpoints:
    def test_health(self):
        body = client.get("/health").json()
        assert body["ok"] is True
        assert body["service"] == SERVICE

    def test_root_identifies_the_service(self):
        body = client.get("/").json()
        assert body["status"] == "running"
        assert body["service"] == SERVICE

    def test_metrics_are_prometheus_formatted(self):
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]

    def test_info_lists_the_real_endpoints(self):
        body = client.get("/info").json()
        paths = {e["path"] for e in body["endpoints"]}
        assert "/normalize" in paths
        assert body["batch_limit"] == MAX_BATCH

    def test_openapi_documents_the_normalize_route(self):
        assert "/normalize" in client.get("/openapi.json").json()["paths"]


class TestNormalizeEndpoint:
    def test_a_batch_is_canonicalised(self):
        response = client.post(
            "/normalize",
            json={
                "records": [
                    {"name": "Coca-Cola 2 LTR", "brand": "Coca-Cola", "upc": "036000291452"},
                    {"name": "coca cola 2l", "brand": "coca cola"},
                ]
            },
        )
        body = response.json()
        assert response.status_code == 200
        assert body["count"] == 2
        assert len(body["duplicate_groups"]) == 1

    def test_the_response_reports_real_counts_not_placeholders(self):
        """Every service in this repository used to return a fixed value
        regardless of input. This is the test that would have caught it."""
        for size in (1, 4, 9):
            body = client.post(
                "/normalize", json={"records": [{"name": f"Item {i} 1l"} for i in range(size)]}
            ).json()
            assert body["count"] == size
            assert len(body["normalised"]) == size

    def test_warnings_are_counted(self):
        body = client.post(
            "/normalize", json={"records": [{"name": "Thing 1l", "upc": "00000"}]}
        ).json()
        assert body["records_with_warnings"] == 1

    def test_an_empty_batch_is_rejected(self):
        assert client.post("/normalize", json={"records": []}).status_code == 422

    def test_an_oversized_batch_is_rejected(self):
        payload = {"records": [{"name": "x 1l"}] * (MAX_BATCH + 1)}
        assert client.post("/normalize", json=payload).status_code == 413

    def test_extra_supplier_columns_do_not_break_validation(self):
        response = client.post(
            "/normalize", json={"records": [{"name": "Water 500ml", "vendor_sku": "AB-1"}]}
        )
        assert response.status_code == 200

    def test_a_malformed_body_is_rejected(self):
        assert client.post("/normalize", json={"nope": 1}).status_code == 422


class TestNormalizeOne:
    def test_a_single_record_is_canonicalised(self):
        body = client.post(
            "/normalize/one", json={"name": "Spring Water 6 x 500ml", "brand": "Acme"}
        ).json()
        assert body["pack_count"] == 6
        assert body["size_value"] == 500.0
        assert body["size_unit"] == "ml"

    def test_an_invalid_barcode_is_surfaced_not_swallowed(self):
        body = client.post(
            "/normalize/one", json={"name": "Thing 1l", "upc": "036000291453"}
        ).json()
        assert body["gtin"] is None
        assert any("check digit" in w for w in body["warnings"])
