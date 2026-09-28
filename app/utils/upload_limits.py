from __future__ import annotations

from fastapi import HTTPException, UploadFile


async def read_upload_limited(upload: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(min(1024 * 1024, max_bytes - total + 1)):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="Uploaded file exceeds the configured size limit")
        chunks.append(chunk)
    if not total:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    return b"".join(chunks)
