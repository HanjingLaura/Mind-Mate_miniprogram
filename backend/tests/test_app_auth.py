import unittest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from database import Base
from routers.user import login
from schemas import LoginRequest
from services.auth_service import get_trusted_openid, issue_app_token, parse_app_token


class AppAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_token_roundtrip(self):
        token = issue_app_token("app_user_one")
        self.assertEqual(parse_app_token(token), "app_user_one")
        self.assertIsNone(parse_app_token(token + "x"))

    def test_bearer_header_is_trusted(self):
        token = issue_app_token("app_user_one")
        self.assertEqual(get_trusted_openid(None, f"Bearer {token}"), "app_user_one")

    async def test_device_login_issues_token(self):
        request = Request({"type": "http", "headers": []})
        result = await login(LoginRequest(device_id="phone-abc"), request, self.db)
        self.assertTrue(result["openid"].startswith("app_"))
        self.assertTrue(result["token"])
        self.assertEqual(parse_app_token(result["token"]), result["openid"])
        self.assertTrue(result["is_new"])


if __name__ == "__main__":
    unittest.main()
