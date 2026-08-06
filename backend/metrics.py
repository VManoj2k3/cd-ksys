"""Minimal Prometheus-text metrics, no external dependency.

Counters and gauges are process-local (the service is single-process by
design — the in-memory job store requires one worker). Rendered on demand
by GET /api/metrics.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
_gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
_help: dict[str, tuple[str, str]] = {}  # name -> (type, help text)


def describe(name: str, kind: str, help_text: str) -> None:
    _help[name] = (kind, help_text)


def _key(name: str, labels: dict[str, str] | None):
    return name, tuple(sorted((labels or {}).items()))


def inc(name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
    k = _key(name, labels)
    with _lock:
        _counters[k] = _counters.get(k, 0.0) + value


def set_gauge(name: str, value: float, labels: dict[str, str] | None = None) -> None:
    with _lock:
        _gauges[_key(name, labels)] = float(value)


def observe(name: str, seconds: float) -> None:
    """Maintain <name>_sum and <name>_count (enough for rate/avg dashboards)."""
    with _lock:
        for suffix, v in ((f"{name}_sum", seconds), (f"{name}_count", 1.0)):
            k = (suffix, ())
            _counters[k] = _counters.get(k, 0.0) + v


def _fmt(value: float) -> str:
    return str(int(value)) if value == int(value) else repr(value)


def render() -> str:
    with _lock:
        counters = dict(_counters)
        gauges = dict(_gauges)
    lines: list[str] = []
    emitted_help: set[str] = set()

    def emit(store: dict, default_kind: str) -> None:
        for (name, labels) in sorted(store):
            base = name[:-4] if name.endswith("_sum") else \
                name[:-6] if name.endswith("_count") else name
            if base in _help and base not in emitted_help:
                kind, text = _help[base]
                lines.append(f"# HELP {base} {text}")
                lines.append(f"# TYPE {base} {kind}")
                emitted_help.add(base)
            label_str = ""
            if labels:
                inner = ",".join(f'{k}="{v}"' for k, v in labels)
                label_str = "{" + inner + "}"
            lines.append(f"{name}{label_str} {_fmt(store[(name, labels)])}")

    emit(counters, "counter")
    emit(gauges, "gauge")
    return "\n".join(lines) + "\n"


def reset_for_tests() -> None:
    with _lock:
        _counters.clear()
        _gauges.clear()
