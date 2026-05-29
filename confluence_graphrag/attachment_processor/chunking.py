from __future__ import annotations

from typing import List


def chunk_text(text: str, size: int = 1000, overlap: int = 150) -> List[str]:
    """
    Split *text* into chunks of at most *size* characters with *overlap*
    characters of context carried over from the previous chunk.

    Tries to break on whitespace when possible so chunks don't start
    mid-word.  Returns an empty list for blank input.
    """
    text = text.strip()
    if not text:
        return []

    if len(text) <= size:
        return [text]

    chunks: List[str] = []
    start = 0

    while start < len(text):
        end = start + size

        if end < len(text):
            # Try to break on the last whitespace before end
            ws = text.rfind(" ", start, end)
            if ws > start:
                end = ws

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        # Advance, keeping the overlap
        start = end - overlap
        if start <= 0:
            break

    return chunks
