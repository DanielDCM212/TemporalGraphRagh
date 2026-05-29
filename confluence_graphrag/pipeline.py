from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from pymongo.asynchronous.database import AsyncDatabase

from .attachment_processor import AttachmentConfig, AttachmentProcessor
from .attachment_processor.models import ExtractedAttachment
from .entity_extraction import ExtractionConfig, PageEntityExtractor
from .graph import GraphConfig, TemporalGraphBuilder, create_adapter
from .ingestion.confluence_client import ConfluenceClient
from .ingestion.config import IngestionConfig
from .ingestion.models import AttachmentMetadata, PageMetadata
from .parser.html_parser import HTMLContentParser
from .parser.models import AttachmentRef, ParsedCell, ParsedTable
from .table_normalization.classifier import TableClassifier
from .table_normalization.models import NormalizedRow
from .table_normalization.normalizer import IncrementalNormalizer
from .table_normalization.row_store import NormalizedRowStore
from .table_normalization.schema_store import CanonicalSchemaStore

logger = logging.getLogger(__name__)


def _collect_all_attachment_refs(
    content_tree,
    api_attachments: List[AttachmentMetadata],
) -> List[Tuple[AttachmentRef, Optional[AttachmentMetadata]]]:
    """
    Walk the ContentTree to collect every AttachmentRef with its location
    provenance, then reconcile with the API AttachmentMetadata (download URL,
    size) by filename.

    Returns a list of (ref, meta | None) pairs covering:
      - page-level attachments (content_tree.attachments)
      - table-cell attachments (cell.attachments in every table / sub-table)
      - API-only attachments not referenced in the HTML (added with page-level
        provenance so nothing is dropped)
    """
    # Build filename → metadata lookup from the API list
    meta_by_filename: Dict[str, AttachmentMetadata] = {
        m.filename: m for m in api_attachments
    }

    pairs: List[Tuple[AttachmentRef, Optional[AttachmentMetadata]]] = []
    seen_attachment_ids: set = set()

    def _add(ref: AttachmentRef) -> None:
        key = ref.attachment_id
        if key in seen_attachment_ids:
            return
        seen_attachment_ids.add(key)
        meta = meta_by_filename.get(ref.filename)
        pairs.append((ref, meta))

    # 1. Page-level attachments
    for ref in content_tree.attachments:
        _add(ref)

    # 2. Table-cell attachments (recurse into sub-tables)
    def _walk_table(table: ParsedTable) -> None:
        for row in table.cells:
            for cell in row:
                if cell.is_propagated:
                    continue
                for ref in cell.attachments:
                    _add(ref)
                for sub in cell.sub_tables:
                    _walk_table(sub)

    for table in content_tree.tables:
        _walk_table(table)

    # 3. API attachments not referenced anywhere in the HTML
    from .parser.models import AttachmentType, Provenance
    from datetime import datetime

    page_prov = content_tree.attachments[0].provenance if content_tree.attachments else None

    for meta in api_attachments:
        if meta.filename in {r.filename for r, _ in pairs}:
            continue
        # Build a minimal page-level provenance for this orphan attachment
        from .parser.models import Provenance as _Prov
        prov = _Prov(
            page_id=content_tree.page_id,
            page_title=content_tree.page_title,
            page_date=content_tree.page_date,
        )
        ext = meta.filename.rsplit(".", 1)[-1].lower() if "." in meta.filename else ""
        from .parser.html_parser import HTMLContentParser as _P
        att_type = _P.ATTACHMENT_TYPE_MAP.get(ext, AttachmentType.UNKNOWN)
        ref = AttachmentRef(
            attachment_id=meta.attachment_id,
            filename=meta.filename,
            url=meta.download_url,
            attachment_type=att_type,
            provenance=prov,
        )
        if meta.attachment_id not in seen_attachment_ids:
            seen_attachment_ids.add(meta.attachment_id)
            pairs.append((ref, meta))

    return pairs


class FullPagePipeline:
    """
    Wires Stage 2 → 3A → 3B → 4 → 5 → 6 into a single ingest() call.
    Stage 1 (BatchIngestor / IncrementalWatcher) calls ingest() per page.
    """

    def __init__(
        self,
        normalizer: IncrementalNormalizer,
        extractor: PageEntityExtractor,
        builder: TemporalGraphBuilder,
        row_store: NormalizedRowStore,
        attachment_processor: Optional[AttachmentProcessor] = None,
        attachment_chunk_size: int = 1000,
        attachment_chunk_overlap: int = 150,
    ) -> None:
        self._normalizer              = normalizer
        self._extractor               = extractor
        self._builder                 = builder
        self._row_store               = row_store
        self._attachment_processor    = attachment_processor
        self._attachment_chunk_size   = attachment_chunk_size
        self._attachment_chunk_overlap = attachment_chunk_overlap

    async def ingest(self, metadata: PageMetadata) -> None:
        parser = HTMLContentParser(metadata.page_id, metadata.title)
        content_tree = parser.parse(metadata.html_content)

        # Stage 3A — process attachments
        extracted_attachments: List[ExtractedAttachment] = []
        if self._attachment_processor and metadata.attachments:
            refs = _collect_all_attachment_refs(content_tree, metadata.attachments)
            if refs:
                extracted_attachments = await self._attachment_processor.process(refs)

        # Merge attachment tables into content_tree so Stage 3B and the graph
        # builder handle them identically to page-body tables.
        if extracted_attachments:
            table_offset = max((t.table_index for t in content_tree.tables), default=-1) + 1
            for att in extracted_attachments:
                for tbl in att.tables:
                    # Reassign a unique table_index in the page scope
                    from dataclasses import replace
                    merged_tbl = ParsedTable(
                        table_index=table_offset,
                        headers=tbl.headers,
                        cells=tbl.cells,
                        provenance=tbl.provenance,
                        raw_html=tbl.raw_html,
                    )
                    # Tag table with source attachment for provenance
                    merged_tbl.provenance.attachment_id = att.attachment_id
                    content_tree.tables.append(merged_tbl)
                    table_offset += 1

        # Soft-delete stale normalized rows before re-ingesting
        await self._row_store.soft_delete_page(metadata.page_id)

        # Stage 3B — normalize tables and persist all rows
        all_rows: List[NormalizedRow] = []
        for table in content_tree.tables:
            rows = await self._normalizer.normalize_table(table)
            if rows:
                all_rows.extend(rows)

        if all_rows:
            await self._row_store.upsert_rows(all_rows)
            logger.debug(
                "Saved %d normalized rows for page %s",
                len(all_rows), metadata.page_id,
            )

        # Stage 4 — entity extraction (page text + attachment text)
        extra_texts: List[Tuple[str, str]] = [
            (att.text, att.provenance.to_path())
            for att in extracted_attachments
            if att.text and not att.error
        ]
        entity_set = await self._extractor.extract(content_tree, extra_texts=extra_texts or None)

        # Stage 5 + 6 — graph ingestion and embeddings
        await self._builder.ingest_page(entity_set, content_tree, attachments=extracted_attachments)

    async def soft_delete_page(self, page_id: str) -> None:
        await self._builder._adapter.soft_delete_page(page_id)
        await self._row_store.soft_delete_page(page_id)

    async def close(self) -> None:
        await self._extractor.close()
        await self._builder.close()


def build_pipeline(
    ingestion_cfg: IngestionConfig,
    extraction_cfg: ExtractionConfig,
    graph_cfg: GraphConfig,
    db: AsyncDatabase,
    client: Optional[ConfluenceClient] = None,
) -> FullPagePipeline:
    normalizer = IncrementalNormalizer(
        config=ingestion_cfg,
        classifier=TableClassifier(config=extraction_cfg),
        store=CanonicalSchemaStore(ingestion_cfg),
    )
    extractor = PageEntityExtractor(config=extraction_cfg, db=db)
    adapter   = create_adapter(graph_cfg)
    builder   = TemporalGraphBuilder(adapter=adapter, config=graph_cfg)
    row_store = NormalizedRowStore(ingestion_cfg)

    attachment_processor: Optional[AttachmentProcessor] = None
    attachment_cfg = AttachmentConfig()
    if client is not None:
        attachment_processor = AttachmentProcessor(client=client, config=attachment_cfg)

    return FullPagePipeline(
        normalizer=normalizer,
        extractor=extractor,
        builder=builder,
        row_store=row_store,
        attachment_processor=attachment_processor,
        attachment_chunk_size=attachment_cfg.chunk_size,
        attachment_chunk_overlap=attachment_cfg.chunk_overlap,
    )
