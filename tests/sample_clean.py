"""Clean sample — the pipeline must report ZERO violations here."""
from pathlib import Path


def total_of_valid(items: list[int | None]) -> int:
    """Sum non-null items."""
    return sum(item for item in items if item is not None)


def read_text(path: Path) -> str:
    """Read a file safely with an explicit encoding."""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def receive_batch(batch_idx: int, batch_cnt: int) -> str:
    """Abbreviations like idx/cnt must not trigger the spell layer."""
    return f"batch {batch_idx} of {batch_cnt}"
