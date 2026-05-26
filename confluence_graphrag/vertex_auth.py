from __future__ import annotations

import logging
import os
from typing import Optional

from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

_genai_client = None

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def _default_credentials() -> Credentials:
    import google.auth
    creds, _ = google.auth.default(scopes=_SCOPES)
    return creds


def get_genai_client(
    *,
    project: str = "",
    location: str = "",
    credentials: Optional[Credentials] = None,
):
    """Return a cached google.genai.Client.

    Vertex AI is used when *project* resolves to a non-empty value; otherwise
    falls back to Application Default Credentials.

    Resolution order for each setting:
      1. Argument passed to this call
      2. Environment variables: GCP_PROJECT, GCP_LOCATION
      3. Built-in default (location → "us-central1")

    When VERTEX_API_ENDPOINT is set, the Vertex AI client is pointed at that
    custom endpoint (e.g. a private service endpoint or a local emulator).

    The client is cached after the first successful call; subsequent calls
    ignore any arguments and return the cached instance.
    """
    global _genai_client
    if _genai_client is not None:
        return _genai_client

    import google.genai as genai

    _project = project or os.getenv("GCP_PROJECT", "")
    _location = location or os.getenv("GCP_LOCATION", "us-central1")
    _endpoint = os.getenv("VERTEX_API_ENDPOINT", "")
    _creds = credentials or _default_credentials()

    if _project:
        kwargs: dict = dict(
            vertexai=True,
            project=_project,
            location=_location,
            credentials=_creds,
        )
        if _endpoint:
            kwargs["http_options"] = {"api_endpoint": _endpoint}
            logger.debug(
                "vertex_auth: Vertex AI client  project=%s  location=%s  endpoint=%s",
                _project, _location, _endpoint,
            )
        else:
            logger.debug(
                "vertex_auth: Vertex AI client  project=%s  location=%s",
                _project, _location,
            )
        _genai_client = genai.Client(**kwargs)
    else:
        logger.debug("vertex_auth: ADC client (no project set)")
        _genai_client = genai.Client(credentials=_creds)

    return _genai_client


def get_chat_llm(
    model: str = "gemini-2.5-flash",
    *,
    temperature: float = 0,
    credentials: Optional[Credentials] = None,
):
    """Return a LangChain ChatGoogleGenerativeAI instance.

    Uses *credentials* when provided, otherwise falls back to ADC.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    _creds = credentials or _default_credentials()
    return ChatGoogleGenerativeAI(
        model=model,
        temperature=temperature,
        credentials=_creds,
    )


def get_embeddings(
    model: str = "models/text-embedding-004",
    *,
    credentials: Optional[Credentials] = None,
):
    """Return a LangChain GoogleGenerativeAIEmbeddings instance.

    Uses *credentials* when provided, otherwise falls back to ADC.
    """
    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    _creds = credentials or _default_credentials()
    return GoogleGenerativeAIEmbeddings(
        model=model,
        credentials=_creds,
    )


def reset_genai_client() -> None:
    """Clear the cached genai client (useful in tests)."""
    global _genai_client
    _genai_client = None
