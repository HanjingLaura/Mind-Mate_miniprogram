"""本地图片上传 — 替代小程序云存储，供跨端 App 发送图片。"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from services.auth_service import get_trusted_openid

router = APIRouter(prefix="/api/upload", tags=["upload"])

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_MAX_BYTES = 10 * 1024 * 1024


@router.post("/image")
async def upload_image(
    file: UploadFile = File(...),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    if trusted_openid is None:
        raise HTTPException(status_code=401, detail="请先登录")

    content_type = (file.content_type or "").lower()
    suffix = _ALLOWED_TYPES.get(content_type)
    if not suffix:
        raise HTTPException(status_code=400, detail="仅支持 jpg / png / webp / gif")

    data = await file.read()
    if len(data) > _MAX_BYTES:
        raise HTTPException(status_code=400, detail="图片不能超过 10MB")

    filename = f"{uuid.uuid4().hex}{suffix}"
    dest = UPLOAD_DIR / filename
    dest.write_bytes(data)

    relative = f"/uploads/{filename}"
    return {
        "url": relative,
        "image_url": relative,
        "image_cloud_id": "",
        "filename": filename,
    }


@router.get("/file/{filename}")
async def get_uploaded_file(filename: str):
    dest = UPLOAD_DIR / Path(filename).name
    if not dest.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(dest)
