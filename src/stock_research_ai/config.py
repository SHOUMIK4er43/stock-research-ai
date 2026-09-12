"""Validated runtime configuration, kept outside the application core."""

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    #: Live AnstModel provider configuration (Part 6B0). Absent provider/api_key means no
    #: live provider is configured; bootstrap never substitutes a fake in that case.
    analyst_provider: str | None = None
    #: SecretStr so Settings' own repr/str/model_dump can never leak this value; the
    #: adapter unwraps it exactly once via .get_secret_value() (bootstrap.py) and never
    #: threads it through any Pydantic field, log statement, or exception message.
    analyst_api_key: SecretStr | None = None
    analyst_model: str = "claude-sonnet-5"
    analyst_base_url: str = "https://api.anthropic.com"
    analyst_timeout_seconds: float = Field(default=90.0, gt=0)
    analyst_max_output_tokens: int = Field(default=4096, ge=1)

    @field_validator("database_path")
    @classmethod
    def absolute_database_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("artifact_path")
    @classmethod
    def absolute_artifact_path(cls, value: Path | None) -> Path | None:
        return value.expanduser().resolve() if value is not None else None
