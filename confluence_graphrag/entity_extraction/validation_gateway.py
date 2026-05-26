from __future__ import annotations

import asyncio
import logging
from typing import List

import httpx

from .config import ExtractionConfig
from .models import CandidateId, ValidatedId

logger = logging.getLogger(__name__)


class ValidationGateway:
    """
    Validates candidate IDs against the external API before any ID reaches
    the graph store.  Unconfirmed IDs are returned as is_valid=False and
    routed to the unvalidated_ids collection by the caller.

    A timeout or API error never blocks the pipeline — the candidate is
    simply marked unvalidated.
    """

    def __init__(self, config: ExtractionConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._config.validation_api_url,
                headers={"Authorization": f"Bearer {self._config.validation_api_key}"},
                timeout=self._config.validation_api_timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def validate(self, candidate: CandidateId) -> ValidatedId:
        if not self._config.validation_api_url:
            # API not configured — treat all as unvalidated (useful for dev/testing)
            logger.debug("Validation API not configured, skipping '%s'", candidate.raw_value)
            return ValidatedId(candidate=candidate, is_valid=False)

        client = self._get_client()
        try:
            response = await client.get(
                "/validate",
                params={"id": candidate.raw_value, "type": candidate.id_type},
            )
            if response.status_code == 200:
                return ValidatedId(
                    candidate=candidate,
                    is_valid=True,
                    api_response=response.json(),
                )
            logger.debug(
                "ID '%s' rejected by validation API (status=%d)",
                candidate.raw_value, response.status_code,
            )
        except httpx.TimeoutException:
            logger.warning(
                "Validation API timeout for '%s' — marking unvalidated",
                candidate.raw_value,
            )
        except Exception as exc:
            logger.error("Validation error for '%s': %s", candidate.raw_value, exc)

        return ValidatedId(candidate=candidate, is_valid=False)

    async def validate_batch(
        self, candidates: List[CandidateId]
    ) -> List[ValidatedId]:
        """Validate up to `validation_concurrency` IDs in parallel."""
        if not candidates:
            return []

        semaphore = asyncio.Semaphore(self._config.validation_concurrency)

        async def _validate(c: CandidateId) -> ValidatedId:
            async with semaphore:
                return await self.validate(c)

        return list(await asyncio.gather(*[_validate(c) for c in candidates]))
