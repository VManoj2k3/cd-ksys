"""Labeled semantic bugs for the LLM layer — deterministic layers stay silent.

Every bug here is a real runtime defect on a specific line; used by
tests/accuracy_eval.py to measure LLM recall + fix quality on the GPU stack.
"""


def average(values):
    total = 0
    for value in values:
        total += value
    return total / len(values)  # BUG line 12: ZeroDivisionError on empty list


def sum_items(items):
    total = 0
    for i in range(len(items) + 1):  # BUG line 17: IndexError (off by one)
        total += items[i]
    return total


def read_settings(path):
    handle = open(path)  # BUG line 23: file handle never closed (leak)
    return handle.read()


def drop_zeros(numbers):
    for number in numbers:
        if number == 0:
            numbers.remove(number)  # BUG line 30: mutation while iterating
    return numbers


def find_user(users, name):
    for user in users:
        if user.name == name:
            return user
    return users[0]  # BUG line 37: IndexError when users is empty
