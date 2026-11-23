"""Admin config/account APIs, secret masking, and WebUI route."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from codearts2api.app import create_app
from codearts2api.config import Settings
from codearts2api.oauth.session import OAuthSession, SessionStore


AUTH = {"Authorization": "Bearer test-local-key"}


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def test_webui_routes_return_html(client: TestClient) -> None:
    for path in ("/", "/ui"):
        response = client.get(path)
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        body = response.text
        assert "账号池" in body
        assert "CA2A_API_KEY" in body
        assert "sessionStorage" in body or "sessionStorage" in client.get("/static/app.js").text



@pytest.mark.parametrize("path", ["/ui", "/ui/"])
def test_legacy_webui_redirects_to_root(client: TestClient, path: str) -> None:
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/"
    assert client.get(path).url.path == "/"


def test_root_assets_and_api_routes_are_not_shadowed(client: TestClient) -> None:
    response = client.get("/")
    assert not response.history
    assets = re.findall(r'(?:src|href)="(/static/[^"?]+)"', response.text)
    assert set(assets) == {"/static/styles.css", "/static/app.js"}
    for asset in assets:
        loaded = client.get(asset)
        assert loaded.status_code == 200
        assert "text/html" not in loaded.headers["content-type"]
    assert 'api("/admin/config"' in client.get("/static/app.js").text
    for path in ("/healthz", "/readyz", "/openapi.json"):
        assert client.get(path).headers["content-type"].startswith("application/json")
    assert client.get("/docs").status_code == 200
    for path in ("/v1/models", "/admin/config", "/admin/accounts"):
        assert client.get(path).status_code == 401
    assert client.get("/not-a-real-route").status_code == 404
    assert client.get("/static/missing.js").status_code == 404


def test_missing_webui_bundle_is_reported(client: TestClient, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("codearts2api.app.STATIC_DIR", tmp_path)
    response = client.get("/")
    assert response.status_code == 404
    assert response.json()["detail"] == "WebUI is not packaged"

def test_admin_requires_api_key(client: TestClient) -> None:
    assert client.get("/admin/config").status_code == 401
    assert client.get("/admin/accounts").status_code == 401
    assert client.post("/admin/accounts", json={"name": "x", "kind": "aksk", "ak": "a", "sk": "b"}).status_code == 401
    assert client.get("/admin/status").status_code == 401
    # Public pages remain open.
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 200


def test_account_crud_masks_secrets(client: TestClient) -> None:
    created = client.post(
        "/admin/accounts",
        headers=AUTH,
        json={
            "name": "work",
            "kind": "aksk",
            "ak": "AKIDEXAMPLEKEY",
            "sk": "raw-secret-key-value",
            "security_token": "raw-security-token",
            "note": "desk",
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["name"] == "work"
    assert body["ak_masked"] == "AKID****"
    assert body["has_sk"] is True
    dumped = created.text
    assert "raw-secret-key-value" not in dumped
    assert "raw-security-token" not in dumped
    assert "AKIDEXAMPLEKEY" not in dumped

    listing = client.get("/admin/accounts", headers=AUTH)
    assert listing.status_code == 200
    accounts = listing.json()["accounts"]
    work = next(item for item in accounts if item["name"] == "work")
    account_id = work["id"]
    assert "raw-secret-key-value" not in listing.text

    patched = client.patch(
        f"/admin/accounts/{account_id}",
        headers=AUTH,
        json={"name": "work-2", "sk": ""},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "work-2"
    assert patched.json()["has_sk"] is True
    assert "raw-secret-key-value" not in patched.text

    deleted = client.delete(f"/admin/accounts/{account_id}", headers=AUTH)
    assert deleted.status_code == 200
    remaining = client.get("/admin/accounts", headers=AUTH).json()["accounts"]
    assert all(item["id"] != account_id for item in remaining)


def test_admin_config_get_and_put(client: TestClient) -> None:
    current = client.get("/admin/config", headers=AUTH)
    assert current.status_code == 200
    payload = current.json()
    assert payload["host"] == "127.0.0.1"
    assert payload["pool_strategy"] == "round_robin" or payload["pool_strategy"]
    assert "api_key" not in payload
    assert payload["api_key_configured"] is True

    updated = client.put(
        "/admin/config",
        headers=AUTH,
        json={
            "timezone": "Asia/Shanghai",
            "checkin_cron": "15 9 * * *",
            "pool_strategy": "random",
            "cooldown_seconds": 30,
            "model_cache_ttl": 12,
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["checkin_cron"] == "15 9 * * *"
    assert body["pool_strategy"] == "random"
    assert body["cooldown_seconds"] == 30
    assert body["model_cache_ttl"] == 12

    rejected = client.put("/admin/config", headers=AUTH, json={"host": "0.0.0.0"})
    assert rejected.status_code == 400

    bad = client.put("/admin/config", headers=AUTH, json={"checkin_cron": "not-a-cron"})
    assert bad.status_code == 400


def test_status_includes_pool(client: TestClient) -> None:
    response = client.get("/admin/status", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["last_checkin"] is None
    assert "pool" in body
    assert body["pool"]["count"] >= 1
    assert "credentials" in body
    assert "raw-secret" not in response.text


def test_settings_update_reschedules_cron(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(
        codearts_ak="test-ak",
        codearts_sk="test-sk",
        api_key="test-local-key",
        scheduler_enabled=True,
        checkin_run_on_startup=False,
        checkin_cron="0 8 * * *",
        timezone="Asia/Shanghai",
        data_dir=str(tmp_path / "data"),
        auth_dir=str(tmp_path / "auths"),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        before = client.get("/admin/status", headers=AUTH).json()["scheduler"]
        assert before["enabled"] is True
        assert before["cron"] == "0 8 * * *"
        previous_next = before["next_run_at"]
        updated = client.put(
            "/admin/config",
            headers=AUTH,
            json={"checkin_cron": "30 1 * * *", "timezone": "Asia/Shanghai"},
        )
        assert updated.status_code == 200, updated.text
        after = client.get("/admin/status", headers=AUTH).json()["scheduler"]
        assert after["enabled"] is True
        assert after["cron"] == "30 1 * * *"
        assert after["next_run_at"] is not None
        assert after["next_run_at"] != previous_next


def test_probe_account_is_mockable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    listing = client.get("/admin/accounts", headers=AUTH).json()["accounts"]
    account_id = listing[0]["id"]

    async def fake_probe(snap, cred):  # type: ignore[no-untyped-def]
        return {
            "ok": True,
            "http_status": 200,
            "account_type": "credit",
            "is_credit_package": True,
            "message": "Probe succeeded",
        }

    monkeypatch.setattr("codearts2api.app.probe_account", fake_probe)
    response = client.post(f"/admin/accounts/{account_id}/test", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["account_type"] == "credit"


def test_persisted_checkin_visible_after_reload(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(
        codearts_ak="test-ak",
        codearts_sk="test-sk",
        api_key="test-local-key",
        scheduler_enabled=False,
        checkin_run_on_startup=False,
        data_dir=str(tmp_path / "data"),
        auth_dir=str(tmp_path / "auths"),
    )
    first = create_app(settings)
    with TestClient(first) as client:
        accounts = client.get("/admin/accounts", headers=AUTH).json()["accounts"]
        account_id = accounts[0]["id"]
    # Write a last_checkin through the pool file and reload the app.
    from codearts2api.pool import AccountPool

    pool = AccountPool(settings)
    pool.load_or_migrate()
    pool.record_checkin(
        account_id,
        {"status": "already_claimed", "message": "Already claimed today", "actions": []},
        "2026-11-21T08:00:00+08:00",
    )
    reloaded = create_app(
        Settings(
            codearts_ak="test-ak",
            codearts_sk="test-sk",
            api_key="test-local-key",
            scheduler_enabled=False,
            checkin_run_on_startup=False,
            data_dir=str(tmp_path / "data"),
            auth_dir=str(tmp_path / "auths"),
        )
    )
    with TestClient(reloaded) as client:
        accounts = client.get("/admin/accounts", headers=AUTH).json()["accounts"]
        match = next(item for item in accounts if item["id"] == account_id)
        assert match["last_checkin"]["status"] == "already_claimed"
        assert match["last_checkin_at"] == "2026-11-21T08:00:00+08:00"


def test_oauth_account_create_does_not_leak_session_secrets(tmp_path, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    SessionStore(settings.auth_dir).save(
        OAuthSession(
            access_key_id="oauth-ak-secret",
            secret_access_key="oauth-sk-secret",
            refresh_token="refresh-token-secret",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )
    app = create_app(settings)
    with TestClient(app) as client:
        body = client.get("/admin/accounts", headers=AUTH)
        assert body.status_code == 200
        assert "oauth-sk-secret" not in body.text
        assert "refresh-token-secret" not in body.text
        assert "oauth-ak-secret" not in body.text
        oauth = next(item for item in body.json()["accounts"] if item["kind"] == "oauth")
        assert oauth["oauth"]["has_refresh_token"] is True
