"""Typed, validated runtime settings with environment-variable compatibility."""
from __future__ import annotations

import os
import sys
import json
import tempfile
from functools import lru_cache
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

_SECRET_NAMES = (
    "TOKENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "ORCAROUTER_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "XAI_API_KEY",
    "GROK_API_KEY",
)
_CONFIG_ENV_NAMES = (
    "CONFIG_PATH",
    "MAX_CONCURRENT_JOBS", "MAX_CONCURRENT_PER_DOMAIN", "MAX_SESSIONS_PER_ORIGIN",
    "MAX_ATTEMPTS", "REQUEST_TIMEOUT", "MIN_CHAPTER_CHARS",
    "TRANSLATOR_REQUEST_TIMEOUT", "TRANSLATOR_ENABLED", "CALIBRE_COMMAND",
    "CALIBRE_API_ENABLED", "CALIBRE_API_URL", "CALIBRE_API_FALLBACK_TO_LOCAL",
    "DB_PATH", "OUTPUT_DIR", "DATA_DIR", "OUTPUT_MAX_MB", "API_PORT",
    "DIAGNOSTICS_ALLOW_EXPORT", "LOG_LEVEL",
)


def base_dir() -> Path:
    """Directory the application reads its bundled/config files from."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resolve_path(path: Path | str) -> Path:
    """Resolve a possibly-relative path against the application base dir."""
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve()
    return (base_dir() / candidate).resolve()


def _config_file_path(environ: Mapping[str, str] | None = None) -> Path:
    config_path = (environ or {}).get("CONFIG_PATH", os.getenv("CONFIG_PATH", "data/config.json"))
    return resolve_path(config_path)


def _at_least(name: str, value: int | float, minimum: int | float) -> None:
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")


def _settings(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    config_path = _config_file_path(environ)
    values: dict[str, str] = {}
    if config_path.exists():
        stored = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(stored, dict):
            raise ValueError(f"{config_path} must contain a JSON object")
        fields = {
            ("crawler", "max_concurrent_jobs"): "MAX_CONCURRENT_JOBS",
            ("crawler", "max_sessions_per_origin"): "MAX_CONCURRENT_PER_DOMAIN",
            ("crawler", "max_attempts"): "MAX_ATTEMPTS",
            ("crawler", "request_timeout"): "REQUEST_TIMEOUT",
            ("crawler", "min_chapter_chars"): "MIN_CHAPTER_CHARS",
            ("translator", "request_timeout"): "TRANSLATOR_REQUEST_TIMEOUT",
            ("translator", "enabled"): "TRANSLATOR_ENABLED",
            ("calibre", "command"): "CALIBRE_COMMAND",
            ("calibre", "api_enabled"): "CALIBRE_API_ENABLED",
            ("calibre", "api_url"): "CALIBRE_API_URL",
            ("calibre", "api_fallback_to_local"): "CALIBRE_API_FALLBACK_TO_LOCAL",
            ("paths", "db"): "DB_PATH",
            ("paths", "output"): "OUTPUT_DIR",
            ("paths", "data_dir"): "DATA_DIR",
            ("diagnostics", "allow_export"): "DIAGNOSTICS_ALLOW_EXPORT",
            ("diagnostics", "log_level"): "LOG_LEVEL",
        }
        for (section, key), env_name in fields.items():
            section_values = stored.get(section, {})
            if isinstance(section_values, dict) and key in section_values:
                values[env_name] = str(section_values[key])
        for key, env_name in (("output_max_mb", "OUTPUT_MAX_MB"), ("api_port", "API_PORT")):
            if key in stored:
                values[env_name] = str(stored[key])
    values.update({
        key: value
        for key, value in dotenv_values(resolve_path(".env")).items()
        if value is not None
    })
    values.update(os.environ)
    if environ is not None:
        values.update(environ)
    return values


@dataclass(frozen=True)
class CrawlerConfig:
    max_concurrent_jobs: int
    max_sessions_per_origin: int
    max_attempts: int
    request_timeout: float
    min_chapter_chars: int


@dataclass(frozen=True)
class TranslatorConfig:
    request_timeout: float
    enabled: bool


@dataclass(frozen=True)
class CalibreConfig:
    command: str
    api_enabled: bool
    api_url: str
    api_fallback_to_local: bool


@dataclass(frozen=True)
class PathsConfig:
    db: Path
    output: Path
    data_dir: Path


@dataclass(frozen=True)
class DiagnosticsConfig:
    allow_export: bool
    log_level: str


@dataclass(frozen=True)
class Config:
    crawler: CrawlerConfig
    translator: TranslatorConfig
    calibre: CalibreConfig
    paths: PathsConfig
    diagnostics: DiagnosticsConfig
    output_max_mb: int
    api_port: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Config":
        values = _settings(environ)

        def integer(name: str, default: int) -> int:
            try:
                return int(values.get(name, str(default)))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be an integer") from exc

        def number(name: str, default: float) -> float:
            try:
                return float(values.get(name, str(default)))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be numeric") from exc

        def boolean(name: str, default: bool) -> bool:
            raw = values.get(name)
            if raw is None:
                return default
            lowered = raw.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"{name} must be a boolean")

        crawler = CrawlerConfig(
            max_concurrent_jobs=integer("MAX_CONCURRENT_JOBS", 3),
            max_sessions_per_origin=integer(
                "MAX_CONCURRENT_PER_DOMAIN",
                integer("MAX_SESSIONS_PER_ORIGIN", 2),
            ),
            max_attempts=integer("MAX_ATTEMPTS", 5),
            request_timeout=number("REQUEST_TIMEOUT", 30.0),
            min_chapter_chars=integer("MIN_CHAPTER_CHARS", 50),
        )
        translator = TranslatorConfig(
            request_timeout=number("TRANSLATOR_REQUEST_TIMEOUT", 900.0),
            enabled=boolean("TRANSLATOR_ENABLED", True),
        )
        calibre = CalibreConfig(
            command=values.get("CALIBRE_COMMAND", "ebook-convert"),
            api_enabled=boolean("CALIBRE_API_ENABLED", False),
            api_url=values.get("CALIBRE_API_URL", ""),
            api_fallback_to_local=boolean("CALIBRE_API_FALLBACK_TO_LOCAL", True),
        )
        paths = PathsConfig(
            db=resolve_path(values.get("DB_PATH", "data/novelbridge.db")),
            output=resolve_path(values.get("OUTPUT_DIR", "output")),
            data_dir=resolve_path(values.get("DATA_DIR", "data")),
        )
        log_level = values.get("LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, or WARNING")
        config = cls(
            crawler=crawler,
            translator=translator,
            calibre=calibre,
            paths=paths,
            diagnostics=DiagnosticsConfig(
                allow_export=boolean("DIAGNOSTICS_ALLOW_EXPORT", True),
                log_level=log_level,
            ),
            output_max_mb=integer("OUTPUT_MAX_MB", 2048),
            api_port=integer("API_PORT", 8000),
        )
        _at_least("MAX_CONCURRENT_JOBS", crawler.max_concurrent_jobs, 1)
        _at_least("MAX_CONCURRENT_PER_DOMAIN", crawler.max_sessions_per_origin, 1)
        _at_least("MAX_ATTEMPTS", crawler.max_attempts, 1)
        _at_least("REQUEST_TIMEOUT", crawler.request_timeout, 1)
        _at_least("MIN_CHAPTER_CHARS", crawler.min_chapter_chars, 1)
        _at_least("TRANSLATOR_REQUEST_TIMEOUT", translator.request_timeout, 1)
        _at_least("OUTPUT_MAX_MB", config.output_max_mb, 0)
        _at_least("API_PORT", config.api_port, 1)
        if config.api_port > 65535:
            raise ValueError("API_PORT must not exceed 65535")
        if config.calibre.api_enabled and not config.calibre.api_url:
            raise ValueError("CALIBRE_API_URL is required when CALIBRE_API_ENABLED is true")
        return config

    def to_public_dict(self) -> dict:
        result = asdict(self)
        for key, value in result["paths"].items():
            result["paths"][key] = str(value)
        values = _settings()
        result["secrets"] = {
            name: {"configured": bool(values.get(name, "").strip()), "value": "***"}
            for name in _SECRET_NAMES
        }
        return result

    def save(self, path: Path | None = None) -> None:
        destination = resolve_path(path) if path else _config_file_path()
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        for key, value in data["paths"].items():
            data["paths"][key] = str(value)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(data, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
        except OSError:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()
            raise


@lru_cache(maxsize=16)
def _cached_config(
    config_path: str,
    config_mtime_ns: int | None,
    dotenv_mtime_ns: int | None,
    environment: tuple[tuple[str, str], ...],
) -> Config:
    del config_path, config_mtime_ns, dotenv_mtime_ns, environment
    return Config.from_env()


def get_config(environ: Mapping[str, str] | None = None) -> Config:
    config_path = _config_file_path(environ)
    dotenv_path = resolve_path(".env")

    def mtime(path: Path) -> int | None:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None

    environment = dict(os.environ)
    if environ is not None:
        environment.update(environ)
    relevant_environment = tuple(
        sorted((key, environment[key]) for key in _CONFIG_ENV_NAMES if key in environment)
    )
    if environ is not None:
        # Preserve explicit mappings without mutating process-global state.
        return Config.from_env(environ)
    return _cached_config(
        str(config_path),
        mtime(config_path),
        mtime(dotenv_path),
        relevant_environment,
    )


def update_diagnostics_settings(allow_export: bool, log_level: str) -> Config:
    """Persist developer logging preferences and apply them to this process."""
    normalized_level = log_level.upper()
    if normalized_level not in {"DEBUG", "INFO", "WARNING"}:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, or WARNING")
    current = get_config()
    updated = replace(
        current,
        diagnostics=DiagnosticsConfig(
            allow_export=allow_export,
            log_level=normalized_level,
        ),
    )
    updated.save()
    os.environ["DIAGNOSTICS_ALLOW_EXPORT"] = str(allow_export).lower()
    os.environ["LOG_LEVEL"] = normalized_level
    _cached_config.cache_clear()
    return updated


def output_path() -> Path:
    """Single source of truth for the artifact output directory.

    Reads the live config (explicit OUTPUT_DIR env > config.json) on every
    call, so pipeline writers, artifact-download validation and the scrubber
    can never disagree about where artifacts live, and runtime changes to
    config.json are picked up immediately.
    """
    return resolve_path(get_config().paths.output)
