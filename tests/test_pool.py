"""Account pool migration, selection, failover, and check-in iteration."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from codearts2api.checkin import CheckinResult
from codearts2api.client import UpstreamError
from codearts2api.config import Settings
from codearts2api.credentials import CredentialResolver
from codearts2api.oauth.session import OAuthSession, SessionStore
from codearts2api.operations import post_chat_with_failover, run_checkin_for_accounts
from codearts2api.pool import ENV_ACCOUNT_ID, AccountPool, is_failover_status


def _settings(tmp_path, **kwargs: Any) -> Settings:
    defaults = dict(
        api_key="test-local-key",
        scheduler_enabled=False,
        checkin_run_on_startup=False,
        data_dir=str(tmp_path / "data"),
        auth_dir=str(tmp_path / "auths"),
        pool_strategy="first_available",
        cooldown_seconds=60,
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def test_migrate_from_env_aksk(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(tmp_path, codearts_ak="env-ak-value", codearts_sk="env-sk-value")
    pool = AccountPool(settings)
    pool.load_or_migrate()
    assert len(pool.accounts) == 1
    account = pool.get(ENV_ACCOUNT_ID)
    assert account is not None
    assert account.kind == "aksk"
    assert account.enabled is True
    assert account.ak == "env-ak-value"
    assert account.sk == "env-sk-value"
    assert pool.path.is_file()
    if sys.platform != "win32":
        assert pool.path.stat().st_mode & 0o777 == 0o600

    # Restart: env account stays in sync; a UI account is not wiped.
    pool.add_account(name="work", kind="aksk", ak="ui-ak", sk="ui-sk")
    settings2 = _settings(
        tmp_path,
        codearts_ak="rotated-ak",
        codearts_sk="rotated-sk",
    )
    again = AccountPool(settings2)
    again.load_or_migrate()
    env = again.get(ENV_ACCOUNT_ID)
    assert env is not None
    assert env.ak == "rotated-ak"
    assert env.sk == "rotated-sk"
    names = {item.name for item in again.accounts}
    assert names == {"env", "work"}
    work = again.find_by_name("work")
    assert work is not None
    assert work.ak == "ui-ak"


def test_migrate_from_session_json(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(tmp_path)
    store = SessionStore(settings.auth_dir)
    store.save(
        OAuthSession(
            access_key_id="oauth-ak",
            secret_access_key="oauth-sk",
            security_token="oauth-st",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            source="oauth",
        )
    )
    pool = AccountPool(settings)
    pool.load_or_migrate()
    assert len(pool.accounts) == 1
    account = pool.accounts[0]
    assert account.kind == "oauth"
    assert account.session_file == "session.json"
    session = pool.session_store_for(account).load()
    assert session is not None
    assert session.access_key_id == "oauth-ak"
    resolver = CredentialResolver(settings, store=pool.session_store, pool=pool)
    resolved = resolver.resolve()
    assert resolved.access_key_id == "oauth-ak"
    assert resolved.account_id == account.id


def test_migrate_env_and_session_together(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(tmp_path, codearts_ak="env-ak", codearts_sk="env-sk")
    SessionStore(settings.auth_dir).save(
        OAuthSession(
            access_key_id="oauth-ak",
            secret_access_key="oauth-sk",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )
    pool = AccountPool(settings)
    pool.load_or_migrate()
    kinds = sorted(item.kind for item in pool.accounts)
    assert kinds == ["aksk", "oauth"]


def test_is_failover_status() -> None:
    assert is_failover_status(401)
    assert is_failover_status(403)
    assert is_failover_status(429)
    assert is_failover_status(500)
    assert is_failover_status(503)
    assert not is_failover_status(400)
    assert not is_failover_status(200)


class FakeChatClient:
    def __init__(self, outcomes: dict[str, Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def post_chat(self, payload: dict[str, Any], credential=None) -> Any:
        account_id = credential.account_id if credential else "unknown"
        self.calls.append(account_id)
        outcome = self.outcomes[account_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_chat_failover_on_cooldown_statuses(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(tmp_path, cooldown_seconds=60, pool_strategy="first_available")
    pool = AccountPool(settings)
    pool.load_or_migrate()
    first = pool.add_account(name="one", kind="aksk", ak="ak-one", sk="sk-one")
    second = pool.add_account(name="two", kind="aksk", ak="ak-two", sk="sk-two")
    resolver = CredentialResolver(settings, pool=pool)
    client = FakeChatClient(
        {
            first.id: UpstreamError("unauthorized", 401),
            second.id: {"id": "ok", "choices": []},
        }
    )
    raw = await post_chat_with_failover(
        client=client,  # type: ignore[arg-type]
        resolver=resolver,
        pool=pool,
        payload={"model": "GLM-5.2", "messages": []},
    )
    assert raw["id"] == "ok"
    assert client.calls == [first.id, second.id]
    assert pool.in_cooldown(first.id) is True
    assert pool.in_cooldown(second.id) is False

    # Next pick should skip the account in cooldown.
    client.outcomes[second.id] = {"id": "ok-2", "choices": []}
    raw = await post_chat_with_failover(
        client=client,  # type: ignore[arg-type]
        resolver=resolver,
        pool=pool,
        payload={"model": "GLM-5.2", "messages": []},
    )
    assert raw["id"] == "ok-2"
    assert client.calls[-1] == second.id
    assert first.id not in client.calls[2:]


@pytest.mark.asyncio
async def test_chat_returns_last_error_when_all_fail(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(tmp_path, pool_strategy="first_available")
    pool = AccountPool(settings)
    pool.load_or_migrate()
    a = pool.add_account(name="a", kind="aksk", ak="ak-a", sk="sk-a")
    b = pool.add_account(name="b", kind="aksk", ak="ak-b", sk="sk-b")
    resolver = CredentialResolver(settings, pool=pool)
    client = FakeChatClient(
        {
            a.id: UpstreamError("rate limited", 429),
            b.id: UpstreamError("unavailable", 503),
        }
    )
    with pytest.raises(UpstreamError) as excinfo:
        await post_chat_with_failover(
            client=client,  # type: ignore[arg-type]
            resolver=resolver,
            pool=pool,
            payload={"model": "M", "messages": []},
        )
    assert excinfo.value.status_code == 503
    assert "unavailable" in str(excinfo.value)


class RecordingTransport:
    def __init__(self, account_name: str) -> None:
        self.account_name = account_name

    async def get_plugin_statistics(self) -> Any:
        return {"package": {"is_credit_package": True}}

    async def list_activities(self, channel: str = "IDE") -> Any:
        return {
            "data": [
                {
                    "campaignId": f"camp-{self.account_name}",
                    "type": "DAILY_CLAIM",
                    "claimable": True,
                    "status": "CLAIMABLE",
                }
            ]
        }

    async def claim_campaign(self, campaign_id: Any, channel: str = "IDE") -> tuple[Any, int]:
        return {"id": None, "account": self.account_name}, 200

    async def confirm_campaign(self, campaign_id: Any) -> tuple[Any, int]:
        raise AssertionError("confirm should be skipped when id is null")


class FakeBoundClient:
    def __init__(self) -> None:
        self.bound: list[str] = []

    def bind(self, cred) -> RecordingTransport:  # type: ignore[no-untyped-def]
        self.bound.append(cred.account_id)
        return RecordingTransport(cred.account_name or cred.account_id)


@pytest.mark.asyncio
async def test_checkin_iterates_all_enabled_accounts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(tmp_path)
    pool = AccountPool(settings)
    pool.load_or_migrate()
    a = pool.add_account(name="alpha", kind="aksk", ak="ak-a", sk="sk-a")
    b = pool.add_account(name="beta", kind="aksk", ak="ak-b", sk="sk-b")
    pool.add_account(name="disabled", kind="aksk", ak="ak-c", sk="sk-c", enabled=False)
    resolver = CredentialResolver(settings, pool=pool)
    client = FakeBoundClient()
    per_account, overall, checked_in_at = await run_checkin_for_accounts(
        client=client,  # type: ignore[arg-type]
        resolver=resolver,
        pool=pool,
        accounts=pool.enabled_accounts(),
        campaign_type="USER_LOGIN",
        all_claimable=False,
        timezone_name="Asia/Shanghai",
    )
    assert {item["account_id"] for item in per_account} == {a.id, b.id}
    assert all(item["result"]["status"] == "claimed" for item in per_account)
    assert overall["status"] == "claimed"
    assert checked_in_at
    assert pool.get(a.id).last_checkin["status"] == "claimed"  # type: ignore[index]
    assert pool.get(b.id).last_checkin_at == checked_in_at
    assert "disabled" not in {item["name"] for item in per_account}


def test_round_robin_rotates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(tmp_path, pool_strategy="round_robin")
    pool = AccountPool(settings)
    pool.load_or_migrate()
    a = pool.add_account(name="a", kind="aksk", ak="ak-a", sk="sk-a")
    b = pool.add_account(name="b", kind="aksk", ak="ak-b", sk="sk-b")
    first = [item.id for item in pool.iter_candidates()]
    second = [item.id for item in pool.iter_candidates()]
    assert first[0] != second[0]
    assert set(first) == {a.id, b.id}
    assert set(second) == {a.id, b.id}


def test_public_account_masks_secrets(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(tmp_path)
    pool = AccountPool(settings)
    pool.load_or_migrate()
    account = pool.add_account(
        name="secret",
        kind="aksk",
        ak="ABCDEFG12345",
        sk="super-secret-sk",
        security_token="temp-token-value",
    )
    view = pool.public_account(account)
    dumped = str(view)
    assert "super-secret-sk" not in dumped
    assert "temp-token-value" not in dumped
    assert view["ak_masked"] == "ABCD****"
    assert view["has_sk"] is True
    secrets = pool.all_secrets()
    assert "super-secret-sk" in secrets
    assert "ABCDEFG12345" in secrets

def test_config_example_json_is_loadable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json
    import shutil

    from codearts2api.config import OVERRIDABLE_FIELDS

    example = Path(__file__).resolve().parents[1] / "config.example.json"
    payload = json.loads(example.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    settings_block = payload["settings"]
    unknown = [key for key in settings_block if key not in OVERRIDABLE_FIELDS]
    assert unknown == []
    assert "api_key" not in settings_block
    assert settings_block["checkin_cron"] == "0 8 * * *"
    assert settings_block["timezone"] == "Asia/Shanghai"
    assert settings_block["scheduler_enabled"] is True
    assert "accounts" not in payload

    dest = tmp_path / "data"
    dest.mkdir()
    shutil.copy(example, dest / "config.json")
    settings = _settings(tmp_path, checkin_cron="0 0 * * *", timezone="UTC")
    pool = AccountPool(settings)
    pool.load_or_migrate()
    assert pool.settings.checkin_cron == "0 8 * * *"
    assert pool.settings.timezone == "Asia/Shanghai"
    assert pool.settings.scheduler_enabled is True
    assert pool.accounts == []


def test_credential_source_env_alias_normalizes_to_aksk(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from codearts2api.pool import allowed_kinds, normalize_credential_source

    settings = _settings(tmp_path, credential_source="env")
    assert settings.credential_source == "aksk"
    assert normalize_credential_source("env") == "aksk"
    assert normalize_credential_source("AKSK") == "aksk"
    assert allowed_kinds("env") == {"aksk"}
    assert allowed_kinds("aksk") == {"aksk"}
    assert allowed_kinds("oauth") == {"oauth"}
    assert allowed_kinds("auto") is None
