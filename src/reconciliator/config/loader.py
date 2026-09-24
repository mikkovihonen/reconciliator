"""Configuration file loading, modular resolution, and logging initialization."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

from reconciliator.config.app import AppConfig
from reconciliator.config.infrastructure import LoggingConfig


def _get_source_stem(p: Path) -> str:
    name = p.name
    for suffix in [".example.yaml", ".example.yml", ".yaml", ".yml"]:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return p.stem


def _load_source_file(file_path: Path) -> dict[str, Any]:
    with file_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"Expected mapping in source config file {file_path}, got {type(data).__name__}"
        )
    return data


def _load_modular_file(file_val: Any, base_dir: Path) -> dict[str, Any]:
    """Load settings from an external YAML file or return dict."""
    if isinstance(file_val, (str, Path)):
        p = Path(file_val)
        target = (base_dir / p).resolve() if not p.is_absolute() else p
        if not target.is_file():
            raise FileNotFoundError(
                f"Configuration file not found: {file_val} (resolved to {target})"
            )
        loaded = _load_source_file(target)
        return loaded
    elif isinstance(file_val, dict):
        return file_val
    return {}


def _resolve_source_entry(
    val: Any, base_dir: Path, default_key: Optional[str] = None
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(val, str):
        target = (base_dir / val).resolve() if not Path(val).is_absolute() else Path(val)
        if target.is_dir():
            files = sorted(list(target.glob("*.yaml")) + list(target.glob("*.yml")))
            for f in files:
                loaded = _load_source_file(f)
                if (
                    len(loaded) == 1
                    and isinstance(next(iter(loaded.values())), dict)
                    and not any(
                        k in loaded
                        for k in ("enabled", "source_type", "api_url", "output_dir", "input_path")
                    )
                ):
                    result.update(loaded)
                else:
                    result[_get_source_stem(f)] = loaded
        elif "*" in val:
            for f in sorted(base_dir.glob(val)):
                loaded = _load_source_file(f)
                if (
                    len(loaded) == 1
                    and isinstance(next(iter(loaded.values())), dict)
                    and not any(
                        k in loaded
                        for k in ("enabled", "source_type", "api_url", "output_dir", "input_path")
                    )
                ):
                    result.update(loaded)
                else:
                    result[_get_source_stem(f)] = loaded
        elif target.is_file():
            loaded = _load_source_file(target)
            if (
                len(loaded) == 1
                and isinstance(next(iter(loaded.values())), dict)
                and not any(
                    k in loaded
                    for k in ("enabled", "source_type", "api_url", "output_dir", "input_path")
                )
            ):
                result.update(loaded)
            elif default_key:
                result[default_key] = loaded
            else:
                result[_get_source_stem(target)] = loaded
        else:
            raise FileNotFoundError(
                f"Source configuration file or directory not found: {val} (resolved to {target})"
            )
    elif isinstance(val, dict):
        if default_key and any(
            k in val for k in ("enabled", "source_type", "api_url", "output_dir", "input_path")
        ):
            result[default_key] = val
        else:
            result.update(val)

    for s_name, s_data in list(result.items()):
        if isinstance(s_data, dict) and "normalization" in s_data:
            if isinstance(s_data["normalization"], (str, Path)):
                s_data["normalization"] = _load_modular_file(s_data["normalization"], base_dir)

    return result


def _load_modular_sources(section_data: Any, base_dir: Path) -> dict[str, Any]:
    if section_data is None:
        return {}
    if isinstance(section_data, str):
        return _resolve_source_entry(section_data, base_dir)
    elif isinstance(section_data, list):
        merged: dict[str, Any] = {}
        for item in section_data:
            merged.update(_resolve_source_entry(item, base_dir))
        return merged
    elif isinstance(section_data, dict):
        merged = {}
        for k, v in section_data.items():
            if isinstance(v, str):
                loaded = _resolve_source_entry(v, base_dir, default_key=k)
                merged.update(loaded)
            else:
                merged[k] = v
        return merged
    return dict(section_data) if isinstance(section_data, dict) else {}


def _resolve_config_paths(config: AppConfig, base_dir: Path) -> AppConfig:
    """Resolve all relative paths in AppConfig relative to base_dir."""

    def resolve_p(p: Optional[Path]) -> Optional[Path]:
        if p is None:
            return None
        if p.is_absolute():
            return p
        return (base_dir / p).resolve()

    # Storage paths
    config.storage.data_dir = resolve_p(config.storage.data_dir)  # type: ignore[assignment]
    config.storage.work_index = resolve_p(config.storage.work_index)
    config.storage.manifestation_index = resolve_p(config.storage.manifestation_index)
    config.storage.agent_index = resolve_p(config.storage.agent_index)

    # Embedding template
    if config.embedding.template_path:
        t_path = config.embedding.template_path
        if not t_path.is_absolute():
            candidate = (base_dir / t_path).resolve()
            if candidate.exists():
                config.embedding.template_path = candidate
            else:
                pkg_template = (
                    Path(__file__).resolve().parent.parent / "embedding" / "templates" / t_path.name
                )
                if pkg_template.exists():
                    config.embedding.template_path = pkg_template
                else:
                    config.embedding.template_path = candidate

    # Download configs
    for skos_cfg in config.ingestion.skosmos.values():
        if skos_cfg.output_dir:
            skos_cfg.output_dir = resolve_p(skos_cfg.output_dir)  # type: ignore[assignment]

    # Ingestion configs
    if config.ingestion.vocab and config.ingestion.vocab.input_path:
        config.ingestion.vocab.input_path = resolve_p(config.ingestion.vocab.input_path)  # type: ignore[assignment]

    for oai_cfg in config.ingestion.oai.values():
        if oai_cfg.input_path:
            oai_cfg.input_path = resolve_p(oai_cfg.input_path)

    if config.ingestion.wikidata and config.ingestion.wikidata.dump_path:
        config.ingestion.wikidata.dump_path = resolve_p(config.ingestion.wikidata.dump_path)  # type: ignore[assignment]

    # Reconciliation paths
    if config.reconcile:
        config.reconcile.target_file = resolve_p(config.reconcile.target_file)  # type: ignore[assignment]
        if config.reconcile.output_file:
            config.reconcile.output_file = resolve_p(config.reconcile.output_file)
        if config.reconcile.reports_dir:
            config.reconcile.reports_dir = resolve_p(config.reconcile.reports_dir)
        if config.reconcile.agent_reports:
            config.reconcile.agent_reports.detailed_file = resolve_p(
                config.reconcile.agent_reports.detailed_file
            )  # type: ignore[assignment]
            config.reconcile.agent_reports.summary_file = resolve_p(
                config.reconcile.agent_reports.summary_file
            )  # type: ignore[assignment]
        if config.reconcile.subject_reports:
            config.reconcile.subject_reports.detailed_file = resolve_p(
                config.reconcile.subject_reports.detailed_file
            )  # type: ignore[assignment]
            config.reconcile.subject_reports.summary_file = resolve_p(
                config.reconcile.subject_reports.summary_file
            )  # type: ignore[assignment]
        if config.reconcile.work_reports:
            config.reconcile.work_reports.detailed_file = resolve_p(
                config.reconcile.work_reports.detailed_file
            )  # type: ignore[assignment]
            config.reconcile.work_reports.summary_file = resolve_p(
                config.reconcile.work_reports.summary_file
            )  # type: ignore[assignment]
        if config.reconcile.expression_reports:
            config.reconcile.expression_reports.detailed_file = resolve_p(
                config.reconcile.expression_reports.detailed_file
            )  # type: ignore[assignment]
            config.reconcile.expression_reports.summary_file = resolve_p(
                config.reconcile.expression_reports.summary_file
            )  # type: ignore[assignment]
        if config.reconcile.templates:
            config.reconcile.templates.work = resolve_p(config.reconcile.templates.work)  # type: ignore[assignment]
            config.reconcile.templates.expression = resolve_p(config.reconcile.templates.expression)  # type: ignore[assignment]
            config.reconcile.templates.manifestation = resolve_p(
                config.reconcile.templates.manifestation
            )  # type: ignore[assignment]
            config.reconcile.templates.agent = resolve_p(config.reconcile.templates.agent)  # type: ignore[assignment]

    # Logging path
    if config.logging and config.logging.log_file:
        config.logging.log_file = resolve_p(config.logging.log_file)

    return config


def configure_logging(logging_cfg: Optional[LoggingConfig | AppConfig] = None) -> None:
    """Configure standard logging handlers, formatters, and root level based on configuration."""
    from reconciliator.logging import TaskLogFilter, TaskLogFormatter, TelemetryLogHandler

    if isinstance(logging_cfg, AppConfig):
        cfg = logging_cfg.logging
    elif isinstance(logging_cfg, LoggingConfig):
        cfg = logging_cfg
    else:
        cfg = LoggingConfig()

    root_logger = logging.getLogger()
    level_num = getattr(logging, cfg.level, logging.INFO)
    root_logger.setLevel(level_num)

    # Ensure clean default LogRecord factory
    logging.setLogRecordFactory(logging.LogRecord)

    task_filter = TaskLogFilter(default_task_id=cfg.task_id_default)
    # Attach TaskLogFilter to ensure record.task_id is always present
    for f in list(root_logger.filters):
        if isinstance(f, TaskLogFilter):
            root_logger.removeFilter(f)
    root_logger.addFilter(task_filter)

    formatter = TaskLogFormatter(
        fmt=cfg.format, datefmt=cfg.date_format, default_task_id=cfg.task_id_default
    )

    # Remove existing handlers (preserving pytest LogCaptureHandler if present)
    for handler in list(root_logger.handlers):
        if handler.__class__.__name__ == "LogCaptureHandler":
            continue
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(level_num)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(task_filter)
    root_logger.addHandler(stream_handler)

    if cfg.log_file:
        log_path = Path(cfg.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
        file_handler.setLevel(level_num)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(task_filter)
        root_logger.addHandler(file_handler)

    if cfg.enable_telemetry_handler:
        telemetry_handler = TelemetryLogHandler()
        root_logger.addHandler(telemetry_handler)

    logging.captureWarnings(cfg.capture_warnings)


def _validate_constituent_sources(config: AppConfig) -> None:
    """Resolve every source's constituent profile and extractor, once, here."""
    from reconciliator.constituents import resolve_for_source

    for name, source in (config.ingestion.oai or {}).items():
        resolve_for_source(
            name,
            source.constituents,
            source.constituents_mode,
            default_profile=config.constituents.profile,
            default_mode=config.constituents.mode,
            config_dir=config.config_dir,
        )


def load_config(config_path: Path | str, configure_log: bool = False) -> AppConfig:
    """Load, validate, and resolve paths for a Reconciliator YAML configuration file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        try:
            raw_data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"YAML parsing error in {path}: {e}") from e

    base_dir = path.parent
    if "ingestion" in raw_data and isinstance(raw_data["ingestion"], dict):
        ing = raw_data["ingestion"]
        if "skosmos" in ing:
            ing["skosmos"] = _load_modular_sources(ing["skosmos"], base_dir)
        if "oai" in ing:
            ing["oai"] = _load_modular_sources(ing["oai"], base_dir)
        if "vocab" in ing and isinstance(ing["vocab"], dict):
            if "normalization" in ing["vocab"] and ing["vocab"]["normalization"] is not None:
                ing["vocab"]["normalization"] = _load_modular_file(
                    ing["vocab"]["normalization"], base_dir
                )
        if "wikidata" in ing and isinstance(ing["wikidata"], dict):
            if "normalization" in ing["wikidata"] and ing["wikidata"]["normalization"] is not None:
                ing["wikidata"]["normalization"] = _load_modular_file(
                    ing["wikidata"]["normalization"], base_dir
                )

    if "reconcile" in raw_data and isinstance(raw_data["reconcile"], dict):
        if (
            "normalization" in raw_data["reconcile"]
            and raw_data["reconcile"]["normalization"] is not None
        ):
            raw_data["reconcile"]["normalization"] = _load_modular_file(
                raw_data["reconcile"]["normalization"], base_dir
            )
    if "record_normalization" in raw_data and isinstance(
        raw_data["record_normalization"], (str, Path)
    ):
        raw_data["record_normalization"] = _load_modular_file(
            raw_data["record_normalization"], base_dir
        )
    if "vocabularies" in raw_data and isinstance(raw_data["vocabularies"], (str, Path)):
        raw_data["vocabularies"] = _load_modular_file(raw_data["vocabularies"], base_dir)
    if "linguistic" in raw_data and isinstance(raw_data["linguistic"], (str, Path)):
        raw_data["linguistic"] = _load_modular_file(raw_data["linguistic"], base_dir)

    config = AppConfig.model_validate(raw_data)
    resolved = _resolve_config_paths(config, base_dir)
    resolved.config_dir = base_dir.resolve()
    if resolved.reconcile is not None:
        resolved.reconcile.config_dir = resolved.config_dir
        if "funnels" in raw_data and isinstance(raw_data["funnels"], dict):
            resolved.reconcile.funnel_settings = resolved.funnels
    _validate_constituent_sources(resolved)
    if configure_log:
        configure_logging(resolved.logging)
    return resolved
