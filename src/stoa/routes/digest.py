"""Admin endpoint for weekly digest generation."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from stoa.deps import get_db
from stoa.routes.admin import require_admin
from stoa.services.digest_generator import generate_digest

router = APIRouter(prefix="/api/admin/digest", tags=["admin", "digest"])


@router.get("/preview")
def preview_digest(
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Generate weekly digest preview.

    Returns auto-generated digest content ready for OC to send via email.
    Herd-inbox does not send emails directly.
    """
    return generate_digest(db)
