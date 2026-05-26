from __future__ import annotations

import logging
from typing import List, Optional

from ..entity_extraction.models import EntitySet
from ..parser.models import ContentTree, TextStyle
from .adapter import GraphStoreAdapter
from .embedder import EmbeddingService, build_event_text, build_page_text, build_row_text
from .factory import GraphConfig
from .models import GraphEdge, GraphNode, RowEmbedding

logger = logging.getLogger(__name__)

_DEFAULT_EMBEDDING_MODEL = "models/text-embedding-004"


class TemporalGraphBuilder:
    """
    Stage 5 orchestrator.

    For each page:
      1. Soft-delete all previously ingested page-owned nodes (ConfPage, Table, Event).
      2. Upsert ConfPage node.
      3. Upsert Table nodes with CONTAINS edges from the page.
      4. Upsert Application nodes (global, merge first_seen/last_seen).
      5. Upsert Project nodes (global, merge first_seen/last_seen).
      6. Upsert Event nodes with HAS_EVENT edges to their page + referenced apps.
      7. Add a Graphiti episodic episode (if graphiti_enabled + Neo4j configured).
    """

    def __init__(
        self,
        adapter: GraphStoreAdapter,
        config: Optional[GraphConfig] = None,
    ) -> None:
        self._adapter  = adapter
        self._config   = config or GraphConfig()
        self._graphiti = None   # initialised lazily
        self._embedder: Optional[EmbeddingService] = None  # initialised lazily

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ingest_page(
        self, entity_set: EntitySet, content_tree: ContentTree
    ) -> None:
        page_id   = entity_set.page_id
        page_date = entity_set.page_date

        # 1. Remove stale data from a prior ingest of this page
        deleted = await self._adapter.soft_delete_page(page_id)
        if deleted:
            logger.debug("Cleared %d stale nodes for page %s", deleted, page_id)

        # 2. ConfPage node
        await self._adapter.upsert_node(GraphNode(
            id=f"page_{page_id}",
            type="ConfPage",
            properties={
                "page_id":    page_id,
                "title":      content_tree.page_title,
                "date":       page_date.isoformat(),
                "has_cancelled": self._has_cancelled_items(content_tree),
            },
            timestamp=page_date,
        ))

        # 3. Table nodes
        for table in content_tree.tables:
            table_id = f"{page_id}_table_{table.table_index}"
            await self._adapter.upsert_node(GraphNode(
                id=table_id,
                type="Table",
                properties={
                    "table_id":    table_id,
                    "table_index": table.table_index,
                    "headers":     table.headers,
                    "row_count":   len(table.cells),
                    "col_count":   len(table.cells[0]) if table.cells else 0,
                    "page_id":     page_id,
                },
                timestamp=page_date,
            ))
            await self._adapter.upsert_edge(GraphEdge(
                source_id=f"page_{page_id}",
                target_id=table_id,
                relation="CONTAINS",
                properties={"order": table.table_index},
            ))

        # 4. Application nodes (global — never soft-deleted)
        for app_id in entity_set.app_ids:
            await self._upsert_application(app_id, page_id, page_date)

        # 5. Project nodes (global — never soft-deleted)
        for proj_id in entity_set.project_ids:
            await self._upsert_project(proj_id, page_id, page_date)

        # 6. Event nodes
        for idx, event in enumerate(entity_set.events):
            event_id = f"event_{page_id}_{idx}"
            await self._adapter.upsert_node(GraphNode(
                id=event_id,
                type="Event",
                properties={
                    "event_type":  event.event_type.value,
                    "description": event.description,
                    "is_cancelled": event.is_cancelled,
                    "provenance":  event.provenance_path,
                    "app_ids":     event.app_ids,
                    "project_ids": event.project_ids,
                    "page_id":     page_id,
                },
                timestamp=page_date,
            ))
            # Page → Event
            await self._adapter.upsert_edge(GraphEdge(
                source_id=f"page_{page_id}",
                target_id=event_id,
                relation="HAS_EVENT",
            ))
            # App → Event (for temporal context queries)
            for app_id in event.app_ids:
                await self._adapter.upsert_edge(GraphEdge(
                    source_id=f"app_{app_id}",
                    target_id=event_id,
                    relation="HAS_EVENT",
                ))
            for proj_id in event.project_ids:
                await self._adapter.upsert_edge(GraphEdge(
                    source_id=f"proj_{proj_id}",
                    target_id=event_id,
                    relation="HAS_EVENT",
                ))

        # 7. Embeddings (page, events, table rows)
        await self._embed_page(entity_set, content_tree)

        # 8. Graphiti episodic memory
        await self._add_graphiti_episode(entity_set, content_tree)

        logger.info(
            "Graph ingestion complete for page %s — apps=%d projects=%d events=%d",
            page_id, len(entity_set.app_ids), len(entity_set.project_ids),
            len(entity_set.events),
        )

    async def close(self) -> None:
        await self._adapter.close()
        if self._graphiti:
            try:
                await self._graphiti.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Application / Project upsert (merge temporal fields)
    # ------------------------------------------------------------------

    async def _upsert_application(self, app_id: str, page_id: str, page_date) -> None:
        node_id = f"app_{app_id}"
        existing = await self._adapter.search_by_property("Application", "app_id", app_id)
        first_seen = existing[0].properties.get("first_seen", page_date.isoformat()) if existing else page_date.isoformat()

        await self._adapter.upsert_node(GraphNode(
            id=node_id,
            type="Application",
            properties={
                "app_id":     app_id,
                "validated":  True,
                "first_seen": first_seen,
                "last_seen":  page_date.isoformat(),
            },
            timestamp=page_date,
        ))
        await self._adapter.upsert_edge(GraphEdge(
            source_id=f"page_{page_id}",
            target_id=node_id,
            relation="REFERENCES_APP",
        ))

    async def _upsert_project(self, proj_id: str, page_id: str, page_date) -> None:
        node_id = f"proj_{proj_id}"
        existing = await self._adapter.search_by_property("Project", "project_id", proj_id)
        first_seen = existing[0].properties.get("first_seen", page_date.isoformat()) if existing else page_date.isoformat()

        await self._adapter.upsert_node(GraphNode(
            id=node_id,
            type="Project",
            properties={
                "project_id": proj_id,
                "validated":  True,
                "first_seen": first_seen,
                "last_seen":  page_date.isoformat(),
            },
            timestamp=page_date,
        ))
        await self._adapter.upsert_edge(GraphEdge(
            source_id=f"page_{page_id}",
            target_id=node_id,
            relation="REFERENCES_PROJ",
        ))

    # ------------------------------------------------------------------
    # Embedding (Stage 6)
    # ------------------------------------------------------------------

    def _get_embedder(self) -> EmbeddingService:
        if self._embedder is None:
            self._embedder = EmbeddingService(
                model=self._config.embedding_model if self._config else _DEFAULT_EMBEDDING_MODEL,
            )
        return self._embedder

    async def _embed_page(self, entity_set: EntitySet, content_tree: ContentTree) -> None:
        embedder = self._get_embedder()
        page_id   = entity_set.page_id
        page_date = entity_set.page_date

        # Build all texts in one list to batch the API call
        page_text = build_page_text(content_tree)

        event_texts = [
            build_event_text(ev, content_tree.page_title, page_date)
            for ev in entity_set.events
        ]

        row_items: List[tuple] = []  # (table_id, row_index, text, timestamp)
        for table in content_tree.tables:
            table_id = f"{page_id}_table_{table.table_index}"
            for row_idx, row in enumerate(table.cells):
                text = build_row_text(table.headers, row)
                if text:
                    row_items.append((table_id, row_idx, text, page_date))

        all_texts = [page_text] + event_texts + [r[2] for r in row_items]

        try:
            all_embeddings = await embedder.aembed_many(all_texts)
        except Exception as exc:
            logger.error("Embedding API failed for page %s: %s", page_id, exc)
            return

        # Distribute embeddings back to their targets
        idx = 0

        await self._adapter.update_node_embedding(
            f"page_{page_id}", page_text, all_embeddings[idx]
        )
        idx += 1

        for ev_idx, ev_text in enumerate(event_texts):
            await self._adapter.update_node_embedding(
                f"event_{page_id}_{ev_idx}", ev_text, all_embeddings[idx]
            )
            idx += 1

        row_embeddings: List[RowEmbedding] = []
        for (table_id, row_idx, row_text, ts), emb in zip(row_items, all_embeddings[idx:]):
            row_embeddings.append(RowEmbedding(
                id=f"{table_id}__row_{row_idx}",
                table_id=table_id,
                page_id=page_id,
                row_index=row_idx,
                text=row_text,
                embedding=emb,
                timestamp=ts,
            ))

        if row_embeddings:
            await self._adapter.upsert_row_embeddings(row_embeddings)

        logger.debug(
            "Embedded page %s — 1 page + %d events + %d rows",
            page_id, len(event_texts), len(row_embeddings),
        )

    # ------------------------------------------------------------------
    # Graphiti episodic memory
    # ------------------------------------------------------------------

    def _get_graphiti(self):
        if self._graphiti is not None:
            return self._graphiti

        if not self._config.graphiti_enabled or not self._config.neo4j_uri:
            return None

        try:
            from graphiti_core import Graphiti
            from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig
            from graphiti_core.llm_client.gemini_client import GeminiClient
            from graphiti_core.llm_client.config import LLMConfig
            from ..vertex_auth import get_genai_client

            genai_client = get_genai_client(
                project=self._config.gcp_project,
                location=self._config.gcp_location,
            )
            llm_config = LLMConfig(model=self._config.gemini_model)
            llm_client = GeminiClient(config=llm_config, client=genai_client)
            embedder   = GeminiEmbedder(
                config=GeminiEmbedderConfig(embedding_model=self._config.embedding_model),
                client=genai_client,
            )
            self._graphiti = Graphiti(
                uri=self._config.neo4j_uri,
                user=self._config.neo4j_user,
                password=self._config.neo4j_password,
                llm_client=llm_client,
                embedder=embedder,
            )
        except Exception as exc:
            logger.warning("Graphiti init failed — episodic memory disabled: %s", exc)
            return None

        return self._graphiti

    async def _add_graphiti_episode(
        self, entity_set: EntitySet, content_tree: ContentTree
    ) -> None:
        graphiti = self._get_graphiti()
        if graphiti is None:
            return

        try:
            from graphiti_core.nodes import EpisodeType
            body = self._build_episode_body(entity_set, content_tree)
            await graphiti.add_episode(
                name=f"page_{entity_set.page_id}",
                episode_body=body,
                source_description=f"Confluence: {content_tree.page_title}",
                reference_time=entity_set.page_date,
                source=EpisodeType.text,
                group_id=entity_set.page_id,
            )
        except Exception as exc:
            # Graphiti failure never blocks the pipeline
            logger.error(
                "Graphiti episode add failed for page %s: %s",
                entity_set.page_id, exc,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_episode_body(entity_set: EntitySet, content_tree: ContentTree) -> str:
        parts = [
            f"Page: {content_tree.page_title}",
            f"Date: {entity_set.page_date.strftime('%Y-%m-%d')}",
        ]
        if entity_set.app_ids:
            parts.append(f"Applications: {', '.join(entity_set.app_ids)}")
        if entity_set.project_ids:
            parts.append(f"Projects: {', '.join(entity_set.project_ids)}")
        for ev in entity_set.events:
            status = " [CANCELLED]" if ev.is_cancelled else ""
            parts.append(f"[{ev.event_type.value.upper()}{status}] {ev.description}")
        return "\n".join(parts)

    @staticmethod
    def _has_cancelled_items(content_tree: ContentTree) -> bool:
        for chunk in content_tree.text_blocks:
            if chunk.style == TextStyle.CANCELLED:
                return True
        for table in content_tree.tables:
            for row in table.cells:
                for cell in row:
                    for tc in cell.text_chunks:
                        if tc.style == TextStyle.CANCELLED:
                            return True
        return False
