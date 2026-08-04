"""FN-bait: every violation here is planted and must be caught."""
import hashlib
import subprocess

import yaml


def append_item(item, bucket=[]):  # mutable default arg -> ruff B006
    bucket.append(item)
    return bucket


def parse_config(cfg_text):
    return yaml.load(cfg_text)  # unsafe yaml load -> bandit B506


def digest(payload):
    return hashlib.md5(payload).hexdigest()  # weak hash -> bandit B324


def run_cmd(cmd):
    subprocess.call(cmd, shell=True)  # shell=True -> bandit B602


def recieve_packet(sock):  # misspelled identifier -> spell layer
    try:
        return sock.recv(1024)
    except:  # bare except -> ruff E722
        return None


def read_lines(path):
    handle = open(path)  # resource leak -> LLM layer
    return handle.readlines()
