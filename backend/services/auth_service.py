"""Trusted identity helpers for Mini Program, App, and local development.

Priority:
1. CloudBase ``X-WX-OPENID``
2. App session ``Authorization: Bearer``
3. Explicit local opt-in via ALLOW_INSECURE_DEV_OPENID
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from config import settings

_DEV_SIGNING_KEY = secrets.token_bytes(32)


def _signing_key() -> bytes:
    if settings.APP_SECRET:
        return settings.APP_SECRET.encode("utf-8")
    if settings.APP_ENV == "production":
        raise RuntimeError("APP_SECRET must be configured before issuing app sessions")
    return _DEV_SIGNING_KEY


def issue_app_token(openid: str) -> str:
    """Issue a time-limited HMAC session token bound to an openid."""
    now = int(time.time())
    payload = base64.urlsafe_b64encode(json.dumps({
        "v": 1,
        "openid": openid,
        "iat": now,
        "exp": now + settings.APP_TOKEN_TTL_SECONDS,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(_signing_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{signature}"


def parse_app_token(token: str) -> str | None:
    raw = (token or "").strip()
    if "." not in raw:
        return None
    payload, signature = raw.rsplit(".", 1)
    expected = hmac.new(_signing_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    if not secrets.compare_digest(signature, expected):
        return None
    padding = "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + padding).decode("utf-8"))
        if not isinstance(data, dict) or int(data.get("exp", 0)) <= int(time.time()):
            return None
        openid = str(data.get("openid", "")).strip()
        return openid or None
    except Exception:
        return None


def get_trusted_openid(
    x_wx_openid: Annotated[str | None, Header(alias="X-WX-OPENID")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> str | None:
    """Resolve a trusted identity or reject a headerless production call."""
    trusted_openid = (x_wx_openid or "").strip()
    if trusted_openid:
        return trusted_openid

    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        openid = parse_app_token(value.strip())
        if not openid:
            raise HTTPException(status_code=401, detail="登录已失效，请重新进入")
        return openid

    if settings.ALLOW_INSECURE_DEV_OPENID:
        return None
    raise HTTPException(status_code=401, detail="缺少可信身份")


TrustedOpenID = Annotated[str | None, Depends(get_trusted_openid)]


def require_openid_match(claimed_openid: str, trusted_openid: str | None) -> str:
    """Return the effective openid after enforcing the trusted identity."""
    claimed = (claimed_openid or "").strip()
    if trusted_openid is None:
        if settings.ALLOW_INSECURE_DEV_OPENID and claimed:
            return claimed
        raise HTTPException(status_code=401, detail="缺少可信身份")
    if claimed and not secrets.compare_digest(claimed, trusted_openid):
        raise HTTPException(status_code=403, detail="无权访问其他用户的数据")
    return trusted_openid
