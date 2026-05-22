"""Admin endpoints for footer management."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from stoa.deps import get_db
from stoa.models import FooterMessage
from stoa.routes.admin import require_admin
from stoa.schemas import FooterCreate, FooterResponse, FootersResponse, FooterUpdate
from stoa.services.footer_rotation import select_footer, select_footers_bulk

router = APIRouter(prefix="/api/admin", tags=["admin", "footers"])
logger = logging.getLogger(__name__)


@router.get("/footer")
def get_single_footer(
    category: str | None = Query(None, pattern="^(token_economics|social_proof|fomo|cheeky)$"),
    context: str | None = Query(None, pattern="^(announcement|discussion)$"),
    exclude: str | None = Query(None, description="Comma-separated list of IDs to exclude"),
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
) -> FooterResponse:
    """Get a single footer using LRU rotation algorithm.

    Query params:
    - category: Filter by category (token_economics, social_proof, fomo, cheeky)
    - context: Filter by context (announcement, discussion)
    - exclude: Comma-separated list of footer IDs to exclude (e.g., "1,2,3")
    """
    exclude_ids = [int(x) for x in exclude.split(",")] if exclude else None

    try:
        footer = select_footer(db, category=category, context=context, exclude_ids=exclude_ids)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return FooterResponse(footer=footer.text, category=footer.category, id=footer.id)  # type: ignore[arg-type]


@router.get("/footers")
def get_bulk_footers(
    count: int = Query(..., ge=1, le=100, description="Number of footers to return"),
    category: str | None = Query(None, pattern="^(token_economics|social_proof|fomo|cheeky)$"),
    context: str | None = Query(None, pattern="^(announcement|discussion)$"),
    exclude: str | None = Query(None, description="Comma-separated list of IDs to exclude"),
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
) -> FootersResponse:
    """Get multiple footers using LRU rotation algorithm.

    Query params:
    - count: Number of footers to return (1-100)
    - category: Filter by category
    - context: Filter by context
    - exclude: Comma-separated list of footer IDs to exclude
    """
    exclude_ids = [int(x) for x in exclude.split(",")] if exclude else None

    try:
        footers = select_footers_bulk(
            db, count=count, category=category, context=context, exclude_ids=exclude_ids
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    footer_list = [FooterResponse(footer=f.text, category=f.category, id=f.id) for f in footers]  # type: ignore[arg-type]
    return FootersResponse(footers=footer_list, count=len(footer_list))


@router.post("/footers", status_code=status.HTTP_201_CREATED)
def create_footer(
    payload: FooterCreate,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Create a new footer message."""
    footer = FooterMessage(
        text=payload.text,
        category=payload.category,
        context=payload.context,
        active=True,
    )
    db.add(footer)
    db.commit()
    db.refresh(footer)

    logger.info("Footer created: %s", footer.id)
    return {"id": footer.id, "text": footer.text, "category": footer.category}


@router.put("/footers/{footer_id}")
def update_footer(
    footer_id: int,
    payload: FooterUpdate,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Update an existing footer."""
    footer = db.query(FooterMessage).filter_by(id=footer_id).first()
    if not footer:
        raise HTTPException(status_code=404, detail="Footer not found")

    if payload.text is not None:
        footer.text = payload.text  # type: ignore[assignment]
    if payload.category is not None:
        footer.category = payload.category  # type: ignore[assignment]
    if payload.context is not None:
        footer.context = payload.context  # type: ignore[assignment]
    if payload.active is not None:
        footer.active = payload.active  # type: ignore[assignment]

    db.commit()
    db.refresh(footer)

    logger.info("Footer updated: %s", footer_id)
    return {"id": footer.id, "text": footer.text, "active": footer.active}


@router.delete("/footers/{footer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_footer(
    footer_id: int,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
) -> None:
    """Soft-delete a footer (set active=false)."""
    footer = db.query(FooterMessage).filter_by(id=footer_id).first()
    if not footer:
        raise HTTPException(status_code=404, detail="Footer not found")

    footer.active = False  # type: ignore[assignment]
    db.commit()

    logger.info("Footer soft-deleted: %s", footer_id)
