"""Central configuration loader. Every tunable comes from config.yaml / .env.

Nothing elsewhere in the codebase may hardcode a tunable value; modules
import `CFG` (a nested dict wrapper) and read from it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = Path(os.environ.get("KOOSYS_CONFIG", PROJECT_ROOT / "config.yaml"))


class Cfg:
    """Dotted-path access wrapper over the YAML config with defaults."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def raw(self) -> dict[str, Any]:
        return self._data


def load_config(path: Path | None = None) -> Cfg:
    p = path or _CONFIG_PATH
    with open(p, "r", encoding="utf-8") as fh:
        return Cfg(yaml.safe_load(fh) or {})


CFG = load_config()
