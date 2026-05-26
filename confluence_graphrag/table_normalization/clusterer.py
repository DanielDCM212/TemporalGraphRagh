from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
from rapidfuzz import fuzz
from sklearn.cluster import DBSCAN

from .models import CanonicalSchema, TableFingerprint

logger = logging.getLogger(__name__)

# D2: thresholds for auto-assign vs review queue
AUTO_ASSIGN_THRESHOLD = 0.70
CLUSTER_MIN_SIMILARITY = 0.75


class FingerprintClusterer:
    """
    Groups TableFingerprints by header similarity using rapidfuzz + DBSCAN.
    This reduces LLM calls: instead of classifying 300 individual tables, we
    classify ~N clusters of similar tables.
    """

    def cluster(
        self, fingerprints: List[TableFingerprint]
    ) -> List[List[TableFingerprint]]:
        if not fingerprints:
            return []
        if len(fingerprints) == 1:
            return [fingerprints]

        n = len(fingerprints)
        sim = np.zeros((n, n))

        for i in range(n):
            sim[i][i] = 1.0
            for j in range(i + 1, n):
                score = _header_similarity(
                    fingerprints[i].raw_headers,
                    fingerprints[j].raw_headers,
                )
                sim[i][j] = score
                sim[j][i] = score

        dist = np.clip(1.0 - sim, 0.0, 1.0)

        labels = DBSCAN(
            eps=1.0 - CLUSTER_MIN_SIMILARITY,
            min_samples=1,
            metric='precomputed',
        ).fit(dist).labels_

        clusters: Dict[int, List[TableFingerprint]] = {}
        for idx, label in enumerate(labels):
            clusters.setdefault(label, []).append(fingerprints[idx])

        logger.debug(
            "Clustered %d fingerprints into %d groups", n, len(clusters)
        )
        return list(clusters.values())

    def best_match(
        self,
        fingerprint: TableFingerprint,
        schemas: List[CanonicalSchema],
    ) -> Tuple[Optional[CanonicalSchema], float]:
        """
        D2: find the closest approved schema for a new table.
        Returns (schema, score). Caller decides what to do based on thresholds.
        """
        best_schema: Optional[CanonicalSchema] = None
        best_score = 0.0

        for schema in schemas:
            # Compare against canonical names AND known raw variants so that
            # Spanish/typo headers match existing schemas that already know them.
            known_headers = schema.canonical_columns + list(schema.column_mapping.keys())
            score = _header_similarity(fingerprint.raw_headers, known_headers)
            if score > best_score:
                best_score = score
                best_schema = schema

        return best_schema, best_score


# ── Module-level helper (used by both clusterer and normalizer) ───────────────

def _header_similarity(headers_a: List[str], headers_b: List[str]) -> float:
    if not headers_a or not headers_b:
        return 0.0
    a = [h.lower().strip() for h in headers_a]
    b = [h.lower().strip() for h in headers_b]
    scores = [max(fuzz.ratio(ha, hb) / 100.0 for hb in b) for ha in a]
    return sum(scores) / len(scores)
