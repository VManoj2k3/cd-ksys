"""Same 5 semantic bugs as py_semantic_bugs.py, a DIFFERENT but PEP8-clean
style: while-loops instead of for, index-based iteration, different naming,
docstrings instead of trailing comments. Deterministically clean, so it
isolates LLM recall across coding style on a live stack (accuracy_eval)."""


def mean_of(series):
    """Arithmetic mean. BUG line 10: ZeroDivisionError on an empty series."""
    running = 0
    idx = 0
    while idx < len(series):
        running += series[idx]
        idx += 1
    return running / len(series)


def accumulate(elements):
    """Total of all elements. BUG line 20: range overshoots by one (IndexError)."""
    total = 0
    position = 0
    while position <= len(elements):
        total += elements[position]
        position += 1
    return total


def slurp(location):
    """Return file contents. BUG line 28: the handle is never closed (leak)."""
    descriptor = open(location)
    return descriptor.read()


def without_zeros(sequence):
    """Strip zero entries. BUG line 35: removing during iteration skips items."""
    for entry in sequence:
        if entry == 0:
            sequence.remove(entry)
    return sequence


def lookup(records, wanted):
    """Find a record. BUG line 44: records[0] raises when records is empty."""
    for record in records:
        if record == wanted:
            return record
    return records[0]
