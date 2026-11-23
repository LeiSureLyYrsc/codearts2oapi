"""Web authorization with a real admin API and loopback listener, stubbed upstream."""

from __future__ import annotations

import json
import socket
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from codearts2api import app as app_module, pool
from codearts2api.app import create_app
from codearts2api.client import SnapAccessClient
from codearts2api.oauth import flow
from codearts2api.oauth.pkce import sha256_b64url
from codearts2api.oauth.session import OAuthSession

AUTH = {"Authorization": "Bearer test-local-key"}
START = "/admin/login/start"
CALLBACK = "/admin/login/callback"
STATUS = "/admin/login/status"


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def _start(client, name="工作账号", **fields):
    response = client.post(START, headers=AUTH, json={"name": name, **fields})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["account_id"] is None
    return payload


def _submit(client, login, url):
    return client.post(CALLBACK, headers=AUTH, json={"login_id": login["login_id"], "callback_url": url})


def _settled(client, login):
    end = time.monotonic() + 4
    while True:
        response = client.get(STATUS, headers=AUTH, params={"login_id": login["login_id"]})
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] != "pending":
            return payload
        assert time.monotonic() < end, "OAuth did not settle"
        time.sleep(0.02)


def _accounts(client):
    return client.get("/admin/accounts", headers=AUTH).json()["accounts"]


def test_manual_code_adds_account_only_after_exchange(client, tmp_path, monkeypatch):
    before = _accounts(client)
    accounts_dir = tmp_path / "data" / "accounts"
    before_files = set(accounts_dir.glob("*.json"))
    calls = []

    def exchange(endpoints, **kwargs):
        calls.append(kwargs)
        return OAuthSession("new-ak", "new-sk", refresh_token="new-refresh")

    monkeypatch.setattr(flow, "exchange_authorization_code", exchange)

    old_create = client.post("/admin/accounts", headers=AUTH, json={"name": "empty", "kind": "oauth"})
    assert old_create.status_code == 400
    with_static_keys = client.post(
        "/admin/accounts", headers=AUTH, json={"name": "empty", "kind": "oauth", "ak": "ak", "sk": "sk"},
    )
    assert with_static_keys.status_code == 400
    login = _start(client, enabled=False, note="测试备注")
    assert _accounts(client) == before
    assert set(accounts_dir.glob("*.json")) == before_files

    query = urlencode({"code": "temporary+code", "redirect": "https://example.invalid/not-requested", "state": "fixture"})
    url = login["callback_url"].replace("127.0.0.1", "localhost") + "?" + query
    submitted = _submit(client, login, url)
    assert submitted.status_code == 200, submitted.text
    assert url not in submitted.text
    assert "temporary" not in submitted.text
    result = _settled(client, login)
    assert result["status"] == "logged_in", result
    assert len(calls) == 1
    assert calls[0]["code"] == "temporary+code"
    assert calls[0]["redirect_uri"] == login["callback_url"]
    challenge = parse_qs(urlsplit(login["authorize_url"]).query)["code_challenge"][0]
    assert calls[0]["dpop"] is not None
    assert sha256_b64url(calls[0]["code_verifier"]) == challenge

    new_id = result["account_id"]
    oauth = next(account for account in _accounts(client) if account["id"] == new_id)
    assert (oauth["name"], oauth["note"], oauth["enabled"]) == ("工作账号", "测试备注", False)
    assert oauth["oauth"]["has_refresh_token"] is True
    [new_file] = set(accounts_dir.glob("*.json")) - before_files
    saved = json.loads(new_file.read_text(encoding="utf-8"))
    assert saved["account"]["id"] == new_id
    assert saved["oauth_session"]["refresh_token"] == "new-refresh"
    assert _settled(client, login)["account_id"] == new_id
    duplicate = _submit(client, login, url)
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["account_id"] == new_id
    assert len(calls) == 1
    assert len(_accounts(client)) == len(before) + 1

    with TestClient(create_app(client.app.state.settings)) as restarted:
        restored = next(account for account in _accounts(restarted) if account["id"] == new_id)
        assert restored["oauth"]["has_refresh_token"] is True
        assert restored["enabled"] is False


def test_new_oauth_account_can_read_models_and_credit_quota(client, monkeypatch):
    runtime = client.app.state.runtime
    runtime.model_cache.set([])
    for account in _accounts(client):
        disabled = client.patch(f"/admin/accounts/{account['id']}", headers=AUTH, json={"enabled": False})
        assert disabled.status_code == 200

    monkeypatch.setattr(
        flow,
        "exchange_authorization_code",
        lambda _endpoints, **_: OAuthSession("oauth-test-ak", "oauth-test-sk", security_token="test-sts-token"),
    )
    login = _start(client)
    submitted = _submit(client, login, f"{login['callback_url']}?code=mock-code")
    assert submitted.status_code == 200
    settled = _settled(client, login)
    assert settled["status"] == "logged_in", settled

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.headers["X-Security-Token"] == "test-sts-token"
        assert "Access=oauth-test-ak," in request.headers["Authorization"]
        assert request.headers["Content-Type"] == "application/json"
        if request.url.path == "/v1/model/builtin":
            assert request.headers["Agent-Type"] == "PromptCenter"
            assert request.headers["X-Language"] == "zh-cn"
            return httpx.Response(200, json={
                "builtinModels": [{"model_id": "test-model", "enable": True}], "count": 1,
            })
        assert request.url.path == "/snap-manager/v1/statistics/plugin"
        return httpx.Response(200, json={
            "package": {"is_credit_package": True},
            "metrics": [{
                "name": "usageTotalPackageCredit", "package_credit_amount": 10, "package_credit_remain": 0,
            }],
        })

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    monkeypatch.setattr(runtime, "client", SnapAccessClient(runtime.settings, upstream_client, runtime.resolver))
    try:
        models = client.get("/v1/models", headers=AUTH)
        assert models.status_code == 200, models.text
        assert [model["id"] for model in models.json()["data"]] == ["test-model"]

        quota = client.post(f"/admin/accounts/{settled['account_id']}/test", headers=AUTH)
        assert quota.status_code == 200, quota.text
        assert quota.json()["ok"] is True
        assert quota.json()["quota"] == {
            "unit": "credit", "total": 10, "used": None, "remaining": 0, "expiring": None,
        }
    finally:
        client.portal.call(upstream_client.aclose)


@pytest.mark.parametrize("redirect", [None, "https://example.invalid/not-requested"])
def test_manual_callback_validation_preserves_attempt_and_hides_input(client, monkeypatch, redirect, capsys):
    calls = []

    def poll(endpoints, **kwargs):
        calls.append(kwargs)
        return OAuthSession("ticket-ak", "ticket-sk", source="oauth-ticket")

    monkeypatch.setattr(flow, "poll_login_ticket", poll)
    login = _start(client)
    callback = login["callback_url"]
    parsed = urlsplit(callback)
    other_port = 1 if parsed.port != 1 else 2
    bad_urls = [
        f"http://example.invalid:{parsed.port}/oauth/callback?code=private-input",
        callback.replace("http:", "https:") + "?code=private-input",
        urlunsplit(("http", f"127.0.0.1:{other_port}", parsed.path, "code=private-input", "")),
        callback.replace("/oauth/callback", "/login") + "?code=private-input",
        callback.replace("127.0.0.1", "person@127.0.0.1") + "?code=private-input",
        callback + "?code=private-input#fragment",
        callback + "?code=private-input%0A",
        callback + "?code=private-input&code=other",
        callback + "?code=private-input&secret=other",
        callback + "?unused=private-input",
        callback + "?code=private-input" + "x" * 20000,
    ]
    for url in bad_urls:
        result = _submit(client, login, url)
        expected = {400, 413} if len(url) > 16384 else {400}
        assert result.status_code in expected, result.text[:240]
        assert "private-input" not in result.text
    malformed = client.post(
        CALLBACK,
        headers={**AUTH, "Content-Type": "application/json"},
        content=b'{"callback_url":"private-input',
    )
    assert malformed.status_code == 400
    assert "private-input" not in malformed.text
    assert not calls
    assert client.get(STATUS, headers=AUTH, params={"login_id": login["login_id"]}).json()["status"] == "pending"

    params = {"secret": "ticket-secret"}
    if redirect:
        params["redirect"] = redirect
    query = urlencode(params)
    submitted = _submit(client, login, f"{callback}?{query}")
    assert submitted.status_code == 200, submitted.text
    assert _settled(client, login)["status"] == "logged_in"
    assert len(calls) == 1
    assert calls[0]["secret"] == "ticket-secret"
    assert calls[0]["ticket_id"] == parse_qs(urlsplit(login["authorize_url"]).query)["ticket_id"][0]
    assert "private-input" not in capsys.readouterr().err


def test_cancelled_attempt_does_not_create_account_or_replace_new_attempt(client, monkeypatch):
    calls = []

    def reject_exchange(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("A stale callback must not reach the exchanger")

    monkeypatch.setattr(flow, "exchange_authorization_code", reject_exchange)
    before = _accounts(client)
    first = _start(client, "已取消")
    conflict = client.post(START, headers=AUTH, json={"name": "另一个授权"})
    assert conflict.status_code == 409
    assert first["authorize_url"] not in conflict.text
    unscoped_status = client.get(STATUS, headers=AUTH).json()
    assert "authorize_url" not in unscoped_status
    assert "login_id" not in unscoped_status
    deleted = client.delete("/admin/login", headers=AUTH, params={"login_id": first["login_id"]})
    assert 200 <= deleted.status_code < 300, deleted.text
    second = _start(client, "当前授权")
    assert first["login_id"] != second["login_id"]
    _submit(client, first, second["callback_url"] + "?code=stale")
    client.delete("/admin/login", headers=AUTH, params={"login_id": first["login_id"]})
    status = client.get(STATUS, headers=AUTH, params={"login_id": second["login_id"]})
    assert status.json()["status"] == "pending"
    assert _accounts(client) == before
    assert not calls


def test_manual_and_automatic_callbacks_share_one_exchange(client, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def exchange(endpoints, **kwargs):
        calls.append(kwargs["code"])
        started.set()
        assert release.wait(5), "Test did not release the pending exchange"
        return OAuthSession("new-ak", "new-sk")

    monkeypatch.setattr(flow, "exchange_authorization_code", exchange)
    before = _accounts(client)
    login = _start(client)
    try:
        assert _submit(client, login, login["callback_url"] + "?code=manual").status_code == 200
        assert started.wait(3)
        with httpx.Client(trust_env=False) as browser:
            browser.get(login["callback_url"], params={"code": "automatic"}, timeout=3)
        assert _accounts(client) == before
    finally:
        release.set()
    assert _settled(client, login)["status"] == "logged_in"
    assert calls == ["manual"]


def test_cancelling_during_exchange_discards_the_result(client, monkeypatch):
    started, release, returned = threading.Event(), threading.Event(), threading.Event()

    def exchange(*args, **kwargs):
        started.set()
        assert release.wait(5)
        returned.set()
        return OAuthSession("unused-ak", "unused-sk")

    monkeypatch.setattr(flow, "exchange_authorization_code", exchange)
    before = _accounts(client)
    login = _start(client)
    try:
        assert _submit(client, login, login["callback_url"] + "?code=dispatched").status_code == 200
        assert started.wait(3)
        assert client.delete("/admin/login", headers=AUTH, params={"login_id": login["login_id"]}).is_success
    finally:
        release.set()
    assert returned.wait(3)
    assert _settled(client, login)["status"] in {"idle", "error"}
    assert _accounts(client) == before


def test_cancel_does_not_commit_a_callback_completed_before_status_poll(client, monkeypatch):
    monkeypatch.setattr(flow, "exchange_authorization_code", lambda *args, **kw: OAuthSession("unused-ak", "unused-sk"))
    before = _accounts(client)
    login = _start(client)
    with httpx.Client(trust_env=False) as browser:
        response = browser.get(login["callback_url"], params={"code": "fixture"})
        assert response.is_redirect
    assert client.delete("/admin/login", headers=AUTH, params={"login_id": login["login_id"]}).is_success
    assert _settled(client, login)["status"] == "idle"
    assert _accounts(client) == before


def test_existing_account_keeps_credentials_on_error_and_accepts_automatic_callback(client, monkeypatch, capsys):
    def exchange(endpoints, **kwargs):
        code = kwargs["code"]
        if code == "private-rejected-code":
            raise flow.LoginError(f"Upstream repeated {code}")
        return OAuthSession("new-ak", "new-sk", refresh_token=code)

    monkeypatch.setattr(flow, "exchange_authorization_code", exchange)
    imported = client.post(
        "/admin/accounts/import",
        headers=AUTH,
        json={"files": [{
            "version": 1,
            "account": {"id": "oauth", "name": "Imported OAuth", "kind": "oauth"},
            "oauth_session": OAuthSession("imported-ak", "imported-sk").to_dict(),
        }]},
    )
    assert imported.status_code == 200, imported.text
    account_id = "oauth"
    route = f"/admin/accounts/{account_id}/login"
    login_response = client.post(route, headers=AUTH)
    assert login_response.status_code == 200, login_response.text
    login = login_response.json()
    assert login["account_id"] == account_id
    with httpx.Client(trust_env=False) as browser:
        auto = browser.get(login["callback_url"], params={"code": "original-refresh"})
        assert auto.status_code in (200, 307)
    assert _settled(client, login)["account_id"] == account_id
    exported = f"/admin/accounts/{account_id}/export"
    original = client.get(exported, headers=AUTH).json()["oauth_session"]
    refreshed = client.post(route, headers=AUTH).json()
    assert refreshed["account_id"] == account_id
    assert _submit(client, refreshed, refreshed["callback_url"] + "?code=private-rejected-code").status_code == 200
    failure = _settled(client, refreshed)
    assert failure["status"] == "error"
    assert "private-rejected-code" not in failure.get("message", "")
    assert client.get(exported, headers=AUTH).json()["oauth_session"] == original

    refreshed = client.post(route, headers=AUTH).json()
    assert _submit(client, refreshed, refreshed["callback_url"] + "?code=replacement-refresh").status_code == 200
    replaced = _settled(client, refreshed)
    assert replaced["status"] == "logged_in", replaced
    assert replaced["account_id"] == account_id
    assert client.get(exported, headers=AUTH).json()["oauth_session"]["refresh_token"] == "replacement-refresh"
    logs = capsys.readouterr().err
    assert "private-rejected-code" not in logs
    assert "original-refresh" not in logs


def test_web_oauth_routes_require_admin_key(client):
    assert client.post(START, json={"name": "no-key"}).status_code == 401
    assert client.post(CALLBACK, json={"login_id": "no-key", "callback_url": "http://localhost/"}).status_code == 401
    assert client.get(STATUS, params={"login_id": "no-key"}).status_code == 401
    assert client.delete("/admin/login", params={"login_id": "no-key"}).status_code == 401


def test_legacy_login_can_finish_without_a_login_id(client, monkeypatch):
    monkeypatch.setattr(
        flow, "exchange_authorization_code", lambda *args, **kw: OAuthSession("fixture-ak", "fixture-sk"),
    )
    response = client.get("/admin/login", headers=AUTH)
    assert response.status_code == 200, response.text
    login = response.json()
    with httpx.Client(trust_env=False) as browser:
        callback = browser.get(login["callback_url"], params={"code": "fixture"})
        assert callback.is_redirect
    result = client.get(STATUS, headers=AUTH).json()
    assert result["status"] == "logged_in", result
    assert next(account for account in _accounts(client) if account["id"] == result["account_id"])["kind"] == "oauth"


def test_existing_name_cannot_be_claimed_by_new_oauth_login(client):
    before = _accounts(client)
    assert len(before) == 1
    original = before[0]
    duplicate = client.post(START, headers=AUTH, json={"name": f" {original['name'].upper()} "})
    assert duplicate.status_code in (400, 409)
    assert _accounts(client) == before


def test_new_login_can_start_after_an_abandoned_attempt_expires(client):
    first = _start(client, "旧授权")
    active = client.app.state.runtime.login_active
    active.created_at -= app_module.CALLBACK_TIMEOUT_SECONDS + 1
    second = _start(client, "新授权")
    assert first["login_id"] != second["login_id"]
    assert _settled(client, first)["status"] in {"idle", "error"}


def test_abandoned_listener_closes_without_a_status_request(client, monkeypatch):
    monkeypatch.setattr(app_module, "CALLBACK_TIMEOUT_SECONDS", 0.5)
    login = _start(client)
    port = urlsplit(login["callback_url"]).port

    def bind_callback_port():
        with socket.socket() as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))

    # Connect errors on Windows may time out instead of reporting refusal.
    with pytest.raises(OSError):
        bind_callback_port()
    deadline = time.monotonic() + 3
    while True:
        try:
            bind_callback_port()
        except OSError:
            assert time.monotonic() < deadline, "Abandoned callback listener did not release its port"
            time.sleep(0.02)
        else:
            break
    assert _settled(client, login)["status"] in {"idle", "error"}


def test_oauth_storage_failure_leaves_no_account(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        flow, "exchange_authorization_code", lambda *args, **kw: OAuthSession("fixture-ak", "fixture-sk"),
    )

    def fail_to_write(*args, **kwargs):
        raise OSError("unwritable private path")

    before = _accounts(client)
    directory = tmp_path / "data" / "accounts"
    before_files = set(directory.glob("*.json"))
    login = _start(client)
    monkeypatch.setattr(pool, "write_private_json", fail_to_write)
    response = _submit(client, login, login["callback_url"] + "?code=issued-code")
    assert response.status_code == 200, response.text
    result = _settled(client, login)
    assert result["status"] == "error", result
    assert "private path" not in result.get("message", "")
    assert _accounts(client) == before
    assert set(directory.glob("*.json")) == before_files
