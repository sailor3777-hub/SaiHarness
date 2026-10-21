"""Strings the code reads meaning from: the tool error prefix, "file not found".

No Qt and no third-party imports: both the window and the tools_server.py process import this module. Texts with
these prefixes are already stored in users' databases (chat history), so earlier wordings are recognized along
with the current ones: extend LEGACY_* whenever a wording changes.
"""

ERROR_WORD = "ERROR"                  # a tool result that failed starts with this word
ERROR_PREFIX = ERROR_WORD + ": "
LEGACY_ERROR_WORDS: tuple[str, ...] = ("ОШИБКА",)   # Russian version

NOT_FOUND = "file not found"          # skip reason for a file (core.files, core.docs); tools_server adds a hint for it


def error(text: str) -> str:
    """A "did not work" tool result: the model and the chat recognize it by the prefix."""
    return ERROR_PREFIX + text


def is_error(text: str) -> bool:
    """The tool result starts with the error prefix (current or from older history)."""
    return (text or "").startswith(tuple(word + ": " for word in (ERROR_WORD, *LEGACY_ERROR_WORDS)))
