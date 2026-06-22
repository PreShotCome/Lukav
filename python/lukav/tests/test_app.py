"""Phase 0 smoke tests — app builds, /healthz responds, / renders."""
from __future__ import annotations

from fastapi.testclient import TestClient

from lukav.web.app import create_app


def test_app_constructs():
    app = create_app()
    assert app.title == "Lukav"
    paths = {route.path for route in app.routes}
    assert "/healthz" in paths
    assert "/" in paths


def test_healthz_returns_ok():
    client = TestClient(create_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "lukav"


def test_index_renders_html():
    client = TestClient(create_app())
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Lukav" in resp.text


def test_tool_registry_is_built():
    from lukav.tools._all import build_full_registry
    registry = build_full_registry()
    assert len(registry.names()) > 0
