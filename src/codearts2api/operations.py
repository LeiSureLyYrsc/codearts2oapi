"""Multi-account check-in, benefit claim, and chat failover helpers."""

from __future__ import annotations

import logging
import math
from typing import Any, TYPE_CHECKING

import anyio

from codearts2api.checkin import (
    CheckinResult,
    account_type_label,
    extract_business_code,
    extract_package,
    is_credit_account,
    run_checkin,
)
from codearts2api.client import SnapAccessClient, UpstreamError, _error_message, chat_error_status
from codearts2api.credentials import CredentialResolver, ResolvedCredential
from codearts2api.pool import Account, AccountPool, is_failover_status, now_in_timezone

if TYPE_CHECKING:
    from codearts2api.chat_concurrency import ChatRequestTicket

logger = logging.getLogger(__name__)


def aggregate_checkin_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {
            "status": "error",
            "account_type": "unknown",
            "is_credit_package": None,
            "message": "No enabled accounts to check in",
            "actions": [],
        }
    if len(results) == 1:
        return dict(results[0]["result"])
    statuses = [item["result"].get("status") for item in results]
    unique = set(statuses)
    if unique == {"claimed"}:
        status = "claimed"
        message = f"Check-in completed for {len(results)} accounts"
    elif unique == {"already_claimed"}:
        status = "already_claimed"
        message = f"Already claimed today for {len(results)} accounts"
    elif unique <= {"claimed", "already_claimed"}:
        status = "claimed"
        message = "Check-in finished for all accounts (claimed or already claimed)"
    elif "error" in unique or "confirm_failed" in unique:
        status = "error"
        message = "One or more accounts failed check-in"
    else:
        status = "mixed"
        message = "Check-in finished with mixed per-account results"
    return {
        "status": status,
        "account_type": "pool",
        "is_credit_package": None,
        "message": message,
        "actions": [],
        "account_count": len(results),
    }


async def run_checkin_for_accounts(
    *,
    client: SnapAccessClient,
    resolver: CredentialResolver,
    pool: AccountPool,
    accounts: list[Account],
    campaign_type: str,
    all_claimable: bool,
    timezone_name: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    checked_in_at = now_in_timezone(timezone_name).isoformat()
    per_account: list[dict[str, Any]] = []
    for account in accounts:
        try:
            cred = resolver.resolve_account(account)
            result = await run_checkin(
                client.bind(cred),
                campaign_type=campaign_type,
                all_claimable=all_claimable,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Check-in failed for account id=%s name=%s", account.id, account.name)
            result = CheckinResult(
                status="error",
                account_type="unknown",
                is_credit_package=None,
                message=f"Check-in failed: {exc}",
                raw_error=type(exc).__name__,
            )
        payload = result.to_dict()
        pool.record_checkin(account.id, payload, checked_in_at)
        logger.info(
            "check-in finished account=%s name=%s status=%s account_type=%s",
            account.id,
            account.name,
            result.status,
            result.account_type,
        )
        per_account.append(
            {
                "account_id": account.id,
                "name": account.name,
                "result": payload,
                "checked_in_at": checked_in_at,
            }
        )
    overall = aggregate_checkin_results(per_account)
    return per_account, overall, checked_in_at


async def run_benefit_claim_for_accounts(
    *,
    client: SnapAccessClient,
    resolver: CredentialResolver,
    pool: AccountPool,
    accounts: list[Account],
    timezone_name: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    claimed_at = now_in_timezone(timezone_name).isoformat()
    per_account: list[dict[str, Any]] = []
    for account in accounts:
        try:
            cred = resolver.resolve_account(account)
            result = await client.claim_opengw_benefit(credential=cred)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Benefit claim failed for account id=%s name=%s", account.id, account.name)
            result = {
                "status": "error",
                "message": str(exc),
            }
        pool.record_benefit(account.id, result, claimed_at)
        logger.info(
            "benefit-claim finished account=%s name=%s status=%s",
            account.id,
            account.name,
            result.get("status"),
        )
        per_account.append(
            {
                "account_id": account.id,
                "name": account.name,
                "result": result,
                "claimed_at": claimed_at,
            }
        )
    if not per_account:
        overall: dict[str, Any] = {"status": "error", "message": "No enabled accounts"}
    elif len(per_account) == 1:
        overall = dict(per_account[0]["result"])
    else:
        statuses = {item["result"].get("status") for item in per_account}
        if statuses <= {"claimed"}:
            status = "claimed"
        elif "error" in statuses:
            status = "error"
        else:
            status = "mixed"
        overall = {"status": status, "account_count": len(per_account)}
    return per_account, overall, claimed_at


async def probe_account(
    client: SnapAccessClient,
    cred: ResolvedCredential,
) -> dict[str, Any]:
    """Probe an account with the official plugin statistics path."""
    try:
        payload = await client.get_plugin_statistics(credential=cred)
    except UpstreamError as exc:
        logger.info("account probe failed account=%s status=%s", cred.account_id, exc.status_code)
        return {"ok": False, "http_status": exc.status_code, "message": str(exc), "quota": None}
    except Exception as exc:  # noqa: BLE001
        logger.exception("account probe failed account=%s", cred.account_id)
        return {"ok": False, "message": str(exc), "quota": None}
    quota = extract_quota(payload)
    if extract_package(payload) is None and quota is None:
        code = _safe_business_code(payload)
        if code is not None:
            # HTTP 200 with a business error and no package/metrics is not a
            # token account; do not report the account as credit-ineligible.
            logger.info("account probe business error account=%s code=%s", cred.account_id, code)
            return {
                "ok": False,
                "http_status": 200,
                "account_type": "unknown",
                "is_credit_package": None,
                "message": f"Probe failed: upstream business error (code={code})",
                "quota": None,
            }
    return {
        "ok": True,
        "http_status": 200,
        "account_type": account_type_label(payload),
        "is_credit_package": is_credit_account(payload),
        "message": "Probe succeeded",
        "quota": quota,
    }


# Metric names and fields from GET /snap-manager/v1/statistics/plugin in the
# shipped CodeArts Desktop 0.2.9 bundle.
QUOTA_SPECS: dict[str, dict[str, Any]] = {
    "credit": {
        "metric": "usageTotalPackageCredit",
        "fields": {
            "total": "package_credit_amount",
            "used": "package_credit_used",
            "remaining": "package_credit_remain",
            "expiring": "package_credit_expiring_amount",
        },
    },
    "token": {
        "metric": "usageTokenChatMessages",
        "fields": {"total": "package_token_amount", "used": "usage_token_num"},
        "derive_remaining": True,
    },
}


def extract_quota(payload: Any) -> dict[str, Any] | None:
    """Quota from GET /snap-manager/v1/statistics/plugin (Desktop 0.2.9 fields).

    Credit packages report explicit ``package_credit_*`` values, returned as-is
    and never derived. Token packages report ``package_token_amount`` and
    ``usage_token_num``; remaining is their difference only when both counters
    are known and consistent. Missing, unparseable, negative, or ``-1``
    (unlimited) values stay ``None`` instead of becoming 0, and a record with
    no usable value yields no quota at all.
    """
    metrics = _metrics(payload)
    quotas = {unit: _quota(metrics, unit, spec) for unit, spec in QUOTA_SPECS.items()}
    package = extract_package(payload)
    kind = package.get("is_credit_package") if isinstance(package, dict) else None
    if kind is True:
        return quotas["credit"]
    if kind is False:
        return quotas["token"]
    return quotas["token"] or quotas["credit"]


def _metrics(payload: Any) -> list[dict[str, Any]]:
    """Return metrics[] from the body or its data/result wrapper."""
    body: dict[str, Any] = payload if isinstance(payload, dict) else {}
    for candidate in (body, body.get("data"), body.get("result")):
        records = candidate.get("metrics") if isinstance(candidate, dict) else None
        if isinstance(records, list):
            return [record for record in records if isinstance(record, dict)]
    return []


def _quota(metrics: list[dict[str, Any]], unit: str, spec: dict[str, Any]) -> dict[str, Any] | None:
    record = next((item for item in metrics if str(item.get("name") or "").strip() == spec["metric"]), None)
    if record is None:
        return None
    fields = spec["fields"]
    values = {
        key: _number(record.get(fields[key])) if key in fields else None
        for key in ("total", "used", "remaining", "expiring")
    }
    total, used = values["total"], values["used"]
    if (
        spec.get("derive_remaining")
        and values["remaining"] is None
        and total is not None
        and used is not None
        and total >= used
    ):
        values["remaining"] = total - used
    if all(value is None for value in values.values()):
        return None
    return {"unit": unit, **values}


def _number(value: Any) -> int | float | None:
    """Return a non-negative finite number, else None; reject bools and sentinels."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            # int() keeps large counters exact; float() accepts decimals.
            # Both can reject text that looks numeric ("²" is isdigit, and
            # int() refuses strings above Python's digit limit).
            value = int(text) if text.lstrip("-").isdigit() else float(text)
        except ValueError:
            return None
    if isinstance(value, int):  # Keep large counters exact; never round-trip via float.
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return int(value) if value.is_integer() else value
    return None


def _safe_business_code(payload: Any) -> str | None:
    """Return a short sanitized upstream business code, or None."""
    code = extract_business_code(payload)
    if code is None:
        return None
    return "".join(ch for ch in code if ch.isalnum() or ch in "_.-")[:32] or None


async def post_chat_with_failover(
    *,
    client: SnapAccessClient,
    resolver: CredentialResolver,
    pool: AccountPool,
    payload: dict[str, Any],
    session_id: str | None = None,
    ticket: ChatRequestTicket | None = None,
) -> Any:
    last_error: UpstreamError | None = None
    busy = False
    for account in pool.iter_candidates():
        if ticket is not None and not ticket.bind(account.id, account.chat_concurrency_limit):
            busy = True
            continue
        try:
            cred = resolver.resolve_account(account)
        except RuntimeError as exc:
            if ticket is not None:
                ticket.unbind()
            last_error = UpstreamError(str(exc), 503)
            continue
        try:
            # Keep older test doubles / integrations compatible when no affinity
            # value was supplied.
            if session_id is None:
                raw = await client.post_chat(payload, credential=cred)
            else:
                raw = await client.post_chat(payload, credential=cred, session_id=session_id)
            pool.clear_cooldown(account.id)
            return raw
        except UpstreamError as exc:
            last_error = exc
            if is_failover_status(exc.status_code) and not _is_session_limit_error(exc):
                pool.mark_cooldown(account.id, exc.status_code)
                logger.info(
                    "chat failover from account=%s name=%s status=%s",
                    account.id,
                    account.name,
                    exc.status_code,
                )
                if ticket is not None:
                    ticket.unbind()
                continue
            raise
    if last_error is not None:
        raise last_error
    if busy:
        raise UpstreamError("Eligible accounts have reached their chat request concurrency limit", 429)
    raise UpstreamError("No healthy accounts available in the pool", 503)


async def open_chat_stream_with_failover(
    *,
    client: SnapAccessClient,
    resolver: CredentialResolver,
    pool: AccountPool,
    payload: dict[str, Any],
    session_id: str | None = None,
    ticket: ChatRequestTicket | None = None,
):
    """Return (stream_cm, response) for the first account that accepts the stream."""
    last_error: UpstreamError | None = None
    busy = False
    for account in pool.iter_candidates():
        if ticket is not None and not ticket.bind(account.id, account.chat_concurrency_limit):
            busy = True
            continue
        try:
            cred = resolver.resolve_account(account)
        except RuntimeError as exc:
            if ticket is not None:
                ticket.unbind()
            last_error = UpstreamError(str(exc), 503)
            continue
        if session_id is None:
            stream_cm = client.stream_chat(payload, credential=cred)
        else:
            stream_cm = client.stream_chat(payload, credential=cred, session_id=session_id)
        try:
            response = await stream_cm.__aenter__()
        except UpstreamError as exc:
            await _close_cm(stream_cm, exc)
            last_error = exc
            if is_failover_status(exc.status_code) and not _is_session_limit_error(exc):
                pool.mark_cooldown(account.id, exc.status_code)
                logger.info(
                    "chat stream failover from account=%s name=%s status=%s",
                    account.id,
                    account.name,
                    exc.status_code,
                )
                if ticket is not None:
                    ticket.unbind()
                continue
            raise
        except Exception as exc:  # noqa: BLE001
            await _close_cm(stream_cm, exc)
            raise
        if not 200 <= response.status_code < 300:
            try:
                raw = await response.aread()
                parsed = _parse_stream_error(raw, response.headers.get("content-type", ""))
                message = _error_message(parsed, response.status_code)
            finally:
                await _close_cm(stream_cm)
            err = UpstreamError(message, response.status_code, parsed)
            last_error = err
            if not _is_session_limit_error(err) and is_failover_status(err.status_code):
                pool.mark_cooldown(account.id, response.status_code)
                logger.info(
                    "chat stream failover from account=%s name=%s status=%s",
                    account.id,
                    account.name,
                    err.status_code,
                )
                if ticket is not None:
                    ticket.unbind()
                continue
            raise err
        pool.clear_cooldown(account.id)
        return stream_cm, response
    if last_error is not None:
        raise last_error
    if busy:
        raise UpstreamError("Eligible accounts have reached their chat request concurrency limit", 429)
    raise UpstreamError("No healthy accounts available in the pool", 503)


def _parse_stream_error(raw: bytes, content_type: str) -> Any:
    """Parse JSON or a compact SSE JSON error; never expose arbitrary response text."""
    import json

    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    if "event-stream" in content_type.lower() or text.startswith(("data:", "event:")):
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data and data != "[DONE]":
                try:
                    value = json.loads(data)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    return value
    return None


def _is_session_limit_error(error: UpstreamError) -> bool:
    """Avoid moving a capped conversation onto an unrelated pooled account."""
    message = str(error)
    return "并发会话数" in message and "上限" in message


async def list_models_with_failover(
    *,
    client: SnapAccessClient,
    resolver: CredentialResolver,
    pool: AccountPool,
) -> Any:
    last_error: UpstreamError | None = None
    for account in pool.iter_candidates():
        try:
            cred = resolver.resolve_account(account)
        except RuntimeError as exc:
            last_error = UpstreamError(str(exc), 503)
            continue
        try:
            payload = await client.list_builtin_models(credential=cred)
            pool.clear_cooldown(account.id)
            return payload
        except UpstreamError as exc:
            last_error = exc
            if is_failover_status(exc.status_code):
                pool.mark_cooldown(account.id, exc.status_code)
                logger.info(
                    "models failover from account=%s name=%s status=%s",
                    account.id,
                    account.name,
                    exc.status_code,
                )
                continue
            raise
    if last_error is not None:
        raise last_error
    raise UpstreamError("No healthy accounts available in the pool", 503)


async def _close_cm(stream_cm: Any, exc: BaseException | None = None) -> None:
    try:
        with anyio.CancelScope(shield=True):
            if exc is None:
                await stream_cm.__aexit__(None, None, None)
            else:
                await stream_cm.__aexit__(type(exc), exc, exc.__traceback__)
    except Exception:  # noqa: BLE001
        logger.debug("Failed to close upstream stream after failover", exc_info=True)
