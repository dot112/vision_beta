"""Product lists: the codes each camera that reads codes checks against (up to four lists)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.dependencies import get_db, require_operator, require_supervisor
from app.services.line_config import readers_using_list
from app.services.product_service import (
    MAX_PRODUCT_LISTS,
    ProductListInUse,
    ProductListLimit,
    ProductService,
    list_name_from_file,
)
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


async def _readers(db: AsyncSession, list_id: str) -> List[Dict[str, Any]]:
    """The cameras that check their codes against a list, each with its line and camera name."""
    from app.services.camera_service import CameraService
    from app.services.settings_persistence_service import SettingsPersistenceService

    users = readers_using_list(SettingsPersistenceService.get_lines(), list_id)
    if not users:
        return []
    names = {camera.id: camera.name for camera in await CameraService.list_cameras(db)}
    return [
        {
            "line_id": line["id"],
            "line_name": line["name"],
            "camera_id": camera["camera_id"],
            "camera_name": names.get(camera["camera_id"], camera["camera_id"]),
        }
        for line, camera in users
    ]


async def _list_or_404(db: AsyncSession, list_id: Optional[str], create: bool = False):
    try:
        return await ProductService.resolve_list(db, list_id, create=create)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _csv_text(file: UploadFile) -> str:
    raw = await read_upload_limited(file, MAX_CSV_BYTES)
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="The CSV file must be UTF-8 text") from None


# ── Lists ─────────────────────────────────────────────────────────────────────

@router.get("/lists", summary="The product lists, with the number of codes in each and the cameras that use it")
async def list_product_lists(db: AsyncSession = Depends(get_db), user: User = Depends(require_operator)) -> Dict[str, Any]:
    lists = await ProductService.list_lists(db)
    for product_list in lists:
        product_list["used_by"] = await _readers(db, product_list["id"])
    return {"lists": lists, "max_lists": MAX_PRODUCT_LISTS}


@router.post("/lists", status_code=201, summary="Create an empty product list (Level 2+ Supervisor)")
async def create_product_list(body: Dict[str, Any], db: AsyncSession = Depends(get_db), user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    try:
        product_list = await ProductService.create_list(db, body.get("name"))
    except ProductListLimit as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _audit(user, "CREATE_PRODUCT_LIST", f"Created product list '{product_list['name']}'")
    return {**product_list, "used_by": []}


@router.put("/lists/{list_id}", summary="Rename a product list (Level 2+ Supervisor)")
async def rename_product_list(list_id: str, body: Dict[str, Any], db: AsyncSession = Depends(get_db), user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    try:
        product_list = await ProductService.rename_list(db, list_id, body.get("name"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if product_list is None:
        raise HTTPException(status_code=404, detail="Product list not found")
    _audit(user, "RENAME_PRODUCT_LIST", f"Renamed a product list to '{product_list['name']}'")
    return {**product_list, "used_by": await _readers(db, list_id)}


@router.delete("/lists/{list_id}", summary="Delete a product list and its codes (Level 2+ Supervisor)")
async def delete_product_list(list_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    product_list = await ProductService.get_list(db, list_id)
    if product_list is None:
        raise HTTPException(status_code=404, detail="Product list not found")
    name = product_list.name
    used_by = [f"{reader['line_name']} ({reader['camera_name']})" for reader in await _readers(db, list_id)]
    try:
        await ProductService.delete_list(db, list_id, used_by=used_by)
    except ProductListInUse as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(user, "DELETE_PRODUCT_LIST", f"Deleted product list '{name}'")
    return {"status": "deleted", "id": list_id}


@router.post("/lists/{list_id}/replace", summary="Replace a list's contents from a CSV file; its id and name stay (Level 2+ Supervisor)")
async def replace_product_list(
    list_id: str,
    file: UploadFile = File(..., description="CSV with a header row: code,name[,description]"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    text = await _csv_text(file)
    try:
        result = await ProductService.replace_contents(db, list_id, text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Product list not found")
    _audit(user, "REPLACE_PRODUCT_LIST",
           f"Replaced the contents of '{result['list']['name']}': {result['added']} added, {result['updated']} updated, {result['removed']} removed")
    return result


# ── Products ──────────────────────────────────────────────────────────────────

@router.get("", summary="List the product codes of one list")
async def list_products(
    list_id: Optional[str] = Query(default=None, max_length=36, description="The product list; the oldest list when left out"),
    search: Optional[str] = Query(default=None, max_length=128, description="Match part of a code or name"),
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator),
) -> Dict[str, Any]:
    if not list_id and not await ProductService.list_lists(db):
        return {"list_id": None, "total": 0, "products": []}
    product_list = await _list_or_404(db, list_id)
    return await ProductService.list_products(db, product_list.id, search=search, limit=limit, offset=offset)


@router.get("/export", summary="Download one list's product codes as CSV")
async def export_products(
    list_id: Optional[str] = Query(default=None, max_length=36, description="The product list; the oldest list when left out"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator),
) -> Response:
    product_list = await _list_or_404(db, list_id, create=True)
    text = await ProductService.export_csv(db, product_list.id)
    filename = re.sub(r"[^A-Za-z0-9 ._()-]+", "_", product_list.name).strip(" .") or "products"
    return Response(
        content=text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"', "Cache-Control": "no-store"},
    )


@router.post("/import", summary="Make a new product list from a CSV file (Level 2+ Supervisor)")
async def import_products(
    file: UploadFile = File(..., description="CSV with a header row: code,name[,description]"),
    name: Optional[str] = Query(default=None, max_length=128, description="Name of the new list; the file name when left out"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    """Each import makes a new list, up to four. To update a list from a file, use
    POST /products/lists/{list_id}/replace: it keeps the list's id, so the cameras set to it keep working."""
    text = await _csv_text(file)
    try:
        result = await ProductService.import_csv(db, text, name=name or list_name_from_file(file.filename))
    except ProductListLimit as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _audit(user, "IMPORT_PRODUCTS", f"Imported product list '{result['list']['name']}' with {result['added']} code(s)")
    return result


@router.get("/{product_id}", summary="One product code")
async def get_product(product_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_operator)) -> Dict[str, Any]:
    product = await ProductService.get(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.post("", status_code=201, summary="Add a product code to a list (Level 2+ Supervisor)")
async def create_product(body: Dict[str, Any], db: AsyncSession = Depends(get_db), user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    try:
        product = await ProductService.create(db, body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
