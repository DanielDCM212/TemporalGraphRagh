from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

import motor.motor_asyncio

from .adapter import GraphStoreAdapter
from .models import GraphEdge, GraphNode

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
        self._client = motor.motor_asyncio.AsyncIOMotorClient(connection_string)
        self._db     = self._client[database]
        self.nodes   = self._db["graph_nodes"]
        self.edges   = self._db["graph_edges"]

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
        await self.edges.create_index([("source_id", pymongo.ASCENDING)])
        await self.edges.create_index([("target_id", pymongo.ASCENDING)])
        await self.edges.create_index([("relation", pymongo.ASCENDING)])

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
            ).to_list(length=None)

            next_ids = [e["target_id"] for e in edges if e["target_id"] not in visited]
            if not next_ids:
                break

            docs = await self.nodes.find(
                {"_id": {"$in": next_ids}, "is_deleted": False}
            ).to_list(length=None)

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
        docs = await cursor.to_list(length=limit)
        return [self._doc_to_node(d) for d in docs]

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
        )
