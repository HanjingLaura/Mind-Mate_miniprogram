import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import SubscribeAuth, User
from routers.user import SubscribeAuthRequest, subscribe_auth, subscribe_status
from services.auth_service import get_trusted_openid, require_openid_match


class TrustedIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(openid="trusted-user")
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_production_rejects_missing_or_mismatched_identity(self):
        with patch("services.auth_service.settings.ALLOW_INSECURE_DEV_OPENID", False):
            with self.assertRaises(HTTPException) as missing:
                get_trusted_openid(None)
            self.assertEqual(missing.exception.status_code, 401)

            with self.assertRaises(HTTPException) as mismatch:
                require_openid_match("another-user", "trusted-user")
            self.assertEqual(mismatch.exception.status_code, 403)

    def test_local_openid_requires_explicit_opt_in(self):
        with patch("services.auth_service.settings.ALLOW_INSECURE_DEV_OPENID", True):
            self.assertIsNone(get_trusted_openid(None))
            self.assertEqual(require_openid_match("local-user", None), "local-user")

    async def test_subscription_uses_only_server_configured_template(self):
        old = SubscribeAuth(
            user_id=self.user.id,
            template_id="old-template",
            scene="general",
        )
        self.db.add(old)
        self.db.commit()

        with patch("routers.user.settings.WECHAT_SUBSCRIBE_TEMPLATE_ID", "server-template"):
            result = await subscribe_auth(
                SubscribeAuthRequest(
                    openid=self.user.openid,
                    template_id="attacker-template",
                    scene="general",
                ),
                self.db,
                self.user.openid,
            )
            status = await subscribe_status(
                self.user.openid,
                self.db,
                self.user.openid,
            )

        templates = [row.template_id for row in self.db.query(SubscribeAuth).all()]
        self.assertIn("server-template", templates)
        self.assertNotIn("attacker-template", templates)
        self.assertEqual(result["unused_count"], 1)
        self.assertEqual(status["unused_count"], 1)


if __name__ == "__main__":
    unittest.main()
