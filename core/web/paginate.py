"""Paged output of long text: the model reads a page in chunks (offset) instead of getting a truncated middle."""

MIN_LIMIT = 1_000
MAX_LIMIT = 10_500   # the router cuts a tool result to 12,000 characters together with the header and links


def clamp_limit(limit: int, default: int = 9_000) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = default
    return max(MIN_LIMIT, min(limit, MAX_LIMIT))


def page_slice(text: str, offset: int, limit: int) -> str:
    """A chunk of text about limit characters long starting at offset, with a hint on how to read further.
    The end of the chunk moves to a line boundary if one is nearby."""
    total = len(text)
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    if offset >= total:
        return f"[offset={offset} is past the text: the page has {total} characters.]"
    end = min(total, offset + limit)
    if end < total:
        nl = text.rfind("\n", offset + limit * 4 // 5, end)
        if nl > offset:
            end = nl
    chunk = text[offset:end].strip("\n")
    if offset == 0 and end >= total:
        return chunk
    footer = f"[Showing characters {offset}–{end} of {total}."
    footer += f" To continue: web_extract with the same url and offset={end}.]" if end < total else " This is the end of the page.]"
    return f"{chunk}\n\n{footer}"
