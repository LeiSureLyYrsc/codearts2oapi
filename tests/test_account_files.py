"""Settings initialization, credential separation, migration and portable auth files."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codearts2api.account_files import account_filename
from codearts2api.app import create_app
from codearts2api.config import Settings
from codearts2api.oauth.dpop import generate_p256_keypair
from codearts2api.oauth.session import OAuthSession, SessionStore
from codearts2api.pool import AccountPool


def settings_for(root: Path, **kwargs) -> Settings:
    return Settings(_env_file=None, api_key="test-gateway-key", scheduler_enabled=False,
                    checkin_run_on_startup=False, data_dir=str(root / "data"),
                    auth_dir=str(root / "auths"), **kwargs)


def pool_for(root: Path, **kwargs) -> AccountPool:
    pool = AccountPool(settings_for(root, **kwargs))
    pool.load_or_migrate()
    return pool


def auth_payload(account_id="example", name="example"):
    return {"version": 1, "account": {"id": account_id, "name": name, "kind": "aksk",
            "ak": "test-access-key", "sk": "test-secret-key", "project_id": "test-project"}}


def test_config_created_without_credentials_and_recreated(tmp_path):
    pool = pool_for(tmp_path)
    original = json.loads(pool.path.read_text())
    assert original["version"] == 2
    assert original["settings"]["timezone"] == "Asia/Shanghai"
    assert original["settings"]["checkin_cron"] == "0 8 * * *"
    assert "accounts" not in original
    assert "api_key" not in original["settings"]
    account = pool.add_account(name="one", kind="aksk", ak="ak", sk="sk")
    pool.path.unlink()
    again = pool_for(tmp_path)
    assert again.path.is_file()
    assert again.get(account.id).sk == "sk"


def test_chat_concurrency_limits_persist_and_default_for_legacy_accounts(tmp_path):
    pool = pool_for(tmp_path)
    account = pool.add_account(
        name="limited", kind="aksk", ak="ak", sk="sk", chat_concurrency_limit=3
    )
    assert account.chat_concurrency_limit == 3
    assert pool.export_account(account.id)["account"]["chat_concurrency_limit"] == 3
    pool.update_account(account.id, {"chat_concurrency_limit": 0})
    pool.update_runtime_settings({"chat_concurrency_limit": 5})

    legacy = auth_payload("legacy", "legacy")
    other = pool_for(tmp_path / "other")
    imported = other.import_accounts([legacy])[0]
    assert imported.chat_concurrency_limit == 0
    assert other.export_account("legacy")["account"]["chat_concurrency_limit"] == 0

    reloaded = pool_for(tmp_path)
    assert reloaded.get(account.id).chat_concurrency_limit == 0
    assert reloaded.settings.chat_concurrency_limit == 5
    assert json.loads(reloaded.path.read_text())["settings"]["chat_concurrency_limit"] == 5


def test_chat_concurrency_limit_rejects_negative_values(tmp_path):
    pool = pool_for(tmp_path)
    for invalid in (-1, -0.1, True, {}):
        with pytest.raises(ValueError, match="chat_concurrency_limit"):
            pool.update_runtime_settings({"chat_concurrency_limit": invalid})
        with pytest.raises(ValueError, match="chat_concurrency_limit"):
            pool.add_account(name="negative", kind="aksk", ak="ak", sk="sk",
                             chat_concurrency_limit=invalid)
    account = pool.add_account(name="valid", kind="aksk", ak="ak", sk="sk")
    for invalid in (-1, -0.1, True, {}):
        with pytest.raises(ValueError, match="chat_concurrency_limit"):
            pool.update_account(account.id, {"chat_concurrency_limit": invalid})


def test_account_crud_stays_out_of_runtime_config(tmp_path):
    pool = pool_for(tmp_path)
    account = pool.add_account(name="one", kind="aksk", ak="test-ak", sk="secret-value")
    path = pool.accounts_dir / account_filename(account.id)
    assert json.loads(path.read_text())["account"]["sk"] == "secret-value"
    assert "secret-value" not in pool.path.read_text()
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
    pool.update_account(account.id, {"sk": "rotated-value"})
    assert pool_for(tmp_path).get(account.id).sk == "rotated-value"
    pool.delete_account(account.id)
    assert not path.exists()
    assert not pool_for(tmp_path).accounts


def test_migrate_legacy_accounts_and_oauth_session(tmp_path):
    settings = settings_for(tmp_path)
    legacy_store = SessionStore(settings.auth_dir)
    legacy_store.save(OAuthSession(access_key_id="oauth-ak", secret_access_key="oauth-sk"))
    path = Path(settings.data_dir) / "config.json"
    path.parent.mkdir(parents=True)
    old = {"version": 1, "settings": {"checkin_cron": "30 9 * * *"}, "accounts": [
        auth_payload()["account"],
        {"id": "oauth", "name": "oauth", "kind": "oauth", "session_file": "session.json"}]}
    path.write_text(json.dumps(old), encoding="utf-8")
    pool = AccountPool(settings)
    pool.load_or_migrate()
    assert pool.settings.checkin_cron == "30 9 * * *"
    assert "accounts" not in json.loads(path.read_text())
    assert "test-secret-key" not in path.read_text()
    assert len(list(pool.accounts_dir.glob("*.json"))) == 2
    oauth = pool.get("oauth")
    assert pool.session_store_for(oauth).load().access_key_id == "oauth-ak"
    stored = json.loads((pool.accounts_dir / "oauth.json").read_text())
    assert stored["oauth_session"]["secret_access_key"] == "oauth-sk"
    assert not legacy_store.path.exists()
    assert len(pool_for(tmp_path).accounts) == 2
    pool.delete_account(oauth.id)
    assert len(pool_for(tmp_path).accounts) == 1  # no resurrection from legacy session.json


def test_failed_migration_preserves_config(tmp_path, monkeypatch):
    pool = pool_for(tmp_path)
    legacy = {"version": 1, "accounts": [auth_payload()["account"]]}
    raw = json.dumps(legacy)
    pool.path.write_text(raw)
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr("codearts2api.pool.write_private_json", fail)
    with pytest.raises(OSError):
        pool.load_or_migrate()
    assert pool.path.read_text() == raw


def test_bad_config_or_auth_is_not_overwritten(tmp_path):
    pool = pool_for(tmp_path)
    pool.path.write_text("{broken")
    with pytest.raises(ValueError):
        pool_for(tmp_path)
    assert pool.path.read_text() == "{broken"
    pool.path.write_text("{}")
    bad = pool.accounts_dir / "broken.json"
    bad.write_text("{broken")
    with pytest.raises(ValueError):
        pool_for(tmp_path)
    assert bad.read_text() == "{broken"
    assert pool.path.read_text() == "{}"


def test_import_export_single_and_multiple_accounts(tmp_path):
    pool = pool_for(tmp_path)
    imported = pool.import_accounts([auth_payload(), auth_payload("two", "second")])
    assert len(imported) == 2
    exported = pool.export_account("example")
    other = pool_for(tmp_path / "other")
    other.import_accounts([exported])
    assert other.get("example").sk == "test-secret-key"
    assert len(pool_for(tmp_path).accounts) == 2
    assert "accounts" not in json.loads(pool.path.read_text())


@pytest.mark.parametrize("field,value", [("id", "../outside"), ("id", "a/b"),
    ("kind", "other"), ("sk", ""), ("ak", None), ("enabled", "false"),
    ("session_file", "../session.json"), ("name", "   ")])
def test_import_rejects_invalid_fields_without_echoing_secrets(tmp_path, field, value):
    pool = pool_for(tmp_path)
    payload = auth_payload()
    payload["account"][field] = value
    with pytest.raises(ValueError) as error:
        pool.import_accounts([payload])
    assert "test-secret-key" not in str(error.value)
    assert pool.accounts == []
    assert list(pool.accounts_dir.glob("*.json")) == []


@pytest.mark.parametrize("second", [auth_payload(), auth_payload("EXAMPLE", "other"),
                                     auth_payload("different", "EXAMPLE")])
def test_batch_conflicts_are_rejected_without_partial_import(tmp_path, second):
    pool = pool_for(tmp_path)
    with pytest.raises(ValueError):
        pool.import_accounts([auth_payload(), copy.deepcopy(second)])
    assert pool.accounts == []
    assert list(pool.accounts_dir.glob("*.json")) == []


def test_existing_conflict_never_overwrites(tmp_path):
    pool = pool_for(tmp_path)
    pool.import_accounts([auth_payload()])
    duplicate = auth_payload()
    duplicate["account"]["sk"] = "do-not-overwrite"
    with pytest.raises(ValueError):
        pool.import_accounts([auth_payload("new", "new"), duplicate])
    assert pool_for(tmp_path).get("example").sk == "test-secret-key"
    assert pool.get("new") is None


def test_oauth_single_file_export_import_and_logout(tmp_path):
    pool = pool_for(tmp_path)
    account = pool.add_account(name="oauth", kind="oauth")
    session = OAuthSession(access_key_id="oauth-ak", secret_access_key="oauth-sk",
                           refresh_token="test-refresh", dpop=generate_p256_keypair())
    pool.attach_session(account, session)
    assert not list(Path(pool.settings.auth_dir).glob("*.json"))
    payload = pool.export_account(account.id)
    assert payload["oauth_session"]["refresh_token"] == "test-refresh"
    other = pool_for(tmp_path / "other")
    imported = other.import_accounts([payload])[0]
    loaded = other.session_store_for(imported).load()
    assert loaded.secret_access_key == "oauth-sk"
    assert loaded.can_refresh()
    other.update_account(imported.id, {"note": "must retain session"})
    assert pool_for(tmp_path / "other").session_store_for(imported).load().can_refresh()
    other.session_store_for(imported).clear()
    assert other.get(imported.id) is not None
    assert other.session_store_for(imported).load() is None
    assert other.export_account(imported.id).get("oauth_session") is None


def test_import_export_api_requires_auth_and_prevents_caching(tmp_path):
    with TestClient(create_app(settings_for(tmp_path))) as client:
        assert client.post("/admin/accounts/import", json={"files": [auth_payload()]}).status_code == 401
        auth = {"Authorization": "Bearer test-gateway-key"}
        response = client.post("/admin/accounts/import", headers=auth, json={"files": [auth_payload()]})
        assert response.status_code == 200
        assert "test-secret-key" not in response.text
        assert client.get("/admin/accounts/example/export").status_code == 401
        response = client.get("/admin/accounts/example/export", headers=auth)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "attachment" in response.headers["content-disposition"]
        assert response.json()["account"]["sk"] == "test-secret-key"
        assert client.get("/admin/accounts/missing/export", headers=auth).status_code == 404
        assert client.post("/admin/accounts/import", headers=auth, json={"files": [auth_payload()]}).status_code == 400
        assert client.post("/admin/accounts/import", headers=auth, content="{bad").status_code == 400
        assert client.post("/admin/accounts/import", headers=auth, content="x" * (4 * 1024 * 1024 + 1)).status_code == 413
        assert "test-secret-key" not in client.get("/admin/config", headers=auth).text


def test_migration_retry_after_config_write_failure(tmp_path, monkeypatch):
    from codearts2api.account_files import write_private_json
    pool = pool_for(tmp_path)
    raw = json.dumps({"version": 1, "accounts": [auth_payload()["account"]]})
    pool.path.write_text(raw)
    def fail_config(path, payload):
        if path.name == "config.json":
            raise OSError("disk full")
        write_private_json(path, payload)
    with monkeypatch.context() as patch:
        patch.setattr("codearts2api.pool.write_private_json", fail_config)
        with pytest.raises(OSError):
            pool.load_or_migrate()
    assert pool.path.read_text() == raw
    again = pool_for(tmp_path)
    assert len(again.accounts) == 1
    assert "accounts" not in json.loads(again.path.read_text())


def test_unknown_config_version_is_not_overwritten(tmp_path):
    pool = pool_for(tmp_path)
    raw = '{"version": 999}'
    pool.path.write_text(raw)
    with pytest.raises(ValueError):
        pool_for(tmp_path)
    assert pool.path.read_text() == raw


def test_invalid_oauth_import_is_rejected_without_writes(tmp_path):
    pool = pool_for(tmp_path)
    payload = {"version": 1, "account": {"id": "oauth", "name": "oauth", "kind": "oauth"},
               "oauth_session": {"refresh_token": "test-refresh"}}
    with pytest.raises(ValueError):
        pool.import_accounts([payload])
    assert pool.accounts == []
    assert list(pool.accounts_dir.glob("*.json")) == []


def test_config_api_does_not_accept_account_credentials(tmp_path):
    with TestClient(create_app(settings_for(tmp_path))) as client:
        headers = {"Authorization": "Bearer test-gateway-key"}
        for body in [{"accounts": []}, {"codearts_ak": "must-not-be-config"},
                     {"sk": "must-not-be-config"}, {"security_token": "must-not-be-config"},
                     {"project_id": "must-not-be-config"}]:
            assert client.put("/admin/config", headers=headers, json=body).status_code == 400
        assert "must-not-be-config" not in (tmp_path / "data" / "config.json").read_text()


def test_selection_order_survives_restart(tmp_path):
    pool = pool_for(tmp_path)
    first = pool.add_account(name="first", kind="aksk", ak="a", sk="s")
    second = pool.add_account(name="second", kind="aksk", ak="b", sk="t")
    pool.update_runtime_settings({"pool_strategy": "first_available"})
    again = pool_for(tmp_path)
    assert [account.id for account in again.iter_candidates()] == [first.id, second.id]


def test_name_conflict_ignores_whitespace(tmp_path):
    pool = pool_for(tmp_path)
    pool.import_accounts([auth_payload()])
    with pytest.raises(ValueError):
        pool.import_accounts([auth_payload("different", " EXAMPLE ")])
