import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from routers.pay import _validate_paid_order
from services.pay_service import WeChatPayV3


class _FakeResponse:
    headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return {"prepay_id": "wx-test-prepay-id"}


class _FakeAsyncClient:
    def __init__(self, response=None):
        self.request = None
        self.response = response or _FakeResponse()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *, content, headers, timeout):
        self.request = {
            "url": url,
            "content": content,
            "headers": headers,
            "timeout": timeout,
        }
        return self.response

    async def get(self, url, *, headers, timeout):
        self.request = {
            "url": url,
            "headers": headers,
            "timeout": timeout,
        }
        return self.response


class WeChatPayRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_order_signs_the_exact_json_bytes_that_are_sent(self):
        pay = WeChatPayV3()
        pay.appid = "wx-app"
        pay.mchid = "1900000001"
        pay.cert_serial_no = "CERTSERIAL"
        pay.notify_url = "https://example.com/api/pay/callback"
        fake_client = _FakeAsyncClient()
        signed_messages = []

        def fake_sign(message):
            signed_messages.append(message)
            return "signed"

        with patch.object(pay, "_sign", side_effect=fake_sign), patch(
            "services.pay_service.httpx.AsyncClient", return_value=fake_client
        ):
            result = await pay.create_order("openid-1", "MMORDER1")

        sent_body = fake_client.request["content"].decode("utf-8")
        parsed_body = json.loads(sent_body)
        self.assertEqual(parsed_body["out_trade_no"], "MMORDER1")
        self.assertNotIn(": ", sent_body)
        self.assertIn(f"\n{sent_body}\n", signed_messages[0])
        self.assertNotIn("Wechatpay-Serial", fake_client.request["headers"])
        self.assertEqual(result["prepay_id"], "wx-test-prepay-id")

    async def test_query_order_signs_the_canonical_path_with_mchid(self):
        pay = WeChatPayV3()
        pay.mchid = "1900000001"
        fake_client = _FakeAsyncClient()
        signed_messages = []

        def fake_sign(message):
            signed_messages.append(message)
            return "signed"

        with patch.object(pay, "_sign", side_effect=fake_sign), patch(
            "services.pay_service.httpx.AsyncClient", return_value=fake_client
        ):
            await pay.query_order("MMORDER1")

        expected_path = "/v3/pay/transactions/out-trade-no/MMORDER1?mchid=1900000001"
        self.assertTrue(fake_client.request["url"].endswith(expected_path))
        self.assertEqual(
            signed_messages[0],
            f"GET\n{expected_path}\n" + signed_messages[0].split("\n", 2)[2],
        )


class PayCallbackValidationTests(unittest.TestCase):
    def setUp(self):
        self.order = SimpleNamespace(
            out_trade_no="MMORDER1",
            openid="openid-1",
            amount_cents=199,
        )
        self.payload = {
            "appid": "wx-app",
            "mchid": "1900000001",
            "out_trade_no": "MMORDER1",
            "payer": {"openid": "openid-1"},
            "amount": {"total": 199, "currency": "CNY"},
        }

    def test_valid_paid_order_is_accepted(self):
        with patch("routers.pay.settings.WECHAT_APPID", "wx-app"), patch(
            "routers.pay.settings.WECHAT_MCHID", "1900000001"
        ):
            self.assertEqual(_validate_paid_order(self.payload, self.order), "")

    def test_wrong_amount_is_rejected(self):
        self.payload["amount"]["total"] = 1
        with patch("routers.pay.settings.WECHAT_APPID", "wx-app"), patch(
            "routers.pay.settings.WECHAT_MCHID", "1900000001"
        ):
            self.assertEqual(
                _validate_paid_order(self.payload, self.order),
                "支付金额不匹配",
            )

    def test_wrong_payer_is_rejected(self):
        self.payload["payer"]["openid"] = "someone-else"
        with patch("routers.pay.settings.WECHAT_APPID", "wx-app"), patch(
            "routers.pay.settings.WECHAT_MCHID", "1900000001"
        ):
            self.assertEqual(
                _validate_paid_order(self.payload, self.order),
                "付款人 openid 不匹配",
            )


class WeChatPayPublicKeyTests(unittest.TestCase):
    def test_public_key_mode_needs_no_platform_certificate(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        public_key_id = "PUB_KEY_ID_test"
        pay = WeChatPayV3()
        pay.appid = "wx-app"
        pay.mchid = "1900000001"
        pay.apiv3_key = "a" * 32
        pay.cert_serial_no = "MERCHANT_CERT_SERIAL"
        pay.notify_url = "https://pay.mindmate.cn/api/pay/callback"

        with patch("services.pay_service.settings.WECHAT_PRIVATE_KEY_PATH", ""), patch(
            "services.pay_service.settings.WECHAT_PRIVATE_KEY_B64", "merchant-private-key"
        ), patch(
            "services.pay_service.settings.WECHAT_PAY_PUBLIC_KEY_ID", public_key_id
        ), patch(
            "services.pay_service.settings.WECHAT_PAY_PUBLIC_KEY_PATH", ""
        ), patch(
            "services.pay_service.settings.WECHAT_PAY_PUBLIC_KEY_B64",
            base64.b64encode(public_pem).decode(),
        ), patch("services.pay_service.settings.WECHAT_PLATFORM_CERT_PATH", ""), patch(
            "services.pay_service.settings.WECHAT_PLATFORM_CERT_B64", ""
        ):
            ready, missing = pay.is_configured()
            loaded = pay._get_callback_public_key(public_key_id)

        self.assertTrue(ready, missing)
        self.assertEqual(
            loaded.public_numbers(),
            key.public_key().public_numbers(),
        )


if __name__ == "__main__":
    unittest.main()
