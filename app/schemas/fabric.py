"""Pydantic schemas for the Fabric forms."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.releases.policy import clean_stages


class FabricForm(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    # Required on create; on edit, blank means "keep the stored key".
    api_key: str | None = Field(default=None, max_length=512)
    is_enabled: bool = True
    # NULL = use the global acceptable level (Settings → Assessments).
    max_acceptable_risk_level: int | None = Field(default=None, ge=1, le=5)
    # NULL = use the global hidden stages / sync interval (`app.releases.policy`).
    hidden_release_stages: list[str] | None = None
    inventory_sync_interval_seconds: int | None = Field(default=None, ge=60, le=86400)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name can't be empty.")
        return stripped

    @field_validator("hidden_release_stages")
    @classmethod
    def _known_stages(cls, value: list[str] | None) -> list[str] | None:
        return clean_stages(value)

    @field_validator("api_key")
    @classmethod
    def _strip_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None
