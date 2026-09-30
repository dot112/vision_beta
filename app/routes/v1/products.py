"""Known product codes, checked by every QR reader."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.dependencies import get_db, require_operator, require_supervisor
from app.services.product_service import ProductService
from app.utils.upload_limits import read_upload_limited

router = APIRouter(prefix="/products", tags=["Products"])

MAX_CSV_BYTES = 8 * 1024 * 1024


def _audit(user: User, action: str, details: str) -> None:
    from app.services.settings_persistence_service import SettingsPersistenceService
    SettingsPersistenceService.record_audit(
        username=user.username,
        role=user.role,
        clearance_level=user.clearance_level,
        action=action,
        category="PRODUCTS",
        details=details,
    )
    SettingsPersistenceService.save()


@router.get("", summary="List product codes")
async def list_products(
    search: Optional[str] = Query(default=None, max_length=128, description="Match part of a code or name"),
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator),
) -> Dict[str, Any]:
    return await ProductService.list_products(db, search=search, limit=limit, offset=offset)


@router.get("/export", summary="Download all product codes as CSV")
async def export_products(db: AsyncSession = Depends(get_db), user: User = Depends(require_operator)) -> Response:
    text = await ProductService.export_csv(db)
    return Response(
        content=text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="products.csv"', "Cache-Control": "no-store"},
    )


@router.post("/import", summary="Add or update product codes from a CSV file (Level 2+ Supervisor)")
async def import_products(
    file: UploadFile = File(..., description="CSV with a header row: code,name[,description]"),
    replace: bool = Query(default=False, description="Also remove codes that are not in the file"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> Dict[str, int]:
    raw = await read_upload_limited(file, MAX_CSV_BYTES)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="The CSV file must be UTF-8 text") from None
    try:
        result = await ProductService.import_csv(db, text, replace=replace)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _audit(user, "IMPORT_PRODUCTS", f"Imported product codes: {result['added']} added, {result['updated']} updated, {result['removed']} removed")
    return result


@router.get("/{product_id}", summary="One product code")
async def get_product(product_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_operator)) -> Dict[str, Any]:
    product = await ProductService.get(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.post("", status_code=201, summary="Add a product code (Level 2+ Supervisor)")
async def create_product(body: Dict[str, Any], db: AsyncSession = Depends(get_db), user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    try:
        product = await ProductService.create(db, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _audit(user, "CREATE_PRODUCT", f"Added product code '{product['code']}' ({product['name']})")
    return product


@router.put("/{product_id}", summary="Change a product code (Level 2+ Supervisor)")
async def update_product(product_id: str, body: Dict[str, Any], db: AsyncSession = Depends(get_db), user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    try:
        product = await ProductService.update(db, product_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    _audit(user, "UPDATE_PRODUCT", f"Changed product code '{product['code']}' ({product['name']})")
    return product


@router.delete("/{product_id}", summary="Delete a product code (Level 2+ Supervisor)")
async def delete_product(product_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    product = await ProductService.get(db, product_id)
    if product is None or not await ProductService.delete(db, product_id):
        raise HTTPException(status_code=404, detail="Product not found")
    _audit(user, "DELETE_PRODUCT", f"Deleted product code '{product['code']}'")
    return {"status": "deleted", "id": product_id}
