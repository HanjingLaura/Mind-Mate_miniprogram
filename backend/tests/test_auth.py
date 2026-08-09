import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from database import Base
from routers.admin import bypass_upgrade
from routers.user import login
from schemas import AdminBypassRequest, LoginRequest


class _CodeExchangeFailureResponse:
    def json(self):
        return {"errcode": 40029, "errmsg": "invalid code"}


class _CodeExchangeFailureClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params):
        return _CodeExchangeFailureResponse()


class ProductionAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _request(headers=None):
        encoded_headers = [
            (name.lower().encode(), value.encode())
            for name, value in (headers or {}).items()
        ]
        return Request({"type": "http", "headers": encoded_headers})

    async def test_invalid_wechat_code_never_becomes_a_dev_user(self):
        with patch("routers.user.settings.WECHAT_APPID", "wx-app"), patch(
            "routers.user.settings.WECHAT_SECRET", "app-secret"
        ), patch("httpx.AsyncClient", return_value=_CodeExchangeFailureClient()):
            with self.assertRaises(HTTPException) as raised:
                await login(LoginRequest(code="invalid-code"), self._request(), self.db)

        self.assertEqual(raised.exception.status_code, 502)

    async def test_cloud_hosting_openid_skips_code_exchange(self):
        result = await login(
            LoginRequest(code="unused-code"),
            self._request({"x-wx-openid": "wx_cloud_hosting_user"}),
            self.db,
        )

        self.assertEqual(result["openid"], "wx_cloud_hosting_user")
        self.assertTrue(result["is_new"])

    async def test_admin_bypass_is_hidden_when_disabled(self):
        with patch("routers.admin.settings.ADMIN_BYPASS_ENABLED", False):
            with self.assertRaises(HTTPException) as raised:
                await bypass_upgrade(
                    AdminBypassRequest(user_id=1, secret_key="known-example"),
                    self.db,
                )

        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
