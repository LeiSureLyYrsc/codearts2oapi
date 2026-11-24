"""Check-in state machine: ineligible / already claimed / claim+confirm / claim without confirm."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from codearts2api.checkin import (
    account_type_label,
    extract_campaigns,
    interpret_claim_response,
    is_already_claimed,
    is_claimable,
    is_credit_account,
    run_checkin,
    select_campaigns,
)
from codearts2api.client import SnapAccessClient
from codearts2api.credentials import ResolvedCredential


class FakeTransport:
    def __init__(
        self,
        plugin: Any,
        activities: Any,
        claim_result: tuple[Any, int] | Exception = ({"id": None}, 200),
        confirm_result: tuple[Any, int] | Exception = ({}, 200),
    ) -> None:
        self.plugin = plugin
        self.activities = activities
        self.claim_result = claim_result
        self.confirm_result = confirm_result
        self.claim_calls: list[Any] = []
        self.confirm_calls: list[Any] = []

    async def get_plugin_statistics(self) -> Any:
        return self.plugin

    async def list_activities(self, channel: str = "DESKTOP") -> Any:
        assert channel == "DESKTOP"
        return self.activities

    async def claim_campaign(self, campaign_id: Any, channel: str = "DESKTOP") -> tuple[Any, int]:
        self.claim_calls.append((campaign_id, channel))
        if isinstance(self.claim_result, Exception):
            raise self.claim_result
        return self.claim_result

    async def confirm_campaign(self, user_benefit_id: Any) -> tuple[Any, int]:
        self.confirm_calls.append(user_benefit_id)
        if isinstance(self.confirm_result, Exception):
            raise self.confirm_result
        return self.confirm_result


CREDIT = {"package": {"is_credit_package": True, "plan": "standard"}}
TOKEN = {"package": {"is_credit_package": False}}
DAILY = {
    "campaignId": "camp-login",
    "type": "DAILY_CLAIM",
    "name": "每日签到得积分",
    "claimable": True,
    "status": "CLAIMABLE",
}


def test_credit_account_detection() -> None:
    assert is_credit_account(CREDIT) is True
    assert account_type_label(CREDIT) == "credit"
    assert is_credit_account(TOKEN) is False
    assert account_type_label(TOKEN) == "token"
    assert is_credit_account({"data": {"package": {"is_credit_package": True}}}) is True
    assert is_credit_account({}) is False
    assert is_credit_account({"package": {"is_credit_package": "true"}}) is False


def test_select_campaigns_defaults_to_daily_claim() -> None:
    campaigns = extract_campaigns(
        {
            "data": [
                DAILY,
                {"campaignId": "login-event", "type": "USER_LOGIN", "claimable": True},
                {"campaignId": "other", "type": "OTHER", "claimable": True},
            ]
        }
    )
    assert select_campaigns(campaigns) == [DAILY]
    assert select_campaigns(campaigns, campaign_type="USER_LOGIN") == [DAILY]
    assert select_campaigns([{**DAILY, "type": " daily_claim "}], campaign_type=" user_login ")

    all_claimable = select_campaigns(campaigns, campaign_type="USER_LOGIN", all_claimable=True)
    assert {c["campaignId"] for c in all_claimable} == {"camp-login", "login-event", "other"}


@pytest.mark.parametrize(
    ("delivery", "status", "expected_message"),
    [
        ({"code": 0, "data": {"items": []}}, "no_campaign", "empty activity list (looking for DAILY_CLAIM)"),
        (
            {"code": 0, "data": {"items": [{"campaignId": "not-for-output", "type": "USER_LOGIN"}]}},
            "no_campaign",
            "No matching campaign for DAILY_CLAIM in 1 upstream activities; USER_LOGIN is a separate activity",
        ),
        ({"code": 0, "data": {"items": {}}}, "error", "Unexpected activity response format"),
        ({"code": 0, "data": {"items": [None]}}, "error", "Unexpected activity response format"),
    ],
)
async def test_unmatched_delivery_is_diagnostic_without_claiming(delivery, status, expected_message):
    transport = FakeTransport(plugin=CREDIT, activities=delivery)
    result = await run_checkin(transport, campaign_type="USER_LOGIN")
    assert result.status == status
    assert expected_message in result.message
    assert "not-for-output" not in result.message
    assert result.actions == transport.claim_calls == transport.confirm_calls == []


def test_claimable_and_already_claimed_flags() -> None:
    assert is_already_claimed({"claimed": True}) is True
    assert is_already_claimed({"status": "CLAIMED", "claimable": False}) is True
    assert is_claimable({"claimable": True}) is True
    assert is_claimable({"status": "CLAIMED"}) is False
    assert is_claimable({"status": "CONFIRMED", "claimable": True}) is False
    assert is_claimable({"status": "CONSUMED"}) is False
    assert is_claimable({"status": "ELIGIBLE"}) is True
    assert is_claimable({"claimable": False, "status": "EXPIRED"}) is False
    assert is_already_claimed({"claimable": True, "status": "CLAIMABLE"}) is False


@pytest.mark.asyncio
async def test_ineligible_token_account_skips_claim() -> None:
    transport = FakeTransport(plugin=TOKEN, activities={"data": [DAILY]})
    result = await run_checkin(transport)
    assert result.status == "ineligible"
    assert result.is_credit_package is False
    assert result.account_type == "token"
    assert transport.claim_calls == []
    assert transport.confirm_calls == []


@pytest.mark.asyncio
async def test_already_claimed_from_campaign_status() -> None:
    campaign = {**DAILY, "claimable": False, "status": "CLAIMED"}
    transport = FakeTransport(plugin=CREDIT, activities={"data": [campaign]})
    result = await run_checkin(transport)
    assert result.status == "already_claimed"
    assert result.actions[0].status == "already_claimed"
    assert transport.claim_calls == []


@pytest.mark.asyncio
async def test_already_claimed_business_code_40001() -> None:
    transport = FakeTransport(
        plugin=CREDIT,
        activities={"data": [DAILY]},
        claim_result=({"code": "40001", "message": "already claimed"}, 200),
    )
    result = await run_checkin(transport)
    assert result.status == "already_claimed"
    assert result.actions[0].business_code == "40001"
    assert transport.confirm_calls == []


@pytest.mark.asyncio
async def test_activity_missing_40401() -> None:
    transport = FakeTransport(
        plugin=CREDIT,
        activities={"data": [DAILY]},
        claim_result=({"error_code": "40401"}, 404),
    )
    result = await run_checkin(transport)
    assert result.status == "activity_missing"
    assert result.actions[0].status == "activity_missing"
    assert transport.confirm_calls == []


@pytest.mark.asyncio
async def test_claim_and_confirm_when_id_is_not_null() -> None:
    transport = FakeTransport(
        plugin=CREDIT,
        activities={"data": [DAILY]},
        claim_result=({"id": "record-1", "campaignId": "camp-login"}, 200),
        confirm_result=({"ok": True}, 200),
    )
    result = await run_checkin(transport)
    assert result.status == "claimed"
    assert transport.claim_calls == [("camp-login", "DESKTOP")]
    assert transport.confirm_calls == ["record-1"]
    assert result.actions[0].needs_confirm is True
    assert result.actions[0].confirmed is True


@pytest.mark.asyncio
async def test_claim_without_confirm_when_id_is_null() -> None:
    transport = FakeTransport(
        plugin=CREDIT,
        activities={"data": [DAILY]},
        claim_result=({"id": None}, 200),
        confirm_result=({"ok": True}, 200),
    )
    result = await run_checkin(transport)
    assert result.status == "claimed"
    assert transport.claim_calls == [("camp-login", "DESKTOP")]
    assert transport.confirm_calls == []
    assert result.actions[0].needs_confirm is False
    assert result.actions[0].confirmed is False


@pytest.mark.asyncio
async def test_confirm_failure_is_reported() -> None:
    transport = FakeTransport(
        plugin=CREDIT,
        activities={"data": [DAILY]},
        claim_result=({"id": "record-1"}, 200),
        confirm_result=RuntimeError("timeout"),
    )
    result = await run_checkin(transport)
    assert result.status == "confirm_failed"
    assert "pending" in result.message.lower()
    assert result.actions[0].confirmed is False


@pytest.mark.asyncio
async def test_claim_all_claimable_processes_multiple() -> None:
    other = {"campaignId": "bonus", "type": "BONUS", "claimable": True}
    transport = FakeTransport(
        plugin=CREDIT,
        activities={"data": [DAILY, other]},
        claim_result=({"id": None}, 200),
    )
    result = await run_checkin(transport, all_claimable=True)
    assert result.status == "claimed"
    assert [cid for cid, _ch in transport.claim_calls] == ["camp-login", "bonus"]


def test_interpret_claim_id_in_wrapper() -> None:
    interpretation = interpret_claim_response({"data": {"id": "x"}}, 200)
    assert interpretation.needs_confirm is True
    assert interpretation.claim_record_id == "x"
    interpretation_null = interpret_claim_response({"data": {"id": None}}, 200)
    assert interpretation_null.needs_confirm is False


@pytest.mark.parametrize("stage", ["delivery", "claim", "confirm"])
async def test_business_error_in_http_200_is_not_success(stage):
    rejected = {"code": "NO_ACCESS", "message": "not allowed", "data": None}
    transport = FakeTransport(
        plugin=CREDIT,
        activities=rejected if stage == "delivery" else {"code": 0, "data": {"items": [DAILY]}},
        claim_result=(rejected if stage == "claim" else {"data": {"userBenefitId": "receipt"}}, 200),
        confirm_result=(rejected, 200),
    )
    result = await run_checkin(transport)
    assert result.status == ("confirm_failed" if stage == "confirm" else "error")
    assert transport.confirm_calls == (["receipt"] if stage == "confirm" else [])
    if stage == "delivery":
        assert transport.claim_calls == []


async def test_checkin_signed_desktop_contract_with_cli_oauth_credentials(settings):
    calls = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        headers = request.headers
        assert headers["Content-Type"] == "application/json"
        assert headers["X-Security-Token"] == "cli-sts"
        assert "Access=cli-ak," in headers["Authorization"]
        signed = headers["Authorization"].split("SignedHeaders=", 1)[1].split(",", 1)[0].split(";")
        assert {"content-type", "x-security-token"} <= set(signed)
        if request.url.path == "/snap-manager/v1/statistics/plugin":
            assert request.method == "GET"
            assert "Agent-Type" not in headers
            return httpx.Response(200, json=CREDIT)

        assert headers["Agent-Type"] == "PromptCenter"
        assert headers["X-Language"] == "zh-cn"
        assert {"agent-type", "x-language"} <= set(signed)
        if request.url.path == "/v1/ops/delivery":
            assert request.method == "GET"
            assert request.url.params["channel"] == "DESKTOP"
            assert headers["x-snap-traceid"]
            assert "x-snap-traceid" in signed
            return httpx.Response(200, json={"code": 0, "data": {"items": [
                {"campaignId": 12, "type": "daily_claim", "status": "ELIGIBLE"},
                {"campaignId": 13, "type": "USER_LOGIN", "status": "ELIGIBLE"},
            ]}})

        assert request.method == "POST"
        body = json.loads(request.content)
        if request.url.path == "/v1/ops/claim":
            assert body.pop("idempotentKey").startswith("claim_")
            assert body == {"campaignId": 12, "channel": "DESKTOP"}
            return httpx.Response(200, json={"code": 0, "data": {"userBenefitId": 99}})
        assert request.url.path == "/v1/ops/confirm"
        assert body == {"userBenefitId": 99}
        return httpx.Response(200, json={"code": 0, "data": None})

    credential = ResolvedCredential("cli-ak", "cli-sk", "cli-sts", "oauth")
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        result = await run_checkin(SnapAccessClient(settings, http).bind(credential), campaign_type="USER_LOGIN")

    assert result.status == "claimed"
    assert result.actions[0].confirmed is True
    assert calls == ["/snap-manager/v1/statistics/plugin", "/v1/ops/delivery", "/v1/ops/claim", "/v1/ops/confirm"]
