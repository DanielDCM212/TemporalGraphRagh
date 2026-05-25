from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString, Tag

from .models import (
    AttachmentRef,
    AttachmentType,
    ContentTree,
    ParsedCell,
    ParsedTable,
    Provenance,
    TableRef,
    TextChunk,
    TextStyle,
)

logger = logging.getLogger(__name__)


class HTMLContentParser:

    # D3 decision: Confluence uses only semantic tags for strikethrough
    CANCELLED_TAGS = {'s', 'strike', 'del'}
    BOLD_TAGS = {'strong', 'b'}

    ATTACHMENT_URL_MARKER = '/download/attachments/'

    ATTACHMENT_TYPE_MAP: Dict[str, AttachmentType] = {
        'pdf':  AttachmentType.PDF,
        'xlsx': AttachmentType.EXCEL,
        'xls':  AttachmentType.EXCEL,
        'docx': AttachmentType.DOCX,
        'doc':  AttachmentType.DOCX,
        'pptx': AttachmentType.PPTX,
        'ppt':  AttachmentType.PPTX,
        'png':  AttachmentType.IMAGE,
        'jpg':  AttachmentType.IMAGE,
        'jpeg': AttachmentType.IMAGE,
        'gif':  AttachmentType.IMAGE,
        'bmp':  AttachmentType.IMAGE,
        'webp': AttachmentType.IMAGE,
    }

    DATE_PATTERNS: List[Tuple[str, str]] = [
        (r'(\d{4}-\d{2}-\d{2})', '%Y-%m-%d'),
        (r'(\d{2}/\d{2}/\d{4})', '%d/%m/%Y'),
        (r'(\d{2}-\d{2}-\d{4})', '%d-%m-%Y'),
    ]

    def __init__(self, page_id: str, page_title: str):
        self.page_id = page_id
        self.page_title = page_title
        self.page_date = self._extract_date_from_title(page_title)
        self._table_counter = 0

    # ── Public API ───────────────────────────────────────────────────────────

    def parse(self, html_content: str) -> ContentTree:
        soup = BeautifulSoup(html_content, 'html.parser')
        self._table_counter = 0

        return ContentTree(
            page_id=self.page_id,
            page_title=self.page_title,
            page_date=self.page_date,
            text_blocks=self._extract_page_level_text(soup),
            tables=self._extract_tables(soup, parent_provenance=None),
            attachments=self._extract_page_level_attachments(soup),
            raw_html=html_content,
        )

    # ── Date extraction ──────────────────────────────────────────────────────

    def _extract_date_from_title(self, title: str) -> datetime:
        for pattern, fmt in self.DATE_PATTERNS:
            match = re.search(pattern, title)
            if match:
                try:
                    return datetime.strptime(match.group(1), fmt)
                except ValueError:
                    continue
        return self._extract_date_with_llm(title)

    def _extract_date_with_llm(self, title: str) -> datetime:
        # LLM fallback — wired in Stage 4 (Entity Extraction)
        logger.warning("Date not found in page title '%s'. Using sentinel datetime.min.", title)
        return datetime.min

    # ── Table extraction ─────────────────────────────────────────────────────

    def _extract_tables(
        self,
        context: Tag,
        parent_provenance: Optional[Provenance],
        parent_row: Optional[int] = None,
        parent_col: Optional[int] = None,
    ) -> List[ParsedTable]:
        tables: List[ParsedTable] = []

        for table_tag in self._direct_subtables(context):
            table_index = self._table_counter
            self._table_counter += 1

            if parent_provenance and parent_provenance.table_chain:
                chain = parent_provenance.table_chain.copy()
                chain.append(TableRef(table_index=table_index, row=parent_row, col=parent_col))
            else:
                chain = [TableRef(table_index=table_index, row=None, col=None)]

            table_prov = Provenance(
                page_id=self.page_id,
                page_title=self.page_title,
                page_date=self.page_date,
                table_chain=chain,
            )
            tables.append(self._parse_table(table_tag, table_prov))

        return tables

    def _direct_subtables(self, context: Tag) -> List[Tag]:
        """Tables inside context that are not nested within another table inside context."""
        result: List[Tag] = []
        for table in context.find_all('table'):
            for parent in table.parents:
                if parent is context:
                    result.append(table)
                    break
                if parent.name == 'table':
                    break
        return result

    def _parse_table(self, table_tag: Tag, table_prov: Provenance) -> ParsedTable:
        rows = self._collect_rows(table_tag)

        # D4: occupation map tracks cells propagated forward via rowspan
        occupied: Dict[Tuple[int, int], ParsedCell] = {}

        headers: List[str] = []
        cells_matrix: List[List[ParsedCell]] = []

        for row_idx, row_tag in enumerate(rows):
            cell_tags = row_tag.find_all(['td', 'th'], recursive=False)
            is_header_row = (
                row_idx == 0
                and bool(cell_tags)
                and all(c.name == 'th' for c in cell_tags)
            )

            row_cells: List[ParsedCell] = []
            col_cursor = 0
            cell_iter: Iterator[Tag] = iter(cell_tags)
            current_tag: Optional[Tag] = next(cell_iter, None)

            while current_tag is not None or (row_idx, col_cursor) in occupied:
                # Occupied slot from a previous row's rowspan — insert propagated cell
                if (row_idx, col_cursor) in occupied:
                    row_cells.append(occupied[(row_idx, col_cursor)])
                    col_cursor += 1
                    continue

                if current_tag is None:
                    break

                rowspan = self._safe_int(current_tag.get('rowspan'), default=1)
                colspan = self._safe_int(current_tag.get('colspan'), default=1)

                cell_prov = Provenance(
                    page_id=self.page_id,
                    page_title=self.page_title,
                    page_date=self.page_date,
                    table_chain=table_prov.table_chain.copy(),
                    row=row_idx,
                    col=col_cursor,
                )
                parsed_cell = self._parse_cell(current_tag, row_idx, col_cursor, cell_prov)
                row_cells.append(parsed_cell)

                # D4: propagate cell content to future rows covered by rowspan
                if rowspan > 1:
                    for rs in range(1, rowspan):
                        for cs in range(colspan):
                            target_row = row_idx + rs
                            target_col = col_cursor + cs
                            occupied[(target_row, target_col)] = ParsedCell(
                                row=target_row,
                                col=target_col,
                                text_chunks=parsed_cell.text_chunks,
                                attachments=parsed_cell.attachments,
                                sub_tables=parsed_cell.sub_tables,
                                provenance=Provenance(
                                    page_id=self.page_id,
                                    page_title=self.page_title,
                                    page_date=self.page_date,
                                    table_chain=table_prov.table_chain.copy(),
                                    row=target_row,
                                    col=target_col,
                                ),
                                is_propagated=True,
                            )

                col_cursor += colspan
                current_tag = next(cell_iter, None)

            # Drain any remaining occupied slots at the end of this row
            while (row_idx, col_cursor) in occupied:
                row_cells.append(occupied[(row_idx, col_cursor)])
                col_cursor += 1

            if is_header_row:
                headers = [
                    cell.text_chunks[0].content if cell.text_chunks else ''
                    for cell in row_cells
                ]

            cells_matrix.append(row_cells)

        return ParsedTable(
            table_index=table_prov.table_chain[-1].table_index,
            headers=headers,
            cells=cells_matrix,
            provenance=table_prov,
            raw_html=str(table_tag),
        )

    @staticmethod
    def _collect_rows(table_tag: Tag) -> List[Tag]:
        rows: List[Tag] = []
        for section in table_tag.find_all(['thead', 'tbody', 'tfoot'], recursive=False):
            rows.extend(section.find_all('tr', recursive=False))
        if not rows:
            rows = table_tag.find_all('tr', recursive=False)
        return rows

    # ── Cell parsing ─────────────────────────────────────────────────────────

    def _parse_cell(self, cell_tag: Tag, row: int, col: int, provenance: Provenance) -> ParsedCell:
        return ParsedCell(
            row=row,
            col=col,
            text_chunks=self._extract_text_from_tag(cell_tag, provenance),
            attachments=self._extract_attachments_from_tag(cell_tag, provenance),
            sub_tables=self._extract_tables(cell_tag, provenance, row, col),
            provenance=provenance,
        )

    # ── Text extraction ──────────────────────────────────────────────────────

    def _extract_text_from_tag(self, tag: Tag, provenance: Provenance) -> List[TextChunk]:
        chunks: List[TextChunk] = []
        for node in tag.descendants:
            if not isinstance(node, NavigableString):
                continue
            text = str(node).strip()
            if not text:
                continue
            if self._has_table_ancestor(node, tag):
                continue
            if self._has_ignored_ancestor(node, tag):
                continue
            chunks.append(TextChunk(content=text, style=self._detect_style(node), provenance=provenance))
        return self._merge_chunks(chunks)

    def _extract_page_level_text(self, soup: BeautifulSoup) -> List[TextChunk]:
        page_prov = self._page_provenance()
        chunks: List[TextChunk] = []
        for node in soup.descendants:
            if not isinstance(node, NavigableString):
                continue
            text = str(node).strip()
            if not text:
                continue
            if node.find_parent('table'):
                continue
            if self._has_ignored_ancestor(node, soup):
                continue
            chunks.append(TextChunk(content=text, style=self._detect_style(node), provenance=page_prov))
        return self._merge_chunks(chunks)

    # ── Attachment extraction ────────────────────────────────────────────────

    def _extract_attachments_from_tag(self, tag: Tag, provenance: Provenance) -> List[AttachmentRef]:
        attachments: List[AttachmentRef] = []
        seen: set = set()

        for a_tag in tag.find_all('a', href=True):
            href: str = a_tag['href']
            if self.ATTACHMENT_URL_MARKER not in href:
                continue
            if self._has_table_ancestor(a_tag, tag):
                continue
            if href in seen:
                continue
            seen.add(href)
            filename = href.split('/')[-1].split('?')[0]
            attachments.append(AttachmentRef(
                attachment_id=href,
                filename=filename,
                url=href,
                attachment_type=self._attachment_type(filename),
                provenance=provenance,
            ))

        print("!!!! ATTACHMENTS: ", attachments, " !!!!")
        # Confluence ac:image / ri:attachment tags
        for ac_img in tag.find_all('ac:image'):
            if self._has_table_ancestor(ac_img, tag):
                continue
            ri_att = ac_img.find('ri:attachment')
            if not ri_att:
                continue
            filename = ri_att.get('ri:filename', '')
            if not filename or filename in seen:
                continue
            seen.add(filename)
            attachments.append(AttachmentRef(
                attachment_id=filename,
                filename=filename,
                url='',  # resolved later via Confluence API (attachment_resolver)
                attachment_type=self._attachment_type(filename),
                provenance=provenance,
            ))

        return attachments

    def _extract_page_level_attachments(self, soup: BeautifulSoup) -> List[AttachmentRef]:
        """Attachments that live outside of any table (page-level)."""
        page_prov = self._page_provenance()
        attachments: List[AttachmentRef] = []
        seen: set = set()

        for a_tag in soup.find_all('a', href=True):
            href: str = a_tag['href']
            if self.ATTACHMENT_URL_MARKER not in href:
                continue
            if a_tag.find_parent('table'):
                continue
            if href in seen:
                continue
            seen.add(href)
            filename = href.split('/')[-1].split('?')[0]
            attachments.append(AttachmentRef(
                attachment_id=href,
                filename=filename,
                url=href,
                attachment_type=self._attachment_type(filename),
                provenance=page_prov,
            ))

        for ac_img in soup.find_all('ac:image'):
            if ac_img.find_parent('table'):
                continue
            ri_att = ac_img.find('ri:attachment')
            if not ri_att:
                continue
            filename = ri_att.get('ri:filename', '')
            if not filename or filename in seen:
                continue
            seen.add(filename)
            attachments.append(AttachmentRef(
                attachment_id=filename,
                filename=filename,
                url='',
                attachment_type=self._attachment_type(filename),
                provenance=page_prov,
            ))

        return attachments

    # ── Style detection (D3: semantic tags only) ─────────────────────────────

    def _detect_style(self, node) -> TextStyle:
        for el in [node, *node.parents]:
            name = getattr(el, 'name', None)
            if not name:
                continue
            if name in self.CANCELLED_TAGS:
                return TextStyle.CANCELLED
            if name in self.BOLD_TAGS:
                return TextStyle.BOLD
            if name == 'u':
                return TextStyle.UNDERLINE
            if name in ('em', 'i'):
                return TextStyle.ITALIC
        return TextStyle.NORMAL

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _page_provenance(self) -> Provenance:
        return Provenance(
            page_id=self.page_id,
            page_title=self.page_title,
            page_date=self.page_date,
        )

    def _attachment_type(self, filename: str) -> AttachmentType:
        if '.' not in filename:
            return AttachmentType.UNKNOWN
        ext = filename.rsplit('.', 1)[-1].lower()
        return self.ATTACHMENT_TYPE_MAP.get(ext, AttachmentType.UNKNOWN)

    @staticmethod
    def _has_table_ancestor(node, boundary: Tag) -> bool:
        """True if node has a <table> ancestor between itself and boundary."""
        for parent in node.parents:
            if parent is boundary:
                return False
            if getattr(parent, 'name', None) == 'table':
                return True
        return False

    @staticmethod
    def _has_ignored_ancestor(node, boundary: Tag) -> bool:
        """True if node is inside <script>, <style>, or <head>."""
        ignored = {'script', 'style', 'head'}
        for parent in node.parents:
            if parent is boundary:
                return False
            if getattr(parent, 'name', None) in ignored:
                return True
        return False

    @staticmethod
    def _safe_int(value, default: int) -> int:
        try:
            return max(default, int(value or default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _merge_chunks(chunks: List[TextChunk]) -> List[TextChunk]:
        """Merge consecutive TextChunks with the same style into one."""
        if not chunks:
            return []
        merged = [chunks[0]]
        for chunk in chunks[1:]:
            last = merged[-1]
            if chunk.style == last.style:
                merged[-1] = TextChunk(
                    content=last.content + ' ' + chunk.content,
                    style=last.style,
                    provenance=last.provenance,
                )
            else:
                merged.append(chunk)
        return merged
