"""Self-service profile routes.

Always scoped to the authenticated user: there is no path parameter, so a
user can never address someone else's profile here. Admin edits of other
users live under /users.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.api.deps import CurrentUserDep, SessionDep, get_client_ip, get_user_agent
from app.schemas.auth import CurrentUser, ProfileSummary
from app.schemas.users import ProfileUpdateRequest
from app.services import user_service
from app.services.audit_service import AuditAction, record_audit_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("", response_model=CurrentUser)
async def get_profile(user: CurrentUserDep) -> CurrentUser:
    return CurrentUser.model_validate(user)


@router.put("", response_model=ProfileSummary)
async def update_profile(
    payload: ProfileUpdateRequest,
    request: Request,
    user: CurrentUserDep,
    db: SessionDep,
) -> ProfileSummary:
    profile = await user_service.upsert_profile(
        db,
        user,
        first_name=payload.first_name,
        last_name=payload.last_name,
        display_name=payload.display_name,
        timezone=payload.timezone,
        language=payload.language,
        avatar_url=payload.avatar_url,
    )
    await record_audit_event(
        db,
        action=AuditAction.PROFILE_UPDATED,
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=user.id,
        target_type="profile",
        target_id=str(profile.id),
        details=payload.model_dump(exclude_none=True),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(profile)
    return ProfileSummary.model_validate(profile)
