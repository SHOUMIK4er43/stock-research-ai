"""Validated runtime configuration, kept outside the application core."""

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The only bind hosts `serve-http` accepts, and the only Host headers its TrustedHost
#: middleware serves (interfaces/http.py). The service is local-first with no LAN mode:
#: 0.0.0.0, ::, a LAN or public address, or any other name would expose the paired
#: browser API beyond this machine, and is refused at startup rather than silently bound.
#: ::1 is loopback, but the HTTP layer serves IPv4 loopback only, so it is refused too.
LOOPBACK_BIND_HOSTS = ("127.0.0.1", "localhost")


class Settings(BaseSettings):
    """Environment settings; construction never creates directories or files."""

    model_config = SettingsConfigDict(
        env_prefix="STOCK_RESEARCH_AI_",
        env_file=".env",
        extra="ignore",
    )

    database_path: Path = Field(
        default_factory=lambda: Path.home() / ".stock-research-ai" / "research.sqlite3"
    )
    artifact_path: Path | None = None
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8000, ge=1, le=65535)

    #: Explicit provider selection. An unset selector preserves the legacy Anthropic
    #: credential default; AI Credits requires an explicit selector, key and model.
    analyst_provider: str | None = None
    #: SecretStr so Settings' own repr/str/model_dump can never leak this value; the
    #: adapter unwraps it exactly once via .get_secret_value() (bootstrap.py) and never
    #: threads it through any Pydantic field, log statement, or exception message.
    analyst_api_key: SecretStr | None = None
    aicredits_api_key: SecretStr | None = None
    #: No guessed/default catalog model; selected AI Credits requires explicit configuration.
    aicredits_model: str | None = None
    analyst_model: str = "claude-sonnet-5"
    analyst_base_url: str = "https://api.anthropic.com"
    analyst_timeout_seconds: float = Field(default=90.0, gt=0)
    analyst_max_output_tokens: int = Field(default=4096, ge=1)
    #: AI Credits transport retry count only (adapters/aicredits/analyst.py); the direct
    #: Anthropic adapter keeps its own independent, fixed attempt count. Upper bound of 3
    #: is not arbitrary: it is the exact length of that adapter's fixed two-entry backoff
    #: schedule plus one - a higher value would run out of configured backoff durations.
    analyst_max_attempts: int = Field(default=3, ge=1, le=3)

    #: Browser extension pairing/auth state (Part 6B-A) - a security domain completely
    #: separate from analyst_* (Anthropic) credentials above. User-local, never in the
    #: research/report DB, never under the Git repo.
    browser_auth_path: Path = Field(
        default_factory=lambda: Path.home() / ".stock-research-ai" / "browser_auth.json"
    )

    #: Non-secret exact origin required for browser pairing and CORS response access.
    #: No default: an arbitrary extension cannot opt itself into this allowlist.
    browser_extension_origin: str | None = Field(
        default=None, pattern=r"^chrome-extension://[a-p]{32}$"
    )

    #: Part 6C-A: user-local rotating log file for `serve-http` (logging_setup.py).
    #: Never under the Git repo; never contains a secret value (see that module's
    #: redaction filter).
    log_path: Path = Field(
        default_factory=lambda: Path.home() / ".stock-research-ai" / "logs" / "service.log"
    )

    @field_validator("http_host")
    @classmethod
    def loopback_http_host(cls, value: str) -> str:
        host = value.strip().lower()
        if host not in LOOPBACK_BIND_HOSTS:
            raise ValueError(
                "http_host must be 127.0.0.1 or localhost: the local service is never "
                "exposed on another interface"
            )
        return host

    @field_validator("database_path")
    @classmethod
    def absolute_database_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("artifact_path")
    @classmethod
    def absolute_artifact_path(cls, value: Path | None) -> Path | None:
        return value.expanduser().resolve() if value is not None else None

    @field_validator("browser_auth_path")
    @classmethod
    def absolute_browser_auth_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("log_path")
    @classmethod
    def absolute_log_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()
