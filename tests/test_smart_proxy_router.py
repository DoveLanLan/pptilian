import os
import unittest
from unittest.mock import patch

import app


class SmartProxyRouterTests(unittest.TestCase):
    def test_openai_target_falls_back_to_front_proxy(self):
        country = "http://country-user:country-pass@country.example:8080"
        front = "socks5h://127.0.0.1:10808"
        router = app.SmartProxyRouter(front, country)
        calls = []
        front_socket = object()

        def open_via(proxy_url, target):
            calls.append((proxy_url, target))
            if proxy_url == country:
                raise ConnectionResetError("country proxy reset")
            return front_socket

        with patch.object(router, "_open_via_proxy", open_via):
            result = router._open_chain_to_target("chatgpt.com:443")

        self.assertIs(result, front_socket)
        self.assertEqual(calls, [
            (country, "chatgpt.com:443"),
            (front, "chatgpt.com:443"),
        ])
        self.assertEqual(router.fallback_count, 1)

    def test_provider_target_never_falls_back_outside_country(self):
        country = "http://country-user:country-pass@country.example:8080"
        router = app.SmartProxyRouter("socks5h://127.0.0.1:10808", country)
        calls = []

        def open_via(proxy_url, target):
            calls.append((proxy_url, target))
            raise ConnectionResetError("country proxy reset")

        with patch.object(router, "_open_via_proxy", open_via):
            with self.assertRaises(ConnectionResetError):
                router._open_chain_to_target("api.stripe.com:443")

        self.assertEqual(calls, [(country, "api.stripe.com:443")])
        self.assertEqual(router.fallback_count, 0)

    def test_global_rotation_passes_smart_routes_to_core(self):
        captured = {}

        def generate_payment_link(token, mode_name, proxy_url, **kwargs):
            captured["main"] = proxy_url
            captured["promo"] = kwargs.get("provider_proxy_url")
            return {"long_url": "https://www.paypal.com/agreements/approve?ba_token=test"}

        data = {
            "payment_proxy_pool": "main.example:8080:main-user:main-pass",
            "provider_proxy_pool": "promo.example:8080:promo-user:promo-pass",
            "max_attempts": 1,
        }
        with patch.dict(os.environ, {"FRONT_PROXY": "socks5://127.0.0.1:10808"}), \
                patch.object(app, "generate_payment_link", generate_payment_link), \
                patch.object(app, "_increment_success_count", lambda: 1):
            result = app._generate_with_retries(
                "access-token",
                "PAYPAL全球轮转",
                data,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(captured["main"].startswith("http://127.0.0.1:"))
        self.assertTrue(captured["promo"].startswith("http://127.0.0.1:"))
        self.assertIn("智能路由", result["proxy_used"])
        self.assertIn("OpenAI失败回退", result["proxy_used"])


if __name__ == "__main__":
    unittest.main()
