from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from .adapter import GraphStoreAdapter
from .embedder import EmbeddingService
from .models import RowEmbedding
from .scoring import temporal_score


@dataclass
class RetrievalResult:
    node_id: str
    node_type: str           # "Event" | "ConfPage" | "TableRow"
    text: str                # the text that was embedded
    timestamp: Optional[datetime]
    semantic_score: float
    temporal_score: float
    combined_score: float
    properties: dict


class SemanticRetriever:
    """
    Stage 6 retriever. Embeds a free-text query, runs cosine similarity
    against graph_nodes (Event, ConfPage) and graph_row_embeddings (TableRow),
    then re-ranks by combined_score = semantic × temporal_decay.
    """

    def __init__(self, adapter: GraphStoreAdapter, embedder: EmbeddingService) -> None:
        self._adapter  = adapter
        self._embedder = embedder

    async def search(
        self,
        query: str,
        node_types: Optional[List[str]] = None,
        include_table_rows: bool = True,
        before_date: Optional[datetime] = None,
        limit: int = 20,
        query_date: Optional[datetime] = None,
    ) -> List[RetrievalResult]:
        loop = asyncio.get_running_loop()
        query_embedding = await loop.run_in_executor(None, self._embedder.embed, query)

        candidate_limit = limit * 3
        results: List[RetrievalResult] = []

        types_to_search = node_types or ["Event", "ConfPage"]
        node_hits = await self._adapter.vector_search_nodes(
            query_embedding=query_embedding,
            node_types=types_to_search,
            before_date=before_date,
            limit=candidate_limit,
        )
        for node, sim in node_hits:
            ts = node.timestamp
            t_score = temporal_score(ts, query_date) if ts else 1.0
            results.append(RetrievalResult(
                node_id=node.id,
                node_type=node.type,
                text=node.embedding_text,
                timestamp=ts,
                semantic_score=sim,
                temporal_score=t_score,
                combined_score=sim * t_score,
                properties=node.properties,
            ))

        if include_table_rows:
            row_hits = await self._adapter.vector_search_rows(
                query_embedding=query_embedding,
                before_date=before_date,
                limit=candidate_limit,
            )
            for row, sim in row_hits:
                ts = row.timestamp
                t_score = temporal_score(ts, query_date) if ts else 1.0
                results.append(RetrievalResult(
                    node_id=row.id,
                    node_type="TableRow",
                    text=row.text,
                    timestamp=ts,
                    semantic_score=sim,
                    temporal_score=t_score,
                    combined_score=sim * t_score,
                    properties={
                        "table_id":  row.table_id,
                        "page_id":   row.page_id,
                        "row_index": row.row_index,
                    },
                ))

        results.sort(key=lambda r: r.combined_score, reverse=True)
        return results[:limit]
