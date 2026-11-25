"""Private, atomic JSON storage and portable account-auth validation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from codearts2api.oauth.session import OAuthSession, SessionStore


class AuthAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    name: StrictStr = Field(min_length=1, max_length=256)
    kind: Literal["aksk", "oauth"]
    enabled: StrictBool = True
    note: StrictStr = ""
    ak: StrictStr | None = None
    sk: StrictStr | None = None
    security_token: StrictStr | None = None
    project_id: StrictStr | None = None
    chat_concurrency_limit: StrictInt = Field(default=0, ge=0)
    session_file: StrictStr | None = None
    created_at: StrictStr | None = None
    updated_at: StrictStr | None = None
    last_checkin: dict[str, Any] | None = None
    last_checkin_at: StrictStr | None = None
    last_error: StrictStr | None = None
    last_benefit: dict[str, Any] | None = None
    last_benefit_at: StrictStr | None = None


def account_filename(account_id: str) -> str:
    # UUIDs and ordinary IDs remain readable; arbitrary legacy IDs are hashed.
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", account_id) and account_id.upper() not in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))
    }:
        return account_id + ".json"
    return "id-" + hashlib.sha256(account_id.encode()).hexdigest() + ".json"


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".auth-", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.chmod(tmp, 0o600)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


def validate_auth_file(payload: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not isinstance(payload, dict) or set(payload) - {"version", "account", "oauth_session"}:
        raise ValueError("Expected an account auth file (version, account, optional oauth_session)")
    if type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("Unsupported auth file version")
    try:
        account = AuthAccount.model_validate(payload.get("account")).model_dump()
    except ValueError:
        # Never echo validation input: it contains credentials.
        raise ValueError("Invalid account fields or types") from None
    if not account["name"].strip() or not account["id"].strip():
        raise ValueError("Account id and name must not be blank")
    account["name"] = account["name"].strip()
    if account["kind"] == "aksk" and not all(
        account[key] and account[key].strip() for key in ("ak", "sk")
    ):
        raise ValueError("AK/SK accounts require non-empty ak and sk")
    session_file = account["session_file"]
    if session_file and (not re.fullmatch(r"[A-Za-z0-9_-]+\.json", session_file)):
        raise ValueError("Unsafe OAuth session filename")
    session = payload.get("oauth_session")
    if session is not None:
        if account["kind"] != "oauth" or not isinstance(session, dict):
            raise ValueError("oauth_session is only valid for OAuth accounts")
        if not all(isinstance(session.get(k), str) and session[k].strip()
                   for k in ("access_key_id", "secret_access_key")):
            raise ValueError("OAuth session requires access_key_id and secret_access_key")
        try:
            session = OAuthSession.from_dict(session).to_dict()
        except Exception:
            raise ValueError("Invalid OAuth session") from None
    return account, session


class AccountSessionStore(SessionStore):
    """Expose the existing OAuth store interface over one account auth file."""

    def __init__(self, path: Path, account: Any, lock: Any) -> None:
        super().__init__(path.parent, path.name)
        self.account = account
        self.lock = lock

    def _payload(self) -> dict[str, Any]:
        if self.path.is_file():
            return json.loads(self.path.read_text(encoding="utf-8-sig"))
        return {"version": 1, "account": self.account.to_storage()}

    def load(self) -> OAuthSession | None:
        with self.lock:
            raw = self._payload().get("oauth_session")
            if raw is None:
                return None
            return OAuthSession.from_dict(raw)

    def save(self, session: OAuthSession) -> Path:
        with self.lock:
            payload = self._payload()
            payload["account"] = self.account.to_storage()
            payload["oauth_session"] = session.to_dict()
            write_private_json(self.path, payload)
            return self.path

    def clear(self) -> None:
        with self.lock:
            if self.path.is_file():
                payload = self._payload()
                payload.pop("oauth_session", None)
                write_private_json(self.path, payload)
