"""Authentication, Microsoft Entra ID JWT verification, and RBAC dependencies."""

from __future__ import annotations

import logging
from typing import Any, Optional

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from reconciliator.config import AppConfig, EntraAuthConfig
from reconciliator.models import UserProfile

logger = logging.getLogger(__name__)

security_scheme = HTTPBearer(auto_error=False)

_jwks_clients: dict[str, jwt.PyJWKClient] = {}


def _get_jwks_client(tenant_id: str) -> jwt.PyJWKClient:
    """Cache and return a PyJWKClient for Microsoft Entra ID keys."""
    if tenant_id not in _jwks_clients:
        jwks_url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        _jwks_clients[tenant_id] = jwt.PyJWKClient(jwks_url, cache_keys=True, max_cached_keys=16)
    return _jwks_clients[tenant_id]


def verify_entra_token(token: str, auth_cfg: EntraAuthConfig) -> dict[str, Any]:
    """Verify Microsoft Entra ID JWT token against JWKS and validate claims."""
    if not auth_cfg.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Microsoft Entra ID tenant_id is not configured",
        )

    jwks_client = _get_jwks_client(auth_cfg.tenant_id)
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
    except Exception as e:
        logger.warning("Failed to retrieve signing key for token: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token signature key: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    expected_issuer = f"https://login.microsoftonline.com/{auth_cfg.tenant_id}/v2.0"
    expected_aud = auth_cfg.audience or auth_cfg.client_id or None

    decode_kwargs: dict[str, Any] = {
        "key": signing_key.key,
        "algorithms": ["RS256"],
        "issuer": expected_issuer,
        "options": {"verify_exp": True, "verify_iss": True},
    }
    if expected_aud:
        decode_kwargs["audience"] = expected_aud
    else:
        decode_kwargs["options"]["verify_aud"] = False

    try:
        payload = jwt.decode(token, **decode_kwargs)
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        logger.warning("Token validation failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token validation failed: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    x_dev_role: Optional[str] = Header(None, alias="X-Dev-Role"),
) -> UserProfile:
    """Dependency that resolves the current user with roles (Entra ID or Local Dev Mode)."""
    cfg: AppConfig = getattr(request.app.state, "config", None)
    auth_cfg = cfg.auth if cfg else EntraAuthConfig()

    # 1. Local Development Mode (auth disabled)
    if not auth_cfg.enabled:
        role = (x_dev_role or auth_cfg.dev_default_role or "Admin").strip()
        role_lower = role.lower()
        is_admin = role_lower == "admin"
        is_curator = role_lower in ("admin", "curator")
        is_reader = True

        return UserProfile(
            sub="dev-local-user",
            email=auth_cfg.dev_user_email,
            name=auth_cfg.dev_user_name,
            roles=[role],
            is_admin=is_admin,
            is_curator=is_curator,
            is_reader=is_reader,
        )

    # 2. Production Mode with Entra ID
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claims = verify_entra_token(credentials.credentials, auth_cfg)
    sub = str(claims.get("sub") or claims.get("oid") or "unknown-user")
    email = str(claims.get("preferred_username") or claims.get("email") or claims.get("upn") or sub)
    name = str(claims.get("name") or email)

    token_roles: list[str] = claims.get(auth_cfg.role_claim, [])
    if isinstance(token_roles, str):
        token_roles = [token_roles]

    is_admin = any(r in auth_cfg.admin_roles for r in token_roles)
    is_curator = is_admin or any(r in auth_cfg.curator_roles for r in token_roles)
    is_reader = True  # Any valid authenticated Entra user has at least Reader access

    return UserProfile(
        sub=sub,
        email=email,
        name=name,
        roles=token_roles,
        is_admin=is_admin,
        is_curator=is_curator,
        is_reader=is_reader,
    )


def require_admin(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """Guard requiring Admin role."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrative privileges required for this action",
        )
    return user


def require_curator(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """Guard requiring Curator or Admin role."""
    if not user.is_curator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Curator privileges required for this action",
        )
    return user


def require_reader(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """Guard requiring at least Reader role."""
    if not user.is_reader:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Reader privileges required for this action",
        )
    return user

