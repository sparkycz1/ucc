"""The primary and the fallback AI model (Settings → AI), for release
assessments and the assistant alike.

`model_chain` is what to try, in order: the primary (or, for an assistant
conversation, the model it was started with), then the fallback — each only
if it and its provider are still enabled, the fallback only if it is a
different model. A caller tries the next one when the call fails: a
provider error, a timeout (each provider's own, `AiProviderConfig.
timeout_seconds`), or an answer it can't use.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ai_model import AiModel
from app.db.models.ai_provider import AiProviderConfig
from app.db.models.app_settings import AppSettings


@dataclass(frozen=True)
class Candidate:
    provider: AiProviderConfig
    model: AiModel
    # The fallback model (Settings → AI), not the first choice.
    fallback: bool = False

    @property
    def label(self) -> str:
        return f"{self.provider.kind.value}/{self.model.model_id}"


async def enabled_model(
    db: AsyncSession, model_id: uuid.UUID | None
) -> tuple[AiProviderConfig, AiModel] | None:
    """The model and its provider, if both are still enabled."""
    if model_id is None:
        return None
    model = await db.get(AiModel, model_id)
    if model is None or not model.enabled:
        return None
    provider = await db.get(AiProviderConfig, model.provider_id)
    if provider is None or not provider.enabled:
        return None
    return provider, model


async def model_chain(
    db: AsyncSession,
    app_settings: AppSettings,
    first: tuple[AiProviderConfig, AiModel] | None = None,
) -> list[Candidate]:
    """What to try, in order: `first` (default: the primary model), then
    the fallback. Empty when neither is usable."""
    chain: list[Candidate] = []
    primary = first or await enabled_model(db, app_settings.release_assessment_model_id)
    if primary is not None:
        chain.append(Candidate(*primary))
    fallback = await enabled_model(db, app_settings.ai_fallback_model_id)
    if fallback is not None and all(c.model.id != fallback[1].id for c in chain):
        chain.append(Candidate(*fallback, fallback=True))
    return chain
