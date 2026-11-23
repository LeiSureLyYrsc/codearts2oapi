"""Quota extraction for the admin account probe.

Field and metric names come from the shipped CodeArts Desktop 0.2.9 bundle:
``GET /snap-manager/v1/statistics/plugin`` returns ``package`` plus
``metrics[]``; credit quotas live in ``usageTotalPackageCredit`` and token
quotas in ``usageTokenChatMessages``.
"""

from __future__ import annotations

from typing import Any

import pytest

from codearts2api.client import UpstreamError
from codearts2api.credentials import ResolvedCredential
from codearts2api.operations import extract_quota, probe_account


CREDIT_PACKAGE = {"package": {"is_credit_package": True, "package_name_en": "Basic"}}
TOKEN_PACKAGE = {"package": {"is_credit_package": False, "is_token_package": True}}


def test_credit_quota_uses_official_metric_fields() -> None:
    payload = {
        **CREDIT_PACKAGE,
        "metrics": [
            {
                "name": "usageTotalPackageCredit",
                "package_credit_amount": 1000,
                "package_credit_used": 250,
                "package_credit_remain": 750,
                "package_credit_expiring_amount": 100,
            }
        ],
    }
    assert extract_quota(payload) == {
        "unit": "credit",
        "total": 1000,
        "used": 250,
        "remaining": 750,
        "expiring": 100,
    }


def test_credit_quota_remains_null_when_not_reported() -> None:
    """Credit remaining is explicit upstream truth; never derive total-used."""
    payload = {
        **CREDIT_PACKAGE,
        "metrics": [
            {
                "name": "usageTotalPackageCredit",
                "package_credit_amount": 1000,
                "package_credit_used": 250,
            }
        ],
    }
    assert extract_quota(payload) == {
        "unit": "credit",
        "total": 1000,
        "used": 250,
        "remaining": None,
        "expiring": None,
    }


def test_credit_quota_keeps_explicit_zero() -> None:
    payload = {
        **CREDIT_PACKAGE,
        "metrics": [
            {
                "name": "usageTotalPackageCredit",
                "package_credit_amount": 10,
                "package_credit_remain": 0,
            }
        ],
    }
    quota = extract_quota(payload)
    assert quota is not None
    assert quota["remaining"] == 0
    assert quota["used"] is None


def test_token_quota_derives_remaining_from_finite_counters() -> None:
    payload = {
        **TOKEN_PACKAGE,
        "metrics": [
            {
                "name": "usageTokenChatMessages",
                "package_token_amount": 1000,
                "usage_token_num": 250,
            }
        ],
    }
    assert extract_quota(payload) == {
        "unit": "token",
        "total": 1000,
        "used": 250,
        "remaining": 750,
        "expiring": None,
    }


@pytest.mark.parametrize(
    ("metric", "total", "used"),
    [
        ({"name": "usageTokenChatMessages", "package_token_amount": -1, "usage_token_num": 5}, None, 5),
        ({"name": "usageTokenChatMessages", "package_token_amount": 1000, "usage_token_num": -1}, 1000, None),
        ({"name": "usageTokenChatMessages", "package_token_amount": 1000}, 1000, None),
        ({"name": "usageTokenChatMessages", "usage_token_num": 5}, None, 5),
        ({"name": "usageTokenChatMessages", "package_token_amount": "n/a", "usage_token_num": 5}, None, 5),
    ],
)
def test_token_quota_never_invents_a_number(
    metric: dict[str, Any],
    total: int | None,
    used: int | None,
) -> None:
    """Unlimited (-1), missing, or invalid counters must not read as 0."""
    payload = {**TOKEN_PACKAGE, "metrics": [metric]}
    quota = extract_quota(payload)
    assert quota is not None
    assert quota["total"] == total
    assert quota["used"] == used
    # Neither counter pair is complete, so no remaining is invented.
    assert quota["remaining"] is None


def test_token_quota_rejects_inconsistent_counters() -> None:
    payload = {
        **TOKEN_PACKAGE,
        "metrics": [
            {"name": "usageTokenChatMessages", "package_token_amount": 10, "usage_token_num": 11}
        ],
    }
    quota = extract_quota(payload)
    assert quota is not None
    assert quota["remaining"] is None
    assert quota["used"] == 11


def test_quota_absent_returns_none() -> None:
    assert extract_quota({"package": {"is_credit_package": True}}) is None
    assert extract_quota({**CREDIT_PACKAGE, "metrics": []}) is None
    assert extract_quota({**CREDIT_PACKAGE, "metrics": [{"name": "usageOther"}]}) is None
    assert extract_quota(None) is None


def test_quota_record_without_usable_values_returns_none() -> None:
    payload = {
        **CREDIT_PACKAGE,
        "metrics": [{"name": "usageTotalPackageCredit", "package_credit_amount": "unknown"}],
    }
    assert extract_quota(payload) is None


def test_quota_respects_data_and_result_wrappers() -> None:
    payload = {
        "data": {
            "package": {"is_credit_package": True},
            "metrics": [{"name": "usageTotalPackageCredit", "package_credit_remain": 7}],
        }
    }
    quota = extract_quota(payload)
    assert quota is not None
    assert quota["remaining"] == 7

    wrapped = {
        "result": {
            "package": {"is_credit_package": False},
            "metrics": [
                {"name": "usageTokenChatMessages", "package_token_amount": 9, "usage_token_num": 4}
            ],
        }
    }
    token = extract_quota(wrapped)
    assert token is not None
    assert (token["unit"], token["remaining"]) == ("token", 5)


def test_credit_package_ignores_token_metric() -> None:
    """Never answer a credit package with a token counter."""
    payload = {
        **CREDIT_PACKAGE,
        "metrics": [
            {"name": "usageTokenChatMessages", "package_token_amount": 100, "usage_token_num": 1}
        ],
    }
    assert extract_quota(payload) is None


def test_large_counters_keep_full_precision() -> None:
    payload = {
        **TOKEN_PACKAGE,
        "metrics": [
            {
                "name": "usageTokenChatMessages",
                "package_token_amount": 2**60 + 3,
                "usage_token_num": 3,
            }
        ],
    }
    quota = extract_quota(payload)
    assert quota is not None
    assert quota["total"] == 2**60 + 3
    assert quota["remaining"] == 2**60
    assert extract_quota({"metrics": [{
        "name": "usageTotalPackageCredit", "package_credit_amount": "9007199254740993",
    }]}) == {
        "unit": "credit",
        "total": 9007199254740993,
        "used": None,
        "remaining": None,
        "expiring": None,
    }


def test_quota_returns_only_allowlisted_fields() -> None:
    payload = {
        **CREDIT_PACKAGE,
        "package": {**CREDIT_PACKAGE["package"], "package_url": "https://secret.invalid/token"},
        "metrics": [
            {
                "name": "usageTotalPackageCredit",
                "package_credit_amount": 5,
                "package_credit_remain": 5,
                "package_credit_secret": "s3cr3t",
                "show": True,
            }
        ],
        "access_key": "should-not-leak",
    }
    quota = extract_quota(payload)
    assert quota is not None
    assert set(quota) == {"unit", "total", "used", "remaining", "expiring"}
    assert "s3cr3t" not in repr(quota)
    assert "should-not-leak" not in repr(quota)


class FakeClient:
    def __init__(self, payload: Any = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.credentials: list[ResolvedCredential] = []

    async def get_plugin_statistics(self, credential: ResolvedCredential | None = None) -> Any:
        assert credential is not None
        self.credentials.append(credential)
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.mark.asyncio
async def test_probe_uses_the_bound_credential_and_appends_quota() -> None:
    cred = ResolvedCredential(
        access_key_id="bound-ak",
        secret_access_key="bound-sk",
        security_token="bound-sts",
        source="oauth:acct-1",
        account_id="acct-1",
    )
    client = FakeClient(
        {
            "package": {"is_credit_package": True},
            "metrics": [
                {
                    "name": "usageTotalPackageCredit",
                    "package_credit_amount": 10,
                    "package_credit_used": 4,
                    "package_credit_remain": 6,
                    "package_credit_expiring_amount": 2,
                }
            ],
        }
    )
    result = await probe_account(client, cred)  # type: ignore[arg-type]
    assert client.credentials == [cred]
    assert result == {
        "ok": True,
        "http_status": 200,
        "account_type": "credit",
        "is_credit_package": True,
        "message": "Probe succeeded",
        "quota": {
            "unit": "credit",
            "total": 10,
            "used": 4,
            "remaining": 6,
            "expiring": 2,
        },
    }


@pytest.mark.asyncio
async def test_probe_keeps_old_payload_when_package_type_is_unknown() -> None:
    client = FakeClient({"metrics": []})
    result = await probe_account(client, _cred())  # type: ignore[arg-type]
    assert result["ok"] is True
    assert result["account_type"] == "unknown"
    assert result["is_credit_package"] is False
    assert result["quota"] is None


@pytest.mark.asyncio
async def test_probe_http_200_business_error_is_not_reported_ineligible() -> None:
    client = FakeClient(
        {"code": "40002", "message": "internal detail that must not be echoed"}
    )
    result = await probe_account(client, _cred())  # type: ignore[arg-type]
    assert result["ok"] is False
    assert result["http_status"] == 200
    assert result["account_type"] == "unknown"
    assert result["is_credit_package"] is None
    assert result["quota"] is None
    assert "40002" in result["message"]
    assert "internal detail" not in result["message"]


@pytest.mark.asyncio
async def test_probe_preserves_upstream_error_status() -> None:
    client = FakeClient(error=UpstreamError("quota exceeded", 429))
    result = await probe_account(client, _cred())  # type: ignore[arg-type]
    assert result == {
        "ok": False,
        "http_status": 429,
        "message": "quota exceeded",
        "quota": None,
    }


@pytest.mark.asyncio
async def test_probe_reports_unexpected_failures_without_status() -> None:
    client = FakeClient(error=RuntimeError("connection dropped"))
    result = await probe_account(client, _cred())  # type: ignore[arg-type]
    assert result["ok"] is False
    assert "http_status" not in result
    assert result["quota"] is None


def _cred() -> ResolvedCredential:
    return ResolvedCredential(
        access_key_id="ak",
        secret_access_key="sk",
        security_token=None,
        source="oauth:acct-1",
        account_id="acct-1",
    )
