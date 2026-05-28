from __future__ import annotations

import json
import logging
import uuid
from typing import List

from ..entity_extraction.config import ExtractionConfig
from .models import CanonicalSchema, SchemaStatus, TableFingerprint

logger = logging.getLogger(__name__)

# ── Prompt ────────────────────────────────────────────────────────────────────

_CLASSIFICATION_PROMPT = """\
You are an expert in analyzing corporate meeting minutes documents.

Below are groups of tables extracted from 5 years of meeting minutes.
Each group contains header variations that appear to belong to the same type of table.

For each group:
1. Assign a table type (e.g. "agreements", "applications", "risks", "follow_up",
   "approvals", "action_items", etc.)
2. Define the canonical schema — column names in English snake_case
3. Create a mapping from every raw header variant → canonical field name

Column groups (JSON):
{clusters_json}

Respond ONLY with valid JSON in this exact structure:
{{
  "clusters": [
    {{
      "cluster_index": 0,
      "table_type": "agreements",
      "description": "Table of meeting agreements and commitments",
      "canonical_columns": ["agreement", "responsible", "due_date", "status", "app_id"],
      "column_mapping": {{
        "Acuerdo": "agreement",
        "Agreement": "agreement",
        "Acuedo": "agreement",
        "Responsable": "responsible",
        "Resp.": "responsible",
        "Owner": "responsible",
        "Fecha": "due_date",
        "Fec.": "due_date",
        "Date": "due_date",
        "Estatus": "status",
        "Status": "status",
        "Estado": "status"
      }}
    }}
  ]
}}
"""


class TableClassifier:
    """
    Calls Gemini via LangChain to assign a canonical schema to each cluster
    of fingerprints.  The LLM is initialized lazily so the class can be
    instantiated without API credentials (e.g. in tests).
    """

    def __init__(self, config: ExtractionConfig | None = None) -> None:
        self._config = config or ExtractionConfig()
        self._llm = None  # lazy init

    def _get_llm(self):
        if self._llm is None:
            from ..vertex_auth import get_chat_llm
            self._llm = get_chat_llm(model=self._config.gemini_model)
        return self._llm

    def classify_clusters(
        self,
        clusters: List[List[TableFingerprint]],
        status: SchemaStatus = SchemaStatus.PENDING_APPROVAL,
    ) -> List[CanonicalSchema]:
        """
        Sends all clusters to the LLM in one call and returns a CanonicalSchema
        per cluster.  `status` is set by the caller (D1: batch → PENDING_APPROVAL,
        incremental → AUTO_APPROVED).
        """
        cluster_data = [
            {
                "cluster_index": idx,
                "header_variants": list({
                    h for fp in cluster for h in fp.raw_headers
                }),
                "sample_headers": clusters[idx][0].raw_headers,
                "table_count": len(cluster),
            }
            for idx, cluster in enumerate(clusters)
        ]

        prompt = _CLASSIFICATION_PROMPT.format(
            clusters_json=json.dumps(cluster_data, ensure_ascii=False, indent=2)
        )

        llm = self._get_llm()
        response = llm.invoke(prompt)
        raw = response.content.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        parsed = json.loads(raw)
        schemas: List[CanonicalSchema] = []

        for item in parsed.get("clusters", []):
            schemas.append(CanonicalSchema(
                schema_id=str(uuid.uuid4()),
                table_type=item["table_type"],
                description=item.get("description", ""),
                canonical_columns=item["canonical_columns"],
                column_mapping=item["column_mapping"],
                status=status,
            ))

        logger.info(
            "LLM classified %d clusters into %d schemas (status=%s)",
            len(clusters), len(schemas), status,
        )
        return schemas
