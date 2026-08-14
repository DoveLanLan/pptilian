import contextlib
import os
import unittest
from unittest.mock import patch

import app
import core


class PaypalBrProxyRoutingTests(unittest.TestCase):
    def test_domain_routes_split_chatgpt_from_provider(self):
        with patch.dict(os.environ, {"FRONT_PROXY": "socks5://127.0.0.1:10808"}):
            routes = app._build_paypal_br_domain_routes(
                "185.40.86.39:80:test-user:test-password",
            )

        self.assertIsNotNone(routes)
        checkout_proxy, checkout_label, provider_proxy, provider_label = routes
        self.assertEqual(checkout_proxy, "socks5h://127.0.0.1:10808")
        self.assertEqual(checkout_label, "ChatGPT socks5h://127.0.0.1:10808")
        self.assertEqual(provider_proxy, "http://test-user:test-password@185.40.86.39:80")
        self.assertIn("Stripe/PayPal 直连", provider_label)
        self.assertNotIn("test-password", provider_label)
        self.assertIn("test-user:***@185.40.86.39:80", provider_label)

    def test_domain_routes_preserve_old_path_without_front(self):
        with patch.dict(os.environ, {}, clear=True):
            routes = app._build_paypal_br_domain_routes(
                "185.40.86.39:80:test-user:test-password",
            )

        self.assertIsNone(routes)

    def test_br_approve_reuses_chatgpt_route(self):
        checkout_proxy = "socks5h://127.0.0.1:10808"
        provider_proxy = "http://test-user:test-password@185.40.86.39:80"
        captured = {}
        checkout = {
            "cs_id": "cs_test",
            "billing_country": "BR",
            "currency": "BRL",
        }

        def redirect_after_confirm(*args, **kwargs):
            captured["approval_proxy"] = args[7]
            return "https://www.paypal.com/checkoutnow?token=test"

        replacements = {
            "opll_create_checkout": lambda *args, **kwargs: checkout,
            "opll_build_stripe_session": lambda proxy: object(),
            "opll_stripe_init": lambda *args, **kwargs: {
                "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_test",
            },
            "opll_to_openai_pay_url": lambda value: value,
            "opll_stripe_key_for_checkout": lambda value: "pk_test",
            "opll_stripe_context": lambda value: {"currency": "brl"},
            "opll_stripe_amount_info": lambda value: (0, "test"),
            "opll_payment_method_available": lambda *args: True,
            "opll_billing_for_country": lambda country: {"country": country},
            "opll_stripe_create_paypal_method": lambda *args, **kwargs: "pm_test",
            "opll_stripe_confirm": lambda *args, **kwargs: {},
            "opll_redirect_url_after_confirm": redirect_after_confirm,
            "opll_is_paypal_success_url": lambda *args, **kwargs: True,
        }
        with contextlib.ExitStack() as stack:
            for name, replacement in replacements.items():
                stack.enter_context(patch.object(core, name, replacement))
            result = core.generate_opll_paypal_long_link(
                "access-token",
                "BR",
                "BRL",
                proxy_url=checkout_proxy,
                provider_proxy_url=provider_proxy,
                paypal_result_mode="paypal_link",
                payment_locale="pt-BR",
                force_country=True,
            )

        self.assertEqual(captured["approval_proxy"], checkout_proxy)
        self.assertTrue(result["long_url"].startswith("https://www.paypal.com/"))

    def test_retry_entry_passes_split_routes_to_payment_core(self):
        captured = {}

        def generate_payment_link(token, mode_name, proxy_url, **kwargs):
            captured["token"] = token
            captured["mode_name"] = mode_name
            captured["checkout_proxy"] = proxy_url
            captured["provider_proxy"] = kwargs.get("provider_proxy_url")
            return {"long_url": "https://www.paypal.com/checkoutnow?token=test"}

        data = {
            "payment_proxy_pool": "185.40.86.39:80:test-user:test-password",
            "max_attempts": 1,
        }
        with patch.dict(os.environ, {"FRONT_PROXY": "socks5://127.0.0.1:10808"}), \
                patch.object(app, "generate_payment_link", generate_payment_link), \
                patch.object(app, "_increment_success_count", lambda: 1):
            result = app._generate_with_retries(
                "access-token",
                "PayPal 长链接 BR/BRL",
                data,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["checkout_proxy"], "socks5h://127.0.0.1:10808")
        self.assertEqual(
            captured["provider_proxy"],
            "http://test-user:test-password@185.40.86.39:80",
        )
        self.assertIn("ChatGPT socks5h://127.0.0.1:10808", result["proxy_used"])
        self.assertIn("Stripe/PayPal 直连", result["proxy_used"])

    def test_proxy_candidate_uses_split_route_for_paypal_br_detection(self):
        class FakeResponse:
            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self._payload = payload or {}

            def json(self):
                return self._payload

        class FakeSession:
            def __init__(self):
                self.trust_env = True
                self.proxies = {}

            def get(self, url, **kwargs):
                if "chatgpt.com" in url:
                    return FakeResponse(200)
                if "api.stripe.com" in url:
                    return FakeResponse(404)
                if "paypal.com" in url:
                    return FakeResponse(302)
                if "ipinfo.io" in url:
                    return FakeResponse(200, {
                        "ip": "203.0.113.10",
                        "country": "BR",
                        "city": "Sao Paulo",
                        "org": "Test ISP",
                    })
                raise AssertionError(f"unexpected URL: {url}")

        with patch.dict(os.environ, {"FRONT_PROXY": "socks5://127.0.0.1:10808"}), \
                patch.object(app.requests, "Session", FakeSession):
            result = app._test_proxy_candidate(
                1,
                "185.40.86.39:80:test-user:test-password",
                False,
                "PayPal 长链接 BR/BRL",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["route_mode"], "paypal_br_split")
        self.assertEqual(result["chatgpt_status"], 200)
        self.assertEqual(result["stripe_status"], 404)
        self.assertEqual(result["paypal_status"], 302)
        self.assertEqual(result["country"], "BR")
        self.assertIn("ChatGPT socks5h://127.0.0.1:10808", result["proxy_used"])
        self.assertIn("Stripe/PayPal 直连", result["proxy_used"])


if __name__ == "__main__":
    unittest.main()
