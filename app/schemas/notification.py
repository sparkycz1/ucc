"""Pydantic schemas for the Notifications forms (`app/web/routes/notifications.py`)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.db.models.notification_log import NotificationDeliveryChannel
from app.db.models.notification_rule import NotificationEventType


class NotificationRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1024)
    enabled: bool = True
    event_types: list[str] = Field(default_factory=list)
    delivery_channel: str = NotificationDeliveryChannel.EMAIL.value
    webhook_url: str | None = Field(default=None, max_length=2048)

    @field_validator("event_types")
    @classmethod
    def _validate_event_types(cls, value: list[str]) -> list[str]:
        parsed: list[str] = []
        for raw in value:
            try:
                parsed.append(NotificationEventType(raw).value)
            except ValueError:
                raise ValueError(f'Unknown event type "{raw}".') from None
        if not parsed:
            raise ValueError("Choose at least one event to notify on.")
        # De-duplicate, preserving order — a rule submitted twice for the
        # same event (unlikely from the checkbox UI, always possible from
        # the API) should behave the same as listing it once.
        return list(dict.fromkeys(parsed))

    @field_validator("delivery_channel")
    @classmethod
    def _validate_delivery_channel(cls, value: str) -> str:
        try:
            return NotificationDeliveryChannel(value).value
        except ValueError:
            raise ValueError(f'Unknown delivery channel "{value}".') from None

    def model_post_init(self, __context: object) -> None:
        if self.delivery_channel == NotificationDeliveryChannel.WEBHOOK.value:
            if not self.webhook_url:
                raise ValueError("A webhook URL is required when delivering via webhook.")
            if not self.webhook_url.startswith(("http://", "https://")):
                raise ValueError("The webhook URL must start with http:// or https://.")


class NotificationTemplateUpdate(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=4000)


class NotificationCustomTemplateCreate(BaseModel):
    """A named, reusable template (see `NotificationCustomTemplate`) — same
    fields as `NotificationTemplateUpdate` plus the name that makes it
    selectable from a rule."""

    name: str = Field(min_length=1, max_length=255)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=4000)
