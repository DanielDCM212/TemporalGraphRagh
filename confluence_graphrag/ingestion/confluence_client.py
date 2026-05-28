from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import AsyncIterator, List, Optional

import httpx
from atlassian import Confluence

from .config import IngestionConfig
from .models import AttachmentMetadata, PageMetadata

logger = logging.getLogger(__name__)

_DATE_PATTERNS = [
    (r'(\d{4}-\d{2}-\d{2})', '%Y-%m-%d'),
    (r'(\d{2}/\d{2}/\d{4})', '%d/%m/%Y'),
    (r'(\d{2}-\d{2}-\d{4})', '%d-%m-%Y'),
]


def _parse_date_from_title(title: str) -> Optional[datetime]:
    for pattern, fmt in _DATE_PATTERNS:
        match = re.search(pattern, title)
        if match:
            try:
                return datetime.strptime(match.group(1), fmt)
            except ValueError:
                continue
    return None


def _parse_confluence_timestamp(ts: str) -> datetime:
    """Parse Confluence ISO-8601 timestamp to a naive UTC datetime."""
    return datetime.fromisoformat(ts.replace('Z', '+00:00')).replace(tzinfo=None)


class ConfluenceClient:
    """
    Async wrapper around atlassian-python-api.

    All sync SDK calls are offloaded to a thread via asyncio.to_thread so the
    event loop is never blocked.  File downloads use httpx directly.
    """

    def __init__(self, config: IngestionConfig):
        self._config = config
        self._sdk = Confluence(
            url=config.confluence_url,
            username=config.confluence_username,
            token=config.confluence_api_token,
            cloud=config.confluence_cloud,
            verify=config.confluence_verify_ssl,
        )
        self._http = httpx.AsyncClient(
            auth=(config.confluence_username, config.confluence_api_token),
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> ConfluenceClient:
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ── Page iteration ───────────────────────────────────────────────────────

    async def iter_pages(
        self,
        space_key: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> AsyncIterator[PageMetadata]:
        """
        Yields all current pages in the space, sorted oldest-first by content
        date (date extracted from the title).  Date filters apply to the content
        date, not the Confluence modification timestamp.
        """
        pages: List[PageMetadata] = []
        offset = 0
        limit = self._config.confluence_page_limit

        while True:
            batch = await asyncio.to_thread(
                self._sdk.get_all_pages_from_space,
                space_key,
                start=offset,
                limit=limit,
                status='current',
                expand='body.storage,body.view,version,history.lastUpdated',
            )
            if not batch:
                break

            for raw in batch:
                page = self._parse_page(raw, space_key)
                if start_date and page.page_date < start_date:
                    continue
                if end_date and page.page_date > end_date:
                    continue
                pages.append(page)

            if len(batch) < limit:
                break
            offset += limit

        # Sort oldest-first so Graphiti builds the timeline correctly
        pages.sort(key=lambda p: p.page_date)

        for page in pages:
            yield page

    async def get_page(self, page_id: str) -> PageMetadata:
        raw = await asyncio.to_thread(
            self._sdk.get_page_by_id,
            page_id,
            expand='body.storage,body.view,version,history.lastUpdated',
        )
        return self._parse_page(raw, space_key='')

    # ── Attachments ──────────────────────────────────────────────────────────

    async def get_attachments(self, page_id: str) -> List[AttachmentMetadata]:
        raw = await asyncio.to_thread(
            self._sdk.get_attachments_from_content,
            page_id,
        )
        base_url = self._config.confluence_url.rstrip('/')
        attachments: List[AttachmentMetadata] = []

        for result in raw.get('results', []):
            download_path = result.get('_links', {}).get('download', '')
            attachments.append(AttachmentMetadata(
                attachment_id=result['id'],
                filename=result['title'],
                media_type=result.get('metadata', {}).get('mediaType', 'application/octet-stream'),
                download_url=f"{base_url}{download_path}",
                size_bytes=result.get('extensions', {}).get('fileSize', 0),
            ))

        return attachments

    async def download_attachment(self, download_url: str) -> bytes:
        response = await self._http.get(download_url)
        response.raise_for_status()
        return response.content

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _parse_page(self, raw: dict, space_key: str) -> PageMetadata:
        title = raw['title']

        html_content = raw.get('body', {}).get('view', {}).get('value', '')

        version_when = (
            raw.get('version', {}).get('when')
            or raw.get('history', {}).get('lastUpdated', {}).get('when', '')
        )
        last_modified = (
            _parse_confluence_timestamp(version_when) if version_when else datetime.min
        )

        page_date = _parse_date_from_title(title)
        if page_date is None:
            logger.debug("No date in title '%s', using last_modified as page_date", title)
            page_date = last_modified

        return PageMetadata(
            page_id=raw['id'],
            title=title,
            space_key=space_key or raw.get('space', {}).get('key', ''),
            html_content=html_content,
            last_modified=last_modified,
            page_date=page_date,
        )
