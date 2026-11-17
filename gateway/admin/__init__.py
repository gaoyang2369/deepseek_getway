"""Administrative routes; shared business operations live outside this package."""

from fastapi import APIRouter, Depends

from . import auth, budgets, consumption, keys, people, prices, reports, roster, teams
from .auth import auth_router as auth_router
from .auth import require_admin

router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)])
for routes in (auth, teams, roster, keys, budgets, prices, reports, consumption, people):
    router.include_router(routes.router)
