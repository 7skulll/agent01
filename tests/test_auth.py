"""Shared-token gate: opt-in, API-only, with health and CORS preflight exemptions."""

from fastapi.testclient import TestClient
from support import settings

from app.main import create_app


def test_shared_token_protects_api_but_not_health_or_preflight():
    with TestClient(create_app(settings(api_token="s3cret"))) as client:
        unauthorized = client.get("/api/v1/conversations")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["detail"]["code"] == "unauthorized"
        assert unauthorized.json()["detail"]["request_id"]
        assert unauthorized.headers["www-authenticate"] == "Bearer"
        assert unauthorized.headers["x-request-id"] == unauthorized.json()["detail"]["request_id"]

        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/metrics").status_code == 401
        assert (
            client.get(
                "/api/v1/conversations", headers={"Authorization": "Bearer s3cret"}
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/api/v1/conversations", headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v1/conversations", headers={"Authorization": "s3cret"}
            ).status_code
            == 401
        )

        preflight = client.options(
            "/api/v1/conversations",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "*"


def test_empty_token_keeps_existing_behavior():
    with TestClient(create_app(settings())) as client:
        assert client.get("/api/v1/conversations").status_code == 200
