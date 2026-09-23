"""Telemetry, logging, external source registries, authentication, and Bruno export configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from reconciliator.config.defaults import (
    get_bundled_bruno_defaults,
    get_bundled_logging_defaults,
    get_bundled_telemetry_defaults,
)

_TELEMETRY_DEFAULTS = get_bundled_telemetry_defaults()
_LOGGING_DEFAULTS = get_bundled_logging_defaults()
_BRUNO_DEFAULTS = get_bundled_bruno_defaults()


class TelemetryConfig(BaseModel):
    """Progress heartbeat and query monitoring intervals."""

    heartbeat_interval_seconds: float = Field(
        default_factory=lambda: float(_TELEMETRY_DEFAULTS.get("heartbeat_interval_seconds", 60.0))
    )
    watch_interval_seconds: float = Field(
        default_factory=lambda: float(_TELEMETRY_DEFAULTS.get("watch_interval_seconds", 5.0))
    )
    stale_heartbeat_timeout_seconds: float = Field(
        default_factory=lambda: float(
            _TELEMETRY_DEFAULTS.get("stale_heartbeat_timeout_seconds", 120.0)
        )
    )
    adopt_previous_task_id: bool = Field(
        default_factory=lambda: bool(_TELEMETRY_DEFAULTS.get("adopt_previous_task_id", True))
    )
    show_vector_daemon: bool = Field(
        default_factory=lambda: bool(_TELEMETRY_DEFAULTS.get("show_vector_daemon", True))
    )


class LoggingConfig(BaseModel):
    """Logging subsystem configuration surface."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: str = "%(asctime)s [%(levelname)s] [task_id=%(task_id)s] %(name)s: %(message)s"
    date_format: str = "%Y-%m-%d %H:%M:%S"
    log_file: Optional[Path] = None
    capture_warnings: bool = False
    task_id_default: str = "-"
    enable_telemetry_handler: bool = True
    sanitization_log_level: Literal["ERROR", "WARNING", "INFO"] = "ERROR"

    @field_validator("level", mode="before")
    @classmethod
    def normalize_level(cls, v: Any) -> str:
        if isinstance(v, str):
            v_up = v.upper()
            if v_up == "WARN":
                return "WARNING"
            return v_up
        return str(v)


class BrunoConfig(BaseModel):
    """Configuration for dynamic Bruno API test suite generation."""

    model_config = ConfigDict(extra="ignore")

    collection_name: str = Field(
        default_factory=lambda: str(_BRUNO_DEFAULTS.get("collection_name", "Reconciliator API"))
    )
    version: str = Field(default_factory=lambda: str(_BRUNO_DEFAULTS.get("version", "1")))
    environment_name: str = Field(
        default_factory=lambda: str(_BRUNO_DEFAULTS.get("environment_name", "local"))
    )
    base_url: str = Field(
        default_factory=lambda: str(_BRUNO_DEFAULTS.get("base_url", "http://localhost:8000"))
    )
    include_assertions: bool = Field(
        default_factory=lambda: bool(_BRUNO_DEFAULTS.get("include_assertions", True))
    )
    default_timeout_ms: int = Field(
        default_factory=lambda: int(_BRUNO_DEFAULTS.get("default_timeout_ms", 5000))
    )


BrunoExportConfig = BrunoConfig


class SourceConfig(BaseModel):
    """Configuration for an authority or bibliographic source identifier / URI scheme."""

    uri_patterns: list[str] = Field(default_factory=list)
    canonical_uri_template: Optional[str] = None
    tag_prefixes: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    identifier_pattern: Optional[str] = None
    uppercase_identifier: bool = False
    subfield: str = "0"
    entity_type: Optional[str] = None


SourceRegistryEntry = SourceConfig


class EntraAuthConfig(BaseModel):
    """Configuration for Microsoft Entra ID authentication and local development mode."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    tenant_id: str = ""
    client_id: str = ""
    client_secret: Optional[str] = None
    audience: Optional[str] = None
    role_claim: str = "roles"
    admin_roles: list[str] = Field(
        default_factory=lambda: ["Reconciliator.Admin", "Admin", "admin"]
    )
    curator_roles: list[str] = Field(
        default_factory=lambda: ["Reconciliator.Curator", "Curator", "curator"]
    )
    reader_roles: list[str] = Field(
        default_factory=lambda: ["Reconciliator.Reader", "Reader", "reader"]
    )
    dev_default_role: str = "Admin"
    dev_user_email: str = "dev@localhost"
    dev_user_name: str = "Local Developer"
