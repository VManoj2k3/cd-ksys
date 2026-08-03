"""Sample with known, intentional violations — used to verify the pipeline."""
import os
import json  # unused import -> ruff F401


def recieve_data(items):  # misspelled identifier -> spell layer
    # this occured because of a lenght mismatch  -> spell layer (comments)
    total = 0
    unused = 42  # unused variable -> ruff F841
    for item in items:
        if item == None:  # -> ruff E711
            continue
        total += item
    return total


def run_command(user_input):
    os.system("ls " + user_input)  # command injection -> bandit B605
    return eval(user_input)  # eval -> bandit B307


def load(path):
    f = open(path)  # resource leak: file never closed -> LLM layer
    data = f.read()
    return data
