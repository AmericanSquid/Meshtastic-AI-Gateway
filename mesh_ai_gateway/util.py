from __future__ import annotations


def truncate_utf8(text: str, max_bytes: int) -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    suffix = "…"
    budget = max(1, max_bytes - len(suffix.encode("utf-8")))
    out: list[str] = []
    used = 0
    for char in text:
        size = len(char.encode("utf-8"))
        if used + size > budget:
            break
        out.append(char)
        used += size
    return "".join(out).rstrip() + suffix


def split_utf8(text: str, max_bytes: int) -> list[str]:
    if not text:
        return [""]
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]

    # Reserve room for a prefix such as "(12/12) ".
    payload_budget = max(16, max_bytes - 12)
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for char in text:
        size = len(char.encode("utf-8"))
        if current and current_bytes + size > payload_budget:
            chunks.append("".join(current).strip())
            current = []
            current_bytes = 0
        current.append(char)
        current_bytes += size
    if current:
        chunks.append("".join(current).strip())

    if len(chunks) == 1:
        return chunks
    total = len(chunks)
    return [f"({index}/{total}) {chunk}" for index, chunk in enumerate(chunks, start=1)]
