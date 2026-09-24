"""Infrastructure, compute, storage, telemetry, and external system configuration models."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, Optional, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reconciliator.config.defaults import (
    get_bundled_bruno_defaults,
    get_bundled_compute_defaults,
    get_bundled_embedding_defaults,
    get_bundled_logging_defaults,
    get_bundled_postgres_defaults,
    get_bundled_storage_defaults,
    get_bundled_telemetry_defaults,
)

_COMPUTE_DEFAULTS = get_bundled_compute_defaults()
_STORAGE_DEFAULTS = get_bundled_storage_defaults()
_PG_DEFAULTS = get_bundled_postgres_defaults()
_TELEMETRY_DEFAULTS = get_bundled_telemetry_defaults()
_LOGGING_DEFAULTS = get_bundled_logging_defaults()
_BRUNO_DEFAULTS = get_bundled_bruno_defaults()
_EMBED_DEFAULTS = get_bundled_embedding_defaults()

VectorModelType = Literal["embedding", "llm"]


class ComputeConfig(BaseModel):
    """Hardware acceleration and compute parallelism configuration."""

    device: Literal["auto", "mps", "cuda", "cpu"] = Field(
        default_factory=lambda: _COMPUTE_DEFAULTS.get("device", "auto")
    )
    threads: Optional[int] = Field(
        default_factory=lambda: _COMPUTE_DEFAULTS.get("threads")
    )


class EmbeddingConfig(BaseModel):
    """Embedding model and hardware acceleration configuration."""

    model_name: str = Field(
        default_factory=lambda: str(
            _EMBED_DEFAULTS.get("model_name", "TurkuNLP/sbert-cased-finnish-paraphrase")
        )
    )
    template_path: Path = Field(
        default_factory=lambda: Path(
            _EMBED_DEFAULTS.get(
                "template_path", "reconciliator/embedding/templates/default_serialization.j2"
            )
        )
    )
    device: Literal["auto", "cuda", "mps", "cpu"] = Field(
        default_factory=lambda: _EMBED_DEFAULTS.get("device", "auto")
    )
    batch_size: int = Field(default_factory=lambda: int(_EMBED_DEFAULTS.get("batch_size", 64)))
    cuda_batch_scale: int = Field(
        default_factory=lambda: int(_EMBED_DEFAULTS.get("cuda_batch_scale", 4))
    )
    mps_batch_scale: int = Field(
        default_factory=lambda: int(_EMBED_DEFAULTS.get("mps_batch_scale", 2))
    )
    num_cpu_threads: Optional[int] = Field(
        default_factory=lambda: _EMBED_DEFAULTS.get("num_cpu_threads")
    )
    force_template: bool = Field(
        default_factory=lambda: bool(_EMBED_DEFAULTS.get("force_template", False))
    )
    vector_dim: Optional[int] = Field(default_factory=lambda: _EMBED_DEFAULTS.get("vector_dim"))
    max_seq_length: Optional[int] = Field(
        default_factory=lambda: _EMBED_DEFAULTS.get("max_seq_length")
    )

    @model_validator(mode="after")
    def validate_batch_size(self) -> Self:
        if self.batch_size <= 0:
            raise ValueError(f"batch_size must be a positive integer, got {self.batch_size}")
        return self


class TorchEngineConfig(BaseModel):
    """PyTorch vector index engine configuration."""

    dtype: Literal["float32", "float16", "bfloat16"] = Field(
        default_factory=lambda: _STORAGE_DEFAULTS.get("vector_engine", {})
        .get("torch", {})
        .get("dtype", "float32")
    )
    mmap: bool = Field(
        default_factory=lambda: bool(
            _STORAGE_DEFAULTS.get("vector_engine", {}).get("torch", {}).get("mmap", False)
        )
    )
    chunk_size: int = Field(
        default_factory=lambda: int(
            _STORAGE_DEFAULTS.get("vector_engine", {}).get("torch", {}).get("chunk_size", 50000)
        )
    )
    initial_capacity: int = Field(
        default_factory=lambda: int(
            _STORAGE_DEFAULTS.get("vector_engine", {})
            .get("torch", {})
            .get("initial_capacity", 10000)
        )
    )


class FAISSEngineConfig(BaseModel):
    """FAISS vector index engine configuration."""

    index_type: Literal["flat", "sq8"] = Field(
        default_factory=lambda: _STORAGE_DEFAULTS.get("vector_engine", {})
        .get("faiss", {})
        .get("index_type", "flat")
    )
    mmap: bool = Field(
        default_factory=lambda: bool(
            _STORAGE_DEFAULTS.get("vector_engine", {}).get("faiss", {}).get("mmap", False)
        )
    )
    nprobe: int = Field(
        default_factory=lambda: int(
            _STORAGE_DEFAULTS.get("vector_engine", {}).get("faiss", {}).get("nprobe", 16)
        )
    )
    nlist: int = Field(
        default_factory=lambda: int(
            _STORAGE_DEFAULTS.get("vector_engine", {}).get("faiss", {}).get("nlist", 2048)
        )
    )


class USearchEngineConfig(BaseModel):
    """USearch vector index engine configuration."""

    metric: Literal["cos", "ip", "l2"] = Field(
        default_factory=lambda: _STORAGE_DEFAULTS.get("vector_engine", {})
        .get("usearch", {})
        .get("metric", "cos")
    )
    dtype: Literal["f32", "f16", "i8", "b1"] = Field(
        default_factory=lambda: _STORAGE_DEFAULTS.get("vector_engine", {})
        .get("usearch", {})
        .get("dtype", "f16")
    )
    connectivity: int = Field(
        default_factory=lambda: int(
            _STORAGE_DEFAULTS.get("vector_engine", {}).get("usearch", {}).get("connectivity", 16)
        )
    )
    expansion_add: int = Field(
        default_factory=lambda: int(
            _STORAGE_DEFAULTS.get("vector_engine", {})
            .get("usearch", {})
            .get("expansion_add", 128)
        )
    )
    expansion_search: int = Field(
        default_factory=lambda: int(
            _STORAGE_DEFAULTS.get("vector_engine", {})
            .get("usearch", {})
            .get("expansion_search", 64)
        )
    )
    mmap: bool = Field(
        default_factory=lambda: bool(
            _STORAGE_DEFAULTS.get("vector_engine", {}).get("usearch", {}).get("mmap", True)
        )
    )


class VectorDaemonConfig(BaseModel):
    """Configuration for the singleton background vector service."""

    enabled: bool = Field(
        default_factory=lambda: bool(
            _STORAGE_DEFAULTS.get("vector_engine", {}).get("daemon", {}).get("enabled", True)
        )
    )
    socket_path: Optional[Path] = Field(
        default_factory=lambda: (
            Path(_STORAGE_DEFAULTS["vector_engine"]["daemon"]["socket_path"])
            if _STORAGE_DEFAULTS.get("vector_engine", {}).get("daemon", {}).get("socket_path")
            else None
        )
    )
    auto_start: bool = Field(
        default_factory=lambda: bool(
            _STORAGE_DEFAULTS.get("vector_engine", {}).get("daemon", {}).get("auto_start", True)
        )
    )
    idle_timeout_seconds: Optional[int] = Field(
        default_factory=lambda: _STORAGE_DEFAULTS.get("vector_engine", {})
        .get("daemon", {})
        .get("idle_timeout_seconds", 1800)
    )
    auto_save_interval_seconds: int = Field(
        default_factory=lambda: int(
            _STORAGE_DEFAULTS.get("vector_engine", {})
            .get("daemon", {})
            .get("auto_save_interval_seconds", 60)
        )
    )
    heartbeat_interval_seconds: float = Field(
        default_factory=lambda: float(
            _STORAGE_DEFAULTS.get("vector_engine", {})
            .get("daemon", {})
            .get("heartbeat_interval_seconds", 2.0)
        )
    )
    max_batch_bytes: int = Field(
        default_factory=lambda: int(
            _STORAGE_DEFAULTS.get("vector_engine", {})
            .get("daemon", {})
            .get("max_batch_bytes", 67108864)
        )
    )


class VectorEngineConfig(BaseModel):
    """Vector search engine backend and hardware acceleration configuration."""

    backend: Literal["auto", "faiss", "torch", "usearch"] = Field(
        default_factory=lambda: _STORAGE_DEFAULTS.get("vector_engine", {}).get("backend", "auto")
    )
    device: Literal["auto", "mps", "cuda", "cpu"] = Field(
        default_factory=lambda: _STORAGE_DEFAULTS.get("vector_engine", {}).get("device", "auto")
    )
    mode: Literal["daemon", "embedded"] = Field(
        default_factory=lambda: _STORAGE_DEFAULTS.get("vector_engine", {}).get("mode", "embedded")
    )
    threads: Optional[int] = Field(
        default_factory=lambda: _STORAGE_DEFAULTS.get("vector_engine", {}).get("threads")
    )
    torch: TorchEngineConfig = Field(default_factory=TorchEngineConfig)
    faiss: FAISSEngineConfig = Field(default_factory=FAISSEngineConfig)
    usearch: USearchEngineConfig = Field(default_factory=USearchEngineConfig)
    daemon: VectorDaemonConfig = Field(default_factory=VectorDaemonConfig)


class RepositoryFTSConfig(BaseModel):
    """Configuration for PostgreSQL full-text search ranking in WEMIRepository."""

    ranking_function: str = Field(
        default_factory=lambda: str(
            _STORAGE_DEFAULTS.get("fts", {}).get("ranking_function", "bm25")
        ),
        pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$",
    )
    column_weights: Optional[list[float]] = Field(
        default_factory=lambda: _STORAGE_DEFAULTS.get("fts", {}).get("column_weights")
    )
    default_limit: int = Field(
        default_factory=lambda: int(_STORAGE_DEFAULTS.get("fts", {}).get("default_limit", 20)),
        ge=1,
    )
    prefix_search: bool = Field(
        default_factory=lambda: bool(_STORAGE_DEFAULTS.get("fts", {}).get("prefix_search", False))
    )


class PostgresConfig(BaseModel):
    """PostgreSQL connection, engine tuning, and container configuration."""

    host: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("host", "localhost")))
    port: int = Field(default_factory=lambda: int(_PG_DEFAULTS.get("port", 5432)))
    database: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("database", "reconciliator"))
    )
    user: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("user", "postgres")))
    password: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("password", "postgres"))
    )
    database_url: Optional[str] = Field(default_factory=lambda: _PG_DEFAULTS.get("database_url"))

    pool_size: int = Field(default_factory=lambda: int(_PG_DEFAULTS.get("pool_size", 10)))
    max_overflow: int = Field(default_factory=lambda: int(_PG_DEFAULTS.get("max_overflow", 20)))
    pool_timeout: float = Field(
        default_factory=lambda: float(_PG_DEFAULTS.get("pool_timeout", 30.0))
    )
    pool_recycle: int = Field(default_factory=lambda: int(_PG_DEFAULTS.get("pool_recycle", 1800)))
    pool_pre_ping: bool = Field(
        default_factory=lambda: bool(_PG_DEFAULTS.get("pool_pre_ping", True))
    )

    shared_buffers: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("shared_buffers", "1GB"))
    )
    work_mem: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("work_mem", "64MB")))
    maintenance_work_mem: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("maintenance_work_mem", "256MB"))
    )
    effective_cache_size: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("effective_cache_size", "3GB"))
    )
    random_page_cost: float = Field(
        default_factory=lambda: float(_PG_DEFAULTS.get("random_page_cost", 1.1))
    )
    synchronous_commit: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("synchronous_commit", "off"))
    )

    container_name: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("container_name", "reconciliator-pg"))
    )
    container_image: str = Field(
        default_factory=lambda: str(
            _PG_DEFAULTS.get("container_image", "docker.io/library/postgres:16-alpine")
        )
    )
    container_timeout_seconds: float = Field(
        default_factory=lambda: float(_PG_DEFAULTS.get("container_timeout_seconds", 30.0))
    )
    auto_start_container: bool = Field(
        default_factory=lambda: bool(_PG_DEFAULTS.get("auto_start_container", True))
    )
    locale: Optional[str] = Field(default_factory=lambda: _PG_DEFAULTS.get("locale"))
    lc_collate: Optional[str] = Field(default_factory=lambda: _PG_DEFAULTS.get("lc_collate"))
    lc_ctype: Optional[str] = Field(default_factory=lambda: _PG_DEFAULTS.get("lc_ctype"))
    initdb_args: Optional[str] = Field(default_factory=lambda: _PG_DEFAULTS.get("initdb_args"))

    max_parameters_per_batch: int = Field(
        default_factory=lambda: int(_PG_DEFAULTS.get("max_parameters_per_batch", 32767))
    )
    default_chunk_size: Optional[int] = Field(
        default_factory=lambda: _PG_DEFAULTS.get("default_chunk_size")
    )
    data_dir: Optional[Path] = Field(
        default_factory=lambda: (
            Path(_PG_DEFAULTS["data_dir"]) if _PG_DEFAULTS.get("data_dir") else None
        )
    )

    def get_effective_data_dir(self) -> Optional[Path]:
        """Resolve effective host data directory for PostgreSQL storage bind mount."""
        env_dir = os.environ.get("RECONCILIATOR_POSTGRES_DATA_DIR")
        if env_dir:
            return Path(env_dir).expanduser().resolve()
        if self.data_dir:
            return Path(self.data_dir).expanduser().resolve()
        return None

    def get_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        env_url = os.environ.get("RECONCILIATOR_DATABASE_URL")
        if env_url:
            return env_url
        return f"postgresql+psycopg://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


class StorageConfig(BaseModel):
    """File paths for databases, vector indices, and backend engines."""

    data_dir: Path = Field(
        default_factory=lambda: Path(_STORAGE_DEFAULTS.get("data_dir", "data"))
    )
    database_url: Optional[str] = None
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    concept_index: Optional[Path] = None
    work_index: Optional[Path] = None
    manifestation_index: Optional[Path] = None
    agent_index: Optional[Path] = None
    vector_engine: VectorEngineConfig = Field(default_factory=VectorEngineConfig)
    fts: RepositoryFTSConfig = Field(default_factory=RepositoryFTSConfig)

    @model_validator(mode="after")
    def _resolve_defaults(self) -> Self:
        d = self.data_dir
        if self.database_url is None:
            base_url = (
                os.environ.get("RECONCILIATOR_DATABASE_URL") or self.postgres.get_database_url()
            )
            is_default_data = d == Path("data") or (
                d.name == "data" and d.resolve() == (Path.cwd() / "data").resolve()
            )
            if not is_default_data and "?" not in base_url:
                import hashlib
                import psycopg

                schema = f"test_{hashlib.md5(str(d.resolve()).encode()).hexdigest()[:10]}"
                raw_url = base_url.split("?")[0].replace(
                    "postgresql+psycopg://", "postgresql://", 1
                )
                try:
                    with psycopg.connect(raw_url) as conn:
                        with conn.cursor() as cur:
                            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema};")
                except Exception:
                    pass
                self.database_url = f"{base_url}?options=-csearch_path%3D{schema},public"
            else:
                self.database_url = base_url
        if self.concept_index is None:
            self.concept_index = d / "concept.index"
        if self.work_index is None:
            self.work_index = d / "work.index"
        if self.manifestation_index is None:
            self.manifestation_index = d / "manifestation.index"
        if self.agent_index is None:
            self.agent_index = d / "agent.index"
        return self


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

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default_factory=lambda: str(_LOGGING_DEFAULTS.get("level", "INFO"))  # type: ignore[assignment]
    )
    format: str = Field(
        default_factory=lambda: str(
            _LOGGING_DEFAULTS.get(
                "format",
                "%(asctime)s [%(levelname)s] [task_id=%(task_id)s] %(name)s: %(message)s",
            )
        )
    )
    date_format: str = Field(
        default_factory=lambda: str(_LOGGING_DEFAULTS.get("date_format", "%Y-%m-%d %H:%M:%S"))
    )
    log_file: Optional[Path] = Field(
        default_factory=lambda: (
            Path(_LOGGING_DEFAULTS["log_file"]) if _LOGGING_DEFAULTS.get("log_file") else None
        )
    )
    capture_warnings: bool = Field(
        default_factory=lambda: bool(_LOGGING_DEFAULTS.get("capture_warnings", False))
    )
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

