from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class GraphNode:
    id: str
    type: str           # ConfPage | Table | Application | Project | Event
    properties: Dict
    timestamp: Optional[datetime] = None
    is_deleted: bool = False
    embedding: Optional[List[float]] = None
    embedding_text: str = ""


@dataclass
class GraphEdge:
    source_id: str
    target_id: str
    relation: str       # CONTAINS | REFERENCES_APP | REFERENCES_PROJ | HAS_EVENT | ...
    properties: Dict = field(default_factory=dict)


@dataclass
class RowEmbedding:
    """Per-row embedding for a ParsedTable, stored in graph_row_embeddings."""
    id: str                          # "{table_id}__row_{row_index}"
    table_id: str
    page_id: str
    row_index: int
    text: str
    embedding: List[float]
    timestamp: Optional[datetime] = None
    is_deleted: bool = False
