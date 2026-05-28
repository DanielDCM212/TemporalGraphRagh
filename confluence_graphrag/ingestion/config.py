from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class IngestionConfig(BaseSettings):
    # Confluence connection
    confluence_url: str
    confluence_username: str
    confluence_api_token: str
    confluence_cloud: bool = True       # False for Confluence Server / Data Center
    confluence_verify_ssl: bool = True
    confluence_ca_bundle: str = ""      # path to custom CA bundle; empty means use system default

    # MongoDB
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db: str = "confluence_graphrag"

    # Ingestion behavior
    incremental_poll_interval_hours: int = Field(default=24, ge=1)   # D5
    confluence_page_limit: int = Field(default=50, ge=1, le=100)     # pages per API call
    max_retry_count: int = Field(default=3, ge=0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
