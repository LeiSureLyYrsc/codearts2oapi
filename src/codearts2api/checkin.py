"""Official daily check-in (DAILY_CLAIM) state machine and orchestrator.

Official activity 「每日签到得积分」 uses campaign type DAILY_CLAIM and is
reported as running 2026-10-27 20:00 through 2026-12-31 23:59:59
Asia/Shanghai. Amounts depend on the plan (experience 100, standard 150,
premium/flagship 200) and are valid for that day only.

The public API catalog does not document these endpoints. They go through
the official-client snap-access gateway:

- GET  /snap-manager/v1/statistics/plugin   (account / package)
- GET  /v1/ops/delivery?channel=DESKTOP
- POST /v1/ops/claim   {"campaignId": <id>, "idempotentKey": <key>, "channel": "DESKTOP"}
- POST /v1/ops/confirm {"userBenefitId": <claim record id>}

Daily check-in is only for credit-billing accounts
(``package.is_credit_package is True``). Token-billing accounts are skipped.

After claim, if the response ``id`` is not null, confirm MUST be called or
credits can stay pending.

Business codes reported by the community:

- 40001 = already claimed today (treated as success)
- 40401 = activity missing
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from codearts2api.client import CHECKIN_CHANNEL, UpstreamError

logger = logging.getLogger(__name__)

DAILY_CAMPAIGN_TYPE = "DAILY_CLAIM"
ALREADY_CLAIMED_CODES = frozenset({"40001", "E40001"})
ACTIVITY_MISSING_CODES = frozenset({"40401", "E40401"})

CLAIMED_STATUSES = frozenset(
    {
        "CLAIMED",
        "CONFIRMED",
        "CONSUMED",
        "ALREADY_CLAIMED",
        "RECEIVED",
        "DONE",
        "SUCCESS",
        "FINISHED",
        "CLAIMED_TODAY",
        "COMPLETED",
    }
)
CLAIMABLE_STATUSES = frozenset(
    {
        "CLAIMABLE",
        "ELIGIBLE",
        "AVAILABLE",
        "INIT",
        "UNCLAIMED",
        "PENDING",
        "READY",
        "OPEN",
    }
)


class CheckinTransport(Protocol):
    async def get_plugin_statistics(self) -> Any: ...

    async def list_activities(self, channel: str = CHECKIN_CHANNEL) -> Any: ...

    async def claim_campaign(self, campaign_id: Any, channel: str = CHECKIN_CHANNEL) -> tuple[Any, int]: ...

    async def confirm_campaign(self, user_benefit_id: Any) -> tuple[Any, int]: ...


@dataclass(slots=True)
class CampaignAction:
    campaign_id: Any
    campaign_type: str | None
    name: str | None
    status: str
    message: str
    needs_confirm: bool = False
    confirmed: bool = False
    business_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CheckinResult:
    status: str
    account_type: str
    is_credit_package: bool | None
    message: str
    actions: list[CampaignAction] = field(default_factory=list)
    raw_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "account_type": self.account_type,
            "is_credit_package": self.is_credit_package,
            "message": self.message,
            "actions": [
                {
                    "campaign_id": action.campaign_id,
                    "campaign_type": action.campaign_type,
                    "name": action.name,
                    "status": action.status,
                    "message": action.message,
                    "needs_confirm": action.needs_confirm,
                    "confirmed": action.confirmed,
                    "business_code": action.business_code,
                }
                for action in self.actions
            ],
        }


def extract_package(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("package"), dict):
        return payload["package"]
    for wrapper in ("data", "result"):
        inner = payload.get(wrapper)
        if isinstance(inner, dict):
            if isinstance(inner.get("package"), dict):
                return inner["package"]
            if "is_credit_package" in inner:
                return inner
    if "is_credit_package" in payload:
        return payload
    return None


def is_credit_account(payload: Any) -> bool:
    package = extract_package(payload)
    if package is None:
        return False
    return package.get("is_credit_package") is True


def account_type_label(payload: Any) -> str:
    package = extract_package(payload)
    if package is None:
        return "unknown"
    if package.get("is_credit_package") is True:
        return "credit"
    return "token"


def extract_campaigns(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "result", "campaigns", "list", "records", "content", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = extract_campaigns(value)
            if nested:
                return nested
    return []


def campaign_type_of(campaign: dict[str, Any]) -> str | None:
    for key in ("type", "campaignType", "campaign_type"):
        value = campaign.get(key)
        if value is not None and str(value) != "":
            return str(value)
    return None


def campaign_id_of(campaign: dict[str, Any]) -> Any:
    for key in ("campaignId", "campaign_id"):
        if campaign.get(key) is not None:
            return campaign[key]
    # ``id`` is a last resort; some payloads use it for the campaign id.
    if campaign.get("id") is not None:
        return campaign["id"]
    return None


def campaign_name_of(campaign: dict[str, Any]) -> str | None:
    for key in ("name", "title", "campaignName", "campaign_name"):
        value = campaign.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def is_already_claimed(campaign: dict[str, Any]) -> bool:
    if campaign.get("claimed") is True or campaign.get("already_claimed") is True:
        return True
    status = str(campaign.get("status") or "").upper()
    return status in CLAIMED_STATUSES


def is_claimable(campaign: dict[str, Any]) -> bool:
    if is_already_claimed(campaign):
        return False
    if "claimable" in campaign:
        return bool(campaign["claimable"])
    status = str(campaign.get("status") or "").upper()
    if status in CLAIMABLE_STATUSES:
        return True
    if "claimable" not in campaign and not status:
        # Official client pre-checks claimable/status. If both are absent we
        # still allow an attempt so a daily campaign is not silently dropped.
        return True
    return False


def select_campaigns(
    campaigns: list[dict[str, Any]],
    *,
    campaign_type: str = DAILY_CAMPAIGN_TYPE,
    all_claimable: bool = False,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    requested_type = campaign_type.strip().upper()
    if requested_type == "USER_LOGIN":
        requested_type = DAILY_CAMPAIGN_TYPE  # previous default, not the separate desktop login activity
    for campaign in campaigns:
        cid = campaign_id_of(campaign)
        key = str(cid) if cid is not None else repr(campaign)
        if key in seen:
            continue
        typ = campaign_type_of(campaign)
        if all_claimable:
            if not is_claimable(campaign) and not is_already_claimed(campaign):
                continue
        else:
            if not typ or typ.upper() != requested_type:
                continue
        seen.add(key)
        selected.append(campaign)
    return selected


def extract_business_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in (
        "error_code",
        "errorCode",
        "business_code",
        "businessCode",
        "result_code",
        "resultCode",
        "code",
    ):
        value = payload.get(key)
        if value is None or value == "":
            continue
        text = str(value)
        # Ignore generic success markers so they are not confused with 40001/40401.
        if text in {"0", "00", "0000", "200", "OK", "ok", "success", "SUCCESS"}:
            continue
        return text
    for wrapper in ("error", "data", "result"):
        inner = payload.get(wrapper)
        if isinstance(inner, dict):
            nested = extract_business_code(inner)
            if nested:
                return nested
    return None


def extract_claim_record_id(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return None
    for body in (payload.get("data"), payload.get("result"), payload):
        if not isinstance(body, dict):
            continue
        for key in ("id", "userBenefitId"):
            record_id = body.get(key)
            if (type(record_id) is int and record_id > 0) or (isinstance(record_id, str) and record_id.strip()):
                return record_id
    return None


def _business_error(payload: Any, code: str) -> str:
    if isinstance(payload, dict):
        for key in ("message", "error_msg", "errorMsg", "msg"):
            message = payload.get(key)
            if isinstance(message, str) and message:
                return message
    return f"upstream business code {code}"


@dataclass(slots=True)
class ClaimInterpretation:
    status: str
    needs_confirm: bool
    business_code: str | None = None
    claim_record_id: Any = None
    message: str = ""


def interpret_claim_response(payload: Any, http_status: int | None = None) -> ClaimInterpretation:
    code = extract_business_code(payload)
    if code in ALREADY_CLAIMED_CODES:
        return ClaimInterpretation(
            status="already_claimed",
            needs_confirm=False,
            business_code=code,
            message="Already claimed today (business code 40001)",
        )
    if code in ACTIVITY_MISSING_CODES:
        return ClaimInterpretation(
            status="activity_missing",
            needs_confirm=False,
            business_code=code,
            message="Activity missing (business code 40401)",
        )

    record_id = extract_claim_record_id(payload)
    if http_status is not None and http_status >= 400:
        return ClaimInterpretation(
            status="error",
            needs_confirm=False,
            business_code=code,
            claim_record_id=record_id,
            message=f"Claim failed with HTTP {http_status}",
        )
    if code is not None:
        return ClaimInterpretation(
            status="error", needs_confirm=False, business_code=code,
            message=f"Claim failed: {_business_error(payload, code)}",
        )
    if not isinstance(payload, dict) and http_status != 204:
        return ClaimInterpretation(status="error", needs_confirm=False, message="Unexpected claim response")
    if record_id is not None:
        return ClaimInterpretation(
            status="claimed",
            needs_confirm=True,
            business_code=code,
            claim_record_id=record_id,
            message="Claim succeeded; confirm is required because id is not null",
        )
    return ClaimInterpretation(
        status="claimed",
        needs_confirm=False,
        business_code=code,
        claim_record_id=None,
        message="Claim succeeded; confirm skipped because id is null",
    )


def _http_status_from_exc(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    return None


async def run_checkin(
    transport: CheckinTransport,
    *,
    campaign_type: str = DAILY_CAMPAIGN_TYPE,
    all_claimable: bool = False,
) -> CheckinResult:
    """Run the desktop daily claim flow, skipping already claimed campaigns."""
    try:
        plugin = await transport.get_plugin_statistics()
        package_code = extract_business_code(plugin) if extract_package(plugin) is None else None
        if package_code is not None:
            raise UpstreamError(_business_error(plugin, package_code), 200)
    except Exception as exc:  # noqa: BLE001 — surface any transport failure
        logger.exception("Failed to load plugin statistics")
        return CheckinResult(
            status="error",
            account_type="unknown",
            is_credit_package=None,
            message=f"Failed to query account package: {exc}",
            raw_error=type(exc).__name__,
        )

    credit = is_credit_account(plugin)
    label = account_type_label(plugin)
    if not credit:
        return CheckinResult(
            status="ineligible",
            account_type=label,
            is_credit_package=False,
            message=(
                "Official daily check-in is only for credit-billing accounts "
                "(package.is_credit_package === true). Token-billing accounts are skipped."
            ),
        )

    try:
        delivery = await transport.list_activities(channel=CHECKIN_CHANNEL)
        delivery_code = extract_business_code(delivery)
        if delivery_code is not None:
            raise UpstreamError(_business_error(delivery, delivery_code), 200)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to list check-in activities")
        return CheckinResult(
            status="error",
            account_type=label,
            is_credit_package=True,
            message=f"Failed to list activities: {exc}",
            raw_error=type(exc).__name__,
        )

    campaigns = select_campaigns(
        extract_campaigns(delivery),
        campaign_type=campaign_type,
        all_claimable=all_claimable,
    )
    if not campaigns:
        return CheckinResult(
            status="no_campaign",
            account_type=label,
            is_credit_package=True,
            message=(
                f"No matching campaign found (type={campaign_type}, "
                f"all_claimable={all_claimable})"
            ),
        )

    actions: list[CampaignAction] = []
    for campaign in campaigns:
        actions.append(
            await _process_campaign(transport, campaign)
        )

    return _summarize(label, actions)


async def _process_campaign(
    transport: CheckinTransport,
    campaign: dict[str, Any],
) -> CampaignAction:
    cid = campaign_id_of(campaign)
    typ = campaign_type_of(campaign)
    name = campaign_name_of(campaign)
    if cid is None:
        return CampaignAction(
            campaign_id=None,
            campaign_type=typ,
            name=name,
            status="error",
            message="Campaign is missing an id / campaignId",
        )

    if is_already_claimed(campaign):
        return CampaignAction(
            campaign_id=cid,
            campaign_type=typ,
            name=name,
            status="already_claimed",
            message="Campaign already claimed; skipping claim request",
        )

    if not is_claimable(campaign):
        return CampaignAction(
            campaign_id=cid,
            campaign_type=typ,
            name=name,
            status="skipped",
            message=f"Campaign is not claimable (status={campaign.get('status')!r})",
            details={"status": campaign.get("status"), "claimable": campaign.get("claimable")},
        )

    try:
        payload, http_status = await transport.claim_campaign(cid, channel=CHECKIN_CHANNEL)
    except Exception as exc:  # noqa: BLE001
        # Some gateways raise on 4xx even when the body carries a business code.
        body = _exc_payload(exc)
        http_status = _http_status_from_exc(exc)
        if body is not None:
            interpretation = interpret_claim_response(body, http_status)
            if interpretation.status in {"already_claimed", "activity_missing"}:
                return CampaignAction(
                    campaign_id=cid,
                    campaign_type=typ,
                    name=name,
                    status=interpretation.status,
                    message=interpretation.message,
                    business_code=interpretation.business_code,
                )
        logger.exception("Claim request failed for campaign %s", cid)
        return CampaignAction(
            campaign_id=cid,
            campaign_type=typ,
            name=name,
            status="error",
            message=f"Claim request failed: {exc}",
        )

    interpretation = interpret_claim_response(payload, http_status)
    action = CampaignAction(
        campaign_id=cid,
        campaign_type=typ,
        name=name,
        status=interpretation.status,
        message=interpretation.message,
        needs_confirm=interpretation.needs_confirm,
        business_code=interpretation.business_code,
    )
    if not interpretation.needs_confirm:
        return action

    try:
        confirm_payload, confirm_status = await transport.confirm_campaign(interpretation.claim_record_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Confirm request failed for campaign %s", cid)
        action.status = "confirm_failed"
        action.message = (
            "Claim succeeded but confirm failed; credits may stay pending. "
            f"Error: {exc}"
        )
        return action

    if confirm_status >= 400:
        action.status = "confirm_failed"
        action.message = (
            "Claim succeeded but confirm returned HTTP "
            f"{confirm_status}; credits may stay pending."
        )
        action.details["confirm_payload"] = (
            confirm_payload if isinstance(confirm_payload, dict) else {"raw": str(confirm_payload)}
        )
        return action

    confirm_code = extract_business_code(confirm_payload)
    if confirm_code is not None or (not isinstance(confirm_payload, dict) and confirm_status != 204):
        action.status = "confirm_failed"
        action.business_code = confirm_code
        reason = _business_error(confirm_payload, confirm_code) if confirm_code is not None else "unexpected response"
        action.message = f"Claim succeeded but confirm failed: {reason}; credits may stay pending."
        return action

    action.confirmed = True
    action.status = "claimed"
    action.message = "Claimed and confirmed"
    return action


def _exc_payload(exc: BaseException) -> Any | None:
    if isinstance(exc, UpstreamError):
        return exc.payload
    response = getattr(exc, "response", None)
    if response is None:
        return None
    for attr in ("_cached_json",):
        value = getattr(response, attr, None)
        if isinstance(value, dict):
            return value
    reader = getattr(response, "json", None)
    if callable(reader):
        try:
            value = reader()
        except Exception:  # noqa: BLE001
            return None
        return value
    return None


def _summarize(account_type: str, actions: list[CampaignAction]) -> CheckinResult:
    statuses = {action.status for action in actions}
    if statuses == {"already_claimed"} or (
        "already_claimed" in statuses and statuses <= {"already_claimed", "skipped"}
    ):
        overall = "already_claimed"
        message = "Already claimed today"
    elif "confirm_failed" in statuses:
        overall = "confirm_failed"
        message = "Claim succeeded but confirm failed; credits may stay pending"
    elif "claimed" in statuses:
        overall = "claimed"
        message = "Check-in completed"
    elif statuses == {"activity_missing"}:
        overall = "activity_missing"
        message = "Activity missing"
    elif statuses <= {"skipped"}:
        overall = "no_campaign"
        message = "Matching campaigns were not claimable"
    elif "error" in statuses and "claimed" not in statuses:
        overall = "error"
        message = "One or more claim attempts failed"
    else:
        overall = next(iter(statuses))
        message = "Check-in finished with mixed results"

    return CheckinResult(
        status=overall,
        account_type=account_type,
        is_credit_package=True,
        message=message,
        actions=actions,
    )
