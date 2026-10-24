"""Public service liveness endpoint.

The health response intentionally contains only a coarse service status and the
current UTC timestamp. It does not inspect or report configuration, secrets, or
external dependency state.

Requirements: 11.1, 11.2.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict


router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """The deliberately minimal public health payload."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    timestamp: datetime


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Return service liveness without exposing operational details."""
    return HealthResponse(
        status="ok",
        timestamp=datetime.now(timezone.utc),
    )
