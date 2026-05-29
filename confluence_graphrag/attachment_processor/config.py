from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AttachmentConfig(BaseSettings):
    """Configuration for Stage 3A — Attachment Processor."""

    # Size guard — skip files larger than this
    max_attachment_size_bytes: int = Field(default=26_214_400, ge=1)  # 25 MB

    # Chunking
    chunk_size: int = Field(default=1000, ge=100)
    chunk_overlap: int = Field(default=150, ge=0)

    # Concurrency: number of attachments downloaded/extracted in parallel
    attachment_concurrency: int = Field(default=4, ge=1)

    # Gemini Vision for images (and scanned-PDF fallback)
    enable_vision: bool = True
    vision_model: str = "gemini-2.5-flash"

    # GCP (shared with ExtractionConfig / GraphConfig)
    gcp_project: str = ""
    gcp_location: str = "us-central1"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
