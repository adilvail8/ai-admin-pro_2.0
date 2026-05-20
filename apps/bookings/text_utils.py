"""Text-level helpers shared by intent detection, master matching, and
affirmative-signal checks.

Pure functions, no Django or model imports — safe to depend on from
anywhere in the app without introducing import cycles.
"""


def _repair_mojibake(value: str) -> str:
    """Attempt to recover original UTF-8 text from a string that was decoded
    as latin-1 by mistake (the classic CP1251→UTF-8 corruption pattern
    leaving sequences like ``Р°``/``С‚`` in Cyrillic data).

    Returns the lowercase repaired string when round-tripping through
    latin-1/utf-8 succeeds, otherwise returns ``value`` unchanged.
    """
    try:
        return value.encode("latin1").decode("utf-8").lower()
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
