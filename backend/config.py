"""Typed, validated runtime settings with environment-variable compatibility."""
from __future__ import annotations

import os
import json
import tempfile
from dataclasses import asdict, dataclass
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


def _at_least(name: str, value: int | float, minimum: int | float) -> None:
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")


def _settings(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    config_path = Path(os.getenv("CONFIG_PATH", "data/config.json"))
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
        for key, value in dotenv_values().items()
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
class Config:
    crawler: CrawlerConfig
    translator: TranslatorConfig
    calibre: CalibreConfig
    paths: PathsConfig
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
            db=Path(values.get("DB_PATH", "data/novelbridge.db")),
            output=Path(values.get("OUTPUT_DIR", "output")),
            data_dir=Path(values.get("DATA_DIR", "data")),
        )
        config = cls(
            crawler=crawler,
            translator=translator,
            calibre=calibre,
            paths=paths,
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
        destination = path or Path(os.getenv("CONFIG_PATH", "data/config.json"))
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


def get_config(environ: Mapping[str, str] | None = None) -> Config:
    return Config.from_env(environ)
