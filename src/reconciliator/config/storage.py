"""Storage and vector engine configuration models."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional, Self

from pydantic import BaseModel, Field, model_validator

from reconciliator.config.defaults import get_bundled_postgres_defaults

_PG_DEFAULTS = get_bundled_postgres_defaults()


class TorchEngineConfig(BaseModel):
    """PyTorch vector index engine configuration."""

    dtype: Literal["float32", "float16", "bfloat16"] = "float32"
    mmap: bool = False
    chunk_size: int = 50000
    initial_capacity: int = 10000


class FAISSEngineConfig(BaseModel):
    """FAISS vector index engine configuration."""

    index_type: Literal["flat", "sq8"] = "flat"
    mmap: bool = False
    nprobe: int = 16
    nlist: int = 2048


class USearchEngineConfig(BaseModel):
    """USearch vector index engine configuration."""

    metric: Literal["cos", "ip", "l2"] = "cos"
    dtype: Literal["f32", "f16", "i8", "b1"] = "f16"
    connectivity: int = 16
    expansion_add: int = 128
    expansion_search: int = 64
    mmap: bool = True


class VectorDaemonConfig(BaseModel):
    """Configuration for the singleton background vector service."""

    enabled: bool = True
    socket_path: Optional[Path] = None
    auto_start: bool = True
    idle_timeout_seconds: Optional[int] = 1800
    auto_save_interval_seconds: int = 60
    heartbeat_interval_seconds: float = 2.0
    max_batch_bytes: int = 67108864


class VectorEngineConfig(BaseModel):
    """Vector search engine backend and hardware acceleration configuration."""

    backend: Literal["auto", "faiss", "torch", "usearch"] = "auto"
    device: Literal["auto", "mps", "cuda", "cpu"] = "auto"
    mode: Literal["daemon", "embedded"] = "embedded"
    threads: Optional[int] = None
    torch: TorchEngineConfig = Field(default_factory=TorchEngineConfig)
    faiss: FAISSEngineConfig = Field(default_factory=FAISSEngineConfig)
    usearch: USearchEngineConfig = Field(default_factory=USearchEngineConfig)
    daemon: VectorDaemonConfig = Field(default_factory=VectorDaemonConfig)


class RepositoryFTSConfig(BaseModel):
    """Configuration for PostgreSQL full-text search (tsvector/tsquery) ranking in WEMIRepository."""

    ranking_function: str = Field(
        default="bm25",
        pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$",
        description="Text search ranking function (e.g. ts_rank or ts_rank_cd)",
    )
    column_weights: Optional[list[float]] = None
    default_limit: int = Field(default=20, ge=1)
    prefix_search: bool = False


class PostgresConfig(BaseModel):
    """PostgreSQL connection, engine tuning, and container configuration."""

    host: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("host", "localhost")))
    port: int = Field(default_factory=lambda: int(_PG_DEFAULTS.get("port", 5432)))
    database: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("database", "reconciliator"))
    )
    user: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("user", "reconciliator")))
    password: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("password", "reconciliator"))
    )
    database_url: Optional[str] = Field(default_factory=lambda: _PG_DEFAULTS.get("database_url"))

    # Connection pooling & timeouts
    pool_size: int = Field(default_factory=lambda: int(_PG_DEFAULTS.get("pool_size", 10)))
    max_overflow: int = Field(default_factory=lambda: int(_PG_DEFAULTS.get("max_overflow", 20)))
    pool_timeout: float = Field(
        default_factory=lambda: float(_PG_DEFAULTS.get("pool_timeout", 30.0))
    )
    pool_recycle: int = Field(default_factory=lambda: int(_PG_DEFAULTS.get("pool_recycle", 1800)))
    pool_pre_ping: bool = Field(
        default_factory=lambda: bool(_PG_DEFAULTS.get("pool_pre_ping", True))
    )

    # Performance tuning parameters
    shared_buffers: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("shared_buffers", "256MB"))
    )
    work_mem: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("work_mem", "16MB")))
    maintenance_work_mem: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("maintenance_work_mem", "64MB"))
    )
    effective_cache_size: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("effective_cache_size", "1GB"))
    )
    random_page_cost: float = Field(
        default_factory=lambda: float(_PG_DEFAULTS.get("random_page_cost", 1.1))
    )
    synchronous_commit: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("synchronous_commit", "on"))
    )

    # Container settings
    container_name: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("container_name", "reconciliator-postgres"))
    )
    container_image: str = Field(
        default_factory=lambda: str(_PG_DEFAULTS.get("container_image", "postgres:16-alpine"))
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

    # Batch execution limits
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

    data_dir: Path = Path("data")
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
