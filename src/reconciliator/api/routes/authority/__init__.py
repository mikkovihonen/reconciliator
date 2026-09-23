"""Authority protocol and resolution API routes."""

from reconciliator.api.routes.authority.oai_provider import router as oai_router
from reconciliator.api.routes.authority.reconcile import router as reconcile_router
from reconciliator.api.routes.authority.resolver import router as resolver_router

__all__ = ["reconcile_router", "resolver_router", "oai_router"]
