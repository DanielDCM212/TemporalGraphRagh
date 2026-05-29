from __future__ import annotations

import asyncio
import logging
from typing import List

from ..entity_extraction.models import ExtractedEvent
from ..parser.models import ContentTree, ParsedTable

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "models/text-embedding-004"


class EmbeddingService:
    """Lazy wrapper around Google GenerativeAI text embeddings (auth via ADC)."""

    def __init__(self, model: str = _DEFAULT_MODEL) -> None:
        self._model = model
        self._embedder = None

    def _get_embedder(self):
        if self._embedder is None:
            from ..vertex_auth import get_embeddings
            self._embedder = get_embeddings(model=self._model)
        return self._embedder

    def embed(self, text: str) -> List[float]:
        return self._get_embedder().embed_query(text)

    def embed_many(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        return self._get_embedder().embed_documents(texts)

    async def aembed_many(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.embed_many, texts)


# ── Text builders ──────────────────────────────────────────────────────────

def build_page_text(content_tree: ContentTree) -> str:
    parts = [
        f"Title: {content_tree.page_title}",
        f"Date: {content_tree.page_date.strftime('%Y-%m-%d')}",
    ]
    body = " ".join(
        chunk.content for chunk in content_tree.text_blocks if chunk.content.strip()
    )
    if body:
        parts.append(body)
    return "\n".join(parts)


def build_event_text(event: ExtractedEvent, page_title: str, page_date) -> str:
    parts = [f"[{event.event_type.value}] {event.description}"]
    parts.append(f"Page: {page_title} | Date: {page_date.strftime('%Y-%m-%d')}")
    if event.app_ids:
        parts.append(f"Apps: {', '.join(event.app_ids)}")
    if event.project_ids:
        parts.append(f"Projects: {', '.join(event.project_ids)}")
    return "\n".join(parts)


def build_row_text(headers: List[str], row: list) -> str:
    parts = []
    for i, cell in enumerate(row):
        header = headers[i] if i < len(headers) else f"col_{i}"
        cell_text = " ".join(
            tc.content for tc in cell.text_chunks if tc.content.strip()
        )
        if cell_text:
            parts.append(f"{header}: {cell_text}")
    return " | ".join(parts)


def build_attachment_chunk_text(
    filename: str,
    chunk: str,
    page_title: str,
    page_date,
) -> str:
    """
    Add light context around an attachment text chunk so it retrieves well
    even without the surrounding document.
    """
    return (
        f"File: {filename}\n"
        f"Page: {page_title} | Date: {page_date.strftime('%Y-%m-%d')}\n"
        f"{chunk}"
    )
