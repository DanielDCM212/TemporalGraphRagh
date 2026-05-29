from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .adapter import GraphStoreAdapter
from .models import AttachmentChunk, GraphEdge, GraphNode, RowEmbedding

logger = logging.getLogger(__name__)

_PAGE_OWNED_TYPES = {"ConfPage", "Table", "Event", "Attachment"}


class Neo4jAdapter(GraphStoreAdapter):
    """
    Graph store backed by Neo4j using the official async Python driver.
    This adapter is the production swap target for MongoDBAdapter.

    All nodes carry a `GraphNode` label plus a type-specific label
    (e.g. `Application`). Soft-delete sets `is_deleted = true` in place.
    """

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import AsyncGraphDatabase
        self._driver = AsyncGraphDatabase.driver(uri, auth=(user, password))

    async def ensure_indexes(self) -> None:
        async with self._driver.session() as session:
            for label in ("ConfPage", "Table", "Application", "Project", "Event"):
                await session.run(
                    f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n.id)"
                )
            await session.run(
                "CREATE INDEX IF NOT EXISTS FOR (n:GraphNode) ON (n.page_id)"
            )
            await session.run(
                "CREATE INDEX IF NOT EXISTS FOR (n:Application) ON (n.app_id)"
            )
            await session.run(
                "CREATE INDEX IF NOT EXISTS FOR (n:Project) ON (n.project_id)"
            )

    async def upsert_node(self, node: GraphNode) -> str:
        props = {
            "id":         node.id,
            "is_deleted": node.is_deleted,
            "timestamp":  node.timestamp,
            **node.properties,
        }
        cypher = f"""
        MERGE (n:GraphNode:{node.type} {{id: $id}})
        SET n += $props
        RETURN n.id AS id
        """
        async with self._driver.session() as session:
            await session.run(cypher, id=node.id, props=_serialise(props))
        return node.id

    async def upsert_edge(self, edge: GraphEdge) -> str:
        cypher = f"""
        MATCH (src:GraphNode {{id: $src_id}})
        MATCH (tgt:GraphNode {{id: $tgt_id}})
        MERGE (src)-[r:{edge.relation}]->(tgt)
        SET r += $props
        RETURN id(r) AS eid
        """
        async with self._driver.session() as session:
            await session.run(
                cypher,
                src_id=edge.source_id,
                tgt_id=edge.target_id,
                props=edge.properties or {},
            )
        return f"{edge.source_id}__{edge.relation}__{edge.target_id}"

    async def soft_delete_page(self, page_id: str) -> int:
        type_labels = "|".join(f"n:{t}" for t in _PAGE_OWNED_TYPES)
        cypher = f"""
        MATCH (n:GraphNode)
        WHERE n.page_id = $page_id AND ({type_labels}) AND n.is_deleted = false
        SET n.is_deleted = true
        RETURN count(n) AS affected
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, page_id=page_id)
            record = await result.single()
            affected = record["affected"] if record else 0
        if affected:
            logger.debug("Soft-deleted %d nodes for page %s", affected, page_id)
        return affected

    async def traverse(
        self,
        start_node_id: str,
        relation_types: List[str],
        max_depth: int = 3,
    ) -> List[GraphNode]:
        rel_pattern = "|".join(relation_types)
        cypher = f"""
        MATCH (start:GraphNode {{id: $start_id}})
        CALL apoc.path.subgraphNodes(start, {{
            relationshipFilter: '{rel_pattern}',
            maxLevel: $depth
        }}) YIELD node
        WHERE node.is_deleted = false AND node.id <> $start_id
        RETURN node
        """
        async with self._driver.session() as session:
            result = await session.run(
                cypher, start_id=start_node_id, depth=max_depth
            )
            records = await result.data()
        return [_record_to_node(r["node"]) for r in records]

    async def search_by_property(
        self, node_type: str, property_key: str, value: str
    ) -> List[GraphNode]:
        cypher = f"""
        MATCH (n:GraphNode:{node_type})
        WHERE n.{property_key} = $value AND n.is_deleted = false
        RETURN n
        LIMIT 100
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, value=value)
            records = await result.data()
        return [_record_to_node(r["n"]) for r in records]

    async def get_temporal_context(
        self,
        app_id: str,
        before_date: Optional[datetime] = None,
        limit: int = 10,
    ) -> List[GraphNode]:
        date_filter = "AND n.timestamp <= $before_date" if before_date else ""
        cypher = f"""
        MATCH (n:GraphNode:Event)
        WHERE $app_id IN n.app_ids AND n.is_deleted = false {date_filter}
        RETURN n ORDER BY n.timestamp DESC LIMIT $limit
        """
        params: Dict[str, Any] = {"app_id": app_id, "limit": limit}
        if before_date:
            params["before_date"] = before_date
        async with self._driver.session() as session:
            result = await session.run(cypher, **params)
            records = await result.data()
        return [_record_to_node(r["n"]) for r in records]

    async def update_node_embedding(
        self, node_id: str, text: str, embedding: List[float]
    ) -> None:
        # TODO: implement with Neo4j GDS vector index
        raise NotImplementedError("vector embeddings not yet implemented for Neo4j")

    async def upsert_row_embeddings(self, rows: List[RowEmbedding]) -> None:
        raise NotImplementedError("vector embeddings not yet implemented for Neo4j")

    async def vector_search_nodes(
        self,
        query_embedding: List[float],
        node_types: List[str],
        before_date: Optional[datetime],
        limit: int,
    ) -> List[Tuple[GraphNode, float]]:
        raise NotImplementedError("vector embeddings not yet implemented for Neo4j")

    async def vector_search_rows(
        self,
        query_embedding: List[float],
        before_date: Optional[datetime],
        limit: int,
    ) -> List[Tuple[RowEmbedding, float]]:
        raise NotImplementedError("vector embeddings not yet implemented for Neo4j")

    async def upsert_attachment_chunks(self, chunks: List[AttachmentChunk]) -> None:
        raise NotImplementedError("vector embeddings not yet implemented for Neo4j")

    async def vector_search_attachment_chunks(
        self,
        query_embedding: List[float],
        before_date: Optional[datetime],
        limit: int,
    ) -> List[Tuple[AttachmentChunk, float]]:
        raise NotImplementedError("vector embeddings not yet implemented for Neo4j")

    async def close(self) -> None:
        await self._driver.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialise(props: dict) -> dict:
    """Convert non-primitive values to strings so Neo4j accepts them."""
    out: dict = {}
    for k, v in props.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, list):
            out[k] = [str(i) if not isinstance(i, (str, int, float, bool)) else i for i in v]
        else:
            out[k] = str(v)
    return out


def _record_to_node(record: dict) -> GraphNode:
    meta_keys = {"id", "is_deleted", "timestamp"}
    properties = {k: v for k, v in record.items() if k not in meta_keys}
    return GraphNode(
        id=record.get("id", ""),
        type=properties.pop("__type__", "Unknown"),
        properties=properties,
        timestamp=record.get("timestamp"),
        is_deleted=record.get("is_deleted", False),
    )
