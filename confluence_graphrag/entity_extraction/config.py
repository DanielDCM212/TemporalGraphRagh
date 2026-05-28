from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExtractionConfig(BaseSettings):
    # External ID validation API
    validation_api_url: str = ""
    validation_api_key: str = ""
    validation_api_timeout: float = 10.0
    validation_concurrency: int = Field(default=10, ge=1)   # semaphore limit

    # Gemini / Vertex AI
    google_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gcp_project: str = ""
    gcp_location: str = "us-central1"
    vertex_ai_endpoint: str = ""

    # MongoDB (shared with ingestion config — read from same .env)
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db: str = "confluence_graphrag"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
