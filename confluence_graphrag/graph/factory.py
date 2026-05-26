from __future__ import annotations

from enum import Enum

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .adapter import GraphStoreAdapter


class GraphBackend(str, Enum):
    MONGODB = "mongodb"
    NEO4J   = "neo4j"


class GraphConfig(BaseSettings):
    # Primary graph backend
    graph_backend: GraphBackend = GraphBackend.MONGODB

    # MongoDB
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db:  str = "confluence_graphrag"

    # Neo4j (production swap target)
    neo4j_uri:      str = ""
    neo4j_user:     str = ""
    neo4j_password: str = ""

    # Gemini (Google AI / Vertex AI for Graphiti)
    google_api_key:  str = ""
    gcp_project:     str = ""
    gcp_location:    str = "us-central1"
    gemini_model:    str = "gemini-2.5-flash"
    embedding_model: str = "text-embedding-004"

    # Graphiti episodic memory layer (requires Neo4j)
    graphiti_enabled: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def create_adapter(config: GraphConfig | None = None) -> GraphStoreAdapter:
    if config is None:
        config = GraphConfig()

    if config.graph_backend == GraphBackend.MONGODB:
        from .mongodb_adapter import MongoDBAdapter
        return MongoDBAdapter(config.mongodb_uri, config.mongodb_db)

    if config.graph_backend == GraphBackend.NEO4J:
        if not config.neo4j_uri:
            raise ValueError("NEO4J_URI must be set when graph_backend=neo4j")
        from .neo4j_adapter import Neo4jAdapter
        return Neo4jAdapter(config.neo4j_uri, config.neo4j_user, config.neo4j_password)

    raise ValueError(f"Unsupported graph backend: {config.graph_backend}")
