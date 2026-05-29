from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from pymongo.asynchronous.database import AsyncDatabase

from ..parser.models import ContentTree, ParsedCell, ParsedTable, TextStyle
from .config import ExtractionConfig
from .llm_extractor import LLMEntityExtractor
from .models import (
    CandidateId,
    EntitySet,
    UnvalidatedIdRecord,
    ValidatedId,
)
from .regex_scanner import RegexScanner
from .validation_gateway import ValidationGateway

logger = logging.getLogger(__name__)


class PageEntityExtractor:
    """
    Stage 4 orchestrator.

    For each parsed page:
      1. Collect all non-propagated text with provenance paths.
      2. Regex-scan every chunk for 11-digit (app_id) and 9-digit (project_id) IDs.
      3. Call the LLM once per page to catch non-standard formats and extract events.
      4. Merge + deduplicate candidates from both sources.
      5. Validate candidates against the external API (semaphore-limited).
      6. Persist unvalidated candidates to MongoDB for manual review.
      7. Return EntitySet (validated IDs + events) for Stage 5.
    """

    def __init__(
        self,
        config: Optional[ExtractionConfig] = None,
        db: Optional[AsyncDatabase] = None,
    ) -> None:
        self._config = config or ExtractionConfig()
        self._db = db
        self._scanner = RegexScanner()
        self._llm_extractor = LLMEntityExtractor(self._config)
        self._gateway = ValidationGateway(self._config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def extract(
        self,
        content_tree: ContentTree,
        extra_texts: Optional[List[Tuple[str, str]]] = None,
    ) -> EntitySet:
        """
        Extract entities from a parsed page.

        *extra_texts* is an optional list of (text, provenance_path) pairs
        from attachment content (Stage 3A).  These are merged with the page
        text chunks before regex and LLM scanning so IDs/events inside
        attached files are captured with their attachment provenance path.
        """
        page_id   = content_tree.page_id
        page_date = content_tree.page_date

        # 1. Collect text chunks (skip propagated cells to avoid duplicate IDs)
        chunks = self._collect_text_chunks(content_tree)

        # Append attachment text chunks (already has attachment provenance paths)
        if extra_texts:
            chunks.extend(extra_texts)

        # 2. Regex scan — fast, zero-cost, deduplicates across chunks
        regex_candidates = self._scanner.scan_many(chunks)

        # 3. LLM call — one per page, catches non-standard formats + events
        full_text = " ".join(text for text, _ in chunks)
        provenance_path = f"page:{page_id}"
        llm_result = await self._llm_extractor.extract(
            text=full_text,
            page_date=page_date,
            provenance_path=provenance_path,
            has_cancelled_items=self._has_cancelled_items(content_tree),
        )

        # 4. Merge regex candidates with LLM additional IDs (dedup by raw_value)
        all_candidates = self._merge_candidates(
            regex_candidates, llm_result, provenance_path
        )

        # 5. Validate against external API
        validated = await self._gateway.validate_batch(all_candidates)

        # 6. Persist unvalidated candidates to MongoDB
        unvalidated = [v for v in validated if not v.is_valid]
        await self._persist_unvalidated(unvalidated, page_id)

        # 7. Build EntitySet
        app_ids     = _extract_valid_ids(validated, "app_id")
        project_ids = _extract_valid_ids(validated, "project_id")

        return EntitySet(
            page_id=page_id,
            page_date=page_date,
            app_ids=app_ids,
            project_ids=project_ids,
            unvalidated_ids=[v.candidate for v in unvalidated],
            events=llm_result.events,
        )

    async def close(self) -> None:
        await self._gateway.close()

    # ------------------------------------------------------------------
    # Text collection
    # ------------------------------------------------------------------

    def _collect_text_chunks(
        self, content_tree: ContentTree
    ) -> List[Tuple[str, str]]:
        """
        Returns (text, provenance_path) pairs for every non-propagated text source:
          - top-level text blocks
          - table cells (recursing into sub-tables)
        """
        chunks: List[Tuple[str, str]] = []

        for chunk in content_tree.text_blocks:
            text = chunk.content.strip()
            if text:
                chunks.append((text, chunk.provenance.to_path()))

        for table in content_tree.tables:
            self._collect_from_table(table, chunks)

        return chunks

    def _collect_from_table(
        self, table: ParsedTable, out: List[Tuple[str, str]]
    ) -> None:
        for row in table.cells:
            for cell in row:
                if cell.is_propagated:
                    continue
                prov_path = (
                    cell.provenance.to_path()
                    if cell.provenance
                    else f"table:{table.table_index}/row:{cell.row}/col:{cell.col}"
                )
                text = " ".join(
                    tc.content for tc in cell.text_chunks if tc.content.strip()
                )
                if text:
                    out.append((text, prov_path))
                for sub in cell.sub_tables:
                    self._collect_from_table(sub, out)

    # ------------------------------------------------------------------
    # Candidate merging
    # ------------------------------------------------------------------

    def _merge_candidates(
        self,
        regex_candidates: List[CandidateId],
        llm_result,
        provenance_path: str,
    ) -> List[CandidateId]:
        seen: set = {c.raw_value for c in regex_candidates}
        merged = list(regex_candidates)

        for raw in llm_result.additional_app_ids:
            val = raw.strip().replace(" ", "").replace("-", "").replace(".", "")
            if len(val) == 11 and val.isdigit() and val not in seen:
                seen.add(val)
                merged.append(CandidateId(
                    raw_value=val,
                    id_type="app_id",
                    context=f"LLM-detected: {raw}",
                    provenance_path=provenance_path,
                ))

        for raw in llm_result.additional_project_ids:
            val = raw.strip().replace(" ", "").replace("-", "").replace(".", "").replace("/", "")
            if len(val) == 9 and val.isdigit() and val not in seen:
                seen.add(val)
                merged.append(CandidateId(
                    raw_value=val,
                    id_type="project_id",
                    context=f"LLM-detected: {raw}",
                    provenance_path=provenance_path,
                ))

        return merged

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist_unvalidated(
        self, unvalidated: List[ValidatedId], page_id: str
    ) -> None:
        if not unvalidated or self._db is None:
            return

        collection = self._db["unvalidated_ids"]
        now = datetime.now(tz=timezone.utc)
        docs = [
            UnvalidatedIdRecord(
                candidate_id=v.candidate.raw_value,
                id_type=v.candidate.id_type,
                provenance_path=v.candidate.provenance_path,
                context=v.candidate.context,
                page_id=page_id,
                detected_at=now,
            ).model_dump()
            for v in unvalidated
        ]

        try:
            await collection.insert_many(docs, ordered=False)
        except Exception as exc:
            logger.error("Failed to persist unvalidated IDs for page %s: %s", page_id, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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


def _extract_valid_ids(validated: List[ValidatedId], id_type: str) -> List[str]:
    return [v.candidate.raw_value for v in validated if v.is_valid and v.candidate.id_type == id_type]
