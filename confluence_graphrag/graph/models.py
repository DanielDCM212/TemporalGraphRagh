from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional


@dataclass
class GraphNode:
    id: str
    type: str           # ConfPage | Table | Application | Project | Event
    properties: Dict
    timestamp: Optional[datetime] = None
    is_deleted: bool = False


@dataclass
class GraphEdge:
    source_id: str
    target_id: str
    relation: str       # CONTAINS | REFERENCES_APP | REFERENCES_PROJ | HAS_EVENT | ...
    properties: Dict = field(default_factory=dict)
