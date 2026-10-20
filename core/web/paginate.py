"""Постраничная выдача длинного текста: модель читает страницу кусками (offset), а не получает обрезанную середину."""

MIN_LIMIT = 1_000
MAX_LIMIT = 10_500   # результат инструмента режется роутером до 12 000 символов вместе с заголовком и ссылками


def clamp_limit(limit: int, default: int = 9_000) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = default
    return max(MIN_LIMIT, min(limit, MAX_LIMIT))


def page_slice(text: str, offset: int, limit: int) -> str:
    """Кусок text длиной около limit символов, начиная с offset, с подсказкой, как читать дальше.
    Конец куска сдвигается к границе строки, если она рядом."""
    total = len(text)
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    if offset >= total:
        return f"[offset={offset} за пределами текста: на странице {total} символов.]"
    end = min(total, offset + limit)
    if end < total:
        nl = text.rfind("\n", offset + limit * 4 // 5, end)
        if nl > offset:
            end = nl
    chunk = text[offset:end].strip("\n")
    if offset == 0 and end >= total:
        return chunk
    footer = f"[Показаны символы {offset}–{end} из {total}."
    footer += f" Продолжение: web_extract с тем же url и offset={end}.]" if end < total else " Это конец страницы.]"
    return f"{chunk}\n\n{footer}"
