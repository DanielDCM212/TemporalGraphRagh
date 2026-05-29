from __future__ import annotations

import base64
import logging
from typing import List, Tuple

from ...parser.models import ParsedTable, Provenance

logger = logging.getLogger(__name__)

_VISION_PROMPT = (
    "You are processing an image attached to a meeting minutes document. "
    "Describe all text content, tables, and data visible in the image in detail. "
    "If the image contains a table, reproduce it in plain text with | separators. "
    "Be comprehensive — your description will be used for knowledge retrieval."
)


class ImageExtractor:
    """
    Extract text from images using Gemini Vision (google-genai).

    Accepts PNG / JPEG / GIF / BMP / WEBP.  Returns (description, []) — images
    do not produce ParsedTable objects; their content is chunk-embedded as text.
    """

    def __init__(self, model: str = "gemini-2.5-flash", gcp_project: str = "", gcp_location: str = "us-central1") -> None:
        self._model = model
        self._gcp_project = gcp_project
        self._gcp_location = gcp_location

    def extract(
        self,
        data: bytes,
        filename: str,
        provenance: Provenance,
    ) -> Tuple[str, List[ParsedTable]]:
        try:
            from ....vertex_auth import get_genai_client
        except ImportError:
            from ...vertex_auth import get_genai_client

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpeg"
        mime_map = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "bmp": "image/bmp",
            "webp": "image/webp",
        }
        mime_type = mime_map.get(ext, "image/jpeg")

        client = get_genai_client(
            project=self._gcp_project,
            location=self._gcp_location,
        )

        # google-genai inline image part
        import google.genai.types as genai_types

        response = client.models.generate_content(
            model=self._model,
            contents=[
                genai_types.Part.from_bytes(data=data, mime_type=mime_type),
                _VISION_PROMPT,
            ],
        )
        description = response.text or ""
        logger.debug("Vision extracted %d chars from %s", len(description), filename)
        return description, []
