"""Admin endpoints for footer management (async)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.database import get_db
from stoa.models import FooterMessage
from stoa.routes.admin import require_admin
from stoa.schemas import FooterCreate, FooterResponse, FootersResponse, FooterUpdate
from stoa.services.footer_rotation import select_footer, select_footers_bulk

router = APIRouter(prefix="/api/admin", tags=["admin", "footers"])
logger = logging.getLogger(__name__)


@router.get("/footer")
async def get_single_footer(
    category: str | None = Query(None, pattern="^(token_economics|social_proof|fomo|cheeky)$"),
    context: str | None = Query(None, pattern="^(announcement|discussion)$"),
    exclude: str | None = Query(None, description="Comma-separated list of IDs to exclude"),
    _admin: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> FooterResponse:
    """Get a single footer using LRU rotation algorithm."""
    exclude_ids = [int(x) for x in exclude.split(",")] if exclude else None

    try:
        footer = await select_footer(
            db, category=category, context=context, exclude_ids=exclude_ids
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return FooterResponse(footer=footer.text, category=footer.category, id=footer.id)


@router.get("/footers")
async def get_bulk_footers(
    count: int = Query(..., ge=1, le=100, description="Number of footers to return"),
    category: str | None = Query(None, pattern="^(token_economics|social_proof|fomo|cheeky)$"),
    context: str | None = Query(None, pattern="^(announcement|discussion)$"),
    exclude: str | None = Query(None, description="Comma-separated list of IDs to exclude"),
    _admin: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> FootersResponse:
    """Get multiple footers using LRU rotation algorithm."""
    exclude_ids = [int(x) for x in exclude.split(",")] if exclude else None

    try:
        footers = await select_footers_bulk(
            db, count=count, category=category, context=context, exclude_ids=exclude_ids
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    footer_list = [FooterResponse(footer=f.text, category=f.category, id=f.id) for f in footers]
    return FootersResponse(footers=footer_list, count=len(footer_list))


@router.post("/footers", status_code=status.HTTP_201_CREATED)
async def create_footer(
    payload: FooterCreate,
    _admin: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Create a new footer message."""
    footer = FooterMessage(
        text=payload.text,
        category=payload.category,
        context=payload.context,
        active=True,
    )
    db.add(footer)
    await db.flush()
    await db.refresh(footer)

    logger.info("Footer created: %s", footer.id)
    return {"id": footer.id, "text": footer.text, "category": footer.category}


@router.put("/footers/{footer_id}")
async def update_footer(
    footer_id: int,
    payload: FooterUpdate,
    _admin: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Update an existing footer."""
    from sqlalchemy import select

    result = await db.execute(select(FooterMessage).where(FooterMessage.id == footer_id))
    footer = result.scalar_one_or_none()
    if not footer:
        raise HTTPException(status_code=404, detail="Footer not found")

    if payload.text is not None:
        footer.text = payload.text
    if payload.category is not None:
        footer.category = payload.category
    if payload.context is not None:
        footer.context = payload.context
    if payload.active is not None:
        footer.active = payload.active

    await db.flush()
    await db.refresh(footer)

    logger.info("Footer updated: %s", footer_id)
    return {"id": footer.id, "text": footer.text, "active": footer.active}


@router.delete("/footers/{footer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_footer(
    footer_id: int,
    _admin: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete a footer (set active=false)."""
    from sqlalchemy import select

    result = await db.execute(select(FooterMessage).where(FooterMessage.id == footer_id))
    footer = result.scalar_one_or_none()
    if not footer:
        raise HTTPException(status_code=404, detail="Footer not found")

    footer.active = False
    await db.flush()

    logger.info("Footer soft-deleted: %s", footer_id)
