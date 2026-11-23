"""Request and response models used by the local gateway."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatCompletionsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    n: int | None = None
    stop: Any | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    user: str | None = None


class CheckinRequest(BaseModel):
    all_claimable: bool = False
    account_id: str | None = None


class AccountCreateRequest(BaseModel):
    name: str
    kind: Literal["aksk", "oauth"]
    enabled: bool = True
    note: str = ""
    ak: str | None = None
    sk: str | None = None
    security_token: str | None = None
    project_id: str | None = None


class AccountUpdateRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    note: str | None = None
    ak: str | None = None
    sk: str | None = None
    security_token: str | None = None
    project_id: str | None = None


class OAuthLoginStartRequest(BaseModel):
    """Kick off a new OAuth account; the account exists only after success."""

    name: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=200)
    enabled: bool = True


class OpenAIErrorBody(BaseModel):
    message: str
    type: str = "api_error"
    code: str | None = None
    param: str | None = None


CheckinStatus = Literal[
    "ineligible",
    "already_claimed",
    "claimed",
    "confirm_failed",
    "activity_missing",
    "no_campaign",
    "error",
    "skipped",
]
