from __future__ import annotations

import logging
from typing import Dict, List, Optional

from pymongo import AsyncMongoClient

from confluence_graphrag.ingestion.config import IngestionConfig
from confluence_graphrag.parser.models import ParsedTable

from .classifier import TableClassifier
from .clusterer import AUTO_ASSIGN_THRESHOLD, FingerprintClusterer
from .consolidator import TableConsolidator
from .fingerprinter import TableFingerprinter
from .models import CanonicalSchema, NormalizedRow, SchemaStatus
from .schema_store import CanonicalSchemaStore

logger = logging.getLogger(__name__)

_UNKNOWN_COLLECTION = "unknown_tables"


class BatchNormalizer:
    """
    D1 — Batch mode:
      1. Fingerprint all tables from the historical ingestion
      2. Cluster by header similarity (rapidfuzz + DBSCAN)
      3. Classify clusters with the LLM → schemas saved as PENDING_APPROVAL
      4. Block consolidation until a human approves via schema_store.approve()

    Typical call during setup:
        await batch_normalizer.prepare_schemas(all_tables)
        # human reviews and approves schemas in MongoDB
        rows = await batch_normalizer.consolidate_approved(all_tables)
    """

    def __init__(
        self,
        config: IngestionConfig,
        classifier: TableClassifier,
        store: CanonicalSchemaStore,
    ) -> None:
        self._fingerprinter = TableFingerprinter()
        self._clusterer = FingerprintClusterer()
        self._classifier = classifier
        self._store = store
        self._consolidator = TableConsolidator()

    async def prepare_schemas(self, tables: List[ParsedTable]) -> int:
        """
        Fingerprint + cluster + LLM classify all tables.
        Saves schemas with status=PENDING_APPROVAL.
        Returns the number of schemas generated.
        """
        fingerprints = self._fingerprinter.fingerprint_many(tables)
        clusters = self._clusterer.cluster(fingerprints)

        schemas = self._classifier.classify_clusters(
            clusters, status=SchemaStatus.PENDING_APPROVAL
        )
        await self._store.save_many(schemas)

        logger.info(
            "BatchNormalizer: %d tables → %d clusters → %d schemas (pending approval)",
            len(tables), len(clusters), len(schemas),
        )
        return len(schemas)

    async def consolidate_approved(
        self, tables: List[ParsedTable]
    ) -> Dict[str, List[NormalizedRow]]:
        """
        Apply approved schemas to all tables.
        Returns a dict of {table_type: [NormalizedRow]}.
        Only runs on APPROVED or AUTO_APPROVED schemas.
        """
        approved = await self._store.list_approved()
        if not approved:
            logger.warning("No approved schemas found — run prepare_schemas() first")
            return {}

        # Build type → schema index for fast lookup
        schema_by_type: Dict[str, CanonicalSchema] = {s.table_type: s for s in approved}
        fingerprints = self._fingerprinter.fingerprint_many(tables)
        clusters = self._clusterer.cluster(fingerprints)

        result: Dict[str, List[NormalizedRow]] = {}

        for fp, table in zip(fingerprints, tables):
            best_schema, score = self._clusterer.best_match(fp, approved)
            if best_schema is None or score < AUTO_ASSIGN_THRESHOLD:
                continue
            rows = self._consolidator.consolidate(table, best_schema)
            result.setdefault(best_schema.table_type, []).extend(rows)

        return result


class IncrementalNormalizer:
    """
    D1 + D2 — Incremental mode (new/updated pages):
      - Schemas are AUTO_APPROVED (no human gate)
      - D2: similarity > 0.7  → auto-assign to nearest schema
      - D2: similarity < 0.7  → route to unknown_tables collection
    """

    def __init__(
        self,
        config: IngestionConfig,
        classifier: TableClassifier,
        store: CanonicalSchemaStore,
    ) -> None:
        self._config = config
        self._fingerprinter = TableFingerprinter()
        self._clusterer = FingerprintClusterer()
        self._classifier = classifier
        self._store = store
        self._consolidator = TableConsolidator()

        client = AsyncMongoClient(config.mongodb_uri)
        self._db = client[config.mongodb_db]

    async def normalize_table(
        self, table: ParsedTable
    ) -> Optional[List[NormalizedRow]]:
        """
        Normalize a single table from an incremental page.
        Returns NormalizedRow list on success, None if routed to unknown_tables.
        """
        fp = self._fingerprinter.fingerprint(table)
        approved = await self._store.list_approved()

        best_schema, score = self._clusterer.best_match(fp, approved)

        # D2: auto-assign if above threshold
        if best_schema and score >= AUTO_ASSIGN_THRESHOLD:
            rows = self._consolidator.consolidate(table, best_schema)
            logger.debug(
                "Table %s → schema '%s' (score=%.2f)",
                fp.table_id, best_schema.table_type, score,
            )
            return rows

        # D2: no good match — check if we can classify as a new type
        if not approved or score < AUTO_ASSIGN_THRESHOLD:
            if self._can_classify_as_new(fp, approved):
                new_schemas = self._classifier.classify_clusters(
                    [[fp]], status=SchemaStatus.AUTO_APPROVED
                )
                await self._store.save_many(new_schemas)
                if new_schemas:
                    rows = self._consolidator.consolidate(table, new_schemas[0])
                    logger.info(
                        "New schema created for table %s (type='%s')",
                        fp.table_id, new_schemas[0].table_type,
                    )
                    return rows

        # D2: route to unknown_tables for manual review
        await self._db[_UNKNOWN_COLLECTION].insert_one({
            "table_id": fp.table_id,
            "page_id": fp.page_id,
            "page_date": fp.page_date.isoformat(),
            "raw_headers": fp.raw_headers,
            "best_match_type": best_schema.table_type if best_schema else None,
            "best_match_score": round(score, 3),
            "sample_values": fp.sample_values,
            "provenance_path": table.provenance.to_path(),
        })
        logger.warning(
            "Table %s sent to unknown_tables (best score=%.2f < %.2f)",
            fp.table_id, score, AUTO_ASSIGN_THRESHOLD,
        )
        return None

    @staticmethod
    def _can_classify_as_new(fp, existing_schemas: List[CanonicalSchema]) -> bool:
        """
        Only attempt LLM classification if the fingerprint has a reasonable
        number of headers and doesn't look like a layout/formatting table.
        """
        if fp.col_count < 2 or fp.row_count < 2:
            return False
        # If there are already approved schemas, only create new ones for
        # clearly different header sets (low similarity with ALL existing ones)
        if existing_schemas:
            from .clusterer import _header_similarity
            max_sim = max(
                _header_similarity(fp.raw_headers, s.canonical_columns)
                for s in existing_schemas
            )
            if max_sim >= AUTO_ASSIGN_THRESHOLD:
                return False  # close enough to existing, don't create duplicate
        return True
