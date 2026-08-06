"""Central configuration loader. Every tunable comes from config.yaml / .env.

Nothing elsewhere in the codebase may hardcode a tunable value; modules
import `CFG` (a nested dict wrapper) and read from it.

Loading order:
1. `config.yaml` at the project root (the base — documents every tunable).
2. If KOOSYS_CONFIG is set, that file REPLACES the base entirely.
3. If KOOSYS_CONFIG_OVERLAY is set, that file is DEEP-MERGED over the result —
   deployment configs only need the keys they change, so they can never drift
   out of sync with the base when new tunables are added.

`validate_config()` is the startup gate: it returns hard errors (refuse to
start) and warnings (log loudly, keep going).
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
_OVERLAY_PATH = os.environ.get("KOOSYS_CONFIG_OVERLAY", "")


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


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Return base with overlay merged on top. Dicts merge recursively;
    everything else (scalars, lists) is replaced by the overlay value."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def load_config(path: Path | None = None) -> Cfg:
    data = _read_yaml(path or _CONFIG_PATH)
    if _OVERLAY_PATH:
        data = _deep_merge(data, _read_yaml(Path(_OVERLAY_PATH)))
    return Cfg(data)


# ------------------------------------------------------------------ validation
_KNOWN_LANGUAGES = {"python", "c", "cpp", "java", "typescript"}
_AUTH_MODES = {"none", "ldap", "token"}
_LOG_LEVELS = {"debug", "info", "warning", "error", "critical"}


def _pos_int(cfg: Cfg, key: str, errors: list[str], minimum: int = 1) -> int | None:
    raw = cfg.get(key)
    if raw is None:
        return None  # missing = use code default, not an error
    try:
        val = int(raw)
    except (TypeError, ValueError):
        errors.append(f"{key}: expected an integer, got {raw!r}")
        return None
    if val < minimum:
        errors.append(f"{key}: must be >= {minimum}, got {val}")
        return None
    return val


def validate_config(cfg: Cfg) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors mean: refuse to start."""
    errors: list[str] = []
    warnings: list[str] = []

    # ---- server ----
    host = cfg.get("server.host")
    if not host or not str(host).strip():
        errors.append("server.host: must be set (e.g. 127.0.0.1)")
    port = cfg.get("server.port")
    try:
        if not (1 <= int(port) <= 65535):
            errors.append(f"server.port: must be 1-65535, got {port}")
    except (TypeError, ValueError):
        errors.append(f"server.port: expected an integer, got {port!r}")
    for key in ("server.max_file_size_kb", "server.job_ttl_seconds",
                "server.max_request_kb", "server.gc_interval_seconds",
                "tools.timeout_seconds", "review_max_seconds"):
        _pos_int(cfg, key, errors)

    concurrent = _pos_int(cfg, "server.max_concurrent_reviews", errors)
    active = _pos_int(cfg, "server.max_active_jobs", errors)
    stored = _pos_int(cfg, "server.max_jobs_in_memory", errors)
    if concurrent and active and active < concurrent:
        errors.append("server.max_active_jobs: must be >= server.max_concurrent_reviews")
    if active and stored and stored < active:
        errors.append("server.max_jobs_in_memory: must be >= server.max_active_jobs")
    _pos_int(cfg, "server.reviews_per_user_per_minute", errors)

    # ---- auth ----
    mode = str(cfg.get("auth.mode", "none")).lower()
    if mode not in _AUTH_MODES:
        errors.append(f"auth.mode: must be one of {sorted(_AUTH_MODES)}, got {mode!r}")
    secret = cfg.get("auth.session_secret", "") or os.environ.get(
        "KOOSYS_SESSION_SECRET", "")
    if mode in ("ldap", "token"):
        if not secret:
            errors.append(
                "auth.session_secret (or env KOOSYS_SESSION_SECRET) is required "
                f"when auth.mode={mode} — generate one with: openssl rand -hex 32")
        elif len(str(secret)) < 32:
            warnings.append("auth.session_secret: shorter than 32 chars — use a "
                            "long random value (openssl rand -hex 32)")
        if not cfg.get("auth.cookie_secure", False):
            warnings.append("auth.cookie_secure is false — set it to true when the "
                            "service is served over HTTPS (reverse proxy)")
    if mode == "token" and not (cfg.get("auth.shared_token", "")
                                or os.environ.get("KOOSYS_SHARED_TOKEN", "")):
        errors.append("auth.shared_token (or env KOOSYS_SHARED_TOKEN) is required "
                      "when auth.mode=token")
    if mode == "ldap":
        if not cfg.get("auth.ldap.server_uri", ""):
            errors.append("auth.ldap.server_uri is required when auth.mode=ldap")
        if not cfg.get("auth.ldap.bind_dn_template", "") and \
                not cfg.get("auth.ldap.base_dn", ""):
            errors.append("auth.ldap: set bind_dn_template (AD) or base_dn "
                          "(search-then-bind) — both are empty")
        if not cfg.get("auth.ldap.tls_verify", True):
            warnings.append("auth.ldap.tls_verify is false — LDAP server "
                            "certificate is NOT verified")
    if mode == "none" and str(host) not in ("127.0.0.1", "localhost", "::1"):
        warnings.append(f"auth.mode=none with server.host={host} — the service "
                        "is open to the network without authentication")
    _pos_int(cfg, "auth.session_ttl_seconds", errors)
    _pos_int(cfg, "auth.login_max_attempts", errors)
    _pos_int(cfg, "auth.login_window_seconds", errors)

    # ---- languages ----
    enabled = cfg.get("languages.enabled", [])
    if not isinstance(enabled, list) or not enabled:
        errors.append("languages.enabled: must be a non-empty list")
    else:
        unknown = [lang for lang in enabled if lang not in _KNOWN_LANGUAGES]
        if unknown:
            errors.append(f"languages.enabled: unknown languages {unknown} "
                          f"(known: {sorted(_KNOWN_LANGUAGES)})")

    # ---- llm ----
    if not cfg.get("llm.mock", False) and not cfg.get("llm.base_url", ""):
        errors.append("llm.base_url: required unless llm.mock is true")
    for key in ("llm.timeout_seconds", "llm.max_tokens_review",
                "llm.max_tokens_verify", "llm.max_tokens_fix",
                "llm.max_parallel_requests", "llm.health_cache_seconds"):
        _pos_int(cfg, key, errors)
    chunk = _pos_int(cfg, "llm.chunk_lines", errors)
    if chunk is not None and chunk < 20:
        errors.append(f"llm.chunk_lines: must be >= 20, got {chunk}")

    # ---- review budgets ----
    fix_budget = _pos_int(cfg, "review.fix_budget_seconds", errors)
    review_cap = cfg.get("review_max_seconds")
    try:
        if fix_budget and review_cap and fix_budget >= int(review_cap):
            warnings.append("review.fix_budget_seconds >= review_max_seconds — "
                            "the fix stage can consume the whole review budget")
    except (TypeError, ValueError):
        pass

    # ---- logging ----
    level = str(cfg.get("logging.level", "info")).lower()
    if level not in _LOG_LEVELS:
        errors.append(f"logging.level: must be one of {sorted(_LOG_LEVELS)}, "
                      f"got {level!r}")
    fmt = str(cfg.get("logging.format", "plain")).lower()
    if fmt not in ("plain", "json"):
        errors.append(f"logging.format: must be 'plain' or 'json', got {fmt!r}")

    return errors, warnings


CFG = load_config()
