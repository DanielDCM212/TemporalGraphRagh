from __future__ import annotations

import math
from datetime import datetime, timezone

_HALF_LIFE_DAYS = 365.0


def temporal_score(event_date: datetime, query_date: datetime | None = None) -> float:
    """
    Exponential decay score based on elapsed time.
    Events closer to query_date score higher; half-life is 365 days.

    Returns a value in (0, 1].
    """
    if query_date is None:
        query_date = datetime.now(tz=timezone.utc)

    # Normalise both to UTC-aware for safe subtraction
    if event_date.tzinfo is None:
        event_date = event_date.replace(tzinfo=timezone.utc)
    if query_date.tzinfo is None:
        query_date = query_date.replace(tzinfo=timezone.utc)

    days_elapsed = max(0.0, (query_date - event_date).total_seconds() / 86_400)
    return math.exp(-math.log(2) * days_elapsed / _HALF_LIFE_DAYS)


def combined_score(semantic_similarity: float, event_date: datetime, query_date: datetime | None = None) -> float:
    """Final retrieval score: semantic × temporal."""
    return semantic_similarity * temporal_score(event_date, query_date)
