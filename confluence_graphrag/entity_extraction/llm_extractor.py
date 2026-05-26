from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from .config import ExtractionConfig
from .models import LLMExtractionResult
from .prompts import ENTITY_EXTRACTION_PROMPT

logger = logging.getLogger(__name__)


class LLMEntityExtractor:
    """
    Calls Gemini Pro once per page to:
      1. Find IDs that regex missed (non-standard formats like "PRJ-123.456.789")
      2. Extract structured events (decisions, approvals, cancellations, etc.)

    The LLM is initialized lazily so importing this class never requires
    Vertex AI credentials to be present.
    """

    def __init__(self, config: ExtractionConfig) -> None:
        self._config = config
        self._llm = None

    def _get_llm(self):
        if self._llm is None:
            from langchain_google_genai import ChatGoogleGenerativeAI
            from langchain.output_parsers import PydanticOutputParser

            self._llm = ChatGoogleGenerativeAI(
                model=self._config.gemini_model,
                temperature=0,
                google_api_key=self._config.google_api_key or None,
            )
            self._parser = PydanticOutputParser(pydantic_object=LLMExtractionResult)
        return self._llm, self._parser

    async def extract(
        self,
        text: str,
        page_date: datetime,
        provenance_path: str,
        has_cancelled_items: bool = False,
    ) -> LLMExtractionResult:
        if not text.strip():
            return LLMExtractionResult()

        llm, parser = self._get_llm()

        prompt = ENTITY_EXTRACTION_PROMPT.format(
            text=text[:8000],  # cap to avoid token overflow on very large pages
            page_date=page_date.strftime("%Y-%m-%d"),
            provenance_path=provenance_path,
            has_cancelled_items=has_cancelled_items,
            format_instructions=parser.get_format_instructions(),
        )

        try:
            response = await asyncio.to_thread(llm.invoke, prompt)
            return parser.parse(response.content)
        except Exception as exc:
            logger.error("LLM extraction failed for '%s': %s", provenance_path, exc)
            return LLMExtractionResult()
