"""Runtime settings.

Runtime options belong in auto-created data/config.json or the WebUI.
Account credentials live in separate data/accounts/*.json auth files.
Environment variables remain valid overrides for local/dev and for process-only
values (host, port, data_dir, auth_dir). Secrets such as CA2A_API_KEY may come from
the environment or be persisted in the config file after a WebUI save.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SNAP_BASE_URL = "https://snap-access.cn-north-4.myhuaweicloud.com"
DEFAULT_TIMEZONE = "Asia/Shanghai"

# Fields stored in data/config.json and editable from the WebUI.
# host / port / data_dir / auth_dir stay env-only (process bind and paths).
# api_key may be bootstrapped from env and later persisted.
OVERRIDABLE_FIELDS = (
    "timezone",
    "checkin_cron",
    "checkin_run_on_startup",
    "scheduler_enabled",
    "campaign_type",
    "checkin_claim_all",
    "model_cache_ttl",
    "snap_base_url",
    "opengw_base_url",
    "x_language",
    "log_level",
    "http_timeout",
    "http_stream_timeout",
    "credential_source",
    "oauth_client_id",
    "oauth_portal_url",
    "oauth_sts_url",
    "oauth_locale",
    "oauth_callback_port",
    "pool_strategy",
    "cooldown_seconds",
    "chat_concurrency_limit",
    "api_key",
)

READ_ONLY_FIELDS = ("host", "port", "data_dir", "auth_dir")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    codearts_ak: SecretStr | None = Field(default=None)
    codearts_sk: SecretStr | None = Field(default=None)
    codearts_security_token: SecretStr | None = Field(default=None)
    codearts_project_id: str | None = Field(default=None)

    snap_base_url: str = Field(default=DEFAULT_SNAP_BASE_URL)

    host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("CA2A_HOST", "CODEARTS2API_HOST"),
    )
    port: int = Field(
        default=8787,
        validation_alias=AliasChoices("CA2A_PORT", "CODEARTS2API_PORT"),
    )
    api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("CA2A_API_KEY", "CODEARTS2API_API_KEY"),
    )

    timezone: str = Field(
        default=DEFAULT_TIMEZONE,
        validation_alias=AliasChoices("CA2A_TIMEZONE", "CODEARTS2API_TIMEZONE"),
    )
    checkin_cron: str = Field(
        default="0 8 * * *",
        validation_alias=AliasChoices("CA2A_CHECKIN_CRON", "CODEARTS2API_CHECKIN_CRON"),
    )
    checkin_run_on_startup: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CA2A_CHECKIN_RUN_ON_STARTUP",
            "CODEARTS2API_CHECKIN_RUN_ON_STARTUP",
        ),
    )
    scheduler_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CA2A_SCHEDULER_ENABLED",
            "CODEARTS2API_SCHEDULER_ENABLED",
        ),
    )
    campaign_type: str = Field(
        default="DAILY_CLAIM",
        validation_alias=AliasChoices("CA2A_CAMPAIGN_TYPE", "CODEARTS2API_CAMPAIGN_TYPE"),
    )
    checkin_claim_all: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "CA2A_CHECKIN_CLAIM_ALL",
            "CODEARTS2API_CHECKIN_CLAIM_ALL",
        ),
    )

    model_cache_ttl: int = Field(
        default=60,
        validation_alias=AliasChoices(
            "CA2A_MODEL_CACHE_TTL",
            "CODEARTS2API_MODEL_CACHE_TTL",
        ),
    )

    x_language: str | None = Field(
        default=None,
        validation_alias=AliasChoices("CA2A_X_LANGUAGE", "CODEARTS2API_X_LANGUAGE"),
    )

    opengw_base_url: str | None = Field(
        default="https://opengw.developer.huaweicloud.com",
        validation_alias=AliasChoices("OPENGW_BASE_URL", "CA2A_OPENGW_BASE_URL"),
    )

    auth_dir: str = Field(
        default="auths",
        validation_alias=AliasChoices("CA2A_AUTH_DIR", "CODEARTS2API_AUTH_DIR"),
    )
    data_dir: str = Field(
        default="data",
        validation_alias=AliasChoices("CA2A_DATA_DIR", "CODEARTS2API_DATA_DIR"),
    )
    credential_source: str = Field(
        default="auto",
        validation_alias=AliasChoices("CA2A_CREDENTIAL_SOURCE", "CODEARTS2API_CREDENTIAL_SOURCE"),
    )
    oauth_client_id: str = Field(
        default="CodeArts_Tui",
        validation_alias=AliasChoices("CA2A_OAUTH_CLIENT_ID", "CODEARTS2API_OAUTH_CLIENT_ID"),
    )
    oauth_portal_url: str = Field(
        default="https://codearts.huaweicloud.com/portal",
        validation_alias=AliasChoices("CA2A_OAUTH_PORTAL_URL", "CODEARTS2API_OAUTH_PORTAL_URL"),
    )
    oauth_sts_url: str = Field(
        default="https://sts.cn-north-4.myhuaweicloud.com",
        validation_alias=AliasChoices("CA2A_OAUTH_STS_URL", "CODEARTS2API_OAUTH_STS_URL"),
    )
    oauth_callback_port: int = Field(
        default=0,
        validation_alias=AliasChoices("CA2A_OAUTH_CALLBACK_PORT", "CODEARTS2API_OAUTH_CALLBACK_PORT"),
    )
    oauth_locale: str = Field(
        default="zh-cn",
        validation_alias=AliasChoices("CA2A_OAUTH_LOCALE", "CODEARTS2API_OAUTH_LOCALE"),
    )

    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("CA2A_LOG_LEVEL", "CODEARTS2API_LOG_LEVEL"),
    )
    http_timeout: float = Field(
        default=60.0,
        validation_alias=AliasChoices("CA2A_HTTP_TIMEOUT", "CODEARTS2API_HTTP_TIMEOUT"),
    )
    http_stream_timeout: float = Field(
        default=300.0,
        validation_alias=AliasChoices(
            "CA2A_HTTP_STREAM_TIMEOUT",
            "CODEARTS2API_HTTP_STREAM_TIMEOUT",
        ),
    )
    pool_strategy: str = Field(
        default="round_robin",
        validation_alias=AliasChoices("CA2A_POOL_STRATEGY", "CODEARTS2API_POOL_STRATEGY"),
    )
    cooldown_seconds: int = Field(
        default=60,
        validation_alias=AliasChoices("CA2A_COOLDOWN_SECONDS", "CODEARTS2API_COOLDOWN_SECONDS"),
    )
    chat_concurrency_limit: int = Field(
        default=0,
        validation_alias=AliasChoices("CA2A_CHAT_CONCURRENCY_LIMIT", "CODEARTS2API_CHAT_CONCURRENCY_LIMIT"),
        ge=0,
    )

    @field_validator("credential_source")
    @classmethod
    def _credential_source(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized == "env":
            normalized = "aksk"
        if normalized not in {"auto", "oauth", "aksk"}:
            raise ValueError("credential_source must be auto, oauth, or aksk")
        return normalized

    @field_validator("pool_strategy")
    @classmethod
    def _pool_strategy(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_")
        if normalized not in {"round_robin", "first_available", "random"}:
            raise ValueError("CA2A_POOL_STRATEGY must be round_robin, first_available, or random")
        return normalized

    @field_validator("oauth_portal_url", "oauth_sts_url")
    @classmethod
    def _strip_oauth_urls(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("snap_base_url")
    @classmethod
    def _strip_snap_base(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("opengw_base_url")
    @classmethod
    def _strip_opengw(cls, value: str | None) -> str | None:
        if value is None or value.strip() == "":
            return None
        return value.strip().rstrip("/")

    @field_validator("x_language", "codearts_project_id")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None or value.strip() == "":
            return None
        return value.strip()

    @field_validator("cooldown_seconds")
    @classmethod
    def _cooldown_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("CA2A_COOLDOWN_SECONDS must be >= 0")
        return value

    def has_credentials(self) -> bool:
        return bool(
            self.codearts_ak
            and self.codearts_ak.get_secret_value()
            and self.codearts_sk
            and self.codearts_sk.get_secret_value()
        )

    def has_api_key(self) -> bool:
        return bool(self.api_key and self.api_key.get_secret_value())

    def secret_values_for_redaction(self, pool: object | None = None) -> list[str]:
        values: list[str] = []
        for item in (
            self.codearts_ak,
            self.codearts_sk,
            self.codearts_security_token,
            self.api_key,
        ):
            if item is None:
                continue
            raw = item.get_secret_value()
            if raw:
                values.append(raw)
        if pool is not None and hasattr(pool, "all_secrets"):
            values.extend(pool.all_secrets())
        return values

    def public_config(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "data_dir": self.data_dir,
            "auth_dir": self.auth_dir,
            "api_key_configured": self.has_api_key(),
            "read_only": list(READ_ONLY_FIELDS),
            "timezone": self.timezone,
            "checkin_cron": self.checkin_cron,
            "checkin_run_on_startup": self.checkin_run_on_startup,
            "scheduler_enabled": self.scheduler_enabled,
            "campaign_type": self.campaign_type,
            "checkin_claim_all": self.checkin_claim_all,
            "model_cache_ttl": self.model_cache_ttl,
            "snap_base_url": self.snap_base_url,
            "opengw_base_url": self.opengw_base_url,
            "x_language": self.x_language,
            "log_level": self.log_level,
            "http_timeout": self.http_timeout,
            "http_stream_timeout": self.http_stream_timeout,
            "credential_source": self.credential_source,
            "oauth_client_id": self.oauth_client_id,
            "oauth_portal_url": self.oauth_portal_url,
            "oauth_sts_url": self.oauth_sts_url,
            "oauth_locale": self.oauth_locale,
            "oauth_callback_port": self.oauth_callback_port,
            "pool_strategy": self.pool_strategy,
            "cooldown_seconds": self.cooldown_seconds,
            "chat_concurrency_limit": self.chat_concurrency_limit,
        }
