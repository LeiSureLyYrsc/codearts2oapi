"""Persisted multi-account credential pool and runtime settings overlay."""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from pydantic import SecretStr

from codearts2api.account_files import AccountSessionStore, account_filename, validate_auth_file, write_private_json
from codearts2api.config import OVERRIDABLE_FIELDS, Settings
from codearts2api.oauth.session import SESSION_FILENAME, OAuthSession, SessionStore

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "config.json"
ENV_ACCOUNT_ID = "env"
DEFAULT_OAUTH_NAME = "oauth"
FAILOVER_STATUSES = frozenset({401, 403, 429, *range(500, 600)})
ACCOUNT_KINDS = frozenset({"aksk", "oauth"})
POOL_STRATEGIES = frozenset({"round_robin", "first_available", "random"})


def is_failover_status(status_code: int) -> bool:
    return status_code in FAILOVER_STATUSES


def mask_secret(value: str | None, *, prefix: int = 4) -> str | None:
    if not value:
        return None
    if len(value) <= prefix:
        return "****"
    return value[:prefix] + "****"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_in_timezone(tz_name: str) -> datetime:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    return datetime.now(tz)


@dataclass
class Account:
    id: str
    name: str
    kind: str
    enabled: bool = True
    note: str = ""
    ak: str | None = None
    sk: str | None = None
    security_token: str | None = None
    project_id: str | None = None
    chat_concurrency_limit: int = 0
    session_file: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    last_checkin: dict[str, Any] | None = None
    last_checkin_at: str | None = None
    last_error: str | None = None
    last_benefit: dict[str, Any] | None = None
    last_benefit_at: str | None = None

    def has_aksk(self) -> bool:
        return bool(self.ak and self.sk)

    def to_storage(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "enabled": self.enabled,
            "note": self.note,
            "ak": self.ak,
            "sk": self.sk,
            "security_token": self.security_token,
            "project_id": self.project_id,
            "chat_concurrency_limit": self.chat_concurrency_limit,
            "session_file": self.session_file,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_checkin": self.last_checkin,
            "last_checkin_at": self.last_checkin_at,
            "last_error": self.last_error,
            "last_benefit": self.last_benefit,
            "last_benefit_at": self.last_benefit_at,
        }

    @classmethod
    def from_storage(cls, payload: dict[str, Any]) -> "Account":
        kind = str(payload.get("kind") or "aksk").strip().lower()
        if kind not in ACCOUNT_KINDS:
            kind = "aksk"
        return cls(
            id=str(payload.get("id") or uuid.uuid4()),
            name=str(payload.get("name") or payload.get("id") or "account"),
            kind=kind,
            enabled=bool(payload.get("enabled", True)),
            note=str(payload.get("note") or ""),
            ak=payload.get("ak") or None,
            sk=payload.get("sk") or None,
            security_token=payload.get("security_token") or None,
            project_id=payload.get("project_id") or None,
            chat_concurrency_limit=payload.get("chat_concurrency_limit", 0),
            session_file=payload.get("session_file") or None,
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
            last_checkin=payload.get("last_checkin") if isinstance(payload.get("last_checkin"), dict) else None,
            last_checkin_at=payload.get("last_checkin_at"),
            last_error=payload.get("last_error"),
            last_benefit=payload.get("last_benefit") if isinstance(payload.get("last_benefit"), dict) else None,
            last_benefit_at=payload.get("last_benefit_at"),
        )


class AccountPool:
    """Runtime settings in config.json; credentials in separate per-account auth files."""

    def __init__(self, settings: Settings, session_store: SessionStore | None = None) -> None:
        self.settings = settings
        self.data_dir = Path(settings.data_dir)
        self.path = self.data_dir / CONFIG_FILENAME
        self.accounts_dir = self.data_dir / "accounts"
        self.session_store = session_store or SessionStore(settings.auth_dir)
        self.accounts: list[Account] = []
        self.persisted_settings: dict[str, Any] = {}
        self.api_key_persisted = False
        self._lock = threading.RLock()
        self._rr_index = 0
        self._cooldowns: dict[str, float] = {}
        self._cooldown_until_wall: dict[str, str] = {}
        self._cooldown_status: dict[str, int] = {}
        self._loaded = False

    def load_or_migrate(self) -> None:
        with self._lock:
            self._load_file()
            self._apply_persisted_settings()
            self._migrate()
            # Always materialize defaults, including on an empty first start.
            # Write all auth files BEFORE removing credentials from legacy config.
            self._save_unlocked()
            self._loaded = True

    def _load_file(self) -> None:
        payload: dict[str, Any] = {}
        if self.path.is_file():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                raise ValueError("Cannot read runtime config; refusing to overwrite it") from None
            if not isinstance(payload, dict):
                raise ValueError("Runtime config must be a JSON object")
        if "version" in payload and (type(payload["version"]) is not int or payload["version"] not in {1, 2}):
            raise ValueError("Unsupported runtime config version; refusing to overwrite")
        raw_accounts = payload.get("accounts", [])
        if not isinstance(raw_accounts, list) or any(not isinstance(x, dict) for x in raw_accounts):
            raise ValueError("Invalid legacy accounts; config was not modified")
        self.accounts = []
        for path in sorted(self.accounts_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8-sig"))
                data, _ = validate_auth_file(record)
                if path.name != account_filename(data["id"]):
                    raise ValueError("Account filename does not match id")
                if self.get(data["id"]) is not None:
                    raise ValueError("Duplicate account id")
                self.accounts.append(Account.from_storage(data))
            except (OSError, ValueError):
                raise ValueError(f"Invalid account auth file: {path.name}; refusing to overwrite") from None
        for item in raw_accounts:
            account = Account.from_storage(item)
            validate_auth_file({"version": 1, "account": account.to_storage()})
            existing = self.get(account.id)
            if existing is not None:
                if existing.to_storage() != account.to_storage():
                    raise ValueError("Legacy migration conflicts with an existing auth file")
            else:
                self.accounts.append(account)
        # File iteration order must not randomize first_available after a restart.
        self.accounts.sort(key=lambda account: account.created_at or "")
        block = payload.get("settings", {})
        if not isinstance(block, dict):
            raise ValueError("Runtime settings must be a JSON object")
        # Credentials are never accepted as runtime settings.
        self.persisted_settings = {k: v for k, v in block.items() if k in OVERRIDABLE_FIELDS}
        self.api_key_persisted = bool(self.persisted_settings.get("api_key"))
        pool_block = payload.get("pool") or {}
        try:
            self._rr_index = int(pool_block.get("rr_index", 0))
        except (AttributeError, TypeError, ValueError):
            self._rr_index = 0

    def _save_unlocked(self) -> None:
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        paths = [account_filename(a.id).casefold() for a in self.accounts]
        if len(set(paths)) != len(paths):
            raise ValueError("Conflicting account filenames; migration stopped")
        migrated_sessions = []
        for account in self.accounts:
            path = self.accounts_dir / account_filename(account.id)
            payload = {"version": 1, "account": account.to_storage()}
            if path.is_file():
                old = json.loads(path.read_text(encoding="utf-8-sig"))
                if "oauth_session" in old:
                    payload["oauth_session"] = old["oauth_session"]
            if account.kind == "oauth" and "oauth_session" not in payload:
                filename = account.session_file or f"{account.id}.json"
                if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
                    raise ValueError("Unsafe legacy session filename")
                legacy = self.session_store.for_file(filename)
                session = legacy.load()
                if legacy.path.is_file() and session is None:
                    raise ValueError("Invalid legacy OAuth session; migration stopped")
                if session:
                    payload["oauth_session"] = session.to_dict()
                    migrated_sessions.append(legacy.path)
            # A prior interrupted migration may have written the auth file already.
            if account.kind == "oauth" and "oauth_session" in payload and account.session_file:
                legacy = self.session_store.for_file(account.session_file)
                if legacy.path.is_file():
                    old_session = legacy.load()
                    if old_session and old_session.to_dict() == payload["oauth_session"]:
                        migrated_sessions.append(legacy.path)
            write_private_json(path, payload)
        # Persist only application settings; never bootstrap secrets/credentials.
        defaults = {key: getattr(self.settings, key) for key in OVERRIDABLE_FIELDS if key != "api_key"}
        defaults.update(self.persisted_settings)
        self.persisted_settings = defaults
        write_private_json(self.path, {
            "version": 2, "settings": defaults, "pool": {"rr_index": self._rr_index},
        })
        for path in set(migrated_sessions):
            path.unlink(missing_ok=True)

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()

    def _migrate(self) -> bool:
        changed = False
        if self.settings.has_credentials() and not self.accounts:
            env_account = self._build_env_account()
            self.accounts.append(env_account)
            logger.info("Seeded account pool from CODEARTS_AK/SK id=%s", env_account.id)
            changed = True
        elif self.settings.has_credentials():
            existing = self.get(ENV_ACCOUNT_ID)
            if existing is not None and existing.kind == "aksk":
                if self._sync_env_account(existing):
                    changed = True

        if self._legacy_session_exists() and not self._account_uses_session_file(SESSION_FILENAME):
            account = self._build_oauth_account(
                account_id=str(uuid.uuid4()),
                name=DEFAULT_OAUTH_NAME,
                session_file=SESSION_FILENAME,
            )
            self.accounts.append(account)
            logger.info("Imported OAuth session.json as pool account id=%s", account.id)
            changed = True
        return changed

    def _build_env_account(self) -> Account:
        token = None
        if self.settings.codearts_security_token:
            token = self.settings.codearts_security_token.get_secret_value() or None
        now = utc_now_iso()
        return Account(
            id=ENV_ACCOUNT_ID,
            name="env",
            kind="aksk",
            enabled=True,
            note="Seeded from CODEARTS_AK/SK environment variables",
            ak=self.settings.codearts_ak.get_secret_value() if self.settings.codearts_ak else None,
            sk=self.settings.codearts_sk.get_secret_value() if self.settings.codearts_sk else None,
            security_token=token,
            project_id=self.settings.codearts_project_id,
            created_at=now,
            updated_at=now,
        )

    def _sync_env_account(self, account: Account) -> bool:
        ak = self.settings.codearts_ak.get_secret_value() if self.settings.codearts_ak else None
        sk = self.settings.codearts_sk.get_secret_value() if self.settings.codearts_sk else None
        token = None
        if self.settings.codearts_security_token:
            token = self.settings.codearts_security_token.get_secret_value() or None
        project_id = self.settings.codearts_project_id
        changed = False
        if account.ak != ak or account.sk != sk or account.security_token != token or account.project_id != project_id:
            account.ak = ak
            account.sk = sk
            account.security_token = token
            account.project_id = project_id
            account.updated_at = utc_now_iso()
            changed = True
        return changed

    def _legacy_session_exists(self) -> bool:
        path = Path(self.settings.auth_dir) / SESSION_FILENAME
        return path.is_file()

    def _account_uses_session_file(self, filename: str) -> bool:
        return any(
            account.kind == "oauth" and (account.session_file or SESSION_FILENAME) == filename
            for account in self.accounts
        )

    def _build_oauth_account(self, *, account_id: str, name: str, session_file: str) -> Account:
        now = utc_now_iso()
        return Account(
            id=account_id,
            name=name,
            kind="oauth",
            enabled=True,
            session_file=session_file,
            created_at=now,
            updated_at=now,
        )

    def _apply_persisted_settings(self) -> None:
        if not self.persisted_settings:
            return
        apply_settings_overlay(self.settings, self.persisted_settings)
        if self.persisted_settings.get("api_key"):
            self.api_key_persisted = True

    def export_account(self, account_id: str) -> dict[str, Any]:
        with self._lock:
            account = self.get(account_id)
            if account is None:
                raise KeyError(account_id)
            payload = {"version": 1, "account": account.to_storage()}
            if account.kind == "oauth":
                session = self.session_store_for(account).load()
                if session:
                    payload["oauth_session"] = session.to_dict()
            return payload

    def import_accounts(self, files: list[dict[str, Any]]) -> list[Account]:
        if not files or len(files) > 100:
            raise ValueError("Import between 1 and 100 auth files")
        parsed = [validate_auth_file(payload) for payload in files]
        with self._lock:
            ids = {a.id.casefold() for a in self.accounts}
            names = {a.name.strip().casefold() for a in self.accounts}
            filenames = {account_filename(a.id).casefold() for a in self.accounts}
            planned = []
            for data, session in parsed:
                filename = account_filename(data["id"]).casefold()
                if filename in filenames:
                    raise ValueError("Account filename conflict; no files imported")
                filenames.add(filename)
                if data["id"].casefold() in ids or data["name"].casefold() in names:
                    raise ValueError("Account id or name conflict; no files imported")
                ids.add(data["id"].casefold())
                names.add(data["name"].casefold())
                account = Account.from_storage(data)
                if account.created_at is None:
                    account.created_at = utc_now_iso()
                # Never trust a portable file's session path or share another account's session.
                account.session_file = f"{uuid.uuid4()}.json" if account.kind == "oauth" else None
                planned.append((account, session))
            written = []
            try:
                for account, session in planned:
                    path = self.accounts_dir / account_filename(account.id)
                    if path.exists():
                        raise ValueError("Auth file already exists")
                    written.append(path)
                    payload = {"version": 1, "account": account.to_storage()}
                    if session:
                        payload["oauth_session"] = session
                    write_private_json(path, payload)
            except Exception:
                for path in written:
                    path.unlink(missing_ok=True)
                raise
            accounts = [account for account, _ in planned]
            self.accounts.extend(accounts)
            return accounts

    def get(self, account_id: str) -> Account | None:
        for account in self.accounts:
            if account.id == account_id:
                return account
        return None

    def find_by_name(self, name: str) -> Account | None:
        needle = name.strip().lower()
        for account in self.accounts:
            if account.name.strip().lower() == needle:
                return account
        return None

    def enabled_accounts(self, *, kind: str | None = None) -> list[Account]:
        result: list[Account] = []
        for account in self.accounts:
            if not account.enabled:
                continue
            if kind is not None and account.kind != kind:
                continue
            result.append(account)
        return result

    def session_store_for(self, account: Account) -> SessionStore:
        return AccountSessionStore(self.accounts_dir / account_filename(account.id), account, self._lock)

    def attach_session(self, account: Account, session: OAuthSession) -> Path:
        if not account.session_file:
            account.session_file = f"{account.id}.json"
        path = self.session_store_for(account).save(session)
        account.updated_at = utc_now_iso()
        self.save()
        return path

    def find_or_create_oauth(self, name: str | None = None, *, use_legacy_session: bool = False) -> Account:
        with self._lock:
            if name:
                existing = self.find_by_name(name)
                if existing is not None:
                    if existing.kind != "oauth":
                        raise ValueError(f"Account {name!r} exists but is not an OAuth account")
                    return existing
            elif use_legacy_session:
                for account in self.accounts:
                    if account.kind == "oauth" and (account.session_file or "") == SESSION_FILENAME:
                        return account
                existing_default = self.find_by_name(DEFAULT_OAUTH_NAME)
                if existing_default is not None and existing_default.kind == "oauth":
                    return existing_default
            account_id = str(uuid.uuid4())
            session_file = SESSION_FILENAME if use_legacy_session else f"{account_id}.json"
            account = self._build_oauth_account(
                account_id=account_id,
                name=name or DEFAULT_OAUTH_NAME,
                session_file=session_file,
            )
            self.accounts.append(account)
            self._save_unlocked()
            logger.info("Created OAuth pool account id=%s name=%s", account.id, account.name)
            return account

    def create_oauth_account(
        self,
        *,
        name: str,
        session: OAuthSession,
        enabled: bool = True,
        note: str = "",
        chat_concurrency_limit: int = 0,
    ) -> Account:
        """Create an OAuth account and its first session in one atomic write.

        The auth file is written first; the account joins the live pool only after
        the write succeeds, so a failed write leaves no half-created account.
        """
        name = name.strip()
        if not name:
            raise ValueError("请填写账号名称")
        if not session.access_key_id or not session.secret_access_key:
            raise ValueError("OAuth session is missing credentials")
        if type(chat_concurrency_limit) is not int or chat_concurrency_limit < 0:
            raise ValueError("chat_concurrency_limit must be >= 0")
        with self._lock:
            if self.find_by_name(name) is not None:
                raise ValueError(f"已存在同名账号「{name}」，请换一个名称，或先删除旧账号。")
            now = utc_now_iso()
            account_id = str(uuid.uuid4())
            account = Account(
                id=account_id,
                name=name,
                kind="oauth",
                enabled=bool(enabled),
                note=note or "",
                chat_concurrency_limit=chat_concurrency_limit,
                session_file=f"{account_id}.json",
                created_at=now,
                updated_at=now,
            )
            path = self.accounts_dir / account_filename(account.id)
            write_private_json(path, {
                "version": 1,
                "account": account.to_storage(),
                "oauth_session": session.to_dict(),
            })
            self.accounts.append(account)
            logger.info("Created OAuth pool account id=%s name=%s", account.id, account.name)
            return account

    def add_account(
        self,
        *,
        name: str,
        kind: str,
        enabled: bool = True,
        note: str = "",
        ak: str | None = None,
        sk: str | None = None,
        security_token: str | None = None,
        project_id: str | None = None,
        chat_concurrency_limit: int = 0,
    ) -> Account:
        kind = kind.strip().lower()
        if kind not in ACCOUNT_KINDS:
            raise ValueError("kind must be aksk or oauth")
        if type(chat_concurrency_limit) is not int or chat_concurrency_limit < 0:
            raise ValueError("chat_concurrency_limit must be >= 0")
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        if kind == "aksk" and (not ak or not sk):
            raise ValueError("AK/SK accounts require ak and sk")
        with self._lock:
            now = utc_now_iso()
            account_id = str(uuid.uuid4())
            account = Account(
                id=account_id,
                name=name,
                kind=kind,
                enabled=enabled,
                note=note or "",
                ak=ak or None,
                sk=sk or None,
                security_token=security_token or None,
                project_id=project_id or None,
                chat_concurrency_limit=chat_concurrency_limit,
                session_file=f"{account_id}.json" if kind == "oauth" else None,
                created_at=now,
                updated_at=now,
            )
            self.accounts.append(account)
            self._save_unlocked()
            logger.info("Added pool account id=%s name=%s kind=%s", account.id, account.name, account.kind)
            return account

    def update_account(self, account_id: str, data: dict[str, Any]) -> Account:
        with self._lock:
            account = self.get(account_id)
            if account is None:
                raise KeyError(account_id)
            if "name" in data and data["name"] is not None:
                name = str(data["name"]).strip()
                if not name:
                    raise ValueError("name must not be empty")
                account.name = name
            if "enabled" in data and data["enabled"] is not None:
                account.enabled = bool(data["enabled"])
            if "note" in data and data["note"] is not None:
                account.note = str(data["note"])
            if "project_id" in data:
                value = data["project_id"]
                account.project_id = None if value in (None, "") else str(value)
            if "chat_concurrency_limit" in data and data["chat_concurrency_limit"] is not None:
                limit = data["chat_concurrency_limit"]
                if type(limit) is not int or limit < 0:
                    raise ValueError("chat_concurrency_limit must be >= 0")
                account.chat_concurrency_limit = limit
            # Blank secret fields mean keep the existing value.
            if "ak" in data and data["ak"]:
                account.ak = str(data["ak"])
            if "sk" in data and data["sk"]:
                account.sk = str(data["sk"])
            if "security_token" in data:
                token = data["security_token"]
                if token:
                    account.security_token = str(token)
                elif token is None:
                    account.security_token = None
            if account.kind == "aksk" and not account.has_aksk():
                raise ValueError("AK/SK accounts require ak and sk")
            account.updated_at = utc_now_iso()
            self._save_unlocked()
            logger.info("Updated pool account id=%s name=%s", account.id, account.name)
            return account

    def delete_account(self, account_id: str) -> None:
        with self._lock:
            account = self.get(account_id)
            if account is None:
                raise KeyError(account_id)
            (self.accounts_dir / account_filename(account_id)).unlink(missing_ok=True)
            self.accounts = [item for item in self.accounts if item.id != account_id]
            self._cooldowns.pop(account_id, None)
            self._cooldown_until_wall.pop(account_id, None)
            self._cooldown_status.pop(account_id, None)
            self._save_unlocked()
            logger.info("Deleted pool account id=%s name=%s", account_id, account.name)

    def record_checkin(self, account_id: str, result: dict[str, Any], checked_in_at: str) -> None:
        with self._lock:
            account = self.get(account_id)
            if account is None:
                return
            account.last_checkin = result
            account.last_checkin_at = checked_in_at
            if result.get("status") == "error":
                account.last_error = str(result.get("message") or "error")
            else:
                account.last_error = None
            account.updated_at = utc_now_iso()
            self._save_unlocked()

    def record_benefit(self, account_id: str, result: dict[str, Any], claimed_at: str) -> None:
        with self._lock:
            account = self.get(account_id)
            if account is None:
                return
            account.last_benefit = result
            account.last_benefit_at = claimed_at
            account.updated_at = utc_now_iso()
            self._save_unlocked()

    def mark_cooldown(self, account_id: str, status_code: int, seconds: int | None = None) -> None:
        duration = self.settings.cooldown_seconds if seconds is None else seconds
        if duration <= 0:
            return
        deadline = time.monotonic() + duration
        until = (datetime.now(timezone.utc) + timedelta(seconds=duration)).isoformat()
        with self._lock:
            self._cooldowns[account_id] = deadline
            self._cooldown_until_wall[account_id] = until
            self._cooldown_status[account_id] = status_code
        account = self.get(account_id)
        name = account.name if account else account_id
        logger.info(
            "Account id=%s name=%s entered cooldown for %ss after HTTP %s",
            account_id,
            name,
            duration,
            status_code,
        )

    def clear_cooldown(self, account_id: str) -> None:
        with self._lock:
            self._cooldowns.pop(account_id, None)
            self._cooldown_until_wall.pop(account_id, None)
            self._cooldown_status.pop(account_id, None)

    def in_cooldown(self, account_id: str) -> bool:
        deadline = self._cooldowns.get(account_id)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            self.clear_cooldown(account_id)
            return False
        return True

    def cooldown_info(self, account_id: str) -> dict[str, Any]:
        active = self.in_cooldown(account_id)
        return {
            "in_cooldown": active,
            "cooldown_until": self._cooldown_until_wall.get(account_id) if active else None,
            "cooldown_http_status": self._cooldown_status.get(account_id) if active else None,
        }

    def iter_candidates(self) -> list[Account]:
        """Enabled accounts that can be tried, ordered by the configured strategy.

        Healthy (not in cooldown) accounts are preferred. If every enabled account
        is in cooldown, the enabled list is still returned so a single-account
        setup is not hard-blocked until the cooldown expires.
        """
        with self._lock:
            kinds = allowed_kinds(self.settings.credential_source)
            enabled = [
                account for account in self.accounts
                if account.enabled and (kinds is None or account.kind in kinds)
            ]
            healthy = [account for account in enabled if not self.in_cooldown(account.id)]
            chosen = healthy or enabled
            return self._order(chosen)

    def _order(self, accounts: list[Account]) -> list[Account]:
        if not accounts:
            return []
        strategy = self.settings.pool_strategy
        if strategy == "first_available":
            return list(accounts)
        if strategy == "random":
            shuffled = list(accounts)
            random.shuffle(shuffled)
            return shuffled
        n = len(accounts)
        start = self._rr_index % n
        self._rr_index = (start + 1) % n
        return accounts[start:] + accounts[:start]

    def update_runtime_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        cleaned = validate_settings_update(updates)
        with self._lock:
            for key, value in cleaned.items():
                if key == "api_key":
                    if value:
                        self.persisted_settings["api_key"] = value
                        self.api_key_persisted = True
                    continue
                if value is None and key == "opengw_base_url":
                    self.persisted_settings[key] = None
                    continue
                if value is None and key == "x_language":
                    self.persisted_settings[key] = None
                    continue
                self.persisted_settings[key] = value
            apply_settings_overlay(self.settings, self.persisted_settings)
            self._save_unlocked()
        return self.public_config()

    def public_config(self) -> dict[str, Any]:
        payload = self.settings.public_config()
        payload["api_key_source"] = "ui" if self.api_key_persisted else "env"
        return payload

    def public_account(self, account: Account, session: OAuthSession | None = None) -> dict[str, Any]:
        info = self.cooldown_info(account.id)
        oauth_view = None
        if account.kind == "oauth":
            loaded = session
            if loaded is None:
                loaded = self.session_store_for(account).load()
            oauth_view = loaded.public_view() if loaded else {
                "source": "oauth",
                "has_access_key": False,
                "has_refresh_token": False,
                "expires_at": None,
                "expired": True,
                "can_refresh": False,
            }
        return {
            "id": account.id,
            "name": account.name,
            "kind": account.kind,
            "enabled": account.enabled,
            "note": account.note,
            "ak_masked": mask_secret(account.ak),
            "has_ak": bool(account.ak),
            "has_sk": bool(account.sk),
            "has_security_token": bool(account.security_token),
            "project_id": account.project_id,
            "chat_concurrency_limit": account.chat_concurrency_limit,
            "session_file": account.session_file if account.kind == "oauth" else None,
            "oauth": oauth_view,
            "last_checkin": account.last_checkin,
            "last_checkin_at": account.last_checkin_at,
            "last_error": account.last_error,
            "last_benefit": account.last_benefit,
            "last_benefit_at": account.last_benefit_at,
            "created_at": account.created_at,
            "updated_at": account.updated_at,
            **info,
        }

    def public_accounts(self) -> list[dict[str, Any]]:
        return [self.public_account(account) for account in self.accounts]

    def status_summary(self) -> dict[str, Any]:
        enabled = self.enabled_accounts()
        return {
            "count": len(self.accounts),
            "enabled": len(enabled),
            "strategy": self.settings.pool_strategy,
            "cooldown_seconds": self.settings.cooldown_seconds,
            "accounts": self.public_accounts(),
        }

    def all_secrets(self) -> list[str]:
        values: list[str] = []
        for account in self.accounts:
            for item in (account.ak, account.sk, account.security_token):
                if item:
                    values.append(item)
            if account.kind != "oauth":
                continue
            session = self.session_store_for(account).load()
            if session is None:
                continue
            for item in (
                session.access_key_id,
                session.secret_access_key,
                session.security_token,
                session.refresh_token,
            ):
                if item:
                    values.append(item)
        if self.persisted_settings.get("api_key"):
            values.append(str(self.persisted_settings["api_key"]))
        return values


def apply_settings_overlay(settings: Settings, overlay: dict[str, Any]) -> None:
    for key in OVERRIDABLE_FIELDS:
        if key not in overlay:
            continue
        value = overlay[key]
        if key == "api_key":
            if value:
                settings.api_key = SecretStr(str(value))
            continue
        if key == "credential_source" and value is not None:
            value = normalize_credential_source(str(value))
        setattr(settings, key, value)


def validate_settings_update(updates: dict[str, Any]) -> dict[str, Any]:
    forbidden = {"host", "port", "data_dir", "auth_dir", "codearts_ak", "codearts_sk", "codearts_security_token"}
    unknown = [key for key in updates if key not in OVERRIDABLE_FIELDS and key not in forbidden]
    blocked = [key for key in updates if key in forbidden]
    if blocked:
        raise ValueError(f"These settings cannot be changed from the WebUI: {', '.join(blocked)}")
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(unknown)}")

    cleaned: dict[str, Any] = {}
    for key, value in updates.items():
        if key not in OVERRIDABLE_FIELDS:
            continue
        cleaned[key] = value

    if "timezone" in cleaned and cleaned["timezone"] is not None:
        tz = str(cleaned["timezone"]).strip()
        try:
            ZoneInfo(tz)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Invalid timezone: {tz}") from exc
        cleaned["timezone"] = tz

    if "checkin_cron" in cleaned and cleaned["checkin_cron"] is not None:
        cron = str(cleaned["checkin_cron"]).strip()
        tz_name = str(cleaned.get("timezone") or "Asia/Shanghai")
        try:
            CronTrigger.from_crontab(cron, timezone=ZoneInfo(tz_name))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Invalid cron expression: {cron}") from exc
        cleaned["checkin_cron"] = cron

    if "pool_strategy" in cleaned and cleaned["pool_strategy"] is not None:
        strategy = str(cleaned["pool_strategy"]).strip().lower().replace("-", "_")
        if strategy not in POOL_STRATEGIES:
            raise ValueError("pool_strategy must be round_robin, first_available, or random")
        cleaned["pool_strategy"] = strategy

    if "credential_source" in cleaned and cleaned["credential_source"] is not None:
        try:
            cleaned["credential_source"] = normalize_credential_source(str(cleaned["credential_source"]))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    if "log_level" in cleaned and cleaned["log_level"] is not None:
        level = str(cleaned["log_level"]).strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        cleaned["log_level"] = level

    if "cooldown_seconds" in cleaned and cleaned["cooldown_seconds"] is not None:
        seconds = int(cleaned["cooldown_seconds"])
        if seconds < 0:
            raise ValueError("cooldown_seconds must be >= 0")
        cleaned["cooldown_seconds"] = seconds

    if "chat_concurrency_limit" in cleaned and cleaned["chat_concurrency_limit"] is not None:
        limit = cleaned["chat_concurrency_limit"]
        if type(limit) is not int or limit < 0:
            raise ValueError("chat_concurrency_limit must be >= 0")
        cleaned["chat_concurrency_limit"] = limit

    if "model_cache_ttl" in cleaned and cleaned["model_cache_ttl"] is not None:
        ttl = int(cleaned["model_cache_ttl"])
        if ttl < 0:
            raise ValueError("model_cache_ttl must be >= 0")
        cleaned["model_cache_ttl"] = ttl

    if "http_timeout" in cleaned and cleaned["http_timeout"] is not None:
        timeout = float(cleaned["http_timeout"])
        if timeout <= 0:
            raise ValueError("http_timeout must be > 0")
        cleaned["http_timeout"] = timeout

    if "http_stream_timeout" in cleaned and cleaned["http_stream_timeout"] is not None:
        timeout = float(cleaned["http_stream_timeout"])
        if timeout <= 0:
            raise ValueError("http_stream_timeout must be > 0")
        cleaned["http_stream_timeout"] = timeout

    if "oauth_callback_port" in cleaned and cleaned["oauth_callback_port"] is not None:
        port = int(cleaned["oauth_callback_port"])
        if port < 0 or port > 65535:
            raise ValueError("oauth_callback_port must be between 0 and 65535")
        cleaned["oauth_callback_port"] = port

    for url_key in ("snap_base_url", "oauth_portal_url", "oauth_sts_url"):
        if url_key in cleaned and cleaned[url_key] is not None:
            cleaned[url_key] = str(cleaned[url_key]).strip().rstrip("/")

    if "opengw_base_url" in cleaned:
        value = cleaned["opengw_base_url"]
        if value is None or str(value).strip() == "":
            cleaned["opengw_base_url"] = None
        else:
            cleaned["opengw_base_url"] = str(value).strip().rstrip("/")

    if "x_language" in cleaned:
        value = cleaned["x_language"]
        if value is None or str(value).strip() == "":
            cleaned["x_language"] = None
        else:
            cleaned["x_language"] = str(value).strip()

    for bool_key in ("checkin_run_on_startup", "scheduler_enabled", "checkin_claim_all"):
        if bool_key in cleaned and cleaned[bool_key] is not None:
            cleaned[bool_key] = bool(cleaned[bool_key])

    if "campaign_type" in cleaned and cleaned["campaign_type"] is not None:
        cleaned["campaign_type"] = str(cleaned["campaign_type"]).strip()

    if "oauth_client_id" in cleaned and cleaned["oauth_client_id"] is not None:
        cleaned["oauth_client_id"] = str(cleaned["oauth_client_id"]).strip()

    if "oauth_locale" in cleaned and cleaned["oauth_locale"] is not None:
        cleaned["oauth_locale"] = str(cleaned["oauth_locale"]).strip()

    if "api_key" in cleaned:
        value = cleaned["api_key"]
        if value is None or str(value).strip() == "":
            cleaned.pop("api_key")
        else:
            cleaned["api_key"] = str(value)

    return cleaned


def collect_redaction_secrets(settings: Settings, pool: AccountPool | None) -> list[str]:
    values = list(settings.secret_values_for_redaction())
    if pool is not None:
        values.extend(pool.all_secrets())
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for item in values:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def normalize_credential_source(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "env":
        normalized = "aksk"
    if normalized not in {"auto", "oauth", "aksk"}:
        raise ValueError("credential_source must be auto, oauth, or aksk")
    return normalized


def allowed_kinds(credential_source: str) -> set[str] | None:
    source = credential_source.strip().lower()
    if source == "env":
        source = "aksk"
    if source == "oauth":
        return {"oauth"}
    if source == "aksk":
        return {"aksk"}
    return None
