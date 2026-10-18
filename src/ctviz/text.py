"""Number agreement for generated sentences ("1 trial has", "2 trials have")."""

from __future__ import annotations


def plural(count: int, noun: str, plural_noun: str | None = None) -> str:
    """'1 trial', '2 trials'; `plural_noun` for irregular plurals ('1 study', '2 studies')."""
    if count == 1:
        return f"{count} {noun}"
    return f"{count} {plural_noun or noun + 's'}"


def verb(count: int, singular: str, plural_form: str) -> str:
    """The verb form agreeing with `count` ('is'/'are', 'has'/'have')."""
    return singular if count == 1 else plural_form
