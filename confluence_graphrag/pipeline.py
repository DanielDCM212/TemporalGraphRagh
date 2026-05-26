from __future__ import annotations

import motor.motor_asyncio

from .entity_extraction import ExtractionConfig, PageEntityExtractor
from .graph import GraphConfig, TemporalGraphBuilder, create_adapter
from .ingestion.config import IngestionConfig
from .ingestion.models import PageMetadata
from .parser.html_parser import HTMLContentParser
from .table_normalization.classifier import TableClassifier
from .table_normalization.normalizer import IncrementalNormalizer
from .table_normalization.schema_store import CanonicalSchemaStore


class FullPagePipeline:
    """
    Wires Stage 2 → 3B → 4 → 5 → 6 into a single ingest() call.
    Stage 1 (BatchIngestor / IncrementalWatcher) calls ingest() per page.
    """

    def __init__(
        self,
        normalizer: IncrementalNormalizer,
        extractor: PageEntityExtractor,
        builder: TemporalGraphBuilder,
    ) -> None:
        self._normalizer = normalizer
        self._extractor  = extractor
        self._builder    = builder

    async def ingest(self, metadata: PageMetadata) -> None:
        parser = HTMLContentParser(metadata.page_id, metadata.title)
        content_tree = parser.parse(metadata.html_content)

        for table in content_tree.tables:
            await self._normalizer.normalize_table(table)

        entity_set = await self._extractor.extract(content_tree)
        await self._builder.ingest_page(entity_set, content_tree)

    async def soft_delete_page(self, page_id: str) -> None:
        await self._builder._adapter.soft_delete_page(page_id)

    async def close(self) -> None:
        await self._extractor.close()
        await self._builder.close()


def build_pipeline(
    ingestion_cfg: IngestionConfig,
    extraction_cfg: ExtractionConfig,
    graph_cfg: GraphConfig,
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
) -> FullPagePipeline:
    normalizer = IncrementalNormalizer(
        config=ingestion_cfg,
        classifier=TableClassifier(google_api_key=graph_cfg.google_api_key),
        store=CanonicalSchemaStore(ingestion_cfg),
    )
    extractor = PageEntityExtractor(config=extraction_cfg, db=db)
    adapter   = create_adapter(graph_cfg)
    builder   = TemporalGraphBuilder(adapter=adapter, config=graph_cfg)
    return FullPagePipeline(normalizer, extractor, builder)
