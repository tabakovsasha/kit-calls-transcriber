"""Admin-only runtime settings: Whisper model, performance profile.

Persists to the database so UI changes survive restarts. The in-process cache is
refreshed on write, and read synchronously by the worker at job start.
"""

from fastapi import APIRouter

from app.api.deps import AdminUserDep, SessionDep
from app.core.runtime import runtime
from app.schemas.settings import (
    DefaultModelRequest,
    ModelActionResponse,
    PerformanceProfileRequest,
    RuntimeSettingsResponse,
    WhisperModelsResponse,
)
from app.services import app_settings_service, hardware_service, whisper_service

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/hardware")
async def get_hardware_capabilities(
    _admin: AdminUserDep,
):
    """Snapshot of CPU, RAM, GPU for the UI's resource display."""
    return hardware_service.detect_runtime_capabilities()


@router.get("/whisper/models", response_model=WhisperModelsResponse)
async def list_whisper_models(
    db: SessionDep,
    _admin: AdminUserDep,
) -> WhisperModelsResponse:
    """All supported models with download status and disk size."""
    default_model = await app_settings_service.get(
        db, app_settings_service.KEY_DEFAULT_MODEL, "base"
    )
    statuses = whisper_service.list_model_statuses()
    for entry in statuses:
        entry["is_default"] = entry["name"] == default_model
    
    return WhisperModelsResponse(models=statuses, default_model=default_model)


@router.post("/whisper/models/{model_name}/download", response_model=ModelActionResponse)
async def download_whisper_model(
    model_name: str,
    _admin: AdminUserDep,
) -> ModelActionResponse:
    """Start background download. Polls /whisper/models for progress."""
    normalized = await whisper_service.download_model_in_background(model_name)
    status = whisper_service.get_model_status(normalized)
    return ModelActionResponse(
        whisper_model=normalized,
        success=True,
        status=status,
    )


@router.delete("/whisper/models/{model_name}", response_model=ModelActionResponse)
async def delete_whisper_model(
    model_name: str,
    _admin: AdminUserDep,
) -> ModelActionResponse:
    """Remove a downloaded model from disk. Fails if in use."""
    result = await whisper_service.delete_model(model_name)
    return ModelActionResponse(
        whisper_model=model_name,
        success=result.get("success", False),
        deleted=result.get("deleted"),
        error=result.get("error"),
    )


@router.get("/current", response_model=RuntimeSettingsResponse)
async def get_runtime_settings(
    db: SessionDep,
    _admin: AdminUserDep,
) -> RuntimeSettingsResponse:
    """Current default model, profile, and the computed runtime plan."""
    from app.core.runtime import PROFILE_MAX, PROFILE_MODERATE, calculate_performance_plan
    
    model = await app_settings_service.get(
        db, app_settings_service.KEY_DEFAULT_MODEL, "base"
    )
    profile = await app_settings_service.get(
        db, app_settings_service.KEY_PROFILE, PROFILE_MODERATE
    )
    # Compute both plans so the UI can show "Optimal" vs "Maximum" side by side.
    moderate_plan = calculate_performance_plan(PROFILE_MODERATE, model)
    max_plan = calculate_performance_plan(PROFILE_MAX, model)
    
    return RuntimeSettingsResponse(
        default_model=model,
        profile=profile,
        runtime_plan=runtime.plan,
        moderate_plan=moderate_plan,
        max_plan=max_plan,
    )


@router.put("/default-model", response_model=RuntimeSettingsResponse)
async def set_default_whisper_model(
    payload: DefaultModelRequest,
    db: SessionDep,
    _admin: AdminUserDep,
) -> RuntimeSettingsResponse:
    """Change default model and recompute the runtime plan."""
    from app.core.runtime import PROFILE_MAX, PROFILE_MODERATE, calculate_performance_plan
    
    await app_settings_service.set_value(
        db, app_settings_service.KEY_DEFAULT_MODEL, payload.whisper_model
    )
    await db.commit()
    await runtime.apply_model(payload.whisper_model)
    
    profile = await app_settings_service.get(
        db, app_settings_service.KEY_PROFILE, PROFILE_MODERATE
    )
    moderate_plan = calculate_performance_plan(PROFILE_MODERATE, payload.whisper_model)
    max_plan = calculate_performance_plan(PROFILE_MAX, payload.whisper_model)
    
    return RuntimeSettingsResponse(
        default_model=payload.whisper_model,
        profile=profile,
        runtime_plan=runtime.plan,
        moderate_plan=moderate_plan,
        max_plan=max_plan,
    )


@router.put("/performance-profile", response_model=RuntimeSettingsResponse)
async def set_performance_profile(
    payload: PerformanceProfileRequest,
    db: SessionDep,
    _admin: AdminUserDep,
) -> RuntimeSettingsResponse:
    """Change performance profile and recompute concurrency."""
    from app.core.runtime import PROFILE_MAX, PROFILE_MODERATE, calculate_performance_plan
    
    await app_settings_service.set_value(
        db, app_settings_service.KEY_PROFILE, payload.profile
    )
    await db.commit()
    
    model = await app_settings_service.get(
        db, app_settings_service.KEY_DEFAULT_MODEL, "base"
    )
    await runtime.apply_profile(payload.profile, whisper_model=model)
    
    moderate_plan = calculate_performance_plan(PROFILE_MODERATE, model)
    max_plan = calculate_performance_plan(PROFILE_MAX, model)
    
    return RuntimeSettingsResponse(
        default_model=model,
        profile=payload.profile,
        runtime_plan=runtime.plan,
        moderate_plan=moderate_plan,
        max_plan=max_plan,
    )

