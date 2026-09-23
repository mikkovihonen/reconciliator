"""Authentication configuration and current user endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from reconciliator.api.auth import get_current_user
from reconciliator.config import AppConfig, EntraAuthConfig
from reconciliator.models import UserProfile

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.get("/config")
def get_auth_config(request: Request) -> dict[str, Any]:
    """Retrieve public authentication parameters for frontend MSAL or Dev Mode."""
    cfg: AppConfig = getattr(request.app.state, "config", None)
    auth_cfg = cfg.auth if cfg else EntraAuthConfig()

    return {
        "enabled": auth_cfg.enabled,
        "dev_mode": not auth_cfg.enabled,
        "tenant_id": auth_cfg.tenant_id if auth_cfg.enabled else None,
        "client_id": auth_cfg.client_id if auth_cfg.enabled else None,
        "audience": auth_cfg.audience if auth_cfg.enabled else None,
        "dev_default_role": auth_cfg.dev_default_role if not auth_cfg.enabled else None,
        "dev_user_name": auth_cfg.dev_user_name if not auth_cfg.enabled else None,
        "dev_user_email": auth_cfg.dev_user_email if not auth_cfg.enabled else None,
    }


@router.get("/me", response_model=UserProfile)
def get_me(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """Retrieve the currently authenticated user profile and roles."""
    return user
