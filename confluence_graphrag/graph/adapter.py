from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from .models import GraphEdge, GraphNode


class GraphStoreAdapter(ABC):
    """
    Swap-ready interface over the graph backend.
    MongoDBAdapter is the default; Neo4jAdapter is the production target.
    """

    @abstractmethod
    async def upsert_node(self, node: GraphNode) -> str:
        """Insert or replace a node. Returns the node id."""
        ...

    @abstractmethod
    async def upsert_edge(self, edge: GraphEdge) -> str:
        """Insert or replace an edge. Returns a composite key."""
        ...

    @abstractmethod
    async def soft_delete_page(self, page_id: str) -> int:
        """
        Mark page-owned nodes (ConfPage, Table, Event) as is_deleted=True.
        Application and Project nodes are global and are NOT deleted.
        Returns the number of nodes affected.
        """
        ...

    @abstractmethod
    async def traverse(
        self,
        start_node_id: str,
        relation_types: List[str],
        max_depth: int = 3,
    ) -> List[GraphNode]:
        """BFS traversal following the specified relation types."""
        ...

    @abstractmethod
    async def search_by_property(
        self,
        node_type: str,
        property_key: str,
        value: str,
    ) -> List[GraphNode]:
        """Find live nodes of given type where properties[property_key] == value."""
        ...

    @abstractmethod
    async def get_temporal_context(
        self,
        app_id: str,
        before_date: Optional[datetime] = None,
        limit: int = 10,
    ) -> List[GraphNode]:
        """
        Return Event nodes that reference app_id, ordered newest-first.
        If before_date is set, only events on or before that date are returned.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release any held connections."""
        ...
