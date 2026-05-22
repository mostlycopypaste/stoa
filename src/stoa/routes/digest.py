"""Admin endpoint for weekly digest generation (async)."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.database import get_db
from stoa.routes.admin import require_admin
from stoa.services.digest_generator import generate_digest

router = APIRouter(prefix="/api/admin/digest", tags=["admin", "digest"])


@router.get("/preview")
async def preview_digest(
    _admin: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Generate weekly digest preview."""
    return await generate_digest(db)
