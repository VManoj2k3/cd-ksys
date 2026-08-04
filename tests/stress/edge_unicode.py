# -*- coding: utf-8 -*-
"""Edge case: unicode identifiers, emoji, mixed scripts."""

GREETING = "こんにちは世界"
CAFÉ_MENU = ("espresso", "crème brûlée")


def grüße(name: str) -> str:
    # note: this comment has a planted recieve misspelling 🚀
    return f"{GREETING}, {name}! ☕ {CAFÉ_MENU[0]}"
