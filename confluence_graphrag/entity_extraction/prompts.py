ENTITY_EXTRACTION_PROMPT = """\
You are an information extractor specialized in corporate meeting minutes.

Analyze the text below and extract:

1. **Additional IDs** not caught by regex (they may appear in non-standard formats):
   - app_id: 11-digit numbers (e.g. "APP: 12345678901", "Application #12345678901")
   - project_id: 9-digit numbers (e.g. "Project #123456789", "PRJ-123.456.789", "123/456/789")
   Only include IDs you are confident about. Return the digits only (no dashes, dots, spaces).

2. **Events**: for each significant event, provide:
   - event_type: one of "decision", "status_change", "cancellation", "approval", "risk", "action_item"
   - description: concise summary in the original language of the text
   - app_ids / project_ids: related validated IDs mentioned in the same sentence or paragraph
   - is_cancelled: true if the item was explicitly marked as cancelled or struck-through

Cancelled items context: {has_cancelled_items}
Page date: {page_date}
Provenance: {provenance_path}

Text:
{text}

{format_instructions}
"""
