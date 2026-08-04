"""FP-bait: correct async code with careful resource handling."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

POLL_INTERVAL_S = 0.5
WORKER_COUNT = 4


async def read_all(paths: list[Path]) -> dict[str, str]:
    """Files opened via context managers — no resource leaks here."""
    results: dict[str, str] = {}
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            results[path.name] = handle.read()
        await asyncio.sleep(0)
    return results


async def worker(queue: asyncio.Queue, results: list[str]) -> None:
    while True:
        item = await queue.get()
        try:
            if item is None:
                return
            results.append(item.upper())
        finally:
            queue.task_done()


async def fan_out(items: list[str]) -> list[str]:
    queue: asyncio.Queue = asyncio.Queue()
    results: list[str] = []
    workers = [asyncio.create_task(worker(queue, results)) for _ in range(WORKER_COUNT)]
    for item in items:
        await queue.put(item)
    await queue.join()
    for _ in workers:
        await queue.put(None)
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(*workers)
    return results
