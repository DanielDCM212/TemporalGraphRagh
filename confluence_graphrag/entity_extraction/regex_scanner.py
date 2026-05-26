from __future__ import annotations

import re
from typing import List

from .models import CandidateId

# Word-boundary patterns guarantee no overlap:
# \b(\d{11})\b will NOT match a 9-digit substring inside "12345678901"
# because the digit after position 9 is still a \w char (no \b there).
_APP_ID_RE     = re.compile(r'\b(\d{11})\b')
_PROJECT_ID_RE = re.compile(r'\b(\d{9})\b')

_CONTEXT_WINDOW = 60   # characters of surrounding text kept for manual review


class RegexScanner:
    """
    First-pass ID detector using regex.
    Fast and zero-cost — runs before any LLM call.

    app_id    — exactly 11 digits
    project_id — exactly 9 digits

    Word boundaries (\b) ensure an 11-digit number is never also matched
    as a 9-digit project_id (the inner 9 digits have no boundary mid-sequence).
    """

    def scan(self, text: str, provenance_path: str) -> List[CandidateId]:
        candidates: List[CandidateId] = []
        seen: set = set()

        for match in _APP_ID_RE.finditer(text):
            val = match.group(1)
            if val in seen:
                continue
            seen.add(val)
            candidates.append(CandidateId(
                raw_value=val,
                id_type="app_id",
                context=_context(text, match),
                provenance_path=provenance_path,
            ))

        for match in _PROJECT_ID_RE.finditer(text):
            val = match.group(1)
            if val in seen:
                continue
            seen.add(val)
            candidates.append(CandidateId(
                raw_value=val,
                id_type="project_id",
                context=_context(text, match),
                provenance_path=provenance_path,
            ))

        return candidates

    def scan_many(
        self, chunks: List[tuple[str, str]]
    ) -> List[CandidateId]:
        """
        Scan a list of (text, provenance_path) pairs.
        Deduplicates across chunks — the same ID value is only returned once
        (first occurrence wins for provenance).
        """
        all_candidates: List[CandidateId] = []
        seen_values: set = set()

        for text, prov_path in chunks:
            for candidate in self.scan(text, prov_path):
                if candidate.raw_value not in seen_values:
                    seen_values.add(candidate.raw_value)
                    all_candidates.append(candidate)

        return all_candidates


def _context(text: str, match: re.Match) -> str:
    start = max(0, match.start() - _CONTEXT_WINDOW)
    end   = min(len(text), match.end() + _CONTEXT_WINDOW)
    return text[start:end]
