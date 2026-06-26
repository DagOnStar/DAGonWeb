from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path
from typing import Any


DEFAULT_DAGON_INI = """[dagon_service]
route = http://localhost:57009
use = False

[ftp_pub]
ip = localhost

[batch]
threads = 1
remove_dir = False
"""


def read_dagon_ini(path: Path) -> dict[str, dict[str, str]]:
    """Read the application-managed DAGonStar INI without interpolation."""
    parser = ConfigParser(interpolation=None)
    if path.is_file():
        parser.read(path, encoding="utf-8")
    return {section: dict(parser.items(section, raw=True)) for section in parser.sections()}


def runtime_dagon_config(path: Path, scratch_dir: Path) -> dict[str, dict[str, Any]]:
    """Combine DAGonStar's editable settings with DAGonWeb safety invariants."""
    config: dict[str, dict[str, Any]] = read_dagon_ini(path)
    config.setdefault("batch", {}).update({"scratch_dir_base": str(scratch_dir), "remove_dir": False})
    config.setdefault("ftp_pub", {})
    config.setdefault("dagon_service", {}).setdefault("use", "False")
    return config


def parse_dagon_ini(content: str) -> dict[str, dict[str, str]]:
    parser = ConfigParser(interpolation=None)
    parser.read_string(content)
    return {section: dict(parser.items(section, raw=True)) for section in parser.sections()}


def dump_dagon_ini(config: dict[str, dict[str, Any]]) -> str:
    parser = ConfigParser(interpolation=None)
    for section, values in config.items():
        parser[section] = {key: str(value) for key, value in values.items()}
    from io import StringIO

    buffer = StringIO()
    parser.write(buffer)
    return buffer.getvalue()


def merge_workflow_dagon_config(base: dict[str, dict[str, Any]], workflow_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Apply the structured per-workflow DAGon settings to a base config."""
    merged = {section: dict(values) for section, values in base.items()}
    for section, values in workflow_config.items():
        if isinstance(values, dict):
            merged.setdefault(section, {}).update({str(key): value for key, value in values.items() if value not in (None, "")})
    return merged
