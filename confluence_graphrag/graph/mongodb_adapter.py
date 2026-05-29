from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional, Tuple

import numpy as np
from pymongo import AsyncMongoClient

from .adapter import GraphStoreAdapter
from .models import GraphEdge, GraphNode, RowEmbedding

logger = logging.getLogger(__name__)

# Node types owned by a single page — safe to soft-delete on re-ingest.
# Application and Project nodes are global and persist across pages.
_PAGE_OWNED_TYPES = {"ConfPage", "Table", "Event"}


class MongoDBAdapter(GraphStoreAdapter):
    """
    Graph store backed by two MongoDB collections: graph_nodes + graph_edges.

    Document layout for graph_nodes:
    {
        "_id":        <node.id>,
        "type":       <node.type>,
        "timestamp":  <datetime | null>,
        "is_deleted": <bool>,
        "properties": { ...node.properties }
    }
    """

    def __init__(self, connection_string: str, database: str) -> None:
        self._client     = AsyncMongoClient(connection_string)
        self._db         = self._client[database]
        self.nodes       = self._db["graph_nodes"]
        self.edges       = self._db["graph_edges"]
        self.row_embeddings = self._db["graph_row_embeddings"]

    # ------------------------------------------------------------------
    # Index setup (call once at startup)
    # ------------------------------------------------------------------

    async def ensure_indexes(self) -> None:
        import pymongo
        await self.nodes.create_index([("type", pymongo.ASCENDING)])
        await self.nodes.create_index([("is_deleted", pymongo.ASCENDING)])
        await self.nodes.create_index([("timestamp", pymongo.DESCENDING)])
        await self.nodes.create_index([("properties.page_id", pymongo.ASCENDING)])
        await self.nodes.create_index([("properties.app_id", pymongo.ASCENDING)])
        await self.nodes.create_index([("properties.project_id", pymongo.ASCENDING)])
        # Partial index to find embeddable nodes efficiently
        await self.nodes.create_index(
            [("type", pymongo.ASCENDING), ("timestamp", pymongo.DESCENDING)],
            partialFilterExpression={"embedding": {"$exists": True}},
            name="nodes_with_embedding",
        )
        await self.edges.create_index([("source_id", pymongo.ASCENDING)])
        await self.edges.create_index([("target_id", pymongo.ASCENDING)])
        await self.edges.create_index([("relation", pymongo.ASCENDING)])
        # graph_row_embeddings
        await self.row_embeddings.create_index([("page_id", pymongo.ASCENDING)])
        await self.row_embeddings.create_index([("table_id", pymongo.ASCENDING)])
        await self.row_embeddings.create_index([("timestamp", pymongo.DESCENDING)])
        await self.row_embeddings.create_index([("is_deleted", pymongo.ASCENDING)])

    # ------------------------------------------------------------------
    # GraphStoreAdapter implementation
    # ------------------------------------------------------------------

    async def upsert_node(self, node: GraphNode) -> str:
        doc = {
            "_id":        node.id,
            "type":       node.type,
            "timestamp":  node.timestamp,
            "is_deleted": node.is_deleted,
            "properties": node.properties,
        }
        await self.nodes.replace_one({"_id": node.id}, doc, upsert=True)
        return node.id

    async def upsert_edge(self, edge: GraphEdge) -> str:
        edge_id = f"{edge.source_id}__{edge.relation}__{edge.target_id}"
        doc = {
            "_id":       edge_id,
            "source_id": edge.source_id,
            "target_id": edge.target_id,
            "relation":  edge.relation,
            "properties": edge.properties or {},
        }
        await self.edges.replace_one({"_id": edge_id}, doc, upsert=True)
        return edge_id

    async def soft_delete_page(self, page_id: str) -> int:
        result = await self.nodes.update_many(
            {
                "properties.page_id": page_id,
                "type": {"$in": list(_PAGE_OWNED_TYPES)},
                "is_deleted": False,
            },
            {"$set": {"is_deleted": True}},
        )
        await self.row_embeddings.update_many(
            {"page_id": page_id, "is_deleted": False},
            {"$set": {"is_deleted": True}},
        )
        deleted = result.modified_count
        if deleted:
            logger.debug("Soft-deleted %d nodes for page %s", deleted, page_id)
        return deleted

    async def traverse(
        self,
        start_node_id: str,
        relation_types: List[str],
        max_depth: int = 3,
    ) -> List[GraphNode]:
        visited: set = {start_node_id}
        current_ids: List[str] = [start_node_id]
        all_nodes: List[GraphNode] = []

        for _ in range(max_depth):
            edges = await self.edges.find(
                {"source_id": {"$in": current_ids}, "relation": {"$in": relation_types}}
            ).to_list()

            next_ids = [e["target_id"] for e in edges if e["target_id"] not in visited]
            if not next_ids:
                break

            docs = await self.nodes.find(
                {"_id": {"$in": next_ids}, "is_deleted": False}
            ).to_list()

            all_nodes.extend(self._doc_to_node(d) for d in docs)
            visited.update(next_ids)
            current_ids = next_ids

        return all_nodes

    async def search_by_property(
        self, node_type: str, property_key: str, value: str
    ) -> List[GraphNode]:
        docs = await self.nodes.find(
            {
                "type": node_type,
                f"properties.{property_key}": value,
                "is_deleted": False,
            }
        ).to_list(length=100)
        return [self._doc_to_node(d) for d in docs]

    async def get_temporal_context(
        self,
        app_id: str,
        before_date: Optional[datetime] = None,
        limit: int = 10,
    ) -> List[GraphNode]:
        query: dict = {
            "type": "Event",
            "properties.app_ids": app_id,
            "is_deleted": False,
        }
        if before_date:
            query["timestamp"] = {"$lte": before_date}

        cursor = self.nodes.find(query).sort("timestamp", -1).limit(limit)
        docs = await cursor.to_list(limit)
        return [self._doc_to_node(d) for d in docs]

    async def update_node_embedding(
        self, node_id: str, text: str, embedding: List[float]
    ) -> None:
        await self.nodes.update_one(
            {"_id": node_id},
            {"$set": {"embedding_text": text, "embedding": embedding}},
        )

    async def upsert_row_embeddings(self, rows: List[RowEmbedding]) -> None:
        for row in rows:
            doc = {
                "_id":        row.id,
                "table_id":   row.table_id,
                "page_id":    row.page_id,
                "row_index":  row.row_index,
                "text":       row.text,
                "embedding":  row.embedding,
                "timestamp":  row.timestamp,
                "is_deleted": row.is_deleted,
            }
            await self.row_embeddings.replace_one({"_id": row.id}, doc, upsert=True)

    async def vector_search_nodes(
        self,
        query_embedding: List[float],
        node_types: List[str],
        before_date: Optional[datetime],
        limit: int,
    ) -> List[Tuple[GraphNode, float]]:
        query: dict = {
            "type": {"$in": node_types},
            "is_deleted": False,
            "embedding": {"$exists": True},
        }
        if before_date:
            query["timestamp"] = {"$lte": before_date}

        docs = await self.nodes.find(query).to_list()
        return _cosine_rank_nodes(query_embedding, docs, limit, self._doc_to_node)

    async def vector_search_rows(
        self,
        query_embedding: List[float],
        before_date: Optional[datetime],
        limit: int,
    ) -> List[Tuple[RowEmbedding, float]]:
        query: dict = {"is_deleted": False}
        if before_date:
            query["timestamp"] = {"$lte": before_date}

        docs = await self.row_embeddings.find(query).to_list()
        return _cosine_rank_rows(query_embedding, docs, limit)

    async def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _doc_to_node(self, doc: dict) -> GraphNode:
        return GraphNode(
            id=doc["_id"],
            type=doc["type"],
            properties=doc.get("properties", {}),
            timestamp=doc.get("timestamp"),
            is_deleted=doc.get("is_deleted", False),
            embedding=doc.get("embedding"),
            embedding_text=doc.get("embedding_text", ""),
        )


# ---------------------------------------------------------------------------
# Cosine similarity helpers (Python-side, upgrade to Atlas $vectorSearch later)
# ---------------------------------------------------------------------------

def _cosine_rank_nodes(
    query_emb: List[float],
    docs: list,
    limit: int,
    to_node,
) -> List[Tuple[GraphNode, float]]:
    if not docs:
        return []
    q = np.array(query_emb, dtype=np.float32)
    q_norm = np.linalg.norm(q)
    if q_norm < 1e-10:
        return []
    q = q / q_norm

    scored = []
    for doc in docs:
        emb = doc.get("embedding")
        if not emb:
            continue
        e = np.array(emb, dtype=np.float32)
        e_norm = np.linalg.norm(e)
        if e_norm < 1e-10:
            continue
        sim = float(np.dot(q, e / e_norm))
        scored.append((to_node(doc), sim))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


def _cosine_rank_rows(
    query_emb: List[float],
    docs: list,
    limit: int,
) -> List[Tuple[RowEmbedding, float]]:
    if not docs:
        return []
    q = np.array(query_emb, dtype=np.float32)
    q_norm = np.linalg.norm(q)
    if q_norm < 1e-10:
        return []
    q = q / q_norm

    scored = []
    for doc in docs:
        emb = doc.get("embedding")
        if not emb:
            continue
        e = np.array(emb, dtype=np.float32)
        e_norm = np.linalg.norm(e)
        if e_norm < 1e-10:
            continue
        sim = float(np.dot(q, e / e_norm))
        row = RowEmbedding(
            id=doc["_id"],
            table_id=doc["table_id"],
            page_id=doc["page_id"],
            row_index=doc["row_index"],
            text=doc.get("text", ""),
            embedding=doc["embedding"],
            timestamp=doc.get("timestamp"),
            is_deleted=doc.get("is_deleted", False),
        )
        scored.append((row, sim))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]
