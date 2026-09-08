"""Validated runtime configuration, kept outside the application core."""

from pathlib import Path

from pydantic import Field, field_validator
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

    @field_validator("database_path")
    @classmethod
    def absolute_database_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("artifact_path")
    @classmethod
    def absolute_artifact_path(cls, value: Path | None) -> Path | None:
        return value.expanduser().resolve() if value is not None else None
