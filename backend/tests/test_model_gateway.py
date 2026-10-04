import unittest

from config import settings
from services.model_gateway import ModelProviderUnavailable, get_model_routes


class ModelGatewayTests(unittest.TestCase):
    def setUp(self):
        self.original = {
            "provider": settings.LLM_PROVIDER,
            "dashscope": settings.DASHSCOPE_API_KEY,
            "zhipu": settings.ZHIPU_API_KEY,
        }

    def tearDown(self):
        settings.LLM_PROVIDER = self.original["provider"]
        settings.DASHSCOPE_API_KEY = self.original["dashscope"]
        settings.ZHIPU_API_KEY = self.original["zhipu"]

    def test_auto_prefers_bailian(self):
        settings.LLM_PROVIDER = "auto"
        settings.DASHSCOPE_API_KEY = "bailian-test"
        settings.ZHIPU_API_KEY = "zhipu-test"
        self.assertEqual([route.name for route in get_model_routes()], ["bailian", "zhipu"])

    def test_preferred_provider_keeps_fallback(self):
        settings.LLM_PROVIDER = "bailian"
        settings.DASHSCOPE_API_KEY = "bailian-test"
        settings.ZHIPU_API_KEY = "zhipu-test"
        self.assertEqual([route.name for route in get_model_routes()], ["bailian", "zhipu"])

    def test_missing_provider_is_explicit(self):
        settings.DASHSCOPE_API_KEY = ""
        settings.ZHIPU_API_KEY = ""
        with self.assertRaises(ModelProviderUnavailable):
            get_model_routes()


if __name__ == "__main__":
    unittest.main()
