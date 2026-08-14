"""
Core module: Session-based ChatGPT Plus payment link generation.
Extracted from the desktop app — zero GUI dependencies, server-deployable.

Supports all payment modes:
  - 无卡长链接 (hosted Stripe checkout URL, no card needed)
  - PayPal 长链接 (PayPal BA approve URL / BR PayPal URL extraction)
  - GoPay 长链接
  - Apple Pay 支付页

Usage:
    from core import parse_session_json, generate_payment_link

    access_token = parse_session_json(session_json_text)
    result = generate_payment_link(
        access_token=access_token,
        mode="无卡长链接 US/USD",
        proxy_url="http://127.0.0.1:7890",
    )
    print(result["long_url"])
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import importlib.util
import io
import json
import os
import random
import re
import select
import shlex
import socket
import ssl
import threading
import time
import unicodedata
import uuid
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, unquote, urljoin, urlparse, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import socks  # type: ignore
except ImportError:
    socks = None

try:
    from curl_cffi.requests import Session as CurlCffiSession  # type: ignore
except ImportError:
    CurlCffiSession = None  # type: ignore

try:
    from playwright.sync_api import sync_playwright  # type: ignore
except Exception:
    sync_playwright = None  # type: ignore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BROWSER_IMPERSONATE = "chrome136"
BROWSER_CHROME_MAJOR = "136"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{BROWSER_CHROME_MAJOR}.0.0.0 Safari/537.36"
)
DEFAULT_SEC_CH_UA = (
    f'"Google Chrome";v="{BROWSER_CHROME_MAJOR}", '
    f'"Not.A/Brand";v="8", "Chromium";v="{BROWSER_CHROME_MAJOR}"'
)
FIREFOX_MAJOR = "147"
FIREFOX_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:"
    f"{FIREFOX_MAJOR}.0) Gecko/20100101 Firefox/{FIREFOX_MAJOR}.0"
)
PAYPAL_GLOBAL_OAICS_BROWSER_PROFILE = (
    os.environ.get("PAYPAL_GLOBAL_OAICS_BROWSER_PROFILE")
    or os.environ.get("PAYPAL_OAICS_BROWSER_PROFILE")
    or "firefox147"
)
DEFAULT_STRIPE_PK = "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"
PIX_USER_AGENT = DEFAULT_USER_AGENT
# The old standalone beta header is now rejected by Stripe. PIX uses the same
# valid checkout beta header as the rest of the app, while keeping PIX-specific
# confirm/return-url/extraction handling below.
PIX_STRIPE_VERSION_FULL = STRIPE_VERSION_FULL
PIX_STRIPE_RUNTIME_VERSION = "fed52f3bc6"
PAY_LONG_LINK_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Payment mode definitions
# ---------------------------------------------------------------------------

PAYMENT_MODES = {
    "荷兰3.0提链":              {"country": "NL", "currency": "EUR", "local_payment": "ideal", "ideal_v3": True, "split_provider": True},
    "NL荷兰提链":              {"country": "NL", "currency": "EUR", "local_payment": "ideal", "split_provider": True},
    "NL荷兰提链2.0":           {"country": "NL", "currency": "EUR", "local_payment": "ideal", "ideal_v2": True, "split_provider": True},
    "Team Codex低价链 US $0.52": {"country": "US", "currency": "USD", "team_codex_low": True, "checkout_ui_mode": "hosted"},
    "7.11号BA/PM链接提取": {"country": "US", "currency": "USD", "ba_pm_711": True, "split_provider": True, "paypal_result_mode": "pm_or_paypal"},
    "PAYPAL全球轮转": {"country": "US", "currency": "USD", "paypal_global_rotation": True, "split_provider": True, "paypal_result_mode": "ba_or_pm"},
    "PayPal全球无优惠提链": {"country": "US", "currency": "USD", "paypal_global_no_discount": True, "paypal_result_mode": "ba_or_pm"},
    "韩国KAKAO提链":           {"country": "KR", "currency": "KRW", "local_payment": "kakao_pay", "split_provider": True},
    "韩国KAKAO提链2.0":        {"country": "KR", "currency": "KRW", "local_payment": "kakao_pay", "kakao_v2": True, "split_provider": True},
    "韩国KAKAO提链3.0":        {"country": "KR", "currency": "KRW", "local_payment": "kakao_pay", "kakao_v2": True, "kakao_v3": True, "split_provider": True},
    "瑞士TWINT提链":           {"country": "CH", "currency": "CHF", "local_payment": "twint", "twint_v2": True, "split_provider": True},
    "瑞士TWINT提链2.0":        {"country": "CH", "currency": "CHF", "local_payment": "twint", "twint_v2": True, "twint_v2_custom": True, "split_provider": True},
    "泰国PromptPay提链":       {"country": "TH", "currency": "THB", "local_payment": "promptpay", "promptpay_v2": True, "split_provider": True},
    "越南MoMo提链":            {"country": "VN", "currency": "VND", "local_payment": "momo", "momo_flow": True},
    "提取UPI二维码":           {"country": "IN", "currency": "INR", "local_payment": "upi", "split_provider": True},
    "提取UPI二维码2.0":         {"country": "IN", "currency": "INR", "local_payment": "upi", "split_provider": True, "upi_v2": True},
    "提取UPI二维码3.0":         {"country": "IN", "currency": "INR", "local_payment": "upi", "split_provider": True, "upi_v2": True, "upi_v3": True},
    "巴西PIX提链/二维码2.0":       {"country": "BR", "currency": "BRL", "local_payment": "pix", "pix_flow": True, "pix_v2": True, "split_provider": True},
    "巴西PIX提链/二维码3.0":       {"country": "BR", "currency": "BRL", "local_payment": "pix", "pix_flow": True, "pix_v2": True, "pix_v3": True, "split_provider": True},
    "巴西PIX提链":                  {"country": "BR", "currency": "BRL", "local_payment": "pix", "pix_flow": True, "pix_standalone_zero": True, "split_provider": True},
    "巴西PIX后置优惠提链":            {"country": "BR", "currency": "BRL", "local_payment": "pix", "pix_flow": True, "pix_post_promo": True, "split_provider": True},
    "巴西PIX正常二维码":              {"country": "BR", "currency": "BRL", "local_payment": "pix", "pix_normal_qr": True, "split_provider": False},
    "无卡长链接 US/USD":       {
        "country": "US",
        "currency": "USD",
        # 该项目里的“无卡 US”不要再返回 pay.openai.com/c/pay/cs_live 假链；
        # 走与 PayPal US 相同的 Stripe confirm / ChatGPT approve 流程，
        # 成功标准为 pm-redirects.stripe.com 或 PayPal BA approve 真链。
        "true_no_card_us": True,
        "paypal_result_mode": "pm_or_paypal",
    },
    "无卡长链接 BR/BRL":       {"country": "BR", "currency": "BRL"},
    "无卡长链接 DE/EUR":       {"country": "DE", "currency": "EUR"},
    "无卡长链接 FR/EUR":       {"country": "FR", "currency": "EUR"},
    "无卡长链接 GB/GBP":       {"country": "GB", "currency": "GBP"},
    "无卡长链接 CA/CAD":       {"country": "CA", "currency": "CAD"},
    "无卡长链接 AU/AUD":       {"country": "AU", "currency": "AUD"},
    "无卡长链接 JP/JPY":       {"country": "JP", "currency": "JPY"},
    "菲律宾短链 PH/PHP":       {
        "country": "PH",
        "currency": "PHP",
        # 只创建 ChatGPT custom checkout，不进入 Stripe init，输出形如：
        # https://chatgpt.com/checkout/openai_llc/oaics_xxx
        "chatgpt_short_link": True,
        "checkout_ui_mode": "custom",
        "short_link_processor_entity": "openai_llc",
    },
    "菲律宾跨区转优惠提链":      {
        "country": "PH",
        "currency": "PHP",
        # Stage 1 creates a PH/PHP custom checkout with the checkout proxy;
        # stage 2 reuses that checkout id and submits the promotion update with
        # the dedicated promotion proxy. No Stripe/provider stage is entered.
        "ph_cross_region_promo": True,
        "split_provider": True,
        "checkout_ui_mode": "custom",
        "short_link_processor_entity": "openai_llc",
    },
    "菲律宾 GCash 提链":          {
        "country": "PH",
        "currency": "PHP",
        # PH/PHP custom checkout -> cross-region promotion -> select GCash ->
        # return the Adyen checkoutPaymentRedirect action URL.
        "ph_gcash_redirect": True,
        "split_provider": True,
        "checkout_ui_mode": "custom",
        "short_link_processor_entity": "openai_llc",
    },
    "GoPay 长链接 ID/IDR":     {"country": "ID", "currency": "IDR"},
    "PayPal 长链接 US/USD":    {"country": "US", "currency": "USD"},
    "PayPal 长链接 FR/EUR":    {"country": "FR", "currency": "EUR"},
    "PayPal 长链接 BR/BRL":    {
        "country": "BR",
        "currency": "BRL",
        # Brazil PayPal extraction should stay BR end-to-end and only needs
        # a real PayPal page/link, not necessarily the strict BA approve URL.
        "paypal_force_country": True,
        "paypal_result_mode": "paypal_link",
        "paypal_page_country": "BR",
        "payment_locale": "pt-BR",
    },
    "Apple Pay 支付页 US/USD": {"country": "US", "currency": "USD", "apple_pay_hosted": True},
    "Apple Pay 支付页 JP/JPY": {"country": "JP", "currency": "JPY", "apple_pay_hosted": True},
}

# ---------------------------------------------------------------------------
# Country / currency / locale data
# ---------------------------------------------------------------------------

PAYPAL_GLOBAL_BILLING_COUNTRIES = frozenset({
    "JP", "BR", "US", "GB", "IN", "ID", "TH", "KR", "CH",
    "SG", "PL", "MY", "NL", "AE", "AT", "DE", "UA", "VN", "PH",
    "BA", "BH",
})

PAYPAL_GLOBAL_FLOW_BUILD = "explicit_pm_fallback_v15_oaics_reference_7step_20260811"

PAYPAL_GLOBAL_PAYMENT_LOCALES = frozenset({
    "en", "zh-CN", "zh-TW", "ja", "ko", "nl", "de", "fr", "es", "id", "pt-BR",
})

PAYPAL_GLOBAL_EMAIL_DOMAINS = {
    "JP": "outlook.jp", "BR": "outlook.com", "US": "gmail.com", "GB": "icloud.com",
    "IN": "in.example.com", "ID": "id.example.com", "TH": "th.example.com",
    "KR": "kr.example.com", "CH": "ch.example.com", "SG": "sg.example.com",
    "PL": "pl.example.com", "MY": "my.example.com", "NL": "nl.example.com",
    "AE": "ae.example.com", "AT": "at.example.com", "DE": "de.example.com",
    "UA": "ua.example.com", "VN": "vn.example.com", "PH": "ph.example.com",
    "BA": "ba.example.com", "BH": "bh.example.com",
}

# PayPal global now treats these as three separate knobs:
#   1) billing country/currency/profile
#   2) PayPal/main proxy country (checkout + Stripe + PayPal + approve route)
#   3) promotion/update proxy country (checkout/update only)
# BR/TH main routes are the known cases where OAICS custom checkout is preferred
# while billing can stay DE/EUR or another user-selected country.
PAYPAL_GLOBAL_OAICS_MAIN_PROXY_COUNTRIES = frozenset({"BR", "TH"})
PAYPAL_GLOBAL_OAICS_PROBE_COUNTRY = "BR"
PAYPAL_GLOBAL_OAICS_ELIGIBILITY_CACHE: dict[str, dict] = {}
PAYPAL_GLOBAL_OAICS_ELIGIBILITY_LOCK = threading.Lock()

COUNTRY_CURRENCY = {
    "AT": "EUR", "AU": "AUD", "BE": "EUR", "BR": "BRL", "CA": "CAD", "CH": "CHF",
    "CZ": "CZK", "DE": "EUR", "DK": "DKK", "ES": "EUR", "FI": "EUR", "FR": "EUR",
    "GB": "GBP", "HK": "HKD", "ID": "IDR", "IE": "EUR", "IN": "INR", "IT": "EUR",
    "JP": "JPY", "KR": "KRW", "MX": "MXN", "MY": "MYR", "NL": "EUR", "NO": "NOK",
    "NZ": "NZD", "PH": "PHP", "PL": "PLN", "PT": "EUR", "SE": "SEK", "SG": "SGD",
    "TH": "THB", "TW": "TWD", "US": "USD", "VN": "VND",
    "AE": "AED", "AR": "ARS", "BA": "BAM", "BH": "BHD", "BM": "BMD", "BO": "BOB", "BQ": "USD",
    "CL": "CLP", "CO": "COP", "GU": "USD", "IL": "ILS", "PR": "USD", "TR": "TRY",
    "UA": "UAH", "UM": "USD", "ZA": "ZAR",
}

OPENAI_SUPPORTED_COUNTRY_CODES = {
    "AX", "AL", "DZ", "AS", "AD", "AO", "AI", "AQ", "AG", "AR",
    "AM", "AW", "AU", "AT", "AZ", "BS", "BH", "BD", "BB", "BE",
    "BZ", "BJ", "BM", "BT", "BO", "BQ", "BA", "BW", "BV", "BR",
    "IO", "BN", "BG", "BF", "BI", "CV", "KH", "CM", "CA", "KY",
    "CF", "TD", "CL", "CX", "CC", "CO", "KM", "CG", "CK", "CR",
    "CI", "HR", "CW", "CY", "CZ", "DK", "DJ", "DM", "DO", "EC",
    "SV", "GQ", "ER", "EE", "SZ", "FK", "FO", "FJ", "FI", "FR",
    "GF", "PF", "TF", "GA", "GM", "GE", "DE", "GH", "GI", "GR",
    "GL", "GD", "GP", "GU", "GT", "GG", "GN", "GW", "GY", "HT",
    "HM", "VA", "HN", "HU", "IS", "IN", "ID", "IQ", "IE", "IM",
    "IL", "IT", "JM", "JP", "JE", "JO", "KZ", "KE", "KI", "KW",
    "KG", "LA", "LV", "LB", "LS", "LR", "LI", "LT", "LU", "MG",
    "MW", "MY", "MV", "ML", "MT", "MH", "MQ", "MR", "MU", "YT",
    "MX", "FM", "MD", "MC", "MN", "ME", "MS", "MA", "MZ", "MM",
    "NA", "NR", "NP", "NL", "NC", "NZ", "NI", "NE", "NG", "NU",
    "NF", "MK", "MP", "NO", "OM", "PK", "PW", "PS", "PA", "PG",
    "PE", "PH", "PN", "PL", "PT", "PR", "QA", "RE", "RO", "RW",
    "BL", "SH", "KN", "LC", "MF", "PM", "VC", "WS", "SM", "ST",
    "SN", "RS", "SC", "SL", "SG", "SX", "SK", "SI", "SB", "SO",
    "ZA", "GS", "KR", "SS", "ES", "LK", "SR", "SJ", "SE", "CH",
    "TW", "TZ", "TH", "TL", "TG", "TK", "TO", "TT", "TN", "TR",
    "TM", "TC", "TV", "UG", "UA", "AE", "GB", "UM", "US", "UY",
    "UZ", "VU", "WF", "EH", "ZM",
}

EUR_COUNTRIES = {
    "AD", "AT", "BE", "CY", "EE", "FI", "FR", "DE", "GR", "HR",
    "IE", "IT", "LV", "LT", "LU", "MT", "MC", "ME", "NL", "PT",
    "SM", "SK", "SI", "ES",
}
COUNTRY_CURRENCY.update({country: "EUR" for country in EUR_COUNTRIES if country not in COUNTRY_CURRENCY})

# Currency enum accepted by ChatGPT payments/checkout. Some supported countries
# do not have their local currency in this enum (for example UA -> UAH), so
# PAYPAL global rotation must keep the billing country but fall back to USD.
OPENAI_CHECKOUT_ALLOWED_CURRENCIES = {
    "USD", "AUD", "CAD", "GBP", "EUR", "CLP", "JPY", "INR", "IDR",
    "PKR", "THB", "MYR", "TWD", "VND", "PHP", "NGN", "ZAR",
    "KZT", "TZS", "EGP", "BRL", "SEK", "CZK", "PLN", "DKK",
    "NOK", "KRW", "COP", "MXN", "PEN", "HUF", "QAR", "RON",
    "ILS", "AED", "SGD", "NZD", "CHF", "SAR",
}

COUNTRY_PHONE_PREFIX = {
    "AU": "+61", "CA": "+1", "DE": "+49", "GB": "+44", "IE": "+353", "JP": "+81",
    "NZ": "+64", "SG": "+65", "TH": "+66", "US": "+1",
    "AD": "+376", "AE": "+971", "AL": "+355", "AR": "+54", "AT": "+43", "BE": "+32",
    "BA": "+387", "BG": "+359", "BH": "+973", "BM": "+1", "BO": "+591", "BR": "+55", "CH": "+41",
    "CL": "+56", "CO": "+57", "CR": "+506", "CY": "+357", "CZ": "+420", "DK": "+45",
    "EE": "+372", "ES": "+34", "FI": "+358", "FR": "+33", "GI": "+350", "GR": "+30",
    "HK": "+852", "HU": "+36", "ID": "+62", "IL": "+972", "IN": "+91", "IS": "+354",
    "IT": "+39", "KR": "+82", "KZ": "+7", "LI": "+423", "LT": "+370", "LU": "+352",
    "LV": "+371", "MC": "+377", "MD": "+373", "ME": "+382", "MK": "+389", "MT": "+356",
    "MX": "+52", "MY": "+60", "NL": "+31", "NO": "+47", "PH": "+63", "PL": "+48",
    "PT": "+351", "QA": "+974", "RO": "+40", "RS": "+381", "SA": "+966", "SE": "+46",
    "SI": "+386", "SK": "+421", "SM": "+378", "TR": "+90", "TW": "+886", "UA": "+380",
    "UY": "+598", "VN": "+84", "ZA": "+27",
}

LOCALE_MAP = {
    "de": ("de-DE", "de"), "en": ("en-US", "en"), "en-US": ("en-US", "en"),
    "es": ("es-ES", "es"), "fr": ("fr-FR", "fr"), "id": ("id-ID", "id"),
    "it": ("it-IT", "it"), "ja": ("ja-JP", "ja"), "ko": ("ko-KR", "ko"),
    "nl": ("nl-NL", "nl"), "nl-NL": ("nl-NL", "nl"),
    "pt-BR": ("pt-BR", "pt-BR"), "zh-CN": ("zh-CN", "zh-CN"), "zh-TW": ("zh-TW", "zh-TW"),
}

# ---------------------------------------------------------------------------
# Billing data pools (randomized per request)
# ---------------------------------------------------------------------------

US_BILLING_NAMES = [
    ("James", "Smith"), ("John", "Brown"), ("Michael", "Johnson"),
    ("Robert", "Miller"), ("David", "Davis"), ("William", "Wilson"),
]
US_BILLING_STREETS = [
    ("3110 Sunset Boulevard", "Los Angeles", "CA", "90026"),
    ("1200 Market Street", "San Francisco", "CA", "94102"),
    ("500 Main Street", "Austin", "TX", "78701"),
    ("88 Broadway", "New York", "NY", "10007"),
    ("1200 Peachtree St", "Atlanta", "GA", "30309"),
]

DE_BILLING_NAMES = [
    ("Lukas", "Schneider"), ("Felix", "Muller"), ("Jonas", "Weber"),
    ("Leon", "Fischer"), ("Marie", "Wagner"), ("Laura", "Becker"),
    ("Maximilian", "Hoffmann"), ("Paul", "Schulz"), ("Emma", "Koch"),
    ("Hannah", "Bauer"), ("Sophie", "Richter"), ("Noah", "Klein"),
]
DE_BILLING_STREETS = [
    ("Friedrichstrasse 123", "Berlin", "BE", "10117"),
    ("Leopoldstrasse 50", "Munich", "BY", "80802"),
    ("Zeil 85", "Frankfurt am Main", "HE", "60313"),
    ("Konigsallee 60", "Dusseldorf", "NW", "40212"),
    ("Moenckebergstrasse 7", "Hamburg", "HH", "20095"),
    ("Hohenzollernring 72", "Cologne", "NW", "50672"),
    ("Kaiserstrasse 44", "Stuttgart", "BW", "70173"),
    ("Kaufingerstrasse 15", "Munich", "BY", "80331"),
    ("Georgstrasse 24", "Hanover", "NI", "30159"),
    ("Prager Strasse 9", "Dresden", "SN", "01069"),
    ("Schadowstrasse 36", "Dusseldorf", "NW", "40212"),
    ("Breite Strasse 18", "Bonn", "NW", "53111"),
]

GB_BILLING_NAMES = [
    ("Oliver", "Smith"), ("George", "Taylor"), ("Harry", "Brown"),
    ("Noah", "Wilson"), ("Jack", "Davies"), ("Arthur", "Evans"),
    ("Olivia", "Johnson"), ("Amelia", "Roberts"), ("Isla", "Walker"),
    ("Ava", "Thompson"), ("Mia", "White"), ("Grace", "Hughes"),
]
GB_BILLING_STREETS = [
    ("221B Baker Street", "London", "England", "NW1 6XE"),
    ("10 Downing Street", "London", "England", "SW1A 2AA"),
    ("45 Deansgate", "Manchester", "England", "M3 2AY"),
    ("18 Park Row", "Leeds", "England", "LS1 5JA"),
    ("77 Queen Street", "Cardiff", "Wales", "CF10 2GR"),
    ("9 Princes Street", "Edinburgh", "Scotland", "EH2 2ER"),
    ("33 Broad Street", "Birmingham", "England", "B1 2HF"),
    ("14 Castle Street", "Liverpool", "England", "L2 0NE"),
    ("52 College Green", "Bristol", "England", "BS1 5SH"),
    ("6 Royal Avenue", "Belfast", "Northern Ireland", "BT1 1DA"),
]

AU_BILLING_NAMES = [
    ("Jack", "Wilson"), ("Oliver", "Taylor"), ("Noah", "Brown"),
    ("Charlotte", "Smith"), ("Amelia", "Jones"), ("Isla", "Williams"),
]
AU_BILLING_STREETS = [
    ("120 Collins Street", "Melbourne", "Victoria", "3000"),
    ("88 George Street", "Sydney", "New South Wales", "2000"),
    ("45 Queen Street", "Brisbane", "Queensland", "4000"),
    ("22 King William Street", "Adelaide", "South Australia", "5000"),
    ("60 St Georges Terrace", "Perth", "Western Australia", "6000"),
    ("18 Elizabeth Street", "Hobart", "Tasmania", "7000"),
]

BR_BILLING_NAMES = [
    ("Lucas", "Silva"), ("Gabriel", "Santos"), ("Pedro", "Oliveira"),
    ("Matheus", "Souza"), ("Ana", "Costa"), ("Julia", "Pereira"),
    ("Mariana", "Almeida"), ("Beatriz", "Rodrigues"),
]
BR_BILLING_STREETS = [
    ("Avenida Paulista 1000", "Sao Paulo", "SP", "01310-100"),
    ("Rua Augusta 1500", "Sao Paulo", "SP", "01304-001"),
    ("Rua Visconde de Piraja 500", "Rio de Janeiro", "RJ", "22410-002"),
    ("Avenida Atlantica 1702", "Rio de Janeiro", "RJ", "22021-001"),
    ("Setor Comercial Sul Quadra 1", "Brasilia", "DF", "70307-900"),
    ("Avenida Afonso Pena 4000", "Belo Horizonte", "MG", "30130-009"),
]

# Keep the backend PIX profile source aligned with the Brazil data generator in
# static/js/sff_core.js.  The older BR_BILLING_* lists above are intentionally
# retained for the legacy flows.
BR_PROFILE_LOCATIONS = [
    ("São Paulo", "SP", ["01001-000", "01002-000", "01003-000", "01310-000", "01311-000", "01414-000", "04538-132", "04543-011"]),
    ("Rio de Janeiro", "RJ", ["20040-002", "20040-003", "20040-004", "22041-001", "22410-003", "22430-010", "22620-001", "22630-014"]),
    ("Belo Horizonte", "MG", ["30110-001", "30110-002", "30120-000", "30130-003", "30140-070", "30140-071", "30190-050", "30190-060"]),
    ("Salvador", "BA", ["40010-000", "40020-000", "40020-060", "40140-040", "40220-000", "40220-310", "41810-010", "41810-020"]),
    ("Brasília", "DF", ["70040-010", "70040-020", "70070-010", "70070-020", "70390-010", "70390-020", "70710-000", "70710-010"]),
    ("Curitiba", "PR", ["80010-010", "80010-020", "80020-000", "80020-110", "80240-000", "80240-021", "80410-000", "80420-001"]),
    ("Porto Alegre", "RS", ["90010-010", "90010-020", "90020-010", "90020-090", "90410-000", "90420-000", "90450-010", "90460-001"]),
    ("Recife", "PE", ["50010-000", "50010-010", "50020-010", "50020-020", "51011-000", "51011-051", "51020-000", "51021-010"]),
    ("Fortaleza", "CE", ["60055-100", "60110-000", "60115-000", "60120-000", "60120-010", "60125-000", "60135-000", "60150-010"]),
    ("Manaus", "AM", ["69005-010", "69005-020", "69010-000", "69010-020", "69020-010", "69020-120", "69040-010", "69040-011"]),
    ("Campinas", "SP", ["13010-010", "13010-020", "13013-000", "13013-020", "13015-000", "13015-001", "13020-000", "13020-030"]),
    ("São Bernardo do Campo", "SP", ["09606-000", "09606-010", "09710-000", "09710-020", "09720-000", "09720-010", "09750-000", "09750-020"]),
    ("Niterói", "RJ", ["24020-005", "24020-006", "24020-007", "24210-000", "24210-010", "24210-020", "24220-000", "24220-010"]),
    ("Florianópolis", "SC", ["88010-000", "88010-010", "88015-000", "88015-010", "88020-000", "88020-010", "88030-000", "88036-002"]),
    ("Goiânia", "GO", ["74000-010", "74000-020", "74015-010", "74015-020", "74020-010", "74020-020", "74030-010", "74030-020"]),
]
BR_PROFILE_STREETS = [
    "Avenida Paulista", "Avenida Rio Branco", "Avenida Atlântica", "Avenida Brasil",
    "Avenida Nossa Senhora de Copacabana", "Avenida Ipiranga", "Avenida 9 de Julho",
    "Avenida Presidente Vargas", "Avenida Getúlio Vargas", "Avenida Independência",
    "Rua das Flores", "Rua do Comércio", "Rua da Consolação", "Rua Augusta",
    "Rua Oscar Freire", "Rua 25 de Março", "Rua da Praia", "Rua dos Andradas",
    "Rua Bela Cintra", "Rua Haddock Lobo", "Rua da Assembleia", "Rua 7 de Setembro",
    "Rua Voluntários da Pátria", "Rua Gonçalves Dias", "Rua Barão do Flamengo",
    "Rua Visconde de Pirajá", "Rua Domingos de Morais", "Rua Padre João Manuel",
    "Rua Estados Unidos", "Rua Canadá", "Rua França", "Rua Inglaterra",
    "Alameda Santos", "Alameda Lorena", "Alameda Itu", "Alameda Campinas",
    "Praça da República", "Praça da Sé", "Praça Tiradentes", "Praça XV de Novembro",
    "Estrada do Campo Limpo", "Estrada da Baronesa", "Rodovia dos Imigrantes",
]
BR_PROFILE_NEIGHBORHOODS = [
    "Centro", "Copacabana", "Ipanema", "Leblon", "Barra da Tijuca", "Botafogo",
    "Flamengo", "Lapa", "Santa Teresa", "Tijuca", "Jardins", "Moema", "Pinheiros",
    "Vila Madalena", "Itaim Bibi", "Brooklin", "Morumbi", "Perdizes", "Higienópolis",
    "Bela Vista", "Consolação", "Liberdade", "Vila Mariana", "Santo Amaro",
    "Savassi", "Funcionários", "Lourdes", "Boa Viagem", "Ondina", "Pituba",
    "Batel", "Água Verde", "Asa Sul", "Asa Norte", "Lago Sul", "Sudoeste",
]
BR_PROFILE_FIRST_NAMES = [
    "Bruno", "Gabriel", "Lucas", "Mateus", "Pedro", "Rafael", "João", "Miguel",
    "Arthur", "Davi", "Bernardo", "Heitor", "Enzo", "Lorenzo", "Théo", "Vicente",
    "Felipe", "Gustavo", "Henrique", "Eduardo", "Marcos", "André", "Carlos", "Daniel",
    "Leonardo", "Victor", "Matheus", "Samuel", "Lucca", "Nicolas", "Guilherme", "Caio",
    "Paulo", "Francisco", "Ricardo", "Fernando", "Antônio", "José", "Fábio", "Diego",
    "Rodrigo", "Alexandre", "Roberto", "Renato", "Sérgio", "Jorge", "Otávio", "Raul",
    "Ana", "Beatriz", "Camila", "Daniela", "Fernanda", "Gabriela", "Isabela", "Juliana",
    "Larissa", "Mariana", "Natália", "Patrícia", "Rafaela", "Sabrina", "Tatiana", "Vitória",
    "Adriana", "Bianca", "Carolina", "Débora", "Elaine", "Flávia", "Helena", "Ingrid",
    "Jéssica", "Luciana", "Manuela", "Nicole", "Priscila", "Renata", "Simone", "Tânia",
    "Laura", "Sofia", "Isabella", "Manuela", "Júlia", "Heloísa", "Luiza", "Lorena",
    "Alice", "Valentina", "Clara", "Cecília", "Maitê", "Maria Eduarda", "Mirella", "Elisa",
]
BR_PROFILE_LAST_NAMES = [
    "Silva", "Santos", "Oliveira", "Souza", "Pereira", "Lima", "Costa", "Ferreira",
    "Rodrigues", "Almeida", "Nascimento", "Araújo", "Ribeiro", "Carvalho", "Cardoso",
    "Barros", "Machado", "Cavalcanti", "Barbosa", "Castro", "Dias", "Gomes", "Marques",
    "Teixeira", "Coelho", "Freitas", "Batista", "Ramos", "Vieira", "Andrade", "Mendes",
    "Pinto", "Correia", "Monteiro", "Melo", "Nunes", "Lopes", "Duarte", "Moreira",
    "Fernandes", "Campos", "Leite", "Cunha", "Neves", "Sales", "Pacheco", "Tavares",
    "Martins", "Morais", "Dantas", "Rezende", "Guimarães", "Moura", "Farias", "Borges",
    "Soares", "Rocha", "Viana", "Medeiros", "Peixoto", "Xavier", "Santana", "Macedo",
    "Siqueira", "Pimentel", "Magalhães", "Bittencourt", "Albuquerque", "Montenegro",
    "Caldeira", "Figueiredo", "Gonçalves", "Bueno", "Amaral", "Miranda", "Azevedo", "Branco",
]

EXTRA_BILLING_NAMES = [
    ("Alex", "Tan"), ("Daniel", "Lee"), ("Emma", "Wong"),
    ("Mia", "Chen"), ("Noah", "Martin"), ("Olivia", "Nguyen"),
]
EXTRA_BILLING_STREETS = {
    "TH": [
        ("999 Rama I Road", "Bangkok", "Bangkok", "10330"),
        ("88 Sukhumvit Road", "Bangkok", "Bangkok", "10110"),
        ("45 Nimman Road", "Chiang Mai", "Chiang Mai", "50200"),
    ],
    "JP": [
        ("1-1 Marunouchi", "Chiyoda-ku", "Tokyo", "100-0005"),
        ("2-2-1 Yaesu", "Chuo-ku", "Tokyo", "104-0028"),
        ("3-1 Umeda", "Osaka", "Osaka", "530-0001"),
    ],
    "SG": [
        ("10 Anson Road", "Singapore", "Singapore", "079903"),
        ("1 Raffles Place", "Singapore", "Singapore", "048616"),
        ("80 Robinson Road", "Singapore", "Singapore", "068898"),
    ],
    "NZ": [
        ("22 Queen Street", "Auckland", "Auckland", "1010"),
        ("50 Lambton Quay", "Wellington", "Wellington", "6011"),
        ("120 Hereford Street", "Christchurch", "Canterbury", "8011"),
    ],
    "CA": [
        ("100 King Street West", "Toronto", "ON", "M5X 1A9"),
        ("555 West Hastings Street", "Vancouver", "BC", "V6B 4N6"),
        ("1250 Rene-Levesque Blvd", "Montreal", "QC", "H3B 4W8"),
    ],
    "IE": [
        ("1 Grand Canal Square", "Dublin", "Dublin", "D02 P820"),
        ("10 South Mall", "Cork", "Cork", "T12 RD43"),
        ("5 Eyre Square", "Galway", "Galway", "H91 FPK2"),
    ],
}

BILLING_PROFILE_CITY_BY_COUNTRY = {
    "BA": ["Sarajevo", "Banja Luka", "Mostar"], "BH": ["Manama", "Muharraq", "Riffa"],
    "AT": ["Vienna", "Graz", "Linz"], "BE": ["Brussels", "Antwerp", "Ghent"],
    "BR": ["Sao Paulo", "Rio de Janeiro", "Brasilia"],
    "CH": ["Zurich", "Geneva", "Basel"], "DK": ["Copenhagen", "Aarhus", "Odense"],
    "ES": ["Madrid", "Barcelona", "Valencia"],
    "FI": ["Helsinki", "Espoo", "Tampere"], "FR": ["Paris", "Lyon", "Marseille"],
    "ID": ["Jakarta", "Surabaya", "Bandung"],
    "IT": ["Rome", "Milan", "Turin"], "KR": ["Seoul", "Busan", "Incheon"],
    "MX": ["Mexico City", "Guadalajara", "Monterrey"],
    "NL": ["Amsterdam", "Rotterdam", "Utrecht"], "NO": ["Oslo", "Bergen", "Trondheim"],
    "PL": ["Warsaw", "Krakow", "Gdansk"],
    "PT": ["Lisbon", "Porto", "Coimbra"],
    "SE": ["Stockholm", "Gothenburg", "Malmo"],
    "TW": ["Taipei", "Taichung", "Kaohsiung"],
}

POSTAL_PATTERN_BY_COUNTRY = {
    "BA": "#####", "BH": "###",
    "AD": "AD###", "AR": "C####", "AU": "####", "AT": "####", "BE": "####",
    "BR": "#####-###", "CA": "A#A #A#", "CH": "####", "CL": "#######",
    "CZ": "### ##", "DE": "#####", "DK": "####", "ES": "#####", "FI": "#####",
    "FR": "#####", "GB": "AA# #AA", "IE": "A## A###", "ID": "#####",
    "IN": "######", "IT": "#####", "JP": "###-####", "KR": "#####",
    "MX": "#####", "NL": "#### AA", "NO": "####", "NZ": "####",
    "PL": "##-###", "PT": "####-###", "SE": "### ##", "SG": "######",
    "TH": "#####", "US": "#####",
}

BILLING_STREET_POOL = ["Market Street", "Central Avenue", "Station Road", "Main Street", "High Street", "King Street"]
BILLING_PROFILE_BY_COUNTRY = {
    country: {
        "currency": COUNTRY_CURRENCY.get(country, "USD"),
        "phone_prefix": COUNTRY_PHONE_PREFIX.get(country, "+1"),
        "city_pool": BILLING_PROFILE_CITY_BY_COUNTRY.get(country, ["Capital City", "Central District", "Market Town"]),
        "postal_pattern": POSTAL_PATTERN_BY_COUNTRY.get(country, "#####"),
        "street_pool": BILLING_STREET_POOL,
    }
    for country in OPENAI_SUPPORTED_COUNTRY_CODES
}


# ===================================================================
# Session parsing
# ===================================================================

def find_access_token(value) -> str:
    """Recursively search a dict/list for accessToken/access_token/token."""
    if isinstance(value, dict):
        for key in ("accessToken", "access_token", "token"):
            token = str(value.get(key) or "").strip()
            if token:
                return token
        for item in value.values():
            token = find_access_token(item)
            if token:
                return token
    if isinstance(value, list):
        for item in value:
            token = find_access_token(item)
            if token:
                return token
    return ""


def parse_session_json(text: str) -> str:
    """
    Extract access token from Session JSON text or raw Bearer token.
    Returns the access_token string, or empty string if not found.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    if raw.startswith("Bearer "):
        return raw.split(None, 1)[1].strip()
    try:
        return find_access_token(json.loads(raw))
    except Exception:
        pass
    match = re.search(r'"(?:accessToken|access_token|token)"\s*:\s*"([^"]+)"', raw)
    if match:
        return match.group(1).strip()
    return raw if raw.count(".") >= 2 and len(raw) > 80 else ""


# ===================================================================
# Helpers
# ===================================================================

def currency_for_country(country: str) -> str:
    return COUNTRY_CURRENCY.get(str(country or "").upper(), "USD")


def checkout_currency_for_country(country: str) -> tuple[str, str, bool]:
    local_currency = currency_for_country(country).upper()
    if local_currency in OPENAI_CHECKOUT_ALLOWED_CURRENCIES:
        return local_currency, local_currency, False
    return "USD", local_currency, True


def normalize_opll_country(country: str) -> str:
    country = str(country or "").strip().upper()
    return country if country in OPENAI_SUPPORTED_COUNTRY_CODES else "US"


def normalize_paypal_global_billing_country(country: str) -> str:
    country = str(country or "JP").strip().upper()
    if country == "UK":
        country = "GB"
    if country not in PAYPAL_GLOBAL_BILLING_COUNTRIES:
        raise ValueError(
            f"PAYPAL global unsupported billing country: {country}; "
            f"allowed={sorted(PAYPAL_GLOBAL_BILLING_COUNTRIES)}"
        )
    return country


def normalize_paypal_global_payment_locale(locale: str) -> str:
    locale = str(locale or "en").strip() or "en"
    return locale if locale in PAYPAL_GLOBAL_PAYMENT_LOCALES else "en"


def opll_normalize_country_hint(country: str) -> str:
    text = str(country or "").strip().upper()
    if text == "UK":
        text = "GB"
    return text if re.fullmatch(r"[A-Z]{2}", text or "") else ""


def opll_proxy_country_hint(proxy_url: str) -> str:
    """Best-effort country hint from proxy text; never drives billing country."""
    text = unquote(str(proxy_url or "")).upper()
    if not text:
        return ""
    candidates = set(PAYPAL_GLOBAL_BILLING_COUNTRIES) | {
        "TR", "HK", "TW", "ES", "FR", "IT", "CA", "AU", "MX", "AR", "CL",
    }
    # Common provider encodings: region-BR, country-BR, _BR_, -TH-, .de.
    for country in sorted(candidates):
        patterns = (
            rf"(?:^|[^A-Z]){re.escape(country)}(?:[^A-Z]|$)",
            rf"(?:REGION|COUNTRY|CC|LOC|GEO)[-_]{re.escape(country)}(?:[^A-Z]|$)",
            rf"(?:^|[^A-Z]){re.escape(country)}[-_](?:ST|CITY|ZONE|REGION)(?:[^A-Z]|$)",
        )
        if any(re.search(pattern, text) for pattern in patterns):
            return country
    return ""


def opll_email_slug(value: str, fallback: str = "user.mail") -> str:
    text = unicodedata.normalize("NFD", str(value or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", ".", text).strip(".")
    text = re.sub(r"\.{2,}", ".", text)
    return text or fallback


def opll_sanitize_billing_email(value: str, first: str = "user", last: str = "mail", country: str = "US") -> str:
    raw = str(value or "").strip().lower()
    default_domain = PAYPAL_GLOBAL_EMAIL_DOMAINS.get(str(country or "US").upper(), "example.com")
    if "@" in raw:
        local, domain = raw.split("@", 1)
    else:
        local = raw or f"{first}.{last}"
        domain = default_domain
    local = opll_email_slug(local, "user.mail")
    domain = unicodedata.normalize("NFD", str(domain or default_domain).lower())
    domain = "".join(ch for ch in domain if not unicodedata.combining(ch))
    domain = re.sub(r"[^a-z0-9.-]+", "", domain).strip(".-") or default_domain
    if "." not in domain:
        domain = default_domain
    return f"{local}@{domain}"


def locale_parts(locale: str = "en") -> tuple[str, str]:
    return LOCALE_MAP.get(str(locale or "").strip(), LOCALE_MAP["en"])


def opll_payment_locale_for_country(country: str) -> str:
    return {
        "BR": "pt-BR",
        "DE": "de",
        "ES": "es",
        "FR": "fr",
        "ID": "id",
        "IT": "it",
        "JP": "ja",
        "KR": "ko",
    }.get(str(country or "").strip().upper(), "en")


def opll_browser_timezone_for_country(country: str) -> str:
    return {
        "AU": "Australia/Sydney",
        "BR": "America/Sao_Paulo",
        "CA": "America/Toronto",
        "GB": "Europe/London",
        "IE": "Europe/Dublin",
        "DE": "Europe/Berlin",
        "FR": "Europe/Paris",
        "NL": "Europe/Amsterdam",
        "AT": "Europe/Vienna",
        "BE": "Europe/Brussels",
        "ES": "Europe/Madrid",
        "IT": "Europe/Rome",
        "JP": "Asia/Tokyo",
        "KR": "Asia/Seoul",
        "BA": "Europe/Sarajevo",
        "BH": "Asia/Bahrain",
        "AE": "Asia/Dubai",
        "QA": "Asia/Qatar",
        "SA": "Asia/Riyadh",
        "TR": "Europe/Istanbul",
        "VN": "Asia/Ho_Chi_Minh",
        "PH": "Asia/Manila",
        "US": "America/New_York",
    }.get(str(country or "").strip().upper(), "Europe/London")


def opll_short_error(detail: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(detail or "")).strip()
    lower = text.lower()
    html_markers = ("<!doctype", "<html", "<head", "<body", "<style")
    if any(marker in lower for marker in html_markers):
        first_html = min(
            (lower.find(marker) for marker in html_markers if lower.find(marker) >= 0),
            default=-1,
        )
        prefix = text[:first_html].strip(" ;:|-") if first_html >= 0 else ""
        http_match = re.search(r"\bHTTP\s+(\d{3})\b", text, re.IGNORECASE)
        if http_match:
            status = http_match.group(1)
            if prefix:
                base = prefix
                if f"HTTP {status}".lower() not in base.lower():
                    base = f"{base} HTTP {status}"
            else:
                base = f"HTTP {status}"
            text = f"{base} (HTML response hidden)"
        else:
            text = f"{prefix + ' ' if prefix else ''}(HTML response hidden)"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def opll_first_non_empty(values: dict, *keys: str) -> str:
    for key in keys:
        value = str(values.get(key) or "").strip()
        if value:
            return value
    return ""


def opll_random_postal_code(pattern: str) -> str:
    result = []
    for char in str(pattern or "#####"):
        if char == "#":
            result.append(str(random.randint(0, 9)))
        elif char == "A":
            result.append(chr(random.randint(ord("A"), ord("Z"))))
        else:
            result.append(char)
    return "".join(result)


def opll_random_phone_for_country(country: str) -> str:
    country = str(country or "").strip().upper()
    if country == "BR":
        # Brazil mobile: +55 + area code + 9 + 8 digits.
        area_code = random.choice(["11", "21", "31", "41", "51", "61", "71", "81"])
        return f"+55{area_code}9{random.randint(10000000, 99999999)}"
    phone_prefix = str(BILLING_PROFILE_BY_COUNTRY.get(country, {}).get("phone_prefix")
                       or COUNTRY_PHONE_PREFIX.get(country, "+1"))
    return f"{phone_prefix}{random.randint(100000000, 999999999)}"


# ===================================================================
# Proxy helpers
# ===================================================================

def normalize_proxy_url(value: str, default_scheme: str = "http") -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    scheme = default_scheme
    if "://" not in text:
        text = f"{scheme}://{text}"
    else:
        scheme = text.split("://", 1)[0].lower()
    text = _normalize_proxy_authority(text, scheme)
    text = _quote_proxy_userinfo(text)
    text = _proxy_vendor_scheme_url(text)
    return text


def _proxy_vendor_scheme_url(proxy_url: str) -> str:
    """Fix well-known provider exports that are SOCKS gateways but are pasted as http://."""
    text = str(proxy_url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        scheme = (parsed.scheme or "http").lower()
        host = (parsed.hostname or "").lower()
        port = int(parsed.port or 0)
        if "kookeey.info" in host and port in {1000, 1086} and scheme in {"http", "https"}:
            return urlunsplit(("socks5h", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return text
    return text


def proxy_region_hint(proxy_url: str) -> str:
    """Best-effort country hint from common proxy provider usernames/passwords."""
    raw = unquote(str(proxy_url or ""))
    if not raw:
        return ""
    keyword_patterns = (
        r"(?i)(?:zone[-_]custom[-_]region|custom[-_]region|region|country|cc|geo|loc)[-_=:/]+([a-z]{2})(?=$|[^a-z0-9])",
        r"(?i)(?:^|[^a-z0-9])([a-z]{2})[-_](?:city|state|st|province)[-_]",
    )
    for pattern in keyword_patterns:
        for match in re.finditer(pattern, raw):
            country = match.group(1).upper()
            if country in OPENAI_SUPPORTED_COUNTRY_CODES:
                return country
    for match in re.finditer(r"(?<![A-Za-z0-9])([A-Z]{2})_(?:[A-Za-z]+_)?(?:city|state|st|province|[A-Za-z]+)(?=[-_@:/]|$)", raw):
        country = match.group(1).upper()
        if country in OPENAI_SUPPORTED_COUNTRY_CODES:
            return country
    # Kookeey/simple export may put the country as a plain delimited token,
    # e.g. password-BH-67036747-5m or user-ES-session.
    for match in re.finditer(r"(?<![A-Za-z0-9])([A-Z]{2})(?=[-_@:/]|$)", raw):
        country = match.group(1).upper()
        if country in OPENAI_SUPPORTED_COUNTRY_CODES:
            return country
    return ""



def opll_normalize_vn_country_proxy(proxy_url: str) -> str:
    """Normalize VN state-level proxy usernames to country-level VN for promo leg."""
    text = str(proxy_url or "").strip()
    if not text:
        return ""
    return re.sub(r"(?i)(zone-custom-region-VN)-st-[^:@/]+", r"\1", text)


def _quote_proxy_userinfo(proxy_url: str) -> str:
    """Percent-encode username/password in user:pass@host proxy URLs."""
    try:
        parsed = urlsplit(str(proxy_url or "").strip())
        if "@" not in parsed.netloc:
            return proxy_url
        userinfo, host = parsed.netloc.rsplit("@", 1)
        if not userinfo or not host:
            return proxy_url
        if ":" in userinfo:
            username, password = userinfo.split(":", 1)
            quoted_userinfo = f"{quote(unquote(username), safe='')}:{quote(unquote(password), safe='')}"
        else:
            quoted_userinfo = quote(unquote(userinfo), safe="")
        return urlunsplit((parsed.scheme, f"{quoted_userinfo}@{host}", parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return proxy_url


def _normalize_proxy_authority(proxy_url: str, scheme: str) -> str:
    """Accept provider format host:port:user:pass as a proxy URL."""
    prefix, rest = proxy_url.split("://", 1)
    authority, sep, suffix = rest.partition("/")
    if "@" in authority or authority.count(":") < 3:
        return proxy_url
    host, port, username, password = authority.split(":", 3)
    if not host or not port.isdigit() or not username or not password:
        return proxy_url
    userinfo = f"{quote(username, safe='')}:{quote(password, safe='')}"
    normalized = f"{prefix or scheme}://{userinfo}@{host}:{port}"
    return normalized + (sep + suffix if sep else "")


def is_socks_proxy_url(proxy_url: str) -> bool:
    return urlsplit(str(proxy_url or "").strip()).scheme.lower() in {
        "socks4", "socks4a", "socks5", "socks5h",
    }


def mask_proxy_url(proxy_url: str) -> str:
    text = str(proxy_url or "").strip()
    if not text:
        return "??"
    region = proxy_region_hint(text)
    suffix = f" [{region}]" if region else ""
    try:
        parsed = urlsplit(text)
        if "@" not in parsed.netloc:
            return text + suffix
        userinfo, host = parsed.netloc.rsplit("@", 1)
        if ":" in userinfo:
            username, _password = userinfo.split(":", 1)
            userinfo = f"{username}:***"
        else:
            userinfo = "***"
        return urlunsplit((parsed.scheme, f"{userinfo}@{host}", parsed.path, parsed.query, parsed.fragment)) + suffix
    except Exception:
        return re.sub(r":([^:@/]+)@", ":***@", text) + suffix


# ===================================================================
# HTTP session factories
# ===================================================================


def random_proxy_sid(length: int = 10) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choice(alphabet) for _ in range(length))


def randomize_proxy_sid(proxy_url: str) -> str:
    text = str(proxy_url or "").strip()
    if not text:
        return ""
    sid = random_proxy_sid()
    parsed = urlsplit(text)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.lower() == "sid" for key, _value in query_pairs):
        query = urlencode([(key, sid if key.lower() == "sid" else value) for key, value in query_pairs])
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))
    netloc = parsed.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        new_userinfo = re.sub(r"(?i)(sid[-_=])([^-:@;&/?]+)", lambda m: f"{m.group(1)}{sid}", userinfo, count=1)
        if new_userinfo != userinfo:
            return urlunsplit((parsed.scheme, f"{new_userinfo}@{host}", parsed.path, parsed.query, parsed.fragment))
    new_text = re.sub(r"(?i)(sid[-_=])([^-:@;&/?]+)", lambda m: f"{m.group(1)}{sid}", text, count=1)
    return new_text

def opll_is_local_proxy_url(proxy_url: str) -> bool:
    try:
        host = (urlsplit(str(proxy_url or "").strip()).hostname or "").lower()
    except Exception:
        host = ""
    return host in {"127.0.0.1", "localhost", "::1"}


def opll_normalize_browser_profile(browser_profile: str = "") -> str:
    raw = str(browser_profile or "").strip().lower().replace("_", "-")
    aliases = {
        "": BROWSER_IMPERSONATE,
        "default": BROWSER_IMPERSONATE,
        "chrome": BROWSER_IMPERSONATE,
        "chrome-136": "chrome136",
        "chrome136": "chrome136",
        "firefox": "firefox147",
        "ff": "firefox147",
        "firefox-147": "firefox147",
        "firefox147": "firefox147",
        "firefox-135": "firefox135",
        "firefox135": "firefox135",
    }
    return aliases.get(raw, raw or BROWSER_IMPERSONATE)


def opll_browser_profile_config(browser_profile: str = "") -> dict:
    profile = opll_normalize_browser_profile(browser_profile)
    if profile.startswith("firefox"):
        major_match = re.search(r"(\d+)", profile)
        major = major_match.group(1) if major_match else FIREFOX_MAJOR
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:"
            f"{major}.0) Gecko/20100101 Firefox/{major}.0"
        )
        return {
            "profile": profile,
            "impersonate": profile,
            "user_agent": ua,
            "accept_language": "en-US,en;q=0.5",
            "sec_ch_ua": "",
            "sec_ch_ua_mobile": "",
            "sec_ch_ua_platform": "",
        }
    return {
        "profile": BROWSER_IMPERSONATE,
        "impersonate": BROWSER_IMPERSONATE,
        "user_agent": DEFAULT_USER_AGENT,
        "accept_language": "en-US,en;q=0.9",
        "sec_ch_ua": DEFAULT_SEC_CH_UA,
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    }


def opll_browser_profile_headers(browser_profile: str = "",
                                 accept_language: str = "") -> dict:
    cfg = opll_browser_profile_config(browser_profile)
    headers = {
        "User-Agent": cfg["user_agent"],
        "Accept-Language": str(accept_language or cfg["accept_language"] or "en-US,en;q=0.9"),
    }
    if cfg.get("sec_ch_ua"):
        headers.update({
            "sec-ch-ua": cfg["sec_ch_ua"],
            "sec-ch-ua-mobile": cfg["sec_ch_ua_mobile"],
            "sec-ch-ua-platform": cfg["sec_ch_ua_platform"],
        })
    return headers


def opll_browser_profile_from_user_agent(user_agent: str = "") -> str:
    ua = str(user_agent or "")
    if "Firefox/" in ua:
        match = re.search(r"Firefox/(\d+)", ua)
        return f"firefox{match.group(1)}" if match else "firefox"
    if "Chrome/" in ua or "Chromium/" in ua:
        match = re.search(r"(?:Chrome|Chromium)/(\d+)", ua)
        return f"chrome{match.group(1)}" if match else BROWSER_IMPERSONATE
    return BROWSER_IMPERSONATE


def opll_new_http_session(force_requests: bool = False,
                          browser_profile: str = "") -> requests.Session:
    profile_cfg = opll_browser_profile_config(browser_profile)
    active_profile = profile_cfg["profile"]
    if CurlCffiSession is not None and not force_requests:
        try:
            session = CurlCffiSession(impersonate=profile_cfg["impersonate"])  # type: ignore[assignment]
        except Exception:
            profile_cfg = opll_browser_profile_config(BROWSER_IMPERSONATE)
            active_profile = profile_cfg["profile"]
            session = CurlCffiSession(impersonate=profile_cfg["impersonate"])  # type: ignore[assignment]
    else:
        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = True
    # Keep the visible browser identity aligned with curl_cffi's TLS profile.
    # Per-request headers may add locale/origin fields, but must not advertise a
    # different browser family/major than the ClientHello profile selected above.
    session.headers.update(opll_browser_profile_headers(active_profile))
    try:
        setattr(session, "_opll_browser_profile", active_profile)
    except Exception:
        pass
    return session


def opll_http_error_detail(response, body_limit: int = 500) -> str:
    """Build a compact response signature for classifying HTTP 403 failures."""
    status = int(getattr(response, "status_code", 0) or 0)
    text = str(getattr(response, "text", "") or "")
    preview = re.sub(r"\s+", " ", text).strip()[:max(0, int(body_limit))]
    body_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    raw_headers = getattr(response, "headers", {}) or {}
    headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
    request_id = (
        headers.get("x-request-id")
        or headers.get("request-id")
        or headers.get("openai-request-id")
        or headers.get("stripe-request-id")
        or headers.get("paypal-debug-id")
        or ""
    )
    request_headers = {}
    try:
        raw_request_headers = getattr(getattr(response, "request", None), "headers", {}) or {}
        request_headers = {str(k).lower(): str(v) for k, v in raw_request_headers.items()}
    except Exception:
        request_headers = {}
    req_ua = request_headers.get("user-agent", "")
    profile_label = opll_browser_profile_from_user_agent(req_ua) if req_ua else BROWSER_IMPERSONATE
    fields = [f"profile={profile_label}", f"body_sha256={body_hash}"]
    if req_ua:
        fields.append(f"ua={req_ua[:120]}")
    for key, value in (
        ("request_id", request_id),
        ("cf_ray", headers.get("cf-ray", "")),
        ("content_type", headers.get("content-type", "")),
        ("server", headers.get("server", "")),
    ):
        if value:
            fields.append(f"{key}={value[:160]}")
    body_part = f" {preview}" if preview else ""
    return f"HTTP {status}{body_part} [diag {'; '.join(fields)}]"


def opll_normalize_chatgpt_cookie(cookie: str = "") -> str:
    """Normalize Cookie input; accepts raw Cookie or full DevTools Copy as cURL.

    Supported input:
    - raw cookie: a=b; c=d
    - Cookie: a=b; c=d
    - full curl copied from Chrome DevTools, including -b/--cookie or -H 'cookie: ...'
    """
    value = str(cookie or "").strip()
    if not value:
        return ""
    # Normalize line continuations from Copy as cURL (bash).
    compact = re.sub(r"\\\s*\r?\n", " ", value).strip()

    def _clean(v: str) -> str:
        v = str(v or "").strip().strip('"').strip("'").strip()
        if v.lower().startswith("cookie:"):
            v = v.split(":", 1)[1].strip()
        return v

    # Header form pasted directly.
    if compact.lower().startswith("cookie:"):
        return _clean(compact)

    # Full curl: parse like shell first, then fall back to regex.
    if re.search(r"(?is)^\s*curl\b|\s(?:-b|--cookie|--header|-H)\s", compact):
        try:
            parts = shlex.split(compact, posix=True)
            for i, part in enumerate(parts):
                low = part.lower()
                if low in ("-b", "--cookie") and i + 1 < len(parts):
                    found = _clean(parts[i + 1])
                    if found:
                        return found
                if low.startswith("--cookie="):
                    found = _clean(part.split("=", 1)[1])
                    if found:
                        return found
                if low in ("-h", "--header") and i + 1 < len(parts):
                    header = parts[i + 1]
                    if header.lower().startswith("cookie:"):
                        found = _clean(header)
                        if found:
                            return found
        except Exception:
            pass
        patterns = [
            r"(?:^|\s)(?:-b|--cookie)\s+(['\"])(.*?)\1",
            r"(?:^|\s)--cookie=([^\s]+)",
            r"(?:^|\s)(?:-H|--header)\s+(['\"])(cookie\s*:\s*.*?)\1",
        ]
        for pat in patterns:
            match = re.search(pat, compact, flags=re.I | re.S)
            if match:
                found = _clean(match.group(2) if len(match.groups()) >= 2 else match.group(1))
                if found:
                    return found
        # It looked like a full curl command, but no Cookie argument/header was found.
        # Returning an empty value prevents accidentally sending the whole curl as Cookie.
        return ""

    return _clean(compact)


def opll_cookie_value(cookie: str, name: str) -> str:
    pattern = r"(?:^|;\s*)" + re.escape(name) + r"=([^;]+)"
    match = re.search(pattern, opll_normalize_chatgpt_cookie(cookie))
    return match.group(1).strip() if match else ""


def opll_cookie_has_login_material(cookie: str) -> bool:
    value = opll_normalize_chatgpt_cookie(cookie)
    if not value:
        return False
    required_any = ("__Secure-next-auth.session-token", "__Secure-next-auth.session-token.0", "oai-sc")
    return any(name + "=" in value for name in required_any)


def opll_session_from_cookie(cookie: str, proxy_url: str = "") -> dict:
    """Fetch /api/auth/session using the browser Cookie; returns {} on failure.

    This keeps accessToken and Cookie from the same Chrome profile when possible.
    """
    cookie = opll_normalize_chatgpt_cookie(cookie)
    if not cookie:
        return {}
    headers = {
        "Cookie": cookie,
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://chatgpt.com/",
        "Accept-Language": "en-US,en;q=0.9",
    }
    session = opll_new_http_session()
    if hasattr(session, "trust_env"):
        session.trust_env = False if proxy_url else True
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    try:
        resp = session.get("https://chatgpt.com/api/auth/session", headers=headers, timeout=PAY_LONG_LINK_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json() or {}
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def opll_access_token_with_cookie(access_token: str, chatgpt_cookie: str = "", proxy_url: str = "") -> str:
    token = parse_session_json(access_token) or str(access_token or "").strip()
    cookie = opll_normalize_chatgpt_cookie(chatgpt_cookie)
    if cookie:
        session_data = opll_session_from_cookie(cookie, proxy_url="")
        cookie_token = str(session_data.get("accessToken") or session_data.get("access_token") or "").strip()
        if cookie_token:
            return cookie_token
    return token


def opll_build_chatgpt_session(access_token: str, proxy_url: str = "", chatgpt_cookie: str = "",
                               browser_profile: str = "") -> requests.Session:
    token = parse_session_json(access_token) or str(access_token or "").strip()
    if not token:
        raise RuntimeError("Access Token is required")
    cookie = opll_normalize_chatgpt_cookie(chatgpt_cookie)
    # Match the standalone PIX extractor: if browser cookies are not supplied,
    # keep oai-device-id stable for the same access token. Randomizing this on
    # every approve made ChatGPT approve return blocked/exception much more often.
    device_id = opll_cookie_value(cookie, "oai-did") or str(uuid.uuid5(uuid.NAMESPACE_URL, f"pix-device:{token}"))
    session = opll_new_http_session(
        force_requests=opll_is_local_proxy_url(proxy_url),
        browser_profile=browser_profile,
    )
    profile_headers = opll_browser_profile_headers(getattr(session, "_opll_browser_profile", browser_profile))
    session.headers.update({
        "User-Agent": profile_headers["User-Agent"],
        "Accept": "*/*",
        "Accept-Language": profile_headers["Accept-Language"],
        "Authorization": f"Bearer {token}",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "Content-Type": "application/json",
        "oai-device-id": device_id,
        "oai-language": "en-US",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "Cookie": cookie or f"oai-did={device_id}",
    })
    if profile_headers.get("sec-ch-ua"):
        session.headers.update({
            "sec-ch-ua": profile_headers["sec-ch-ua"],
            "sec-ch-ua-mobile": profile_headers["sec-ch-ua-mobile"],
            "sec-ch-ua-platform": profile_headers["sec-ch-ua-platform"],
        })
    else:
        for key in ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"):
            session.headers.pop(key, None)
    if proxy_url:
        if hasattr(session, "trust_env"):
            session.trust_env = False
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session




def opll_normalize_public_ip(value) -> str:
    raw = str(value or "").strip().strip('"\'[](){}<>')
    if not raw:
        return ""
    # Accept common JSON/text echo formats before validating.
    match = re.search(r"(?<![0-9A-Fa-f:.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9A-Fa-f:.])", raw)
    if match:
        raw = match.group(0)
    else:
        match = re.search(r"(?<![0-9A-Fa-f:.])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:.])", raw)
        if match:
            raw = match.group(0)
    try:
        addr = ipaddress.ip_address(raw)
    except Exception:
        return ""
    return addr.compressed


def opll_session_public_ip(session: requests.Session, timeout: int = 10) -> str:
    """Resolve the public egress IP seen by a requests-like session/proxy."""
    endpoints = (
        "https://api.ipify.org?format=json",
        "https://ifconfig.co/json",
        "https://icanhazip.com/",
        "https://checkip.amazonaws.com/",
    )
    for url in endpoints:
        try:
            response = session.get(
                url,
                headers={"Accept": "application/json,text/plain,*/*"},
                timeout=max(3, min(int(timeout or 10), 15)),
            )
            if int(getattr(response, "status_code", 0) or 0) >= 400:
                continue
            text = str(getattr(response, "text", "") or "").strip()
            payload = None
            if text.startswith("{"):
                try:
                    payload = response.json()
                except Exception:
                    payload = None
            if isinstance(payload, dict):
                for key in ("ip", "query", "origin", "ip_address", "ipAddress"):
                    ip = opll_normalize_public_ip(payload.get(key))
                    if ip:
                        return ip
            ip = opll_normalize_public_ip(text)
            if ip:
                return ip
        except Exception:
            continue
    return ""


def opll_customer_acceptance_ip(stripe: requests.Session, ctx: dict | None = None,
                                checkout: dict | None = None, billing: dict | None = None) -> str:
    ctx = ctx or {}
    checkout = checkout or {}
    billing = billing or {}
    for source in (ctx, checkout, billing):
        if not isinstance(source, dict):
            continue
        for key in (
            "customer_acceptance_ip", "checkout_exit_ip", "stripe_exit_ip",
            "public_ip", "exit_ip", "ip", "ip_address",
        ):
            ip = opll_normalize_public_ip(source.get(key))
            if ip:
                return ip
    return opll_session_public_ip(stripe)


def opll_build_stripe_session(proxy_url: str = "", browser_profile: str = "") -> requests.Session:
    session = opll_new_http_session(
        force_requests=opll_is_local_proxy_url(proxy_url),
        browser_profile=browser_profile,
    )
    profile_headers = opll_browser_profile_headers(getattr(session, "_opll_browser_profile", browser_profile))
    session.headers.update({
        "User-Agent": profile_headers["User-Agent"],
        "Accept-Language": profile_headers["Accept-Language"],
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    })
    if proxy_url:
        if hasattr(session, "trust_env"):
            session.trust_env = False
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session


# ===================================================================
# Checkout creation (OpenAI backend)
# ===================================================================

def opll_extract_processor_entity(data) -> str:
    if not isinstance(data, dict):
        return ""
    direct = data.get("processor_entity") or data.get("processorEntity")
    if direct:
        return str(direct).strip()
    for key in ("checkout_session", "session", "checkout", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            found = opll_extract_processor_entity(nested)
            if found:
                return found
    return ""


def opll_extract_stripe_publishable_key(data) -> str:
    if isinstance(data, str):
        match = re.search(r"pk_live_[A-Za-z0-9]+", data)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in ("stripe_publishable_key", "publishable_key", "publishableKey",
                     "stripePublishableKey", "key"):
            found = opll_extract_stripe_publishable_key(data.get(key))
            if found:
                return found
        for item in data.values():
            found = opll_extract_stripe_publishable_key(item)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = opll_extract_stripe_publishable_key(item)
            if found:
                return found
    return ""


def opll_extract_checkout_id(data) -> str:
    """Extract either the newer OpenAI checkout id (oaics_*) or Stripe cs_* id."""
    if isinstance(data, str):
        match = re.search(r"\b(?:oaics|cs_(?:live|test))_[A-Za-z0-9]+", data)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in (
            "checkout_session_id",
            "checkoutSessionId",
            "checkout_id",
            "checkoutId",
            "checkout_session",
            "checkoutSession",
            "session_id",
            "sessionId",
            "id",
        ):
            found = opll_extract_checkout_id(data.get(key))
            if found:
                return found
        for item in data.values():
            found = opll_extract_checkout_id(item)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = opll_extract_checkout_id(item)
            if found:
                return found
    return ""


def opll_extract_stripe_checkout_id(data) -> str:
    """Extract only a Stripe cs_live_/cs_test_ id, ignoring OpenAI oaics_ ids."""
    if isinstance(data, str):
        match = re.search(r"\bcs_(?:live|test)_[A-Za-z0-9]+", data)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in (
            "stripe_session_id",
            "stripeSessionId",
            "stripe_checkout_session_id",
            "stripeCheckoutSessionId",
            "checkout_session_id",
            "checkoutSessionId",
            "session_id",
            "sessionId",
            "id",
        ):
            found = opll_extract_stripe_checkout_id(data.get(key))
            if found:
                return found
        for item in data.values():
            found = opll_extract_stripe_checkout_id(item)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = opll_extract_stripe_checkout_id(item)
            if found:
                return found
    return ""


def opll_extract_chatgpt_checkout_url(data) -> str:
    if isinstance(data, str):
        text = data.strip()
        if "chatgpt.com/checkout/" in text and re.search(r"/(?:oaics|cs_(?:live|test))_[A-Za-z0-9]+", text):
            match = re.search(r"https://chatgpt\.com/checkout/[^\s\"'<>]+", text)
            return match.group(0) if match else text
        return ""
    if isinstance(data, dict):
        for key in ("url", "checkout_url", "checkoutUrl", "chatgpt_checkout_url", "chatgptCheckoutUrl"):
            found = opll_extract_chatgpt_checkout_url(data.get(key))
            if found:
                return found
        for item in data.values():
            found = opll_extract_chatgpt_checkout_url(item)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = opll_extract_chatgpt_checkout_url(item)
            if found:
                return found
    return ""


def opll_processor_entity_for_country(country: str, processor_entity: str = "") -> str:
    entity = str(processor_entity or "").strip()
    if entity:
        return entity
    return "openai_llc" if str(country or "").upper() == "US" else "openai_ie"


def opll_chatgpt_success_return_url(cs_id: str, country: str, processor_entity: str = "") -> str:
    entity = opll_processor_entity_for_country(country, processor_entity)
    return f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={entity}&plan_type=plus"


def opll_chatgpt_checkout_url(checkout_id: str, processor_entity: str = "openai_llc") -> str:
    checkout_id = str(checkout_id or "").strip()
    if not checkout_id:
        return ""
    entity = str(processor_entity or "").strip() or "openai_llc"
    return f"https://chatgpt.com/checkout/{entity}/{checkout_id}"


def opll_checkout_billing_details(country: str, currency: str,
                                  billing_profile: dict | None = None) -> dict:
    """Build checkout billing_details while preserving the legacy two-field shape."""
    country = normalize_opll_country(country)
    details = {"country": country, "currency": str(currency or "").upper()}
    profile = dict(billing_profile or {})
    if not profile:
        return details
    address = {
        "line1": str(profile.get("line1") or ""),
        "line2": str(profile.get("line2") or ""),
        "city": str(profile.get("city") or ""),
        "state": str(profile.get("state") or ""),
        "postal_code": str(profile.get("postal_code") or ""),
        "country": country,
    }
    details.update({
        "name": str(profile.get("name") or ""),
        "email": str(profile.get("email") or ""),
        "phone": str(profile.get("phone") or ""),
        "address": address,
        "tax_id": str(profile.get("tax_id") or ""),
    })
    return details


def opll_checkout_page_target_headers(entity: str, checkout_session_id: str) -> dict:
    """Headers used by browser-origin checkout page actions."""
    entity = str(entity or "").strip() or "openai_llc"
    cs_id = str(checkout_session_id or "").strip()
    return {
        "Referer": f"https://chatgpt.com/checkout/{entity}/{cs_id}",
        "x-openai-target-path": f"/checkout/{entity}/{cs_id}",
        "x-openai-target-route": "/checkout/[processorEntity]/[checkoutSessionId]",
    }


def opll_checkout_profile_fields(billing_profile: dict | None,
                                 country: str,
                                 currency: str) -> dict:
    """Return top-level checkout fields that mirror the visible billing form."""
    profile = dict(billing_profile or {})
    if not profile:
        return {}
    details = opll_checkout_billing_details(country, currency, profile)
    address = dict(details.get("address") or {})
    return {
        "checkout_email": str(profile.get("email") or ""),
        "customer_email": str(profile.get("email") or ""),
        "billing_email": str(profile.get("email") or ""),
        "billing_country": normalize_opll_country(country),
        "billing_name": str(profile.get("name") or ""),
        "customer_name": str(profile.get("name") or ""),
        "billing_phone": str(profile.get("phone") or ""),
        "customer_phone": str(profile.get("phone") or ""),
        "currency": str(currency or "").upper(),
        "tax_id": str(profile.get("tax_id") or ""),
        "billing_address": address,
        "billing_details": details,
    }


def opll_create_checkout(access_token: str, country: str, currency: str, proxy_url: str = "",
                         checkout_ui_mode: str = "custom",
                         require_stripe_session: bool = True,
                         preferred_processor_entity: str = "",
                         promo_campaign_id: str | None = "plus-1-month-free",
                         billing_profile: dict | None = None,
                         hosted_payload_contract: bool = False,
                         extra_payload: dict | None = None,
                         allow_openai_checkout_session: bool = False,
                         chatgpt_cookie: str = "",
                         browser_profile: str = "") -> dict:
    country = normalize_opll_country(country)
    requested_currency = str(currency or "").strip().upper() or currency_for_country(country)
    if requested_currency in OPENAI_CHECKOUT_ALLOWED_CURRENCIES:
        currency = requested_currency
    else:
        currency, _local_currency, _currency_fallback = checkout_currency_for_country(country)
    checkout_ui_mode = str(checkout_ui_mode or "custom").strip() or "custom"
    payload = {
        "plan_name": "chatgptplusplan",
        "billing_details": opll_checkout_billing_details(
            country, currency, billing_profile,
        ),
        "checkout_ui_mode": checkout_ui_mode,
    }
    # The current hosted checkout contract differs from the custom checkout
    # contract.  Keeping entry_point on a hosted request can make the backend
    # normalize it back to a custom OpenAI session (oaics_*).  A genuine hosted
    # request uses cancel_url and returns the Stripe cs_* needed by payment_pages.
    if hosted_payload_contract and checkout_ui_mode == "hosted":
        payload["cancel_url"] = "https://chatgpt.com/#pricing"
    else:
        payload["entry_point"] = "all_plans_pricing_modal"
    if preferred_processor_entity:
        payload["processor_entity"] = str(preferred_processor_entity).strip()
    if billing_profile:
        payload.update(opll_checkout_profile_fields(billing_profile, country, currency))
    if promo_campaign_id is not None:
        promo = str(promo_campaign_id or "").strip()
        if promo:
            payload["promo_campaign"] = {
                "promo_campaign_id": promo,
                "is_coupon_from_query_param": False,
            }
    if extra_payload:
        payload.update(dict(extra_payload))
    session = opll_build_chatgpt_session(
        access_token,
        proxy_url,
        chatgpt_cookie=chatgpt_cookie,
        browser_profile=browser_profile,
    )
    response = session.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=payload,
        headers={
            "Referer": "https://chatgpt.com/",
            "x-openai-target-path": "/backend-api/payments/checkout",
            "x-openai-target-route": "/backend-api/payments/checkout",
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"checkout create failed: {opll_http_error_detail(response)}")
    data = response.json() or {}
    checkout_id = opll_extract_checkout_id(data)
    if not checkout_id:
        raise RuntimeError(f"checkout response missing checkout id: {str(data)[:500]}")
    if require_stripe_session and not str(checkout_id).startswith("cs_"):
        if not (allow_openai_checkout_session and str(checkout_id).startswith("oaics_")):
            raise RuntimeError(f"checkout response missing cs_id: {str(data)[:500]}")
    if str(checkout_id).startswith("oaics_"):
        session_kind = "openai_custom_checkout"
    elif str(checkout_id).startswith("cs_"):
        session_kind = "stripe_checkout"
    else:
        session_kind = "unknown_checkout"
    processor_entity = opll_extract_processor_entity(data) or str(preferred_processor_entity or "").strip()
    return {
        "cs_id": str(checkout_id),
        "checkout_id": str(checkout_id),
        "checkout_session_id": str(checkout_id),
        "session_kind": session_kind,
        "processor_entity": processor_entity,
        "stripe_publishable_key": opll_extract_stripe_publishable_key(data),
        "billing_country": country,
        "currency": currency,
        "checkout_ui_mode": checkout_ui_mode,
        "browser_profile": getattr(session, "_opll_browser_profile", opll_normalize_browser_profile(browser_profile)),
        "_checkout_billing_profile": dict(billing_profile or {}),
        "raw_checkout": data,
    }


def opll_chatgpt_checkout_update_promotion(access_token: str, checkout: dict, proxy_url: str = "",
                                            chatgpt_cookie: str = "", normalize_vn: bool = True,
                                            billing_profile: dict | None = None,
                                            checkout_page_route: bool = False,
                                            include_full_profile: bool = True,
                                            include_promo: bool = True,
                                            checkout_ui_mode: str = "custom",
                                            extra_payload: dict | None = None,
                                            browser_profile: str = "") -> dict:
    cs_id = str((checkout or {}).get("cs_id") or (checkout or {}).get("checkout_id") or "").strip()
    entity = str((checkout or {}).get("processor_entity") or opll_processor_entity_for_country("NL")).strip()
    billing_country = str((checkout or {}).get("billing_country") or "BR").strip().upper() or "BR"
    billing_currency = str((checkout or {}).get("currency") or currency_for_country(billing_country)).strip().upper()
    if not cs_id:
        raise RuntimeError("checkout/update missing checkout_session_id")
    if normalize_vn:
        proxy_url = opll_normalize_vn_country_proxy(proxy_url)
    session = opll_build_chatgpt_session(
        access_token,
        proxy_url,
        chatgpt_cookie=chatgpt_cookie,
        browser_profile=browser_profile or str((checkout or {}).get("browser_profile") or ""),
    )
    referer = f"https://chatgpt.com/checkout/{entity}/{cs_id}"
    profile = (
        billing_profile or (checkout or {}).get("_checkout_billing_profile")
        if include_full_profile
        else None
    )
    payload = {
        "checkout_session_id": cs_id,
        "processor_entity": entity,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "billing_details": {
            **opll_checkout_billing_details(
                billing_country,
                billing_currency,
                profile,
            ),
        },
        "checkout_ui_mode": str(checkout_ui_mode or "custom").strip() or "custom",
    }
    if include_promo:
        payload["promo_campaign"] = {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }
    if profile:
        payload.update(opll_checkout_profile_fields(profile, billing_country, billing_currency))
    if extra_payload:
        payload.update(dict(extra_payload))
    headers = (
        opll_checkout_page_target_headers(entity, cs_id)
        if checkout_page_route
        else {
            "Referer": referer,
            "x-openai-target-path": "/backend-api/payments/checkout/update",
            "x-openai-target-route": "/backend-api/payments/checkout/update",
        }
    )
    response = session.post(
        "https://chatgpt.com/backend-api/payments/checkout/update",
        json=payload,
        headers=headers,
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(opll_short_error(
            f"checkout/update promotion failed: {opll_http_error_detail(response, 800)}",
            280,
        ))
    try:
        payload = response.json() or {}
    except Exception:
        payload = {"raw": response.text[:500]}
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(f"checkout/update promotion rejected: {payload}")
    return payload if isinstance(payload, dict) else {"payload": payload}


def opll_chatgpt_checkout_sync_billing(access_token: str, checkout: dict, proxy_url: str = "",
                                       billing_profile: dict | None = None,
                                       chatgpt_cookie: str = "") -> dict:
    """Push the generated local billing profile to the checkout on its home route."""
    return opll_chatgpt_checkout_update_promotion(
        access_token,
        checkout,
        proxy_url,
        chatgpt_cookie=chatgpt_cookie,
        normalize_vn=False,
        billing_profile=billing_profile or (checkout or {}).get("_checkout_billing_profile"),
        checkout_page_route=False,
        include_full_profile=True,
        include_promo=False,
        checkout_ui_mode=str((checkout or {}).get("checkout_ui_mode") or "hosted"),
    )


def opll_chatgpt_checkout_update_taxes(access_token: str, checkout: dict, proxy_url: str = "",
                                       billing: dict | None = None, currency: str = "",
                                       chatgpt_cookie: str = "",
                                       browser_profile: str = "") -> dict:
    """Generic OAICS checkout/taxes sync for a user-selected billing profile."""
    checkout_id = str((checkout or {}).get("checkout_session_id") or
                      (checkout or {}).get("checkout_id") or
                      (checkout or {}).get("cs_id") or "").strip()
    entity = str((checkout or {}).get("processor_entity") or
                 opll_processor_entity_for_country((checkout or {}).get("billing_country") or "DE")).strip()
    if not checkout_id:
        raise RuntimeError("checkout/taxes missing checkout_session_id")
    billing = dict(billing or {})
    billing_country = normalize_opll_country(billing.get("country") or (checkout or {}).get("billing_country") or "DE")
    billing_currency = str(currency or (checkout or {}).get("currency") or currency_for_country(billing_country)).strip().upper()
    address = {
        "line1": str(billing.get("line1") or ""),
        "city": str(billing.get("city") or ""),
        "country": billing_country,
        "postal_code": str(billing.get("postal_code") or ""),
        "state": str(billing.get("state") or ""),
    }
    if str(billing.get("line2") or "").strip():
        address["line2"] = str(billing.get("line2")).strip()
    payload = {
        "checkout_session_id": checkout_id,
        "checkout_email": str(billing.get("email") or "buyer@example.com"),
        "customer_email": str(billing.get("email") or "buyer@example.com"),
        "billing_email": str(billing.get("email") or "buyer@example.com"),
        "billing_country": billing_country,
        "billing_name": str(billing.get("name") or "PayPal Buyer"),
        "customer_name": str(billing.get("name") or "PayPal Buyer"),
        "billing_phone": str(billing.get("phone") or ""),
        "customer_phone": str(billing.get("phone") or ""),
        "currency": billing_currency,
        "tax_id": str(billing.get("tax_id") or ""),
        "processor_entity": entity,
        "billing_address": address,
        "customer_address": address,
        "billing_details": opll_checkout_billing_details(billing_country, billing_currency, billing),
    }
    session = opll_build_chatgpt_session(
        access_token,
        proxy_url,
        chatgpt_cookie=chatgpt_cookie,
        browser_profile=browser_profile or str((checkout or {}).get("browser_profile") or ""),
    )
    response = session.post(
        "https://chatgpt.com/backend-api/payments/checkout/taxes",
        json=payload,
        headers={
            "Referer": f"https://chatgpt.com/checkout/{entity}/{checkout_id}",
            "x-openai-target-path": "/backend-api/payments/checkout/taxes",
            "x-openai-target-route": "/backend-api/payments/checkout/taxes",
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"checkout/taxes failed: {opll_http_error_detail(response)}")
    try:
        data = response.json() or {}
    except Exception:
        data = {"raw": response.text[:500]}
    if isinstance(data, dict):
        data["_billing_country_sent"] = billing_country
        data["_billing_email_sent"] = str(billing.get("email") or "")
    return data if isinstance(data, dict) else {"payload": data}


def opll_chatgpt_update_ideal_taxes(access_token: str, checkout: dict, proxy_url: str = "",
                                    email: str = "buyer@example.com", chatgpt_cookie: str = "",
                                    billing: dict | None = None) -> dict:
    cs_id = str((checkout or {}).get("cs_id") or (checkout or {}).get("checkout_id") or "").strip()
    entity = str((checkout or {}).get("processor_entity") or opll_processor_entity_for_country("NL")).strip()
    if not cs_id:
        raise RuntimeError("checkout/taxes missing checkout_session_id")
    billing = dict(billing or {})
    billing_email = str(email or billing.get("email") or "buyer@example.com").strip()
    billing_name = str(billing.get("name") or "Ideal User").strip()
    billing_address = {
        "line1": str(billing.get("line1") or "Herengracht 420"),
        "city": str(billing.get("city") or "Amsterdam"),
        "country": "NL",
        "postal_code": str(billing.get("postal_code") or "1016 GV"),
    }
    if str(billing.get("line2") or "").strip():
        billing_address["line2"] = str(billing.get("line2")).strip()
    if str(billing.get("state") or "").strip():
        billing_address["state"] = str(billing.get("state")).strip()
    session = opll_build_chatgpt_session(access_token, proxy_url, chatgpt_cookie=chatgpt_cookie)
    referer = f"https://chatgpt.com/checkout/{entity}/{cs_id}"
    response = session.post(
        "https://chatgpt.com/backend-api/payments/checkout/taxes",
        json={
            "checkout_session_id": cs_id,
            "checkout_email": billing_email,
            "billing_country": "NL",
            "billing_name": billing_name,
            "currency": "EUR",
            "tax_id": None,
            "processor_entity": entity,
            "billing_address": billing_address,
        },
        headers={
            "Referer": referer,
            "x-openai-target-path": "/backend-api/payments/checkout/taxes",
            "x-openai-target-route": "/backend-api/payments/checkout/taxes",
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"checkout/taxes failed: {opll_http_error_detail(response)}")
    try:
        return response.json() or {}
    except Exception:
        return {"raw": response.text[:500]}


def opll_chatgpt_update_pix_taxes(access_token: str, checkout: dict, proxy_url: str = "",
                                  billing: dict | None = None, chatgpt_cookie: str = "") -> dict:
    cs_id = str((checkout or {}).get("cs_id") or (checkout or {}).get("checkout_id") or "").strip()
    entity = str((checkout or {}).get("processor_entity") or opll_processor_entity_for_country("BR")).strip()
    if not cs_id:
        raise RuntimeError("PIX checkout/taxes missing checkout_session_id")
    billing = dict(billing or opll_brazil_pix_billing(access_token))
    email = str(billing.get("email") or "buyer@example.com")
    address = {
        "line1": str(billing.get("line1") or "Avenida Paulista 300"),
        "line2": str(billing.get("line2") or billing.get("neighborhood") or ""),
        "city": str(billing.get("city") or "Sao Paulo"),
        "country": "BR",
        "postal_code": str(billing.get("postal_code") or "01311-000"),
        "state": str(billing.get("state") or "SP"),
    }
    session = opll_build_chatgpt_session(access_token, proxy_url, chatgpt_cookie=chatgpt_cookie)
    payload = {
        "checkout_session_id": cs_id,
        "checkout_email": email,
        "customer_email": email,
        "billing_email": email,
        "billing_country": "BR",
        "billing_name": str(billing.get("name") or "Pix User"),
        "customer_name": str(billing.get("name") or "Pix User"),
        "billing_phone": str(billing.get("phone") or ""),
        "customer_phone": str(billing.get("phone") or ""),
        "currency": "BRL",
        "tax_id": str(billing.get("tax_id") or ""),
        "processor_entity": entity,
        "billing_address": address,
        "customer_address": address,
        "billing_details": {
            "email": email,
            "name": str(billing.get("name") or "Pix User"),
            "phone": str(billing.get("phone") or ""),
            "country": "BR",
            "currency": "BRL",
            "address": address,
            "tax_id": str(billing.get("tax_id") or ""),
        },
    }
    backend_headers = {
        "Referer": f"https://chatgpt.com/checkout/{entity}/{cs_id}",
        "x-openai-target-path": "/backend-api/payments/checkout/taxes",
        "x-openai-target-route": "/backend-api/payments/checkout/taxes",
    }
    response = session.post(
        "https://chatgpt.com/backend-api/payments/checkout/taxes",
        json=payload,
        headers=backend_headers,
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        fallback = session.post(
            "https://chatgpt.com/backend-api/payments/checkout/taxes",
            json=payload,
            headers=opll_checkout_page_target_headers(entity, cs_id),
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
        if fallback.status_code < response.status_code or fallback.status_code < 400:
            response = fallback
    if response.status_code >= 400:
        raise RuntimeError(f"PIX checkout/taxes failed: HTTP {response.status_code} {response.text[:500]}")
    try:
        data = response.json() or {}
        if isinstance(data, dict):
            data["_billing_country_sent"] = "BR"
            data["_billing_email_sent"] = email
        return data
    except Exception:
        return {"raw": response.text[:500]}


# ===================================================================
# Stripe operations
# ===================================================================

def opll_stripe_key_for_checkout(checkout: dict | None = None) -> str:
    return str((checkout or {}).get("stripe_publishable_key") or "").strip() or DEFAULT_STRIPE_PK


def opll_stripe_init(cs_id: str, country: str, currency: str,
                     proxy_url: str = "", payment_locale: str = "en",
                     stripe: requests.Session | None = None,
                     ctx: dict | None = None,
                     checkout: dict | None = None,
                     browser_timezone: str = "Asia/Shanghai",
                     saved_payment_method_mode: str = "never") -> dict:
    browser_locale, elements_locale = locale_parts(payment_locale)
    saved_mode = str(saved_payment_method_mode or "never").strip() or "never"
    stripe_pk = opll_stripe_key_for_checkout(checkout)
    stripe_session = stripe or opll_build_stripe_session(proxy_url)
    is_pix_init = str(country or "").upper() == "BR" and str(currency or "").upper() == "BRL" and elements_locale.lower().startswith("pt")
    stripe_version = str((ctx or {}).get("stripe_version") or (PIX_STRIPE_VERSION_FULL if is_pix_init else STRIPE_VERSION_FULL))
    if stripe is None:
        stripe_session.headers.update({"User-Agent": DEFAULT_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
        if hasattr(stripe_session, "trust_env"):
            stripe_session.trust_env = False
        if proxy_url:
            stripe_session.proxies.update({"http": proxy_url, "https": proxy_url})
    body = {
        "browser_locale": browser_locale,
        "browser_timezone": str(browser_timezone or "Asia/Shanghai"),
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str((ctx or {}).get("stripe_js_id") or uuid.uuid4()),
        "elements_session_client[locale]": elements_locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "key": stripe_pk,
        "_stripe_version": stripe_version,
    }
    if is_pix_init:
        # Current Stripe payment_pages init endpoint expects POST here. The
        # standalone PIX flow keeps saved-payment-method explicitly at "never";
        # preserving that makes the BR-only path closer to the real page state.
        body["eid"] = "NA"
        body["elements_options_client[saved_payment_method][enable_save]"] = saved_mode
        body["elements_options_client[saved_payment_method][enable_redisplay]"] = saved_mode
        response = stripe_session.post(
            f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
            data=body,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
    else:
        body["elements_options_client[saved_payment_method][enable_save]"] = saved_mode
        body["elements_options_client[saved_payment_method][enable_redisplay]"] = saved_mode
        response = stripe_session.post(
            f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
            data=body,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"stripe init failed: {opll_http_error_detail(response)}")
    return response.json() or {}


def opll_stripe_context(init_payload: dict, payment_locale: str = "en", ctx: dict | None = None) -> dict:
    _browser_locale, elements_locale = locale_parts(payment_locale)
    base = ctx or {}
    session_id = (
        init_payload.get("session_id")
        or opll_get_nested(init_payload, ("elements_session", "id"))
        or opll_get_nested(init_payload, ("session", "id"))
        or base.get("elements_session_id")
        or f"elements_session_{uuid.uuid4().hex[:11]}"
    )
    return {
        "stripe_js_id": str(base.get("stripe_js_id") or uuid.uuid4()),
        "elements_session_id": str(session_id),
        "elements_session_config_id": str(init_payload.get("config_id") or base.get("elements_session_config_id") or uuid.uuid4()),
        "config_id": str(init_payload.get("config_id") or ""),
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "checkout_amount": str(opll_expected_amount(init_payload)),
        "currency": str(init_payload.get("currency") or "").lower(),
        "locale": elements_locale,
        "runtime_version": str(base.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION),
        "stripe_version": str(base.get("stripe_version") or STRIPE_VERSION_FULL),
        "browser_timezone": str(base.get("browser_timezone") or "Asia/Shanghai"),
        "browser_profile": str(base.get("browser_profile") or ""),
        "browser_user_agent": str(base.get("browser_user_agent") or DEFAULT_USER_AGENT),
        "saved_payment_method_mode": str(base.get("saved_payment_method_mode") or "never"),
        "guid": str(base.get("guid") or uuid.uuid4()),
        "muid": str(base.get("muid") or uuid.uuid4()),
        "sid": str(base.get("sid") or uuid.uuid4()),
    }


def opll_stripe_update_tax_region(stripe: requests.Session, cs_id: str, stripe_pk: str,
                                  ctx: dict, billing: dict,
                                  payment_locale: str = "en",
                                  browser_timezone: str = "Asia/Shanghai",
                                  saved_payment_method_mode: str = "auto") -> dict:
    """POST a tax_region/billing-region update to Stripe payment_pages.

    This mirrors the high-success UPI path. For local payment methods (iDEAL,
    UPI, Kakao, etc.) Stripe sometimes exposes the method only after the page is
    updated with the local tax/billing region, even if the first init geocodes
    to the right country.
    """
    _browser_locale, elements_locale = locale_parts(payment_locale)
    saved_mode = str(saved_payment_method_mode or "auto").strip() or "auto"
    body = {
        "tax_region[country]": billing.get("country") or "",
        "tax_region[postal_code]": billing.get("postal_code") or "",
        "tax_region[state]": billing.get("state") or "",
        "tax_region[city]": billing.get("city") or "",
        "tax_region[line1]": billing.get("line1") or "",
        "key": stripe_pk,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
        "browser_locale": _browser_locale,
        "browser_timezone": str(browser_timezone or "Asia/Shanghai"),
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or uuid.uuid4()),
        "elements_session_client[locale]": elements_locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": saved_mode,
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_mode,
    }
    if billing.get("line2"):
        body["tax_region[line2]"] = billing.get("line2") or ""
    if ctx.get("elements_session_id"):
        body["elements_session_client[session_id]"] = str(ctx.get("elements_session_id"))

    def post(payload: dict):
        return stripe.post(
            f"https://api.stripe.com/v1/payment_pages/{cs_id}",
            data=payload,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )

    removed_unknown_params: list[str] = []
    response = post(body)
    for _ in range(8):
        if response.status_code < 400:
            break
        try:
            error = (response.json() or {}).get("error") or {}
        except Exception:
            error = {}
        unknown = str(error.get("param") or "").strip() if isinstance(error, dict) and error.get("code") == "parameter_unknown" else ""
        if not unknown or unknown not in body:
            break
        removed_unknown_params.append(unknown)
        body.pop(unknown, None)
        response = post(body)
    if response.status_code >= 400:
        removed_hint = f"; removed_unknown_params={removed_unknown_params}" if removed_unknown_params else ""
        raise RuntimeError(f"stripe tax region update failed: HTTP {response.status_code} {response.text[:500]}{removed_hint}")
    payload = response.json() or {}
    if isinstance(payload, dict) and removed_unknown_params:
        payload["_removed_unknown_params"] = removed_unknown_params
    return payload


def opll_expected_amount(init_payload: dict) -> str:
    return opll_stripe_amount_info(init_payload)[0]


def opll_flatten_to_stripe_params(data, prefix: str = "") -> dict:
    """Flatten nested Stripe params into bracket notation."""
    params: dict[str, str] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            sub_prefix = f"{prefix}[{key}]" if prefix else str(key)
            params.update(opll_flatten_to_stripe_params(value, sub_prefix))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            sub_prefix = f"{prefix}[{index}]"
            params.update(opll_flatten_to_stripe_params(value, sub_prefix))
    elif data is not None and prefix:
        if isinstance(data, bool):
            params[prefix] = "true" if data else "false"
        else:
            params[prefix] = str(data)
    return params


def opll_stripe_amount_info(init_payload) -> tuple[str, str]:
    if not isinstance(init_payload, dict):
        return "0", "missing_payload"
    if init_payload.get("amount_due_minor") is not None:
        return str(init_payload.get("amount_due_minor")), "amount_due_minor"
    if init_payload.get("amount_due") is not None:
        return str(init_payload.get("amount_due")), "amount_due"
    total_summary = init_payload.get("total_summary") if isinstance(init_payload, dict) else None
    if isinstance(total_summary, dict) and total_summary.get("due") is not None:
        return str(total_summary.get("due")), "total_summary.due"
    invoice = init_payload.get("invoice") if isinstance(init_payload, dict) else None
    if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
        return str(invoice.get("amount_due")), "invoice.amount_due"
    line_items = init_payload.get("line_items") if isinstance(init_payload, dict) else None
    if isinstance(line_items, list):
        total = 0
        found = False
        for item in line_items:
            if isinstance(item, dict) and item.get("amount") is not None:
                try:
                    total += int(item.get("amount") or 0)
                    found = True
                except Exception:
                    pass
        if found:
            return str(total), "line_items.amount"
    elements_session = init_payload.get("elements_session")
    if isinstance(elements_session, dict):
        if elements_session.get("amount") is not None:
            return str(elements_session.get("amount")), "elements_session.amount"
        pi = elements_session.get("payment_intent")
        if isinstance(pi, dict) and pi.get("amount") is not None:
            return str(pi.get("amount")), "elements_session.payment_intent.amount"
    pi = init_payload.get("payment_intent")
    if isinstance(pi, dict) and pi.get("amount") is not None:
        return str(pi.get("amount")), "payment_intent.amount"
    return "0", "fallback_zero"


def opll_find_first_value(value, keys: set[str]):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in keys and item not in (None, ""):
                return item
        for item in value.values():
            found = opll_find_first_value(item, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = opll_find_first_value(item, keys)
            if found not in (None, ""):
                return found
    return None


def opll_parse_epoch_seconds(value) -> int:
    try:
        if value is None or value == "":
            return 0
        text = str(value).strip()
        if not text:
            return 0
        number = float(text)
        if number > 100000000000:
            number = number / 1000
        return int(number)
    except Exception:
        return 0


def opll_checkout_expires_at(*payloads) -> tuple[int, str]:
    keys = {
        "expires_at", "expiresAt", "expires", "expires_on", "expiresOn",
        "expires_at_utc", "expiration", "expiration_time", "expirationTime",
    }
    for payload in payloads:
        value = opll_find_first_value(payload, keys)
        epoch = opll_parse_epoch_seconds(value)
        if epoch:
            return epoch, str(value)
    return 0, ""


def opll_format_minor_amount(amount, currency: str) -> str:
    text = str(amount or "").strip()
    code = str(currency or "").upper()
    if not text:
        return f"- {code}".strip()
    zero_decimal = {
        "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW",
        "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
    }
    try:
        value = int(float(text))
        if code in zero_decimal:
            return f"{value} {code}".strip()
        return f"{value / 100:.2f} {code}".strip()
    except Exception:
        return f"{text} {code}".strip()


def opll_payload_contains_word(value, needle: str) -> bool:
    needle = str(needle or "").strip().lower()
    if not needle:
        return False
    if isinstance(value, dict):
        for key, item in value.items():
            if needle in str(key).lower() or opll_payload_contains_word(item, needle):
                return True
    elif isinstance(value, list):
        return any(opll_payload_contains_word(item, needle) for item in value)
    elif isinstance(value, str):
        return needle in value.lower()
    return False


def opll_collect_payment_method_types(payload) -> list[str]:
    """Collect explicitly exposed Stripe payment method types from init payload.

    Do not use a blind substring search for local payment methods here. Recent
    Stripe init payloads can contain method names in localization/config blobs
    even when the Checkout Session does not actually allow that method; confirm
    then fails with payment_method_types_mismatch.
    """
    methods: set[str] = set()

    def add(value) -> None:
        text = str(value or "").strip().lower()
        if text:
            methods.add(text)

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                if key_text in {
                    "payment_method_types",
                    "ordered_payment_method_types",
                }:
                    if isinstance(item, list):
                        for entry in item:
                            add(entry)
                    else:
                        add(item)
                elif key_text == "payment_method_specs" and isinstance(item, list):
                    for spec in item:
                        if isinstance(spec, dict):
                            add(spec.get("type"))
                            add(spec.get("payment_method_type"))
                elif key_text == "payment_method_options" and isinstance(item, dict):
                    for method_key in item.keys():
                        add(method_key)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return sorted(methods)


def opll_payment_method_available(init_payload, method: str) -> bool:
    method = str(method or "").strip().lower()
    if not method:
        return False
    exposed = opll_collect_payment_method_types(init_payload)
    if exposed:
        return method in exposed
    return opll_payload_contains_word(init_payload, method)


def opll_payment_method_diagnostics(init_payload) -> str:
    if not isinstance(init_payload, dict):
        return f"payload_type={type(init_payload).__name__}"
    specs = []
    for item in init_payload.get("payment_method_specs") or []:
        if isinstance(item, dict) and item.get("type"):
            specs.append(str(item.get("type")))
    return (
        f"payment_method_types={init_payload.get('payment_method_types')}, "
        f"ordered_payment_method_types={init_payload.get('ordered_payment_method_types')}, "
        f"payment_method_specs={specs[:12]}, "
        f"currency={init_payload.get('currency')}, "
        f"geocoding={init_payload.get('geocoding')}"
    )


def opll_legacy_zip_payment_method_diagnostics(init_payload, method: str) -> str:
    """Report how the 2026-07-10 ZIP's legacy detector would classify a method.

    The ZIP version used opll_payload_contains_word(init_payload, method). That
    is kept here only as a compatibility diagnostic, while the actual confirm
    gate remains the explicit Stripe payment method list to avoid mismatch
    errors when payment_method_types is ['card', 'link'].
    """
    method = str(method or "").strip().lower()
    legacy_contains = opll_payload_contains_word(init_payload, method) if method else False
    explicit_methods = opll_collect_payment_method_types(init_payload)
    explicit_contains = method in explicit_methods if method else False
    return (
        f"legacy_zip_logic_contains_{method}={legacy_contains}, "
        f"explicit_payment_method_available={explicit_contains}, "
        f"explicit_payment_methods={explicit_methods[:20]}"
    )


def opll_amount_is_zero(amount) -> bool:
    try:
        return int(float(str(amount or "0").strip() or "0")) == 0
    except Exception:
        return False


def opll_make_qr_data_url(data: str) -> str:
    text = str(data or "").strip()
    if not text:
        return ""
    try:
        import qrcode  # type: ignore
        image = qrcode.make(text)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return ""


def opll_extract_upi_qr(payload) -> dict:
    """Compatibility wrapper for the high-success UPI extractor.

    The old fixed-path implementation has been removed. This wrapper now uses
    the recursive gpt-upi-main parser ported in upi_high_success.py.
    """
    if not isinstance(payload, (dict, list)):
        return {}
    try:
        from upi_high_success import extract_upi_qr_compat
        return extract_upi_qr_compat(payload)
    except Exception:
        return {}


# ===================================================================
# Billing info generation
# ===================================================================

IN_PROFILE_LOCATIONS = (
    ("New Delhi", "Delhi", ("110001", "110016", "110019", "110048")),
    ("Mumbai", "Maharashtra", ("400001", "400050", "400053", "400076")),
    ("Bengaluru", "Karnataka", ("560001", "560038", "560066", "560102")),
    ("Hyderabad", "Telangana", ("500001", "500034", "500081", "500084")),
    ("Chennai", "Tamil Nadu", ("600001", "600028", "600040", "600096")),
    ("Kolkata", "West Bengal", ("700001", "700019", "700029", "700091")),
    ("Pune", "Maharashtra", ("411001", "411014", "411038", "411045")),
)
IN_PROFILE_STREETS = (
    "Mahatma Gandhi Road", "Park Street", "Link Road", "Residency Road",
    "Lake View Road", "Station Road", "Nehru Road", "Temple Road",
    "Market Road", "Green Park",
)
IN_PROFILE_FIRST_NAMES = (
    "Aarav", "Arjun", "Rohan", "Vikram", "Aditya", "Kabir", "Ananya",
    "Diya", "Isha", "Kavya", "Meera", "Priya", "Sneha", "Aditi",
)
IN_PROFILE_LAST_NAMES = (
    "Sharma", "Patel", "Singh", "Reddy", "Gupta", "Mehta", "Nair",
    "Iyer", "Kumar", "Das", "Joshi", "Kapoor", "Rao", "Verma",
)

# Keep this pool identical to “资料生成 → 泰国” in static/js/sff_core.js.
TH_PROFILE_LOCATIONS = (
    ("Bangkok", "Bangkok", ("10110", "10240", "10330", "10400")),
    ("Chiang Mai", "Chiang Mai", ("50000", "50100", "50200", "50300")),
    ("Phuket", "Phuket", ("83000", "83100", "83110", "83120")),
    ("Khon Kaen", "Khon Kaen", ("40000", "40100", "40260", "40320")),
    ("Pattaya", "Chonburi", ("20150", "20160", "20230", "20260")),
    ("Hat Yai", "Songkhla", ("90110", "90112", "90130", "90250")),
    ("Nakhon Ratchasima", "Nakhon Ratchasima", ("30000", "30130", "30210", "30310")),
)
TH_PROFILE_STREETS = (
    "Sukhumvit Road", "Silom Road", "Rama IX Road", "Phetchaburi Road",
    "Phahonyothin Road", "Charoen Krung Road", "Nimmanahaeminda Road",
    "Huay Kaew Road", "Rat-U-Thit Road", "Chalermprakiat Road",
)
TH_PROFILE_FIRST_NAMES = (
    "Anan", "Chai", "Kittisak", "Narin", "Somchai", "Thanawat", "Araya",
    "Benjawan", "Kanya", "Mali", "Nicha", "Pimchanok", "Suda", "Wipada",
)
TH_PROFILE_LAST_NAMES = (
    "Srisuk", "Chantarangsu", "Kittipong", "Saelim", "Wongchai", "Prasert",
    "Rattanakosin", "Sukhum", "Thanasiri", "Boonmee", "Kaewmanee",
    "Panyasiri", "Suwan", "Thongchai",
)

# Keep this pool identical to “资料生成 → 韩国” in static/js/sff_core.js so
# KAKAO 2.0 uses the same identity/address fixture as the visible generator.
KR_PROFILE_LOCATIONS = (
    ("Seoul", "Seoul", ("03027", "04524", "06035", "06164")),
    ("Busan", "Busan", ("47291", "47545", "48058", "48942")),
    ("Incheon", "Incheon", ("21354", "21554", "21984", "22382")),
    ("Daegu", "Daegu", ("41911", "42183", "42412", "42838")),
    ("Daejeon", "Daejeon", ("34126", "34838", "35229", "35412")),
    ("Gwangju", "Gwangju", ("61186", "61475", "61945", "62366")),
    ("Suwon", "Gyeonggi-do", ("16229", "16491", "16622", "16705")),
)
KR_PROFILE_STREETS = (
    "Teheran-ro", "Sejong-daero", "Gangnam-daero", "Eulji-ro", "Jong-ro",
    "Haeundae-ro", "Centum jungang-ro", "Songdo-gukje-daero", "Dunsan-ro",
    "Paldal-ro",
)
KR_PROFILE_FIRST_NAMES = (
    "Min-jun", "Seo-jun", "Ji-ho", "Do-yun", "Hyun-woo", "Seo-yeon",
    "Ji-woo", "Ha-eun", "Soo-ah", "Ye-eun", "Yu-na", "Min-seo",
    "Joon-ho", "Hye-jin",
)
KR_PROFILE_LAST_NAMES = (
    "Kim", "Lee", "Park", "Choi", "Jung", "Kang", "Cho", "Yoon",
    "Jang", "Lim", "Han", "Shin", "Song", "Kwon",
)

# Keep this pool identical to “资料生成 → 瑞士” in static/js/sff_core.js.
CH_PROFILE_LOCATIONS = (
    ("Zürich", "Zürich", ("8001", "8002", "8004", "8050")),
    ("Geneva", "Genève", ("1201", "1204", "1205", "1207")),
    ("Basel", "Basel-Stadt", ("4001", "4051", "4056", "4058")),
    ("Bern", "Bern", ("3001", "3011", "3012", "3014")),
    ("Lausanne", "Vaud", ("1003", "1004", "1006", "1010")),
    ("Lucerne", "Luzern", ("6003", "6004", "6005", "6014")),
    ("Lugano", "Ticino", ("6900", "6902", "6904", "6962")),
)
CH_PROFILE_STREETS = (
    "Bahnhofstrasse", "Seefeldstrasse", "Rue du Rhône", "Avenue de la Gare",
    "Marktgasse", "Spitalgasse", "Freie Strasse", "Via Nassa",
    "Pilatusstrasse", "Bundesgasse",
)
CH_PROFILE_FIRST_NAMES = (
    "Noah", "Liam", "Luca", "Leon", "Matteo", "Julian", "Emma",
    "Mia", "Sofia", "Lena", "Laura", "Lea", "Nina", "Elena",
)
CH_PROFILE_LAST_NAMES = (
    "Müller", "Meier", "Schmid", "Keller", "Weber", "Frei", "Huber",
    "Rossi", "Bernasconi", "Favre", "Dubois", "Morel", "Steiner", "Brunner",
)

# Keep this pool identical to 资料生成 -> 荷兰 in static/js/sff_core.js.
NL_PROFILE_LOCATIONS = (
    ("Amsterdam", "Noord-Holland", ("1011 AB", "1012 JS", "1054 EA", "1071 DJ")),
    ("Rotterdam", "Zuid-Holland", ("3011 AA", "3012 AD", "3021 HC", "3072 AP")),
    ("The Hague", "Zuid-Holland", ("2511 AA", "2514 CE", "2562 AW", "2585 EV")),
    ("Utrecht", "Utrecht", ("3511 AA", "3512 JC", "3521 AL", "3572 CE")),
    ("Eindhoven", "Noord-Brabant", ("5611 AA", "5612 AZ", "5616 CA", "5621 AA")),
    ("Groningen", "Groningen", ("9711 AA", "9712 CP", "9721 AD", "9741 AA")),
    ("Maastricht", "Limburg", ("6211 AA", "6212 AR", "6221 AA", "6224 EA")),
)
NL_PROFILE_STREETS = (
    "Damrak", "Keizersgracht", "Prinsengracht", "Coolsingel",
    "Laan van Meerdervoort", "Oudegracht", "Strijp-S", "Grote Markt",
    "Vrijthof", "Witte de Withstraat",
)
NL_PROFILE_FIRST_NAMES = (
    "Daan", "Sem", "Lucas", "Finn", "Lars", "Bram", "Emma", "Sophie",
    "Julia", "Mila", "Tess", "Lotte", "Nora", "Eva",
)
NL_PROFILE_LAST_NAMES = (
    "de Jong", "Jansen", "de Vries", "van den Berg", "van Dijk", "Bakker",
    "Visser", "Smit", "Meijer", "Bos", "Vos", "Peters", "Hendriks", "Dekker",
)

# Keep these pools identical to 资料生成 -> 越南/菲律宾 in static/js/sff_core.js.
VN_PROFILE_LOCATIONS = (
    ("Ho Chi Minh City", "Ho Chi Minh City", ("700000", "700100", "700200", "700300")),
    ("Hanoi", "Hanoi", ("100000", "100100", "100200", "100300")),
    ("Da Nang", "Da Nang", ("550000", "550100", "550200", "550300")),
    ("Hai Phong", "Hai Phong", ("570000", "570100", "570200", "570300")),
    ("Nha Trang", "Khanh Hoa", ("650000", "650100", "650200", "650300")),
    ("Can Tho", "Can Tho", ("900000", "900100", "900200", "900300")),
    ("Hue", "Thua Thien Hue", ("530000", "530100", "530200", "530300")),
)
VN_PROFILE_STREETS = (
    "Nguyen Hue Street", "Le Loi Street", "Tran Hung Dao Street",
    "Hai Ba Trung Street", "Vo Van Tan Street", "Nguyen Trai Street",
    "Pham Ngu Lao Street", "Ba Trieu Street", "Ly Thuong Kiet Street",
    "Dien Bien Phu Street",
)
VN_PROFILE_FIRST_NAMES = (
    "Minh", "Anh", "Huy", "Nam", "Tuan", "Long", "Linh", "Trang",
    "Mai", "Thao", "Lan", "Phuong", "Ngoc", "Ha",
)
VN_PROFILE_LAST_NAMES = (
    "Nguyen", "Tran", "Le", "Pham", "Hoang", "Huynh", "Phan", "Vu",
    "Vo", "Dang", "Bui", "Do", "Ho", "Ngo",
)

PH_PROFILE_LOCATIONS = (
    ("Manila", "Metro Manila", ("1000", "1004", "1006", "1012")),
    ("Quezon City", "Metro Manila", ("1100", "1101", "1103", "1110")),
    ("Makati", "Metro Manila", ("1200", "1204", "1209", "1227")),
    ("Pasig", "Metro Manila", ("1600", "1603", "1605", "1610")),
    ("Cebu City", "Cebu", ("6000", "6004", "6006", "6014")),
    ("Davao City", "Davao del Sur", ("8000", "8002", "8004", "8016")),
    ("Baguio", "Benguet", ("2600", "2601", "2602", "2604")),
)
PH_PROFILE_STREETS = (
    "Ayala Avenue", "Epifanio de los Santos Avenue", "Roxas Boulevard",
    "Taft Avenue", "Makati Avenue", "Ortigas Avenue", "Commonwealth Avenue",
    "Osmena Boulevard", "Claveria Street", "Session Road",
)
PH_PROFILE_FIRST_NAMES = (
    "Juan", "Jose", "Miguel", "Gabriel", "Paolo", "Carlos", "Maria", "Ana",
    "Angela", "Sofia", "Isabella", "Camille", "Patricia", "Bianca",
)
PH_PROFILE_LAST_NAMES = (
    "Santos", "Reyes", "Cruz", "Garcia", "Mendoza", "Bautista", "Flores",
    "Aquino", "Ramos", "Navarro", "Castillo", "Torres", "Rivera", "Villanueva",
)

# Keep these pools identical to 资料生成 -> 波黑/巴林 in static/js/sff_core.js.
BA_PROFILE_LOCATIONS = (
    ("Sarajevo", "Federation of Bosnia and Herzegovina", ("71000", "71010", "71120", "71210")),
    ("Banja Luka", "Republika Srpska", ("78000", "78010", "78101", "78250")),
    ("Mostar", "Federation of Bosnia and Herzegovina", ("88000", "88101", "88201", "88240")),
    ("Tuzla", "Federation of Bosnia and Herzegovina", ("75000", "75010", "75201", "75270")),
    ("Zenica", "Federation of Bosnia and Herzegovina", ("72000", "72010", "72220", "72240")),
    ("Bijeljina", "Republika Srpska", ("76300", "76310", "76320", "76330")),
    ("Brcko", "Brcko District", ("76100", "76101", "76200", "76230")),
)
BA_PROFILE_STREETS = (
    "Marsala Tita", "Ferhadija", "Zmaja od Bosne", "Kralja Petra I",
    "Alekse Santica", "Bulevar Mira", "Mehmeda Spahe", "Obala Kulina bana",
    "Mese Selimovica", "Branilaca Sarajeva",
)
BA_PROFILE_FIRST_NAMES = (
    "Amar", "Emir", "Haris", "Adnan", "Tarik", "Mirza", "Amina", "Lejla",
    "Sara", "Emina", "Nina", "Ajla", "Merima", "Lamija",
)
BA_PROFILE_LAST_NAMES = (
    "Hodzic", "Kovacevic", "Markovic", "Petrovic", "Basic", "Hadzic", "Dedic",
    "Ilic", "Jovanovic", "Nikolic", "Begic", "Halilovic", "Softic", "Memic",
)

BH_PROFILE_LOCATIONS = (
    ("Manama", "Capital Governorate", ("317", "318", "321", "338")),
    ("Muharraq", "Muharraq Governorate", ("202", "203", "207", "224")),
    ("Riffa", "Southern Governorate", ("901", "903", "905", "909")),
    ("Isa Town", "Southern Governorate", ("801", "803", "806", "812")),
    ("Hamad Town", "Northern Governorate", ("1205", "1207", "1210", "1216")),
    ("Sitra", "Capital Governorate", ("601", "603", "606", "611")),
    ("Budaiya", "Northern Governorate", ("552", "553", "555", "559")),
)
BH_PROFILE_STREETS = (
    "Government Avenue", "Exhibition Avenue", "Al Fateh Highway", "Budaiya Highway",
    "Shaikh Isa Avenue", "King Faisal Highway", "Road 2802", "Road 3801",
    "Road 1704", "Road 1010",
)
BH_PROFILE_FIRST_NAMES = (
    "Ahmed", "Ali", "Hassan", "Mohammed", "Yousef", "Khalid", "Fatima",
    "Maryam", "Noor", "Aisha", "Layla", "Sara", "Zainab", "Hessa",
)
BH_PROFILE_LAST_NAMES = (
    "Al Khalifa", "Al Doseri", "Al Zayani", "Al Mannai", "Al Noaimi", "Al Sayed",
    "Hassan", "Abdullah", "Rahman", "Khan", "Al Arrayed", "Al Jalahma",
    "Al Fardan", "Al Kooheji",
)


def opll_generate_in_profile(payment_email: str = "") -> dict:
    """Generate the same internally-consistent IN identity used by 资料生成."""
    first = random.choice(IN_PROFILE_FIRST_NAMES)
    last = random.choice(IN_PROFILE_LAST_NAMES)
    city, state, postal_codes = random.choice(IN_PROFILE_LOCATIONS)
    house_number = str(random.randint(1, 240))
    street_name = random.choice(IN_PROFILE_STREETS)
    email = str(payment_email or "").strip()
    if not email:
        email = f"{first.lower()}.{last.lower()}{random.randint(1000, 9999)}@in.example.com"
    return {
        "name": f"{first} {last}",
        "first_name": first,
        "last_name": last,
        "email": email,
        "phone": opll_random_phone_for_country("IN"),
        "country": "IN",
        "line1": f"{house_number}, {street_name}",
        "line2": "",
        "house_number": house_number,
        "city": city,
        "state": state,
        "postal_code": random.choice(postal_codes),
    }


def opll_generate_th_profile(payment_email: str = "") -> dict:
    """Generate the same internally-consistent TH identity used by 资料生成."""
    first = random.choice(TH_PROFILE_FIRST_NAMES)
    last = random.choice(TH_PROFILE_LAST_NAMES)
    city, state, postal_codes = random.choice(TH_PROFILE_LOCATIONS)
    house_number = str(random.randint(1, 240))
    email = str(payment_email or "").strip()
    if not email:
        email = f"{first.lower()}.{last.lower()}{random.randint(1000, 9999)}@th.example.com"
    return {
        "name": f"{first} {last}",
        "first_name": first,
        "last_name": last,
        "email": email,
        "phone": opll_random_phone_for_country("TH"),
        "country": "TH",
        "line1": f"{house_number} {random.choice(TH_PROFILE_STREETS)}",
        "line2": "",
        "house_number": house_number,
        "city": city,
        "state": state,
        "postal_code": random.choice(postal_codes),
    }


def opll_generate_kr_profile(payment_email: str = "") -> dict:
    """Generate the same internally-consistent KR identity used by 资料生成."""
    first = random.choice(KR_PROFILE_FIRST_NAMES)
    last = random.choice(KR_PROFILE_LAST_NAMES)
    city, state, postal_codes = random.choice(KR_PROFILE_LOCATIONS)
    house_number = str(random.randint(1, 240))
    email = str(payment_email or "").strip()
    if not email:
        email = f"{first.lower().replace('-', '')}.{last.lower()}{random.randint(1000, 9999)}@kr.example.com"
    return {
        "name": f"{first} {last}",
        "first_name": first,
        "last_name": last,
        "email": email,
        "phone": opll_random_phone_for_country("KR"),
        "country": "KR",
        "line1": f"{house_number}, {random.choice(KR_PROFILE_STREETS)}",
        "line2": "",
        "house_number": house_number,
        "city": city,
        "state": state,
        "postal_code": random.choice(postal_codes),
    }


def opll_generate_ch_profile(payment_email: str = "") -> dict:
    """Generate the same internally-consistent CH identity used by 资料生成."""
    first = random.choice(CH_PROFILE_FIRST_NAMES)
    last = random.choice(CH_PROFILE_LAST_NAMES)
    city, state, postal_codes = random.choice(CH_PROFILE_LOCATIONS)
    house_number = str(random.randint(1, 240))
    email = str(payment_email or "").strip()
    if not email:
        email_name = re.sub(r"[^a-z0-9]+", "", f"{first}.{last}".lower())
        email = f"{email_name}{random.randint(1000, 9999)}@ch.example.com"
    return {
        "name": f"{first} {last}",
        "first_name": first,
        "last_name": last,
        "email": email,
        "phone": opll_random_phone_for_country("CH"),
        "country": "CH",
        "line1": f"{random.choice(CH_PROFILE_STREETS)} {house_number}",
        "line2": "",
        "house_number": house_number,
        "city": city,
        "state": state,
        "postal_code": random.choice(postal_codes),
    }


def opll_generate_nl_profile(payment_email: str = "") -> dict:
    """Generate the same internally-consistent NL identity used by 资料生成."""
    first = random.choice(NL_PROFILE_FIRST_NAMES)
    last = random.choice(NL_PROFILE_LAST_NAMES)
    city, state, postal_codes = random.choice(NL_PROFILE_LOCATIONS)
    house_number = str(random.randint(1, 240))
    email = str(payment_email or "").strip()
    if not email:
        email_name = re.sub(r"[^a-z0-9.]+", "", f"{first}.{last}".lower()).strip(".")
        email = f"{email_name}{random.randint(1000, 9999)}@nl.example.com"
    return {
        "name": f"{first} {last}",
        "first_name": first,
        "last_name": last,
        "email": email,
        "phone": opll_random_phone_for_country("NL"),
        "country": "NL",
        "line1": f"{random.choice(NL_PROFILE_STREETS)} {house_number}",
        "line2": "",
        "house_number": house_number,
        "city": city,
        "state": state,
        "postal_code": random.choice(postal_codes),
        "profile_source": "资料生成/static/js/sff_core.js:nl",
    }


def _opll_generate_southeast_asia_profile(
    country: str,
    locations: tuple,
    streets: tuple,
    first_names: tuple,
    last_names: tuple,
    payment_email: str = "",
) -> dict:
    first = random.choice(first_names)
    last = random.choice(last_names)
    city, state, postal_codes = random.choice(locations)
    house_number = str(random.randint(1, 240))
    email = str(payment_email or "").strip()
    if not email:
        email = (
            f"{opll_email_slug(first)}.{opll_email_slug(last)}{random.randint(1000, 9999)}@"
            f"{PAYPAL_GLOBAL_EMAIL_DOMAINS[country]}"
        )
    email = opll_sanitize_billing_email(email, first, last, country)
    street = random.choice(streets)
    if country == "BA":
        line1 = f"{street} {house_number}"
    elif country == "BH":
        line1 = f"{house_number}, {street}"
    else:
        line1 = f"{house_number} {street}"
    return {
        "name": f"{first} {last}",
        "first_name": first,
        "last_name": last,
        "email": email,
        "phone": opll_random_phone_for_country(country),
        "country": country,
        "line1": line1,
        "line2": "",
        "house_number": house_number,
        "city": city,
        "state": state,
        "postal_code": random.choice(postal_codes),
        "profile_source": f"资料生成/static/js/sff_core.js:{country.lower()}",
    }


def opll_generate_vn_profile(payment_email: str = "") -> dict:
    return _opll_generate_southeast_asia_profile(
        "VN", VN_PROFILE_LOCATIONS, VN_PROFILE_STREETS,
        VN_PROFILE_FIRST_NAMES, VN_PROFILE_LAST_NAMES, payment_email,
    )


def opll_generate_ph_profile(payment_email: str = "") -> dict:
    return _opll_generate_southeast_asia_profile(
        "PH", PH_PROFILE_LOCATIONS, PH_PROFILE_STREETS,
        PH_PROFILE_FIRST_NAMES, PH_PROFILE_LAST_NAMES, payment_email,
    )


def opll_generate_ba_profile(payment_email: str = "") -> dict:
    return _opll_generate_southeast_asia_profile(
        "BA", BA_PROFILE_LOCATIONS, BA_PROFILE_STREETS,
        BA_PROFILE_FIRST_NAMES, BA_PROFILE_LAST_NAMES, payment_email,
    )


def opll_generate_bh_profile(payment_email: str = "") -> dict:
    return _opll_generate_southeast_asia_profile(
        "BH", BH_PROFILE_LOCATIONS, BH_PROFILE_STREETS,
        BH_PROFILE_FIRST_NAMES, BH_PROFILE_LAST_NAMES, payment_email,
    )


def opll_billing_for_country(country: str) -> dict:
    country = normalize_opll_country(country)
    if country == "IN":
        return opll_generate_in_profile()
    if country == "TH":
        return opll_generate_th_profile()
    if country == "KR":
        return opll_generate_kr_profile()
    if country == "CH":
        return opll_generate_ch_profile()
    if country == "DE":
        first, last = random.choice(DE_BILLING_NAMES)
        line1, city, state, postal = random.choice(DE_BILLING_STREETS)
    elif country == "GB":
        first, last = random.choice(GB_BILLING_NAMES)
        line1, city, state, postal = random.choice(GB_BILLING_STREETS)
    elif country == "AU":
        first, last = random.choice(AU_BILLING_NAMES)
        line1, city, state, postal = random.choice(AU_BILLING_STREETS)
    elif country == "BR":
        first, last = random.choice(BR_BILLING_NAMES)
        line1, city, state, postal = random.choice(BR_BILLING_STREETS)
    elif country == "US":
        first, last = random.choice(US_BILLING_NAMES)
        line1, city, state, postal = random.choice(US_BILLING_STREETS)
    elif country in EXTRA_BILLING_STREETS:
        first, last = random.choice(EXTRA_BILLING_NAMES)
        line1, city, state, postal = random.choice(EXTRA_BILLING_STREETS[country])
    elif country in OPENAI_SUPPORTED_COUNTRY_CODES:
        profile = BILLING_PROFILE_BY_COUNTRY[country]
        first, last = random.choice(EXTRA_BILLING_NAMES)
        line1 = f"{random.randint(10, 999)} {random.choice(profile['street_pool'])}"
        city = random.choice(profile["city_pool"])
        state = country
        postal = opll_random_postal_code(str(profile.get("postal_pattern") or "#####"))
    else:
        raise RuntimeError(f"不支持的账单资料地区: {country}")
    suffix = random.randint(1000, 9999)
    return {
        "name": f"{first} {last}",
        "email": f"{first.lower()}.{last.lower()}{suffix}@example.com",
        "phone": opll_random_phone_for_country(country),
        "country": country,
        "line1": line1,
        "city": city,
        "state": state,
        "postal_code": postal,
    }


def opll_generate_valid_br_cpf() -> str:
    """Generate a syntactically valid Brazilian CPF number for Stripe PIX billing."""
    digits = [random.randint(1, 9)] + [random.randint(0, 9) for _ in range(8)]
    # Avoid repeated trivial CPFs.
    if len(set(digits)) == 1:
        digits[0] = (digits[0] + 1) % 10

    def check_digit(values: list[int], start_weight: int) -> int:
        total = sum(v * w for v, w in zip(values, range(start_weight, 1, -1)))
        digit = 11 - (total % 11)
        return 0 if digit >= 10 else digit

    digits.append(check_digit(digits, 10))
    digits.append(check_digit(digits, 11))
    return "".join(str(d) for d in digits)


def opll_generate_br_profile() -> dict:
    """Generate the same Brazil identity/address shape used by 资料生成."""
    first_name = random.choice(BR_PROFILE_FIRST_NAMES)
    surnames = [random.choice(BR_PROFILE_LAST_NAMES)]
    if random.random() < 0.5:
        surnames.append(random.choice(BR_PROFILE_LAST_NAMES))
    last_name = " ".join(surnames)
    city, state, postal_codes = random.choice(BR_PROFILE_LOCATIONS)
    postal_code = random.choice(postal_codes)
    street = random.choice(BR_PROFILE_STREETS)
    house_number = str(random.randint(1, 9999))
    neighborhood = random.choice(BR_PROFILE_NEIGHBORHOODS)
    cpf = opll_generate_valid_br_cpf()
    normalized_name = unicodedata.normalize("NFD", f"{first_name}.{last_name}")
    email_base = re.sub(r"[^a-z0-9.]", "", "".join(
        ch for ch in normalized_name.lower() if not unicodedata.combining(ch)
    )).strip(".") or "user.mail"
    area_codes = {
        "SP": "11", "RJ": "21", "MG": "31", "BA": "71", "DF": "61",
        "PR": "41", "RS": "51", "PE": "81", "CE": "85", "AM": "92",
        "SC": "48", "GO": "62",
    }
    area_code = area_codes.get(state, "11")
    mobile = "9" + "".join(str(random.randint(0, 9)) for _ in range(8))
    year = random.randint(1975, 2004)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return {
        "first_name": first_name,
        "last_name": last_name,
        "name": f"{first_name} {last_name}",
        "email": f"{email_base}{random.randint(1000, 9999)}@outlook.com",
        "phone": f"+55{area_code}{mobile}",
        "country": "BR",
        "street": street,
        "house_number": house_number,
        "line1": f"{street}, {house_number}",
        "line2": neighborhood,
        "neighborhood": neighborhood,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "tax_id": cpf,
        "cpf_formatted": f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}",
        "birthday": f"{day:02d}/{month:02d}/{year}",
        "full_address": f"{street}, {house_number}, {city} - {state}, CEP {postal_code}",
        "profile_source": "资料生成/static/js/sff_core.js:br",
    }


def opll_generate_paypal_global_profile(country: str, payment_email: str = "") -> dict:
    """Generate one billing profile from the same 21-country source exposed by 资料生成."""
    country = normalize_paypal_global_billing_country(country)
    email = str(payment_email or "").strip()
    if country == "BR":
        profile = opll_generate_br_profile()
    elif country == "IN":
        profile = opll_generate_in_profile(email)
    elif country == "TH":
        profile = opll_generate_th_profile(email)
    elif country == "KR":
        profile = opll_generate_kr_profile(email)
    elif country == "CH":
        profile = opll_generate_ch_profile(email)
    elif country == "NL":
        profile = opll_generate_nl_profile(email)
    elif country == "VN":
        profile = opll_generate_vn_profile(email)
    elif country == "PH":
        profile = opll_generate_ph_profile(email)
    elif country == "BA":
        profile = opll_generate_ba_profile(email)
    elif country == "BH":
        profile = opll_generate_bh_profile(email)
    else:
        profile = opll_billing_for_country(country)

    profile = dict(profile or {})
    profile["country"] = country
    if email:
        profile["email"] = opll_sanitize_billing_email(
            email, profile.get("first_name") or "user", profile.get("last_name") or "mail", country
        )
    elif country not in {"BR", "IN", "TH", "KR", "CH", "NL", "VN", "PH", "BA", "BH"}:
        name = str(profile.get("name") or "user mail")
        profile["email"] = (
            f"{opll_email_slug(name)}{random.randint(1000, 9999)}@"
            f"{PAYPAL_GLOBAL_EMAIL_DOMAINS[country]}"
        )
    profile["email"] = opll_sanitize_billing_email(
        profile.get("email") or "",
        profile.get("first_name") or "user",
        profile.get("last_name") or "mail",
        country,
    )
    profile["profile_source"] = (
        profile.get("profile_source")
        or f"资料生成/static/js/sff_core.js:{country.lower()}"
    )
    return profile

def opll_brazil_pix_billing(access_token: str = "") -> dict:
    # PIX 2.0 deliberately uses the generated BR mailbox instead of deriving the
    # billing identity from the account token (which can carry an older IN tax
    # profile).  access_token remains in the signature for legacy call sites.
    return opll_generate_br_profile()


# ===================================================================
# Stripe payment method (PayPal)
# ===================================================================

def opll_stripe_create_paypal_method(stripe: requests.Session, cs_id: str, ctx: dict,
                                      billing: dict, stripe_pk: str = "") -> str:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    safe_email = opll_sanitize_billing_email(
        billing.get("email") or "buyer@example.com",
        billing.get("first_name") or "John",
        billing.get("last_name") or "Doe",
        billing.get("country") or "US",
    )
    body = {
        "billing_details[name]": billing.get("name") or "John Doe",
        "billing_details[email]": safe_email,
        "billing_details[phone]": billing.get("phone") or "",
        "billing_details[address][country]": billing.get("country") or "US",
        "billing_details[address][line1]": billing.get("line1") or "3110 Sunset Boulevard",
        "billing_details[address][city]": billing.get("city") or "Los Angeles",
        "billing_details[address][postal_code]": billing.get("postal_code") or "90026",
        "billing_details[address][state]": billing.get("state") or "CA",
        "type": "paypal",
        "payment_user_agent": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_pk or DEFAULT_STRIPE_PK,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    response = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=PAY_LONG_LINK_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"stripe payment_methods failed: HTTP {response.status_code} {response.text[:500]}")
    pm_id = str((response.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"stripe payment_methods bad response: {response.text[:300]}")
    return pm_id


def opll_stripe_create_ideal_method(stripe: requests.Session, cs_id: str, ctx: dict,
                                    billing: dict, stripe_pk: str = "") -> str:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body = {
        "billing_details[name]": billing.get("name") or "Ideal User",
        "billing_details[email]": billing.get("email") or "buyer@example.com",
        "billing_details[phone]": billing.get("phone") or "",
        "billing_details[address][country]": billing.get("country") or "NL",
        "billing_details[address][line1]": billing.get("line1") or "Damrak 1",
        "billing_details[address][city]": billing.get("city") or "Amsterdam",
        "billing_details[address][postal_code]": billing.get("postal_code") or "1012LG",
        "billing_details[address][state]": billing.get("state") or "NL",
        "type": "ideal",
        "payment_user_agent": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_pk or DEFAULT_STRIPE_PK,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
    }
    response = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=PAY_LONG_LINK_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"stripe iDEAL payment_methods failed: HTTP {response.status_code} {response.text[:500]}")
    pm_id = str((response.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"stripe iDEAL payment_methods bad response: {response.text[:300]}")
    return pm_id


def opll_stripe_create_kakao_pay_method(stripe: requests.Session, cs_id: str, ctx: dict,
                                         billing: dict, stripe_pk: str = "") -> str:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body = {
        "billing_details[name]": billing.get("name") or "Kakao User",
        "billing_details[email]": billing.get("email") or "buyer@example.com",
        "billing_details[phone]": billing.get("phone") or "",
        "billing_details[address][country]": billing.get("country") or "KR",
        "billing_details[address][line1]": billing.get("line1") or "123 Teheran-ro",
        "billing_details[address][city]": billing.get("city") or "Seoul",
        "billing_details[address][postal_code]": billing.get("postal_code") or "06164",
        "billing_details[address][state]": billing.get("state") or "KR",
        "type": "kakao_pay",
        "payment_user_agent": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_pk or DEFAULT_STRIPE_PK,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    response = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=PAY_LONG_LINK_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"stripe KAKAO payment_methods failed: HTTP {response.status_code} {response.text[:500]}")
    pm_id = str((response.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"stripe KAKAO payment_methods bad response: {response.text[:300]}")
    return pm_id


def opll_stripe_create_twint_method(stripe: requests.Session, cs_id: str, ctx: dict,
                                    billing: dict, stripe_pk: str = "") -> str:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body = {
        "billing_details[name]": billing.get("name") or "Twint User",
        "billing_details[email]": billing.get("email") or "buyer@example.com",
        "billing_details[phone]": billing.get("phone") or "",
        "billing_details[address][country]": billing.get("country") or "CH",
        "billing_details[address][line1]": billing.get("line1") or "Bahnhofstrasse 1",
        "billing_details[address][city]": billing.get("city") or "Zürich",
        "billing_details[address][postal_code]": billing.get("postal_code") or "8001",
        "billing_details[address][state]": billing.get("state") or "Zürich",
        "type": "twint",
        "payment_user_agent": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_pk or DEFAULT_STRIPE_PK,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
    }
    response = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=PAY_LONG_LINK_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"stripe TWINT payment_methods failed: HTTP {response.status_code} {response.text[:500]}")
    pm_id = str((response.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"stripe TWINT payment_methods bad response: {response.text[:300]}")
    return pm_id


def opll_stripe_create_promptpay_method(stripe: requests.Session, cs_id: str, ctx: dict,
                                        billing: dict, stripe_pk: str = "") -> str:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body = {
        "billing_details[name]": billing.get("name") or "PromptPay User",
        "billing_details[email]": billing.get("email") or "buyer@example.com",
        "billing_details[phone]": billing.get("phone") or "",
        "billing_details[address][country]": billing.get("country") or "TH",
        "billing_details[address][line1]": billing.get("line1") or "1 Sukhumvit Road",
        "billing_details[address][city]": billing.get("city") or "Bangkok",
        "billing_details[address][postal_code]": billing.get("postal_code") or "10110",
        "billing_details[address][state]": billing.get("state") or "Bangkok",
        "type": "promptpay",
        "payment_user_agent": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_pk or DEFAULT_STRIPE_PK,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
    }
    response = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=PAY_LONG_LINK_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"stripe PromptPay payment_methods failed: HTTP {response.status_code} {response.text[:500]}")
    pm_id = str((response.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"stripe PromptPay payment_methods bad response: {response.text[:300]}")
    return pm_id


def opll_stripe_create_momo_method(stripe: requests.Session, cs_id: str, ctx: dict,
                                   billing: dict, stripe_pk: str = "") -> str:
    """Create the Vietnam MoMo payment method used by the migrated standalone flow."""
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body = {
        "billing_details[name]": billing.get("name") or "Nguyen Van An",
        "billing_details[email]": billing.get("email") or "buyer@example.com",
        "billing_details[phone]": billing.get("phone") or "+84901234567",
        "billing_details[address][country]": billing.get("country") or "VN",
        "billing_details[address][line1]": billing.get("line1") or "12 Nguyen Hue",
        "billing_details[address][line2]": billing.get("line2") or "",
        "billing_details[address][city]": billing.get("city") or "Ho Chi Minh City",
        "billing_details[address][postal_code]": billing.get("postal_code") or "700000",
        "billing_details[address][state]": billing.get("state") or "SG",
        "type": "momo",
        "payment_user_agent": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(30000, 90000)),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[client_session_id]": str(ctx.get("stripe_js_id") or ""),
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": str(ctx.get("elements_session_id") or ""),
        "client_attribution_metadata[elements_session_config_id]": str(ctx.get("elements_session_config_id") or ctx.get("config_id") or ""),
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_pk or DEFAULT_STRIPE_PK,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
    }
    response = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=PAY_LONG_LINK_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"stripe MoMo payment_methods failed: HTTP {response.status_code} {response.text[:500]}")
    pm_id = str((response.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"stripe MoMo payment_methods bad response: {response.text[:300]}")
    return pm_id


def opll_stripe_create_pix_method(stripe: requests.Session, cs_id: str, ctx: dict,
                                  billing: dict, stripe_pk: str = "") -> str:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    stripe_version = str(ctx.get("stripe_version") or STRIPE_VERSION_FULL)
    body = {
        "billing_details[name]": billing.get("name") or "Pix User",
        "billing_details[email]": billing.get("email") or "buyer@example.com",
        "billing_details[phone]": billing.get("phone") or "",
        "billing_details[address][country]": "BR",
        "billing_details[address][line1]": billing.get("line1") or "Avenida Paulista 300",
        "billing_details[address][city]": billing.get("city") or "Sao Paulo",
        "billing_details[address][postal_code]": billing.get("postal_code") or "01311-000",
        "billing_details[address][state]": billing.get("state") or "SP",
        "billing_details[tax_id]": billing.get("tax_id") or "",
        "type": "pix",
        "payment_user_agent": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_pk or DEFAULT_STRIPE_PK,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
    }

    def post(payload: dict):
        return stripe.post("https://api.stripe.com/v1/payment_methods", data=payload, timeout=PAY_LONG_LINK_TIMEOUT)

    response = post(body)
    removed_unknown_params: list[str] = []
    for _ in range(6):
        if response.status_code < 400:
            break
        try:
            error = (response.json() or {}).get("error") or {}
        except Exception:
            error = {}
        unknown = str(error.get("param") or "").strip() if isinstance(error, dict) and error.get("code") == "parameter_unknown" else ""
        if not unknown or unknown not in body:
            break
        removed_unknown_params.append(unknown)
        body.pop(unknown, None)
        response = post(body)
    if response.status_code >= 400:
        removed_hint = f"; removed_unknown_params={removed_unknown_params}" if removed_unknown_params else ""
        raise RuntimeError(f"stripe PIX payment_methods failed: HTTP {response.status_code} {response.text[:500]}{removed_hint}")
    payload = response.json() or {}
    pm_id = str(payload.get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"stripe PIX payment_methods bad response: {response.text[:300]}")
    return pm_id


# ===================================================================
# ChatGPT approve
# ===================================================================

class OpllStripeRequiresApproval(Exception):
    pass


class OpllChatgptApproveBlocked(Exception):
    pass


OPLL_APPROVE_BURST_RESULTS = {"blocked", "exception"}


def opll_chatgpt_approve(chatgpt: requests.Session, cs_id: str, checkout: dict) -> None:
    entity = opll_processor_entity_for_country(checkout["billing_country"], checkout.get("processor_entity", ""))
    try:
        chatgpt.post(
            "https://chatgpt.com/backend-api/sentinel/ping",
            json={},
            headers={
                "Referer": "https://chatgpt.com/",
                "x-openai-target-path": "/backend-api/sentinel/ping",
                "x-openai-target-route": "/backend-api/sentinel/ping",
            },
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
    except Exception:
        pass
    response = chatgpt.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json={"checkout_session_id": cs_id, "processor_entity": entity},
        headers={
            "Referer": f"https://chatgpt.com/checkout/{entity}/{cs_id}",
            "x-openai-target-path": "/backend-api/payments/checkout/approve",
            "x-openai-target-route": "/backend-api/payments/checkout/approve",
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"chatgpt approve failed: {opll_http_error_detail(response)}")
    try:
        result = (response.json() or {}).get("result")
    except Exception:
        result = ""
    normalized_result = str(result or "").strip().lower()
    if normalized_result in OPLL_APPROVE_BURST_RESULTS:
        raise OpllChatgptApproveBlocked(f"chatgpt approve retryable result: {normalized_result!r}")
    if result != "approved":
        raise RuntimeError(f"chatgpt approve unexpected result: {result!r}")


def opll_chatgpt_approve_with_retry(access_token: str, cs_id: str, checkout: dict,
                                     proxy_url: str = "", chatgpt_cookie: str = "") -> requests.Session:
    last_error = ""
    for _ in range(3):
        try:
            chatgpt = opll_build_chatgpt_session(access_token, proxy_url, chatgpt_cookie=chatgpt_cookie)
            opll_chatgpt_approve(chatgpt, cs_id, checkout)
            return chatgpt
        except OpllChatgptApproveBlocked as exc:
            last_error = str(exc)
            break
        except Exception as exc:
            last_error = str(exc)
            time.sleep(1)
    raise RuntimeError(f"ChatGPT approve 连续失败: {last_error}")


def opll_chatgpt_approve_with_routes(access_token: str, cs_id: str, checkout: dict,
                                     proxy_routes=None, chatgpt_cookie: str = "",
                                     attempts_per_route: int = 2) -> dict:
    """Approve using several candidate routes.

    PIX often reaches Stripe with submission_attempt.state=requires_approval.
    A single backend approve can return approved but not materialize the Stripe
    instructions page, so PIX uses this route-aware helper and polls after each
    route instead of blindly repeating the whole checkout.
    """
    token = opll_access_token_with_cookie(access_token, chatgpt_cookie)
    routes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in (proxy_routes or [("default", "")]):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            label = str(item[0] or "route")
            proxy = str(item[1] or "").strip()
        else:
            proxy = str(item or "").strip()
            label = proxy or "direct"
        key = f"{label}\n{proxy}"
        if key in seen:
            continue
        seen.add(key)
        routes.append((label, proxy))
    if not routes:
        routes.append(("direct", ""))

    errors: list[str] = []
    for label, proxy in routes:
        for attempt_no in range(max(1, int(attempts_per_route or 1))):
            try:
                chatgpt = opll_build_chatgpt_session(token, proxy, chatgpt_cookie=chatgpt_cookie)
                opll_chatgpt_approve(chatgpt, cs_id, checkout)
                return {
                    "ok": True,
                    "route": label,
                    "attempt": attempt_no + 1,
                    "errors": errors[-8:],
                }
            except OpllChatgptApproveBlocked as exc:
                errors.append(f"{label}: blocked ({opll_short_error(str(exc), 120)})")
                time.sleep(0.8)
            except Exception as exc:
                errors.append(f"{label}: {opll_short_error(str(exc), 180)}")
                time.sleep(1)
    raise RuntimeError("ChatGPT approve routes failed: " + " | ".join(errors[-10:]))


def opll_chatgpt_confirm_approve_payment(access_token: str, cs_id: str, checkout: dict,
                                          payment_method_type: str,
                                          proxy_url: str = "", chatgpt_cookie: str = "",
                                          submission_attempt_id: str = "",
                                          progress_callback=None) -> dict:
    """Run the checkout-page confirm/approve state machine for a local method."""
    token = opll_access_token_with_cookie(access_token, chatgpt_cookie, proxy_url)
    if not token:
        raise RuntimeError("checkout confirm/approve missing Access Token")
    method = str(payment_method_type or "").strip().lower()
    if not method:
        raise RuntimeError("checkout confirm/approve missing payment method type")
    entity = str(
        (checkout or {}).get("processor_entity")
        or opll_processor_entity_for_country((checkout or {}).get("billing_country") or "BR")
    ).strip()
    referer = f"https://chatgpt.com/checkout/{entity}/{cs_id}"
    target_path = f"/checkout/{entity}/{cs_id}"
    route_headers = {
        "Referer": referer,
        "x-openai-target-path": target_path,
        "x-openai-target-route": "/checkout/[processorEntity]/[checkoutSessionId]",
        "OAI-Chat-Web-Route": "/checkout/[processorEntity]/[checkoutSessionId]",
    }
    chatgpt = opll_build_chatgpt_session(
        token, proxy_url, chatgpt_cookie=chatgpt_cookie)
    trace: list[dict] = []

    def decode(response) -> dict:
        try:
            payload = response.json() or {}
        except Exception:
            payload = {"raw": str(getattr(response, "text", "") or "")[:500]}
        return payload if isinstance(payload, dict) else {"payload": payload}

    confirm = chatgpt.post(
        "https://chatgpt.com/backend-api/payments/checkout/confirm",
        json={
            "checkout_session_id": cs_id,
            "selected_payment_method_type": method,
        },
        headers=route_headers,
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    confirm_payload = decode(confirm)
    confirm_result = str(confirm_payload.get("result") or "").strip().lower()
    trace.append({
        "name": "confirm",
        "status": int(confirm.status_code),
        "result": confirm_result,
    })
    if confirm.status_code < 400 and confirm_result == "approved":
        return {
            "ok": True,
            "name": "confirm",
            "status": int(confirm.status_code),
            "result": confirm_result,
            "attempt": 0,
            "trace": trace,
            "payload": confirm_payload,
        }

    try:
        attempts = int(os.environ.get("CHATGPT_PIX2_APPROVAL_ATTEMPTS", "60"))
    except Exception:
        attempts = 60
    attempts = max(1, min(attempts, 80))
    try:
        blocked_fast_fail = int(os.environ.get("CHATGPT_PIX2_BLOCKED_FAST_FAIL", "5"))
    except Exception:
        blocked_fast_fail = 5
    blocked_fast_fail = max(0, min(blocked_fast_fail, attempts))
    blocked_count = 0
    last_payload = confirm_payload
    last_status = int(confirm.status_code)

    for attempt in range(1, attempts + 1):
        approve_body = {
            "checkout_session_id": cs_id,
            "processor_entity": entity,
        }
        if submission_attempt_id:
            approve_body["submission_attempt_id"] = str(submission_attempt_id)
        response = chatgpt.post(
            "https://chatgpt.com/backend-api/payments/checkout/approve",
            json=approve_body,
            headers=route_headers,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
        payload = decode(response)
        result = str(payload.get("result") or "").strip().lower()
        last_payload = payload
        last_status = int(response.status_code)
        trace.append({
            "name": "approve",
            "attempt": attempt,
            "status": last_status,
            "result": result,
        })
        _emit_payment_stage(
            progress_callback,
            "pix2_chatgpt_approval",
            f"PIX 2.0: ChatGPT approve {attempt}/{attempts}: {result or last_status}",
            7,
            10,
            approval_attempt=attempt,
            approval_attempts=attempts,
            approval_result=result,
        )
        if last_status < 400 and result == "approved":
            return {
                "ok": True,
                "name": "approve",
                "status": last_status,
                "result": result,
                "attempt": attempt,
                "trace": trace,
                "payload": payload,
            }
        if result == "blocked":
            blocked_count += 1
            if blocked_fast_fail and blocked_count >= blocked_fast_fail:
                raise RuntimeError(
                    f"PIX 2.0 checkout approval blocked x{blocked_count} on selected BR route")
        else:
            blocked_count = 0
        time.sleep(0.15)
    raise RuntimeError(
        "PIX 2.0 checkout approval ended without approved result: "
        f"HTTP {last_status} {opll_short_error(str(last_payload), 500)}")


# ===================================================================
# Stripe redirect + confirm
# ===================================================================

def opll_is_external_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def opll_is_paypal_page_host(value: str) -> bool:
    try:
        host = (urlsplit(value).netloc or "").lower()
    except Exception:
        return False
    return host == "paypal.com" or host.endswith(".paypal.com")


def opll_is_paypal_url(value: str) -> bool:
    host = (urlsplit(value).netloc or "").lower()
    return host == "paypal.com" or host.endswith(".paypal.com") or \
           host == "paypalobjects.com" or host.endswith(".paypalobjects.com")


def opll_is_paypal_ba_approve_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    if not opll_is_paypal_page_host(value):
        return False
    path = parsed.path.rstrip("/").lower()
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return path == "/agreements/approve" and bool(str(query.get("ba_token") or "").strip())


def opll_is_pm_redirect_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    return parsed.scheme in ("http", "https") and host == "pm-redirects.stripe.com"


def opll_is_true_no_card_us_url(value: str) -> bool:
    """Success definition used by the modified 无卡 US flow.

    The old hosted checkout URL (pay.openai.com/c/pay/cs_live...) is only a
    form page and can spin forever at Subscribe. The desired result is the
    post-confirm redirect URL or a PayPal approval URL.
    """
    return opll_is_pm_redirect_url(value) or opll_is_paypal_ba_approve_url(value) or \
        opll_is_paypal_approval_entry_url(value)


def opll_is_paypal_approval_entry_url(value: str) -> bool:
    """Accept PayPal approval URLs and login pages that carry the approval target."""
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    if not opll_is_paypal_page_host(value):
        return False
    if opll_is_paypal_ba_approve_url(value):
        return True
    haystack = " ".join([
        parsed.path or "",
        parsed.query or "",
        parsed.fragment or "",
    ]).lower()
    approval_markers = ("agreements/approve", "ba_token", "billingagreement", "billing-agreement")
    login_markers = ("/signin", "/login", "/webapps/hermes")
    return any(item in haystack for item in approval_markers) and \
        (any(item in (parsed.path or "").lower() for item in login_markers) or "return" in haystack)


def opll_is_paypal_page_url(value: str) -> bool:
    """Loose PayPal page validator used by Brazil PayPal extraction.

    It accepts real paypal.com pages while still rejecting PayPal static assets
    and paypalobjects resources. This is intentionally looser than
    opll_is_paypal_approval_entry_url because BR only needs the PayPal link.
    """
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https") or not opll_is_paypal_page_host(value):
        return False
    path = (parsed.path or "/").lower()
    if path.endswith((".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico",
                      ".css", ".js", ".woff", ".woff2")):
        return False
    return True


def opll_is_paypal_success_url(value: str, loose: bool = False) -> bool:
    if opll_is_paypal_approval_entry_url(value):
        return True
    return bool(loose and opll_is_paypal_page_url(value))


PAYPAL_COUNTRY_LOCALE = {
    "BR": ("BR", "pt_BR"),
    "US": ("US", "en_US"),
    "FR": ("FR", "fr_FR"),
    "JP": ("JP", "ja_JP"),
}


def opll_paypal_url_for_country(value: str, country: str = "BR") -> str:
    """Keep the real PayPal URL/token but force the PayPal page country/locale.

    This is used for BR PayPal extraction where the target is a real PayPal
    page and the visible PayPal method page should be switched to Brazil.
    """
    url = str(value or "").strip()
    if not url or not opll_is_paypal_page_url(url):
        return url
    country = str(country or "BR").strip().upper()
    country_x, locale_x = PAYPAL_COUNTRY_LOCALE.get(country, (country, "pt_BR" if country == "BR" else "en_US"))
    try:
        parsed = urlsplit(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["country.x"] = country_x
        query["locale.x"] = locale_x
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    except Exception:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}country.x={quote(country_x)}&locale.x={quote(locale_x)}"


def opll_is_ideal_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    return host == "ideal.nl" or host.endswith(".ideal.nl")


def opll_is_direct_ideal_transaction_url(value: str) -> bool:
    """Return True only for the final signed tx.ideal.nl transaction URL."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except Exception:
        return False
    host = str(parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or host != "tx.ideal.nl":
        return False
    if not re.fullmatch(r"/2/[A-Za-z0-9_-]+", parsed.path or ""):
        return False
    signature = parse_qs(parsed.query, keep_blank_values=True).get("sig") or []
    return bool(signature and str(signature[0] or "").strip())


def opll_extract_direct_ideal_url(value: str) -> str:
    """Normalize direct and pay.ideal.nl wrapped links to the signed tx.ideal.nl form."""
    initial = str(value or "").strip().replace("&amp;", "&")
    if not initial:
        return ""
    queue = [initial]
    seen: set[str] = set()
    while queue and len(seen) < 24:
        candidate = str(queue.pop(0) or "").strip().strip("\"'<>[]()")
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)

        if opll_is_direct_ideal_transaction_url(candidate):
            parsed = urlsplit(candidate)
            return urlunsplit(("https", "tx.ideal.nl", parsed.path, parsed.query, ""))

        decoded = unquote(candidate)
        if decoded != candidate:
            queue.append(decoded)

        try:
            parsed = urlsplit(candidate)
        except Exception:
            parsed = None
        if parsed is not None:
            host = str(parsed.hostname or "").lower()
            path = str(parsed.path or "")
            marker = "/transactions/"
            if host in {"pay.ideal.nl", "www.pay.ideal.nl"} and marker in path:
                encoded_inner = path.split(marker, 1)[1]
                inner = unquote(encoded_inner).strip()
                if inner:
                    inner_parts = urlsplit(inner)
                    inner_query = str(inner_parts.query or "")
                    outer_query = str(parsed.query or "")
                    if outer_query and not inner_query:
                        inner_query = outer_query
                    elif outer_query and "sig=" not in inner_query.lower():
                        inner_query = f"{inner_query}&{outer_query}"
                    inner = urlunsplit((
                        inner_parts.scheme,
                        inner_parts.netloc,
                        inner_parts.path,
                        inner_query,
                        "",
                    ))
                    queue.append(inner)

        for found in re.findall(r"https?://[^\s\"'<>]+", candidate):
            cleaned = found.rstrip(".,;)")
            if cleaned not in seen:
                queue.append(cleaned)
    return ""


def opll_is_kakao_pay_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    return any(hint in host for hint in ("kakao", "kakaopay", "nicepay"))


def opll_is_twint_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    host = (parsed.netloc or "").split("@")[-1].split(":")[0].lower()
    return (
        host in {"twint.ch", "twint.com"}
        or host.endswith(".twint.ch")
        or host.endswith(".twint.com")
    )


def opll_is_ignored_resource_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    ignored_hosts = {"stripe-camo.global.ssl.fastly.net", "files.stripe.com",
                     "q.stripe.com", "js.stripe.com", "m.stripe.network"}
    ignored_suffixes = (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico",
                        ".css", ".js", ".woff", ".woff2")
    if host in ignored_hosts or any(host.endswith(f".{item}") for item in ignored_hosts):
        return True
    return path.endswith(ignored_suffixes)


def opll_text_url_variants(value: str, max_decodes: int = 2) -> list[str]:
    variants: list[str] = []
    text = str(value or "")
    for _ in range(max(1, max_decodes + 1)):
        if text and text not in variants:
            variants.append(text)
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return variants


def opll_collect_urls(payload, urls: list[str] | None = None) -> list[str]:
    found = urls if urls is not None else []
    if isinstance(payload, str):
        for text in opll_text_url_variants(payload):
            for match in re.findall(r"https?://[^\s\"'<>]+", text):
                found.append(match.rstrip("),.;]"))
    elif isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("url", "return_url", "redirect_url", "redirect_to_url") and \
               isinstance(value, str) and opll_is_external_url(value):
                found.append(value)
            else:
                opll_collect_urls(value, found)
    elif isinstance(payload, list):
        for item in payload:
            opll_collect_urls(item, found)
    return found


def opll_unique_urls(urls: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in urls:
        url = str(item or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def opll_pick_paypal_url(urls: list[str], loose: bool = False) -> str:
    candidates = opll_unique_urls(urls)
    for item in candidates:
        if opll_is_paypal_ba_approve_url(item):
            return item
    for item in candidates:
        if opll_is_paypal_approval_entry_url(item):
            return item
    if loose:
        for item in candidates:
            if opll_is_paypal_page_url(item):
                return item
    return ""


def opll_extract_paypal_candidate_url(payload, loose: bool = False) -> str:
    return opll_pick_paypal_url(opll_collect_urls(payload), loose=loose)


def opll_extract_redirect_to_url(payload) -> str:
    if not isinstance(payload, dict):
        urls = opll_collect_urls(payload)
        return opll_pick_paypal_url(urls) or \
            next((item for item in urls if opll_is_paypal_page_url(item)), "")
    next_action = payload.get("next_action")
    if isinstance(next_action, dict) and next_action.get("type") == "redirect_to_url":
        redirect_to_url = next_action.get("redirect_to_url") or {}
        if isinstance(redirect_to_url, dict):
            url = str(redirect_to_url.get("url") or "").strip()
            if url:
                return url
    for key in ("setup_intent", "payment_intent"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = opll_extract_redirect_to_url(nested)
            if found:
                return found
    urls = opll_collect_urls(payload)
    return opll_pick_paypal_url(urls) or \
        next((item for item in urls if opll_is_paypal_page_url(item)), "")


def opll_get_nested(value, path: tuple[str, ...]):
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def opll_deep_first(value, keys: tuple[str, ...]):
    wanted = {str(key).lower() for key in keys}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in wanted and item not in (None, ""):
                return item
        for item in value.values():
            found = opll_deep_first(item, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = opll_deep_first(item, keys)
            if found not in (None, ""):
                return found
    return None


def opll_extract_client_secret(payload) -> str:
    direct = opll_deep_first(payload, ("client_secret", "clientSecret"))
    if isinstance(direct, str) and "_secret_" in direct:
        return direct.strip()
    text = json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list)) else str(payload or "")
    match = re.search(r"\b(?:pi|seti)_[A-Za-z0-9]+_secret_[A-Za-z0-9_]+", text)
    return match.group(0) if match else ""


def opll_extract_confirmation_token_id(payload) -> str:
    direct = opll_deep_first(payload, ("id", "confirmation_token", "confirmationToken"))
    if isinstance(direct, str) and direct.startswith("ctoken_"):
        return direct.strip()
    text = json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list)) else str(payload or "")
    match = re.search(r"\bctoken_[A-Za-z0-9_]+", text)
    return match.group(0) if match else ""


def opll_is_promptpay_instructions_url(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or "").strip())
    except Exception:
        return False
    return (parsed.netloc or "").lower() == "payments.stripe.com" and \
        (parsed.path or "").lower().startswith("/qr/instructions/")


def opll_extract_promptpay(payload) -> dict:
    """Extract PromptPay hosted instructions, QR payload and QR image from Stripe JSON."""
    result = {
        "promptpay_link": "",
        "promptpay_hosted_instructions_url": "",
        "promptpay_qr_data": "",
        "promptpay_qr_image_url": "",
        "promptpay_expires_at": 0,
        "source": "",
    }
    if not isinstance(payload, (dict, list)):
        return result

    bases = (
        ("next_action", "promptpay_display_qr_code"),
        ("next_action", "display_promptpay_qr_code"),
        ("payment_intent", "next_action", "promptpay_display_qr_code"),
        ("payment_intent", "next_action", "display_promptpay_qr_code"),
        ("promptpay_display_qr_code",),
        ("display_promptpay_qr_code",),
        ("promptpay",),
    )
    for base in bases:
        instructions = opll_get_nested(payload, base + ("hosted_instructions_url",))
        if isinstance(instructions, str) and instructions.strip():
            result["promptpay_hosted_instructions_url"] = instructions.strip()
            result["promptpay_link"] = instructions.strip()
            result["source"] = ".".join(base + ("hosted_instructions_url",))
        if not result["promptpay_qr_data"]:
            for key in ("data", "qr_data", "payload"):
                value = opll_get_nested(payload, base + (key,))
                if isinstance(value, str) and value.strip():
                    result["promptpay_qr_data"] = value.strip()
                    break
        if not result["promptpay_qr_image_url"]:
            for key in ("image_url_png", "image_url_svg", "image_url", "qr_code_url"):
                value = opll_get_nested(payload, base + (key,))
                if isinstance(value, str) and value.strip():
                    result["promptpay_qr_image_url"] = value.strip()
                    break
        if not result["promptpay_expires_at"]:
            for key in ("expires_at", "expiresAt", "expiration"):
                value = opll_get_nested(payload, base + (key,))
                parsed = opll_parse_epoch_seconds(value)
                if parsed:
                    result["promptpay_expires_at"] = parsed
                    break

    urls = opll_unique_urls(opll_collect_urls(payload))
    if not result["promptpay_hosted_instructions_url"]:
        instructions = next((item for item in urls if opll_is_promptpay_instructions_url(item)), "")
        if instructions:
            result["promptpay_hosted_instructions_url"] = instructions
            result["promptpay_link"] = instructions
            result["source"] = "recursive_instructions_url"
    if not result["promptpay_qr_image_url"]:
        result["promptpay_qr_image_url"] = next(
            (item for item in urls if "promptpay" in item.lower() and
             any((urlsplit(item).path or "").lower().endswith(ext)
                 for ext in (".png", ".svg", ".jpg", ".jpeg", ".webp"))),
            "",
        )
    if not result["promptpay_expires_at"]:
        result["promptpay_expires_at"], _ = opll_checkout_expires_at(payload)
    return result


def opll_merge_promptpay_extract(target: dict, source: dict) -> dict:
    target = dict(target or {})
    for key, value in (source or {}).items():
        if value and not target.get(key):
            target[key] = value
    instructions = str((source or {}).get("promptpay_hosted_instructions_url") or "").strip()
    if instructions:
        target["promptpay_hosted_instructions_url"] = instructions
        target["promptpay_link"] = instructions
    return target


def opll_is_pix_emv_payload(value: str) -> bool:
    text = str(value or "").strip()
    return text.startswith("000201") and "BR.GOV.BCB.PIX" in text.upper()


def opll_is_pix_instructions_url(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or "").strip())
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    return host == "payments.stripe.com" and path.startswith("/qr/instructions/")


def opll_pick_pix_instructions_url(urls: list[str]) -> str:
    for item in opll_unique_urls(urls or []):
        if opll_is_pix_instructions_url(item):
            return item
    return ""


def opll_is_openai_pay_or_checkout_url(url: str) -> bool:
    try:
        host = (urlsplit(str(url or "").strip()).netloc or "").lower()
    except Exception:
        return False
    return host in {"pay.openai.com", "checkout.stripe.com"}


def opll_is_pix_image_or_resource_url(url: str) -> bool:
    """Return True for Stripe image/CDN/logo resources that are not PIX instructions."""
    try:
        parsed = urlsplit(str(url or "").strip())
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if host in {"stripe-camo.global.ssl.fastly.net", "files.stripe.com", "q.stripe.com"}:
        return True
    if host.endswith(".ssl.fastly.net") and "stripe" in host:
        return True
    if any(path.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico")):
        return True
    if "/logo" in path or "/image" in path or "/icons/" in path:
        return True
    return False


def opll_pix_resource_decoded_url(url: str) -> str:
    decoded = opll_decode_stripe_camo_url(url)
    return decoded or str(url or "").strip()


def opll_is_pix_icon_resource_url(url: str) -> bool:
    """PIX payment-method icons/logos are not QR images."""
    text = opll_pix_resource_decoded_url(url).lower()
    if not text:
        return False
    markers = (
        "icon-pm-pix",
        "/payment-methods/",
        "/img/payment-methods/",
        "js.stripe.com/v3/fingerprinted/img/payment-methods",
        "/icons/",
        "/logo",
        "openai",
    )
    return any(marker in text for marker in markers)


def opll_is_real_pix_qr_image_url(url: str) -> bool:
    text = str(url or "").strip()
    if not text or not opll_is_external_url(text):
        return False
    if opll_is_pix_icon_resource_url(text):
        return False
    decoded = opll_pix_resource_decoded_url(text).lower()
    path = (urlsplit(decoded).path or "").lower()
    return (
        "qr" in decoded
        or "pix" in decoded and any(path.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".svg"))
        or "payments.stripe.com" in decoded and any(path.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".svg"))
    )


def opll_decode_stripe_camo_url(url: str) -> str:
    """Decode the original URL embedded in stripe-camo hex paths, if present."""
    try:
        parsed = urlsplit(str(url or "").strip())
        if (parsed.netloc or "").lower() != "stripe-camo.global.ssl.fastly.net":
            return ""
        parts = [part for part in (parsed.path or "").split("/") if part]
        if len(parts) < 2:
            return ""
        hex_part = parts[-1]
        if not re.fullmatch(r"[0-9a-fA-F]+", hex_part) or len(hex_part) % 2:
            return ""
        decoded = bytes.fromhex(hex_part).decode("utf-8", "ignore").strip()
        return decoded if decoded.startswith(("http://", "https://")) else ""
    except Exception:
        return ""


def opll_merge_pix_extract(target: dict, source: dict) -> dict:
    if not isinstance(target, dict):
        target = {}
    if not isinstance(source, dict):
        return target
    for key, value in source.items():
        if value and not target.get(key):
            target[key] = value
    # Always prefer the real Stripe QR instructions URL as the final PIX link.
    instruction = str(source.get("pix_hosted_instructions_url") or source.get("pix_instructions_url") or "").strip()
    if instruction and opll_is_pix_instructions_url(instruction):
        target["pix_hosted_instructions_url"] = instruction
        target["pix_instructions_url"] = instruction
        target["pix_link"] = instruction
    return target


def opll_extract_pix_link(payload) -> dict:
    """Extract PIX instructions URL / QR image / copia-e-cola payload from Stripe confirm or poll JSON.

    Primary target is payments.stripe.com/qr/instructions/... . The older
    pay.openai.com / checkout.stripe.com URL is preserved as pix_checkout_url.
    """
    result = {
        "pix_link": "",
        "pix_payload": "",
        "pix_qr_image_url": "",
        "pix_hosted_instructions_url": "",
        "pix_instructions_url": "",
        "pix_checkout_url": "",
        "pix_openai_pay_url": "",
        "pix_resource_url": "",
        "source": "",
    }

    def record_url(url: str, source: str = "") -> None:
        url = str(url or "").strip()
        if not url:
            return
        if opll_is_pix_instructions_url(url):
            result["pix_hosted_instructions_url"] = url
            result["pix_instructions_url"] = url
            result["pix_link"] = url
            result["source"] = result.get("source") or source or "pix_instructions_url"
            return
        if opll_is_pix_image_or_resource_url(url):
            decoded = opll_decode_stripe_camo_url(url)
            if decoded and opll_is_pix_instructions_url(decoded):
                record_url(decoded, source or "decoded_stripe_camo_instructions")
                return
            # Payment method icons/logos are diagnostics only; never QR.
            if opll_is_real_pix_qr_image_url(url):
                result["pix_qr_image_url"] = result.get("pix_qr_image_url") or url
            else:
                result["pix_resource_url"] = result.get("pix_resource_url") or url
            return
        if opll_is_openai_pay_or_checkout_url(url):
            result["pix_checkout_url"] = result.get("pix_checkout_url") or url
            if (urlsplit(url).netloc or "").lower() == "pay.openai.com":
                result["pix_openai_pay_url"] = result.get("pix_openai_pay_url") or url
            return
        # Non-instructions URLs are not final PIX links. Store only as diagnostic redirect.
        result["pix_redirect_url"] = result.get("pix_redirect_url") or url
        if not result.get("source"):
            result["source"] = source or "url"

    if isinstance(payload, str):
        text = payload.strip()
        if opll_is_pix_emv_payload(text):
            result["pix_payload"] = text
            result["source"] = "raw_pix_payload"
            return result
        urls = opll_unique_urls(opll_collect_urls(text) + ([text] if opll_is_external_url(text) else []))
        instruction = opll_pick_pix_instructions_url(urls)
        if instruction:
            record_url(instruction, "raw_text_instructions")
            return result
        for url in urls:
            record_url(url, "raw_text_url")
            break
        return result
    if not isinstance(payload, (dict, list)):
        return result

    url_paths = [
        ("next_action", "pix_display_qr_code", "hosted_instructions_url"),
        ("next_action", "display_pix_qr_code", "hosted_instructions_url"),
        ("payment_intent", "next_action", "pix_display_qr_code", "hosted_instructions_url"),
        ("payment_intent", "next_action", "display_pix_qr_code", "hosted_instructions_url"),
        ("pix_display_qr_code", "hosted_instructions_url"),
        ("display_pix_qr_code", "hosted_instructions_url"),
        ("pix", "hosted_instructions_url"),
        ("elements_session", "action", "redirect_to_url", "url"),
        ("action", "redirect_to_url", "url"),
        ("next_action", "redirect_to_url", "url"),
        ("payment_intent", "next_action", "redirect_to_url", "url"),
        ("setup_intent", "next_action", "redirect_to_url", "url"),
        ("pix", "redirect_url"),
        ("pix", "hosted_url"),
        ("pix", "qr_code_url"),
        ("pix", "url"),
    ]
    image_paths = [
        ("next_action", "pix_display_qr_code", "image_url"),
        ("next_action", "pix_display_qr_code", "image_url_png"),
        ("next_action", "pix_display_qr_code", "image_url_svg"),
        ("next_action", "display_pix_qr_code", "image_url"),
        ("next_action", "display_pix_qr_code", "image_url_png"),
        ("next_action", "display_pix_qr_code", "image_url_svg"),
        ("payment_intent", "next_action", "pix_display_qr_code", "image_url"),
        ("payment_intent", "next_action", "pix_display_qr_code", "image_url_png"),
        ("payment_intent", "next_action", "pix_display_qr_code", "image_url_svg"),
        ("payment_intent", "next_action", "display_pix_qr_code", "image_url"),
        ("payment_intent", "next_action", "display_pix_qr_code", "image_url_png"),
        ("payment_intent", "next_action", "display_pix_qr_code", "image_url_svg"),
        ("pix_display_qr_code", "image_url"),
        ("pix_display_qr_code", "image_url_png"),
        ("pix_display_qr_code", "image_url_svg"),
        ("display_pix_qr_code", "image_url"),
        ("display_pix_qr_code", "image_url_png"),
        ("display_pix_qr_code", "image_url_svg"),
        ("pix", "image_url"),
        ("pix", "image_url_png"),
        ("pix", "image_url_svg"),
        ("pix", "qr_image_url"),
    ]
    payload_paths = [
        ("next_action", "pix_display_qr_code", "data"),
        ("next_action", "display_pix_qr_code", "data"),
        ("payment_intent", "next_action", "pix_display_qr_code", "data"),
        ("payment_intent", "next_action", "display_pix_qr_code", "data"),
        ("pix_display_qr_code", "data"),
        ("display_pix_qr_code", "data"),
        ("pix", "data"),
        ("pix", "payload"),
        ("pix", "copia_e_cola"),
        ("pix", "br_code"),
    ]

    for path in url_paths:
        value = opll_get_nested(payload, path)
        if isinstance(value, str) and value.strip():
            record_url(value.strip(), ".".join(path))
    for path in image_paths:
        value = opll_get_nested(payload, path)
        if isinstance(value, str) and value.strip():
            if opll_is_real_pix_qr_image_url(value.strip()):
                result["pix_qr_image_url"] = value.strip()
                break
            result["pix_resource_url"] = result.get("pix_resource_url") or value.strip()
    for path in payload_paths:
        value = opll_get_nested(payload, path)
        if isinstance(value, str) and value.strip():
            result["pix_payload"] = value.strip()
            break

    candidates = opll_unique_urls(opll_collect_urls(payload))
    instruction = opll_pick_pix_instructions_url(candidates)
    if instruction:
        record_url(instruction, "recursive_instructions_url")
    elif not result.get("pix_link"):
        preferred = []
        for item in candidates:
            if opll_is_pix_image_or_resource_url(item):
                record_url(item, "recursive_image_resource")
                continue
            low = item.lower()
            if any(token in low for token in ("pix", "checkout", "stripe", "pay.openai.com", "pm-redirects")):
                preferred.append(item)
        if preferred:
            record_url(preferred[0], "recursive_url")
        elif candidates:
            record_url(candidates[0], "recursive_any_url")

    if not result["pix_payload"]:
        def walk_for_payload(value):
            if isinstance(value, str):
                return value.strip() if opll_is_pix_emv_payload(value) else ""
            if isinstance(value, dict):
                for item in value.values():
                    found = walk_for_payload(item)
                    if found:
                        return found
            elif isinstance(value, list):
                for item in value:
                    found = walk_for_payload(item)
                    if found:
                        return found
            return ""
        result["pix_payload"] = walk_for_payload(payload)
    return result


def opll_submission_attempt_failure_fields(submission) -> dict[str, str]:
    wanted = {"error", "code", "message", "reason", "failure_reason", "decline_code",
              "failure_code", "failure_message", "status", "state", "type",
              "payment_method_type", "confirm_error_reason", "confirm_error_code",
              "confirm_error_message"}
    found: dict[str, str] = {}

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key or "").strip()
                if normalized in wanted and normalized not in found:
                    if isinstance(item, (str, int, float, bool)):
                        text = str(item).strip()
                    elif isinstance(item, dict):
                        text = str(item.get("message") or item.get("code") or
                                   item.get("reason") or item.get("type") or "").strip()
                    else:
                        text = ""
                    if text:
                        found[normalized] = text[:240]
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    if isinstance(submission, dict):
        walk(submission)
    return found


def opll_compact_submission_failure(payload, ctx: dict) -> str:
    """Return the shortest useful Stripe submission failure summary."""
    submission = opll_find_submission_attempt(payload)
    fields = opll_submission_attempt_failure_fields(submission)
    parts: list[str] = []
    if isinstance(submission, dict):
        for label in ("state", "status", "reason", "failure_reason", "code", "decline_code",
                      "failure_code", "message", "failure_message", "error",
                      "confirm_error_reason", "confirm_error_code", "confirm_error_message"):
            value = fields.get(label)
            if value:
                parts.append(f"{label}={opll_short_error(value, 160)}")

        # Also surface nested error-like objects by path, because Stripe often
        # buries the useful detail below submission_attempt.*.error.
        found: list[str] = []

        def walk(value, path: str = "") -> None:
            if len(found) >= 8:
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    key_s = str(key)
                    p = f"{path}.{key_s}" if path else key_s
                    low = key_s.lower()
                    if any(token in low for token in ("error", "reason", "decline", "failure")):
                        if isinstance(item, (str, int, float, bool)):
                            found.append(f"{p}={opll_short_error(str(item), 180)}")
                        elif isinstance(item, dict):
                            msg = item.get("message") or item.get("reason") or item.get("code") or item.get("type")
                            if msg:
                                found.append(f"{p}={opll_short_error(str(msg), 180)}")
                    walk(item, p)
            elif isinstance(value, list):
                for idx, item in enumerate(value[:8]):
                    walk(item, f"{path}[{idx}]")

        walk(submission)
        for item in found:
            if item not in parts:
                parts.append(item)

    if not parts:
        return opll_stripe_payload_diagnostics(payload, ctx)
    return "; ".join(parts[:12])


def opll_find_submission_attempt(payload) -> dict:
    if isinstance(payload, dict):
        item = payload.get("submission_attempt")
        if isinstance(item, dict):
            return item
        for value in payload.values():
            found = opll_find_submission_attempt(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = opll_find_submission_attempt(value)
            if found:
                return found
    return {}


def opll_stripe_error_summary(prefix: str, response) -> str:
    try:
        payload = response.json() or {}
    except Exception:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        error = {}
    extra_fields = error.get("extra_fields") if isinstance(error.get("extra_fields"), dict) else {}
    parts = []
    for label, value in (
        ("code", error.get("code")),
        ("decline_code", error.get("decline_code")),
        ("type", error.get("type")),
        ("message", error.get("message")),
        ("payment_method_type", extra_fields.get("payment_method_type")),
        ("confirm_error_reason", extra_fields.get("confirm_error_reason")),
        ("confirm_error_code", extra_fields.get("confirm_error_code")),
        ("confirm_error_message", extra_fields.get("confirm_error_message")),
    ):
        if value is not None and value != "":
            parts.append(f"{label}={opll_short_error(str(value), 180)}")
    if parts:
        return f"{prefix}: " + ", ".join(parts)
    return f"{prefix}: {opll_short_error(response.text, 500)}"


def opll_stripe_payload_diagnostics(payload, ctx: dict) -> str:
    if not isinstance(payload, dict):
        return f"payload_type={type(payload).__name__}"
    keys = ",".join(sorted(payload.keys())[:12])
    urls = opll_collect_urls(payload)
    paypal_count = sum(1 for item in urls if opll_is_paypal_url(item))
    ba_count = sum(1 for item in urls if opll_is_paypal_ba_approve_url(item))
    ignored_count = sum(1 for item in urls if opll_is_ignored_resource_url(item))
    submission = opll_find_submission_attempt(payload)
    submission_state = str(submission.get("state") or "") if isinstance(submission, dict) else ""
    submission_fields = opll_submission_attempt_failure_fields(submission)
    submission_reason = opll_first_non_empty(submission_fields, "reason", "failure_reason",
                                              "decline_code", "failure_code", "code")
    submission_code = opll_first_non_empty(submission_fields, "code", "decline_code", "failure_code")
    submission_message = opll_first_non_empty(submission_fields, "message", "failure_message", "error")
    return (
        f"keys=[{keys}], urls={len(urls)}, paypal_urls={paypal_count}, "
        f"ba_approve_urls={ba_count}, ignored_resource_urls={ignored_count}, "
        f"submission_attempt={bool(submission)}, submission_state={submission_state or '未知'}, "
        f"submission_reason={submission_reason or '无'}, submission_code={submission_code or '无'}, "
        f"submission_message={submission_message or '无'}, ctx_session={ctx.get('elements_session_id') or ''}"
    )


def opll_stripe_payment_page_pix_extract(stripe: requests.Session, cs_id: str, stripe_pk: str,
                                          payment_locale: str = "pt-BR", timeout_seconds: int = 25,
                                          ctx: dict | None = None) -> dict:
    deadline = time.time() + max(1, timeout_seconds)
    _browser_locale, elements_locale = locale_parts(payment_locale)
    ctx = ctx or {}
    saved_mode = str(ctx.get("saved_payment_method_mode") or "never")
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": str(ctx.get("elements_session_id") or f"elements_session_{uuid.uuid4().hex[:11]}"),
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or uuid.uuid4()),
        "elements_session_client[locale]": elements_locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "key": stripe_pk,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
    }
    params["elements_options_client[saved_payment_method][enable_save]"] = saved_mode
    params["elements_options_client[saved_payment_method][enable_redisplay]"] = saved_mode
    last_pix: dict = {}
    last_err = ""
    while time.time() < deadline:
        response = stripe.get(
            f"https://api.stripe.com/v1/payment_pages/{cs_id}",
            params=params,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
        if response.status_code == 200:
            payload = response.json() or {}
            pix = opll_extract_pix_link(payload)
            last_pix = opll_merge_pix_extract(last_pix, pix)
            if last_pix.get("pix_hosted_instructions_url"):
                return last_pix
            submission = opll_find_submission_attempt(payload)
            if submission.get("state") == "requires_approval":
                raise OpllStripeRequiresApproval("payment page requires ChatGPT approval")
            if submission.get("state") == "failed":
                raise RuntimeError(f"stripe submission failed: {opll_compact_submission_failure(payload, ctx)}")
            last_err = opll_stripe_payload_diagnostics(payload, ctx)
        else:
            last_err = f"HTTP {response.status_code} {response.text[:120]}"
        time.sleep(1)
    if last_pix:
        last_pix["poll_error"] = f"PIX instructions poll timeout: {last_err}"
    return last_pix


def opll_stripe_payment_page_promptpay_extract(stripe: requests.Session, cs_id: str, stripe_pk: str,
                                               payment_locale: str = "th", timeout_seconds: int = 45,
                                               ctx: dict | None = None) -> dict:
    deadline = time.time() + max(1, timeout_seconds)
    _browser_locale, elements_locale = locale_parts(payment_locale)
    ctx = ctx or {}
    saved_mode = str(ctx.get("saved_payment_method_mode") or "never")
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": str(ctx.get("elements_session_id") or f"elements_session_{uuid.uuid4().hex[:11]}"),
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or uuid.uuid4()),
        "elements_session_client[locale]": elements_locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "key": stripe_pk,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
        "elements_options_client[saved_payment_method][enable_save]": saved_mode,
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_mode,
    }
    last_result: dict = {}
    last_err = ""
    while time.time() < deadline:
        response = stripe.get(
            f"https://api.stripe.com/v1/payment_pages/{cs_id}",
            params=params,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
        if response.status_code == 200:
            payload = response.json() or {}
            last_result = opll_merge_promptpay_extract(last_result, opll_extract_promptpay(payload))
            if (last_result.get("promptpay_hosted_instructions_url")
                    or last_result.get("promptpay_qr_data")
                    or last_result.get("promptpay_qr_image_url")):
                return last_result
            submission = opll_find_submission_attempt(payload)
            if submission.get("state") == "requires_approval":
                raise OpllStripeRequiresApproval("payment page requires ChatGPT approval")
            if submission.get("state") == "failed":
                raise RuntimeError(f"stripe submission failed: {opll_compact_submission_failure(payload, ctx)}")
            last_err = opll_stripe_payload_diagnostics(payload, ctx)
        else:
            last_err = f"HTTP {response.status_code} {response.text[:120]}"
        time.sleep(1)
    if last_result:
        last_result["poll_error"] = f"PromptPay instructions poll timeout: {last_err}"
    return last_result


def opll_stripe_payment_page_redirect_url(stripe: requests.Session, cs_id: str, stripe_pk: str,
                                           payment_locale: str = "en", timeout_seconds: int = 45,
                                           ctx: dict | None = None) -> str:
    deadline = time.time() + max(1, timeout_seconds)
    _browser_locale, elements_locale = locale_parts(payment_locale)
    ctx = ctx or {}
    saved_mode = str(ctx.get("saved_payment_method_mode") or "never")
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": str(ctx.get("elements_session_id") or f"elements_session_{uuid.uuid4().hex[:11]}"),
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or uuid.uuid4()),
        "elements_session_client[locale]": elements_locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": saved_mode,
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_mode,
        "key": stripe_pk,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
    }
    last_err = ""
    while time.time() < deadline:
        response = stripe.get(
            f"https://api.stripe.com/v1/payment_pages/{cs_id}",
            params=params,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
        if response.status_code == 200:
            payload = response.json() or {}
            redirect_url = opll_extract_redirect_to_url(payload)
            if redirect_url:
                return redirect_url
            submission = opll_find_submission_attempt(payload)
            if submission.get("state") == "requires_approval":
                raise OpllStripeRequiresApproval("payment page requires ChatGPT approval")
            if submission.get("state") == "failed":
                raise RuntimeError(f"stripe submission failed: {opll_compact_submission_failure(payload, ctx)}")
            last_err = opll_stripe_payload_diagnostics(payload, ctx)
        else:
            last_err = f"HTTP {response.status_code} {response.text[:120]}"
        time.sleep(1)
    raise RuntimeError(f"redirect url resolution timeout: {last_err}")


def opll_resolve_external_redirect(stripe: requests.Session, redirect_url: str,
                                    preferred_hosts: tuple[str, ...] = ("paypal.com",),
                                    loose_paypal: bool = False) -> str:
    current = str(redirect_url or "").strip()
    for _ in range(5):
        if not current:
            return ""
        if opll_is_paypal_success_url(current, loose=loose_paypal):
            return current
        host = (urlsplit(current).netloc or "").lower()
        if preferred_hosts and any(host == item or host.endswith(f".{item}") for item in preferred_hosts):
            return current
        try:
            response = stripe.get(current, allow_redirects=False, timeout=PAY_LONG_LINK_TIMEOUT)
        except Exception:
            return current
        if response.status_code not in (301, 302, 303, 307, 308):
            candidate = opll_extract_paypal_candidate_url(getattr(response, "text", ""), loose=loose_paypal)
            if candidate:
                return candidate
            return current
        location = str(response.headers.get("Location") or "").strip()
        if not location:
            return current
        current = urljoin(current, location)
    return current


def opll_resolve_pix_instructions_url(stripe: requests.Session, *candidate_urls: str) -> str:
    """Follow Stripe/OpenAI checkout URLs and scrape redirects/HTML for payments.stripe.com/qr/instructions."""
    seen: set[str] = set()
    queue: list[str] = []
    for item in candidate_urls:
        url = str(item or "").strip()
        if url and url not in seen:
            seen.add(url)
            queue.append(url)
    for start_url in list(queue):
        if opll_is_pix_instructions_url(start_url):
            return start_url
    while queue and len(seen) < 24:
        current = queue.pop(0)
        if not current:
            continue
        if opll_is_pix_instructions_url(current):
            return current
        try:
            response = stripe.get(current, allow_redirects=False, timeout=PAY_LONG_LINK_TIMEOUT)
        except Exception:
            continue
        location = str(response.headers.get("Location") or "").strip()
        if location:
            location = urljoin(current, location)
            if opll_is_pix_instructions_url(location):
                return location
            if location not in seen:
                seen.add(location)
                queue.append(location)
        body = getattr(response, "text", "") or ""
        urls = opll_unique_urls(opll_collect_urls(body))
        instruction = opll_pick_pix_instructions_url(urls)
        if instruction:
            return instruction
        for url in urls[:8]:
            if opll_is_pix_image_or_resource_url(url):
                continue
            if url not in seen and any(host in url.lower() for host in ("stripe.com", "openai.com", "pm-redirects")):
                seen.add(url)
                queue.append(url)
    return ""


def opll_hydrate_pix_artifacts(stripe: requests.Session, proxy_url: str,
                                initial: dict | None = None,
                                *candidate_urls: str) -> dict:
    """Hydrate PIX instructions, copia-e-cola and QR fields from hosted pages."""
    pix = opll_merge_pix_extract({}, dict(initial or {}))

    def merge_value(value) -> None:
        nonlocal pix
        if value in (None, ""):
            return
        pix = opll_merge_pix_extract(pix, opll_extract_pix_link(value))
        if isinstance(value, str):
            text = value.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
            for match in re.finditer(
                    r"000201[A-Za-z0-9.*+:/?&=_-]{8,700}?6304[0-9A-Fa-f]{4}", text):
                payload = match.group(0)
                if opll_is_pix_emv_payload(payload):
                    pix = opll_merge_pix_extract(pix, {
                        "pix_payload": payload,
                        "source": "hosted_html_pix_payload",
                    })
                    break
            for url in opll_collect_urls(text):
                pix = opll_merge_pix_extract(pix, opll_extract_pix_link(url))

    queue = opll_unique_urls([
        *[str(item or "").strip() for item in candidate_urls],
        str(pix.get("pix_hosted_instructions_url") or ""),
        str(pix.get("pix_instructions_url") or ""),
        str(pix.get("pix_checkout_url") or ""),
        str(pix.get("pix_openai_pay_url") or ""),
        str(pix.get("pix_redirect_url") or ""),
    ])
    seen: set[str] = set()
    for url in queue[:12]:
        if not opll_is_external_url(url) or url in seen:
            continue
        seen.add(url)
        merge_value(url)
        try:
            response = stripe.get(url, allow_redirects=True, timeout=PAY_LONG_LINK_TIMEOUT)
        except Exception:
            continue
        merge_value(str(getattr(response, "url", "") or ""))
        if int(getattr(response, "status_code", 599) or 599) < 400:
            merge_value(str(getattr(response, "text", "") or ""))

    resolved = opll_resolve_pix_instructions_url(
        stripe,
        str(pix.get("pix_hosted_instructions_url") or ""),
        str(pix.get("pix_instructions_url") or ""),
        str(pix.get("pix_checkout_url") or ""),
        str(pix.get("pix_openai_pay_url") or ""),
        str(pix.get("pix_redirect_url") or ""),
        *candidate_urls,
    )
    if resolved:
        pix = opll_merge_pix_extract(pix, {
            "pix_link": resolved,
            "pix_hosted_instructions_url": resolved,
            "pix_instructions_url": resolved,
            "source": "pix2_http_hydration",
        })

    try:
        browser_enabled = str(
            os.environ.get("CHATGPT_PIX2_PLAYWRIGHT_HYDRATE", "true")
        ).strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        browser_enabled = True
    needs_browser = browser_enabled and sync_playwright is not None and bool(
        pix.get("pix_hosted_instructions_url")
        or pix.get("pix_instructions_url")
        or pix.get("pix_checkout_url")
        or candidate_urls
    ) and (
        not pix.get("pix_payload")
        or not opll_is_real_pix_qr_image_url(str(pix.get("pix_qr_image_url") or ""))
        or not pix.get("pix_hosted_instructions_url")
    )
    if needs_browser:
        start_url = str(
            pix.get("pix_hosted_instructions_url")
            or pix.get("pix_instructions_url")
            or pix.get("pix_checkout_url")
            or next((item for item in candidate_urls if opll_is_external_url(str(item or ""))), "")
        ).strip()
        if start_url:
            try:
                timeout_ms = max(5000, min(
                    int(os.environ.get("CHATGPT_PIX2_PLAYWRIGHT_TIMEOUT_MS", "25000")), 60000))
                wait_ms = max(1000, min(
                    int(os.environ.get("CHATGPT_PIX2_PLAYWRIGHT_WAIT_MS", "12000")), 60000))
                with sync_playwright() as playwright:
                    launch_options = {
                        "headless": True,
                        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                    }
                    proxy_options = opll_playwright_proxy_options(proxy_url)
                    if proxy_options:
                        launch_options["proxy"] = proxy_options
                    browser = opll_launch_playwright_chromium(playwright, launch_options)
                    context = browser.new_context(
                        locale="pt-BR",
                        user_agent=PIX_USER_AGENT,
                        viewport={"width": 1365, "height": 900},
                        extra_http_headers={"accept-language": "pt-BR,pt;q=0.9,en;q=0.8"},
                    )
                    page = context.new_page()
                    page.set_default_timeout(timeout_ms)
                    page.on("request", lambda request: merge_value(request.url))

                    def capture_response(response) -> None:
                        merge_value(response.url)
                        try:
                            content_type = str(response.headers.get("content-type") or "").lower()
                            if re.search(r"json|text|html|javascript", content_type):
                                merge_value(response.text())
                        except Exception:
                            pass

                    page.on("response", capture_response)
                    try:
                        page.goto(start_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    except Exception:
                        pass
                    page.wait_for_timeout(wait_ms)
                    merge_value(page.url)
                    try:
                        merge_value(page.content())
                    except Exception:
                        pass
                    context.close()
                    browser.close()
            except Exception as exc:
                pix["pix_hydration_error"] = opll_short_error(str(exc), 500)

    final_resolved = str(
        pix.get("pix_hosted_instructions_url")
        or pix.get("pix_instructions_url")
        or ""
    ).strip()
    if not final_resolved:
        final_resolved = opll_resolve_pix_instructions_url(
            stripe,
            str(pix.get("pix_checkout_url") or ""),
            str(pix.get("pix_openai_pay_url") or ""),
            str(pix.get("pix_redirect_url") or ""),
            *candidate_urls,
        )
        if final_resolved:
            pix = opll_merge_pix_extract(pix, {
                "pix_link": final_resolved,
                "pix_hosted_instructions_url": final_resolved,
                "pix_instructions_url": final_resolved,
                "source": "pix2_browser_hydration",
            })
    return pix


def opll_to_openai_pay_url(stripe_hosted_url: str) -> str:
    url = str(stripe_hosted_url or "").strip()
    if not url:
        return ""
    if url.startswith("https://checkout.stripe.com"):
        return "https://pay.openai.com" + url[len("https://checkout.stripe.com"):]
    parsed = urlsplit(url)
    if parsed.netloc.lower() == "checkout.stripe.com":
        return urlunsplit((parsed.scheme or "https", "pay.openai.com", parsed.path, parsed.query, parsed.fragment))
    return url


def opll_stripe_checkout_long_url(cs_id: str, country: str, processor_entity: str = "") -> str:
    return (
        f"https://checkout.stripe.com/c/pay/{cs_id}"
        f"?returned_from_redirect=true&ui_mode=custom&return_url="
        f"{quote(opll_chatgpt_success_return_url(cs_id, country, processor_entity), safe='')}"
    )


def opll_stripe_confirm_return_url(cs_id: str, checkout: dict, stripe_hosted_url: str) -> str:
    hosted_url = opll_to_openai_pay_url(stripe_hosted_url) or opll_stripe_checkout_long_url(
        cs_id, checkout["billing_country"], checkout.get("processor_entity", ""))
    if "pay.openai.com/" in hosted_url or "checkout.stripe.com/" in hosted_url:
        parsed = urlsplit(hosted_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("success_return_url",
                         opll_chatgpt_success_return_url(cs_id, checkout["billing_country"],
                                                          checkout.get("processor_entity", "")))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    return hosted_url


def opll_stripe_confirm(stripe: requests.Session, cs_id: str, pm_id: str, stripe_pk: str,
                         init_payload: dict, ctx: dict, checkout: dict, stripe_hosted_url: str,
                         payment_method_type: str = "paypal") -> dict:
    is_pix = str(payment_method_type or "").lower() == "pix"
    if is_pix:
        # Follow the live checkout URL back through pay.openai.com. This matches
        # the standalone PIX extractor and is closer to the browser page than a
        # hard-coded /confirm path.
        return_url = opll_to_openai_pay_url(stripe_hosted_url) or f"https://pay.openai.com/c/pay/{cs_id}/confirm"
    else:
        return_url = opll_stripe_confirm_return_url(cs_id, checkout, stripe_hosted_url)
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    stripe_version = str(ctx.get("stripe_version") or STRIPE_VERSION_FULL)
    expected_amount = str(ctx.get("checkout_amount") or opll_expected_amount(init_payload) or "0")
    body = {
        "guid": uuid.uuid4().hex if is_pix else str(ctx.get("guid") or uuid.uuid4()),
        "muid": uuid.uuid4().hex if is_pix else str(ctx.get("muid") or uuid.uuid4()),
        "sid": uuid.uuid4().hex if is_pix else str(ctx.get("sid") or uuid.uuid4()),
        "payment_method": pm_id,
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "version": runtime_version,
        "expected_amount": expected_amount,
        "expected_payment_method_type": payment_method_type,
        "return_url": return_url,
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[locale]": str(ctx.get("locale") or "en"),
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "consent[terms_of_service]": "accepted",
        "key": stripe_pk,
        "_stripe_version": stripe_version,
    }
    if is_pix:
        # Match the newer standalone PIX extractor exactly here: keep
        # payment_intent_creation_flow=deferred and saved_payment_method=never,
        # and do not forward setup_future_usage / payment_method_options. The
        # older 0-BRL mutation caused approve=approved followed by Stripe
        # submission_attempt=failed in the poll stage.
        pass
    response = stripe.post(
        f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
        data=body,
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        summary = opll_stripe_error_summary("stripe confirm failed", response)
        if "payment_method_types_mismatch" in summary:
            summary += f"; {opll_payment_method_diagnostics(init_payload)}"
        raise RuntimeError(summary)
    return response.json() or {}


def opll_oaics_customer_session_secret(*payloads) -> str:
    for payload in payloads:
        value = opll_deep_first(payload, (
            "customer_session_client_secret",
            "customerSessionClientSecret",
            "client_secret",
            "clientSecret",
        ))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def opll_oaics_stripe_elements_init(stripe: requests.Session, checkout: dict,
                                    *state_payloads,
                                    payment_locale: str = "en",
                                    ctx: dict | None = None,
                                    payment_method_type: str = "paypal") -> dict:
    """Stripe Elements session for OpenAI custom checkout (oaics_*).

    This mirrors the working OAICS chain:
      checkout/update zero -> Elements session -> pm=['card','paypal'] -> taxes -> token/confirm.
    The previous implementation sent `type=deferred_intent` by itself; Stripe now expects
    the matching `deferred_intent[...]` block and the custom checkout session id.
    """
    checkout_id = str((checkout or {}).get("checkout_session_id") or
                      (checkout or {}).get("checkout_id") or
                      (checkout or {}).get("cs_id") or "").strip()
    stripe_pk = opll_stripe_key_for_checkout(checkout)
    raw_checkout = (checkout or {}).get("raw_checkout") if isinstance((checkout or {}).get("raw_checkout"), dict) else {}
    customer_secret = opll_oaics_customer_session_secret(raw_checkout, checkout, *state_payloads)
    if not customer_secret:
        raise RuntimeError(f"OAICS elements init missing customer_session_client_secret for {checkout_id}")
    _browser_locale, elements_locale = locale_parts(payment_locale)
    local_ctx = dict(ctx or {})

    def _clean_amount(value) -> str:
        text = str(value if value is not None else "").strip()
        if text.lower() in {"", "none", "null", "nan"}:
            return ""
        return text

    amount = _clean_amount(local_ctx.get("checkout_amount") or local_ctx.get("amount"))
    currency_sent = str(
        local_ctx.get("currency")
        or (checkout or {}).get("currency")
        or ""
    ).strip().lower()
    for payload in (raw_checkout, checkout, *state_payloads):
        if not amount:
            found_amount, _found_source = opll_stripe_amount_info(payload)
            amount = _clean_amount(found_amount)
        if not currency_sent:
            found_currency = opll_deep_first(payload, ("currency", "billing_currency", "currency_code"))
            if isinstance(found_currency, str) and found_currency.strip():
                currency_sent = found_currency.strip().lower()
        if amount and currency_sent:
            break
    amount = amount or "0"
    currency_sent = currency_sent or "eur"

    pm_type = str(payment_method_type or "paypal").strip().lower() or "paypal"
    requested_methods: list[str] = ["card"]
    if pm_type not in requested_methods:
        requested_methods.append(pm_type)

    params = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "client_betas[2]": "disable_deferred_intent_client_validation_beta_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": amount,
        "deferred_intent[currency]": currency_sent,
        "deferred_intent[setup_future_usage]": "off_session",
        "currency": currency_sent,
        "customer_session_client_secret": customer_secret,
        "key": stripe_pk,
        "_stripe_version": str(local_ctx.get("stripe_version") or STRIPE_VERSION_FULL),
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": str(local_ctx.get("stripe_js_id") or uuid.uuid4()),
        "locale": elements_locale,
        "type": "deferred_intent",
        "checkout_session_id": checkout_id,
    }
    for index, method in enumerate(requested_methods):
        params[f"deferred_intent[payment_method_types][{index}]"] = method
    if local_ctx.get("elements_session_id"):
        params["session_id"] = str(local_ctx.get("elements_session_id"))

    def get_elements(payload: dict):
        return stripe.get(
            "https://api.stripe.com/v1/elements/sessions",
            params=payload,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )

    stripped_params: list[str] = []
    response = get_elements(params)
    for _ in range(10):
        if response.status_code < 400:
            break
        try:
            error = (response.json() or {}).get("error") or {}
        except Exception:
            error = {}
        if not isinstance(error, dict):
            break
        code = str(error.get("code") or "").strip()
        param = str(error.get("param") or "").strip()
        message = str(error.get("message") or "").lower()
        remove_keys: list[str] = []
        if code == "parameter_unknown" and param:
            remove_keys = [
                key for key in list(params)
                if key == param or key.startswith(f"{param}[")
            ]
        elif param == "type" and "type" in params and "deferred_intent" in message:
            # Some Stripe edges infer the deferred intent from deferred_intent[...] and reject explicit type.
            remove_keys = ["type"]
        if not remove_keys:
            break
        for key in remove_keys:
            if key in params:
                params.pop(key, None)
                stripped_params.append(key)
        response = get_elements(params)
    if response.status_code >= 400:
        stripped_hint = f"; stripped_params={stripped_params}" if stripped_params else ""
        sent_hint = f"; sent_amount={amount}; sent_currency={currency_sent}; requested_pm={requested_methods}"
        raise RuntimeError(f"OAICS Stripe elements init failed: {opll_http_error_detail(response)}{stripped_hint}{sent_hint}")
    payload = response.json() or {}
    if isinstance(payload, dict):
        methods = opll_collect_payment_method_types(payload)
        if methods:
            payload.setdefault("payment_method_types", methods)
        payload["_oaics_elements_amount_sent"] = amount
        payload["_oaics_elements_currency_sent"] = currency_sent
        payload["_oaics_elements_requested_payment_method_types"] = requested_methods
        if stripped_params:
            payload["_oaics_elements_stripped_params"] = stripped_params
    return payload if isinstance(payload, dict) else {"payload": payload}

def opll_stripe_create_paypal_confirmation_token(stripe: requests.Session, checkout: dict,
                                                 ctx: dict, billing: dict,
                                                 stripe_pk: str = "") -> tuple[str, dict]:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    stripe_version = str(ctx.get("stripe_version") or STRIPE_VERSION_FULL)
    checkout_id = str((checkout or {}).get("checkout_session_id") or
                      (checkout or {}).get("checkout_id") or
                      (checkout or {}).get("cs_id") or "")
    safe_email = opll_sanitize_billing_email(
        billing.get("email") or "buyer@example.com",
        billing.get("first_name") or "John",
        billing.get("last_name") or "Doe",
        billing.get("country") or "US",
    )
    browser_ua = str(
        ctx.get("browser_user_agent")
        or getattr(stripe, "headers", {}).get("User-Agent")
        or DEFAULT_USER_AGENT
    )
    acceptance_ip = opll_customer_acceptance_ip(stripe, ctx, checkout, billing)
    if not acceptance_ip:
        raise RuntimeError(
            "OAICS confirmation_tokens missing checkout exit IP: "
            "could not resolve proxy public IP for mandate_data customer_acceptance"
        )
    ctx["customer_acceptance_ip"] = acceptance_ip
    body = {
        "payment_method_data[type]": "paypal",
        "payment_method_data[billing_details][name]": billing.get("name") or "John Doe",
        "payment_method_data[billing_details][email]": safe_email,
        "payment_method_data[billing_details][phone]": billing.get("phone") or "",
        "payment_method_data[billing_details][address][country]": billing.get("country") or "US",
        "payment_method_data[billing_details][address][line1]": billing.get("line1") or "",
        "payment_method_data[billing_details][address][city]": billing.get("city") or "",
        "payment_method_data[billing_details][address][postal_code]": billing.get("postal_code") or "",
        "payment_method_data[billing_details][address][state]": billing.get("state") or "",
        "payment_method_data[payment_user_agent]": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "payment_method_data[guid]": str(ctx.get("guid") or uuid.uuid4()),
        "payment_method_data[muid]": str(ctx.get("muid") or uuid.uuid4()),
        "payment_method_data[sid]": str(ctx.get("sid") or uuid.uuid4()),
        "setup_future_usage": "off_session",
        "mandate_data[customer_acceptance][type]": "online",
        "mandate_data[customer_acceptance][online][user_agent]": browser_ua,
        "mandate_data[customer_acceptance][online][ip_address]": acceptance_ip,
        "client_context[currency]": str(ctx.get("currency") or (checkout or {}).get("currency") or "").lower(),
        "client_context[mode]": "subscription",
        "client_attribution_metadata[client_session_id]": str(ctx.get("stripe_js_id") or ""),
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[checkout_config_id]": str(ctx.get("config_id") or ""),
        "client_attribution_metadata[elements_session_id]": str(ctx.get("elements_session_id") or ""),
        "client_attribution_metadata[elements_session_config_id]": str(ctx.get("elements_session_config_id") or ""),
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "key": stripe_pk or opll_stripe_key_for_checkout(checkout),
        "_stripe_version": stripe_version,
    }
    if billing.get("line2"):
        body["payment_method_data[billing_details][address][line2]"] = billing.get("line2") or ""
    response = stripe.post(
        "https://api.stripe.com/v1/confirmation_tokens",
        data=body,
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"OAICS confirmation_tokens failed: {opll_http_error_detail(response)}")
    payload = response.json() or {}
    token_id = opll_extract_confirmation_token_id(payload)
    if not token_id:
        raise RuntimeError(f"OAICS confirmation_tokens missing ctoken id: {str(payload)[:500]}")
    return token_id, payload if isinstance(payload, dict) else {"payload": payload}


def opll_chatgpt_checkout_confirm_with_token(access_token: str, checkout: dict,
                                             confirmation_token: str,
                                             proxy_url: str = "",
                                             chatgpt_cookie: str = "",
                                             selected_payment_method_type: str = "paypal",
                                             browser_profile: str = "") -> dict:
    checkout_id = str((checkout or {}).get("checkout_session_id") or
                      (checkout or {}).get("checkout_id") or
                      (checkout or {}).get("cs_id") or "").strip()
    entity = str((checkout or {}).get("processor_entity") or
                 opll_processor_entity_for_country((checkout or {}).get("billing_country") or "DE")).strip()
    if not checkout_id:
        raise RuntimeError("OAICS checkout/confirm missing checkout_session_id")
    session = opll_build_chatgpt_session(
        access_token,
        proxy_url,
        chatgpt_cookie=chatgpt_cookie,
        browser_profile=browser_profile or str((checkout or {}).get("browser_profile") or ""),
    )
    response = session.post(
        "https://chatgpt.com/backend-api/payments/checkout/confirm",
        json={
            "checkout_session_id": checkout_id,
            "confirm_token": confirmation_token,
            "confirmation_token": confirmation_token,
            "selected_payment_method_type": str(selected_payment_method_type or "paypal").lower(),
        },
        headers={
            "Referer": f"https://chatgpt.com/checkout/{entity}/{checkout_id}",
            "x-openai-target-path": "/backend-api/payments/checkout/confirm",
            "x-openai-target-route": "/backend-api/payments/checkout/confirm",
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    try:
        payload = response.json() or {}
    except Exception:
        payload = {"raw": response.text[:500]}
    if response.status_code >= 400:
        raise RuntimeError(f"OAICS checkout/confirm failed: HTTP {response.status_code} {opll_short_error(str(payload), 700)}")
    return payload if isinstance(payload, dict) else {"payload": payload}


def opll_stripe_confirm_intent_with_confirmation_token(stripe: requests.Session,
                                                       client_secret: str,
                                                       confirmation_token: str,
                                                       checkout: dict,
                                                       ctx: dict,
                                                       stripe_pk: str = "",
                                                       return_url: str = "") -> dict:
    secret = str(client_secret or "").strip()
    if "_secret_" not in secret:
        raise RuntimeError(f"OAICS intent confirm missing client_secret: {secret[:80]}")
    intent_id = secret.split("_secret_", 1)[0]
    if intent_id.startswith("pi_"):
        endpoint = f"https://api.stripe.com/v1/payment_intents/{intent_id}/confirm"
    elif intent_id.startswith("seti_"):
        endpoint = f"https://api.stripe.com/v1/setup_intents/{intent_id}/confirm"
    else:
        raise RuntimeError(f"OAICS intent confirm unsupported client_secret prefix: {intent_id[:20]}")
    checkout_id = str((checkout or {}).get("checkout_session_id") or
                      (checkout or {}).get("checkout_id") or
                      (checkout or {}).get("cs_id") or "")
    body = {
        "return_url": return_url or opll_chatgpt_success_return_url(
            checkout_id,
            (checkout or {}).get("billing_country") or "DE",
            (checkout or {}).get("processor_entity") or "",
        ),
        "confirmation_token": confirmation_token,
        "client_secret": secret,
        "key": stripe_pk or opll_stripe_key_for_checkout(checkout),
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
        "client_attribution_metadata[client_session_id]": str(ctx.get("stripe_js_id") or ""),
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[checkout_config_id]": str(ctx.get("config_id") or ""),
        "client_attribution_metadata[elements_session_id]": str(ctx.get("elements_session_id") or ""),
        "client_attribution_metadata[elements_session_config_id]": str(ctx.get("elements_session_config_id") or ""),
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
    }
    response = stripe.post(endpoint, data=body, timeout=PAY_LONG_LINK_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"OAICS intent confirm failed: {opll_http_error_detail(response)}")
    payload = response.json() or {}
    return payload if isinstance(payload, dict) else {"payload": payload}


def opll_playwright_proxy_options(proxy_url: str) -> dict | None:
    raw = str(proxy_url or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if not parsed.hostname or not parsed.port:
        return None
    scheme = (parsed.scheme or "http").lower()
    if scheme == "socks5h":
        scheme = "socks5"
    if scheme == "socks4a":
        scheme = "socks4"
    proxy = {"server": f"{scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def opll_launch_playwright_chromium(playwright, launch_options: dict):
    executable_path = str(os.environ.get("PIX_CHROMIUM_PATH") or "").strip()
    attempts: list[tuple[str, dict]] = []
    if executable_path and os.path.isfile(executable_path):
        attempts.append((f"executable_path={executable_path}", {**launch_options, "executable_path": executable_path}))
    for channel in ("chrome", "msedge"):
        attempts.append((f"channel={channel}", {**launch_options, "channel": channel}))
    attempts.append(("playwright-bundled", dict(launch_options)))
    errors: list[str] = []
    for label, opts in attempts:
        try:
            return playwright.chromium.launch(**opts)
        except Exception as exc:
            errors.append(f"{label}: {opll_short_error(str(exc), 160)}")
    raise RuntimeError("Playwright Chromium launch failed: " + " | ".join(errors[-4:]))



def opll_select_pix_payment_method(page, timeout_ms: int = 15000) -> str:
    """Select PIX on Stripe Hosted Checkout across current and legacy DOM layouts."""
    action_timeout = max(2000, min(int(timeout_ms or 15000), 8000))
    errors: list[str] = []
    dispatched = ""

    def remember(label: str, exc: Exception) -> None:
        text = " ".join(str(exc or "").split())
        errors.append(f"{label}:{text[:120]}")
        if len(errors) > 16:
            del errors[:-16]

    def pix_selected(scope) -> bool:
        selectors = [
            'input[type="radio"][value="pix"]:checked',
            'input[type="radio"][id*="pix"]:checked',
            'input[type="radio"][id*="PIX"]:checked',
            'input[type="radio"][name*="pix"]:checked',
            'input[type="radio"][name*="PIX"]:checked',
            '[role="radio"][aria-checked="true"]:has-text("Pix")',
            '[role="radio"][aria-checked="true"]:has-text("PIX")',
        ]
        for selector in selectors:
            try:
                if scope.locator(selector).count() > 0:
                    return True
            except Exception:
                pass
        try:
            tax_inputs = scope.locator('input[name="taxId"]')
            for index in range(min(tax_inputs.count(), 3)):
                if tax_inputs.nth(index).is_visible():
                    return True
        except Exception:
            pass
        return False

    def try_action(scope, selector: str, use_check: bool, label: str) -> str:
        nonlocal dispatched
        try:
            locator = scope.locator(selector)
            count = locator.count()
        except Exception as exc:
            remember(label + ":locate", exc)
            return ""
        for index in range(min(count, 3)):
            item = locator.nth(index)
            try:
                item.scroll_into_view_if_needed(timeout=action_timeout)
            except Exception:
                pass
            try:
                if use_check:
                    item.check(force=True, timeout=action_timeout)
                else:
                    item.click(timeout=action_timeout)
                dispatched = label
                page.wait_for_timeout(350)
                if pix_selected(scope):
                    return label
            except Exception as exc:
                remember(label + ":normal", exc)
                if not use_check:
                    try:
                        item.click(force=True, timeout=action_timeout)
                        dispatched = label + ":force"
                        page.wait_for_timeout(350)
                        if pix_selected(scope):
                            return dispatched
                    except Exception as force_exc:
                        remember(label + ":force", force_exc)
        return ""

    frames = list(getattr(page, "frames", []) or [page])
    input_selectors = [
        'input[type="radio"][value="pix"]',
        'input[type="radio"][id*="pix"], input[type="radio"][id*="PIX"]',
        'input[type="radio"][name*="pix"], input[type="radio"][name*="PIX"]',
    ]
    clickable_selectors = [
        '[role="radio"][aria-label*="Pix"], [role="radio"][aria-label*="pix"]',
        'label[for*="pix"], label[for*="PIX"]',
        '[data-testid*="pix"], [data-testid*="PIX"]',
    ]

    for frame_index, scope in enumerate(frames):
        if pix_selected(scope):
            return f"already-selected:frame-{frame_index}"
        for selector in input_selectors:
            used = try_action(scope, selector, True, f"radio:frame-{frame_index}:{selector}")
            if used:
                return used
        for selector in clickable_selectors:
            used = try_action(scope, selector, False, f"container:frame-{frame_index}:{selector}")
            if used:
                return used

        label_nodes = []
        try:
            by_id = scope.locator("#payment-method-label-pix")
            if by_id.count() > 0:
                label_nodes.append(by_id.first)
        except Exception as exc:
            remember(f"label-id:frame-{frame_index}", exc)
        try:
            by_text = scope.get_by_text("Pix", exact=True)
            if by_text.count() > 0:
                label_nodes.append(by_text.first)
        except Exception as exc:
            remember(f"label-text:frame-{frame_index}", exc)

        for label_index, label_node in enumerate(label_nodes):
            ancestor_selectors = [
                "xpath=ancestor::*[@role='radio'][1]",
                "xpath=ancestor::label[1]",
                "xpath=ancestor::button[1]",
                "xpath=ancestor::*[@data-testid][1]",
                "xpath=..",
            ]
            for ancestor_selector in ancestor_selectors:
                try:
                    target = label_node.locator(ancestor_selector)
                    if target.count() < 1:
                        continue
                    target = target.first
                    try:
                        target.scroll_into_view_if_needed(timeout=action_timeout)
                    except Exception:
                        pass
                    try:
                        target.click(timeout=action_timeout)
                        dispatched = f"ancestor:frame-{frame_index}:{ancestor_selector}"
                    except Exception as exc:
                        remember(f"ancestor:frame-{frame_index}:{ancestor_selector}", exc)
                        target.click(force=True, timeout=action_timeout)
                        dispatched = f"ancestor-force:frame-{frame_index}:{ancestor_selector}"
                    page.wait_for_timeout(450)
                    if pix_selected(scope):
                        return dispatched
                except Exception as exc:
                    remember(f"ancestor-final:frame-{frame_index}:{ancestor_selector}", exc)

            try:
                clicked_tag = label_node.evaluate(
                    """el => {
                        const target = el.closest(
                            'label,button,[role="radio"],[data-testid*="payment-method"],[data-testid*="pix"]'
                        ) || el.parentElement || el;
                        target.scrollIntoView({block: 'center', inline: 'center'});
                        target.click();
                        return target.tagName + ':' + (target.getAttribute('role') || '');
                    }"""
                )
                dispatched = f"dom-click:frame-{frame_index}:{clicked_tag}"
                page.wait_for_timeout(600)
                if pix_selected(scope):
                    return dispatched
            except Exception as exc:
                remember(f"dom-click:frame-{frame_index}:label-{label_index}", exc)

            try:
                label_node.click(force=True, timeout=action_timeout)
                dispatched = f"label-force:frame-{frame_index}"
                page.wait_for_timeout(600)
                if pix_selected(scope):
                    return dispatched
            except Exception as exc:
                remember(f"label-force:frame-{frame_index}", exc)

    # A force/DOM click on an exact PIX node can update React state asynchronously.
    if dispatched:
        page.wait_for_timeout(900)
        return dispatched

    detail = " | ".join(errors[-8:]) or "PIX label/radio was not found"
    raise RuntimeError("Stripe hosted checkout PIX option selection failed: " + detail)


def opll_browser_confirm_zero_pix(stripe_hosted_url: str, proxy_url: str, billing: dict,
                                  timeout: int = PAY_LONG_LINK_TIMEOUT) -> dict:
    """Confirm a 0-BRL PIX checkout through Stripe Hosted Checkout.

    The standalone PIX source does this with a hosted-checkout browser
    interaction because 0-BRL PIX needs Stripe's recurring mandate path. Pure
    payment_methods + payment_pages/confirm commonly stays at requires_approval
    or returns a failed submission for this specific 0-BRL branch.
    """
    if sync_playwright is None:
        raise RuntimeError("0-BRL PIX browser confirm requires playwright package")
    url = str(stripe_hosted_url or "").strip()
    if not url:
        raise RuntimeError("0-BRL PIX browser confirm missing stripe_hosted_url")
    browser_timeout = max(60_000, int(timeout or PAY_LONG_LINK_TIMEOUT) * 3_000)
    launch_options: dict = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
    }
    proxy = opll_playwright_proxy_options(proxy_url)
    if proxy:
        launch_options["proxy"] = proxy

    with sync_playwright() as playwright:
        browser = opll_launch_playwright_chromium(playwright, launch_options)
        try:
            page = browser.new_page(
                locale="pt-BR",
                timezone_id="America/Sao_Paulo",
                viewport={"width": 1365, "height": 1000},
                user_agent=PIX_USER_AGENT,
            )
            page.set_default_timeout(browser_timeout)
            page.goto(url, wait_until="domcontentloaded", timeout=browser_timeout)
            page.wait_for_timeout(1500)

            def fill_if_present(selector: str, value: str) -> None:
                try:
                    loc = page.locator(selector)
                    if loc.count() > 0:
                        loc.first.fill(str(value or ""), timeout=5000)
                except Exception:
                    pass

            def select_if_present(selector: str, value: str) -> None:
                try:
                    loc = page.locator(selector)
                    if loc.count() > 0:
                        loc.first.select_option(str(value or ""), force=True, timeout=5000)
                except Exception:
                    pass

            pix_selector_used = opll_select_pix_payment_method(page, timeout_ms=15000)
            page.wait_for_timeout(500)

            fill_if_present('input[name="taxId"]', billing.get("tax_id") or "")
            fill_if_present('input[name="billingName"]', billing.get("name") or "Pix User")
            fill_if_present('input[name="billingAddressLine1"]', billing.get("line1") or "Avenida Paulista 1000")
            fill_if_present('input[name="billingAddressLine2"]', billing.get("line2") or "Apto 42")
            fill_if_present('input[name="billingDependentLocality"]', billing.get("dependent_locality") or "Bela Vista")
            fill_if_present('input[name="billingLocality"]', billing.get("city") or "Sao Paulo")
            select_if_present('select#billingAdministrativeArea', billing.get("state") or "SP")
            fill_if_present('input[name="billingPostalCode"]', billing.get("postal_code") or "01310-100")
            fill_if_present('input[name="billingEmail"]', billing.get("email") or "buyer@example.com")
            fill_if_present('input[name="billingPhone"]', billing.get("phone") or "")
            page.wait_for_timeout(2500)

            def is_confirm_response(response) -> bool:
                try:
                    return (
                        response.request.method == "POST"
                        and urlsplit(response.url).path.endswith("/confirm")
                        and "/v1/payment_pages/" in response.url
                    )
                except Exception:
                    return False

            submit_selectors = [
                '[data-testid="hosted-payment-submit-button"]',
                'button[type="submit"]',
                'button:has-text("Assinar")',
                'button:has-text("Pagar")',
                'button:has-text("Confirm")',
            ]
            last_click_error = ""
            with page.expect_response(is_confirm_response, timeout=browser_timeout) as response_info:
                clicked = False
                for selector in submit_selectors:
                    try:
                        loc = page.locator(selector)
                        if loc.count() > 0:
                            loc.first.click(timeout=8000)
                            clicked = True
                            break
                    except Exception as exc:
                        last_click_error = str(exc)
                if not clicked:
                    raise RuntimeError("Stripe hosted checkout submit button not found: " + opll_short_error(last_click_error, 200))
            response = response_info.value
            if response.status >= 400:
                try:
                    text = response.text()
                except Exception:
                    text = ""
                raise RuntimeError(f"Stripe browser confirm failed: HTTP {response.status} {text[:500]}")
            payload = response.json() or {}
            if not isinstance(payload, dict):
                raise RuntimeError("Stripe browser confirm returned non-dict payload")
            return payload
        finally:
            try:
                browser.close()
            except Exception:
                pass


def opll_redirect_url_after_confirm(access_token: str, stripe: requests.Session, confirm_payload: dict,
                                     cs_id: str, stripe_pk: str, ctx: dict, checkout: dict,
                                     proxy_url: str = "", payment_locale: str = "en",
                                     chatgpt_cookie: str = "") -> str:
    redirect_url = opll_extract_redirect_to_url(confirm_payload)
    if redirect_url:
        return redirect_url
    submission = opll_find_submission_attempt(confirm_payload)
    if submission.get("state") == "requires_approval":
        opll_chatgpt_approve_with_retry(access_token, cs_id, checkout, proxy_url, chatgpt_cookie=chatgpt_cookie)
        return opll_stripe_payment_page_redirect_url(
            stripe, cs_id, stripe_pk, payment_locale=payment_locale, ctx=ctx, timeout_seconds=45)
    if submission.get("state") == "failed":
        raise RuntimeError(f"stripe submission failed: {opll_compact_submission_failure(confirm_payload, ctx)}")
    try:
        return opll_stripe_payment_page_redirect_url(
            stripe, cs_id, stripe_pk, payment_locale=payment_locale, ctx=ctx, timeout_seconds=30)
    except OpllStripeRequiresApproval:
        opll_chatgpt_approve_with_retry(access_token, cs_id, checkout, proxy_url, chatgpt_cookie=chatgpt_cookie)
        return opll_stripe_payment_page_redirect_url(
            stripe, cs_id, stripe_pk, payment_locale=payment_locale, ctx=ctx, timeout_seconds=45)


def opll_combo_attempt_order(country: str) -> list[tuple[str, str]]:
    requested = normalize_opll_country(country)
    ordered = [(requested, requested)]
    if requested == "DE":
        ordered.extend([("US", "US"), ("DE", "US"), ("US", "DE")])
    result = []
    seen = set()
    for item in ordered:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


# ===================================================================
# Top-level payment link generation
# ===================================================================

def _emit_payment_stage(progress_callback, key: str, label: str, index: int, total: int = 7, **extra) -> None:
    if progress_callback:
        payload = {
            "event": "attempt_stage",
            "stage_key": key,
            "stage_label": label,
            "stage_index": index,
            "stage_total": total,
            "stage_at": time.time(),
        }
        payload.update({k: v for k, v in (extra or {}).items() if v is not None})
        progress_callback(payload)


# ===================================================================
# UPI QR extraction (delegated to high-success gpt-upi-main port)
# ===================================================================


def generate_opll_upi_qr(access_token: str, entry_proxy_url: str = "",
                         exit_proxy_url: str = "", progress_callback=None,
                         chatgpt_cookie: str = "", upi_approve_mode: str = "full_auto",
                         upi_region: str = "IN", payment_locale: str = "en",
                         payment_email: str = "") -> dict:
    """Generate UPI QR using the high-success flow ported from E:/gpt-upi-main.

    The previous fixed-path / single-approve UPI implementation has been removed
    from this file. The actual implementation lives in upi_high_success.py.
    """
    from upi_high_success import generate_upi_qr_high_success

    return generate_upi_qr_high_success(
        access_token,
        entry_proxy_url=entry_proxy_url,
        exit_proxy_url=exit_proxy_url,
        progress_callback=progress_callback,
        chatgpt_cookie=chatgpt_cookie,
        upi_approve_mode=upi_approve_mode,
        upi_region=upi_region,
        payment_locale=payment_locale,
        payment_email=payment_email,
    )




def opll_upi_v3_new_session(proxy_url: str = ""):
    session = opll_new_http_session(force_requests=opll_is_local_proxy_url(proxy_url))
    if hasattr(session, "trust_env"):
        session.trust_env = False
    session.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
    })
    proxy = str(proxy_url or "").strip()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.35,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
    )
    if hasattr(session, "mount"):
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
    return session


def opll_upi_v3_chatgpt_headers(access_token: str, referer: str = "https://chatgpt.com/",
                                 target_path: str = "", chatgpt_cookie: str = "") -> dict:
    headers = {
        "accept": "application/json",
        "accept-language": "en-IN,en;q=0.9,hi;q=0.8",
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
        "origin": "https://chatgpt.com",
        "referer": referer,
        "user-agent": DEFAULT_USER_AGENT,
    }
    if target_path:
        headers["x-openai-target-path"] = target_path
        headers["x-openai-target-route"] = target_path
    cookie = str(chatgpt_cookie or "").strip()
    if cookie:
        headers["cookie"] = cookie
    return headers


def opll_upi_v3_stripe_headers(publishable_key: str, referer: str) -> dict:
    return {
        "accept": "application/json",
        "accept-language": "en-IN,en;q=0.9,hi;q=0.8",
        "authorization": f"Bearer {publishable_key}",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://checkout.stripe.com",
        "referer": referer,
        "stripe-version": STRIPE_VERSION_FULL,
        "user-agent": DEFAULT_USER_AGENT,
    }


def opll_upi_v3_elements_params(stripe_js_id: str, session_id: str = "",
                                payment_locale: str = "en") -> dict:
    locale = str(payment_locale or "en").strip() or "en"
    params = {
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "locale": locale,
    }
    if session_id:
        params["client_attribution_metadata[elements_session_id]"] = session_id
    return params


def opll_upi_v3_stripe_init(session, checkout_id: str, publishable_key: str,
                            checkout_page: str, payment_locale: str = "en") -> tuple[dict, str]:
    stripe_js_id = str(uuid.uuid4())
    body = {
        "key": publishable_key,
        "eid": "NA",
        "browser_locale": payment_locale,
        "browser_timezone": "Asia/Kolkata",
        "redirect_type": "url",
        "_stripe_version": STRIPE_VERSION_FULL,
        **opll_upi_v3_elements_params(stripe_js_id, payment_locale=payment_locale),
    }
    response = session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/init",
        data=body,
        headers=opll_upi_v3_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"UPI 3.0 Stripe init failed: HTTP {response.status_code} {response.text[:800]}"
        )
    payload = response.json() or {}
    if not isinstance(payload, dict):
        raise RuntimeError("UPI 3.0 Stripe init returned invalid payload")
    return payload, stripe_js_id


def opll_upi_v3_assert_init(payload: dict, stage: str, require_zero: bool,
                            require_upi: bool) -> str:
    amount, _ = opll_stripe_amount_info(payload)
    currency = str(payload.get("currency") or "").lower()
    methods = opll_collect_payment_method_types(payload)
    bad_currency = bool(currency and currency != "inr")
    missing_upi = bool(require_upi and methods and "upi" not in methods)
    if bad_currency or (require_zero and amount != "0") or missing_upi:
        raise RuntimeError(
            f"UPI 3.0 checkout_not_upi_trial: stage={stage} "
            f"amount={amount} currency={currency} methods={methods}"
        )
    return amount


def generate_opll_upi_qr_v3(access_token: str, in_proxy_url: str = "",
                            promo_proxy_url: str = "", progress_callback=None,
                            chatgpt_cookie: str = "", upi_approve_mode: str = "full_auto",
                            upi_region: str = "IN", payment_locale: str = "en",
                            payment_email: str = "") -> dict:
    """UPI 3.0: IN -> promo -> IN custom checkout bootstrap plus the proven UPI QR engine."""
    from upi_high_success import (
        _confirm_and_extract,
        normalize_upi_locale,
        normalize_upi_payment_email,
        normalize_upi_region,
    )

    token = parse_session_json(access_token) or str(access_token or "").strip()
    in_proxy_url = str(in_proxy_url or "").strip()
    promo_proxy_url = str(promo_proxy_url or "").strip()
    country, currency = normalize_upi_region(upi_region)
    payment_locale = normalize_upi_locale(payment_locale)
    payment_email_input = str(payment_email or "").strip()
    payment_email = (
        normalize_upi_payment_email(payment_email_input)
        if payment_email_input
        else ""
    )
    billing_profile = opll_generate_in_profile(payment_email)
    payment_email = str(billing_profile.get("email") or payment_email)
    if not token:
        raise RuntimeError("UPI 3.0 cannot parse Access Token")
    if not in_proxy_url:
        raise RuntimeError("UPI 3.0 requires IN proxy pool")
    if not promo_proxy_url:
        raise RuntimeError("UPI 3.0 requires promo proxy pool")

    total = 12
    checkout_session = opll_upi_v3_new_session(in_proxy_url)
    promotion_session = opll_upi_v3_new_session(promo_proxy_url)
    provider_session = opll_upi_v3_new_session(in_proxy_url)

    _emit_payment_stage(progress_callback, "upi3_auth", "UPI 3.0：校验 ChatGPT Token", 1, total)
    me_response = checkout_session.get(
        "https://chatgpt.com/backend-api/me",
        headers=opll_upi_v3_chatgpt_headers(token, chatgpt_cookie=chatgpt_cookie),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if me_response.status_code != 200:
        raise RuntimeError(
            f"UPI 3.0 ChatGPT /me failed: HTTP {me_response.status_code} {me_response.text[:500]}"
        )

    _emit_payment_stage(
        progress_callback, "upi3_checkout", "UPI 3.0：IN 创建 custom UPI trial checkout", 2, total,
    )
    checkout_body = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "cancel_url": "https://chatgpt.com/#pricing",
        "checkout_ui_mode": "custom",
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    checkout_response = checkout_session.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=checkout_body,
        headers=opll_upi_v3_chatgpt_headers(token, chatgpt_cookie=chatgpt_cookie),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if checkout_response.status_code != 200:
        raise RuntimeError(
            f"UPI 3.0 checkout failed: HTTP {checkout_response.status_code} {checkout_response.text[:800]}"
        )
    raw_checkout = checkout_response.json() or {}
    checkout_id = opll_extract_checkout_id(raw_checkout)
    publishable_key = opll_extract_stripe_publishable_key(raw_checkout)
    if not checkout_id or not publishable_key:
        raise RuntimeError(f"UPI 3.0 checkout missing cs/pk: {list(raw_checkout.keys())}")
    processor_entity = opll_extract_processor_entity(raw_checkout) or "openai_llc"
    checkout_page = f"https://checkout.stripe.com/c/pay/{checkout_id}"
    chatgpt_checkout_url = str(
        raw_checkout.get("url")
        or raw_checkout.get("stripe_hosted_url")
        or raw_checkout.get("checkout_url")
        or f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}"
    ).strip()
    checkout = {
        "cs_id": checkout_id,
        "checkout_id": checkout_id,
        "processor_entity": processor_entity,
        "stripe_publishable_key": publishable_key,
        "billing_country": country,
        "currency": currency,
        "checkout_ui_mode": "custom",
        "chatgpt_checkout_url": chatgpt_checkout_url,
        "raw_checkout": raw_checkout,
        "_upi_billing_profile": billing_profile,
    }

    page_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,*/*",
        "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
        "Referer": "https://chatgpt.com/",
    }
    for page_url in (f"https://pay.openai.com/c/pay/{checkout_id}", checkout_page):
        checkout_session.get(page_url, headers=page_headers, timeout=PAY_LONG_LINK_TIMEOUT)

    _emit_payment_stage(
        progress_callback, "upi3_bootstrap", "UPI 3.0：IN Bootstrap Stripe init", 3, total,
    )
    bootstrap_payload, _ = opll_upi_v3_stripe_init(
        checkout_session, checkout_id, publishable_key, checkout_page, payment_locale,
    )
    opll_upi_v3_assert_init(
        bootstrap_payload, "IN Bootstrap", require_zero=False, require_upi=False,
    )

    _emit_payment_stage(
        progress_callback, "upi3_promo", "UPI 3.0：优惠代理 checkout/update 到 0", 4, total,
    )
    update_path = "/backend-api/payments/checkout/update"
    update_body = {
        "checkout_session_id": checkout_id,
        "processor_entity": processor_entity,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    promotion_response = promotion_session.post(
        f"https://chatgpt.com{update_path}",
        json=update_body,
        headers=opll_upi_v3_chatgpt_headers(
            token,
            referer=f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}",
            target_path=update_path,
            chatgpt_cookie=chatgpt_cookie,
        ),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if promotion_response.status_code >= 400:
        raise RuntimeError(
            f"UPI 3.0 checkout/update failed: HTTP {promotion_response.status_code} "
            f"{promotion_response.text[:800]}"
        )
    try:
        promotion_payload = promotion_response.json() or {}
    except Exception:
        promotion_payload = {"raw": promotion_response.text[:500]}
    if isinstance(promotion_payload, dict) and promotion_payload.get("success") is False:
        raise RuntimeError(f"UPI 3.0 checkout/update rejected: {promotion_payload}")

    _emit_payment_stage(
        progress_callback, "upi3_refresh", "UPI 3.0：回到同一 IN 主代理刷新 Stripe", 5, total,
    )
    post_promo_payload, _ = opll_upi_v3_stripe_init(
        provider_session, checkout_id, publishable_key, checkout_page, payment_locale,
    )
    opll_upi_v3_assert_init(
        post_promo_payload, "promo 后 IN", require_zero=True, require_upi=True,
    )

    def _v3_progress(event):
        if not progress_callback:
            return
        if not isinstance(event, dict):
            progress_callback(event)
            return
        patched = dict(event)
        if patched.get("event") == "attempt_stage":
            original_index = int(patched.get("stage_index") or 0)
            patched["stage_key"] = "upi3_" + str(patched.get("stage_key") or "provider")
            patched["stage_label"] = "UPI 3.0：" + str(patched.get("stage_label") or "processing")
            patched["stage_index"] = max(6, min(11, original_index + 4))
            patched["stage_total"] = total
        progress_callback(patched)

    extracted = _confirm_and_extract(
        token,
        checkout,
        in_proxy_url,
        str(chatgpt_cookie or ""),
        _v3_progress,
        payment_locale=payment_locale,
        payment_email=payment_email,
    )
    qr = extracted.get("qr") or {}
    amount = str(extracted.get("expected_amount") or "")
    hosted_instructions = str(qr.get("qr_hosted_instructions_url") or "").strip()
    upi_uri = str(qr.get("qr_data") or "").strip()
    qr_image_url = str(
        qr.get("qr_image_url_png") or qr.get("qr_image_url_svg") or qr.get("qr_image_url") or ""
    ).strip()
    primary = hosted_instructions or upi_uri or qr_image_url or chatgpt_checkout_url
    if not (hosted_instructions or upi_uri or qr_image_url):
        raise RuntimeError("UPI 3.0 provider flow completed without UPI QR artifact")

    _emit_payment_stage(progress_callback, "upi3_done", "UPI 3.0：印度 UPI 链与二维码提取完成", 12, total)
    expires_at = int(qr.get("qr_expires_at") or 0)
    bootstrap_amount, bootstrap_amount_source = opll_stripe_amount_info(bootstrap_payload)
    post_promo_amount, post_promo_amount_source = opll_stripe_amount_info(post_promo_payload)
    return {
        **{k: v for k, v in checkout.items() if k != "raw_checkout" and not k.startswith("_")},
        "payment_method_country": country,
        "stripe_hosted_url": str(qr.get("stripe_hosted_url") or checkout_page),
        "stripe_redirect_url": qr_image_url,
        "provider_redirect_url": primary,
        "long_url": primary,
        "stripe_amount": amount,
        "stripe_amount_source": "upi_v3.provider_tax.total_summary.due" if amount else "",
        "payment_amount_display": opll_format_minor_amount(amount, currency) if amount else "",
        "expires_at": expires_at,
        "expires_raw": str(qr.get("qr_expires_at") or ""),
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "upi_v3",
        "local_payment_version": "3.0",
        "local_payment_flow": "custom_exact",
        "local_payment_detected": True,
        "payment_intent_status": str(extracted.get("payment_intent_status") or ""),
        "extraction_status": "success",
        "billing_email": str(extracted.get("billing_email") or payment_email),
        "payment_locale": str(extracted.get("payment_locale") or payment_locale),
        "browser_timezone": "Asia/Kolkata",
        "upi_region": country,
        "upi_billing": extracted.get("billing_profile") or billing_profile,
        "upi_qr": qr,
        "upi_qr_data": upi_uri,
        "upi_qr_image_url": str(qr.get("qr_image_url") or qr_image_url),
        "upi_qr_image_url_png": str(qr.get("qr_image_url_png") or ""),
        "upi_qr_image_url_svg": str(qr.get("qr_image_url_svg") or ""),
        "upi_qr_image_data_url": str(qr.get("qr_image_data_url") or ""),
        "upi_qr_hosted_instructions_url": hosted_instructions,
        "requires_chatgpt_cookie": False,
        "chatgpt_cookie_used": bool(str(chatgpt_cookie or "").strip()),
        "upi_approve_mode": "upi_v3_custom_exact",
        "promotion_update": promotion_payload,
        "bootstrap_init": {
            "amount": bootstrap_amount,
            "amount_source": bootstrap_amount_source,
            "currency": str(bootstrap_payload.get("currency") or ""),
            "payment_method_types": opll_collect_payment_method_types(bootstrap_payload),
        },
        "post_promo_init": {
            "amount": post_promo_amount,
            "amount_source": post_promo_amount_source,
            "currency": str(post_promo_payload.get("currency") or ""),
            "payment_method_types": opll_collect_payment_method_types(post_promo_payload),
        },
        "upi_v3_steps": [
            "in_auth",
            "in_custom_checkout",
            "in_bootstrap_init",
            "promo_update",
            "in_refresh",
            *(extracted.get("steps") or []),
            "upi_qr_complete",
        ],
    }


def generate_opll_upi_qr_v2(access_token: str, in_proxy_url: str = "",
                            promo_proxy_url: str = "", progress_callback=None,
                            chatgpt_cookie: str = "", upi_approve_mode: str = "full_auto",
                            upi_region: str = "IN", payment_locale: str = "en",
                            payment_email: str = "") -> dict:
    """UPI 2.0 extraction: IN hosted checkout + promo update + IN UPI confirm/poll."""
    from upi_high_success import (
        _confirm_and_extract,
        normalize_upi_locale,
        normalize_upi_payment_email,
        normalize_upi_region,
    )

    token = parse_session_json(access_token) or str(access_token or "").strip()
    in_proxy_url = str(in_proxy_url or "").strip()
    promo_proxy_url = str(promo_proxy_url or "").strip()
    country, currency = normalize_upi_region(upi_region)
    payment_locale = normalize_upi_locale(payment_locale)
    payment_email_input = str(payment_email or "").strip()
    payment_email = (
        normalize_upi_payment_email(payment_email_input)
        if payment_email_input
        else ""
    )
    billing_profile = opll_generate_in_profile(payment_email)
    payment_email = str(billing_profile.get("email") or payment_email)
    if not token:
        raise RuntimeError("UPI 2.0 cannot parse Access Token")
    if not in_proxy_url:
        raise RuntimeError("UPI 2.0 requires IN proxy pool")
    if not promo_proxy_url:
        raise RuntimeError("UPI 2.0 requires promo proxy pool (VN/TR/etc.)")

    total = 8
    _emit_payment_stage(progress_callback, "upi2_checkout", "UPI 2.0: IN create checkout", 1, total)
    checkout = opll_create_checkout(
        token, country, currency, in_proxy_url,
        # Match the captured successful site path: hosted checkout first, then keep
        # the IN proxy sticky for Stripe / approve / QR polling.
        checkout_ui_mode="hosted", require_stripe_session=True,
        preferred_processor_entity="openai_llc", promo_campaign_id="plus-1-month-free",
    )
    checkout["_upi_billing_profile"] = billing_profile
    if not checkout.get("processor_entity"):
        checkout["processor_entity"] = "openai_llc"
    if not checkout.get("stripe_publishable_key"):
        checkout["stripe_publishable_key"] = DEFAULT_STRIPE_PK
    cs_id = str(checkout.get("cs_id") or checkout.get("checkout_id") or "").strip()
    entity = str(checkout.get("processor_entity") or "openai_llc").strip() or "openai_llc"
    raw_checkout = checkout.get("raw_checkout") if isinstance(checkout.get("raw_checkout"), dict) else {}
    chatgpt_checkout_url = str(raw_checkout.get("url") or raw_checkout.get("stripe_hosted_url") or raw_checkout.get("checkout_url") or "").strip()
    if not chatgpt_checkout_url and cs_id:
        chatgpt_checkout_url = f"https://chatgpt.com/checkout/{entity}/{cs_id}"
    checkout["chatgpt_checkout_url"] = chatgpt_checkout_url

    _emit_payment_stage(progress_callback, "upi2_promo", "UPI 2.0: promo checkout/update to zero", 2, total)
    promotion_payload = opll_chatgpt_checkout_update_promotion(
        token, checkout, promo_proxy_url, chatgpt_cookie=chatgpt_cookie, normalize_vn=False)

    def _v2_progress(event):
        if not progress_callback:
            return
        if isinstance(event, dict):
            patched = dict(event)
            if patched.get("event") == "attempt_stage":
                patched["stage_label"] = "UPI 2.0: " + str(patched.get("stage_label") or "processing")
                try:
                    patched["stage_index"] = min(total, int(patched.get("stage_index") or 0) + 2)
                except Exception:
                    pass
                patched["stage_total"] = total
            progress_callback(patched)
        else:
            progress_callback(event)

    extracted = _confirm_and_extract(
        token,
        checkout,
        in_proxy_url,
        str(chatgpt_cookie or ""),
        _v2_progress,
        payment_locale=payment_locale,
        payment_email=payment_email,
    )
    qr = extracted.get("qr") or {}
    expires_at = int(qr.get("qr_expires_at") or 0)
    amount = str(extracted.get("expected_amount") or "")
    long_url = str(qr.get("qr_hosted_instructions_url") or chatgpt_checkout_url or "").strip()
    _emit_payment_stage(progress_callback, "upi2_done", "UPI 2.0: QR extracted", total, total)
    return {
        **{k: v for k, v in checkout.items() if k != "raw_checkout" and not k.startswith("_")},
        "stripe_hosted_url": str(qr.get("stripe_hosted_url") or chatgpt_checkout_url or ""),
        "long_url": long_url,
        "stripe_amount": amount,
        "stripe_amount_source": "upi_v2.stripe_tax_update.total_summary.due" if amount else "",
        "payment_amount_display": opll_format_minor_amount(amount, "INR") if amount else "",
        "expires_at": expires_at,
        "expires_raw": str(qr.get("qr_expires_at") or ""),
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "upi_v2",
        "local_payment_detected": True,
        "payment_intent_status": str(extracted.get("payment_intent_status") or ""),
        "extraction_status": "success",
        "billing_email": str(extracted.get("billing_email") or payment_email),
        "payment_locale": str(extracted.get("payment_locale") or payment_locale),
        "upi_region": country,
        "upi_billing": extracted.get("billing_profile") or billing_profile,
        "upi_qr": qr,
        "upi_qr_data": str(qr.get("qr_data") or ""),
        "upi_qr_image_url": str(qr.get("qr_image_url") or ""),
        "upi_qr_image_url_png": str(qr.get("qr_image_url_png") or ""),
        "upi_qr_image_url_svg": str(qr.get("qr_image_url_svg") or ""),
        "upi_qr_image_data_url": str(qr.get("qr_image_data_url") or ""),
        "upi_qr_hosted_instructions_url": str(qr.get("qr_hosted_instructions_url") or ""),
        "provider_redirect_url": str(qr.get("qr_image_data_url") or ""),
        "stripe_redirect_url": str(qr.get("qr_image_url_png") or qr.get("qr_image_url_svg") or ""),
        "requires_chatgpt_cookie": False,
        "chatgpt_cookie_used": bool(str(chatgpt_cookie or "").strip()),
        "upi_approve_mode": "upi_v2_zero_promo",
        "promotion_update": promotion_payload,
        "upi_v2_steps": extracted.get("steps") or [],
    }


def generate_opll_pix_qr_v2_single_br_legacy(access_token: str,
                                             br_proxy_url: str = "",
                                             progress_callback=None,
                                             chatgpt_cookie: str = "") -> dict:
    """PIX 2.0: BR checkout -> promo -> Stripe PIX -> approve -> hydrate."""
    token = opll_access_token_with_cookie(
        access_token, chatgpt_cookie, br_proxy_url
    ) or parse_session_json(access_token) or str(access_token or "").strip()
    br_proxy_url = str(br_proxy_url or "").strip()
    if not token:
        raise RuntimeError("PIX 2.0 missing Access Token")
    if not br_proxy_url:
        raise RuntimeError("PIX 2.0 requires one BR proxy pool")

    payment_locale = "pt-BR"
    browser_timezone = "America/Sao_Paulo"
    total = 10
    steps: list[dict] = []
    billing = opll_brazil_pix_billing(token)

    _emit_payment_stage(
        progress_callback, "pix2_checkout",
        "PIX 2.0: BR create hosted checkout", 1, total)
    checkout = opll_create_checkout(
        token, "BR", "BRL", br_proxy_url,
        checkout_ui_mode="hosted",
        require_stripe_session=True,
        preferred_processor_entity=opll_processor_entity_for_country("BR"),
        promo_campaign_id="plus-1-month-free",
    )
    if not checkout.get("processor_entity"):
        checkout["processor_entity"] = opll_processor_entity_for_country("BR")
    if not checkout.get("stripe_publishable_key"):
        checkout["stripe_publishable_key"] = DEFAULT_STRIPE_PK
    checkout["_pix_billing_profile"] = billing
    cs_id = str(checkout.get("cs_id") or checkout.get("checkout_id") or "").strip()
    stripe_pk = opll_stripe_key_for_checkout(checkout)
    steps.append({"name": "br_checkout", "status": 200, "cs_id": bool(cs_id)})

    _emit_payment_stage(
        progress_callback, "pix2_promo",
        "PIX 2.0: checkout/update promotion", 2, total)
    promotion_payload = opll_chatgpt_checkout_update_promotion(
        token,
        checkout,
        br_proxy_url,
        chatgpt_cookie=chatgpt_cookie,
        normalize_vn=False,
    )
    steps.append({"name": "checkout_update_promotion", "status": 200})

    stripe = opll_build_stripe_session(br_proxy_url)
    stripe.headers.update({"User-Agent": PIX_USER_AGENT})
    ctx_seed = {
        "stripe_js_id": str(uuid.uuid4()),
        "browser_timezone": browser_timezone,
        "saved_payment_method_mode": "never",
        "runtime_version": PIX_STRIPE_RUNTIME_VERSION,
        "stripe_version": PIX_STRIPE_VERSION_FULL,
    }

    _emit_payment_stage(
        progress_callback, "pix2_stripe_init",
        "PIX 2.0: BR Stripe init", 3, total)
    init_payload = opll_stripe_init(
        cs_id, "BR", "BRL", br_proxy_url,
        payment_locale=payment_locale,
        stripe=stripe,
        ctx=ctx_seed,
        checkout=checkout,
        browser_timezone=browser_timezone,
        saved_payment_method_mode="never",
    )
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(
            f"PIX 2.0 Stripe init missing hosted URL; keys={sorted(init_payload.keys())}")
    steps.append({"name": "stripe_init", "status": 200})

    _emit_payment_stage(
        progress_callback, "pix2_validate",
        "PIX 2.0: validate zero amount and PIX method", 4, total)
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    try:
        zero_amount = float(str(stripe_amount).strip()) == 0.0
    except Exception:
        zero_amount = False
    payment_methods = opll_collect_payment_method_types(init_payload)
    if stripe_amount_source == "fallback_zero" or not zero_amount:
        raise RuntimeError(
            "PIX 2.0 expected a verified zero-BRL checkout; "
            f"amount={stripe_amount} source={stripe_amount_source}")
    if "pix" not in payment_methods:
        raise RuntimeError(
            "PIX 2.0 zero-BRL checkout did not expose PIX; "
            + opll_payment_method_diagnostics(init_payload))
    steps.append({
        "name": "zero_and_pix_check",
        "status": 200,
        "amount": stripe_amount,
        "payment_methods": payment_methods,
    })

    ctx = opll_stripe_context(init_payload, payment_locale, ctx_seed)
    ctx["runtime_version"] = PIX_STRIPE_RUNTIME_VERSION
    ctx["stripe_version"] = PIX_STRIPE_VERSION_FULL
    ctx["browser_timezone"] = browser_timezone
    ctx["saved_payment_method_mode"] = "never"
    ctx["currency"] = "brl"

    _emit_payment_stage(
        progress_callback, "pix2_tax",
        "PIX 2.0: BR Stripe tax update", 5, total)
    tax_update_payload = opll_stripe_update_tax_region(
        stripe,
        cs_id,
        stripe_pk,
        ctx,
        billing,
        payment_locale=payment_locale,
        browser_timezone=browser_timezone,
        saved_payment_method_mode="never",
    )
    working_payload = dict(init_payload)
    if isinstance(tax_update_payload, dict):
        working_payload.update(tax_update_payload)
    stripe_hosted_url = str(
        working_payload.get("stripe_hosted_url") or stripe_hosted_url).strip()
    ctx = opll_stripe_context(working_payload, payment_locale, ctx)
    ctx["runtime_version"] = PIX_STRIPE_RUNTIME_VERSION
    ctx["stripe_version"] = PIX_STRIPE_VERSION_FULL
    ctx["browser_timezone"] = browser_timezone
    ctx["saved_payment_method_mode"] = "never"
    ctx["currency"] = "brl"
    tax_amount, tax_amount_source = opll_stripe_amount_info(working_payload)
    try:
        tax_zero = float(str(tax_amount).strip()) == 0.0
    except Exception:
        tax_zero = False
    tax_methods = opll_collect_payment_method_types(working_payload)
    if tax_amount_source == "fallback_zero" or not tax_zero:
        raise RuntimeError(
            "PIX 2.0 tax update changed the verified zero-BRL state; "
            f"amount={tax_amount} source={tax_amount_source}")
    if tax_methods and "pix" not in tax_methods:
        raise RuntimeError(
            "PIX 2.0 tax update removed PIX from payment methods: "
            + ",".join(tax_methods))
    stripe_amount, stripe_amount_source = tax_amount, tax_amount_source
    if tax_methods:
        payment_methods = tax_methods
    steps.append({
        "name": "stripe_tax_update",
        "status": 200,
        "amount": stripe_amount,
        "payment_methods": payment_methods,
    })

    _emit_payment_stage(
        progress_callback, "pix2_confirm",
        "PIX 2.0: create and confirm PIX", 6, total)
    pm_id = opll_stripe_create_pix_method(
        stripe, cs_id, ctx, billing, stripe_pk)
    confirm_mode = "api"
    try:
        confirm_payload = opll_stripe_confirm(
            stripe,
            cs_id,
            pm_id,
            stripe_pk,
            working_payload,
            ctx,
            checkout,
            stripe_hosted_url,
            payment_method_type="pix",
        )
    except Exception as api_exc:
        browser_fallback = str(
            os.environ.get("CHATGPT_PIX2_BROWSER_CONFIRM_FALLBACK", "true")
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not browser_fallback:
            raise
        confirm_payload = opll_browser_confirm_zero_pix(
            stripe_hosted_url,
            br_proxy_url,
            billing,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
        confirm_mode = "browser_zero_fallback"
        pm_id = "browser-created"
        if isinstance(confirm_payload, dict):
            confirm_payload["_api_confirm_error"] = opll_short_error(str(api_exc), 500)
    submission = opll_find_submission_attempt(confirm_payload)
    submission_attempt_id = str(submission.get("id") or "").strip()
    steps.append({
        "name": "stripe_confirm_pix",
        "status": 200,
        "mode": confirm_mode,
        "submission_state": str(submission.get("state") or ""),
    })

    _emit_payment_stage(
        progress_callback, "pix2_chatgpt_approval",
        "PIX 2.0: ChatGPT confirm/approve", 7, total)
    approval = opll_chatgpt_confirm_approve_payment(
        token,
        cs_id,
        checkout,
        "pix",
        proxy_url=br_proxy_url,
        chatgpt_cookie=chatgpt_cookie,
        submission_attempt_id=submission_attempt_id,
        progress_callback=progress_callback,
    )
    steps.append({
        "name": "chatgpt_confirm_approve",
        "status": int(approval.get("status") or 0),
        "result": str(approval.get("result") or ""),
        "attempt": int(approval.get("attempt") or 0),
    })

    pix = opll_extract_pix_link(confirm_payload)
    pix = opll_merge_pix_extract(
        pix, opll_extract_pix_link(approval.get("payload")))
    _emit_payment_stage(
        progress_callback, "pix2_poll",
        "PIX 2.0: poll Stripe payment page", 8, total)
    try:
        poll_seconds = int(os.environ.get("CHATGPT_PIX2_POLL_SECONDS", "30"))
    except Exception:
        poll_seconds = 30
    poll_seconds = max(1, min(poll_seconds, 90))
    poll_error = ""
    try:
        polled_pix = opll_stripe_payment_page_pix_extract(
            stripe,
            cs_id,
            stripe_pk,
            payment_locale=payment_locale,
            timeout_seconds=poll_seconds,
            ctx=ctx,
        )
        pix = opll_merge_pix_extract(pix, polled_pix)
    except OpllStripeRequiresApproval as exc:
        poll_error = opll_short_error(str(exc), 300)
        approval = opll_chatgpt_confirm_approve_payment(
            token,
            cs_id,
            checkout,
            "pix",
            proxy_url=br_proxy_url,
            chatgpt_cookie=chatgpt_cookie,
            submission_attempt_id=submission_attempt_id,
            progress_callback=progress_callback,
        )
        polled_pix = opll_stripe_payment_page_pix_extract(
            stripe,
            cs_id,
            stripe_pk,
            payment_locale=payment_locale,
            timeout_seconds=poll_seconds,
            ctx=ctx,
        )
        pix = opll_merge_pix_extract(pix, polled_pix)
    except Exception as exc:
        poll_error = opll_short_error(str(exc), 500)
    steps.append({
        "name": "stripe_payment_page_poll",
        "status": 200 if not poll_error else 206,
        "error": poll_error,
    })

    _emit_payment_stage(
        progress_callback, "pix2_hydrate",
        "PIX 2.0: hydrate hosted instructions", 9, total)
    pix = opll_hydrate_pix_artifacts(
        stripe,
        br_proxy_url,
        pix,
        str(pix.get("pix_hosted_instructions_url") or ""),
        str(pix.get("pix_instructions_url") or ""),
        str(pix.get("pix_checkout_url") or ""),
        str(pix.get("pix_redirect_url") or ""),
        stripe_hosted_url,
        opll_to_openai_pay_url(stripe_hosted_url),
    )
    instructions_url = str(
        pix.get("pix_hosted_instructions_url")
        or pix.get("pix_instructions_url")
        or ""
    ).strip()
    pix_payload = str(pix.get("pix_payload") or "").strip()
    qr_image_url = str(pix.get("pix_qr_image_url") or "").strip()
    if not opll_is_real_pix_qr_image_url(qr_image_url):
        qr_image_url = ""
    pix_qr_image_data_url = opll_make_qr_data_url(pix_payload) if pix_payload else ""
    primary = instructions_url or pix_payload or qr_image_url
    if not primary:
        raise RuntimeError(
            "PIX 2.0 finished approval/poll/hydration without a PIX artifact; "
            f"submission_state={submission.get('state') or '-'}; poll={poll_error or '-'}")

    expires_at, expires_raw = opll_checkout_expires_at(
        checkout, init_payload, tax_update_payload, confirm_payload, pix)
    _emit_payment_stage(
        progress_callback, "pix2_done",
        "PIX 2.0: link and QR extracted", 10, total)
    steps.append({"name": "hosted_instructions_hydration", "status": 200})
    return {
        **{k: v for k, v in checkout.items()
           if k != "raw_checkout" and not k.startswith("_")},
        "payment_method_country": "BR",
        "payment_method_id": pm_id,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": instructions_url,
        "provider_redirect_url": primary,
        "long_url": primary,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": f"pix_v2.{stripe_amount_source}",
        "payment_amount_display": opll_format_minor_amount(stripe_amount, "BRL"),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "pix_v2",
        "local_payment_detected": True,
        "payment_methods": payment_methods,
        "payment_locale": payment_locale,
        "browser_timezone": browser_timezone,
        "billing_email": str(billing.get("email") or ""),
        "pix_billing": billing,
        "pix_link": instructions_url or primary,
        "pix_instructions_url": instructions_url,
        "pix_checkout_url": str(pix.get("pix_checkout_url") or ""),
        "pix_openai_pay_url": str(pix.get("pix_openai_pay_url") or ""),
        "pix_redirect_url": str(pix.get("pix_redirect_url") or ""),
        "pix_payload": pix_payload,
        "pix_qr_image_url": qr_image_url,
        "pix_qr_image_data_url": pix_qr_image_data_url,
        "pix_hosted_instructions_url": instructions_url,
        "pix_resource_url": str(pix.get("pix_resource_url") or ""),
        "pix_source": str(pix.get("source") or "pix2_hydration"),
        "pix_hydration_error": str(pix.get("pix_hydration_error") or ""),
        "pix2_confirm_mode": confirm_mode,
        "pix2_approval": approval,
        "pix2_steps": steps,
        "promotion_update": promotion_payload,
        "tax_update": tax_update_payload,
        "requires_chatgpt_cookie": False,
        "chatgpt_cookie_used": bool(str(chatgpt_cookie or "").strip()),
    }


def generate_opll_pix_qr_v2(access_token: str, br_proxy_url: str = "",
                            promo_proxy_url: str = "", progress_callback=None,
                            chatgpt_cookie: str = "") -> dict:
    """PIX 2.0: exact UPI 2.0 topology and fallbacks, adapted to BR/PIX."""
    from upi_high_success import _confirm_and_extract_pix

    token = parse_session_json(access_token) or str(access_token or "").strip()
    br_proxy_url = str(br_proxy_url or "").strip()
    promo_proxy_url = str(promo_proxy_url or "").strip()
    if not token:
        raise RuntimeError("PIX 2.0 cannot parse Access Token")
    if not br_proxy_url:
        raise RuntimeError("PIX 2.0 requires BR proxy pool")
    if not promo_proxy_url:
        raise RuntimeError("PIX 2.0 requires promo proxy pool (VN/TR/etc.)")

    total = 9
    billing_profile = opll_brazil_pix_billing(token)
    _emit_payment_stage(
        progress_callback,
        "pix2_checkout",
        "PIX 2.0: BR create checkout",
        1,
        total,
    )
    checkout = opll_create_checkout(
        token,
        "BR",
        "BRL",
        br_proxy_url,
        checkout_ui_mode="hosted",
        require_stripe_session=True,
        preferred_processor_entity=opll_processor_entity_for_country("BR"),
        promo_campaign_id="plus-1-month-free",
        billing_profile=billing_profile,
    )
    checkout["_pix_billing_profile"] = billing_profile
    if not checkout.get("processor_entity"):
        checkout["processor_entity"] = opll_processor_entity_for_country("BR")
    if not checkout.get("stripe_publishable_key"):
        checkout["stripe_publishable_key"] = DEFAULT_STRIPE_PK
    cs_id = str(checkout.get("cs_id") or checkout.get("checkout_id") or "").strip()
    entity = str(
        checkout.get("processor_entity") or opll_processor_entity_for_country("BR")
    ).strip() or opll_processor_entity_for_country("BR")
    raw_checkout = (
        checkout.get("raw_checkout")
        if isinstance(checkout.get("raw_checkout"), dict)
        else {}
    )
    chatgpt_checkout_url = str(
        raw_checkout.get("url")
        or raw_checkout.get("stripe_hosted_url")
        or raw_checkout.get("checkout_url")
        or ""
    ).strip()
    if not chatgpt_checkout_url and cs_id:
        chatgpt_checkout_url = f"https://chatgpt.com/checkout/{entity}/{cs_id}"
    checkout["chatgpt_checkout_url"] = chatgpt_checkout_url

    _emit_payment_stage(
        progress_callback,
        "pix2_promo",
        "PIX 2.0: promo checkout/update to zero",
        2,
        total,
    )
    promotion_payload = opll_chatgpt_checkout_update_promotion(
        token,
        checkout,
        promo_proxy_url,
        chatgpt_cookie=chatgpt_cookie,
        normalize_vn=False,
        include_full_profile=False,
    )

    _emit_payment_stage(
        progress_callback,
        "pix2_checkout_billing",
        "PIX 2.0: BR checkout billing sync before Stripe init",
        3,
        total,
    )
    checkout_billing_update = opll_chatgpt_checkout_sync_billing(
        token,
        checkout,
        br_proxy_url,
        billing_profile=billing_profile,
        chatgpt_cookie=chatgpt_cookie,
    )
    checkout_billing_sync = opll_chatgpt_update_pix_taxes(
        token,
        checkout,
        br_proxy_url,
        billing=billing_profile,
        chatgpt_cookie=chatgpt_cookie,
    )

    def _v2_progress(event):
        if not progress_callback:
            return
        if isinstance(event, dict):
            patched = dict(event)
            if patched.get("event") == "attempt_stage":
                patched["stage_label"] = "PIX 2.0: " + str(
                    patched.get("stage_label") or "processing"
                )
                try:
                    patched["stage_index"] = min(
                        total, int(patched.get("stage_index") or 0) + 2
                    )
                except Exception:
                    pass
                patched["stage_total"] = total
            progress_callback(patched)
        else:
            progress_callback(event)

    extracted = _confirm_and_extract_pix(
        token,
        checkout,
        br_proxy_url,
        str(chatgpt_cookie or ""),
        _v2_progress,
        payment_locale="pt-BR",
    )
    pix = extracted.get("pix") or {}
    instructions_url = str(
        pix.get("pix_hosted_instructions_url")
        or pix.get("pix_instructions_url")
        or ""
    ).strip()
    pix_payload = str(pix.get("pix_payload") or "").strip()
    qr_image_url = str(pix.get("pix_qr_image_url") or "").strip()
    pix_qr_image_data_url = (
        opll_make_qr_data_url(pix_payload) if pix_payload else ""
    )
    primary = instructions_url or pix_payload or qr_image_url or chatgpt_checkout_url
    expires_at = int(extracted.get("expires_at") or 0)
    amount = str(extracted.get("expected_amount") or "")
    _emit_payment_stage(
        progress_callback,
        "pix2_done",
        "PIX 2.0: PIX link/QR extracted",
        total,
        total,
    )
    return {
        **{
            key: value
            for key, value in checkout.items()
            if key != "raw_checkout" and not key.startswith("_")
        },
        "payment_method_country": "BR",
        "stripe_hosted_url": str(
            pix.get("pix_checkout_url") or chatgpt_checkout_url or ""
        ),
        "stripe_redirect_url": instructions_url,
        "provider_redirect_url": primary,
        "long_url": primary,
        "stripe_amount": amount,
        "stripe_amount_source": (
            "pix_v2.stripe_tax_update.total_summary.due" if amount else ""
        ),
        "payment_amount_display": (
            opll_format_minor_amount(amount, "BRL") if amount else ""
        ),
        "expires_at": expires_at,
        "expires_raw": str(extracted.get("expires_raw") or ""),
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "pix_v2",
        "local_payment_detected": True,
        "payment_methods": extracted.get("payment_methods") or [],
        "payment_intent_status": str(
            extracted.get("payment_intent_status") or ""
        ),
        "payment_locale": str(extracted.get("payment_locale") or "pt-BR"),
        "browser_timezone": "America/Sao_Paulo",
        "billing_email": str(extracted.get("billing_email") or ""),
        "pix_billing": extracted.get("billing_profile") or billing_profile,
        "pix_link": instructions_url or primary,
        "pix_instructions_url": instructions_url,
        "pix_checkout_url": str(pix.get("pix_checkout_url") or ""),
        "pix_openai_pay_url": str(pix.get("pix_openai_pay_url") or ""),
        "pix_redirect_url": str(pix.get("pix_redirect_url") or ""),
        "pix_payload": pix_payload,
        "pix_qr_image_url": qr_image_url,
        "pix_qr_image_data_url": pix_qr_image_data_url,
        "pix_hosted_instructions_url": instructions_url,
        "pix_resource_url": str(pix.get("pix_resource_url") or ""),
        "pix_source": str(pix.get("source") or "pix2_upi_exact_hydration"),
        "pix_hydration_error": str(pix.get("pix_hydration_error") or ""),
        "pix2_approval": extracted.get("approval") or {},
            "pix2_steps": extracted.get("steps") or [],
            "pix2_proxy_model": "BR checkout/Stripe + promo update + BR confirm/poll",
            "promotion_update": promotion_payload,
            "checkout_billing_update": checkout_billing_update,
            "checkout_billing_sync": checkout_billing_sync,
        "tax_update": extracted.get("tax_payload") or {},
        "confirm_payload": extracted.get("confirm_payload") or {},
        "requires_chatgpt_cookie": False,
        "chatgpt_cookie_used": bool(str(chatgpt_cookie or "").strip()),
    }


def _opll_pix_v3_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or str(default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def opll_pix_v3_new_session(proxy_url: str = ""):
    session = opll_new_http_session(force_requests=opll_is_local_proxy_url(proxy_url))
    session.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    if proxy_url:
        if hasattr(session, "trust_env"):
            session.trust_env = False
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session


def opll_pix_v3_chatgpt_headers(access_token: str, referer: str = "https://chatgpt.com/",
                                   target_path: str = "", chatgpt_cookie: str = "") -> dict:
    token = parse_session_json(access_token) or str(access_token or "").strip()
    cookie = opll_normalize_chatgpt_cookie(chatgpt_cookie)
    device_id = opll_cookie_value(cookie, "oai-did") or str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"pix-v3-device:{token}")
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "oai-language": "pt-BR",
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": referer,
        "oai-device-id": device_id,
        "Cookie": cookie or f"oai-did={device_id}",
    }
    if target_path:
        headers["x-openai-target-path"] = target_path
        headers["x-openai-target-route"] = target_path
    return headers


def opll_pix_v3_stripe_headers(publishable_key: str, referer: str) -> dict:
    origin = "https://checkout.stripe.com" if "checkout.stripe.com" in referer else "https://pay.openai.com"
    return {
        "Authorization": f"Bearer {publishable_key}",
        "Origin": origin,
        "Referer": referer,
        "Accept": "application/json",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Fetch-Site": "same-site" if origin == "https://checkout.stripe.com" else "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": DEFAULT_USER_AGENT,
    }


def opll_pix_v3_elements_params(stripe_js_id: str, session_id: str = "") -> dict:
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": "pt-BR",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
    }
    if session_id:
        params["elements_session_client[session_id]"] = session_id
    return params


def opll_pix_v3_expected_amount(payload: dict) -> str:
    options = payload.get("elements_options") if isinstance(payload.get("elements_options"), dict) else {}
    if options.get("amount") is not None:
        return str(int(options["amount"]))
    total_summary = payload.get("total_summary") if isinstance(payload.get("total_summary"), dict) else {}
    if total_summary.get("due") is not None:
        return str(int(total_summary["due"]))
    invoice = payload.get("invoice") if isinstance(payload.get("invoice"), dict) else {}
    for name in ("amount_due", "total"):
        if invoice.get(name) is not None:
            return str(int(invoice[name]))
    line_items = payload.get("line_items")
    if isinstance(line_items, list):
        amounts = [
            item.get("amount") for item in line_items
            if isinstance(item, dict) and item.get("amount") is not None
        ]
        if amounts:
            return str(sum(int(value) for value in amounts))
    return "unknown"


def opll_generate_pix_v3_billing(access_token: str) -> dict:
    """Generate one BR identity with CPF and keep it sticky for this attempt."""
    billing = dict(opll_brazil_pix_billing(access_token))
    billing["country"] = "BR"
    return billing


def opll_pix_v3_stripe_init(session, checkout_id: str, publishable_key: str,
                               checkout_page: str) -> tuple[dict, str]:
    stripe_js_id = str(uuid.uuid4())
    body = {
        "key": publishable_key,
        "eid": "NA",
        "browser_locale": "pt-BR",
        "browser_timezone": "America/Sao_Paulo",
        "redirect_type": "url",
        "_stripe_version": STRIPE_VERSION_FULL,
        **opll_pix_v3_elements_params(stripe_js_id),
    }
    response = session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/init",
        data=body,
        headers=opll_pix_v3_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"PIX 3.0 stripe init failed: HTTP {response.status_code} {response.text[:800]}")
    payload = response.json() or {}
    if not isinstance(payload, dict):
        raise RuntimeError("PIX 3.0 stripe init returned invalid payload")
    return payload, stripe_js_id


def opll_pix_v3_assert_init(payload: dict, stage: str, require_zero: bool,
                            require_pix: bool) -> str:
    amount = opll_pix_v3_expected_amount(payload)
    currency = str(payload.get("currency") or "").lower()
    methods = [str(item).lower() for item in (payload.get("payment_method_types") or [])]
    bad_currency = bool(currency and currency != "brl")
    if bad_currency or (require_zero and amount != "0") or (require_pix and "pix" not in methods):
        raise RuntimeError(
            f"PIX 3.0 checkout_not_pix_trial: stage={stage} "
            f"amount={amount} currency={currency} methods={methods}"
        )
    return amount


def generate_opll_pix_v3_long_link(access_token: str, br_proxy_url: str = "",
                                    promo_proxy_url: str = "", progress_callback=None,
                                    chatgpt_cookie: str = "") -> dict:
    """PIX 3.0: BR -> promo -> BR custom exact flow combining Kakao 3.0 state order with PIX QR extraction."""
    token = parse_session_json(access_token) or str(access_token or "").strip()
    br_proxy_url = str(br_proxy_url or "").strip()
    promo_proxy_url = str(promo_proxy_url or "").strip()
    if not token:
        raise RuntimeError("PIX 3.0 missing Access Token")
    if not br_proxy_url:
        raise RuntimeError("PIX 3.0 requires BR proxy pool")
    if not promo_proxy_url:
        raise RuntimeError("PIX 3.0 requires promo proxy pool")

    total = 12
    checkout_session = opll_pix_v3_new_session(br_proxy_url)
    promotion_session = opll_pix_v3_new_session(promo_proxy_url)
    provider_session = opll_pix_v3_new_session(br_proxy_url)

    _emit_payment_stage(progress_callback, "pix3_auth", "PIX 3.0：校验 ChatGPT Token", 1, total)
    me_response = checkout_session.get(
        "https://chatgpt.com/backend-api/me",
        headers=opll_pix_v3_chatgpt_headers(token, chatgpt_cookie=chatgpt_cookie),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if me_response.status_code != 200:
        raise RuntimeError(
            f"PIX 3.0 ChatGPT /me failed: HTTP {me_response.status_code} {me_response.text[:500]}"
        )

    _emit_payment_stage(progress_callback, "pix3_checkout",
                        "PIX 3.0：BR 创建 custom PIX trial checkout", 2, total)
    checkout_body = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": "BR", "currency": "BRL"},
        "cancel_url": "https://chatgpt.com/#pricing",
        "checkout_ui_mode": "custom",
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    checkout_response = checkout_session.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=checkout_body,
        headers=opll_pix_v3_chatgpt_headers(token, chatgpt_cookie=chatgpt_cookie),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if checkout_response.status_code != 200:
        raise RuntimeError(
            f"PIX 3.0 checkout failed: HTTP {checkout_response.status_code} {checkout_response.text[:800]}"
        )
    raw_checkout = checkout_response.json() or {}
    checkout_id = opll_extract_checkout_id(raw_checkout)
    publishable_key = opll_extract_stripe_publishable_key(raw_checkout)
    if not checkout_id or not publishable_key:
        raise RuntimeError(f"PIX 3.0 checkout missing cs/pk: {list(raw_checkout.keys())}")
    processor_entity = opll_extract_processor_entity(raw_checkout) or "openai_llc"
    checkout = {
        "cs_id": checkout_id,
        "checkout_id": checkout_id,
        "processor_entity": processor_entity,
        "stripe_publishable_key": publishable_key,
        "billing_country": "BR",
        "currency": "BRL",
        "checkout_ui_mode": "custom",
        "raw_checkout": raw_checkout,
    }
    checkout_page = f"https://checkout.stripe.com/c/pay/{checkout_id}"
    page_headers = {
        "User-Agent": opll_pix_v3_chatgpt_headers(token)["User-Agent"],
        "Accept": "text/html,*/*",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://chatgpt.com/",
    }
    for page_url in (f"https://pay.openai.com/c/pay/{checkout_id}", checkout_page):
        checkout_session.get(page_url, headers=page_headers, timeout=PAY_LONG_LINK_TIMEOUT)

    _emit_payment_stage(progress_callback, "pix3_bootstrap",
                        "PIX 3.0：BR Bootstrap Stripe init", 3, total)
    bootstrap_payload, _ = opll_pix_v3_stripe_init(
        checkout_session, checkout_id, publishable_key, checkout_page,
    )
    opll_pix_v3_assert_init(bootstrap_payload, "BR Bootstrap", require_zero=False, require_pix=False)

    _emit_payment_stage(progress_callback, "pix3_promo",
                        "PIX 3.0：优惠代理 checkout/update 到 0", 4, total)
    update_path = "/backend-api/payments/checkout/update"
    update_body = {
        "checkout_session_id": checkout_id,
        "processor_entity": processor_entity,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    promotion_response = promotion_session.post(
        f"https://chatgpt.com{update_path}",
        json=update_body,
        headers=opll_pix_v3_chatgpt_headers(
            token,
            referer=f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}",
            target_path=update_path,
            chatgpt_cookie=chatgpt_cookie,
        ),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if promotion_response.status_code >= 400:
        raise RuntimeError(
            f"PIX 3.0 checkout/update failed: HTTP {promotion_response.status_code} "
            f"{promotion_response.text[:800]}"
        )
    try:
        promotion_payload = promotion_response.json() or {}
    except Exception:
        promotion_payload = {"raw": promotion_response.text[:500]}
    if isinstance(promotion_payload, dict) and promotion_payload.get("success") is False:
        raise RuntimeError(f"PIX 3.0 checkout/update rejected: {promotion_payload}")

    _emit_payment_stage(progress_callback, "pix3_refresh",
                        "PIX 3.0：优惠更新后回到 BR 刷新 Stripe", 5, total)
    init_payload, stripe_js_id = opll_pix_v3_stripe_init(
        provider_session, checkout_id, publishable_key, checkout_page,
    )
    amount = opll_pix_v3_assert_init(
        init_payload, "promo 后 BR", require_zero=True, require_pix=False,
    )
    billing = opll_generate_pix_v3_billing(token)

    _emit_payment_stage(progress_callback, "pix3_taxes",
                        "PIX 3.0：同步巴西 checkout/taxes 与 Stripe tax_region", 6, total)
    taxes_path = "/backend-api/payments/checkout/taxes"
    billing_address = {
        "line1": billing["line1"],
        "line2": str(billing.get("line2") or ""),
        "city": billing["city"],
        "country": "BR",
        "postal_code": billing["postal_code"],
        "state": billing["state"],
    }
    taxes_body = {
        "checkout_session_id": checkout_id,
        "checkout_email": billing["email"],
        "customer_email": billing["email"],
        "billing_email": billing["email"],
        "billing_country": "BR",
        "billing_name": billing["name"],
        "customer_name": billing["name"],
        "billing_phone": str(billing.get("phone") or ""),
        "customer_phone": str(billing.get("phone") or ""),
        "currency": "BRL",
        "tax_id": str(billing.get("tax_id") or ""),
        "processor_entity": processor_entity,
        "billing_address": billing_address,
        "customer_address": billing_address,
        "billing_details": {
            "email": billing["email"],
            "name": billing["name"],
            "phone": str(billing.get("phone") or ""),
            "country": "BR",
            "currency": "BRL",
            "address": billing_address,
            "tax_id": str(billing.get("tax_id") or ""),
        },
    }
    taxes_response = provider_session.post(
        f"https://chatgpt.com{taxes_path}",
        json=taxes_body,
        headers=opll_pix_v3_chatgpt_headers(
            token,
            referer=f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}",
            target_path=taxes_path,
            chatgpt_cookie=chatgpt_cookie,
        ),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if taxes_response.status_code >= 400:
        raise RuntimeError(
            f"PIX 3.0 checkout/taxes failed: HTTP {taxes_response.status_code} {taxes_response.text[:800]}"
        )
    try:
        checkout_tax_payload = taxes_response.json() or {}
    except Exception:
        checkout_tax_payload = {"raw": taxes_response.text[:500]}

    tax_elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
    tax_body = {
        "key": publishable_key,
        "_stripe_version": STRIPE_VERSION_FULL,
        **opll_pix_v3_elements_params(stripe_js_id, tax_elements_session_id),
        "tax_region[country]": billing["country"],
        "tax_region[postal_code]": billing["postal_code"],
        "tax_region[line1]": billing["line1"],
        "tax_region[line2]": str(billing.get("line2") or ""),
        "tax_region[city]": billing["city"],
        "tax_region[state]": billing["state"],
    }
    def post_tax_region(payload: dict):
        return provider_session.post(
            f"https://api.stripe.com/v1/payment_pages/{checkout_id}",
            data=payload,
            headers=opll_pix_v3_stripe_headers(publishable_key, checkout_page),
            timeout=PAY_LONG_LINK_TIMEOUT,
        )

    removed_tax_params: list[str] = []
    tax_response = post_tax_region(tax_body)
    for _ in range(8):
        if tax_response.status_code < 400:
            break
        try:
            tax_error = (tax_response.json() or {}).get("error") or {}
        except Exception:
            tax_error = {}
        unknown = str(
            tax_error.get("param")
            if isinstance(tax_error, dict) and tax_error.get("code") == "parameter_unknown"
            else ""
        ).strip()
        if not unknown or unknown not in tax_body:
            break
        removed_tax_params.append(unknown)
        tax_body.pop(unknown, None)
        tax_response = post_tax_region(tax_body)
    if tax_response.status_code >= 400:
        raise RuntimeError(
            f"PIX 3.0 Stripe tax_region failed: HTTP {tax_response.status_code} {tax_response.text[:800]}"
        )
    try:
        stripe_tax_payload = tax_response.json() or {}
    except Exception:
        stripe_tax_payload = {"raw": tax_response.text[:500]}
    if isinstance(stripe_tax_payload, dict) and removed_tax_params:
        stripe_tax_payload["_removed_unknown_params"] = removed_tax_params

    _emit_payment_stage(progress_callback, "pix3_tax_refresh",
                        "PIX 3.0：巴西税务同步后刷新 Stripe", 7, total)
    init_payload, stripe_js_id = opll_pix_v3_stripe_init(
        provider_session, checkout_id, publishable_key, checkout_page,
    )
    amount = opll_pix_v3_assert_init(
        init_payload, "BR 税务同步", require_zero=True, require_pix=True,
    )
    elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"

    _emit_payment_stage(progress_callback, "pix3_pre_confirm",
                        "PIX 3.0：Stripe pre_confirm PIX", 8, total)
    pre_confirm_response = provider_session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/pre_confirm",
        data={
            "eid": str(uuid.uuid4()),
            "payment_method_type": "pix",
            "key": publishable_key,
            "_stripe_version": STRIPE_VERSION_FULL,
        },
        headers=opll_pix_v3_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if pre_confirm_response.status_code != 200:
        raise RuntimeError(
            f"PIX 3.0 pre_confirm failed: HTTP {pre_confirm_response.status_code} "
            f"{pre_confirm_response.text[:800]}"
        )

    _emit_payment_stage(progress_callback, "pix3_method",
                        "PIX 3.0：创建 PIX payment method", 9, total)
    stripe_runtime = PIX_STRIPE_RUNTIME_VERSION
    client_session_id = str(uuid.uuid4())
    guid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    muid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    sid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    payment_method_body = {
        "type": "pix",
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[phone]": str(billing.get("phone") or ""),
        "billing_details[tax_id]": str(billing.get("tax_id") or ""),
        "billing_details[address][country]": "BR",
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][line2]": str(billing.get("line2") or ""),
        "billing_details[address][city]": billing["city"],
        "billing_details[address][postal_code]": billing["postal_code"],
        "billing_details[address][state]": billing["state"],
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "_stripe_version": STRIPE_VERSION_FULL,
        "key": publishable_key,
        "payment_user_agent": (
            f"stripe.js/{stripe_runtime}; stripe-js-v3/{stripe_runtime}; checkout"
        ),
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
    }
    config_id = str(init_payload.get("config_id") or "")
    if config_id:
        payment_method_body["client_attribution_metadata[checkout_config_id]"] = config_id
    payment_method_response = provider_session.post(
        "https://api.stripe.com/v1/payment_methods",
        data=payment_method_body,
        headers=opll_pix_v3_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if payment_method_response.status_code != 200:
        raise RuntimeError(
            f"PIX 3.0 payment method failed: HTTP {payment_method_response.status_code} "
            f"{payment_method_response.text[:1000]}"
        )
    payment_method_id = str((payment_method_response.json() or {}).get("id") or "")
    if not payment_method_id.startswith("pm_"):
        raise RuntimeError(f"PIX 3.0 payment method missing id: {payment_method_response.text[:500]}")

    _emit_payment_stage(progress_callback, "pix3_confirm",
                        "PIX 3.0：Stripe custom confirm", 10, total)
    success_url = (
        f"https://chatgpt.com/backend-api/payments/checkout/{processor_entity}/{checkout_id}/success?"
        "billing_country=BR"
    )
    return_url = (
        f"https://checkout.stripe.com/c/pay/{checkout_id}?returned_from_redirect=true&ui_mode=custom&"
        f"return_url={quote(success_url, safe='')}"
    )
    confirm_body = {
        "eid": "NA",
        "payment_method": payment_method_id,
        "expected_amount": amount,
        "tax_id_collection[purchasing_as_business]": "false",
        "expected_payment_method_type": "pix",
        "return_url": return_url,
        "_stripe_version": STRIPE_VERSION_FULL,
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": publishable_key,
        "version": stripe_runtime,
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "link_brand": "link",
        **opll_pix_v3_elements_params(stripe_js_id, elements_session_id),
    }
    if config_id:
        confirm_body["client_attribution_metadata[checkout_config_id]"] = config_id
    confirm_response = provider_session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/confirm",
        data=confirm_body,
        headers=opll_pix_v3_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if confirm_response.status_code != 200:
        raise RuntimeError(
            f"PIX 3.0 confirm failed: HTTP {confirm_response.status_code} {confirm_response.text[:1000]}"
        )
    confirm_payload = confirm_response.json() or {}
    submission = opll_find_submission_attempt(confirm_payload)
    submission_attempt_id = str(submission.get("id") or "").strip()
    pix = opll_extract_pix_link(confirm_payload)

    _emit_payment_stage(progress_callback, "pix3_approve_poll",
                        "PIX 3.0：OpenAI confirm/approve 并轮询 Stripe PIX", 11, total)
    checkout_confirm_path = "/backend-api/payments/checkout/confirm"
    checkout_confirm_response = provider_session.post(
        f"https://chatgpt.com{checkout_confirm_path}",
        json={
            "checkout_session_id": checkout_id,
            "selected_payment_method_type": "pix",
        },
        headers=opll_pix_v3_chatgpt_headers(
            token,
            referer=f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}",
            target_path=checkout_confirm_path,
            chatgpt_cookie=chatgpt_cookie,
        ),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    try:
        checkout_confirm_payload = checkout_confirm_response.json() or {}
    except Exception:
        checkout_confirm_payload = {"raw": checkout_confirm_response.text[:500]}
    pix = opll_merge_pix_extract(pix, opll_extract_pix_link(checkout_confirm_payload))
    confirm_result = str(
        checkout_confirm_payload.get("result") if isinstance(checkout_confirm_payload, dict) else ""
    ).strip().lower()

    approve_payload = {}
    if confirm_result != "approved":
        approve_retry_max = _opll_pix_v3_env_int("PIX_V3_APPROVE_RETRY_MAX", 60, 1, 80)
        approve_path = "/backend-api/payments/checkout/approve"
        last_approve_error = ""
        for retry_index in range(1, approve_retry_max + 1):
            approve_body = {
                "checkout_session_id": checkout_id,
                "processor_entity": processor_entity,
            }
            if submission_attempt_id:
                approve_body["submission_attempt_id"] = submission_attempt_id
            approve_response = provider_session.post(
                f"https://chatgpt.com{approve_path}",
                json=approve_body,
                headers=opll_pix_v3_chatgpt_headers(
                    token,
                    referer=f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}",
                    target_path=approve_path,
                    chatgpt_cookie=chatgpt_cookie,
                ),
                timeout=PAY_LONG_LINK_TIMEOUT,
            )
            try:
                approve_payload = approve_response.json() or {}
            except Exception:
                approve_payload = {"raw": approve_response.text[:500]}
            approved = bool(
                approve_response.status_code < 400
                and isinstance(approve_payload, dict)
                and str(approve_payload.get("result") or "").strip().lower() == "approved"
            )
            if approved:
                last_approve_error = ""
                break
            last_approve_error = (
                f"PIX 3.0 approve failed: HTTP {approve_response.status_code} "
                f"{approve_response.text[:500]}"
            )
            if retry_index < approve_retry_max:
                time.sleep(0.15)
        if last_approve_error:
            raise RuntimeError(last_approve_error)
    else:
        approve_payload = checkout_confirm_payload

    pix = opll_merge_pix_extract(pix, opll_extract_pix_link(approve_payload))
    poll_timeout = _opll_pix_v3_env_int("PIX_V3_POLL_TIMEOUT", 120, 5, 300)
    poll_params = {
        "key": publishable_key,
        "_stripe_version": STRIPE_VERSION_FULL,
        **opll_pix_v3_elements_params(stripe_js_id, elements_session_id),
    }
    deadline = time.time() + poll_timeout
    last_poll_payload = {}
    while time.time() < deadline:
        if (
            pix.get("pix_hosted_instructions_url")
            or pix.get("pix_payload")
            or opll_is_real_pix_qr_image_url(str(pix.get("pix_qr_image_url") or ""))
        ):
            break
        poll_response = provider_session.get(
            f"https://api.stripe.com/v1/payment_pages/{checkout_id}",
            params=poll_params,
            headers=opll_pix_v3_stripe_headers(publishable_key, checkout_page),
            timeout=8,
        )
        if poll_response.status_code == 200:
            last_poll_payload = poll_response.json() or {}
            pix = opll_merge_pix_extract(pix, opll_extract_pix_link(last_poll_payload))
            polled_submission = opll_find_submission_attempt(last_poll_payload)
            if polled_submission.get("state") == "failed":
                raise RuntimeError(
                    f"PIX 3.0 Stripe poll failed: {opll_compact_submission_failure(last_poll_payload, {})}"
                )
        if not (pix.get("pix_hosted_instructions_url") or pix.get("pix_payload")):
            time.sleep(1)

    if not (pix.get("pix_hosted_instructions_url") or pix.get("pix_payload")):
        refresh_payload, stripe_js_id = opll_pix_v3_stripe_init(
            provider_session, checkout_id, publishable_key, checkout_page,
        )
        opll_pix_v3_assert_init(
            refresh_payload, "PIX 轮询后刷新", require_zero=True, require_pix=True,
        )
        pix = opll_merge_pix_extract(pix, opll_extract_pix_link(refresh_payload))
        init_payload = refresh_payload

    pix = opll_hydrate_pix_artifacts(
        provider_session,
        br_proxy_url,
        pix,
        str(pix.get("pix_hosted_instructions_url") or ""),
        str(pix.get("pix_instructions_url") or ""),
        str(pix.get("pix_checkout_url") or ""),
        str(pix.get("pix_redirect_url") or ""),
        checkout_page,
        f"https://pay.openai.com/c/pay/{checkout_id}",
    )
    instructions_url = str(
        pix.get("pix_hosted_instructions_url")
        or pix.get("pix_instructions_url")
        or ""
    ).strip()
    pix_payload = str(pix.get("pix_payload") or "").strip()
    qr_image_url = str(pix.get("pix_qr_image_url") or "").strip()
    if not opll_is_real_pix_qr_image_url(qr_image_url):
        qr_image_url = ""
    pix_qr_image_data_url = opll_make_qr_data_url(pix_payload) if pix_payload else ""
    primary = instructions_url or pix_payload or qr_image_url
    if not primary:
        raise RuntimeError(
            "PIX 3.0 completed custom confirm/approve/poll without PIX artifact; "
            f"submission_state={submission.get('state') or '-'}"
        )

    _emit_payment_stage(progress_callback, "pix3_done",
                        "PIX 3.0：巴西 PIX 链与二维码提取完成", 12, total)
    expires_at, expires_raw = opll_checkout_expires_at(
        raw_checkout, init_payload, confirm_payload, last_poll_payload, pix,
    )
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    payment_methods = opll_collect_payment_method_types(init_payload)
    return {
        **{k: v for k, v in checkout.items() if k != "raw_checkout" and not k.startswith("_")},
        "payment_method_country": "BR",
        "payment_method_id": payment_method_id,
        "stripe_hosted_url": str(init_payload.get("stripe_hosted_url") or checkout_page),
        "stripe_redirect_url": instructions_url,
        "provider_redirect_url": primary,
        "long_url": primary,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": f"pix_v3.{stripe_amount_source}",
        "payment_amount_display": opll_format_minor_amount(stripe_amount, "BRL"),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "pix_v3",
        "local_payment_version": "3.0",
        "local_payment_flow": "custom_exact",
        "local_payment_detected": True,
        "payment_methods": payment_methods,
        "payment_locale": "pt-BR",
        "browser_timezone": "America/Sao_Paulo",
        "billing_email": billing["email"],
        "pix_billing": billing,
        "pix_link": instructions_url or primary,
        "pix_instructions_url": instructions_url,
        "pix_checkout_url": str(pix.get("pix_checkout_url") or checkout_page),
        "pix_openai_pay_url": str(pix.get("pix_openai_pay_url") or ""),
        "pix_redirect_url": str(pix.get("pix_redirect_url") or ""),
        "pix_payload": pix_payload,
        "pix_qr_image_url": qr_image_url,
        "pix_qr_image_data_url": pix_qr_image_data_url,
        "pix_hosted_instructions_url": instructions_url,
        "pix_resource_url": str(pix.get("pix_resource_url") or ""),
        "pix_source": str(pix.get("source") or "pix3_custom_exact"),
        "pix_hydration_error": str(pix.get("pix_hydration_error") or ""),
        "pix3_approval": approve_payload,
        "pix3_checkout_confirm": checkout_confirm_payload,
        "pix3_steps": [
            "br_auth", "br_custom_checkout", "br_bootstrap_init", "promo_update",
            "br_refresh", "br_checkout_taxes", "br_stripe_tax", "br_pre_confirm",
            "pix_payment_method", "pix_custom_confirm", "openai_approve_poll", "pix_hydrate",
        ],
        "promotion_update": promotion_payload,
        "checkout_tax_update": checkout_tax_payload,
        "stripe_tax_update": stripe_tax_payload,
        "confirm_payload": confirm_payload,
        "bootstrap_init": {
            "amount": opll_pix_v3_expected_amount(bootstrap_payload),
            "currency": str(bootstrap_payload.get("currency") or ""),
            "payment_method_types": bootstrap_payload.get("payment_method_types") or [],
        },
        "requires_chatgpt_cookie": False,
        "chatgpt_cookie_used": bool(str(chatgpt_cookie or "").strip()),
    }


def generate_opll_pix_normal_qr(access_token: str,
                                br_proxy_url: str = "",
                                progress_callback=None,
                                chatgpt_cookie: str = "") -> dict:
    """Normal Brazil PIX QR extraction.

    This is the clean payment flow: BR/BRL checkout without promo, Stripe init,
    create PIX payment_method, confirm, then extract hosted instructions / PIX
    copia-e-cola / QR image. It intentionally does not apply 0-BRL promo logic.
    """
    token = opll_access_token_with_cookie(access_token, chatgpt_cookie)
    br_proxy_url = str(br_proxy_url or "").strip()
    if not token:
        raise RuntimeError("PIX normal cannot parse Access Token")
    if not br_proxy_url:
        raise RuntimeError("PIX normal requires BR proxy")

    payment_locale = "pt-BR"
    browser_timezone = "America/Sao_Paulo"
    total = 7

    _emit_payment_stage(progress_callback, "pix_normal_checkout", "PIX normal: create BR/BRL checkout", 1, total)
    checkout = opll_create_checkout(
        token, "BR", "BRL", br_proxy_url,
        checkout_ui_mode="custom",
        require_stripe_session=True,
        preferred_processor_entity=opll_processor_entity_for_country("BR"),
        promo_campaign_id=None,
    )
    if not checkout.get("processor_entity"):
        checkout["processor_entity"] = opll_processor_entity_for_country("BR")
    if not checkout.get("stripe_publishable_key"):
        checkout["stripe_publishable_key"] = DEFAULT_STRIPE_PK
    cs_id = str(checkout.get("cs_id") or checkout.get("checkout_id") or "")
    stripe_pk = opll_stripe_key_for_checkout(checkout)

    stripe = opll_build_stripe_session(br_proxy_url)
    stripe.headers.update({"User-Agent": PIX_USER_AGENT})
    ctx_seed = {
        "stripe_js_id": str(uuid.uuid4()),
        "browser_timezone": browser_timezone,
        "saved_payment_method_mode": "never",
        "runtime_version": PIX_STRIPE_RUNTIME_VERSION,
        "stripe_version": PIX_STRIPE_VERSION_FULL,
    }

    _emit_payment_stage(progress_callback, "pix_normal_init", "PIX normal: Stripe init", 2, total)
    init_payload = opll_stripe_init(
        cs_id, "BR", "BRL", br_proxy_url,
        payment_locale=payment_locale,
        stripe=stripe,
        ctx=ctx_seed,
        checkout=checkout,
        browser_timezone=browser_timezone,
        saved_payment_method_mode="never",
    )
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(f"PIX normal stripe init missing stripe_hosted_url, keys={sorted(init_payload.keys())}")
    ctx = opll_stripe_context(init_payload, payment_locale, ctx_seed)
    ctx["runtime_version"] = PIX_STRIPE_RUNTIME_VERSION
    ctx["stripe_version"] = PIX_STRIPE_VERSION_FULL
    ctx["browser_timezone"] = browser_timezone
    ctx["saved_payment_method_mode"] = "never"
    ctx["currency"] = "brl"

    payment_methods = opll_collect_payment_method_types(init_payload)
    if not opll_payment_method_available(init_payload, "pix"):
        raise RuntimeError("PIX normal page did not expose pix; " + opll_payment_method_diagnostics(init_payload))
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    expires_at, expires_raw = opll_checkout_expires_at(checkout, init_payload)

    billing = opll_brazil_pix_billing(token)
    _emit_payment_stage(progress_callback, "pix_normal_pm", "PIX normal: create PIX payment method", 3, total)
    pm_id = opll_stripe_create_pix_method(stripe, cs_id, ctx, billing, stripe_pk)

    _emit_payment_stage(progress_callback, "pix_normal_confirm", "PIX normal: confirm Stripe PIX", 4, total)
    confirm_payload = opll_stripe_confirm(
        stripe, cs_id, pm_id, stripe_pk, init_payload, ctx, checkout,
        stripe_hosted_url, payment_method_type="pix",
    )
    pix = opll_extract_pix_link(confirm_payload)

    pix_approval_trace: list[str] = []
    def _has_normal_pix_artifact(p: dict) -> bool:
        return bool(
            p.get("pix_hosted_instructions_url")
            or p.get("pix_instructions_url")
            or p.get("pix_payload")
            or opll_is_real_pix_qr_image_url(str(p.get("pix_qr_image_url") or ""))
        )

    if not _has_normal_pix_artifact(pix):
        _emit_payment_stage(progress_callback, "pix_normal_approve", "PIX normal: approve / poll", 5, total)
        try:
            polled_pix = opll_stripe_payment_page_pix_extract(
                stripe, cs_id, stripe_pk, payment_locale=payment_locale, timeout_seconds=12, ctx=ctx)
            pix = opll_merge_pix_extract(pix, polled_pix)
        except OpllStripeRequiresApproval:
            routes = [("BR", br_proxy_url), ("direct", "")]
            for route_label, route_proxy in routes:
                try:
                    info = opll_chatgpt_approve_with_routes(
                        token, cs_id, checkout, [(route_label, route_proxy)],
                        chatgpt_cookie=chatgpt_cookie, attempts_per_route=2)
                    pix_approval_trace.extend(info.get("errors") or [])
                    pix_approval_trace.append(f"approved:{info.get('route')}#{info.get('attempt')}")
                except Exception as exc:
                    pix_approval_trace.append(f"approve_failed:{route_label}:{opll_short_error(str(exc), 180)}")
                    continue
                try:
                    polled_pix = opll_stripe_payment_page_pix_extract(
                        stripe, cs_id, stripe_pk, payment_locale=payment_locale, timeout_seconds=30, ctx=ctx)
                    pix = opll_merge_pix_extract(pix, polled_pix)
                    if _has_normal_pix_artifact(pix):
                        break
                except Exception as exc:
                    pix_approval_trace.append(f"poll_error:{opll_short_error(str(exc), 180)}")
        except Exception as exc:
            pix_approval_trace.append(f"poll_error:{opll_short_error(str(exc), 180)}")

    pix_payload = str(pix.get("pix_payload") or "")
    pix_qr_image_data_url = opll_make_qr_data_url(pix_payload) if pix_payload else ""
    instructions_url = str(pix.get("pix_hosted_instructions_url") or pix.get("pix_instructions_url") or "").strip()
    qr_image_url = str(pix.get("pix_qr_image_url") or "")
    if not opll_is_real_pix_qr_image_url(qr_image_url):
        qr_image_url = ""
    primary = instructions_url or pix_payload or qr_image_url
    if not primary:
        submission = opll_find_submission_attempt(confirm_payload)
        submission_state = str(submission.get("state") or "-") if isinstance(submission, dict) else "-"
        payment_status = str(confirm_payload.get("payment_status") or "-") if isinstance(confirm_payload, dict) else "-"
        raise RuntimeError(
            "PIX normal QR not extracted. "
            f"amount={stripe_amount}({stripe_amount_source}); "
            f"payment_status={payment_status}; submission_state={submission_state}; "
            f"resource={pix.get('pix_resource_url') or pix.get('pix_qr_image_url') or '-'}; "
            f"trace={' > '.join(pix_approval_trace[-8:]) if pix_approval_trace else '-'}"
        )

    _emit_payment_stage(progress_callback, "pix_normal_done", "PIX normal: QR extracted", 7, total)
    return {
        **{k: v for k, v in checkout.items() if k not in ("raw_checkout",)},
        "payment_method_country": "BR",
        "payment_method_id": pm_id,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": instructions_url or str(pix.get("pix_link") or ""),
        "provider_redirect_url": primary,
        "long_url": primary,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, "BRL"),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "pix",
        "local_payment_detected": True,
        "payment_methods": payment_methods,
        "payment_locale": payment_locale,
        "browser_timezone": browser_timezone,
        "billing_email": str(billing.get("email") or ""),
        "pix_link": instructions_url or str(pix.get("pix_link") or primary),
        "pix_instructions_url": instructions_url,
        "pix_checkout_url": str(pix.get("pix_checkout_url") or ""),
        "pix_openai_pay_url": str(pix.get("pix_openai_pay_url") or ""),
        "pix_redirect_url": str(pix.get("pix_redirect_url") or ""),
        "pix_payload": pix_payload,
        "pix_qr_image_url": qr_image_url,
        "pix_qr_image_data_url": pix_qr_image_data_url,
        "pix_hosted_instructions_url": instructions_url,
        "pix_resource_url": str(pix.get("pix_resource_url") or ""),
        "pix_source": str(pix.get("source") or "pix_normal_qr"),
        "pix_approval_trace": pix_approval_trace[-12:],
        "promotion_update": {"skipped": True, "reason": "normal PIX QR flow"},
        "tax_update": {"skipped": True, "reason": "normal PIX QR flow"},
    }


_PIX_STANDALONE_MODULE = None


def opll_load_pix_standalone_module():
    global _PIX_STANDALONE_MODULE
    if _PIX_STANDALONE_MODULE is not None:
        return _PIX_STANDALONE_MODULE
    module_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pix_standalone_extract.py")
    if not os.path.isfile(module_path):
        raise RuntimeError(f"pix_standalone_extract.py not found: {module_path}")
    spec = importlib.util.spec_from_file_location("pix_standalone_extract", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load pix_standalone_extract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    _PIX_STANDALONE_MODULE = module
    return module


def generate_opll_pix_standalone_zero(access_token: str,
                                      br_entry_proxy_url: str = "",
                                      promo_proxy_url: str = "",
                                      br_approve_proxy_url: str = "",
                                      progress_callback=None,
                                      chatgpt_cookie: str = "") -> dict:
    """Run the provided standalone PIX 0-BRL extractor as the primary engine."""
    token = opll_access_token_with_cookie(access_token, chatgpt_cookie)
    br_entry_proxy_url = str(br_entry_proxy_url or "").strip()
    promo_proxy_url = str(promo_proxy_url or "").strip() or br_entry_proxy_url
    br_approve_proxy_url = str(br_approve_proxy_url or "").strip() or br_entry_proxy_url
    if not token:
        raise RuntimeError("PIX standalone cannot parse Access Token")
    if not br_entry_proxy_url:
        raise RuntimeError("PIX standalone requires BR proxy")
    module = opll_load_pix_standalone_module()
    # Keep the provided standalone engine as the primary flow, but use this
    # app's Playwright launcher/filler so Windows can reuse installed Chrome or
    # Edge when bundled Chromium is absent and so hidden/moved fields do not
    # break the hosted-checkout confirm step.
    if hasattr(module, "browser_confirm_zero_pix"):
        module.browser_confirm_zero_pix = opll_browser_confirm_zero_pix
    _emit_payment_stage(progress_callback, "pix_standalone", "PIX standalone source: create/update/browser-confirm/poll", 1, 1)
    result = module.generate_pix_link(
        token,
        create_proxy_url=br_entry_proxy_url,
        followup_proxy_url=promo_proxy_url,
        approve_proxy_url=br_approve_proxy_url,
        # Force plain requests here because curl_cffi can throw TLS library
        # errors through local GOST HTTP bridges on Windows.
        http_backend="requests",
        promo_campaign_id="plus-1-month-free",
        pix_mode="promo_zero",
        timeout=PAY_LONG_LINK_TIMEOUT,
        poll_seconds=60,
        verbose=False,
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"PIX standalone returned non-dict result: {type(result)}")
    pix_payload = str(result.get("pix_copy_paste") or result.get("pix_payload") or "").strip()
    instructions_url = str(result.get("pix_hosted_instructions_url") or result.get("pix_instructions_url") or "").strip()
    qr_image_url = str(result.get("pix_image_url_png") or result.get("pix_image_url_svg") or result.get("pix_qr_image_url") or "").strip()
    pix_qr_image_data_url = opll_make_qr_data_url(pix_payload) if pix_payload else ""
    primary = instructions_url or pix_payload or qr_image_url or str(result.get("long_url") or "").strip()
    if not primary:
        raise RuntimeError(f"PIX standalone did not return a usable PIX artifact; keys={sorted(result.keys())[:20]}")
    stripe_amount = str(result.get("stripe_amount") or "0")
    stripe_amount_source = str(result.get("stripe_amount_source") or "")
    return {
        "cs_id": str(result.get("cs_id") or ""),
        "checkout_id": str(result.get("cs_id") or ""),
        "processor_entity": "openai_ie",
        "billing_country": "BR",
        "currency": "BRL",
        "payment_method_country": "BR",
        "payment_method_id": str(result.get("payment_method_id") or result.get("payment_method") or ""),
        "stripe_hosted_url": str(result.get("stripe_hosted_url") or ""),
        "stripe_redirect_url": instructions_url,
        "provider_redirect_url": primary,
        "long_url": primary,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, "BRL"),
        "expires_at": 0,
        "expires_raw": str(result.get("pix_expires_at") or ""),
        "valid_seconds": 0,
        "local_payment": "pix",
        "local_payment_detected": True,
        "payment_methods": ["pix"],
        "payment_locale": "pt-BR",
        "browser_timezone": "America/Sao_Paulo",
        "pix_link": instructions_url or primary,
        "pix_instructions_url": instructions_url,
        "pix_checkout_url": "",
        "pix_openai_pay_url": "",
        "pix_redirect_url": "",
        "pix_payload": pix_payload,
        "pix_qr_image_url": qr_image_url if opll_is_real_pix_qr_image_url(qr_image_url) else "",
        "pix_qr_image_data_url": pix_qr_image_data_url,
        "pix_hosted_instructions_url": instructions_url,
        "pix_resource_url": "",
        "pix_source": "standalone_pix_extract.py",
        "pix_approval_trace": ["standalone_source_engine"],
        "promotion_update": {"engine": "standalone", "promo_applied": bool(result.get("promo_applied"))},
        "standalone_raw": {k: v for k, v in result.items() if k not in ("access_token", "token")},
    }


def generate_opll_pix_long_link(access_token: str,
                                br_entry_proxy_url: str = "",
                                vn_proxy_url: str = "",
                                br_final_proxy_url: str = "",
                                progress_callback=None,
                                chatgpt_cookie: str = "",
                                prefer_post_promo_pm: bool = False) -> dict:
    """Brazil PIX extraction: BR checkout -> enter PIX -> optional promo -> BR PIX confirm -> extract QR/link."""
    token = opll_access_token_with_cookie(access_token, chatgpt_cookie)
    br_entry_proxy_url = str(br_entry_proxy_url or "").strip()
    vn_proxy_url = opll_normalize_vn_country_proxy(str(vn_proxy_url or "").strip())
    br_final_proxy_url = str(br_final_proxy_url or br_entry_proxy_url or "").strip()
    if not token:
        raise RuntimeError("PIX cannot parse Access Token")
    if not br_entry_proxy_url:
        raise RuntimeError("PIX requires BR stage 1 proxy pool")
    if not br_final_proxy_url:
        raise RuntimeError("PIX requires BR stage 3 proxy pool")

    payment_locale = "pt-BR"
    browser_timezone = "America/Sao_Paulo"
    total = 10

    _emit_payment_stage(progress_callback, "pix_checkout", "PIX: BR stage 1 create checkout", 1, total)
    checkout = opll_create_checkout(
        token, "BR", "BRL", br_entry_proxy_url,
        checkout_ui_mode="custom",
        require_stripe_session=True,
        preferred_processor_entity=opll_processor_entity_for_country("BR"),
        promo_campaign_id=None,
    )
    if not checkout.get("processor_entity"):
        checkout["processor_entity"] = opll_processor_entity_for_country("BR")
    if not checkout.get("stripe_publishable_key"):
        checkout["stripe_publishable_key"] = DEFAULT_STRIPE_PK
    cs_id = str(checkout.get("cs_id") or checkout.get("checkout_id") or "")
    stripe_pk = opll_stripe_key_for_checkout(checkout)

    # PIX "enter first, promo later" path:
    # 1) create BR checkout without promo
    # 2) initialize Stripe while full-price PIX is still visible
    # 3) create/preselect a PIX payment_method
    # 4) apply promo to 0 BRL
    # 5) confirm with the preselected PIX PM
    # This handles the common case where the 0-BRL page only lists card/apple_pay.
    stripe = opll_build_stripe_session(br_final_proxy_url)
    stripe.headers.update({"User-Agent": PIX_USER_AGENT})
    billing = opll_brazil_pix_billing(token)
    ctx_seed = {
        "stripe_js_id": str(uuid.uuid4()),
        "browser_timezone": browser_timezone,
        "saved_payment_method_mode": "never",
        "runtime_version": PIX_STRIPE_RUNTIME_VERSION,
        "stripe_version": PIX_STRIPE_VERSION_FULL,
    }

    _emit_payment_stage(progress_callback, "pix_pre_init", "PIX: pre-init full price to expose PIX", 2, total)
    pre_init_payload = opll_stripe_init(
        cs_id, "BR", "BRL", br_final_proxy_url,
        payment_locale=payment_locale,
        stripe=stripe,
        ctx=ctx_seed,
        checkout=checkout,
        browser_timezone=browser_timezone,
        saved_payment_method_mode="never",
    )
    pre_ctx = opll_stripe_context(pre_init_payload, payment_locale, ctx_seed)
    pre_ctx["runtime_version"] = PIX_STRIPE_RUNTIME_VERSION
    pre_ctx["stripe_version"] = PIX_STRIPE_VERSION_FULL
    pre_ctx["browser_timezone"] = browser_timezone
    pre_ctx["saved_payment_method_mode"] = "never"
    pre_ctx["currency"] = "brl"
    preselected_pm_id = ""
    pre_payment_methods = opll_collect_payment_method_types(pre_init_payload)
    if opll_payment_method_available(pre_init_payload, "pix"):
        _emit_payment_stage(progress_callback, "pix_preselect", "PIX: preselect full-price PIX payment method", 2, total)
        preselected_pm_id = opll_stripe_create_pix_method(stripe, cs_id, pre_ctx, billing, stripe_pk)

    promo_stage_text = "VN stage 2" if vn_proxy_url else "BR-only fallback"
    _emit_payment_stage(progress_callback, "pix_promo", f"PIX: {promo_stage_text} checkout/update promotion", 3, total)
    promotion_errors: list[str] = []
    promotion_payload = None
    promo_candidates = []
    if vn_proxy_url:
        promo_candidates.append(("VN", vn_proxy_url, True))
    promo_candidates.extend([
        ("BR-3", br_final_proxy_url, False),
        ("BR-1", br_entry_proxy_url, False),
        ("direct", "", False),
    ])
    seen_promo_proxies: set[str] = set()
    for promo_label, promo_proxy, promo_normalize_vn in promo_candidates:
        promo_key = f"{promo_label}:{promo_proxy}"
        if promo_key in seen_promo_proxies:
            continue
        seen_promo_proxies.add(promo_key)
        try:
            promotion_payload = opll_chatgpt_checkout_update_promotion(
                token, checkout, promo_proxy, chatgpt_cookie=chatgpt_cookie,
                normalize_vn=promo_normalize_vn)
            if isinstance(promotion_payload, dict):
                promotion_payload["_promo_stage"] = promo_label
            break
        except Exception as exc:
            promotion_errors.append(f"{promo_label}: {opll_short_error(str(exc), 180)}")
    if promotion_payload is None:
        raise RuntimeError("PIX checkout/update promotion failed on all routes: " + " | ".join(promotion_errors))

    _emit_payment_stage(progress_callback, "pix_init_1", "PIX: BR stage 3 Stripe init after promo", 4, total)
    init_payload = opll_stripe_init(
        cs_id, "BR", "BRL", br_final_proxy_url,
        payment_locale=payment_locale,
        stripe=stripe,
        ctx=ctx_seed,
        checkout=checkout,
        browser_timezone=browser_timezone,
        saved_payment_method_mode="never",
    )
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(f"PIX stripe init response missing stripe_hosted_url, keys={sorted(init_payload.keys())}")
    ctx = opll_stripe_context(init_payload, payment_locale, ctx_seed)
    ctx["runtime_version"] = PIX_STRIPE_RUNTIME_VERSION
    ctx["stripe_version"] = PIX_STRIPE_VERSION_FULL
    ctx["browser_timezone"] = browser_timezone
    ctx["saved_payment_method_mode"] = "never"
    ctx["currency"] = "brl"

    tax_payload: dict = {"skipped": True, "reason": "pix-first flow; tax sync only runs as fallback"}
    tax_update_payload: dict = {"skipped": True, "reason": "pix-first flow; tax sync only runs as fallback"}

    _emit_payment_stage(progress_callback, "pix_init_3", "PIX: BR refresh PIX methods", 7, total)
    init_payload = opll_stripe_init(
        cs_id, "BR", "BRL", br_final_proxy_url,
        payment_locale=payment_locale,
        stripe=stripe,
        ctx=ctx,
        checkout=checkout,
        browser_timezone=browser_timezone,
        saved_payment_method_mode="never",
    )
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or stripe_hosted_url).strip()
    payment_methods = opll_collect_payment_method_types(init_payload)
    if not opll_payment_method_available(init_payload, "pix") and not preselected_pm_id:
        # Fallback only: the standalone PIX logic does not need taxes when the
        # BR checkout already exposes pix. Running taxes every time added an
        # extra ChatGPT/Stripe mutation that made approve more fragile.
        _emit_payment_stage(progress_callback, "pix_tax_fallback", "PIX: fallback tax/address sync", 7, total)
        try:
            tax_payload = opll_chatgpt_update_pix_taxes(
                token, checkout, br_final_proxy_url, billing=billing, chatgpt_cookie=chatgpt_cookie)
        except Exception as exc:
            tax_payload = {"error": opll_short_error(str(exc), 240)}
        try:
            tax_update_payload = opll_stripe_update_tax_region(
                stripe, cs_id, stripe_pk, ctx, billing,
                payment_locale=payment_locale,
                browser_timezone=browser_timezone,
                saved_payment_method_mode="never",
            )
            if isinstance(tax_update_payload, dict) and tax_update_payload:
                stripe_hosted_url = str(tax_update_payload.get("stripe_hosted_url") or stripe_hosted_url).strip()
        except Exception as exc:
            tax_update_payload = {"error": opll_short_error(str(exc), 240)}
        init_payload = opll_stripe_init(
            cs_id, "BR", "BRL", br_final_proxy_url,
            payment_locale=payment_locale,
            stripe=stripe,
            ctx=ctx,
            checkout=checkout,
            browser_timezone=browser_timezone,
            saved_payment_method_mode="never",
        )
        stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or stripe_hosted_url).strip()
        payment_methods = opll_collect_payment_method_types(init_payload)
        if not opll_payment_method_available(init_payload, "pix"):
            raise RuntimeError(
                "PIX method not exposed after BR PIX flow; "
                f"{opll_payment_method_diagnostics(init_payload)}; "
                f"tax_fallback={opll_short_error(str(tax_payload), 180)} / {opll_short_error(str(tax_update_payload), 180)}"
            )
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    expires_at, expires_raw = opll_checkout_expires_at(checkout, init_payload)
    ctx = opll_stripe_context(init_payload, payment_locale, ctx)
    ctx["runtime_version"] = PIX_STRIPE_RUNTIME_VERSION
    ctx["stripe_version"] = PIX_STRIPE_VERSION_FULL
    ctx["browser_timezone"] = browser_timezone
    ctx["saved_payment_method_mode"] = "never"
    ctx["currency"] = "brl"

    pm_selection_trace: list[str] = []
    confirm_errors: list[str] = []
    confirm_payload: dict = {}
    pm_id = ""
    pm_label_used = ""
    zero_amount = str(stripe_amount).strip() in {"0", "0.0", "0.00"}

    if zero_amount:
        _emit_payment_stage(progress_callback, "pix_browser_confirm", "PIX: browser select PIX and confirm 0-BRL", 8, total)
        try:
            # This is the key difference from the standalone working source:
            # 0-BRL PIX is confirmed in Stripe Hosted Checkout so Stripe builds
            # the PIX recurring mandate instead of failing the API-only confirm.
            confirm_payload = opll_browser_confirm_zero_pix(
                stripe_hosted_url,
                br_final_proxy_url or br_entry_proxy_url,
                billing,
                timeout=PAY_LONG_LINK_TIMEOUT,
            )
            pm_id = "browser-created"
            pm_label_used = "browser_zero"
            pm_selection_trace.append("browser_zero_confirm:ok")
        except Exception as exc:
            confirm_errors.append(f"browser_zero:{opll_short_error(str(exc), 260)}")
            # Keep a non-browser fallback for environments without Playwright or
            # without a usable installed Chrome/Edge, but the browser path is
            # preferred for 0-BRL.

    if not confirm_payload:
        pm_candidates: list[tuple[str, str]] = []
        postpromo_pm_id = ""
        if prefer_post_promo_pm and opll_payment_method_available(init_payload, "pix"):
            _emit_payment_stage(progress_callback, "pix_method_after_promo", "PIX: create PIX payment method after promo", 8, total)
            try:
                postpromo_pm_id = opll_stripe_create_pix_method(stripe, cs_id, ctx, billing, stripe_pk)
                pm_selection_trace.append("after_promo_pm:ok")
                pm_candidates.append(("after_promo", postpromo_pm_id))
            except Exception as exc:
                pm_selection_trace.append(f"after_promo_pm_failed:{opll_short_error(str(exc), 160)}")
        if preselected_pm_id:
            pm_selection_trace.append("preselected_pm:ok")
            pm_candidates.append(("preselected", preselected_pm_id))
        if not pm_candidates:
            _emit_payment_stage(progress_callback, "pix_method", "PIX: create Stripe PIX payment method", 8, total)
            pm_id_direct = opll_stripe_create_pix_method(stripe, cs_id, ctx, billing, stripe_pk)
            pm_selection_trace.append("direct_pm:ok")
            pm_candidates.append(("direct", pm_id_direct))

        _emit_payment_stage(progress_callback, "pix_confirm", "PIX: confirm/approve Stripe PIX", 9, total)
        for candidate_label, candidate_pm_id in pm_candidates:
            try:
                candidate_confirm = opll_stripe_confirm(
                    stripe, cs_id, candidate_pm_id, stripe_pk, init_payload, ctx, checkout,
                    stripe_hosted_url, payment_method_type="pix",
                )
                submission = opll_find_submission_attempt(candidate_confirm)
                if submission.get("state") == "failed" and len(pm_candidates) > 1:
                    confirm_errors.append(f"{candidate_label}:failed:{opll_compact_submission_failure(candidate_confirm, ctx)}")
                    continue
                confirm_payload = candidate_confirm
                pm_id = candidate_pm_id
                pm_label_used = candidate_label
                pm_selection_trace.append(f"confirm_pm:{candidate_label}")
                break
            except Exception as exc:
                confirm_errors.append(f"{candidate_label}:{opll_short_error(str(exc), 220)}")
        if not confirm_payload:
            raise RuntimeError("PIX confirm failed on all payment-method candidates: " + " | ".join(confirm_errors[-6:]))
    pix = opll_extract_pix_link(confirm_payload)
    stripe_redirect_url = str(pix.get("pix_link") or "")

    # The 0-BRL PIX branch commonly returns submission_attempt.requires_approval
    # before Stripe materializes payments.stripe.com/qr/instructions/...
    # Try approve from each stage route, and poll Stripe after every successful
    # approve. This avoids spending 100 retries on the same pending approval.
    pix_approval_trace: list[str] = pm_selection_trace[-6:] + confirm_errors[-4:]
    approval_routes = [
        ("BR-3", br_final_proxy_url),
        ("BR-1", br_entry_proxy_url),
    ]
    if vn_proxy_url:
        approval_routes.append(("VN", vn_proxy_url))
    approval_routes.append(("direct", ""))

    def _poll_pix_after_approval(seconds: int) -> bool:
        nonlocal pix
        try:
            polled_pix = opll_stripe_payment_page_pix_extract(
                stripe, cs_id, stripe_pk, payment_locale=payment_locale,
                timeout_seconds=seconds, ctx=ctx)
            pix = opll_merge_pix_extract(pix, polled_pix)
        except OpllStripeRequiresApproval as exc:
            pix_approval_trace.append(f"poll_requires_approval:{opll_short_error(str(exc), 80)}")
        except Exception as exc:
            pix_approval_trace.append(f"poll_error:{opll_short_error(str(exc), 120)}")
        return bool(pix.get("pix_hosted_instructions_url"))

    if not pix.get("pix_hosted_instructions_url"):
        _poll_pix_after_approval(6)

    if not pix.get("pix_hosted_instructions_url"):
        for approve_label, approve_proxy in approval_routes:
            try:
                info = opll_chatgpt_approve_with_routes(
                    token, cs_id, checkout, [(approve_label, approve_proxy)],
                    chatgpt_cookie=chatgpt_cookie, attempts_per_route=2)
                pix_approval_trace.extend(info.get("errors") or [])
                pix_approval_trace.append(f"approved:{info.get('route')}#{info.get('attempt')}")
            except Exception as exc:
                pix_approval_trace.append(f"approve_failed:{approve_label}:{opll_short_error(str(exc), 180)}")
                continue
            if _poll_pix_after_approval(24):
                break

    # Keep the older pay.openai.com / checkout.stripe.com URL as a secondary field,
    # but try to follow/scrape it to the desired payments.stripe.com/qr/instructions URL.
    checkout_like_url = str(pix.get("pix_checkout_url") or "")
    if not checkout_like_url and stripe_redirect_url and opll_is_openai_pay_or_checkout_url(stripe_redirect_url):
        checkout_like_url = stripe_redirect_url
        pix["pix_checkout_url"] = checkout_like_url
        if (urlsplit(checkout_like_url).netloc or "").lower() == "pay.openai.com":
            pix["pix_openai_pay_url"] = checkout_like_url
    resolved_instruction = str(pix.get("pix_hosted_instructions_url") or "").strip()
    if not resolved_instruction:
        resolved_instruction = opll_resolve_pix_instructions_url(
            stripe,
            stripe_redirect_url,
            checkout_like_url,
            stripe_hosted_url,
            opll_to_openai_pay_url(stripe_hosted_url),
        )
        if resolved_instruction:
            pix = opll_merge_pix_extract(pix, {
                "pix_link": resolved_instruction,
                "pix_hosted_instructions_url": resolved_instruction,
                "pix_instructions_url": resolved_instruction,
                "source": "resolved_pix_instructions_url",
            })

    if not resolved_instruction and not stripe_redirect_url:
        try:
            stripe_redirect_url = opll_redirect_url_after_confirm(
                token, stripe, confirm_payload, cs_id, stripe_pk, ctx, checkout,
                proxy_url=br_final_proxy_url, payment_locale=payment_locale, chatgpt_cookie=chatgpt_cookie,
            )
            more = opll_extract_pix_link(stripe_redirect_url)
            pix = opll_merge_pix_extract(pix, more)
            resolved_instruction = str(pix.get("pix_hosted_instructions_url") or "").strip()
        except Exception:
            pass
    if stripe_redirect_url and not pix.get("pix_link"):
        pix["pix_link"] = stripe_redirect_url
        pix["source"] = pix.get("source") or "stripe_redirect_url"

    pix_payload = str(pix.get("pix_payload") or "")
    pix_qr_image_data_url = opll_make_qr_data_url(pix_payload) if pix_payload else ""
    instructions_url = str(pix.get("pix_hosted_instructions_url") or pix.get("pix_instructions_url") or "").strip()
    fallback_link = str(pix.get("pix_checkout_url") or pix.get("pix_openai_pay_url") or pix.get("pix_redirect_url") or "").strip()
    if not instructions_url:
        submission = opll_find_submission_attempt(confirm_payload)
        submission_state = str(submission.get("state") or "-") if isinstance(submission, dict) else "-"
        payment_status = str(confirm_payload.get("payment_status") or "-") if isinstance(confirm_payload, dict) else "-"
        stripe_amount_now, stripe_amount_source_now = opll_stripe_amount_info(init_payload)
        approval_hint = "; approve_trace=" + " > ".join(pix_approval_trace[-8:]) if pix_approval_trace else ""
        raise RuntimeError(
            "PIX still waiting for ChatGPT approval; retrying. "
            f"kept_checkout={fallback_link or '-'}; image_resource={pix.get('pix_resource_url') or pix.get('pix_qr_image_url') or '-'}; "
            f"amount={stripe_amount_now}({stripe_amount_source_now}); "
            f"payment_status={payment_status}; submission_state={submission_state}{approval_hint}"
        )
    long_url = instructions_url

    _emit_payment_stage(progress_callback, "pix_done", "PIX: link extracted", 10, total)
    return {
        **{k: v for k, v in checkout.items() if k not in ("raw_checkout",)},
        "payment_method_country": "BR",
        "payment_method_id": pm_id,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": instructions_url or str(pix.get("pix_link") or ""),
        "long_url": long_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, "BRL"),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "pix",
        "local_payment_detected": True,
        "payment_methods": payment_methods,
        "payment_locale": payment_locale,
        "browser_timezone": browser_timezone,
        "billing_email": str(billing.get("email") or ""),
        "pix_link": instructions_url or str(pix.get("pix_link") or ""),
        "pix_instructions_url": instructions_url,
        "pix_checkout_url": str(pix.get("pix_checkout_url") or ""),
        "pix_openai_pay_url": str(pix.get("pix_openai_pay_url") or ""),
        "pix_redirect_url": str(pix.get("pix_redirect_url") or ""),
        "pix_payload": pix_payload,
        "pix_qr_image_url": str(pix.get("pix_qr_image_url") or ""),
        "pix_qr_image_data_url": pix_qr_image_data_url,
        "pix_hosted_instructions_url": str(pix.get("pix_hosted_instructions_url") or ""),
        "pix_resource_url": str(pix.get("pix_resource_url") or ""),
        "pix_source": str(pix.get("source") or ""),
        "pix_approval_trace": pix_approval_trace[-12:],
        "pix_pm_label": pm_label_used,
        "pix_post_promo_mode": bool(prefer_post_promo_pm),
        "promotion_update": promotion_payload,
        "tax_update": tax_payload,
    }


def generate_opll_paypal_long_link(access_token: str, country: str, currency: str,
                                    proxy_url: str = "",
                                    provider_proxy_url: str = "",
                                    progress_callback=None,
                                    paypal_result_mode: str = "approval",
                                    payment_locale: str = "",
                                    force_country: bool = False,
                                    chatgpt_cookie: str = "",
                                    paypal_page_country: str = "") -> dict:
    """
    Generate a PayPal BA approve long link from a ChatGPT access token.
    This is used for modes like "PayPal 长链接 US/USD", "PayPal 长链接 FR/EUR"
    and the BR/BRL PayPal-link-only extraction mode.
    """
    failures: list[str] = []
    requested_country = normalize_opll_country(country)
    requested_currency = currency_for_country(requested_country)
    loose_paypal_result = str(paypal_result_mode or "").strip().lower() in {
        "paypal_link", "paypal", "loose", "any_paypal", "any",
    }
    accept_pm_redirect_result = str(paypal_result_mode or "").strip().lower() in {
        "pm_or_paypal", "pm_redirect", "stripe_redirect", "true_no_card_us", "nocard_us",
    }
    base_locale = str(payment_locale or opll_payment_locale_for_country(requested_country)).strip() or "en"
    provider_proxy_url = str(provider_proxy_url or proxy_url or "").strip()
    # BR residential gateways commonly allow Stripe/PayPal while resetting
    # chatgpt.com.  app.py can therefore route ChatGPT through FRONT_PROXY and
    # the provider through BR directly.  Keep approve on the same ChatGPT route
    # as checkout so the provider gateway is never asked to CONNECT to ChatGPT.
    approval_proxy_url = proxy_url if requested_country == "BR" and force_country \
        else provider_proxy_url
    combo_order = [(requested_country, requested_country)] if force_country \
        else opll_combo_attempt_order(requested_country)
    success_hint = "PayPal 页面/登录/BA approve 链" if loose_paypal_result \
        else "PayPal 登录/BA approve 页面"
    for checkout_country, pm_country in combo_order:
        try:
            _emit_payment_stage(progress_callback, "checkout", "创建 ChatGPT checkout", 1)
            attempt_locale = base_locale if checkout_country == requested_country \
                else opll_payment_locale_for_country(checkout_country)
            checkout = opll_create_checkout(access_token, checkout_country,
                                             currency_for_country(checkout_country), proxy_url)
            _emit_payment_stage(progress_callback, "stripe_init", "初始化 Stripe 支付页", 2)
            stripe = opll_build_stripe_session(provider_proxy_url)
            init_payload = opll_stripe_init(checkout["cs_id"], checkout["billing_country"],
                                             checkout["currency"], provider_proxy_url,
                                             payment_locale=attempt_locale,
                                             stripe=stripe, checkout=checkout)
            stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
            if not stripe_hosted_url:
                raise RuntimeError(f"stripe init response missing stripe_hosted_url, "
                                   f"keys={sorted(init_payload.keys())}")
            hosted_long_url = opll_to_openai_pay_url(stripe_hosted_url)
            stripe_pk = opll_stripe_key_for_checkout(checkout)
            ctx = opll_stripe_context(init_payload)
            if not ctx.get("currency"):
                ctx["currency"] = str(checkout.get("currency") or "").lower()
            stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
            if not opll_payment_method_available(init_payload, "paypal"):
                raise RuntimeError(
                    "Stripe checkout did not expose PayPal; "
                    f"{opll_payment_method_diagnostics(init_payload)}"
                )
            _emit_payment_stage(progress_callback, "paypal_method", "创建 PayPal payment method", 3)
            pm_id = opll_stripe_create_paypal_method(stripe, checkout["cs_id"], ctx,
                                                      opll_billing_for_country(pm_country), stripe_pk)
            _emit_payment_stage(progress_callback, "stripe_confirm", "执行 Stripe confirm", 4)
            confirm_payload = opll_stripe_confirm(stripe, checkout["cs_id"], pm_id, stripe_pk,
                                                   init_payload, ctx, checkout, stripe_hosted_url)
            _emit_payment_stage(progress_callback, "chatgpt_approve", "ChatGPT approve / 等待授权", 5)
            stripe_redirect_url = opll_redirect_url_after_confirm(
                access_token, stripe, confirm_payload, checkout["cs_id"], stripe_pk,
                ctx, checkout, approval_proxy_url, payment_locale=attempt_locale,
                chatgpt_cookie=chatgpt_cookie)
            if accept_pm_redirect_result and opll_is_pm_redirect_url(stripe_redirect_url):
                _emit_payment_stage(progress_callback, "done", "已提取 pm-redirects 真链", 7)
                return {
                    **checkout,
                    "payment_method_country": pm_country,
                    "payment_method_id": pm_id,
                    "stripe_hosted_url": stripe_hosted_url,
                    "stripe_redirect_url": stripe_redirect_url,
                    "provider_redirect_url": stripe_redirect_url,
                    "fallback": (checkout_country, pm_country) != (requested_country, requested_country),
                    "provider_error": "; ".join(failures),
                    "long_url": stripe_redirect_url,
                    "stripe_amount": stripe_amount,
                    "stripe_amount_source": stripe_amount_source,
                    "requested_country": requested_country,
                    "requested_currency": requested_currency,
                    "paypal_result_mode": "pm_or_paypal",
                    "payment_locale": attempt_locale,
                }
            _emit_payment_stage(progress_callback, "paypal_redirect", "跟随 PayPal redirect", 6)
            provider_url = stripe_redirect_url if opll_is_paypal_success_url(
                stripe_redirect_url, loose=loose_paypal_result) else \
                opll_resolve_external_redirect(stripe, stripe_redirect_url,
                                               loose_paypal=loose_paypal_result)
            if accept_pm_redirect_result and opll_is_true_no_card_us_url(provider_url):
                _emit_payment_stage(progress_callback, "done", "已提取 pm-redirects / PayPal 真链", 7)
                return {
                    **checkout,
                    "payment_method_country": pm_country,
                    "payment_method_id": pm_id,
                    "stripe_hosted_url": stripe_hosted_url,
                    "stripe_redirect_url": stripe_redirect_url,
                    "provider_redirect_url": provider_url,
                    "fallback": (checkout_country, pm_country) != (requested_country, requested_country),
                    "provider_error": "; ".join(failures),
                    "long_url": provider_url,
                    "stripe_amount": stripe_amount,
                    "stripe_amount_source": stripe_amount_source,
                    "requested_country": requested_country,
                    "requested_currency": requested_currency,
                    "paypal_result_mode": "pm_or_paypal",
                    "payment_locale": attempt_locale,
                }
            if not opll_is_paypal_success_url(provider_url, loose=loose_paypal_result):
                provider_url = opll_extract_paypal_candidate_url(confirm_payload, loose=loose_paypal_result) or \
                    opll_extract_paypal_candidate_url(init_payload, loose=loose_paypal_result) or provider_url
            if not opll_is_paypal_success_url(provider_url, loose=loose_paypal_result):
                resource_hint = "仅发现 Stripe 资源 URL，未发现 PayPal 链；" \
                    if opll_is_ignored_resource_url(provider_url) else ""
                raise RuntimeError(
                    f"{resource_hint}跳过假链：未进入 {success_hint}；成功标准必须为 "
                    f"{'任意真实 paypal.com 页面链接' if loose_paypal_result else 'paypal.com 登录入口或 https://www.paypal.com/agreements/approve?ba_token=...'}；"
                    f"当前结果: {provider_url or stripe_redirect_url}"
                )
            if paypal_page_country:
                provider_url = opll_paypal_url_for_country(provider_url, paypal_page_country)
            done_label = "已提取 PayPal 页面链接" if loose_paypal_result else "已进入 PayPal 登录/BA approve 页面"
            _emit_payment_stage(progress_callback, "done", done_label, 7)
            return {
                **checkout,
                "payment_method_country": pm_country,
                "payment_method_id": pm_id,
                "stripe_hosted_url": stripe_hosted_url,
                "stripe_redirect_url": stripe_redirect_url,
                "provider_redirect_url": provider_url,
                "fallback": (checkout_country, pm_country) != (requested_country, requested_country),
                "provider_error": "; ".join(failures),
                "long_url": provider_url or hosted_long_url,
                "stripe_amount": stripe_amount,
                "stripe_amount_source": stripe_amount_source,
                "requested_country": requested_country,
                "requested_currency": requested_currency,
                "paypal_result_mode": "paypal_link" if loose_paypal_result else "approval",
                "payment_locale": attempt_locale,
            }
        except Exception as exc:
            failures.append(f"{checkout_country}+{pm_country}: {opll_short_error(str(exc))}")
    raise RuntimeError(f"所有组合均未提取到 {success_hint}；{'; '.join(failures)}")




def opll_email_from_access_token_text(access_token: str, default_email: str = "uykozdwdzj@outlook.com") -> str:
    """Best-effort billing email extraction from pasted Session JSON / AT / JWT."""
    raw = str(access_token or "")
    default_email = str(default_email or "buyer@example.com").strip() or "buyer@example.com"

    def find_email(value) -> str:
        if isinstance(value, dict):
            for key in ("email", "account_email", "billing_email", "login_email", "preferred_username"):
                candidate = str(value.get(key) or "").strip()
                if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", candidate):
                    return candidate
            for item in value.values():
                found = find_email(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = find_email(item)
                if found:
                    return found
        elif isinstance(value, str):
            match = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", value)
            if match:
                return match.group(0)
        return ""

    found = find_email(raw)
    if found:
        return found
    try:
        found = find_email(json.loads(raw))
        if found:
            return found
    except Exception:
        pass
    token = parse_session_json(raw) or raw.strip()
    try:
        parts = token.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            payload += "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload.encode("ascii", "ignore"))
            found = find_email(json.loads(decoded.decode("utf-8", "replace")))
            if found:
                return found
    except Exception:
        pass
    return default_email


def opll_paypal_zero_return_url(stripe_hosted_url: str, cs_id: str) -> str:
    url = str(stripe_hosted_url or "").strip() or f"https://checkout.stripe.com/c/pay/{cs_id}"
    try:
        parsed = urlsplit(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["redirect_pm_type"] = "paypal"
        query["ui_mode"] = "custom"
        return urlunsplit((parsed.scheme or "https", parsed.netloc or "checkout.stripe.com", parsed.path, urlencode(query), parsed.fragment))
    except Exception:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}redirect_pm_type=paypal&ui_mode=custom"


def opll_stripe_confirm_paypal_inline(stripe: requests.Session, cs_id: str, stripe_pk: str,
                                      init_payload: dict, ctx: dict, checkout: dict,
                                      stripe_hosted_url: str, billing: dict) -> dict:
    """Confirm PayPal directly with payment_method_data[type]=paypal."""
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    return_url = opll_paypal_zero_return_url(stripe_hosted_url, cs_id)
    body = {
        "guid": uuid.uuid4().hex,
        "muid": uuid.uuid4().hex,
        "sid": uuid.uuid4().hex,
        "payment_method_data[type]": "paypal",
        "payment_method_data[billing_details][name]": billing.get("name") or "Lucy Carter",
        "payment_method_data[billing_details][email]": billing.get("email") or "buyer@example.com",
        "payment_method_data[billing_details][phone]": billing.get("phone") or "",
        "payment_method_data[billing_details][address][country]": billing.get("country") or "US",
        "payment_method_data[billing_details][address][line1]": billing.get("line1") or "1209 N Orange St",
        "payment_method_data[billing_details][address][city]": billing.get("city") or "Wilmington",
        "payment_method_data[billing_details][address][state]": billing.get("state") or "DE",
        "payment_method_data[billing_details][address][postal_code]": billing.get("postal_code") or "19801",
        "payment_method_data[payment_user_agent]": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "version": runtime_version,
        "expected_amount": "0",
        "expected_payment_method_type": "paypal",
        "return_url": return_url,
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[locale]": str(ctx.get("locale") or "en"),
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "consent[terms_of_service]": "accepted",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    response = stripe.post(
        f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
        data=body,
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"stripe PayPal inline confirm failed: HTTP {response.status_code} {response.text[:500]}")
    return response.json() or {}


def generate_opll_711_ba_pm_link(access_token: str, us_proxy_url: str = "",
                                  promo_proxy_url: str = "", progress_callback=None,
                                  chatgpt_cookie: str = "", billing_email: str = "") -> dict:
    """7.11 BA/PM flow: US keeps PayPal, promo exit updates same checkout to amount=0."""
    us_proxy_url = str(us_proxy_url or "").strip()
    promo_proxy_url = opll_normalize_vn_country_proxy(str(promo_proxy_url or "").strip())
    if not us_proxy_url:
        raise RuntimeError("7.11 BA/PM requires US proxy pool")
    if not promo_proxy_url:
        raise RuntimeError("7.11 BA/PM requires promo proxy pool (VN/TR/etc.)")
    total = 9
    _emit_payment_stage(progress_callback, "checkout", "7.11 BA/PM: US create checkout", 1, total)
    checkout = opll_create_checkout(
        access_token, "US", "USD", us_proxy_url,
        checkout_ui_mode="custom", require_stripe_session=True,
        preferred_processor_entity="openai_llc", promo_campaign_id="plus-1-month-free",
    )
    if not checkout.get("processor_entity"):
        checkout["processor_entity"] = "openai_llc"
    stripe_pk = opll_stripe_key_for_checkout(checkout)

    _emit_payment_stage(progress_callback, "promo_update", "7.11 BA/PM: promo checkout/update to zero", 2, total)
    promotion_payload = opll_chatgpt_checkout_update_promotion(
        access_token, checkout, promo_proxy_url, chatgpt_cookie=chatgpt_cookie)

    _emit_payment_stage(progress_callback, "stripe_init", "7.11 BA/PM: US Stripe init check amount/paypal", 3, total)
    stripe = opll_build_stripe_session(us_proxy_url)
    init_payload = opll_stripe_init(
        checkout["cs_id"], checkout["billing_country"], checkout["currency"], us_proxy_url,
        payment_locale="en-US", stripe=stripe, checkout=checkout,
        browser_timezone="America/New_York", saved_payment_method_mode="never",
    )
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(f"stripe init response missing stripe_hosted_url, keys={sorted(init_payload.keys())}")
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    if str(stripe_amount).strip() not in {"0", "0.0", "0.00"}:
        raise RuntimeError(f"7.11 BA/PM amount is not 0 after promo update: amount={stripe_amount}, source={stripe_amount_source}")
    payment_methods = opll_collect_payment_method_types(init_payload)
    if "paypal" not in payment_methods:
        raise RuntimeError("7.11 BA/PM Stripe checkout did not expose PayPal; "
                           f"{opll_payment_method_diagnostics(init_payload)}")
    ctx = opll_stripe_context(init_payload, "en-US", {
        "browser_timezone": "America/New_York",
        "saved_payment_method_mode": "never",
    })
    ctx["currency"] = "usd"
    ctx["checkout_amount"] = "0"

    stripe_tax_label = (
        "iDEAL 2.0：Stripe NL tax/address update"
        if supplied_billing
        else "7.11 BA/PM: US tax/address update"
    )
    _emit_payment_stage(
        progress_callback, "stripe_tax", stripe_tax_label,
        6 if supplied_billing else 4, total,
    )
    billing = opll_billing_for_country("US")
    billing.update({
        "email": str(billing_email or "").strip() or opll_email_from_access_token_text(access_token),
        "name": "Lucy Carter",
        "line1": "1209 N Orange St",
        "city": "Wilmington",
        "state": "DE",
        "postal_code": "19801",
        "country": "US",
    })
    tax_payload = opll_stripe_update_tax_region(
        stripe, checkout["cs_id"], stripe_pk, ctx, billing,
        payment_locale="en-US", browser_timezone="America/New_York",
        saved_payment_method_mode="never",
    )
    tax_amount, tax_amount_source = opll_stripe_amount_info(tax_payload)
    if str(tax_amount).strip() not in {"", "0", "0.0", "0.00"}:
        raise RuntimeError(f"7.11 BA/PM amount changed after tax update: amount={tax_amount}, source={tax_amount_source}")
    tax_methods = opll_collect_payment_method_types(tax_payload)
    if tax_methods and "paypal" not in tax_methods:
        raise RuntimeError("7.11 BA/PM tax update lost PayPal; "
                           f"{opll_payment_method_diagnostics(tax_payload)}")

    _emit_payment_stage(progress_callback, "stripe_confirm", "7.11 BA/PM: Stripe confirm PayPal", 5, total)
    confirm_payload = opll_stripe_confirm_paypal_inline(
        stripe, checkout["cs_id"], stripe_pk, init_payload, ctx, checkout, stripe_hosted_url, billing)
    confirm_redirect = opll_extract_redirect_to_url(confirm_payload)
    submission = opll_find_submission_attempt(confirm_payload)
    if submission.get("state") == "failed":
        raise RuntimeError(f"7.11 BA/PM stripe confirm failed: {opll_stripe_payload_diagnostics(confirm_payload, ctx)}")

    _emit_payment_stage(progress_callback, "chatgpt_approve", "7.11 BA/PM: OpenAI approve / Stripe poll", 6, total)
    try:
        stripe_redirect_url = opll_redirect_url_after_confirm(
            access_token, stripe, confirm_payload, checkout["cs_id"], stripe_pk, ctx, checkout,
            us_proxy_url, payment_locale="en-US", chatgpt_cookie=chatgpt_cookie)
    except Exception as exc:
        raise RuntimeError(f"7.11 BA/PM approve/follow failed: {opll_short_error(str(exc))}") from exc

    _emit_payment_stage(progress_callback, "follow", "7.11 BA/PM: follow pm-redirects for BA", 7, total)
    provider_url = stripe_redirect_url
    paypal_approval_url = ""
    if opll_is_paypal_ba_approve_url(provider_url):
        paypal_approval_url = provider_url
    elif opll_is_pm_redirect_url(provider_url):
        try:
            followed = opll_resolve_external_redirect(stripe, provider_url)
        except Exception:
            followed = provider_url
        if opll_is_paypal_ba_approve_url(followed):
            paypal_approval_url = followed
            provider_url = followed
    elif opll_is_paypal_success_url(provider_url):
        paypal_approval_url = provider_url if opll_is_paypal_ba_approve_url(provider_url) else ""
    if not paypal_approval_url:
        paypal_approval_url = opll_extract_paypal_candidate_url(confirm_payload) or             opll_extract_paypal_candidate_url(init_payload)
        if paypal_approval_url:
            provider_url = paypal_approval_url

    accepted_pm = opll_is_pm_redirect_url(stripe_redirect_url)
    accepted_ba = opll_is_paypal_ba_approve_url(paypal_approval_url) or opll_is_paypal_approval_entry_url(provider_url)
    if not accepted_ba and not accepted_pm:
        raise RuntimeError(f"7.11 BA/PM did not produce BA or PM link; redirect={stripe_redirect_url or confirm_redirect}")
    long_url = paypal_approval_url if opll_is_paypal_approval_entry_url(paypal_approval_url) else stripe_redirect_url
    _emit_payment_stage(progress_callback, "done", "7.11 BA/PM: link extracted", 9, total)
    return {
        **checkout,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": provider_url,
        "paypal_approval_url": paypal_approval_url,
        "long_url": long_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, "USD"),
        "payment_methods": payment_methods,
        "promotion_update": promotion_payload,
        "billing_email": billing.get("email") or "",
        "checkout_amount": "0 USD",
        "paypal_result_mode": "ba_or_pm",
        "local_payment": "paypal_ba_pm_711",
        "local_payment_detected": True,
        "payment_locale": "en-US",
    }


def _opll_amount_is_zero(value) -> bool:
    text = str(value if value is not None else "").strip()
    if text in {"", "0", "0.0", "0.00"}:
        return True
    try:
        return float(text) == 0.0
    except Exception:
        return False


def opll_checkout_session_id(checkout: dict | None) -> str:
    return str(
        (checkout or {}).get("checkout_session_id")
        or (checkout or {}).get("checkout_id")
        or (checkout or {}).get("cs_id")
        or ""
    ).strip()


def opll_checkout_session_type_from_id(checkout_id: str) -> str:
    checkout_id = str(checkout_id or "").strip()
    if checkout_id.startswith("oaics_"):
        return "oaics"
    if checkout_id.startswith("cs_live_"):
        return "cs_live"
    if checkout_id.startswith("cs_test_"):
        return "cs_test"
    if checkout_id.startswith("cs_"):
        return "cs"
    return "unknown"


def opll_probe_paypal_global_oaics_eligibility(access_token: str,
                                               proxy_url: str = "",
                                               chatgpt_cookie: str = "",
                                               billing_email: str = "",
                                               browser_profile: str = "",
                                               use_cache: bool = True) -> dict:
    """Probe OAICS availability by creating a BR custom checkout."""
    token = parse_session_json(access_token) or str(access_token or "").strip()
    browser_profile = opll_normalize_browser_profile(
        browser_profile or PAYPAL_GLOBAL_OAICS_BROWSER_PROFILE
    )
    token_hash = hashlib.sha256(token.encode("utf-8", errors="ignore")).hexdigest()[:24]
    cache_key = f"{token_hash}:{browser_profile}:{PAYPAL_GLOBAL_OAICS_PROBE_COUNTRY}"
    if use_cache:
        with PAYPAL_GLOBAL_OAICS_ELIGIBILITY_LOCK:
            cached = PAYPAL_GLOBAL_OAICS_ELIGIBILITY_CACHE.get(cache_key)
            if cached:
                return dict(cached, cached=True)

    probe_country = PAYPAL_GLOBAL_OAICS_PROBE_COUNTRY
    probe_currency, _local_currency, _fallback = checkout_currency_for_country(probe_country)
    billing = opll_generate_paypal_global_profile(probe_country, billing_email)
    try:
        checkout = opll_create_checkout(
            token,
            probe_country,
            probe_currency,
            proxy_url,
            checkout_ui_mode="custom",
            require_stripe_session=False,
            preferred_processor_entity=opll_processor_entity_for_country(probe_country),
            promo_campaign_id=None,
            hosted_payload_contract=False,
            allow_openai_checkout_session=True,
            billing_profile=billing,
            chatgpt_cookie=chatgpt_cookie,
            browser_profile=browser_profile,
        )
        checkout_id = opll_checkout_session_id(checkout)
        eligible = checkout_id.startswith("oaics_")
        session_kind = (
            "openai_custom_checkout"
            if eligible else
            str(checkout.get("session_kind") or ("stripe_checkout" if checkout_id.startswith("cs_") else "unknown_checkout"))
        )
        result = {
            "ok": True,
            "eligible": eligible,
            "oaics_eligible": eligible,
            "probe_country": probe_country,
            "probe_currency": probe_currency,
            "checkout_session_id": checkout_id,
            "checkout_id": checkout_id,
            "session_kind": session_kind,
            "checkout_session_type": opll_checkout_session_type_from_id(checkout_id),
            "checkout_branch": "oaics_custom" if eligible else "stripe_hosted",
            "browser_profile": browser_profile,
            "cached": False,
        }
        if use_cache and checkout_id:
            with PAYPAL_GLOBAL_OAICS_ELIGIBILITY_LOCK:
                PAYPAL_GLOBAL_OAICS_ELIGIBILITY_CACHE[cache_key] = dict(result)
        return result
    except Exception as exc:
        return {
            "ok": False,
            "eligible": False,
            "oaics_eligible": False,
            "probe_country": probe_country,
            "probe_currency": probe_currency,
            "checkout_session_id": "",
            "checkout_id": "",
            "session_kind": "probe_failed",
            "checkout_session_type": "probe_failed",
            "checkout_branch": "stripe_hosted",
            "browser_profile": browser_profile,
            "error": opll_short_error(str(exc), 500),
            "cached": False,
        }


def generate_opll_paypal_global_rotation_link(access_token: str, country_proxy_url: str = "",
                                               promo_proxy_url: str = "", progress_callback=None,
                                               chatgpt_cookie: str = "", billing_email: str = "",
                                               billing_country: str = "JP",
                                               payment_locale: str = "en",
                                               apply_promotion: bool = True,
                                               paypal_proxy_country: str = "",
                                               promo_proxy_country: str = "",
                                               checkout_branch: str = "auto") -> dict:
    """PAYPAL global flow, optionally skipping the promotion stage."""
    mode_label = "PAYPAL全球轮转" if apply_promotion else "PayPal全球无优惠提链"
    country_proxy_url = str(country_proxy_url or "").strip()
    promo_proxy_url = opll_normalize_vn_country_proxy(str(promo_proxy_url or "").strip()) if apply_promotion else ""
    if not country_proxy_url:
        if apply_promotion:
            raise RuntimeError("PAYPAL全球轮转 requires main PayPal proxy pool (stage 1 and stage 3 shared)")
        raise RuntimeError("PayPal全球无优惠提链 requires PayPal proxy pool")
    if apply_promotion and not promo_proxy_url:
        raise RuntimeError("PAYPAL全球轮转 requires promo proxy pool (stage 2 checkout/update to zero)")
    billing_country = normalize_paypal_global_billing_country(billing_country)
    payment_locale = normalize_paypal_global_payment_locale(payment_locale)
    paypal_proxy_country = (
        opll_normalize_country_hint(paypal_proxy_country)
        or opll_proxy_country_hint(country_proxy_url)
    )
    promo_proxy_country = (
        opll_normalize_country_hint(promo_proxy_country)
        or opll_proxy_country_hint(promo_proxy_url)
    )
    currency, local_currency, currency_fallback = checkout_currency_for_country(billing_country)
    browser_timezone = opll_browser_timezone_for_country(billing_country)
    billing = opll_generate_paypal_global_profile(billing_country, billing_email)
    requested_checkout_branch = str(checkout_branch or "auto").strip().lower() or "auto"
    auto_detect_oaics = apply_promotion and requested_checkout_branch in {
        "auto", "detect", "probe", "oaics_probe", "auto_probe", "global"
    }
    total = 10 if auto_detect_oaics else (9 if apply_promotion else 7)
    browser_profile = opll_normalize_browser_profile(
        PAYPAL_GLOBAL_OAICS_BROWSER_PROFILE if apply_promotion else ""
    )
    oaics_probe = None
    effective_checkout_branch = requested_checkout_branch
    oaics_preferred_by_route = paypal_proxy_country in PAYPAL_GLOBAL_OAICS_MAIN_PROXY_COUNTRIES
    if auto_detect_oaics:
        _emit_payment_stage(
            progress_callback,
            "paypal_global_oaics_probe",
            f"PAYPAL全球轮转: BR checkout probe OAICS eligibility",
            1,
            total,
        )
        oaics_probe = opll_probe_paypal_global_oaics_eligibility(
            access_token,
            country_proxy_url,
            chatgpt_cookie=chatgpt_cookie,
            billing_email=billing_email,
            browser_profile=browser_profile,
        )
        if bool(oaics_probe.get("oaics_eligible")):
            effective_checkout_branch = "oaics"
        elif oaics_preferred_by_route and not bool(oaics_probe.get("ok")):
            # BR/TH main routes are intended for the OAICS custom branch.  When
            # the lightweight BR probe fails because of a transient proxy/API
            # error, still try the custom contract instead of silently falling
            # into hosted cs_live; the hosted path is where Stripe geocodes the
            # BR/TH exit and often hides PayPal before confirm.
            effective_checkout_branch = "oaics"
        else:
            effective_checkout_branch = "hosted"
    checkout_contract = opll_paypal_global_checkout_contract(
        billing_country,
        checkout_branch=effective_checkout_branch,
        paypal_proxy_country=paypal_proxy_country,
    ) if apply_promotion else {
        "checkout_ui_mode": "hosted",
        "require_stripe_session": True,
        "promo_campaign_id": None,
        "hosted_payload_contract": True,
        "checkout_branch": "stripe_hosted",
        "allow_openai_checkout_session": False,
        "send_billing_profile": False,
    }

    _emit_payment_stage(progress_callback, "paypal_global_checkout",
                        (f"PAYPAL全球轮转: {billing_country}/{currency} selected billing country + create checkout (stage 1/3 proxy)"
                         if apply_promotion else
                         f"PayPal全球无优惠: {billing_country}/{currency} selected billing country + create checkout"),
                        2 if auto_detect_oaics else 1, total)
    create_kwargs = {
        "checkout_ui_mode": checkout_contract["checkout_ui_mode"],
        "require_stripe_session": checkout_contract["require_stripe_session"],
        "preferred_processor_entity": opll_processor_entity_for_country(billing_country),
        "promo_campaign_id": checkout_contract["promo_campaign_id"],
        "hosted_payload_contract": checkout_contract["hosted_payload_contract"],
        "allow_openai_checkout_session": bool(checkout_contract.get("allow_openai_checkout_session")),
        "chatgpt_cookie": chatgpt_cookie,
        "browser_profile": browser_profile,
    }
    if checkout_contract.get("send_billing_profile"):
        create_kwargs["billing_profile"] = billing
    checkout = opll_create_checkout(
        access_token, billing_country, currency, country_proxy_url,
        **create_kwargs,
    )
    checkout["oaics_probe"] = oaics_probe or {}
    checkout["checkout_branch_requested"] = requested_checkout_branch
    checkout["checkout_branch_effective"] = str(checkout_contract.get("checkout_branch") or effective_checkout_branch)
    if browser_profile:
        checkout["browser_profile"] = browser_profile
    if not checkout.get("processor_entity"):
        checkout["processor_entity"] = opll_processor_entity_for_country(billing_country)
    initial_checkout_id = opll_checkout_session_id(checkout)
    pending_oaics_checkout = initial_checkout_id.startswith("oaics_")
    expected_oaics_checkout = (
        apply_promotion
        and str(checkout_contract.get("checkout_branch") or "") == "oaics_custom"
    )
    oaics_seed_error = ""
    oaics_seed_checkout_id = ""
    if expected_oaics_checkout and not pending_oaics_checkout and billing_country != PAYPAL_GLOBAL_OAICS_PROBE_COUNTRY:
        _emit_payment_stage(
            progress_callback,
            "paypal_global_oaics_seed_retry",
            f"PAYPAL全球轮转: {billing_country}/{currency} custom returned cs_; retry BR OAICS seed then rebind billing",
            2 if auto_detect_oaics else 1,
            total,
        )
        try:
            seed_country = PAYPAL_GLOBAL_OAICS_PROBE_COUNTRY
            seed_currency, _seed_local_currency, _seed_fallback = checkout_currency_for_country(seed_country)
            seed_billing = opll_generate_paypal_global_profile(seed_country, billing_email)
            seed_checkout = opll_create_checkout(
                access_token,
                seed_country,
                seed_currency,
                country_proxy_url,
                checkout_ui_mode="custom",
                require_stripe_session=False,
                preferred_processor_entity=opll_processor_entity_for_country(billing_country),
                promo_campaign_id=checkout_contract["promo_campaign_id"],
                hosted_payload_contract=False,
                allow_openai_checkout_session=True,
                billing_profile=seed_billing,
                chatgpt_cookie=chatgpt_cookie,
                browser_profile=browser_profile,
            )
            oaics_seed_checkout_id = opll_checkout_session_id(seed_checkout)
            if oaics_seed_checkout_id.startswith("oaics_"):
                original_checkout_id = initial_checkout_id
                checkout = seed_checkout
                checkout["paypal_global_original_checkout_id"] = original_checkout_id
                checkout["paypal_global_oaics_seed_country"] = seed_country
                checkout["paypal_global_oaics_seed_currency"] = seed_currency
                checkout["paypal_global_oaics_seed_checkout_id"] = oaics_seed_checkout_id
                checkout["billing_country"] = billing_country
                checkout["currency"] = currency
                checkout["_checkout_billing_profile"] = dict(billing)
                checkout["oaics_probe"] = oaics_probe or {}
                checkout["checkout_branch_requested"] = requested_checkout_branch
                checkout["checkout_branch_effective"] = str(checkout_contract.get("checkout_branch") or effective_checkout_branch)
                checkout["browser_profile"] = browser_profile
                if not checkout.get("processor_entity"):
                    checkout["processor_entity"] = opll_processor_entity_for_country(billing_country)
                initial_checkout_id = oaics_seed_checkout_id
                pending_oaics_checkout = True
        except Exception as exc:
            oaics_seed_error = opll_short_error(str(exc), 360)
    if expected_oaics_checkout and not pending_oaics_checkout:
        session_type = opll_checkout_session_type_from_id(initial_checkout_id)
        _emit_payment_stage(
            progress_callback,
            "paypal_global_oaics_strict_mismatch",
            f"PAYPAL全球轮转: OAICS strict expected oaics_ but got {session_type}; retry next account/proxy",
            2 if auto_detect_oaics else 1,
            total,
        )
        raise RuntimeError(
            f"PAYPAL全球轮转 OAICS strict branch expected oaics_ checkout but got "
            f"{session_type}/{initial_checkout_id}; billing={billing_country}/{currency}; "
            f"main_proxy_country={paypal_proxy_country or 'unknown'}; "
            f"seed_checkout={oaics_seed_checkout_id or '-'}; seed_error={oaics_seed_error or '-'}; "
            f"probe_eligible={bool((oaics_probe or {}).get('oaics_eligible'))}; "
            f"skip hosted cs_live fallback for BR/TH auto because Stripe init geo can hide PayPal"
        )
    if not initial_checkout_id.startswith("cs_") and not pending_oaics_checkout:
        raise RuntimeError(f"{mode_label} checkout returned unsupported session id: {initial_checkout_id}")
    if pending_oaics_checkout:
        if not apply_promotion:
            raise RuntimeError("PayPal全球无优惠提链 OAICS checkout is not enabled for no-discount mode")
        return generate_opll_paypal_global_oaics_branch(
            access_token,
            checkout,
            country_proxy_url,
            promo_proxy_url,
            progress_callback=progress_callback,
            chatgpt_cookie=chatgpt_cookie,
            billing=billing,
            billing_country=billing_country,
            currency=currency,
            local_currency=local_currency,
            currency_fallback=currency_fallback,
            payment_locale=payment_locale,
            browser_timezone=browser_timezone,
            paypal_proxy_country=paypal_proxy_country,
            promo_proxy_country=promo_proxy_country,
            total=total,
        )

    promotion_payload = None
    if apply_promotion:
        _emit_payment_stage(progress_callback, "paypal_global_promo",
                            f"PAYPAL全球轮转: promo checkout/update to zero (stage 2 proxy)", 2, total)
        promo_variants = [
            ("full-profile", {
                "include_full_profile": True,
                "billing_profile": billing,
                "checkout_ui_mode": checkout_contract["checkout_ui_mode"],
            }),
            ("standard", {
                "include_full_profile": False,
                "checkout_ui_mode": checkout_contract["checkout_ui_mode"],
            }),
            ("page-route", {
                "include_full_profile": False,
                "checkout_ui_mode": checkout_contract["checkout_ui_mode"],
                "checkout_page_route": True,
            }),
            ("custom-ui", {
                "include_full_profile": False,
                "checkout_ui_mode": "custom",
            }),
        ]
        promotion_payload = None
        promo_errors: list[str] = []
        for variant_index, (promo_variant, promo_kwargs) in enumerate(promo_variants, start=1):
            try:
                # Keep fallback variants internal; the main progress bar should stay on
                # the single stage-2 promo update step. Variant names are preserved in
                # the final error detail when every variant returns HTTP 403.
                promotion_payload = opll_chatgpt_checkout_update_promotion(
                    access_token, checkout, promo_proxy_url, chatgpt_cookie=chatgpt_cookie,
                    browser_profile=browser_profile,
                    **promo_kwargs,
                )
                checkout["paypal_global_promo_variant"] = promo_variant
                break
            except Exception as promo_exc:
                promo_text = str(promo_exc)
                promo_short = opll_short_error(promo_text, 110)
                if "checkout/update promotion failed: HTTP 403" in promo_text:
                    promo_short = "HTTP 403 HTML" if "HTML response hidden" in promo_short else "HTTP 403"
                promo_errors.append(f"{promo_variant}={promo_short}")
                if "checkout/update promotion failed: HTTP 403" not in promo_text:
                    raise
        if promotion_payload is None:
            raise RuntimeError(
                "PAYPAL global promo update HTTP 403 on all fallback variants: "
                + "; ".join(promo_errors[-4:])
            )
        if pending_oaics_checkout:
            stripe_checkout_id = opll_extract_stripe_checkout_id(promotion_payload)
            if not stripe_checkout_id:
                raise RuntimeError(
                    f"PAYPAL全球轮转 oaics_ checkout update did not return Stripe cs_id; "
                    f"oaics_id={initial_checkout_id}; update={str(promotion_payload)[:500]}"
                )
            checkout["openai_checkout_id"] = initial_checkout_id
            checkout["cs_id"] = stripe_checkout_id
            checkout["checkout_id"] = stripe_checkout_id
            checkout["checkout_ui_mode"] = "hosted"
            promoted_pk = opll_extract_stripe_publishable_key(promotion_payload)
            if promoted_pk:
                checkout["stripe_publishable_key"] = promoted_pk
            _emit_payment_stage(
                progress_callback,
                "paypal_global_oaics_hydrated",
                f"PAYPAL全球轮转: {billing_country} oaics_ checkout updated to Stripe cs_id",
                2,
                total,
            )
    elif pending_oaics_checkout:
        raise RuntimeError(f"{mode_label} hosted checkout did not return Stripe cs_id: {initial_checkout_id}")
    stripe_pk = opll_stripe_key_for_checkout(checkout)
    checkout_tax_payload = None

    if apply_promotion and checkout_contract.get("send_billing_profile"):
        _emit_payment_stage(
            progress_callback,
            "paypal_global_checkout_tax_config",
            f"PAYPAL全球轮转 hosted: push {billing_country}/{currency} full billing before Stripe init",
            3,
            total,
        )
        checkout_tax_payload = opll_chatgpt_checkout_update_taxes(
            access_token,
            checkout,
            country_proxy_url,
            billing=billing,
            currency=currency,
            chatgpt_cookie=chatgpt_cookie,
            browser_profile=browser_profile,
        )
        checkout_tax_amount, checkout_tax_amount_source = opll_stripe_amount_info(checkout_tax_payload)
        if checkout_tax_amount not in (None, "") and not _opll_amount_is_zero(checkout_tax_amount):
            raise RuntimeError(
                f"PAYPAL全球轮转 hosted checkout/taxes changed amount: "
                f"amount={checkout_tax_amount}, source={checkout_tax_amount_source}"
            )
        tax_pk = opll_extract_stripe_publishable_key(checkout_tax_payload)
        if tax_pk:
            checkout["stripe_publishable_key"] = tax_pk
            stripe_pk = tax_pk

    _emit_payment_stage(progress_callback, "paypal_global_stripe_init",
                        (f"PAYPAL全球轮转: {billing_country} Stripe init / PayPal / zero check"
                         if apply_promotion else
                         f"PayPal全球无优惠提链: {billing_country} Stripe init / PayPal check"),
                        3 if apply_promotion else 2, total)
    stripe = opll_build_stripe_session(country_proxy_url, browser_profile=browser_profile)
    browser_user_agent = str(getattr(stripe, "headers", {}).get("User-Agent") or FIREFOX_USER_AGENT)
    init_payload = opll_stripe_init(
        checkout["cs_id"], checkout["billing_country"], checkout["currency"], country_proxy_url,
        payment_locale=payment_locale, stripe=stripe, checkout=checkout,
        browser_timezone=browser_timezone, saved_payment_method_mode="never",
    )
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(f"{mode_label} stripe init response missing stripe_hosted_url, keys={sorted(init_payload.keys())}")
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    if apply_promotion and not _opll_amount_is_zero(stripe_amount):
        raise RuntimeError(f"PAYPAL全球轮转 amount is not 0 after promo: amount={stripe_amount}, source={stripe_amount_source}")
    payment_methods = opll_collect_payment_method_types(init_payload)
    # Keep the proven GB path byte-for-byte compatible at the decision level:
    # GB still requires PayPal to be exposed by the first Stripe init. Other
    # billing countries continue through the full local billing/tax sync and
    # then obtain a fresh init payload before deciding that PayPal is absent.
    preserve_gb_early_method_gate = False
    if preserve_gb_early_method_gate and "paypal" not in payment_methods:
        raise RuntimeError(f"{mode_label} Stripe checkout did not expose PayPal; {opll_payment_method_diagnostics(init_payload)}")

    _emit_payment_stage(progress_callback, "paypal_global_tax",
                        f"{mode_label}: sync billing country to {billing_country}", 4 if apply_promotion else 3, total)
    billing["country"] = billing_country
    ctx = opll_stripe_context(init_payload, payment_locale, {
        "browser_timezone": browser_timezone,
        "saved_payment_method_mode": "never",
        "browser_profile": browser_profile,
        "browser_user_agent": browser_user_agent,
    })
    tax_payload = opll_stripe_update_tax_region(
        stripe, checkout["cs_id"], stripe_pk, ctx, billing,
        payment_locale=payment_locale, browser_timezone=browser_timezone,
        saved_payment_method_mode="never",
    )
    tax_amount, tax_amount_source = opll_stripe_amount_info(tax_payload)
    if apply_promotion and not _opll_amount_is_zero(tax_amount):
        raise RuntimeError(f"PAYPAL全球轮转 amount changed after tax sync: amount={tax_amount}, source={tax_amount_source}")
    tax_methods = opll_collect_payment_method_types(tax_payload)
    if preserve_gb_early_method_gate:
        if tax_methods and "paypal" not in tax_methods:
            raise RuntimeError(f"{mode_label} tax update lost PayPal; {opll_payment_method_diagnostics(tax_payload)}")
    else:
        _emit_payment_stage(
            progress_callback,
            "paypal_global_post_tax_methods",
            f"{mode_label}: {billing_country} billing/tax synced; refresh Stripe payment methods",
            4 if apply_promotion else 3,
            total,
        )
        post_tax_init_payload = opll_stripe_init(
            checkout["cs_id"], checkout["billing_country"], checkout["currency"], country_proxy_url,
            payment_locale=payment_locale, stripe=stripe, checkout=checkout,
            browser_timezone=browser_timezone, saved_payment_method_mode="never",
        )
        post_tax_hosted_url = str(post_tax_init_payload.get("stripe_hosted_url") or "").strip()
        if not post_tax_hosted_url:
            raise RuntimeError(
                f"{mode_label} post-tax Stripe init missing stripe_hosted_url, "
                f"keys={sorted(post_tax_init_payload.keys())}"
            )
        post_tax_amount, post_tax_amount_source = opll_stripe_amount_info(post_tax_init_payload)
        if apply_promotion and not _opll_amount_is_zero(post_tax_amount):
            raise RuntimeError(
                f"PAYPAL全球轮转 amount changed after post-tax init: "
                f"amount={post_tax_amount}, source={post_tax_amount_source}"
            )
        post_tax_methods = opll_collect_payment_method_types(post_tax_init_payload)
        if "paypal" not in post_tax_methods:
            # The compared extractor does not treat the Payment Element's
            # advertised card/link list as a hard gate. It explicitly creates
            # a type=paypal PaymentMethod next and lets Stripe confirm/approve
            # return the authoritative eligibility or risk decision.
            _emit_payment_stage(
                progress_callback,
                "paypal_global_explicit_pm_fallback",
                f"{mode_label}: {billing_country} init advertised "
                f"{post_tax_methods or ['card/link only']}; continue explicit PayPal PaymentMethod",
                5,
                total,
            )
        init_payload = post_tax_init_payload
        stripe_hosted_url = post_tax_hosted_url
        stripe_amount, stripe_amount_source = post_tax_amount, post_tax_amount_source
        payment_methods = post_tax_methods
        ctx = opll_stripe_context(init_payload, payment_locale, {
            "browser_timezone": browser_timezone,
            "saved_payment_method_mode": "never",
            "browser_profile": browser_profile,
            "browser_user_agent": browser_user_agent,
        })

    ctx["currency"] = str(checkout.get("currency") or currency).lower()
    if apply_promotion:
        ctx["checkout_amount"] = "0"
    elif stripe_amount not in (None, ""):
        ctx["checkout_amount"] = str(stripe_amount)

    if apply_promotion:
        return generate_opll_paypal_global_cslive_confirmation_token_branch(
            access_token,
            checkout,
            stripe,
            init_payload,
            ctx,
            billing,
            stripe_pk,
            stripe_hosted_url,
            country_proxy_url,
            progress_callback=progress_callback,
            chatgpt_cookie=chatgpt_cookie,
            billing_country=billing_country,
            currency=currency,
            local_currency=local_currency,
            currency_fallback=currency_fallback,
            payment_locale=payment_locale,
            browser_timezone=browser_timezone,
            paypal_proxy_country=paypal_proxy_country,
            promo_proxy_country=promo_proxy_country,
            promotion_payload=promotion_payload,
            checkout_tax_payload=checkout_tax_payload,
            stripe_amount=stripe_amount,
            stripe_amount_source=stripe_amount_source,
            payment_methods=payment_methods,
            total=total,
            oaics_probe=oaics_probe,
        )

    payment_method_id = ""
    if preserve_gb_early_method_gate:
        _emit_payment_stage(progress_callback, "paypal_global_confirm",
                            f"{mode_label}: {billing_country} PayPal confirm", 5 if apply_promotion else 4, total)
        confirm_payload = opll_stripe_confirm_paypal_inline(
            stripe, checkout["cs_id"], stripe_pk, init_payload, ctx, checkout, stripe_hosted_url, billing)
    else:
        _emit_payment_stage(
            progress_callback,
            "paypal_global_payment_method",
            f"{mode_label}: {billing_country} create explicit PayPal PaymentMethod",
            5 if apply_promotion else 4,
            total,
        )
        payment_method_id = opll_stripe_create_paypal_method(
            stripe, checkout["cs_id"], ctx, billing, stripe_pk,
        )
        _emit_payment_stage(
            progress_callback,
            "paypal_global_confirm",
            f"{mode_label}: {billing_country} confirm explicit PayPal PaymentMethod",
            6 if apply_promotion else 5,
            total,
        )
        confirm_payload = opll_stripe_confirm(
            stripe, checkout["cs_id"], payment_method_id, stripe_pk,
            init_payload, ctx, checkout, stripe_hosted_url,
            payment_method_type="paypal",
        )
    confirm_amount, confirm_amount_source = opll_stripe_amount_info(confirm_payload)
    if apply_promotion and confirm_amount and not _opll_amount_is_zero(confirm_amount):
        raise RuntimeError(f"PAYPAL全球轮转 amount is not 0 after confirm: amount={confirm_amount}, source={confirm_amount_source}")
    submission = opll_find_submission_attempt(confirm_payload)
    if submission.get("state") == "failed":
        raise RuntimeError(f"{mode_label} stripe confirm failed: {opll_stripe_payload_diagnostics(confirm_payload, ctx)}")

    _emit_payment_stage(progress_callback, "paypal_global_approve",
                        f"{mode_label}: OpenAI approve / Stripe PayPal polling", 6 if apply_promotion else 5, total)
    stripe_redirect_url = opll_redirect_url_after_confirm(
        access_token, stripe, confirm_payload, checkout["cs_id"], stripe_pk, ctx, checkout,
        country_proxy_url, payment_locale=payment_locale, chatgpt_cookie=chatgpt_cookie)

    _emit_payment_stage(progress_callback, "paypal_global_resolve",
                        f"{mode_label}: follow pm-redirects and extract BA token", 7 if apply_promotion else 6, total)
    provider_url = stripe_redirect_url
    paypal_approval_url = ""
    if opll_is_paypal_ba_approve_url(provider_url):
        paypal_approval_url = provider_url
    elif opll_is_pm_redirect_url(provider_url):
        try:
            followed = opll_resolve_external_redirect(stripe, provider_url)
        except Exception:
            followed = provider_url
        provider_url = followed or provider_url
        if opll_is_paypal_ba_approve_url(provider_url):
            paypal_approval_url = provider_url
    if not paypal_approval_url:
        candidate = opll_extract_paypal_candidate_url(confirm_payload) or opll_extract_paypal_candidate_url(init_payload)
        if opll_is_paypal_ba_approve_url(candidate):
            paypal_approval_url = candidate
            provider_url = candidate
    accepted_ba = opll_is_paypal_ba_approve_url(paypal_approval_url)
    pm_redirect_url = stripe_redirect_url if opll_is_pm_redirect_url(stripe_redirect_url) else ""
    if not pm_redirect_url and opll_is_pm_redirect_url(provider_url):
        pm_redirect_url = provider_url
    accepted_pm = bool(pm_redirect_url)
    if not accepted_ba and not accepted_pm:
        raise RuntimeError(
            f"{mode_label} did not extract BA or PM link; "
            f"current={provider_url or stripe_redirect_url}"
        )
    final_url = paypal_approval_url if accepted_ba else pm_redirect_url
    final_kind = "ba" if accepted_ba else "pm_redirect"

    _emit_payment_stage(
        progress_callback, "done",
        f"PAYPAL global: {final_kind} link extracted" if apply_promotion else f"{mode_label}: {final_kind} link extracted",
        total, total,
    )
    return {
        **checkout,
        "billing_country": billing_country,
        "currency": currency,
        "local_currency": local_currency,
        "currency_fallback": currency_fallback,
        "payment_method_country": billing_country,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": provider_url,
        "paypal_approval_url": paypal_approval_url,
        "pm_redirect_url": pm_redirect_url,
        "paypal_result_kind": final_kind,
        "long_url": final_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, currency),
        "payment_methods": payment_methods,
        "payment_method_id": payment_method_id,
        "paypal_advertised_by_init": "paypal" in payment_methods,
        "promotion_update": promotion_payload,
        "billing_profile": billing,
        "billing_email": billing.get("email") or "",
        "checkout_amount": f"0 {currency}" if apply_promotion else opll_format_minor_amount(stripe_amount, currency),
        "paypal_result_mode": "ba_or_pm",
        "local_payment": "paypal_global_rotation" if apply_promotion else "paypal_global_no_discount",
        "local_payment_detected": True,
        "discount_applied": bool(apply_promotion),
        "payment_locale": payment_locale,
        "browser_timezone": browser_timezone,
        "billing_country_source": "user_selection",
        "country_proxy_hint": "",
        "checkout_branch": "stripe_hosted",
        "session_kind": str(checkout.get("session_kind") or "stripe_checkout"),
        "checkout_session_id": str(checkout.get("checkout_session_id") or checkout.get("checkout_id") or checkout.get("cs_id") or ""),
        "checkout_session_type": opll_checkout_session_type_from_id(
            str(checkout.get("checkout_session_id") or checkout.get("checkout_id") or checkout.get("cs_id") or "")
        ),
        "checkout_branch_requested": requested_checkout_branch,
        "checkout_branch_effective": str(checkout.get("checkout_branch_effective") or "hosted"),
        "oaics_probe": oaics_probe or checkout.get("oaics_probe") or {},
        "oaics_eligible": bool((oaics_probe or checkout.get("oaics_probe") or {}).get("oaics_eligible")),
        "browser_profile": browser_profile,
        "customer_acceptance_ip": str(ctx.get("customer_acceptance_ip") or ""),
        "checkout_exit_ip": str(ctx.get("customer_acceptance_ip") or ""),
        "paypal_proxy_country": paypal_proxy_country,
        "paypal_main_proxy_country": paypal_proxy_country,
        "promo_proxy_country": promo_proxy_country,
    }


def generate_opll_paypal_global_cslive_confirmation_token_branch(
        access_token: str,
        checkout: dict,
        stripe: requests.Session,
        init_payload: dict,
        ctx: dict,
        billing: dict,
        stripe_pk: str,
        stripe_hosted_url: str,
        country_proxy_url: str,
        progress_callback=None,
        chatgpt_cookie: str = "",
        billing_country: str = "DE",
        currency: str = "EUR",
        local_currency: str = "EUR",
        currency_fallback: bool = False,
        payment_locale: str = "en",
        browser_timezone: str = "Europe/Berlin",
        paypal_proxy_country: str = "",
        promo_proxy_country: str = "",
        promotion_payload: dict | None = None,
        checkout_tax_payload: dict | None = None,
        stripe_amount=None,
        stripe_amount_source: str = "",
        payment_methods: list[str] | None = None,
        total: int = 9,
        oaics_probe: dict | None = None) -> dict:
    """Hosted cs_live fallback.

    Important split:
    - oaics_* is the only branch that may call OpenAI /backend-api/payments/checkout/confirm.
    - cs_live_/cs_test_ is Stripe Hosted Checkout and must stay on Stripe payment_pages confirm.
    """
    mode_label = "PAYPAL全球轮转"
    checkout_id = opll_checkout_session_id(checkout)
    if not checkout_id.startswith("cs_"):
        raise RuntimeError(f"cs_live branch expected cs_ checkout id, got {checkout_id}")
    session_type = opll_checkout_session_type_from_id(checkout_id)
    browser_profile = opll_normalize_browser_profile(
        (checkout or {}).get("browser_profile") or PAYPAL_GLOBAL_OAICS_BROWSER_PROFILE
    )
    checkout["browser_profile"] = browser_profile
    ctx = dict(ctx or {})
    ctx["currency"] = str(checkout.get("currency") or currency).lower()
    ctx["checkout_amount"] = "0"
    ctx.setdefault("browser_timezone", browser_timezone)
    ctx.setdefault("saved_payment_method_mode", "never")
    ctx.setdefault("browser_profile", browser_profile)
    ctx.setdefault("browser_user_agent", str(getattr(stripe, "headers", {}).get("User-Agent") or FIREFOX_USER_AGENT))
    payment_methods = list(payment_methods or [])

    _emit_payment_stage(
        progress_callback,
        "paypal_global_cslive_confirmation_token",
        f"{mode_label} cs_live: create PayPal confirmation token",
        5,
        total,
    )
    confirmation_token, confirmation_payload = opll_stripe_create_paypal_confirmation_token(
        stripe,
        checkout,
        ctx,
        billing,
        stripe_pk,
    )
    payment_method_id = str(
        opll_deep_first(confirmation_payload, ("payment_method", "payment_method_id", "paymentMethod"))
        or ""
    )

    payment_method_source = "confirmation_token"
    if not payment_method_id.startswith("pm_"):
        _emit_payment_stage(
            progress_callback,
            "paypal_global_cslive_payment_method",
            f"{mode_label} cs_live: create explicit PayPal PaymentMethod",
            6,
            total,
        )
        payment_method_id = opll_stripe_create_paypal_method(
            stripe,
            checkout_id,
            ctx,
            billing,
            stripe_pk,
        )
        payment_method_source = "explicit_payment_method"

    _emit_payment_stage(
        progress_callback,
        "paypal_global_cslive_stripe_confirm",
        f"{mode_label} cs_live: Stripe hosted confirm",
        7,
        total,
    )
    checkout_confirm_payload: dict = {}
    client_secret = ""
    intent_confirm_payload = opll_stripe_confirm(
        stripe,
        checkout_id,
        payment_method_id,
        stripe_pk,
        init_payload,
        ctx,
        checkout,
        stripe_hosted_url,
        payment_method_type="paypal",
    )
    confirm_amount, confirm_amount_source = opll_stripe_amount_info(intent_confirm_payload)
    if confirm_amount and not _opll_amount_is_zero(confirm_amount):
        raise RuntimeError(
            f"{mode_label} cs_live amount is not 0 after Stripe hosted confirm: "
            f"amount={confirm_amount}, source={confirm_amount_source}"
        )

    provider_url = ""
    paypal_approval_url = ""
    pm_redirect_url = ""
    for payload in (intent_confirm_payload, checkout_confirm_payload, confirmation_payload, init_payload):
        if not payload:
            continue
        candidate = opll_extract_redirect_to_url(payload) or opll_extract_paypal_candidate_url(payload)
        if not candidate:
            continue
        if opll_is_paypal_ba_approve_url(candidate):
            paypal_approval_url = candidate
            provider_url = candidate
            break
        if opll_is_pm_redirect_url(candidate):
            pm_redirect_url = candidate
            provider_url = candidate
            try:
                followed = opll_resolve_external_redirect(stripe, candidate)
            except Exception:
                followed = ""
            if opll_is_paypal_ba_approve_url(followed):
                paypal_approval_url = followed
                provider_url = followed
                break
    if not paypal_approval_url and pm_redirect_url:
        provider_url = provider_url or pm_redirect_url

    accepted_ba = opll_is_paypal_ba_approve_url(paypal_approval_url)
    accepted_pm = bool(pm_redirect_url)
    if not accepted_ba and not accepted_pm:
        raise RuntimeError(
            f"{mode_label} cs_live did not extract BA or PM link; "
            f"checkout_confirm={str(checkout_confirm_payload)[:400]} "
            f"intent_confirm={str(intent_confirm_payload)[:400]}"
        )
    final_url = paypal_approval_url if accepted_ba else pm_redirect_url
    final_kind = "ba" if accepted_ba else "pm_redirect"

    _emit_payment_stage(
        progress_callback,
        "done",
        f"PAYPAL global cs_live {final_kind} link extracted",
        total,
        total,
    )
    return {
        **checkout,
        "billing_country": billing_country,
        "currency": currency,
        "local_currency": local_currency,
        "currency_fallback": currency_fallback,
        "payment_method_country": billing_country,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": provider_url,
        "provider_redirect_url": provider_url,
        "paypal_approval_url": paypal_approval_url,
        "pm_redirect_url": pm_redirect_url,
        "paypal_result_kind": final_kind,
        "long_url": final_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, currency),
        "payment_methods": payment_methods,
        "payment_method_id": payment_method_id,
        "payment_method_source": payment_method_source,
        "confirmation_token": confirmation_token,
        "client_secret_prefix": client_secret.split("_secret_", 1)[0] if client_secret else "",
        "paypal_advertised_by_init": "paypal" in payment_methods,
        "promotion_update": promotion_payload,
        "checkout_tax_update": checkout_tax_payload,
        "checkout_confirm": checkout_confirm_payload,
        "intent_confirm": intent_confirm_payload,
        "billing_profile": billing,
        "billing_email": billing.get("email") or "",
        "checkout_amount": f"0 {currency}",
        "paypal_result_mode": "ba_or_pm",
        "local_payment": "paypal_global_rotation",
        "local_payment_detected": True,
        "discount_applied": True,
        "payment_locale": payment_locale,
        "browser_timezone": browser_timezone,
        "browser_profile": browser_profile,
        "customer_acceptance_ip": str(ctx.get("customer_acceptance_ip") or ""),
        "checkout_exit_ip": str(ctx.get("customer_acceptance_ip") or ""),
        "billing_country_source": "user_selection",
        "country_proxy_hint": "",
        "checkout_branch": "stripe_hosted_confirmation_token",
        "session_kind": str(checkout.get("session_kind") or "stripe_checkout"),
        "checkout_session_id": checkout_id,
        "checkout_session_type": session_type,
        "checkout_branch_requested": str(checkout.get("checkout_branch_requested") or ""),
        "checkout_branch_effective": "hosted",
        "oaics_probe": oaics_probe or checkout.get("oaics_probe") or {},
        "oaics_eligible": bool((oaics_probe or checkout.get("oaics_probe") or {}).get("oaics_eligible")),
        "paypal_proxy_country": paypal_proxy_country,
        "paypal_main_proxy_country": paypal_proxy_country,
        "promo_proxy_country": promo_proxy_country,
    }


def generate_opll_paypal_global_oaics_branch(access_token: str, checkout: dict,
                                             country_proxy_url: str,
                                             promo_proxy_url: str,
                                             progress_callback=None,
                                             chatgpt_cookie: str = "",
                                             billing: dict | None = None,
                                             billing_country: str = "DE",
                                             currency: str = "EUR",
                                             local_currency: str = "EUR",
                                             currency_fallback: bool = False,
                                             payment_locale: str = "en",
                                             browser_timezone: str = "Europe/Berlin",
                                             paypal_proxy_country: str = "",
                                             promo_proxy_country: str = "",
                                             total: int = 9) -> dict:
    """PAYPAL全球轮转 OAICS custom branch with three-country separation."""
    mode_label = "PAYPAL全球轮转"
    browser_profile = opll_normalize_browser_profile(PAYPAL_GLOBAL_OAICS_BROWSER_PROFILE)
    checkout["browser_profile"] = browser_profile
    checkout_id = str((checkout or {}).get("checkout_session_id") or
                      (checkout or {}).get("checkout_id") or
                      (checkout or {}).get("cs_id") or "").strip()
    if not checkout_id.startswith("oaics_"):
        raise RuntimeError(f"OAICS branch expected oaics_ checkout id, got {checkout_id}")
    billing_country = normalize_paypal_global_billing_country(billing_country)
    payment_locale = normalize_paypal_global_payment_locale(payment_locale)
    billing = dict(billing or opll_generate_paypal_global_profile(billing_country))
    billing["country"] = billing_country
    stripe_pk = opll_stripe_key_for_checkout(checkout)

    _emit_payment_stage(
        progress_callback,
        "paypal_global_oaics_promo",
        f"PAYPAL全球轮转 OAICS: promo checkout/update to zero (stage 2 proxy)",
        2,
        total,
    )
    promotion_payload = None
    promo_errors: list[str] = []
    oaics_promo_variants = [
        ("full-profile", {
            "include_full_profile": True,
            "billing_profile": billing,
            "checkout_ui_mode": "custom",
        }),
        ("standard", {
            "include_full_profile": False,
            "checkout_ui_mode": "custom",
        }),
        ("page-route", {
            "include_full_profile": False,
            "checkout_ui_mode": "custom",
            "checkout_page_route": True,
        }),
        ("hosted-contract", {
            "include_full_profile": False,
            "checkout_ui_mode": "hosted",
        }),
    ]
    for promo_variant, promo_kwargs in oaics_promo_variants:
        try:
            candidate_payload = opll_chatgpt_checkout_update_promotion(
                access_token,
                checkout,
                promo_proxy_url,
                chatgpt_cookie=chatgpt_cookie,
                normalize_vn=False,
                browser_profile=browser_profile,
                **promo_kwargs,
            )
            candidate_amount, candidate_amount_source = opll_stripe_amount_info(candidate_payload)
            if candidate_amount not in (None, "") and not _opll_amount_is_zero(candidate_amount):
                promo_errors.append(
                    f"{promo_variant}=nonzero amount={candidate_amount}, source={candidate_amount_source}"
                )
                continue
            promotion_payload = candidate_payload
            checkout["paypal_global_promo_variant"] = f"oaics-{promo_variant}"
            break
        except Exception as exc:
            promo_errors.append(f"{promo_variant}={opll_short_error(str(exc), 220)}")
    if promotion_payload is None:
        raise RuntimeError(
            "PAYPAL全球轮转 OAICS promo/update failed on all variants: "
            + "; ".join(promo_errors[-4:])
        )

    promotion_amount, promotion_amount_source = opll_stripe_amount_info(promotion_payload)
    if promotion_amount not in (None, "") and not _opll_amount_is_zero(promotion_amount):
        raise RuntimeError(
            f"PAYPAL全球轮转 OAICS amount is not 0 after promo: "
            f"amount={promotion_amount}, source={promotion_amount_source}"
        )

    stripe = opll_build_stripe_session(country_proxy_url, browser_profile=browser_profile)
    browser_user_agent = str(getattr(stripe, "headers", {}).get("User-Agent") or FIREFOX_USER_AGENT)
    elements_seed_ctx = {
        "browser_timezone": browser_timezone,
        "saved_payment_method_mode": "never",
        "browser_profile": browser_profile,
        "browser_user_agent": browser_user_agent,
        "currency": str(currency or checkout.get("currency") or "EUR").lower(),
        "checkout_amount": "0",
    }
    init_payload = {}
    stripe_amount = ""
    stripe_amount_source = ""
    elements_probe_errors: list[str] = []
    for sync_attempt in range(6):
        _emit_payment_stage(
            progress_callback,
            "paypal_global_oaics_elements_init",
            f"PAYPAL全球轮转 OAICS: promo check {sync_attempt + 1}/6 Elements session",
            3,
            total,
        )
        try:
            candidate_payload = opll_oaics_stripe_elements_init(
                stripe,
                checkout,
                promotion_payload,
                payment_locale=payment_locale,
                ctx=elements_seed_ctx,
                payment_method_type="paypal",
            )
            candidate_amount, candidate_amount_source = opll_stripe_amount_info(candidate_payload)
            candidate_methods = opll_collect_payment_method_types(candidate_payload)
            if (candidate_amount in (None, "") or _opll_amount_is_zero(candidate_amount)) and "paypal" in candidate_methods:
                init_payload = candidate_payload
                stripe_amount = candidate_amount
                stripe_amount_source = candidate_amount_source
                break
            elements_probe_errors.append(
                f"{sync_attempt + 1}/6 amount={candidate_amount}, source={candidate_amount_source}, "
                f"pm={candidate_methods or []}, diag={opll_payment_method_diagnostics(candidate_payload)}"
            )
        except Exception as exc:
            elements_probe_errors.append(f"{sync_attempt + 1}/6 {opll_short_error(str(exc), 260)}")
        time.sleep(0.8 if sync_attempt == 0 else 1.5)
    if not init_payload:
        raise RuntimeError(
            "PAYPAL全球轮转 OAICS promo Elements session did not reach zero+paypal after checkout/update: "
            + " | ".join(elements_probe_errors[-6:])
        )

    ctx = opll_stripe_context(init_payload, payment_locale, elements_seed_ctx)
    ctx["currency"] = str(currency or checkout.get("currency") or "").lower()
    ctx["checkout_amount"] = "0"
    payment_methods_after_init = opll_collect_payment_method_types(init_payload)
    ctx["payment_method_types"] = payment_methods_after_init
    if "paypal" not in payment_methods_after_init:
        raise RuntimeError(
            "PAYPAL全球轮转 OAICS elements/sessions did not expose PayPal after promo: "
            + opll_payment_method_diagnostics(init_payload)
        )
    if stripe_amount not in (None, "") and not _opll_amount_is_zero(stripe_amount):
        raise RuntimeError(
            f"PAYPAL全球轮转 OAICS amount is not 0 after elements init: "
            f"amount={stripe_amount}, source={stripe_amount_source}"
        )

    _emit_payment_stage(
        progress_callback,
        "paypal_global_oaics_taxes",
        f"PAYPAL全球轮转 OAICS: sync billing country to {billing_country}",
        4,
        total,
    )
    tax_payload = opll_chatgpt_checkout_update_taxes(
        access_token,
        checkout,
        country_proxy_url,
        billing=billing,
        currency=currency,
        chatgpt_cookie=chatgpt_cookie,
        browser_profile=browser_profile,
    )
    tax_amount, tax_amount_source = opll_stripe_amount_info(tax_payload)
    if tax_amount not in (None, "") and not _opll_amount_is_zero(tax_amount):
        raise RuntimeError(
            f"PAYPAL全球轮转 OAICS amount changed after tax sync: "
            f"amount={tax_amount}, source={tax_amount_source}"
        )

    _emit_payment_stage(
        progress_callback,
        "paypal_global_oaics_elements_refresh",
        f"PAYPAL全球轮转 OAICS: refresh Stripe Elements after tax",
        5,
        total,
    )
    post_tax_init_payload = opll_oaics_stripe_elements_init(
        stripe,
        checkout,
        promotion_payload,
        tax_payload,
        payment_locale=payment_locale,
        ctx=ctx,
        payment_method_type="paypal",
    )
    post_tax_amount, post_tax_amount_source = opll_stripe_amount_info(post_tax_init_payload)
    if post_tax_amount not in (None, "") and not _opll_amount_is_zero(post_tax_amount):
        raise RuntimeError(
            f"PAYPAL全球轮转 OAICS amount changed after post-tax init: "
            f"amount={post_tax_amount}, source={post_tax_amount_source}"
        )
    ctx = opll_stripe_context(post_tax_init_payload, payment_locale, ctx)
    ctx["currency"] = str(currency or checkout.get("currency") or "").lower()
    ctx["checkout_amount"] = "0"
    payment_methods = opll_collect_payment_method_types(post_tax_init_payload) or \
        opll_collect_payment_method_types(init_payload)
    ctx["payment_method_types"] = payment_methods
    if "paypal" not in payment_methods:
        raise RuntimeError(
            "PAYPAL全球轮转 OAICS post-tax elements/sessions removed PayPal: "
            + opll_payment_method_diagnostics(post_tax_init_payload or init_payload)
        )

    _emit_payment_stage(
        progress_callback,
        "paypal_global_oaics_confirmation_token",
        f"PAYPAL全球轮转 OAICS: create PayPal confirmation token",
        6,
        total,
    )
    confirmation_token, confirmation_payload = opll_stripe_create_paypal_confirmation_token(
        stripe,
        checkout,
        ctx,
        billing,
        stripe_pk,
    )
    payment_method_id = str(
        opll_deep_first(confirmation_payload, ("payment_method", "payment_method_id", "paymentMethod"))
        or ""
    )

    _emit_payment_stage(
        progress_callback,
        "paypal_global_oaics_checkout_confirm",
        f"PAYPAL全球轮转 OAICS: OpenAI checkout/confirm",
        7,
        total,
    )
    checkout_confirm_payload = opll_chatgpt_checkout_confirm_with_token(
        access_token,
        checkout,
        confirmation_token,
        proxy_url=country_proxy_url,
        chatgpt_cookie=chatgpt_cookie,
        selected_payment_method_type="paypal",
        browser_profile=browser_profile,
    )
    client_secret = opll_extract_client_secret(checkout_confirm_payload)
    if not client_secret:
        raise RuntimeError(
            "PAYPAL全球轮转 OAICS checkout/confirm did not return client_secret; "
            f"payload={str(checkout_confirm_payload)[:500]}"
        )

    _emit_payment_stage(
        progress_callback,
        "paypal_global_oaics_intent_confirm",
        f"PAYPAL全球轮转 OAICS: Stripe Intent confirm",
        8,
        total,
    )
    return_url = opll_chatgpt_success_return_url(
        checkout_id,
        billing_country,
        checkout.get("processor_entity") or "",
    )
    intent_confirm_payload = opll_stripe_confirm_intent_with_confirmation_token(
        stripe,
        client_secret,
        confirmation_token,
        checkout,
        ctx,
        stripe_pk,
        return_url=return_url,
    )
    confirm_amount, confirm_amount_source = opll_stripe_amount_info(intent_confirm_payload)
    if confirm_amount and not _opll_amount_is_zero(confirm_amount):
        raise RuntimeError(
            f"PAYPAL全球轮转 OAICS amount is not 0 after confirm: "
            f"amount={confirm_amount}, source={confirm_amount_source}"
        )

    _emit_payment_stage(
        progress_callback,
        "paypal_global_oaics_resolve",
        f"PAYPAL全球轮转 OAICS: resolve PayPal redirect / BA",
        9,
        total,
    )
    stripe_redirect_url = (
        opll_extract_redirect_to_url(intent_confirm_payload)
        or opll_extract_redirect_to_url(checkout_confirm_payload)
        or opll_extract_redirect_to_url(confirmation_payload)
    )
    provider_url = stripe_redirect_url
    paypal_approval_url = ""
    if opll_is_paypal_ba_approve_url(provider_url):
        paypal_approval_url = provider_url
    elif opll_is_pm_redirect_url(provider_url):
        try:
            followed = opll_resolve_external_redirect(stripe, provider_url)
        except Exception:
            followed = provider_url
        provider_url = followed or provider_url
        if opll_is_paypal_ba_approve_url(provider_url):
            paypal_approval_url = provider_url
    if not paypal_approval_url:
        candidate = (
            opll_extract_paypal_candidate_url(intent_confirm_payload)
            or opll_extract_paypal_candidate_url(checkout_confirm_payload)
            or opll_extract_paypal_candidate_url(confirmation_payload)
            or opll_extract_paypal_candidate_url(post_tax_init_payload)
        )
        if opll_is_paypal_ba_approve_url(candidate):
            paypal_approval_url = candidate
            provider_url = candidate

    accepted_ba = opll_is_paypal_ba_approve_url(paypal_approval_url)
    pm_redirect_url = stripe_redirect_url if opll_is_pm_redirect_url(stripe_redirect_url) else ""
    if not pm_redirect_url and opll_is_pm_redirect_url(provider_url):
        pm_redirect_url = provider_url
    accepted_pm = bool(pm_redirect_url)
    if not accepted_ba and not accepted_pm:
        raise RuntimeError(
            f"{mode_label} OAICS did not extract BA or PM link; "
            f"current={provider_url or stripe_redirect_url}"
        )
    final_url = paypal_approval_url if accepted_ba else pm_redirect_url
    final_kind = "ba" if accepted_ba else "pm_redirect"

    _emit_payment_stage(
        progress_callback,
        "done",
        f"PAYPAL global OAICS: {final_kind} link extracted",
        total,
        total,
    )
    return {
        **checkout,
        "checkout_session_id": checkout_id,
        "cs_id": checkout_id,
        "checkout_id": checkout_id,
        "session_kind": "openai_custom_checkout",
        "checkout_branch": "oaics_custom",
        "checkout_session_type": "oaics",
        "checkout_branch_requested": str(checkout.get("checkout_branch_requested") or ""),
        "checkout_branch_effective": "oaics",
        "oaics_probe": checkout.get("oaics_probe") or {},
        "oaics_eligible": True,
        "billing_country": billing_country,
        "currency": currency,
        "local_currency": local_currency,
        "currency_fallback": currency_fallback,
        "payment_method_country": billing_country,
        "stripe_hosted_url": "",
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": provider_url,
        "paypal_approval_url": paypal_approval_url,
        "paypal_url": paypal_approval_url,
        "pm_redirect_url": pm_redirect_url,
        "paypal_result_kind": final_kind,
        "long_url": final_url,
        "stripe_amount": post_tax_amount or stripe_amount or promotion_amount,
        "stripe_amount_source": post_tax_amount_source or stripe_amount_source or promotion_amount_source,
        "payment_amount_display": "0 " + currency,
        "payment_methods": payment_methods,
        "payment_method_id": payment_method_id,
        "confirmation_token": confirmation_token,
        "client_secret_prefix": client_secret.split("_secret_", 1)[0] if client_secret else "",
        "paypal_advertised_by_init": "paypal" in payment_methods,
        "promotion_update": promotion_payload,
        "checkout_tax_update": tax_payload,
        "billing_profile": billing,
        "billing_email": billing.get("email") or "",
        "checkout_amount": f"0 {currency}",
        "paypal_result_mode": "ba_or_pm",
        "local_payment": "paypal_global_rotation",
        "local_payment_detected": True,
        "discount_applied": True,
        "promotion_applied": True,
        "promotion_zero_verified": True,
        "promotion_amount": promotion_amount,
        "promotion_amount_source": promotion_amount_source,
        "payment_locale": payment_locale,
        "browser_timezone": browser_timezone,
        "browser_profile": browser_profile,
        "customer_acceptance_ip": str(ctx.get("customer_acceptance_ip") or ""),
        "checkout_exit_ip": str(ctx.get("customer_acceptance_ip") or ""),
        "billing_country_source": "user_selection",
        "country_proxy_hint": "",
        "paypal_proxy_country": paypal_proxy_country,
        "paypal_main_proxy_country": paypal_proxy_country,
        "promo_proxy_country": promo_proxy_country,
        "oaics_elements_init": init_payload,
        "oaics_elements_refresh": post_tax_init_payload,
        "oaics_tax_update": tax_payload,
        "oaics_checkout_confirm": checkout_confirm_payload,
        "oaics_intent_confirm": intent_confirm_payload,
    }


def generate_opll_team_codex_low_link(access_token: str, proxy_url: str = "",
                                      progress_callback=None,
                                      workspace_name: str = "work",
                                      quantity: int = 13) -> dict:
    """Generate a Team Codex usage-based credit checkout link.

    The successful low-price flow uses ChatGPT's usage-based workspace credit
    checkout. quantity=13 credits is currently 52 minor USD units ($0.52).
    """
    workspace = str(workspace_name or "work").strip() or "work"
    try:
        qty = int(quantity or 13)
    except Exception:
        qty = 13
    if qty <= 0:
        qty = 13
    _emit_payment_stage(progress_callback, "checkout", "Team Codex create 13 credits checkout", 1, 4)
    payload = {
        "plan_name": "chatgptbusiness_usage_based",
        "entry_point": "team_workspace_purchase_modal",
        "checkout_ui_mode": "hosted",
        "billing_details": {"country": "US", "currency": "USD"},
        "usage_based_workspace_credit_purchase_data": {
            "workspace_name": workspace,
            "quantity": qty,
            "unit": "credit",
        },
        "cancel_url": "https://chatgpt.com/#pricing",
    }
    session = opll_build_chatgpt_session(access_token, proxy_url)
    response = session.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=payload,
        headers={
            "Referer": "https://chatgpt.com/#pricing",
            "x-openai-target-path": "/backend-api/payments/checkout",
            "x-openai-target-route": "/backend-api/payments/checkout",
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Team Codex checkout create failed: HTTP {response.status_code} {response.text[:500]}")
    data = response.json() or {}
    cs_id = opll_extract_checkout_id(data)
    if not cs_id or not str(cs_id).startswith("cs_"):
        raise RuntimeError(f"Team Codex checkout response missing cs_id: {str(data)[:500]}")
    processor_entity = opll_extract_processor_entity(data) or "openai_llc"
    stripe_pk = opll_extract_stripe_publishable_key(data) or DEFAULT_STRIPE_PK
    checkout = {
        "cs_id": str(cs_id),
        "checkout_id": str(cs_id),
        "processor_entity": processor_entity,
        "stripe_publishable_key": stripe_pk,
        "billing_country": "US",
        "currency": "USD",
        "checkout_ui_mode": "hosted",
        "raw_checkout": data,
    }


def opll_paypal_global_checkout_contract(country: str, checkout_branch: str = "auto",
                                         paypal_proxy_country: str = "") -> dict:
    """Country-specific first-stage contract for PAYPAL global rotation.

    The visible mode stays PAYPAL全球轮转.  Internally it can take either the
    older hosted/cs path or the OAICS custom path.  Billing country and PayPal
    proxy country are intentionally independent.
    """
    country = normalize_paypal_global_billing_country(country)
    branch = str(checkout_branch or "auto").strip().lower()
    paypal_proxy_country = opll_normalize_country_hint(paypal_proxy_country)
    use_oaics = branch in {"oaics", "custom", "oaics_custom", "openai_custom_checkout"}
    if branch in {"hosted", "cs", "stripe_hosted"}:
        use_oaics = False
    elif branch == "auto":
        use_oaics = country == "GB" or paypal_proxy_country in PAYPAL_GLOBAL_OAICS_MAIN_PROXY_COUNTRIES
    if use_oaics:
        return {
            "checkout_ui_mode": "custom",
            "require_stripe_session": False,
            "promo_campaign_id": None,
            "hosted_payload_contract": False,
            "checkout_branch": "oaics_custom",
            "allow_openai_checkout_session": True,
            "send_billing_profile": True,
        }
    return {
        "checkout_ui_mode": "hosted",
        "require_stripe_session": True,
        "promo_campaign_id": None,
        "hosted_payload_contract": True,
        "checkout_branch": "stripe_hosted",
        "allow_openai_checkout_session": True,
        "send_billing_profile": True,
    }
    _emit_payment_stage(progress_callback, "stripe_init", "Team Codex init Stripe and read amount", 2, 4)
    stripe = opll_build_stripe_session(proxy_url)
    init_payload = opll_stripe_init(
        str(cs_id), "US", "USD", proxy_url, payment_locale="en-US",
        stripe=stripe, checkout=checkout,
    )
    stripe_hosted_url = str(data.get("url") or init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(f"Team Codex stripe init response missing hosted url, keys={sorted(init_payload.keys())}")
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    if str(stripe_amount).strip() not in {"52", "53"}:
        raise RuntimeError(
            f"Team Codex amount is not $0.52-ish: amount={stripe_amount} source={stripe_amount_source}; retry with another US proxy/session"
        )
    _emit_payment_stage(progress_callback, "build_link", "Team Codex build pay.openai.com link", 3, 4)
    long_url = str(data.get("url") or "").strip() or opll_to_openai_pay_url(stripe_hosted_url) or stripe_hosted_url
    stripe_redirect_url = f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}?kind=codex_team"
    expires_at, expires_raw = opll_checkout_expires_at(checkout, init_payload)
    _emit_payment_stage(progress_callback, "done", "Team Codex 0.52 link generated", 4, 4)
    return {
        **checkout,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": long_url,
        "long_url": long_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, "USD"),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "team_codex",
        "local_payment_detected": True,
        "team_codex_quantity": str(qty),
        "checkout_amount": f"{stripe_amount} USD",
    }

def generate_opll_chatgpt_short_link(access_token: str, country: str, currency: str,
                                     proxy_url: str = "",
                                     progress_callback=None,
                                     processor_entity: str = "openai_llc") -> dict:
    """
    Generate a ChatGPT custom checkout short link.

    This intentionally stops after ChatGPT checkout creation and returns the
    first-party checkout URL, e.g.:
      https://chatgpt.com/checkout/openai_llc/oaics_xxx
    """
    _emit_payment_stage(progress_callback, "checkout", f"创建 {country.upper()} ChatGPT custom checkout", 1, 2)
    checkout = opll_create_checkout(
        access_token,
        country,
        currency,
        proxy_url,
        checkout_ui_mode="custom",
        require_stripe_session=False,
        preferred_processor_entity=processor_entity,
    )
    checkout_id = str(checkout.get("checkout_id") or checkout.get("cs_id") or "").strip()
    entity = str(checkout.get("processor_entity") or processor_entity or "openai_llc").strip()
    raw_checkout = checkout.get("raw_checkout") if isinstance(checkout.get("raw_checkout"), dict) else {}
    short_url = opll_extract_chatgpt_checkout_url(raw_checkout) or opll_chatgpt_checkout_url(checkout_id, entity)
    if not short_url:
        raise RuntimeError("checkout short link build failed: missing checkout id")
    parsed_short = urlsplit(short_url)
    parts = [part for part in parsed_short.path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "checkout":
        entity = parts[1] or entity
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(raw_checkout)
    expires_at, expires_raw = opll_checkout_expires_at(checkout, raw_checkout)
    _emit_payment_stage(progress_callback, "done", "已生成 ChatGPT 菲律宾短链", 2, 2)
    return {
        **checkout,
        "checkout_id": checkout_id,
        "short_url": short_url,
        "chatgpt_checkout_url": short_url,
        "long_url": short_url,
        "processor_entity": entity,
        "stripe_hosted_url": "",
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, checkout["currency"]),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "chatgpt_short_link",
        "local_payment_detected": True,
    }


def generate_opll_ph_cross_region_promo_short_link(
        access_token: str,
        checkout_proxy_url: str,
        promotion_proxy_url: str,
        progress_callback=None,
        chatgpt_cookie: str = "",
        processor_entity: str = "openai_llc") -> dict:
    """Create PH/PHP custom checkout, then apply its promotion on stage 2."""
    checkout_proxy_url = str(checkout_proxy_url or "").strip()
    promotion_proxy_url = str(promotion_proxy_url or "").strip()
    if not checkout_proxy_url:
        raise RuntimeError("菲律宾跨区转优惠提链缺少 Checkout 代理")
    if not promotion_proxy_url:
        raise RuntimeError("菲律宾跨区转优惠提链缺少优惠代理")

    _emit_payment_stage(
        progress_callback,
        "ph_cross_checkout",
        "菲律宾跨区：创建 PH/PHP custom checkout",
        1,
        3,
    )
    # Keep the stages unambiguous: creation carries no campaign; stage 2 owns it.
    checkout = opll_create_checkout(
        access_token,
        "PH",
        "PHP",
        checkout_proxy_url,
        checkout_ui_mode="custom",
        require_stripe_session=False,
        preferred_processor_entity=processor_entity,
        promo_campaign_id=None,
    )

    _emit_payment_stage(
        progress_callback,
        "ph_cross_promo",
        "菲律宾跨区：优惠代理提交 checkout/update",
        2,
        3,
    )
    promotion = opll_chatgpt_checkout_update_promotion(
        access_token,
        checkout,
        promotion_proxy_url,
        chatgpt_cookie=str(chatgpt_cookie or ""),
        normalize_vn=False,
        include_full_profile=False,
        include_promo=True,
        checkout_ui_mode="custom",
    )

    original_id = str(checkout.get("checkout_id") or checkout.get("cs_id") or "").strip()
    checkout_id = str(opll_extract_checkout_id(promotion) or original_id).strip()
    entity = str(
        opll_extract_processor_entity(promotion)
        or checkout.get("processor_entity")
        or processor_entity
        or "openai_llc"
    ).strip()
    short_url = (
        opll_extract_chatgpt_checkout_url(promotion)
        or opll_extract_chatgpt_checkout_url(checkout.get("raw_checkout") or {})
        or opll_chatgpt_checkout_url(checkout_id, entity)
    )
    if not checkout_id or not short_url:
        raise RuntimeError("菲律宾跨区优惠更新后缺少 checkout id / short url")

    promotion_amount, promotion_amount_source = opll_stripe_amount_info(promotion)
    promotion_amount_observed = promotion_amount_source not in {"missing_payload", "fallback_zero"}
    promotion_zero_verified = bool(
        promotion_amount_observed and opll_amount_is_zero(promotion_amount)
    )
    expires_at, expires_raw = opll_checkout_expires_at(checkout, promotion)
    _emit_payment_stage(
        progress_callback,
        "done",
        "已生成菲律宾跨区转优惠短链",
        3,
        3,
    )
    return {
        **checkout,
        "cs_id": checkout_id,
        "checkout_id": checkout_id,
        "processor_entity": entity,
        "short_url": short_url,
        "chatgpt_checkout_url": short_url,
        "long_url": short_url,
        "stripe_hosted_url": "",
        "promotion_response": promotion,
        "promotion_applied": True,
        "promotion_amount": promotion_amount if promotion_amount_observed else "",
        "promotion_amount_source": promotion_amount_source,
        "promotion_zero_verified": promotion_zero_verified,
        "payment_amount_display": (
            opll_format_minor_amount(promotion_amount, "PHP")
            if promotion_amount_observed else "待 Checkout 页面确认"
        ),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "billing_country": "PH",
        "currency": "PHP",
        "local_payment": "chatgpt_ph_cross_region_promo",
        "local_payment_detected": True,
    }


def opll_is_gcash_adyen_redirect_url(value: str) -> bool:
    """Return True only for an Adyen checkoutPaymentRedirect action URL."""
    text = str(value or "").strip()
    if not text:
        return False
    try:
        parsed = urlsplit(text)
    except Exception:
        return False
    host = str(parsed.hostname or "").lower()
    return bool(
        parsed.scheme in {"http", "https"}
        and host in {"checkoutshopper-live.adyen.com", "checkoutshopper-test.adyen.com"}
        and parsed.path.rstrip("/").endswith("/checkoutPaymentRedirect")
        and parse_qs(parsed.query, keep_blank_values=True).get("redirectData")
    )


def opll_find_gcash_adyen_redirect_url(payload) -> str:
    """Recursively extract the GCash Adyen action URL without decoding redirectData."""
    seen: set[int] = set()

    def visit(value) -> str:
        if isinstance(value, str):
            text = value.strip().replace("\\u0026", "&")
            if opll_is_gcash_adyen_redirect_url(text):
                return text
            match = re.search(
                r"https://checkoutshopper-(?:live|test)\.adyen\.com/"
                r"checkoutshopper/checkoutPaymentRedirect\?redirectData=[^\s\"'<>]+",
                text,
                flags=re.IGNORECASE,
            )
            if match and opll_is_gcash_adyen_redirect_url(match.group(0)):
                return match.group(0)
            if text.startswith(("{", "[")):
                try:
                    return visit(json.loads(text))
                except Exception:
                    return ""
            return ""
        if not isinstance(value, (dict, list, tuple)):
            return ""
        marker = id(value)
        if marker in seen:
            return ""
        seen.add(marker)
        if isinstance(value, dict):
            # Action and redirect fields are checked first so an unrelated URL
            # elsewhere in the provider payload cannot win.
            priority = (
                "action", "url", "redirect_url", "redirectUrl",
                "provider_redirect_url", "providerRedirectUrl",
            )
            for key in priority:
                if key in value:
                    found = visit(value.get(key))
                    if found:
                        return found
            children = value.values()
        else:
            children = value
        for child in children:
            found = visit(child)
            if found:
                return found
        return ""

    return visit(payload)


def opll_find_checkout_verification_url(payload, checkout_id: str = "") -> str:
    """Extract the first-party verification hop used before the Adyen redirect."""
    expected_id = str(checkout_id or "").strip()
    pattern = re.compile(r"https://chatgpt\.com/checkout/verify\?[^\s\"'<>]+", re.IGNORECASE)

    def visit(value) -> str:
        if isinstance(value, str):
            text = value.strip().replace("\\u0026", "&")
            match = pattern.search(text)
            if match:
                return match.group(0)
            if text.startswith(("{", "[")):
                try:
                    return visit(json.loads(text))
                except Exception:
                    return ""
            return ""
        if isinstance(value, dict):
            for key in ("verification_url", "verificationUrl", "return_url", "returnUrl", "url"):
                if key in value:
                    found = visit(value.get(key))
                    if found:
                        return found
            for child in value.values():
                found = visit(child)
                if found:
                    return found
        elif isinstance(value, (list, tuple)):
            for child in value:
                found = visit(child)
                if found:
                    return found
        return ""

    found = visit(payload)
    if found:
        return found
    if expected_id:
        return "https://chatgpt.com/checkout/verify?" + urlencode({"stripe_session_id": expected_id})
    return ""


def opll_find_gcash_custom_payment_method_id(*payloads) -> str:
    """Find the Stripe custom-payment-method id used by the Adyen GCash bridge."""
    contextual: list[str] = []
    all_candidates: list[str] = []
    seen_nodes: set[int] = set()

    def remember(value: str, is_gcash: bool = False) -> None:
        match = re.search(r"\bcpmt_[A-Za-z0-9_-]+", str(value or ""))
        if not match:
            return
        candidate = match.group(0)
        if candidate not in all_candidates:
            all_candidates.append(candidate)
        if is_gcash and candidate not in contextual:
            contextual.append(candidate)

    def text_context(value) -> str:
        texts: list[str] = []

        def collect(node, depth: int = 0) -> None:
            if depth > 4 or len(texts) >= 40:
                return
            if isinstance(node, str):
                texts.append(node)
            elif isinstance(node, dict):
                for key, child in node.items():
                    texts.append(str(key))
                    collect(child, depth + 1)
            elif isinstance(node, (list, tuple)):
                for child in node:
                    collect(child, depth + 1)

        collect(value)
        return " ".join(texts).lower()

    def visit(value) -> None:
        if isinstance(value, str):
            remember(value, "gcash" in value.lower())
            if value.lstrip().startswith(("{", "[")):
                try:
                    visit(json.loads(value))
                except Exception:
                    pass
            return
        if not isinstance(value, (dict, list, tuple)):
            return
        marker = id(value)
        if marker in seen_nodes:
            return
        seen_nodes.add(marker)
        if isinstance(value, dict):
            context = text_context(value)
            is_gcash = "gcash" in context or (
                "adyen" in context and "ph" in context
            )
            for key in (
                "id", "custom_payment_method_type_id", "customPaymentMethodTypeId",
                "payment_method_type_id", "paymentMethodTypeId", "type_id", "value",
            ):
                remember(str(value.get(key) or ""), is_gcash)
            for child in value.values():
                visit(child)
        else:
            for child in value:
                visit(child)

    for payload in payloads:
        visit(payload)
    if contextual:
        return contextual[0]
    if len(all_candidates) == 1:
        return all_candidates[0]
    return ""


def opll_resolve_gcash_verification_redirect(chatgpt, verification_url: str) -> str:
    """Follow only redirect headers and retain the signed Adyen action URL."""
    current = str(verification_url or "").strip()
    visited: set[str] = set()
    for _ in range(8):
        if not current or current in visited:
            break
        visited.add(current)
        if opll_is_gcash_adyen_redirect_url(current):
            return current
        response = chatgpt.get(
            current,
            headers={"Referer": "https://chatgpt.com/"},
            allow_redirects=False,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
        body_url = opll_find_gcash_adyen_redirect_url(str(getattr(response, "text", "") or ""))
        if body_url:
            return body_url
        location = str(getattr(response, "headers", {}).get("Location") or "").strip()
        if not location:
            break
        current = urljoin(current, location)
    return ""


def opll_gcash_checkout_approval_headers(chatgpt) -> dict:
    """Build the Sentinel header used by ChatGPT's manual checkout approval."""
    device_id = str(getattr(chatgpt, "headers", {}).get("oai-device-id") or "").strip()
    user_agent = str(
        getattr(chatgpt, "headers", {}).get("User-Agent") or DEFAULT_USER_AGENT
    ).strip()
    if not device_id:
        return {}

    # Do not reuse the authenticated ChatGPT session for sentinel.openai.com:
    # requests would merge its Authorization header into the cross-origin call.
    sentinel_session = requests.Session()
    sentinel_session.trust_env = False
    proxies = getattr(chatgpt, "proxies", None)
    if proxies:
        sentinel_session.proxies.update(dict(proxies))
    sentinel_session.headers.update({"User-Agent": user_agent})
    sentinel_session.cookies.set("oai-did", device_id, domain=".chatgpt.com", path="/")

    # The checkout page loads the SDK from chatgpt.com, therefore the SDK posts
    # its challenge to the same-origin ChatGPT sentinel endpoint.  The shared
    # generator defaults to sentinel.openai.com (the auth/signup variant), so
    # rewrite only the challenge call while retaining its real SDK/PoW solver.
    original_post = sentinel_session.post

    def checkout_sentinel_post(url, *args, **kwargs):
        if str(url).rstrip("/").endswith("/backend-api/sentinel/req"):
            url = "https://chatgpt.com/backend-api/sentinel/req"
            headers = dict(kwargs.get("headers") or {})
            headers.update({
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/backend-api/sentinel/frame.html",
            })
            kwargs["headers"] = headers
        return original_post(url, *args, **kwargs)

    sentinel_session.post = checkout_sentinel_post
    try:
        from sentinel import get_sentinel_token

        token = get_sentinel_token(
            sentinel_session,
            device_id=device_id,
            flow="checkout_session_approval",
            user_agent=user_agent,
        )
        return {
            "OpenAI-Sentinel-Token": token,
            "OAI-Telemetry": "[1,null]",
        } if token else {}
    finally:
        sentinel_session.close()


def opll_submit_gcash_action(access_token: str, checkout: dict, proxy_url: str = "",
                              chatgpt_cookie: str = "", progress_callback=None) -> dict:
    """Select GCash on the oaics checkout and return its signed redirect action."""
    checkout_id = str((checkout or {}).get("checkout_id") or (checkout or {}).get("cs_id") or "").strip()
    if not checkout_id:
        raise RuntimeError("菲律宾 GCash 提链缺少 checkout id")
    entity = str((checkout or {}).get("processor_entity") or "openai_llc").strip() or "openai_llc"
    token = opll_access_token_with_cookie(access_token, chatgpt_cookie, proxy_url)
    if not token:
        raise RuntimeError("菲律宾 GCash 提链缺少 Access Token")
    chatgpt = opll_build_chatgpt_session(token, proxy_url, chatgpt_cookie=chatgpt_cookie)
    checkout_page = f"https://chatgpt.com/checkout/{entity}/{checkout_id}"
    route_headers = {
        "Referer": checkout_page,
        "x-openai-target-path": f"/checkout/{entity}/{checkout_id}",
        "x-openai-target-route": "/checkout/[processorEntity]/[checkoutSessionId]",
        "OAI-Chat-Web-Route": "/checkout/[processorEntity]/[checkoutSessionId]",
    }

    def decode(response) -> dict:
        try:
            value = response.json() or {}
        except Exception:
            value = {"raw": str(getattr(response, "text", "") or "")[:1000]}
        return value if isinstance(value, dict) else {"payload": value}

    # GCash is exposed by the checkout as a Stripe custom payment method whose
    # id starts with cpmt_.  Sending the literal string "gcash" makes the web
    # backend classify it as a regular Stripe method and demand a confirmation
    # token.  Refresh the checkout state because checkout/update may replace it.
    state_payload: dict = {}
    state_response = chatgpt.get(
        f"https://chatgpt.com/backend-api/payments/checkout/{entity}/{checkout_id}",
        headers=route_headers,
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if state_response.status_code < 400:
        state_payload = decode(state_response)
    gcash_method_id = opll_find_gcash_custom_payment_method_id(
        (checkout or {}).get("raw_checkout"),
        (checkout or {}).get("_gcash_promotion_response"),
        state_payload,
    )
    if not gcash_method_id:
        raise RuntimeError(
            "菲律宾 GCash checkout 未返回 GCash custom payment method id (cpmt_*)"
        )

    # A checkout may expose the GCash tile while its browser-side checkout
    # state still says canConfirm=false (for example, billing/snapshot state is
    # completed only inside the signed-in checkout page).  Calling the custom
    # method start endpoint in that state deterministically returns HTTP 409.
    # Preserve the useful GCash-enabled checkout handoff instead of burning the
    # retry pool on a state that only the checkout page can complete.
    checkout_state = (
        state_payload.get("checkout_state")
        if isinstance(state_payload, dict) else None
    )
    if isinstance(checkout_state, dict) and checkout_state.get("canConfirm") is False:
        return {
            "action_url": "",
            "verification_url": checkout_page,
            "custom_payment_method_type_id": gcash_method_id,
            "confirm_payload": {"status": "requires_browser_confirmation"},
            "last_payload": state_payload,
            "requires_browser_confirm": True,
            "trace": [{
                "name": "checkout_preflight",
                "status": int(state_response.status_code),
                "result": "requires_browser_confirmation",
                "payment_method_type": gcash_method_id,
            }],
        }

    confirm_headers = dict(route_headers)
    confirm_headers.update(opll_gcash_checkout_approval_headers(chatgpt))
    confirm = chatgpt.post(
        "https://chatgpt.com/backend-api/payments/checkout/confirm",
        json={
            "checkout_session_id": checkout_id,
            "selected_payment_method_type": gcash_method_id,
        },
        headers=confirm_headers,
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    confirm_payload = decode(confirm)
    if confirm.status_code >= 400:
        raise RuntimeError(
            f"菲律宾 GCash confirm failed: HTTP {confirm.status_code} "
            f"{opll_short_error(str(confirm_payload), 700)}"
        )

    trace = [{
        "name": "confirm",
        "status": int(confirm.status_code),
        "result": str(confirm_payload.get("result") or confirm_payload.get("status") or ""),
        "payment_method_type": gcash_method_id,
    }]
    confirm_state = str(
        confirm_payload.get("result") or confirm_payload.get("status") or ""
    ).strip().lower()
    if confirm_state == "blocked":
        # Custom checkouts commonly require the existing OpenAI manual-approval
        # hop before the Adyen custom-payment flow can be started.
        try:
            chatgpt.post(
                "https://chatgpt.com/backend-api/sentinel/ping",
                json={},
                headers={
                    "Referer": "https://chatgpt.com/",
                    "x-openai-target-path": "/backend-api/sentinel/ping",
                    "x-openai-target-route": "/backend-api/sentinel/ping",
                },
                timeout=PAY_LONG_LINK_TIMEOUT,
            )
        except Exception:
            pass
        approved = False
        last_approve_payload: dict = {}
        for attempt in range(1, 9):
            approve_headers = {
                "Referer": checkout_page,
                "x-openai-target-path": "/backend-api/payments/checkout/approve",
                "x-openai-target-route": "/backend-api/payments/checkout/approve",
            }
            # The current web client requests a fresh Sentinel token for the
            # checkout_session_approval flow before every approval attempt.
            approve_headers.update(opll_gcash_checkout_approval_headers(chatgpt))
            approve = chatgpt.post(
                "https://chatgpt.com/backend-api/payments/checkout/approve",
                json={
                    "checkout_session_id": checkout_id,
                    "processor_entity": entity,
                },
                headers=approve_headers,
                timeout=PAY_LONG_LINK_TIMEOUT,
            )
            last_approve_payload = decode(approve)
            approve_result = str(
                last_approve_payload.get("result")
                or last_approve_payload.get("status")
                or ""
            ).strip().lower()
            trace.append({
                "name": "approve",
                "attempt": attempt,
                "status": int(approve.status_code),
                "result": approve_result,
            })
            if approve.status_code < 400 and approve_result == "approved":
                approved = True
                break
            if approve.status_code >= 400:
                break
            time.sleep(0.25)
        if not approved:
            raise RuntimeError(
                "菲律宾 GCash checkout approve 未通过: "
                + opll_short_error(str(last_approve_payload), 700)
            )

    start = chatgpt.post(
        "https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/start",
        json={
            "checkout_session_id": checkout_id,
            "custom_payment_method_type_id": gcash_method_id,
        },
        headers=route_headers,
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    start_payload = decode(start)
    trace.append({
        "name": "custom_payment_method_start",
        "status": int(start.status_code),
        "result": str(start_payload.get("status") or start_payload.get("result") or ""),
    })
    if start.status_code >= 400:
        raise RuntimeError(
            f"菲律宾 GCash custom payment method start failed: HTTP {start.status_code} "
            f"{opll_short_error(str(start_payload), 700)}"
        )

    action_url = opll_find_gcash_adyen_redirect_url(start_payload)
    verification_url = (
        opll_find_checkout_verification_url(start_payload)
        or opll_find_checkout_verification_url(confirm_payload)
    )
    last_payload = start_payload

    verification_url = verification_url or opll_find_checkout_verification_url({}, checkout_id)
    if verification_url and not action_url:
        try:
            action_url = opll_resolve_gcash_verification_redirect(chatgpt, verification_url)
        except Exception:
            action_url = ""
    if not action_url and not verification_url:
        raise RuntimeError(
            "菲律宾 GCash 响应未返回 Adyen action.url / verification_url: "
            + opll_short_error(str(last_payload), 800)
        )
    return {
        "action_url": action_url,
        "verification_url": verification_url,
        "custom_payment_method_type_id": gcash_method_id,
        "confirm_payload": confirm_payload,
        "last_payload": last_payload,
        "trace": trace,
    }


def generate_opll_ph_gcash_link(access_token: str, checkout_proxy_url: str,
                                promotion_proxy_url: str, progress_callback=None,
                                chatgpt_cookie: str = "",
                                processor_entity: str = "openai_llc") -> dict:
    """PH/PHP checkout -> promotion -> GCash -> Adyen redirect link."""
    checkout_proxy_url = str(checkout_proxy_url or "").strip()
    promotion_proxy_url = str(promotion_proxy_url or "").strip()
    if not checkout_proxy_url or not promotion_proxy_url:
        raise RuntimeError("菲律宾 GCash 提链必须填写 Checkout 代理和优惠代理")
    total = 4

    _emit_payment_stage(progress_callback, "gcash_checkout",
                        "GCash：创建 PH/PHP custom checkout", 1, total)
    checkout = opll_create_checkout(
        access_token,
        "PH",
        "PHP",
        checkout_proxy_url,
        checkout_ui_mode="custom",
        require_stripe_session=False,
        preferred_processor_entity=processor_entity,
        promo_campaign_id=None,
    )

    _emit_payment_stage(progress_callback, "gcash_promo",
                        "GCash：优惠代理提交 checkout/update", 2, total)
    promotion = opll_chatgpt_checkout_update_promotion(
        access_token,
        checkout,
        promotion_proxy_url,
        chatgpt_cookie=str(chatgpt_cookie or ""),
        normalize_vn=False,
        include_full_profile=False,
        include_promo=True,
        checkout_ui_mode="custom",
    )
    checkout_id = str(
        opll_extract_checkout_id(promotion)
        or checkout.get("checkout_id")
        or checkout.get("cs_id")
        or ""
    ).strip()
    entity = str(
        opll_extract_processor_entity(promotion)
        or checkout.get("processor_entity")
        or processor_entity
        or "openai_llc"
    ).strip()
    if not checkout_id:
        raise RuntimeError("菲律宾 GCash 优惠更新后缺少 checkout id")
    checkout["checkout_id"] = checkout_id
    checkout["cs_id"] = checkout_id
    checkout["processor_entity"] = entity
    checkout["_gcash_promotion_response"] = promotion

    promotion_amount, promotion_amount_source = opll_stripe_amount_info(promotion)
    promotion_amount_observed = promotion_amount_source not in {"missing_payload", "fallback_zero"}
    promotion_zero_verified = bool(
        promotion_amount_observed and opll_amount_is_zero(promotion_amount)
    )

    _emit_payment_stage(progress_callback, "gcash_action",
                        "GCash：选择钱包并提取 Adyen 跳转链", 3, total)
    action = opll_submit_gcash_action(
        access_token,
        checkout,
        checkout_proxy_url,
        chatgpt_cookie=str(chatgpt_cookie or ""),
        progress_callback=progress_callback,
    )
    action_url = str(action.get("action_url") or "").strip()
    verification_url = str(action.get("verification_url") or "").strip()
    long_url = action_url or verification_url
    if not long_url:
        raise RuntimeError("菲律宾 GCash 提链结果为空")

    expires_at, expires_raw = opll_checkout_expires_at(checkout, promotion)
    if not expires_at:
        expires_at = int(time.time()) + 1800
        expires_raw = str(expires_at)
    checkout_url = opll_chatgpt_checkout_url(checkout_id, entity)
    _emit_payment_stage(progress_callback, "gcash_done",
                        "GCash：菲律宾跳转链提取完成", 4, total)
    return {
        **{k: v for k, v in checkout.items() if k != "raw_checkout" and not k.startswith("_")},
        "checkout_id": checkout_id,
        "cs_id": checkout_id,
        "processor_entity": entity,
        "chatgpt_checkout_url": checkout_url,
        "long_url": long_url,
        "provider_redirect_url": action_url,
        "gcash_redirect_url": action_url,
        "verification_url": verification_url,
        "billing_country": "PH",
        "currency": "PHP",
        "payment_method_country": "PH",
        "payment_amount_display": (
            opll_format_minor_amount(promotion_amount, "PHP")
            if promotion_amount_observed else "优惠已提交"
        ),
        "promotion_update": promotion,
        "promotion_applied": True,
        "promotion_amount": promotion_amount if promotion_amount_observed else "",
        "promotion_amount_source": promotion_amount_source,
        "promotion_zero_verified": promotion_zero_verified,
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())),
        "local_payment": "gcash",
        "local_payment_detected": True,
        "local_payment_version": (
            "checkout-handoff-v1"
            if action.get("requires_browser_confirm")
            else "adyen-redirect-v1"
        ),
        "gcash_direct_redirect": bool(action_url),
        "gcash_requires_browser_confirm": bool(action.get("requires_browser_confirm")),
        "gcash_confirm_trace": action.get("trace") or [],
    }


def generate_opll_hosted_long_link(access_token: str, country: str, currency: str,
                                    proxy_url: str = "",
                                    required_payment: str = "",
                                    progress_callback=None) -> dict:
    """
    Generate a hosted Stripe checkout URL (no card / GoPay / Apple Pay).
    Used for modes like "无卡长链接 US/USD", "GoPay 长链接 ID/IDR", etc.
    """
    _emit_payment_stage(progress_callback, "checkout", "创建 ChatGPT checkout", 1, 3)
    checkout = opll_create_checkout(access_token, country, currency, proxy_url)
    _emit_payment_stage(progress_callback, "stripe_init", "初始化 Stripe 支付页", 2, 3)
    init_payload = opll_stripe_init(checkout["cs_id"], checkout["billing_country"],
                                     checkout["currency"], proxy_url, checkout=checkout)
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(f"stripe init response missing stripe_hosted_url, "
                           f"keys={sorted(init_payload.keys())}")
    local_payment_detected = opll_payment_method_available(init_payload, required_payment)
    if required_payment and not local_payment_detected:
        raise RuntimeError(
            f"payment page did not expose {required_payment.upper()} yet; "
            f"{opll_payment_method_diagnostics(init_payload)}; retry with another attempt/proxy"
        )
    long_url = opll_to_openai_pay_url(stripe_hosted_url) or opll_stripe_checkout_long_url(
        checkout["cs_id"], checkout["billing_country"], checkout.get("processor_entity", ""))
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    expires_at, expires_raw = opll_checkout_expires_at(checkout, init_payload)
    _emit_payment_stage(progress_callback, "done", "已生成支付长链接", 3, 3)
    return {
        **checkout,
        "stripe_hosted_url": stripe_hosted_url,
        "long_url": long_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, checkout["currency"]),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": required_payment,
        "local_payment_detected": local_payment_detected,
    }


def generate_opll_ideal_v3_long_link(access_token: str, nl_proxy_url: str = "",
                                      promo_proxy_url: str = "", progress_callback=None,
                                      chatgpt_cookie: str = "") -> dict:
    """iDEAL 3.0: NL main route + generic promotion route -> signed 0.01 EUR tx link and QR."""
    nl_proxy_url = str(nl_proxy_url or "").strip()
    promo_proxy_url = str(promo_proxy_url or "").strip()
    if not nl_proxy_url:
        raise RuntimeError("荷兰3.0提链必须填写 NL 代理池")
    if not promo_proxy_url:
        raise RuntimeError("荷兰3.0提链必须填写优惠地区代理池")
    missing_email = "__ideal3_missing_email__"
    account_email = opll_email_from_access_token_text(
        access_token, default_email=missing_email,
    )
    billing = opll_generate_nl_profile(
        "" if account_email == missing_email else account_email,
    )

    def relay_progress(event: dict) -> None:
        if not progress_callback:
            return
        payload = dict(event or {})
        label = str(payload.get("stage_label") or "")
        payload["stage_label"] = label.replace("iDEAL 2.0", "荷兰3.0").replace("VN", "优惠地区")
        progress_callback(payload)

    result = generate_opll_ideal_v2_long_link(
        access_token,
        nl_proxy_url,
        promo_proxy_url,
        relay_progress,
        chatgpt_cookie=chatgpt_cookie,
        billing_profile=billing,
    )
    stripe_amount = str(result.get("stripe_amount") or "").strip()
    try:
        amount_minor = int(float(stripe_amount))
    except Exception:
        amount_minor = -1
    if amount_minor != 1:
        raise RuntimeError(
            f"荷兰3.0金额校验失败：需要 0.01 EUR，当前 amount={stripe_amount or 'missing'}"
        )

    direct_url = ""
    for candidate in (
        result.get("provider_redirect_url"),
        result.get("long_url"),
        result.get("stripe_redirect_url"),
    ):
        direct_url = opll_extract_direct_ideal_url(str(candidate or ""))
        if direct_url:
            break
    if not direct_url:
        raise RuntimeError(
            "荷兰3.0未提取到签名 tx.ideal.nl/2/... 直链；"
            f"当前结果: {result.get('provider_redirect_url') or result.get('stripe_redirect_url') or '-'}"
        )

    qr_image_data_url = opll_make_qr_data_url(direct_url)
    if not qr_image_data_url:
        raise RuntimeError("荷兰3.0二维码生成失败")

    result.update({
        "provider_redirect_url": direct_url,
        "long_url": direct_url,
        "ideal_direct_url": direct_url,
        "ideal_qr_data": direct_url,
        "ideal_qr_image_data_url": qr_image_data_url,
        "payment_amount_display": "0.01 EUR",
        "local_payment": "ideal",
        "local_payment_version": "3.0",
        "local_payment_detected": True,
        "promotion_proxy_role": "discount_region",
        "billing_email": str(billing.get("email") or ""),
        "ideal_billing": billing,
    })
    return result


def generate_opll_ideal_v2_long_link(access_token: str, nl_proxy_url: str = "",
                                      vn_proxy_url: str = "", progress_callback=None,
                                      chatgpt_cookie: str = "",
                                      billing_profile: dict | None = None) -> dict:
    """iDEAL 2.0: NL keeps iDEAL available, VN applies the promotion, then NL finishes."""
    payment_locale = "nl"
    browser_timezone = "Europe/Amsterdam"
    nl_proxy_url = str(nl_proxy_url or "").strip()
    vn_proxy_url = opll_normalize_vn_country_proxy(str(vn_proxy_url or "").strip())
    if not nl_proxy_url:
        raise RuntimeError("iDEAL 2.0 requires NL proxy pool")
    if not vn_proxy_url:
        raise RuntimeError("iDEAL 2.0 requires VN promotion proxy pool")

    total = 10
    _emit_payment_stage(progress_callback, "checkout", "iDEAL 2.0：NL 创建 ChatGPT checkout", 1, total)
    supplied_billing = dict(billing_profile or {})
    checkout = opll_create_checkout(
        access_token, "NL", "EUR", nl_proxy_url,
        billing_profile=supplied_billing or None,
    )
    stripe_pk = opll_stripe_key_for_checkout(checkout)
    stripe = opll_build_stripe_session(nl_proxy_url)

    _emit_payment_stage(progress_callback, "stripe_init", "iDEAL 2.0：NL 初始化 Stripe", 2, total)
    init_payload = opll_stripe_init(
        checkout["cs_id"], checkout["billing_country"], checkout["currency"],
        nl_proxy_url, payment_locale=payment_locale, stripe=stripe, checkout=checkout,
        browser_timezone=browser_timezone, saved_payment_method_mode="auto",
    )
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(f"stripe init response missing stripe_hosted_url, keys={sorted(init_payload.keys())}")
    if not opll_payment_method_available(init_payload, "ideal"):
        raise RuntimeError(
            "NL bootstrap checkout does not offer iDEAL; "
            f"{opll_payment_method_diagnostics(init_payload)}; retry with another NL proxy"
        )

    _emit_payment_stage(progress_callback, "vn_promotion", "iDEAL 2.0：VN checkout/update 优惠", 3, total)
    opll_chatgpt_checkout_update_promotion(access_token, checkout, vn_proxy_url, chatgpt_cookie=chatgpt_cookie)

    _emit_payment_stage(progress_callback, "promotion_init", "iDEAL 2.0：NL 刷新 Stripe 金额", 4, total)
    init_payload = opll_stripe_init(
        checkout["cs_id"], checkout["billing_country"], checkout["currency"],
        nl_proxy_url, payment_locale=payment_locale, stripe=stripe, checkout=checkout,
        browser_timezone=browser_timezone, saved_payment_method_mode="auto",
    )
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or stripe_hosted_url).strip()
    if not opll_payment_method_available(init_payload, "ideal"):
        raise RuntimeError(
            "VN promotion refresh lost iDEAL; "
            f"{opll_payment_method_diagnostics(init_payload)}; retry with another NL proxy"
        )
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    if int(float(str(stripe_amount or "0") or 0)) > 50:
        raise RuntimeError(f"amount policy failed after VN promotion update: amount={stripe_amount}, allowed<=50")

    ctx = opll_stripe_context(init_payload, payment_locale, {
        "browser_timezone": browser_timezone,
        "saved_payment_method_mode": "auto",
    })
    ctx["currency"] = str(checkout.get("currency") or "EUR").lower()

    _emit_payment_stage(progress_callback, "chatgpt_taxes", "iDEAL 2.0：同步 ChatGPT NL 税区", 5, total)
    if supplied_billing:
        billing = supplied_billing
        opll_chatgpt_update_ideal_taxes(
            access_token,
            checkout,
            nl_proxy_url,
            email=str(billing.get("email") or ""),
            chatgpt_cookie=chatgpt_cookie,
            billing=billing,
        )
    else:
        opll_chatgpt_update_ideal_taxes(
            access_token, checkout, nl_proxy_url,
            chatgpt_cookie=chatgpt_cookie,
        )
        billing = opll_billing_for_country("NL")
        billing.update({
            "name": "Ideal User",
            "line1": "Herengracht 420",
            "city": "Amsterdam",
            "postal_code": "1016 GV",
            "state": "NL",
        })

    _emit_payment_stage(progress_callback, "stripe_tax", "7.11 BA/PM: US tax/address update", 4, total)
    opll_stripe_update_tax_region(
        stripe, checkout["cs_id"], stripe_pk, ctx, billing,
        payment_locale=payment_locale, browser_timezone=browser_timezone, saved_payment_method_mode="auto",
    )

    _emit_payment_stage(progress_callback, "tax_refresh", "iDEAL 2.0：刷新 tax init", 7, total)
    init_payload = opll_stripe_init(
        checkout["cs_id"], checkout["billing_country"], checkout["currency"],
        nl_proxy_url, payment_locale=payment_locale, stripe=stripe, checkout=checkout,
        browser_timezone=browser_timezone, saved_payment_method_mode="auto",
    )
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or stripe_hosted_url).strip()
    if not opll_payment_method_available(init_payload, "ideal"):
        raise RuntimeError(
            "iDEAL tax refresh does not offer iDEAL; "
            f"{opll_payment_method_diagnostics(init_payload)}; retry with another NL proxy"
        )
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    if int(float(str(stripe_amount or "0") or 0)) > 50:
        raise RuntimeError(f"amount policy failed after iDEAL tax sync: amount={stripe_amount}, allowed<=50")
    expires_at, expires_raw = opll_checkout_expires_at(checkout, init_payload)
    ctx = opll_stripe_context(init_payload, payment_locale, ctx)
    ctx["browser_timezone"] = browser_timezone
    ctx["saved_payment_method_mode"] = "auto"

    _emit_payment_stage(progress_callback, "ideal_method", "iDEAL 2.0：创建 iDEAL payment method", 8, total)
    pm_id = opll_stripe_create_ideal_method(stripe, checkout["cs_id"], ctx, billing, stripe_pk)

    _emit_payment_stage(progress_callback, "stripe_confirm", "iDEAL 2.0：Stripe confirm + approve", 9, total)
    confirm_payload = opll_stripe_confirm(
        stripe, checkout["cs_id"], pm_id, stripe_pk, init_payload, ctx, checkout,
        stripe_hosted_url, payment_method_type="ideal",
    )
    stripe_redirect_url = opll_redirect_url_after_confirm(
        access_token, stripe, confirm_payload, checkout["cs_id"], stripe_pk, ctx, checkout,
        proxy_url=nl_proxy_url, payment_locale=payment_locale, chatgpt_cookie=chatgpt_cookie,
    )

    _emit_payment_stage(progress_callback, "ideal_redirect", "iDEAL 2.0：提取最终 iDEAL 链接", 10, total)
    provider_url = stripe_redirect_url if opll_is_ideal_url(stripe_redirect_url) else \
        opll_resolve_external_redirect(stripe, stripe_redirect_url, preferred_hosts=("ideal.nl",))
    if not opll_is_ideal_url(provider_url):
        raise RuntimeError(f"iDEAL 2.0 未提取到荷兰跳转链；当前结果: {provider_url or stripe_redirect_url}")

    return {
        **checkout,
        "payment_method_country": "NL",
        "payment_method_id": pm_id,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": provider_url,
        "long_url": provider_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, checkout["currency"]),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "ideal",
        "local_payment_version": "2.0",
        "local_payment_detected": True,
        "billing_email": str(billing.get("email") or ""),
        "ideal_billing": billing,
    }


def generate_opll_ideal_long_link(access_token: str, entry_proxy_url: str = "",
                                  exit_proxy_url: str = "", progress_callback=None) -> dict:
    payment_locale = "nl"
    browser_timezone = "Europe/Amsterdam"
    _emit_payment_stage(progress_callback, "checkout", "JP入口创建 NL ChatGPT checkout", 1, 7)
    checkout = opll_create_checkout(access_token, "NL", "EUR", entry_proxy_url)
    _emit_payment_stage(progress_callback, "stripe_init", "NL出口初始化 Stripe iDEAL 支付页", 2, 7)
    stripe = opll_build_stripe_session(exit_proxy_url or entry_proxy_url)
    init_payload = opll_stripe_init(checkout["cs_id"], checkout["billing_country"],
                                    checkout["currency"], exit_proxy_url or entry_proxy_url,
                                    payment_locale=payment_locale,
                                    stripe=stripe, checkout=checkout,
                                    browser_timezone=browser_timezone,
                                    saved_payment_method_mode="auto")
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(f"stripe init response missing stripe_hosted_url, keys={sorted(init_payload.keys())}")
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    expires_at, expires_raw = opll_checkout_expires_at(checkout, init_payload)
    stripe_pk = opll_stripe_key_for_checkout(checkout)
    ctx = opll_stripe_context(init_payload, payment_locale, {
        "browser_timezone": browser_timezone,
        "saved_payment_method_mode": "auto",
    })
    tax_update_error = ""
    if not opll_payment_method_available(init_payload, "ideal"):
        _emit_payment_stage(progress_callback, "stripe_tax_update", "Stripe tax region -> NL", 3, 7)
        try:
            updated_payload = opll_stripe_update_tax_region(
                stripe,
                checkout["cs_id"],
                stripe_pk,
                ctx,
                opll_billing_for_country("NL"),
                payment_locale=payment_locale,
                browser_timezone=browser_timezone,
                saved_payment_method_mode="auto",
            )
            if isinstance(updated_payload, dict) and updated_payload:
                init_payload = updated_payload
                updated_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
                if updated_hosted_url:
                    stripe_hosted_url = updated_hosted_url
                stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
                expires_at, expires_raw = opll_checkout_expires_at(checkout, init_payload)
                ctx = opll_stripe_context(init_payload, payment_locale, ctx)
                ctx["browser_timezone"] = browser_timezone
                ctx["saved_payment_method_mode"] = "auto"
        except Exception as exc:
            tax_update_error = opll_short_error(str(exc), 240)
    # 不能再用旧 ZIP 的全文搜索 opll_payload_contains_word(init_payload, "ideal")。
    # Stripe localization/experiment blob 里可能出现 ideal 字样，但显式 method 列表没有 ideal；
    # 这种情况下继续 confirm 会直接 payment_method_types_mismatch。
    if not opll_payment_method_available(init_payload, "ideal"):
        tax_hint = f"; stripe_tax_update_error={tax_update_error}" if tax_update_error else ""
        raise RuntimeError(
            "payment page did not expose iDEAL yet; "
            f"{opll_payment_method_diagnostics(init_payload)}; "
            f"{opll_legacy_zip_payment_method_diagnostics(init_payload, 'ideal')}; "
            "legacy ZIP confirm skipped because explicit payment_method_types does not contain iDEAL; "
            "retry with another NL exit proxy"
            f"{tax_hint}"
        )
    if not ctx.get("currency"):
        ctx["currency"] = str(checkout.get("currency") or "").lower()
    _emit_payment_stage(progress_callback, "ideal_method", "创建 iDEAL payment method", 4, 7)
    pm_id = opll_stripe_create_ideal_method(stripe, checkout["cs_id"], ctx,
                                            opll_billing_for_country("NL"), stripe_pk)
    _emit_payment_stage(progress_callback, "stripe_confirm", "执行 Stripe confirm 获取 iDEAL 跳转", 5, 7)
    confirm_payload = opll_stripe_confirm(stripe, checkout["cs_id"], pm_id, stripe_pk,
                                          init_payload, ctx, checkout, stripe_hosted_url,
                                          payment_method_type="ideal")
    stripe_redirect_url = opll_extract_redirect_to_url(confirm_payload)
    submission = opll_find_submission_attempt(confirm_payload)
    if not stripe_redirect_url and submission.get("state") == "requires_approval":
        _emit_payment_stage(progress_callback, "chatgpt_approve", "ChatGPT approve / 入口代理授权", 6, 7)
        opll_chatgpt_approve_with_retry(access_token, checkout["cs_id"], checkout, entry_proxy_url)
        stripe_redirect_url = opll_stripe_payment_page_redirect_url(stripe, checkout["cs_id"], stripe_pk,
                                                                    payment_locale=payment_locale,
                                                                    ctx=ctx, timeout_seconds=45)
    elif not stripe_redirect_url:
        try:
            stripe_redirect_url = opll_stripe_payment_page_redirect_url(stripe, checkout["cs_id"], stripe_pk,
                                                                        payment_locale=payment_locale,
                                                                        ctx=ctx, timeout_seconds=30)
        except OpllStripeRequiresApproval:
            _emit_payment_stage(progress_callback, "chatgpt_approve", "ChatGPT approve / 入口代理授权", 6, 7)
            opll_chatgpt_approve_with_retry(access_token, checkout["cs_id"], checkout, entry_proxy_url)
            stripe_redirect_url = opll_stripe_payment_page_redirect_url(stripe, checkout["cs_id"], stripe_pk,
                                                                        payment_locale=payment_locale,
                                                                        ctx=ctx, timeout_seconds=45)
    if submission.get("state") == "failed":
        raise RuntimeError(f"stripe iDEAL submission failed: {opll_stripe_payload_diagnostics(confirm_payload, ctx)}")
    _emit_payment_stage(progress_callback, "ideal_redirect", "解析 iDEAL Provider Redirect URL", 6, 7)
    provider_url = stripe_redirect_url if opll_is_ideal_url(stripe_redirect_url) else \
        opll_resolve_external_redirect(stripe, stripe_redirect_url, preferred_hosts=("ideal.nl",))
    if not opll_is_ideal_url(provider_url):
        raise RuntimeError(f"未提取到 iDEAL 荷兰跳转链；当前结果: {provider_url or stripe_redirect_url}")
    long_url = provider_url
    _emit_payment_stage(progress_callback, "done", "已提取 iDEAL 荷兰链", 7, 7)
    return {
        **checkout,
        "payment_method_country": "NL",
        "payment_method_id": pm_id,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": provider_url,
        "long_url": long_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, checkout["currency"]),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "ideal",
        "local_payment_detected": True,
    }


def _opll_momo_amount_is_acceptable(value, max_minor_amount: int = 50) -> bool:
    text = str(value if value is not None else "").strip()
    if not text:
        return False
    try:
        amount = float(text)
    except Exception:
        return text in {"0", "0.0", "0.00"}
    return 0 <= amount <= max_minor_amount


def generate_opll_momo_long_link(access_token: str, vn_proxy_url: str = "",
                                  provider_proxy_url: str = "", progress_callback=None,
                                  chatgpt_cookie: str = "") -> dict:
    """Vietnam MoMo flow migrated from the standalone MoMo link extractor."""
    payment_locale = "vi"
    browser_timezone = "Asia/Ho_Chi_Minh"
    vn_proxy_url = str(vn_proxy_url or "").strip()
    provider_proxy_url = str(provider_proxy_url or "").strip() or vn_proxy_url
    billing = opll_generate_vn_profile()
    total = 8

    _emit_payment_stage(progress_callback, "momo_checkout",
                        "MoMo：VN 创建 ChatGPT checkout", 1, total)
    checkout = opll_create_checkout(
        access_token, "VN", "VND", vn_proxy_url,
        checkout_ui_mode="custom",
        require_stripe_session=True,
        preferred_processor_entity=opll_processor_entity_for_country("VN"),
        promo_campaign_id="plus-1-month-free",
        extra_payload={
            "price_interval": "month",
            "seat_quantity": 1,
            "subscription_data": {"trial_period_days": 30},
        },
    )
    checkout["_momo_billing_profile"] = billing

    _emit_payment_stage(progress_callback, "momo_promo",
                        "MoMo：checkout/update 应用试用与优惠", 2, total)
    promotion_payload = opll_chatgpt_checkout_update_promotion(
        access_token,
        checkout,
        provider_proxy_url,
        chatgpt_cookie=chatgpt_cookie,
        normalize_vn=True,
        include_full_profile=False,
        include_promo=True,
        checkout_ui_mode="custom",
        extra_payload={"subscription_data": {"trial_period_days": 30}},
    )

    _emit_payment_stage(progress_callback, "momo_stripe_init",
                        "MoMo：VN 初始化 Stripe 支付页", 3, total)
    stripe = opll_build_stripe_session(provider_proxy_url)
    init_payload = opll_stripe_init(
        checkout["cs_id"], "VN", "VND", provider_proxy_url,
        payment_locale=payment_locale,
        stripe=stripe,
        checkout=checkout,
        browser_timezone=browser_timezone,
        saved_payment_method_mode="never",
    )
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(
            f"MoMo stripe init response missing stripe_hosted_url, keys={sorted(init_payload.keys())}"
        )
    if not opll_payment_method_available(init_payload, "momo"):
        raise RuntimeError(
            "跳过：当前账号的 Stripe 支付页未开放 MoMo；"
            f"{opll_payment_method_diagnostics(init_payload)}"
        )

    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    if not _opll_momo_amount_is_acceptable(stripe_amount):
        raise RuntimeError(
            f"MoMo amount policy failed: amount={stripe_amount or '-'}, allowed<=50"
        )
    expires_at, expires_raw = opll_checkout_expires_at(checkout, init_payload)
    stripe_pk = opll_stripe_key_for_checkout(checkout)
    ctx = opll_stripe_context(init_payload, payment_locale, {
        "browser_timezone": browser_timezone,
        "saved_payment_method_mode": "never",
    })
    ctx["currency"] = "vnd"

    _emit_payment_stage(progress_callback, "momo_method",
                        "MoMo：创建 Stripe payment method", 4, total)
    pm_id = opll_stripe_create_momo_method(
        stripe, checkout["cs_id"], ctx, billing, stripe_pk,
    )

    _emit_payment_stage(progress_callback, "momo_confirm",
                        "MoMo：Stripe confirm", 5, total)
    confirm_payload = opll_stripe_confirm(
        stripe, checkout["cs_id"], pm_id, stripe_pk,
        init_payload, ctx, checkout, stripe_hosted_url,
        payment_method_type="momo",
    )
    submission = opll_find_submission_attempt(confirm_payload)
    if submission.get("state") == "failed":
        raise RuntimeError(
            f"MoMo stripe confirm failed: {opll_stripe_payload_diagnostics(confirm_payload, ctx)}"
        )

    _emit_payment_stage(progress_callback, "momo_approve",
                        "MoMo：OpenAI approve / Stripe 轮询", 6, total)
    stripe_redirect_url = opll_redirect_url_after_confirm(
        access_token,
        stripe,
        confirm_payload,
        checkout["cs_id"],
        stripe_pk,
        ctx,
        checkout,
        provider_proxy_url,
        payment_locale=payment_locale,
        chatgpt_cookie=chatgpt_cookie,
    )

    _emit_payment_stage(progress_callback, "momo_redirect",
                        "MoMo：解析越南钱包跳转链", 7, total)
    provider_url = opll_resolve_external_redirect(
        stripe, stripe_redirect_url, preferred_hosts=(),
    )
    long_url = provider_url or stripe_redirect_url
    if not long_url:
        raise RuntimeError(
            "MoMo 未提取到跳转链；"
            f"submission_state={submission.get('state') or '-'}"
        )

    _emit_payment_stage(progress_callback, "momo_done",
                        "MoMo：越南支付链提取完成", 8, total)
    return {
        **{k: v for k, v in checkout.items() if k != "raw_checkout" and not k.startswith("_")},
        "payment_method_country": "VN",
        "payment_method_id": pm_id,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": provider_url,
        "long_url": long_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, "VND"),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "momo",
        "local_payment_detected": True,
        "payment_locale": payment_locale,
        "browser_timezone": browser_timezone,
        "momo_billing": billing,
        "promotion_update": promotion_payload,
    }


def generate_opll_promptpay_long_link(access_token: str, th_proxy_url: str = "",
                                      promo_proxy_url: str = "", progress_callback=None,
                                      chatgpt_cookie: str = "") -> dict:
    """PromptPay: TH checkout/Stripe, promo update, TH billing, confirm/approve and QR extraction."""
    payment_locale = "th"
    browser_timezone = "Asia/Bangkok"
    th_proxy_url = str(th_proxy_url or "").strip()
    promo_proxy_url = str(promo_proxy_url or "").strip()
    if not th_proxy_url:
        raise RuntimeError("PromptPay requires TH proxy pool")
    if not promo_proxy_url:
        raise RuntimeError("PromptPay requires promo proxy pool")

    total = 9
    _emit_payment_stage(progress_callback, "promptpay_checkout",
                        "PromptPay：TH 创建 ChatGPT checkout", 1, total)
    checkout = opll_create_checkout(
        access_token, "TH", "THB", th_proxy_url,
        checkout_ui_mode="hosted", require_stripe_session=True,
        preferred_processor_entity=opll_processor_entity_for_country("TH"),
        promo_campaign_id="plus-1-month-free",
    )
    billing = opll_generate_th_profile()
    checkout["_promptpay_billing_profile"] = billing
    stripe_pk = opll_stripe_key_for_checkout(checkout)

    _emit_payment_stage(progress_callback, "promptpay_promo",
                        "PromptPay：优惠代理 checkout/update 到 0", 2, total)
    promotion_payload = opll_chatgpt_checkout_update_promotion(
        access_token, checkout, promo_proxy_url, chatgpt_cookie=chatgpt_cookie,
    )

    _emit_payment_stage(progress_callback, "promptpay_stripe_init",
                        "PromptPay：TH Stripe init", 3, total)
    stripe = opll_build_stripe_session(th_proxy_url)
    init_payload = opll_stripe_init(
        checkout["cs_id"], checkout["billing_country"], checkout["currency"],
        th_proxy_url, payment_locale=payment_locale, stripe=stripe, checkout=checkout,
        browser_timezone=browser_timezone, saved_payment_method_mode="never",
    )
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(
            f"PromptPay stripe init response missing stripe_hosted_url, keys={sorted(init_payload.keys())}"
        )
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    if stripe_amount and not _opll_amount_is_zero(stripe_amount):
        raise RuntimeError(
            f"PromptPay amount is not 0 after promo: amount={stripe_amount}, source={stripe_amount_source}"
        )

    ctx = opll_stripe_context(init_payload, payment_locale, {
        "browser_timezone": browser_timezone,
        "saved_payment_method_mode": "never",
    })
    ctx["currency"] = "thb"

    _emit_payment_stage(progress_callback, "promptpay_tax",
                        "PromptPay：同步泰国账单与税区", 4, total)
    tax_payload = opll_stripe_update_tax_region(
        stripe, checkout["cs_id"], stripe_pk, ctx, billing,
        payment_locale=payment_locale, browser_timezone=browser_timezone,
        saved_payment_method_mode="never",
    )
    if isinstance(tax_payload, dict) and tax_payload:
        init_payload = tax_payload
        stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or stripe_hosted_url).strip()
        stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
        ctx = opll_stripe_context(init_payload, payment_locale, ctx)
        ctx["browser_timezone"] = browser_timezone
        ctx["saved_payment_method_mode"] = "never"
        ctx["currency"] = "thb"
    if stripe_amount and not _opll_amount_is_zero(stripe_amount):
        raise RuntimeError(
            f"PromptPay amount changed after TH billing sync: amount={stripe_amount}, source={stripe_amount_source}"
        )
    if not opll_payment_method_available(init_payload, "promptpay"):
        raise RuntimeError(
            "PromptPay Stripe checkout did not expose PromptPay; "
            f"{opll_payment_method_diagnostics(init_payload)}; retry with another TH proxy"
        )
    payment_methods = opll_collect_payment_method_types(init_payload)
    expires_at, expires_raw = opll_checkout_expires_at(checkout, init_payload)

    _emit_payment_stage(progress_callback, "promptpay_method",
                        "PromptPay：创建 PromptPay payment method", 5, total)
    pm_id = opll_stripe_create_promptpay_method(
        stripe, checkout["cs_id"], ctx, billing, stripe_pk,
    )

    _emit_payment_stage(progress_callback, "promptpay_confirm",
                        "PromptPay：Stripe confirm", 6, total)
    confirm_payload = opll_stripe_confirm(
        stripe, checkout["cs_id"], pm_id, stripe_pk, init_payload, ctx, checkout,
        stripe_hosted_url, payment_method_type="promptpay",
    )
    promptpay = opll_extract_promptpay(confirm_payload)
    submission = opll_find_submission_attempt(confirm_payload)
    if submission.get("state") == "failed":
        raise RuntimeError(
            f"PromptPay stripe confirm failed: {opll_stripe_payload_diagnostics(confirm_payload, ctx)}"
        )

    _emit_payment_stage(progress_callback, "promptpay_approve",
                        "PromptPay：OpenAI approve / Stripe 轮询", 7, total)
    if not any(promptpay.get(key) for key in (
        "promptpay_hosted_instructions_url", "promptpay_qr_data", "promptpay_qr_image_url"
    )):
        try:
            polled = opll_stripe_payment_page_promptpay_extract(
                stripe, checkout["cs_id"], stripe_pk, payment_locale=payment_locale,
                timeout_seconds=12, ctx=ctx,
            )
            promptpay = opll_merge_promptpay_extract(promptpay, polled)
        except OpllStripeRequiresApproval:
            opll_chatgpt_approve_with_retry(
                access_token, checkout["cs_id"], checkout, th_proxy_url,
                chatgpt_cookie=chatgpt_cookie,
            )
            polled = opll_stripe_payment_page_promptpay_extract(
                stripe, checkout["cs_id"], stripe_pk, payment_locale=payment_locale,
                timeout_seconds=45, ctx=ctx,
            )
            promptpay = opll_merge_promptpay_extract(promptpay, polled)

    instructions_url = str(promptpay.get("promptpay_hosted_instructions_url") or "").strip()
    qr_data = str(promptpay.get("promptpay_qr_data") or "").strip()
    qr_image_url = str(promptpay.get("promptpay_qr_image_url") or "").strip()
    qr_image_data_url = opll_make_qr_data_url(qr_data) if qr_data else ""
    long_url = instructions_url or qr_image_url or qr_data
    if not long_url:
        raise RuntimeError(
            "PromptPay 未提取到二维码或跳转链；"
            f"submission_state={submission.get('state') or '-'}; "
            f"keys={sorted(confirm_payload.keys()) if isinstance(confirm_payload, dict) else []}"
        )
    promptpay_expires_at = int(promptpay.get("promptpay_expires_at") or expires_at or 0)

    _emit_payment_stage(progress_callback, "promptpay_redirect",
                        "PromptPay：解析泰国二维码与跳转链", 8, total)
    _emit_payment_stage(progress_callback, "promptpay_done",
                        "PromptPay：泰国 PromptPay 链提取完成", 9, total)
    return {
        **{k: v for k, v in checkout.items() if k != "raw_checkout" and not k.startswith("_")},
        "payment_method_country": "TH",
        "payment_method_id": pm_id,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": instructions_url or qr_image_url,
        "provider_redirect_url": long_url,
        "long_url": long_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, "THB"),
        "expires_at": promptpay_expires_at,
        "expires_raw": str(promptpay_expires_at or expires_raw),
        "valid_seconds": max(0, promptpay_expires_at - int(time.time())) if promptpay_expires_at else 0,
        "local_payment": "promptpay",
        "local_payment_version": "2.0",
        "local_payment_detected": True,
        "payment_methods": payment_methods,
        "payment_locale": payment_locale,
        "browser_timezone": browser_timezone,
        "billing_email": str(billing.get("email") or ""),
        "promptpay_billing": billing,
        "promptpay_link": instructions_url or long_url,
        "promptpay_hosted_instructions_url": instructions_url,
        "promptpay_qr_data": qr_data,
        "promptpay_qr_image_url": qr_image_url,
        "promptpay_qr_image_data_url": qr_image_data_url,
        "promotion_update": promotion_payload,
        "tax_update": tax_payload,
    }


def generate_opll_twint_long_link(access_token: str, ch_proxy_url: str = "",
                                  promo_proxy_url: str = "", progress_callback=None,
                                  chatgpt_cookie: str = "") -> dict:
    """TWINT: CH checkout/Stripe, promo update, CH billing, confirm and approve."""
    payment_locale = "de"
    browser_timezone = "Europe/Zurich"
    ch_proxy_url = str(ch_proxy_url or "").strip()
    promo_proxy_url = str(promo_proxy_url or "").strip()
    if not ch_proxy_url:
        raise RuntimeError("TWINT requires CH proxy pool")
    if not promo_proxy_url:
        raise RuntimeError("TWINT requires promo proxy pool")

    total = 9
    _emit_payment_stage(progress_callback, "twint_checkout",
                        "TWINT：CH 创建 ChatGPT checkout", 1, total)
    checkout = opll_create_checkout(
        access_token, "CH", "CHF", ch_proxy_url,
        checkout_ui_mode="hosted", require_stripe_session=True,
        preferred_processor_entity=opll_processor_entity_for_country("CH"),
        promo_campaign_id="plus-1-month-free",
    )
    billing = opll_generate_ch_profile()
    checkout["_twint_billing_profile"] = billing
    stripe_pk = opll_stripe_key_for_checkout(checkout)

    _emit_payment_stage(progress_callback, "twint_promo",
                        "TWINT：优惠代理 checkout/update 到 0", 2, total)
    promotion_payload = opll_chatgpt_checkout_update_promotion(
        access_token, checkout, promo_proxy_url, chatgpt_cookie=chatgpt_cookie,
    )

    _emit_payment_stage(progress_callback, "twint_stripe_init",
                        "TWINT：CH Stripe init", 3, total)
    stripe = opll_build_stripe_session(ch_proxy_url)
    init_payload = opll_stripe_init(
        checkout["cs_id"], checkout["billing_country"], checkout["currency"],
        ch_proxy_url, payment_locale=payment_locale, stripe=stripe, checkout=checkout,
        browser_timezone=browser_timezone, saved_payment_method_mode="never",
    )
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(
            f"TWINT stripe init response missing stripe_hosted_url, keys={sorted(init_payload.keys())}"
        )
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    if stripe_amount and not _opll_amount_is_zero(stripe_amount):
        raise RuntimeError(
            f"TWINT amount is not 0 after promo: amount={stripe_amount}, source={stripe_amount_source}"
        )

    ctx = opll_stripe_context(init_payload, payment_locale, {
        "browser_timezone": browser_timezone,
        "saved_payment_method_mode": "never",
    })
    ctx["currency"] = str(checkout.get("currency") or "CHF").lower()

    _emit_payment_stage(progress_callback, "twint_tax",
                        "TWINT：同步瑞士账单与税区", 4, total)
    tax_payload = opll_stripe_update_tax_region(
        stripe, checkout["cs_id"], stripe_pk, ctx, billing,
        payment_locale=payment_locale, browser_timezone=browser_timezone,
        saved_payment_method_mode="never",
    )
    if isinstance(tax_payload, dict) and tax_payload:
        init_payload = tax_payload
        stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or stripe_hosted_url).strip()
        stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
        ctx = opll_stripe_context(init_payload, payment_locale, ctx)
        ctx["browser_timezone"] = browser_timezone
        ctx["saved_payment_method_mode"] = "never"
        ctx["currency"] = str(checkout.get("currency") or "CHF").lower()
    if stripe_amount and not _opll_amount_is_zero(stripe_amount):
        raise RuntimeError(
            f"TWINT amount changed after CH billing sync: amount={stripe_amount}, source={stripe_amount_source}"
        )
    if not opll_payment_method_available(init_payload, "twint"):
        raise RuntimeError(
            "TWINT Stripe checkout did not expose TWINT; "
            f"{opll_payment_method_diagnostics(init_payload)}; retry with another CH proxy"
        )
    expires_at, expires_raw = opll_checkout_expires_at(checkout, init_payload)

    _emit_payment_stage(progress_callback, "twint_method",
                        "TWINT：创建 TWINT payment method", 5, total)
    pm_id = opll_stripe_create_twint_method(
        stripe, checkout["cs_id"], ctx, billing, stripe_pk,
    )

    _emit_payment_stage(progress_callback, "twint_confirm",
                        "TWINT：Stripe confirm", 6, total)
    confirm_payload = opll_stripe_confirm(
        stripe, checkout["cs_id"], pm_id, stripe_pk, init_payload, ctx, checkout,
        stripe_hosted_url, payment_method_type="twint",
    )
    submission = opll_find_submission_attempt(confirm_payload)
    if submission.get("state") == "failed":
        raise RuntimeError(
            f"TWINT stripe confirm failed: {opll_stripe_payload_diagnostics(confirm_payload, ctx)}"
        )

    _emit_payment_stage(progress_callback, "twint_approve",
                        "TWINT：OpenAI approve / Stripe 轮询", 7, total)
    stripe_redirect_url = opll_redirect_url_after_confirm(
        access_token, stripe, confirm_payload, checkout["cs_id"], stripe_pk, ctx,
        checkout, proxy_url=ch_proxy_url, payment_locale=payment_locale,
        chatgpt_cookie=chatgpt_cookie,
    )

    _emit_payment_stage(progress_callback, "twint_redirect",
                        "TWINT：解析瑞士 TWINT 跳转链", 8, total)
    provider_url = stripe_redirect_url if opll_is_twint_url(stripe_redirect_url) else \
        opll_resolve_external_redirect(
            stripe,
            stripe_redirect_url,
            preferred_hosts=("twint.ch", "twint.com"),
        )
    if not opll_is_twint_url(provider_url):
        raise RuntimeError(
            f"TWINT 未提取到瑞士跳转链；当前结果: {provider_url or stripe_redirect_url}"
        )

    _emit_payment_stage(progress_callback, "twint_done",
                        "TWINT：瑞士 TWINT 链提取完成", 9, total)
    return {
        **{k: v for k, v in checkout.items() if k != "raw_checkout" and not k.startswith("_")},
        "payment_method_country": "CH",
        "payment_method_id": pm_id,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": provider_url,
        "long_url": provider_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, "CHF"),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "twint",
        "local_payment_version": "2.0",
        "local_payment_detected": True,
        "payment_locale": payment_locale,
        "browser_timezone": browser_timezone,
        "billing_email": str(billing.get("email") or ""),
        "twint_billing": billing,
        "promotion_update": promotion_payload,
        "tax_update": tax_payload,
    }


def _opll_twint_v2_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or str(default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def opll_twint_v2_new_session(proxy_url: str = ""):
    session = opll_new_http_session(force_requests=opll_is_local_proxy_url(proxy_url))
    session.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "de-CH,de;q=0.9,fr;q=0.8,it;q=0.7,en;q=0.6",
    })
    if proxy_url:
        if hasattr(session, "trust_env"):
            session.trust_env = False
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session


def opll_twint_v2_chatgpt_headers(access_token: str, referer: str = "https://chatgpt.com/",
                                   target_path: str = "", chatgpt_cookie: str = "") -> dict:
    token = parse_session_json(access_token) or str(access_token or "").strip()
    cookie = opll_normalize_chatgpt_cookie(chatgpt_cookie)
    device_id = opll_cookie_value(cookie, "oai-did") or str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"twint-v2-device:{token}")
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "oai-language": "de-CH",
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": referer,
        "oai-device-id": device_id,
        "Cookie": cookie or f"oai-did={device_id}",
    }
    if target_path:
        headers["x-openai-target-path"] = target_path
        headers["x-openai-target-route"] = target_path
    return headers


def opll_twint_v2_stripe_headers(publishable_key: str, referer: str) -> dict:
    origin = "https://checkout.stripe.com" if "checkout.stripe.com" in referer else "https://pay.openai.com"
    return {
        "Authorization": f"Bearer {publishable_key}",
        "Origin": origin,
        "Referer": referer,
        "Accept": "application/json",
        "Accept-Language": "de-CH,de;q=0.9,fr;q=0.8,it;q=0.7,en;q=0.6",
        "Sec-Fetch-Site": "same-site" if origin == "https://checkout.stripe.com" else "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": DEFAULT_USER_AGENT,
    }


def opll_twint_v2_elements_params(stripe_js_id: str, session_id: str = "") -> dict:
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": "de",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
    }
    if session_id:
        params["elements_session_client[session_id]"] = session_id
    return params


def opll_twint_v2_expected_amount(payload: dict) -> str:
    options = payload.get("elements_options") if isinstance(payload.get("elements_options"), dict) else {}
    if options.get("amount") is not None:
        return str(int(options["amount"]))
    total_summary = payload.get("total_summary") if isinstance(payload.get("total_summary"), dict) else {}
    if total_summary.get("due") is not None:
        return str(int(total_summary["due"]))
    invoice = payload.get("invoice") if isinstance(payload.get("invoice"), dict) else {}
    for name in ("amount_due", "total"):
        if invoice.get(name) is not None:
            return str(int(invoice[name]))
    line_items = payload.get("line_items")
    if isinstance(line_items, list):
        amounts = [
            item.get("amount") for item in line_items
            if isinstance(item, dict) and item.get("amount") is not None
        ]
        if amounts:
            return str(sum(int(value) for value in amounts))
    return "unknown"


def opll_generate_twint_v2_billing(access_token: str) -> dict:
    """Create one Swiss billing fixture and keep it sticky for the entire attempt."""
    _ = access_token
    billing = dict(opll_generate_ch_profile())
    billing["country"] = "CH"
    return billing


def opll_twint_v2_stripe_init(session, checkout_id: str, publishable_key: str,
                               checkout_page: str) -> tuple[dict, str]:
    stripe_js_id = str(uuid.uuid4())
    body = {
        "key": publishable_key,
        "eid": "NA",
        "browser_locale": "de-CH",
        "browser_timezone": "Europe/Zurich",
        "redirect_type": "url",
        "_stripe_version": STRIPE_VERSION_FULL,
        **opll_twint_v2_elements_params(stripe_js_id),
    }
    response = session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/init",
        data=body,
        headers=opll_twint_v2_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"TWINT 2.0 stripe init failed: HTTP {response.status_code} {response.text[:800]}")
    payload = response.json() or {}
    if not isinstance(payload, dict):
        raise RuntimeError("TWINT 2.0 stripe init returned invalid payload")
    return payload, stripe_js_id


def opll_twint_v2_assert_init(payload: dict, stage: str, require_zero: bool) -> str:
    amount = opll_twint_v2_expected_amount(payload)
    currency = str(payload.get("currency") or "").lower()
    methods = [str(item).lower() for item in (payload.get("payment_method_types") or [])]
    if "twint" not in methods or (require_zero and (amount != "0" or currency != "chf")):
        raise RuntimeError(
            f"TWINT 2.0 checkout_not_twint_trial: stage={stage} "
            f"amount={amount} currency={currency} methods={methods}"
        )
    return amount


def generate_opll_twint_v2_long_link(access_token: str, ch_proxy_url: str = "",
                                         promo_proxy_url: str = "", progress_callback=None,
                                         chatgpt_cookie: str = "") -> dict:
    """TWINT 2.0: sticky CH -> promo -> CH custom checkout flow based on the Kakao 3.0 state machine."""
    token = parse_session_json(access_token) or str(access_token or "").strip()
    ch_proxy_url = str(ch_proxy_url or "").strip()
    promo_proxy_url = str(promo_proxy_url or "").strip()
    if not ch_proxy_url:
        raise RuntimeError("TWINT 2.0 requires CH proxy pool")
    if not promo_proxy_url:
        raise RuntimeError("TWINT 2.0 requires promo proxy pool")

    total = 12
    checkout_session = opll_twint_v2_new_session(ch_proxy_url)
    promotion_session = opll_twint_v2_new_session(promo_proxy_url)
    provider_session = opll_twint_v2_new_session(ch_proxy_url)

    _emit_payment_stage(progress_callback, "twint2_auth", "TWINT 2.0：校验 ChatGPT Token", 1, total)
    me_response = checkout_session.get(
        "https://chatgpt.com/backend-api/me",
        headers=opll_twint_v2_chatgpt_headers(token, chatgpt_cookie=chatgpt_cookie),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if me_response.status_code != 200:
        raise RuntimeError(
            f"TWINT 2.0 ChatGPT /me failed: HTTP {me_response.status_code} {me_response.text[:500]}"
        )

    _emit_payment_stage(progress_callback, "twint2_checkout",
                        "TWINT 2.0：CH 创建 custom TWINT trial checkout", 2, total)
    checkout_body = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": "CH", "currency": "CHF"},
        "cancel_url": "https://chatgpt.com/#pricing",
        "checkout_ui_mode": "custom",
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    checkout_response = checkout_session.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=checkout_body,
        headers=opll_twint_v2_chatgpt_headers(token, chatgpt_cookie=chatgpt_cookie),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if checkout_response.status_code != 200:
        raise RuntimeError(
            f"TWINT 2.0 checkout failed: HTTP {checkout_response.status_code} {checkout_response.text[:800]}"
        )
    raw_checkout = checkout_response.json() or {}
    checkout_id = opll_extract_checkout_id(raw_checkout)
    publishable_key = opll_extract_stripe_publishable_key(raw_checkout)
    if not checkout_id or not publishable_key:
        raise RuntimeError(f"TWINT 2.0 checkout missing cs/pk: {list(raw_checkout.keys())}")
    processor_entity = opll_extract_processor_entity(raw_checkout) or "openai_llc"
    checkout = {
        "cs_id": checkout_id,
        "checkout_id": checkout_id,
        "processor_entity": processor_entity,
        "stripe_publishable_key": publishable_key,
        "billing_country": "CH",
        "currency": "CHF",
        "checkout_ui_mode": "custom",
        "raw_checkout": raw_checkout,
    }
    checkout_page = f"https://checkout.stripe.com/c/pay/{checkout_id}"
    page_headers = {
        "User-Agent": opll_twint_v2_chatgpt_headers(token)["User-Agent"],
        "Accept": "text/html,*/*",
        "Accept-Language": "de-CH,de;q=0.9,fr;q=0.8,it;q=0.7,en;q=0.6",
        "Referer": "https://chatgpt.com/",
    }
    for page_url in (f"https://pay.openai.com/c/pay/{checkout_id}", checkout_page):
        checkout_session.get(page_url, headers=page_headers, timeout=PAY_LONG_LINK_TIMEOUT)

    _emit_payment_stage(progress_callback, "twint2_bootstrap",
                        "TWINT 2.0：CH Bootstrap Stripe init", 3, total)
    bootstrap_payload, _ = opll_twint_v2_stripe_init(
        checkout_session, checkout_id, publishable_key, checkout_page,
    )
    opll_twint_v2_assert_init(bootstrap_payload, "CH Bootstrap", require_zero=False)

    _emit_payment_stage(progress_callback, "twint2_promo",
                        "TWINT 2.0：优惠代理 checkout/update 到 0", 4, total)
    update_path = "/backend-api/payments/checkout/update"
    update_body = {
        "checkout_session_id": checkout_id,
        "processor_entity": processor_entity,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    promotion_response = promotion_session.post(
        f"https://chatgpt.com{update_path}",
        json=update_body,
        headers=opll_twint_v2_chatgpt_headers(
            token,
            referer=f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}",
            target_path=update_path,
            chatgpt_cookie=chatgpt_cookie,
        ),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if promotion_response.status_code >= 400:
        raise RuntimeError(
            f"TWINT 2.0 checkout/update failed: HTTP {promotion_response.status_code} "
            f"{promotion_response.text[:800]}"
        )
    try:
        promotion_payload = promotion_response.json() or {}
    except Exception:
        promotion_payload = {"raw": promotion_response.text[:500]}
    if isinstance(promotion_payload, dict) and promotion_payload.get("success") is False:
        raise RuntimeError(f"TWINT 2.0 checkout/update rejected: {promotion_payload}")

    _emit_payment_stage(progress_callback, "twint2_refresh",
                        "TWINT 2.0：优惠更新后回到 CH 刷新 Stripe", 5, total)
    init_payload, stripe_js_id = opll_twint_v2_stripe_init(
        provider_session, checkout_id, publishable_key, checkout_page,
    )
    amount = opll_twint_v2_assert_init(init_payload, "promo 后 CH", require_zero=True)
    billing = opll_generate_twint_v2_billing(token)

    _emit_payment_stage(progress_callback, "twint2_taxes",
                        "TWINT 2.0：同步瑞士 checkout/taxes 与 Stripe tax_region", 6, total)
    taxes_path = "/backend-api/payments/checkout/taxes"
    taxes_body = {
        "checkout_session_id": checkout_id,
        "checkout_email": billing["email"],
        "billing_country": "CH",
        "billing_name": billing["name"],
        "currency": "CHF",
        "tax_id": None,
        "processor_entity": processor_entity,
        "billing_address": {
            "line1": billing["line1"],
            "city": billing["city"],
            "country": "CH",
            "postal_code": billing["postal_code"],
            "state": billing["state"],
        },
    }
    taxes_response = provider_session.post(
        f"https://chatgpt.com{taxes_path}",
        json=taxes_body,
        headers=opll_twint_v2_chatgpt_headers(
            token,
            referer=f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}",
            target_path=taxes_path,
            chatgpt_cookie=chatgpt_cookie,
        ),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if taxes_response.status_code >= 400:
        raise RuntimeError(
            f"TWINT 2.0 checkout/taxes failed: HTTP {taxes_response.status_code} {taxes_response.text[:800]}"
        )
    try:
        checkout_tax_payload = taxes_response.json() or {}
    except Exception:
        checkout_tax_payload = {"raw": taxes_response.text[:500]}

    tax_elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
    tax_body = {
        "key": publishable_key,
        "_stripe_version": STRIPE_VERSION_FULL,
        **opll_twint_v2_elements_params(stripe_js_id, tax_elements_session_id),
        "tax_region[country]": billing["country"],
        "tax_region[postal_code]": billing["postal_code"],
        "tax_region[line1]": billing["line1"],
        "tax_region[city]": billing["city"],
        "tax_region[state]": billing["state"],
    }
    tax_response = provider_session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}",
        data=tax_body,
        headers=opll_twint_v2_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if tax_response.status_code >= 400:
        raise RuntimeError(
            f"TWINT 2.0 Stripe tax_region failed: HTTP {tax_response.status_code} {tax_response.text[:800]}"
        )
    try:
        stripe_tax_payload = tax_response.json() or {}
    except Exception:
        stripe_tax_payload = {"raw": tax_response.text[:500]}

    _emit_payment_stage(progress_callback, "twint2_tax_refresh",
                        "TWINT 2.0：瑞士税务同步后刷新 Stripe", 7, total)
    init_payload, stripe_js_id = opll_twint_v2_stripe_init(
        provider_session, checkout_id, publishable_key, checkout_page,
    )
    amount = opll_twint_v2_assert_init(init_payload, "CH 税务同步", require_zero=True)
    elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"

    _emit_payment_stage(progress_callback, "twint2_pre_confirm",
                        "TWINT 2.0：Stripe pre_confirm TWINT Pay", 8, total)
    pre_confirm_response = provider_session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/pre_confirm",
        data={
            "eid": str(uuid.uuid4()),
            "payment_method_type": "twint",
            "key": publishable_key,
            "_stripe_version": STRIPE_VERSION_FULL,
        },
        headers=opll_twint_v2_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if pre_confirm_response.status_code != 200:
        raise RuntimeError(
            f"TWINT 2.0 pre_confirm failed: HTTP {pre_confirm_response.status_code} "
            f"{pre_confirm_response.text[:800]}"
        )

    _emit_payment_stage(progress_callback, "twint2_method",
                        "TWINT 2.0：创建 TWINT Pay payment method", 9, total)
    stripe_runtime = "c00af4ce81"
    client_session_id = str(uuid.uuid4())
    guid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    muid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    sid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    payment_method_body = {
        "type": "twint",
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[phone]": str(billing.get("phone") or ""),
        "billing_details[address][country]": "CH",
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][line2]": billing["line2"],
        "billing_details[address][city]": billing["city"],
        "billing_details[address][postal_code]": billing["postal_code"],
        "billing_details[address][state]": billing["state"],
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "_stripe_version": STRIPE_VERSION_FULL,
        "key": publishable_key,
        "payment_user_agent": (
            f"stripe.js/{stripe_runtime}; stripe-js-v3/{stripe_runtime}; checkout"
        ),
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
    }
    config_id = str(init_payload.get("config_id") or "")
    if config_id:
        payment_method_body["client_attribution_metadata[checkout_config_id]"] = config_id
    payment_method_response = provider_session.post(
        "https://api.stripe.com/v1/payment_methods",
        data=payment_method_body,
        headers=opll_twint_v2_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if payment_method_response.status_code != 200:
        raise RuntimeError(
            f"TWINT 2.0 payment method failed: HTTP {payment_method_response.status_code} "
            f"{payment_method_response.text[:1000]}"
        )
    payment_method_id = str((payment_method_response.json() or {}).get("id") or "")
    if not payment_method_id.startswith("pm_"):
        raise RuntimeError(f"TWINT 2.0 payment method missing id: {payment_method_response.text[:500]}")

    _emit_payment_stage(progress_callback, "twint2_confirm",
                        "TWINT 2.0：Stripe custom confirm", 10, total)
    success_url = (
        f"https://chatgpt.com/backend-api/payments/checkout/{processor_entity}/{checkout_id}/success?"
        "billing_country=CH"
    )
    return_url = (
        f"https://checkout.stripe.com/c/pay/{checkout_id}?returned_from_redirect=true&ui_mode=custom&"
        f"return_url={quote(success_url, safe='')}"
    )
    confirm_body = {
        "eid": "NA",
        "payment_method": payment_method_id,
        "expected_amount": amount,
        "tax_id_collection[purchasing_as_business]": "false",
        "expected_payment_method_type": "twint",
        "return_url": return_url,
        "_stripe_version": STRIPE_VERSION_FULL,
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": publishable_key,
        "version": stripe_runtime,
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "link_brand": "link",
        **opll_twint_v2_elements_params(stripe_js_id, elements_session_id),
    }
    if config_id:
        confirm_body["client_attribution_metadata[checkout_config_id]"] = config_id
    confirm_response = provider_session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/confirm",
        data=confirm_body,
        headers=opll_twint_v2_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if confirm_response.status_code != 200:
        raise RuntimeError(
            f"TWINT 2.0 confirm failed: HTTP {confirm_response.status_code} {confirm_response.text[:1000]}"
        )
    confirm_payload = confirm_response.json() or {}
    stripe_redirect_url = opll_extract_redirect_to_url(confirm_payload)
    submission = opll_find_submission_attempt(confirm_payload)

    _emit_payment_stage(progress_callback, "twint2_approve_poll",
                        "TWINT 2.0：OpenAI approve 并轮询 Stripe redirect", 11, total)
    requires_manual_approval = bool(
        submission.get("state") == "requires_approval"
        or (isinstance(raw_checkout, dict) and raw_checkout.get("requires_manual_approval"))
    )
    if not stripe_redirect_url and requires_manual_approval:
        approve_retry_max = _opll_twint_v2_env_int("TWINT_V2_APPROVE_RETRY_MAX", 1, 1, 10)
        last_approve_error = ""
        approve_path = "/backend-api/payments/checkout/approve"
        for retry_index in range(1, approve_retry_max + 1):
            approve_response = provider_session.post(
                f"https://chatgpt.com{approve_path}",
                json={
                    "checkout_session_id": checkout_id,
                    "processor_entity": processor_entity,
                },
                headers=opll_twint_v2_chatgpt_headers(
                    token,
                    referer=f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}",
                    target_path=approve_path,
                    chatgpt_cookie=chatgpt_cookie,
                ),
                timeout=PAY_LONG_LINK_TIMEOUT,
            )
            approved = False
            if approve_response.status_code == 200:
                try:
                    approved = (approve_response.json() or {}).get("result") == "approved"
                except Exception:
                    approved = False
            if approved:
                last_approve_error = ""
                break
            last_approve_error = (
                f"TWINT 2.0 approve failed: HTTP {approve_response.status_code} "
                f"{approve_response.text[:500]}"
            )
            if retry_index < approve_retry_max:
                time.sleep(1)
        if last_approve_error:
            raise RuntimeError(last_approve_error)

    poll_timeout = _opll_twint_v2_env_int("TWINT_V2_POLL_TIMEOUT", 120, 30, 300)
    poll_params = {
        "key": publishable_key,
        **opll_twint_v2_elements_params(stripe_js_id, elements_session_id),
    }
    deadline = time.time() + poll_timeout
    while not stripe_redirect_url and time.time() < deadline:
        poll_response = provider_session.get(
            f"https://api.stripe.com/v1/payment_pages/{checkout_id}",
            params=poll_params,
            headers=opll_twint_v2_stripe_headers(publishable_key, checkout_page),
            timeout=8,
        )
        if poll_response.status_code == 200:
            stripe_redirect_url = opll_extract_redirect_to_url(poll_response.json() or {})
        if not stripe_redirect_url:
            time.sleep(1)
    if not stripe_redirect_url:
        raise RuntimeError("TWINT 2.0 redirect url timeout")

    provider_url = stripe_redirect_url
    for _ in range(6):
        if opll_is_twint_url(provider_url):
            break
        redirect_response = provider_session.get(
            provider_url,
            allow_redirects=False,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
        location = str(redirect_response.headers.get("Location") or "")
        if redirect_response.status_code not in {301, 302, 303, 307, 308} or not location:
            break
        provider_url = urljoin(provider_url, location)
    if not opll_is_twint_url(provider_url):
        raise RuntimeError(
            f"TWINT 2.0 未提取到 TWINT 跳转链；当前结果: "
            f"{provider_url or stripe_redirect_url}"
        )

    _emit_payment_stage(progress_callback, "twint2_done",
                        "TWINT 2.0：瑞士 TWINT 链提取完成", 12, total)
    expires_at, expires_raw = opll_checkout_expires_at(raw_checkout, init_payload)
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    return {
        **{k: v for k, v in checkout.items() if k != "raw_checkout" and not k.startswith("_")},
        "payment_method_country": "CH",
        "payment_method_id": payment_method_id,
        "stripe_hosted_url": str(init_payload.get("stripe_hosted_url") or checkout_page),
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": provider_url,
        "long_url": provider_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, "CHF"),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "twint",
        "local_payment_version": "2.0",
        "local_payment_flow": "custom_exact",
        "local_payment_detected": True,
        "payment_locale": "de",
        "browser_timezone": "Europe/Zurich",
        "billing_email": billing["email"],
        "twint_billing": billing,
        "promotion_update": promotion_payload,
        "checkout_tax_update": checkout_tax_payload,
        "stripe_tax_update": stripe_tax_payload,
        "bootstrap_init": {
            "amount": opll_twint_v2_expected_amount(bootstrap_payload),
            "currency": str(bootstrap_payload.get("currency") or ""),
            "payment_method_types": bootstrap_payload.get("payment_method_types") or [],
        },
    }


def _opll_kakao_v3_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or str(default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def opll_kakao_v3_new_session(proxy_url: str = ""):
    session = opll_new_http_session(force_requests=opll_is_local_proxy_url(proxy_url))
    session.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    })
    if proxy_url:
        if hasattr(session, "trust_env"):
            session.trust_env = False
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session


def opll_kakao_v3_chatgpt_headers(access_token: str, referer: str = "https://chatgpt.com/",
                                   target_path: str = "", chatgpt_cookie: str = "") -> dict:
    token = parse_session_json(access_token) or str(access_token or "").strip()
    cookie = opll_normalize_chatgpt_cookie(chatgpt_cookie)
    device_id = opll_cookie_value(cookie, "oai-did") or str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"kakao-v3-device:{token}")
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "oai-language": "ko-KR",
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": referer,
        "oai-device-id": device_id,
        "Cookie": cookie or f"oai-did={device_id}",
    }
    if target_path:
        headers["x-openai-target-path"] = target_path
        headers["x-openai-target-route"] = target_path
    return headers


def opll_kakao_v3_stripe_headers(publishable_key: str, referer: str) -> dict:
    origin = "https://checkout.stripe.com" if "checkout.stripe.com" in referer else "https://pay.openai.com"
    return {
        "Authorization": f"Bearer {publishable_key}",
        "Origin": origin,
        "Referer": referer,
        "Accept": "application/json",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Sec-Fetch-Site": "same-site" if origin == "https://checkout.stripe.com" else "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": DEFAULT_USER_AGENT,
    }


def opll_kakao_v3_elements_params(stripe_js_id: str, session_id: str = "") -> dict:
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": "ko",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
    }
    if session_id:
        params["elements_session_client[session_id]"] = session_id
    return params


def opll_kakao_v3_expected_amount(payload: dict) -> str:
    options = payload.get("elements_options") if isinstance(payload.get("elements_options"), dict) else {}
    if options.get("amount") is not None:
        return str(int(options["amount"]))
    total_summary = payload.get("total_summary") if isinstance(payload.get("total_summary"), dict) else {}
    if total_summary.get("due") is not None:
        return str(int(total_summary["due"]))
    invoice = payload.get("invoice") if isinstance(payload.get("invoice"), dict) else {}
    for name in ("amount_due", "total"):
        if invoice.get(name) is not None:
            return str(int(invoice[name]))
    line_items = payload.get("line_items")
    if isinstance(line_items, list):
        amounts = [
            item.get("amount") for item in line_items
            if isinstance(item, dict) and item.get("amount") is not None
        ]
        if amounts:
            return str(sum(int(value) for value in amounts))
    return "unknown"


def opll_generate_kakao_v3_billing(access_token: str) -> dict:
    family_names = (
        "김", "이", "박", "최", "정", "강", "조", "윤",
        "장", "임", "한", "오", "서", "신", "권", "황",
    )
    given_names = (
        "민준", "서준", "도윤", "예준", "시우", "주원", "하준", "지호", "지후", "준서",
        "서연", "서윤", "지우", "서현", "하은", "하윤", "민서", "지유", "윤서", "채원",
    )
    addresses = (
        {"district": "강남구", "road": "테헤란로", "postal": "06164", "base": 87, "span": 40},
        {"district": "강남구", "road": "봉은사로", "postal": "06097", "base": 524, "span": 32},
        {"district": "서초구", "road": "서초대로", "postal": "06611", "base": 396, "span": 36},
        {"district": "송파구", "road": "올림픽로", "postal": "05510", "base": 300, "span": 36},
        {"district": "마포구", "road": "월드컵북로", "postal": "03925", "base": 396, "span": 36},
    )
    seed = hashlib.sha256(f"{access_token}:{uuid.uuid4()}".encode()).digest()
    rng = random.Random(seed)
    address = rng.choice(addresses)
    name = f"{rng.choice(family_names)}{rng.choice(given_names)}"
    local_name = hashlib.sha256(name.encode()).hexdigest()[:10]
    return {
        "name": name,
        "email": f"{local_name}@{rng.choice(('gmail.com', 'naver.com', 'daum.net', 'kakao.com'))}",
        "line1": f"{address['road']} {address['base'] + rng.randrange(address['span'])}",
        "line2": "",
        "city": "서울특별시",
        "state": str(address["district"]),
        "postal_code": str(address["postal"]),
        "country": "KR",
    }


def opll_kakao_v3_stripe_init(session, checkout_id: str, publishable_key: str,
                               checkout_page: str) -> tuple[dict, str]:
    stripe_js_id = str(uuid.uuid4())
    body = {
        "key": publishable_key,
        "eid": "NA",
        "browser_locale": "ko-KR",
        "browser_timezone": "Asia/Seoul",
        "redirect_type": "url",
        "_stripe_version": STRIPE_VERSION_FULL,
        **opll_kakao_v3_elements_params(stripe_js_id),
    }
    response = session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/init",
        data=body,
        headers=opll_kakao_v3_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"KAKAO 3.0 stripe init failed: HTTP {response.status_code} {response.text[:800]}")
    payload = response.json() or {}
    if not isinstance(payload, dict):
        raise RuntimeError("KAKAO 3.0 stripe init returned invalid payload")
    return payload, stripe_js_id


def opll_kakao_v3_assert_init(payload: dict, stage: str, require_zero: bool) -> str:
    amount = opll_kakao_v3_expected_amount(payload)
    currency = str(payload.get("currency") or "").lower()
    methods = [str(item).lower() for item in (payload.get("payment_method_types") or [])]
    if "kakao_pay" not in methods or (require_zero and (amount != "0" or currency != "krw")):
        raise RuntimeError(
            f"KAKAO 3.0 checkout_not_kakao_trial: stage={stage} "
            f"amount={amount} currency={currency} methods={methods}"
        )
    return amount


def generate_opll_kakao_pay_v3_long_link(access_token: str, kr_proxy_url: str = "",
                                         promo_proxy_url: str = "", progress_callback=None,
                                         chatgpt_cookie: str = "") -> dict:
    """KAKAO 3.0: port of kakao_extract.py's sticky KR -> promo -> KR custom flow."""
    token = parse_session_json(access_token) or str(access_token or "").strip()
    kr_proxy_url = str(kr_proxy_url or "").strip()
    promo_proxy_url = str(promo_proxy_url or "").strip()
    if not kr_proxy_url:
        raise RuntimeError("KAKAO 3.0 requires KR proxy pool")
    if not promo_proxy_url:
        raise RuntimeError("KAKAO 3.0 requires promo proxy pool")

    total = 12
    checkout_session = opll_kakao_v3_new_session(kr_proxy_url)
    promotion_session = opll_kakao_v3_new_session(promo_proxy_url)
    provider_session = opll_kakao_v3_new_session(kr_proxy_url)

    _emit_payment_stage(progress_callback, "kakao3_auth", "KAKAO 3.0：校验 ChatGPT Token", 1, total)
    me_response = checkout_session.get(
        "https://chatgpt.com/backend-api/me",
        headers=opll_kakao_v3_chatgpt_headers(token, chatgpt_cookie=chatgpt_cookie),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if me_response.status_code != 200:
        raise RuntimeError(
            f"KAKAO 3.0 ChatGPT /me failed: HTTP {me_response.status_code} {me_response.text[:500]}"
        )

    _emit_payment_stage(progress_callback, "kakao3_checkout",
                        "KAKAO 3.0：KR 创建 custom Kakao trial checkout", 2, total)
    checkout_body = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": "KR", "currency": "KRW"},
        "cancel_url": "https://chatgpt.com/#pricing",
        "checkout_ui_mode": "custom",
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    checkout_response = checkout_session.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=checkout_body,
        headers=opll_kakao_v3_chatgpt_headers(token, chatgpt_cookie=chatgpt_cookie),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if checkout_response.status_code != 200:
        raise RuntimeError(
            f"KAKAO 3.0 checkout failed: HTTP {checkout_response.status_code} {checkout_response.text[:800]}"
        )
    raw_checkout = checkout_response.json() or {}
    checkout_id = opll_extract_checkout_id(raw_checkout)
    publishable_key = opll_extract_stripe_publishable_key(raw_checkout)
    if not checkout_id or not publishable_key:
        raise RuntimeError(f"KAKAO 3.0 checkout missing cs/pk: {list(raw_checkout.keys())}")
    processor_entity = opll_extract_processor_entity(raw_checkout) or "openai_llc"
    checkout = {
        "cs_id": checkout_id,
        "checkout_id": checkout_id,
        "processor_entity": processor_entity,
        "stripe_publishable_key": publishable_key,
        "billing_country": "KR",
        "currency": "KRW",
        "checkout_ui_mode": "custom",
        "raw_checkout": raw_checkout,
    }
    checkout_page = f"https://checkout.stripe.com/c/pay/{checkout_id}"
    page_headers = {
        "User-Agent": opll_kakao_v3_chatgpt_headers(token)["User-Agent"],
        "Accept": "text/html,*/*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Referer": "https://chatgpt.com/",
    }
    for page_url in (f"https://pay.openai.com/c/pay/{checkout_id}", checkout_page):
        checkout_session.get(page_url, headers=page_headers, timeout=PAY_LONG_LINK_TIMEOUT)

    _emit_payment_stage(progress_callback, "kakao3_bootstrap",
                        "KAKAO 3.0：KR Bootstrap Stripe init", 3, total)
    bootstrap_payload, _ = opll_kakao_v3_stripe_init(
        checkout_session, checkout_id, publishable_key, checkout_page,
    )
    opll_kakao_v3_assert_init(bootstrap_payload, "KR Bootstrap", require_zero=False)

    _emit_payment_stage(progress_callback, "kakao3_promo",
                        "KAKAO 3.0：优惠代理 checkout/update 到 0", 4, total)
    update_path = "/backend-api/payments/checkout/update"
    update_body = {
        "checkout_session_id": checkout_id,
        "processor_entity": processor_entity,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    promotion_response = promotion_session.post(
        f"https://chatgpt.com{update_path}",
        json=update_body,
        headers=opll_kakao_v3_chatgpt_headers(
            token,
            referer=f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}",
            target_path=update_path,
            chatgpt_cookie=chatgpt_cookie,
        ),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if promotion_response.status_code >= 400:
        raise RuntimeError(
            f"KAKAO 3.0 checkout/update failed: HTTP {promotion_response.status_code} "
            f"{promotion_response.text[:800]}"
        )
    try:
        promotion_payload = promotion_response.json() or {}
    except Exception:
        promotion_payload = {"raw": promotion_response.text[:500]}
    if isinstance(promotion_payload, dict) and promotion_payload.get("success") is False:
        raise RuntimeError(f"KAKAO 3.0 checkout/update rejected: {promotion_payload}")

    _emit_payment_stage(progress_callback, "kakao3_refresh",
                        "KAKAO 3.0：优惠更新后回到 KR 刷新 Stripe", 5, total)
    init_payload, stripe_js_id = opll_kakao_v3_stripe_init(
        provider_session, checkout_id, publishable_key, checkout_page,
    )
    amount = opll_kakao_v3_assert_init(init_payload, "promo 后 KR", require_zero=True)
    billing = opll_generate_kakao_v3_billing(token)

    _emit_payment_stage(progress_callback, "kakao3_taxes",
                        "KAKAO 3.0：同步韩国 checkout/taxes 与 Stripe tax_region", 6, total)
    taxes_path = "/backend-api/payments/checkout/taxes"
    taxes_body = {
        "checkout_session_id": checkout_id,
        "checkout_email": billing["email"],
        "billing_country": "KR",
        "billing_name": billing["name"],
        "currency": "KRW",
        "tax_id": None,
        "processor_entity": processor_entity,
        "billing_address": {
            "line1": billing["line1"],
            "city": billing["city"],
            "country": "KR",
            "postal_code": billing["postal_code"],
            "state": billing["state"],
        },
    }
    taxes_response = provider_session.post(
        f"https://chatgpt.com{taxes_path}",
        json=taxes_body,
        headers=opll_kakao_v3_chatgpt_headers(
            token,
            referer=f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}",
            target_path=taxes_path,
            chatgpt_cookie=chatgpt_cookie,
        ),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if taxes_response.status_code >= 400:
        raise RuntimeError(
            f"KAKAO 3.0 checkout/taxes failed: HTTP {taxes_response.status_code} {taxes_response.text[:800]}"
        )
    try:
        checkout_tax_payload = taxes_response.json() or {}
    except Exception:
        checkout_tax_payload = {"raw": taxes_response.text[:500]}

    tax_elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
    tax_body = {
        "key": publishable_key,
        "_stripe_version": STRIPE_VERSION_FULL,
        **opll_kakao_v3_elements_params(stripe_js_id, tax_elements_session_id),
        "tax_region[country]": billing["country"],
        "tax_region[postal_code]": billing["postal_code"],
        "tax_region[line1]": billing["line1"],
        "tax_region[city]": billing["city"],
        "tax_region[state]": billing["state"],
    }
    tax_response = provider_session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}",
        data=tax_body,
        headers=opll_kakao_v3_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if tax_response.status_code >= 400:
        raise RuntimeError(
            f"KAKAO 3.0 Stripe tax_region failed: HTTP {tax_response.status_code} {tax_response.text[:800]}"
        )
    try:
        stripe_tax_payload = tax_response.json() or {}
    except Exception:
        stripe_tax_payload = {"raw": tax_response.text[:500]}

    _emit_payment_stage(progress_callback, "kakao3_tax_refresh",
                        "KAKAO 3.0：韩国税务同步后刷新 Stripe", 7, total)
    init_payload, stripe_js_id = opll_kakao_v3_stripe_init(
        provider_session, checkout_id, publishable_key, checkout_page,
    )
    amount = opll_kakao_v3_assert_init(init_payload, "KR 税务同步", require_zero=True)
    elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"

    _emit_payment_stage(progress_callback, "kakao3_pre_confirm",
                        "KAKAO 3.0：Stripe pre_confirm Kakao Pay", 8, total)
    pre_confirm_response = provider_session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/pre_confirm",
        data={
            "eid": str(uuid.uuid4()),
            "payment_method_type": "kakao_pay",
            "key": publishable_key,
            "_stripe_version": STRIPE_VERSION_FULL,
        },
        headers=opll_kakao_v3_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if pre_confirm_response.status_code != 200:
        raise RuntimeError(
            f"KAKAO 3.0 pre_confirm failed: HTTP {pre_confirm_response.status_code} "
            f"{pre_confirm_response.text[:800]}"
        )

    _emit_payment_stage(progress_callback, "kakao3_method",
                        "KAKAO 3.0：创建 Kakao Pay payment method", 9, total)
    stripe_runtime = "c00af4ce81"
    client_session_id = str(uuid.uuid4())
    guid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    muid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    sid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    payment_method_body = {
        "type": "kakao_pay",
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[address][country]": "KR",
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][line2]": billing["line2"],
        "billing_details[address][city]": billing["city"],
        "billing_details[address][postal_code]": billing["postal_code"],
        "billing_details[address][state]": billing["state"],
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "_stripe_version": STRIPE_VERSION_FULL,
        "key": publishable_key,
        "payment_user_agent": (
            f"stripe.js/{stripe_runtime}; stripe-js-v3/{stripe_runtime}; checkout"
        ),
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
    }
    config_id = str(init_payload.get("config_id") or "")
    if config_id:
        payment_method_body["client_attribution_metadata[checkout_config_id]"] = config_id
    payment_method_response = provider_session.post(
        "https://api.stripe.com/v1/payment_methods",
        data=payment_method_body,
        headers=opll_kakao_v3_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if payment_method_response.status_code != 200:
        raise RuntimeError(
            f"KAKAO 3.0 payment method failed: HTTP {payment_method_response.status_code} "
            f"{payment_method_response.text[:1000]}"
        )
    payment_method_id = str((payment_method_response.json() or {}).get("id") or "")
    if not payment_method_id.startswith("pm_"):
        raise RuntimeError(f"KAKAO 3.0 payment method missing id: {payment_method_response.text[:500]}")

    _emit_payment_stage(progress_callback, "kakao3_confirm",
                        "KAKAO 3.0：Stripe custom confirm", 10, total)
    success_url = (
        f"https://chatgpt.com/backend-api/payments/checkout/{processor_entity}/{checkout_id}/success?"
        "billing_country=KR"
    )
    return_url = (
        f"https://checkout.stripe.com/c/pay/{checkout_id}?returned_from_redirect=true&ui_mode=custom&"
        f"return_url={quote(success_url, safe='')}"
    )
    confirm_body = {
        "eid": "NA",
        "payment_method": payment_method_id,
        "expected_amount": amount,
        "tax_id_collection[purchasing_as_business]": "false",
        "expected_payment_method_type": "kakao_pay",
        "return_url": return_url,
        "_stripe_version": STRIPE_VERSION_FULL,
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": publishable_key,
        "version": stripe_runtime,
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "link_brand": "link",
        **opll_kakao_v3_elements_params(stripe_js_id, elements_session_id),
    }
    if config_id:
        confirm_body["client_attribution_metadata[checkout_config_id]"] = config_id
    confirm_response = provider_session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/confirm",
        data=confirm_body,
        headers=opll_kakao_v3_stripe_headers(publishable_key, checkout_page),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if confirm_response.status_code != 200:
        raise RuntimeError(
            f"KAKAO 3.0 confirm failed: HTTP {confirm_response.status_code} {confirm_response.text[:1000]}"
        )
    confirm_payload = confirm_response.json() or {}
    stripe_redirect_url = opll_extract_redirect_to_url(confirm_payload)
    submission = opll_find_submission_attempt(confirm_payload)

    _emit_payment_stage(progress_callback, "kakao3_approve_poll",
                        "KAKAO 3.0：OpenAI approve 并轮询 Stripe redirect", 11, total)
    requires_manual_approval = bool(
        submission.get("state") == "requires_approval"
        or (isinstance(raw_checkout, dict) and raw_checkout.get("requires_manual_approval"))
    )
    if not stripe_redirect_url and requires_manual_approval:
        approve_retry_max = _opll_kakao_v3_env_int("KAKAO_APPROVE_RETRY_MAX", 1, 1, 10)
        last_approve_error = ""
        approve_path = "/backend-api/payments/checkout/approve"
        for retry_index in range(1, approve_retry_max + 1):
            approve_response = provider_session.post(
                f"https://chatgpt.com{approve_path}",
                json={
                    "checkout_session_id": checkout_id,
                    "processor_entity": processor_entity,
                },
                headers=opll_kakao_v3_chatgpt_headers(
                    token,
                    referer=f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}",
                    target_path=approve_path,
                    chatgpt_cookie=chatgpt_cookie,
                ),
                timeout=PAY_LONG_LINK_TIMEOUT,
            )
            approved = False
            if approve_response.status_code == 200:
                try:
                    approved = (approve_response.json() or {}).get("result") == "approved"
                except Exception:
                    approved = False
            if approved:
                last_approve_error = ""
                break
            last_approve_error = (
                f"KAKAO 3.0 approve failed: HTTP {approve_response.status_code} "
                f"{approve_response.text[:500]}"
            )
            if retry_index < approve_retry_max:
                time.sleep(1)
        if last_approve_error:
            raise RuntimeError(last_approve_error)

    poll_timeout = _opll_kakao_v3_env_int("KAKAO_POLL_TIMEOUT", 120, 30, 300)
    poll_params = {
        "key": publishable_key,
        **opll_kakao_v3_elements_params(stripe_js_id, elements_session_id),
    }
    deadline = time.time() + poll_timeout
    while not stripe_redirect_url and time.time() < deadline:
        poll_response = provider_session.get(
            f"https://api.stripe.com/v1/payment_pages/{checkout_id}",
            params=poll_params,
            headers=opll_kakao_v3_stripe_headers(publishable_key, checkout_page),
            timeout=8,
        )
        if poll_response.status_code == 200:
            stripe_redirect_url = opll_extract_redirect_to_url(poll_response.json() or {})
        if not stripe_redirect_url:
            time.sleep(1)
    if not stripe_redirect_url:
        raise RuntimeError("KAKAO 3.0 redirect url timeout")

    provider_url = stripe_redirect_url
    for _ in range(6):
        if opll_is_kakao_pay_url(provider_url):
            break
        redirect_response = provider_session.get(
            provider_url,
            allow_redirects=False,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
        location = str(redirect_response.headers.get("Location") or "")
        if redirect_response.status_code not in {301, 302, 303, 307, 308} or not location:
            break
        provider_url = urljoin(provider_url, location)
    if not opll_is_kakao_pay_url(provider_url):
        raise RuntimeError(
            f"KAKAO 3.0 未提取到 Kakao/Nicepay 跳转链；当前结果: "
            f"{provider_url or stripe_redirect_url}"
        )

    _emit_payment_stage(progress_callback, "kakao3_done",
                        "KAKAO 3.0：韩国 Kakao Pay 链提取完成", 12, total)
    expires_at, expires_raw = opll_checkout_expires_at(raw_checkout, init_payload)
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    return {
        **{k: v for k, v in checkout.items() if k != "raw_checkout" and not k.startswith("_")},
        "payment_method_country": "KR",
        "payment_method_id": payment_method_id,
        "stripe_hosted_url": str(init_payload.get("stripe_hosted_url") or checkout_page),
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": provider_url,
        "long_url": provider_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, "KRW"),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "kakao_pay",
        "local_payment_version": "3.0",
        "local_payment_detected": True,
        "payment_locale": "ko",
        "browser_timezone": "Asia/Seoul",
        "billing_email": billing["email"],
        "kakao_billing": billing,
        "promotion_update": promotion_payload,
        "checkout_tax_update": checkout_tax_payload,
        "stripe_tax_update": stripe_tax_payload,
        "bootstrap_init": {
            "amount": opll_kakao_v3_expected_amount(bootstrap_payload),
            "currency": str(bootstrap_payload.get("currency") or ""),
            "payment_method_types": bootstrap_payload.get("payment_method_types") or [],
        },
    }


def generate_opll_kakao_pay_v2_long_link(access_token: str, kr_proxy_url: str = "",
                                         promo_proxy_url: str = "", progress_callback=None,
                                         chatgpt_cookie: str = "") -> dict:
    """KAKAO 2.0: KR checkout/Stripe, promo update, KR billing, confirm and approve."""
    payment_locale = "ko"
    browser_timezone = "Asia/Seoul"
    kr_proxy_url = str(kr_proxy_url or "").strip()
    promo_proxy_url = str(promo_proxy_url or "").strip()
    if not kr_proxy_url:
        raise RuntimeError("KAKAO 2.0 requires KR proxy pool")
    if not promo_proxy_url:
        raise RuntimeError("KAKAO 2.0 requires promo proxy pool")

    total = 9
    _emit_payment_stage(progress_callback, "kakao2_checkout",
                        "KAKAO 2.0：KR 创建 ChatGPT checkout", 1, total)
    checkout = opll_create_checkout(
        access_token, "KR", "KRW", kr_proxy_url,
        checkout_ui_mode="hosted", require_stripe_session=True,
        preferred_processor_entity=opll_processor_entity_for_country("KR"),
        promo_campaign_id="plus-1-month-free",
    )
    billing = opll_generate_kr_profile()
    checkout["_kakao_billing_profile"] = billing
    stripe_pk = opll_stripe_key_for_checkout(checkout)

    _emit_payment_stage(progress_callback, "kakao2_promo",
                        "KAKAO 2.0：优惠代理 checkout/update 到 0", 2, total)
    promotion_payload = opll_chatgpt_checkout_update_promotion(
        access_token, checkout, promo_proxy_url, chatgpt_cookie=chatgpt_cookie,
    )

    _emit_payment_stage(progress_callback, "kakao2_stripe_init",
                        "KAKAO 2.0：KR Stripe init", 3, total)
    stripe = opll_build_stripe_session(kr_proxy_url)
    init_payload = opll_stripe_init(
        checkout["cs_id"], checkout["billing_country"], checkout["currency"],
        kr_proxy_url, payment_locale=payment_locale, stripe=stripe, checkout=checkout,
        browser_timezone=browser_timezone, saved_payment_method_mode="never",
    )
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(
            f"KAKAO 2.0 stripe init response missing stripe_hosted_url, keys={sorted(init_payload.keys())}"
        )
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    if stripe_amount and not _opll_amount_is_zero(stripe_amount):
        raise RuntimeError(
            f"KAKAO 2.0 amount is not 0 after promo: amount={stripe_amount}, source={stripe_amount_source}"
        )

    ctx = opll_stripe_context(init_payload, payment_locale, {
        "browser_timezone": browser_timezone,
        "saved_payment_method_mode": "never",
    })
    ctx["currency"] = str(checkout.get("currency") or "KRW").lower()

    _emit_payment_stage(progress_callback, "kakao2_tax",
                        "KAKAO 2.0：同步韩国账单与税区", 4, total)
    tax_payload = opll_stripe_update_tax_region(
        stripe, checkout["cs_id"], stripe_pk, ctx, billing,
        payment_locale=payment_locale, browser_timezone=browser_timezone,
        saved_payment_method_mode="never",
    )
    if isinstance(tax_payload, dict) and tax_payload:
        init_payload = tax_payload
        stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or stripe_hosted_url).strip()
        stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
        ctx = opll_stripe_context(init_payload, payment_locale, ctx)
        ctx["browser_timezone"] = browser_timezone
        ctx["saved_payment_method_mode"] = "never"
        ctx["currency"] = str(checkout.get("currency") or "KRW").lower()
    if stripe_amount and not _opll_amount_is_zero(stripe_amount):
        raise RuntimeError(
            f"KAKAO 2.0 amount changed after KR billing sync: amount={stripe_amount}, source={stripe_amount_source}"
        )
    if not opll_payment_method_available(init_payload, "kakao_pay"):
        raise RuntimeError(
            "KAKAO 2.0 Stripe checkout did not expose KAKAO Pay; "
            f"{opll_payment_method_diagnostics(init_payload)}; retry with another KR proxy"
        )
    expires_at, expires_raw = opll_checkout_expires_at(checkout, init_payload)

    _emit_payment_stage(progress_callback, "kakao2_method",
                        "KAKAO 2.0：创建 Kakao Pay payment method", 5, total)
    pm_id = opll_stripe_create_kakao_pay_method(
        stripe, checkout["cs_id"], ctx, billing, stripe_pk,
    )

    _emit_payment_stage(progress_callback, "kakao2_confirm",
                        "KAKAO 2.0：Stripe confirm", 6, total)
    confirm_payload = opll_stripe_confirm(
        stripe, checkout["cs_id"], pm_id, stripe_pk, init_payload, ctx, checkout,
        stripe_hosted_url, payment_method_type="kakao_pay",
    )
    submission = opll_find_submission_attempt(confirm_payload)
    if submission.get("state") == "failed":
        raise RuntimeError(
            f"KAKAO 2.0 stripe confirm failed: {opll_stripe_payload_diagnostics(confirm_payload, ctx)}"
        )

    _emit_payment_stage(progress_callback, "kakao2_approve",
                        "KAKAO 2.0：OpenAI approve / Stripe 轮询", 7, total)
    stripe_redirect_url = opll_redirect_url_after_confirm(
        access_token, stripe, confirm_payload, checkout["cs_id"], stripe_pk, ctx,
        checkout, proxy_url=kr_proxy_url, payment_locale=payment_locale,
        chatgpt_cookie=chatgpt_cookie,
    )

    _emit_payment_stage(progress_callback, "kakao2_redirect",
                        "KAKAO 2.0：解析韩国 Kakao Pay 跳转链", 8, total)
    provider_url = stripe_redirect_url if opll_is_kakao_pay_url(stripe_redirect_url) else \
        opll_resolve_external_redirect(
            stripe,
            stripe_redirect_url,
            preferred_hosts=("kakaopay.com", "kakao.com", "nicepay.co.kr", "kakaopaycorp.com"),
        )
    if not opll_is_kakao_pay_url(provider_url):
        raise RuntimeError(
            f"KAKAO 2.0 未提取到韩国跳转链；当前结果: {provider_url or stripe_redirect_url}"
        )

    _emit_payment_stage(progress_callback, "kakao2_done",
                        "KAKAO 2.0：韩国 Kakao Pay 链提取完成", 9, total)
    return {
        **{k: v for k, v in checkout.items() if k != "raw_checkout" and not k.startswith("_")},
        "payment_method_country": "KR",
        "payment_method_id": pm_id,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": provider_url,
        "long_url": provider_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, "KRW"),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "kakao_pay",
        "local_payment_version": "2.0",
        "local_payment_detected": True,
        "payment_locale": payment_locale,
        "browser_timezone": browser_timezone,
        "billing_email": str(billing.get("email") or ""),
        "kakao_billing": billing,
        "promotion_update": promotion_payload,
        "tax_update": tax_payload,
    }


def generate_opll_kakao_pay_long_link(access_token: str, entry_proxy_url: str = "",
                                      exit_proxy_url: str = "", progress_callback=None) -> dict:
    _emit_payment_stage(progress_callback, "checkout", "JP入口创建 KR ChatGPT checkout", 1, 6)
    checkout = opll_create_checkout(access_token, "KR", "KRW", entry_proxy_url)
    provider_proxy_url = exit_proxy_url or entry_proxy_url
    _emit_payment_stage(progress_callback, "stripe_init", "KR出口初始化 Stripe KAKAO 支付页", 2, 6)
    stripe = opll_build_stripe_session(provider_proxy_url)
    init_payload = opll_stripe_init(checkout["cs_id"], checkout["billing_country"],
                                    checkout["currency"], provider_proxy_url,
                                    stripe=stripe, checkout=checkout)
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(f"stripe init response missing stripe_hosted_url, keys={sorted(init_payload.keys())}")
    if not opll_payment_method_available(init_payload, "kakao_pay"):
        raise RuntimeError(
            "payment page did not expose KAKAO Pay yet; "
            f"{opll_payment_method_diagnostics(init_payload)}; retry with another KR exit proxy"
        )
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    expires_at, expires_raw = opll_checkout_expires_at(checkout, init_payload)
    stripe_pk = opll_stripe_key_for_checkout(checkout)
    ctx = opll_stripe_context(init_payload)
    if not ctx.get("currency"):
        ctx["currency"] = str(checkout.get("currency") or "").lower()
    _emit_payment_stage(progress_callback, "kakao_method", "创建 KAKAO payment method", 3, 6)
    pm_id = opll_stripe_create_kakao_pay_method(stripe, checkout["cs_id"], ctx,
                                                opll_billing_for_country("KR"), stripe_pk)
    _emit_payment_stage(progress_callback, "stripe_confirm", "执行 Stripe confirm 获取 KAKAO 跳转", 4, 6)
    confirm_payload = opll_stripe_confirm(stripe, checkout["cs_id"], pm_id, stripe_pk,
                                          init_payload, ctx, checkout, stripe_hosted_url,
                                          payment_method_type="kakao_pay")
    stripe_redirect_url = opll_extract_redirect_to_url(confirm_payload)
    submission = opll_find_submission_attempt(confirm_payload)
    if not stripe_redirect_url and submission.get("state") == "requires_approval":
        _emit_payment_stage(progress_callback, "chatgpt_approve", "ChatGPT approve / 入口代理授权", 5, 6)
        opll_chatgpt_approve_with_retry(access_token, checkout["cs_id"], checkout, entry_proxy_url)
        stripe_redirect_url = opll_stripe_payment_page_redirect_url(stripe, checkout["cs_id"], stripe_pk,
                                                                    ctx=ctx, timeout_seconds=45)
    elif not stripe_redirect_url:
        try:
            stripe_redirect_url = opll_stripe_payment_page_redirect_url(stripe, checkout["cs_id"], stripe_pk,
                                                                        ctx=ctx, timeout_seconds=30)
        except OpllStripeRequiresApproval:
            _emit_payment_stage(progress_callback, "chatgpt_approve", "ChatGPT approve / 入口代理授权", 5, 6)
            opll_chatgpt_approve_with_retry(access_token, checkout["cs_id"], checkout, entry_proxy_url)
            stripe_redirect_url = opll_stripe_payment_page_redirect_url(stripe, checkout["cs_id"], stripe_pk,
                                                                        ctx=ctx, timeout_seconds=45)
    if submission.get("state") == "failed":
        raise RuntimeError(f"stripe KAKAO submission failed: {opll_stripe_payload_diagnostics(confirm_payload, ctx)}")
    _emit_payment_stage(progress_callback, "kakao_redirect", "解析 KAKAO Provider Redirect URL", 5, 6)
    provider_url = stripe_redirect_url if opll_is_kakao_pay_url(stripe_redirect_url) else \
        opll_resolve_external_redirect(
            stripe,
            stripe_redirect_url,
            preferred_hosts=("kakaopay.com", "kakao.com", "nicepay.co.kr", "kakaopaycorp.com"),
        )
    if not opll_is_kakao_pay_url(provider_url):
        raise RuntimeError(f"未提取到 KAKAO 韩国跳转链；当前结果: {provider_url or stripe_redirect_url}")
    long_url = provider_url
    _emit_payment_stage(progress_callback, "done", "已提取 KAKAO 韩国链", 6, 6)
    return {
        **checkout,
        "payment_method_country": "KR",
        "payment_method_id": pm_id,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": provider_url,
        "long_url": long_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "payment_amount_display": opll_format_minor_amount(stripe_amount, checkout["currency"]),
        "expires_at": expires_at,
        "expires_raw": expires_raw,
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "kakao_pay",
        "local_payment_detected": True,
    }


# ===================================================================
# Unified entry point
# ===================================================================

def generate_payment_link(access_token: str, mode: str = "无卡长链接 US/USD",
                           proxy_url: str = "",
                           provider_proxy_url: str = "",
                           progress_callback=None,
                           chatgpt_cookie: str = "",
                           upi_approve_mode: str = "full_auto",
                           pix_final_proxy_url: str = "",
                           upi_region: str = "IN",
                           upi_payment_locale: str = "en",
                            upi_payment_email: str = "",
                            paypal_billing_country: str = "JP",
                            paypal_payment_locale: str = "en",
                            paypal_billing_email: str = "",
                            paypal_proxy_country: str = "",
                            paypal_promo_country: str = "",
                            paypal_checkout_branch: str = "auto") -> dict:
    """
    Generate a ChatGPT Plus payment link from an access token (Session).

    Args:
        access_token: ChatGPT session access token (raw JSON or bare token).
        mode: Payment mode name, one of PAYMENT_MODES keys:
              - "无卡长链接 US/USD" (hosted Stripe URL, no card)
              - "无卡长链接 BR/BRL"
              - "无卡长链接 DE/EUR"
              - "无卡长链接 FR/EUR"
              - "无卡长链接 GB/GBP"
              - "无卡长链接 CA/CAD"
              - "无卡长链接 AU/AUD"
              - "无卡长链接 JP/JPY"
              - "菲律宾短链 PH/PHP"
              - "GoPay 长链接 ID/IDR"
              - "PayPal 长链接 US/USD" (PayPal BA approve URL)
              - "PayPal 长链接 FR/EUR"
              - "PayPal 长链接 BR/BRL" (Brazil end-to-end PayPal URL extraction)
              - "Apple Pay 支付页 US/USD"
              - "Apple Pay 支付页 JP/JPY"
        proxy_url: Optional HTTP proxy URL, e.g. "http://127.0.0.1:7890".

    Returns:
        dict with keys: long_url, cs_id, billing_country, currency,
                        stripe_hosted_url, processor_entity, etc.
    """
    mode_config = PAYMENT_MODES.get(mode)
    if not mode_config:
        raise ValueError(f"Unknown payment mode: {mode}. Available: {list(PAYMENT_MODES.keys())}")

    token = parse_session_json(access_token) or str(access_token or "").strip()
    if not token:
        raise RuntimeError("无法从输入内容中解析 Access Token")

    country = str(mode_config.get("country") or "US")
    currency = str(mode_config.get("currency") or currency_for_country(country))
    is_paypal = mode.startswith("PayPal 长链接")
    is_true_no_card_us = bool(mode_config.get("true_no_card_us"))
    apple_pay_hosted = bool(mode_config.get("apple_pay_hosted"))
    is_chatgpt_short_link = bool(mode_config.get("chatgpt_short_link"))
    is_ph_cross_region_promo = bool(mode_config.get("ph_cross_region_promo"))
    is_team_codex_low = bool(mode_config.get("team_codex_low"))
    is_ba_pm_711 = bool(mode_config.get("ba_pm_711"))
    is_paypal_global_rotation = bool(mode_config.get("paypal_global_rotation"))
    is_paypal_global_no_discount = bool(mode_config.get("paypal_global_no_discount"))
    is_upi_v2 = bool(mode_config.get("upi_v2"))
    is_upi_v3 = bool(mode_config.get("upi_v3"))
    is_upi_qr = str(mode_config.get("local_payment") or "").lower() == "upi"
    is_pix_v2 = bool(mode_config.get("pix_v2"))
    is_pix_v3 = bool(mode_config.get("pix_v3"))
    is_pix_normal_qr = bool(mode_config.get("pix_normal_qr"))
    is_pix_post_promo = bool(mode_config.get("pix_post_promo"))
    is_pix_standalone_zero = bool(mode_config.get("pix_standalone_zero"))
    is_pix_link = str(mode_config.get("local_payment") or "").lower() == "pix" or bool(mode_config.get("pix_flow"))
    is_ideal_link = str(mode_config.get("local_payment") or "").lower() == "ideal"
    is_ideal_v2 = bool(mode_config.get("ideal_v2"))
    is_ideal_v3 = bool(mode_config.get("ideal_v3"))
    is_kakao_v2 = bool(mode_config.get("kakao_v2"))
    is_kakao_v3 = bool(mode_config.get("kakao_v3"))
    is_kakao_pay_link = str(mode_config.get("local_payment") or "").lower() == "kakao_pay"
    is_twint_link = str(mode_config.get("local_payment") or "").lower() == "twint"
    is_twint_v2_custom = bool(mode_config.get("twint_v2_custom"))
    is_promptpay_link = str(mode_config.get("local_payment") or "").lower() == "promptpay"
    is_momo_link = str(mode_config.get("local_payment") or "").lower() == "momo"
    is_ph_gcash_redirect = bool(mode_config.get("ph_gcash_redirect"))

    if is_ph_gcash_redirect:
        result = generate_opll_ph_gcash_link(
            token,
            proxy_url,
            provider_proxy_url,
            progress_callback,
            chatgpt_cookie=str(chatgpt_cookie or ""),
            processor_entity=str(mode_config.get("short_link_processor_entity") or "openai_llc"),
        )
    elif is_ph_cross_region_promo:
        result = generate_opll_ph_cross_region_promo_short_link(
            token,
            proxy_url,
            provider_proxy_url,
            progress_callback,
            chatgpt_cookie=str(chatgpt_cookie or ""),
            processor_entity=str(mode_config.get("short_link_processor_entity") or "openai_llc"),
        )
    elif is_chatgpt_short_link:
        result = generate_opll_chatgpt_short_link(
            token,
            country,
            currency,
            proxy_url,
            progress_callback,
            processor_entity=str(mode_config.get("short_link_processor_entity") or "openai_llc"),
        )
    elif is_team_codex_low:
        result = generate_opll_team_codex_low_link(
            token,
            proxy_url,
            progress_callback,
        )
    elif is_ba_pm_711:
        result = generate_opll_711_ba_pm_link(
            token,
            proxy_url,
            provider_proxy_url,
            progress_callback,
            chatgpt_cookie=str(chatgpt_cookie or ""),
        )
    elif is_paypal_global_rotation:
        result = generate_opll_paypal_global_rotation_link(
            token,
            proxy_url,
            provider_proxy_url,
            progress_callback,
            chatgpt_cookie=str(chatgpt_cookie or ""),
            billing_email=str(paypal_billing_email or ""),
            billing_country=str(paypal_billing_country or "JP"),
            payment_locale=str(paypal_payment_locale or "en"),
            paypal_proxy_country=str(paypal_proxy_country or ""),
            promo_proxy_country=str(paypal_promo_country or ""),
            checkout_branch=str(paypal_checkout_branch or "auto"),
        )
    elif is_paypal_global_no_discount:
        result = generate_opll_paypal_global_rotation_link(
            token,
            proxy_url,
            "",
            progress_callback,
            chatgpt_cookie=str(chatgpt_cookie or ""),
            billing_email=str(paypal_billing_email or ""),
            billing_country=str(paypal_billing_country or "JP"),
            payment_locale=str(paypal_payment_locale or "en"),
            apply_promotion=False,
            paypal_proxy_country=str(paypal_proxy_country or ""),
            promo_proxy_country=str(paypal_promo_country or ""),
            checkout_branch=str(paypal_checkout_branch or "auto"),
        )
    elif is_upi_v3:
        result = generate_opll_upi_qr_v3(
            token,
            proxy_url,
            provider_proxy_url,
            progress_callback,
            chatgpt_cookie=chatgpt_cookie,
            upi_approve_mode=upi_approve_mode,
            upi_region=upi_region,
            payment_locale=upi_payment_locale,
            payment_email=upi_payment_email,
        )
    elif is_upi_v2:
        result = generate_opll_upi_qr_v2(
            token,
            proxy_url,
            provider_proxy_url,
            progress_callback,
            chatgpt_cookie=chatgpt_cookie,
            upi_approve_mode=upi_approve_mode,
            upi_region=upi_region,
            payment_locale=upi_payment_locale,
            payment_email=upi_payment_email,
        )
    elif is_pix_v3:
        result = generate_opll_pix_v3_long_link(
            token,
            proxy_url,
            provider_proxy_url,
            progress_callback,
            chatgpt_cookie=chatgpt_cookie,
        )
    elif is_pix_v2:
        result = generate_opll_pix_qr_v2(
            token,
            proxy_url,
            provider_proxy_url,
            progress_callback,
            chatgpt_cookie=chatgpt_cookie,
        )
    elif is_pix_normal_qr:
        result = generate_opll_pix_normal_qr(
            token,
            pix_final_proxy_url or proxy_url,
            progress_callback,
            chatgpt_cookie=chatgpt_cookie,
        )
    elif is_pix_standalone_zero:
        result = generate_opll_pix_standalone_zero(
            token,
            proxy_url,
            provider_proxy_url,
            pix_final_proxy_url or proxy_url,
            progress_callback,
            chatgpt_cookie=chatgpt_cookie,
        )
    elif is_pix_link:
        result = generate_opll_pix_long_link(
            token,
            proxy_url,
            provider_proxy_url,
            pix_final_proxy_url or proxy_url,
            progress_callback,
            chatgpt_cookie=chatgpt_cookie,
            prefer_post_promo_pm=is_pix_post_promo,
        )
    elif is_upi_qr:
        result = generate_opll_upi_qr(
            token,
            proxy_url,
            provider_proxy_url,
            progress_callback,
            chatgpt_cookie=chatgpt_cookie,
            upi_approve_mode=upi_approve_mode,
            upi_region=upi_region,
            payment_locale=upi_payment_locale,
            payment_email=upi_payment_email,
        )
    elif is_ideal_link:
        if is_ideal_v3:
            result = generate_opll_ideal_v3_long_link(
                token, proxy_url, provider_proxy_url, progress_callback,
                chatgpt_cookie=str(chatgpt_cookie or ""),
            )
        elif is_ideal_v2:
            result = generate_opll_ideal_v2_long_link(
                token, proxy_url, provider_proxy_url, progress_callback,
                chatgpt_cookie=str(chatgpt_cookie or ""),
            )
        else:
            result = generate_opll_ideal_long_link(token, proxy_url, provider_proxy_url, progress_callback)
    elif is_kakao_pay_link:
        if is_kakao_v3:
            result = generate_opll_kakao_pay_v3_long_link(
                token, proxy_url, provider_proxy_url, progress_callback,
                chatgpt_cookie=str(chatgpt_cookie or ""),
            )
        elif is_kakao_v2:
            result = generate_opll_kakao_pay_v2_long_link(
                token, proxy_url, provider_proxy_url, progress_callback,
                chatgpt_cookie=str(chatgpt_cookie or ""),
            )
        else:
            result = generate_opll_kakao_pay_long_link(token, proxy_url, provider_proxy_url, progress_callback)
    elif is_twint_link:
        if is_twint_v2_custom:
            result = generate_opll_twint_v2_long_link(
                token, proxy_url, provider_proxy_url, progress_callback,
                chatgpt_cookie=str(chatgpt_cookie or ""),
            )
        else:
            result = generate_opll_twint_long_link(
                token, proxy_url, provider_proxy_url, progress_callback,
                chatgpt_cookie=str(chatgpt_cookie or ""),
            )
    elif is_promptpay_link:
        result = generate_opll_promptpay_long_link(
            token, proxy_url, provider_proxy_url, progress_callback,
            chatgpt_cookie=str(chatgpt_cookie or ""),
        )
    elif is_momo_link:
        result = generate_opll_momo_long_link(
            token, proxy_url, provider_proxy_url, progress_callback,
            chatgpt_cookie=str(chatgpt_cookie or ""),
        )
    elif is_paypal or is_true_no_card_us:
        paypal_result_mode = str(mode_config.get("paypal_result_mode") or "approval")
        result = generate_opll_paypal_long_link(
            token,
            country,
            currency,
            proxy_url,
            provider_proxy_url or proxy_url,
            progress_callback,
            paypal_result_mode=paypal_result_mode,
            payment_locale=str(mode_config.get("payment_locale") or ""),
            force_country=bool(mode_config.get("paypal_force_country")),
            chatgpt_cookie=chatgpt_cookie,
            paypal_page_country=str(mode_config.get("paypal_page_country") or ""),
        )
    else:
        # 无卡长链接 / GoPay / Apple Pay — all use hosted
        result = generate_opll_hosted_long_link(
            token, country, currency, proxy_url, str(mode_config.get("local_payment") or ""), progress_callback
        )

    return {
        "success": True,
        "mode": mode,
        "long_url": str(result.get("long_url") or ""),
        "short_url": str(result.get("short_url") or ""),
        "checkout_session_id": str(result.get("checkout_session_id") or result.get("checkout_id") or result.get("cs_id") or ""),
        "checkout_id": str(result.get("checkout_id") or result.get("cs_id") or ""),
        "cs_id": str(result.get("cs_id") or ""),
        "session_kind": str(result.get("session_kind") or ""),
        "checkout_branch": str(result.get("checkout_branch") or ""),
        "checkout_session_type": str(result.get("checkout_session_type") or opll_checkout_session_type_from_id(str(result.get("checkout_session_id") or result.get("checkout_id") or result.get("cs_id") or ""))),
        "checkout_branch_requested": str(result.get("checkout_branch_requested") or ""),
        "checkout_branch_effective": str(result.get("checkout_branch_effective") or ""),
        "oaics_eligible": bool(result.get("oaics_eligible")),
        "oaics_probe": result.get("oaics_probe") or {},
        "browser_profile": str(result.get("browser_profile") or ""),
        "confirmation_token": str(result.get("confirmation_token") or ""),
        "client_secret_prefix": str(result.get("client_secret_prefix") or ""),
        "billing_country": str(result.get("billing_country") or country),
        "currency": str(result.get("currency") or currency),
        "stripe_hosted_url": str(result.get("stripe_hosted_url") or ""),
        "processor_entity": str(result.get("processor_entity") or ""),
        "payment_method_country": str(result.get("payment_method_country") or ""),
        "stripe_amount": str(result.get("stripe_amount") or ""),
        "stripe_amount_source": str(result.get("stripe_amount_source") or ""),
        "payment_amount_display": str(result.get("payment_amount_display") or ""),
        "expires_at": int(result.get("expires_at") or 0),
        "expires_raw": str(result.get("expires_raw") or ""),
        "valid_seconds": int(result.get("valid_seconds") or 0),
        "local_payment": str(result.get("local_payment") or ""),
        "local_payment_detected": bool(result.get("local_payment_detected")),
        "provider_redirect_url": str(result.get("provider_redirect_url") or ""),
        "verification_url": str(result.get("verification_url") or ""),
        "gcash_redirect_url": str(result.get("gcash_redirect_url") or ""),
        "stripe_redirect_url": str(result.get("stripe_redirect_url") or ""),
        "paypal_result_mode": str(result.get("paypal_result_mode") or ""),
        "paypal_approval_url": str(result.get("paypal_approval_url") or ""),
        "paypal_url": str(result.get("paypal_url") or result.get("paypal_approval_url") or ""),
        "pm_redirect_url": str(result.get("pm_redirect_url") or ""),
        "payment_method_id": str(result.get("payment_method_id") or ""),
        "paypal_proxy_country": str(result.get("paypal_proxy_country") or ""),
        "paypal_main_proxy_country": str(result.get("paypal_main_proxy_country") or result.get("paypal_proxy_country") or ""),
        "promo_proxy_country": str(result.get("promo_proxy_country") or ""),
        "billing_email": str(result.get("billing_email") or ""),
        "billing_profile": result.get("billing_profile") or {},
        "billing_country_source": str(result.get("billing_country_source") or ""),
        "payment_methods": result.get("payment_methods") or [],
        "payment_locale": str(result.get("payment_locale") or ""),
        "extraction_status": str(result.get("extraction_status") or ""),
        "upi_region": str(result.get("upi_region") or ""),
        "upi_billing": result.get("upi_billing") or {},
        "twint_billing": result.get("twint_billing") or {},
        "promptpay_billing": result.get("promptpay_billing") or {},
        "local_payment_version": str(result.get("local_payment_version") or ""),
        "browser_timezone": str(result.get("browser_timezone") or ""),
        "country_proxy_hint": str(result.get("country_proxy_hint") or ""),
        "upi_qr_data": str(result.get("upi_qr_data") or ""),
        "upi_qr_image_url": str(result.get("upi_qr_image_url") or ""),
        "upi_qr_image_url_png": str(result.get("upi_qr_image_url_png") or ""),
        "upi_qr_image_url_svg": str(result.get("upi_qr_image_url_svg") or ""),
        "upi_qr_image_data_url": str(result.get("upi_qr_image_data_url") or ""),
        "upi_qr_hosted_instructions_url": str(result.get("upi_qr_hosted_instructions_url") or ""),
        "chatgpt_checkout_url": str(result.get("chatgpt_checkout_url") or ""),
        "upi_approve_mode": str(result.get("upi_approve_mode") or upi_approve_mode),
        "pix_link": str(result.get("pix_link") or ""),
        "pix_instructions_url": str(result.get("pix_instructions_url") or ""),
        "pix_checkout_url": str(result.get("pix_checkout_url") or ""),
        "pix_openai_pay_url": str(result.get("pix_openai_pay_url") or ""),
        "pix_redirect_url": str(result.get("pix_redirect_url") or ""),
        "pix_payload": str(result.get("pix_payload") or ""),
        "pix_qr_image_url": str(result.get("pix_qr_image_url") or ""),
        "pix_qr_image_data_url": str(result.get("pix_qr_image_data_url") or ""),
        "pix_hosted_instructions_url": str(result.get("pix_hosted_instructions_url") or ""),
        "pix_resource_url": str(result.get("pix_resource_url") or ""),
        "pix_source": str(result.get("pix_source") or ""),
        "promptpay_link": str(result.get("promptpay_link") or ""),
        "promptpay_hosted_instructions_url": str(result.get("promptpay_hosted_instructions_url") or ""),
        "promptpay_qr_data": str(result.get("promptpay_qr_data") or ""),
        "promptpay_qr_image_url": str(result.get("promptpay_qr_image_url") or ""),
        "promptpay_qr_image_data_url": str(result.get("promptpay_qr_image_data_url") or ""),
        "promotion_applied": bool(result.get("promotion_applied")),
        "promotion_zero_verified": bool(result.get("promotion_zero_verified")),
        "promotion_amount": str(result.get("promotion_amount") or ""),
        "promotion_amount_source": str(result.get("promotion_amount_source") or ""),
        "ideal_direct_url": str(result.get("ideal_direct_url") or ""),
        "ideal_qr_data": str(result.get("ideal_qr_data") or ""),
        "ideal_qr_image_data_url": str(result.get("ideal_qr_image_data_url") or ""),
        "ideal_billing": result.get("ideal_billing") or {},
        "fallback": bool(result.get("fallback")),
        "raw_result": result,
    }


# ===================================================================
# Proxy chain server (for chaining local + dynamic proxy)
# ===================================================================

class ProxyChainServer:
    """Chain local proxy -> dynamic proxy -> target, with runtime switching."""

    def __init__(self, local_proxy: str, dynamic_proxy: str,
                 log_callback=None):
        self.local_proxy = normalize_proxy_url(local_proxy)
        self.dynamic_proxy = normalize_proxy_url(dynamic_proxy)
        self.log = log_callback or (lambda msg: None)
        self.lock = threading.Lock()
        self.active_sockets: set[socket.socket] = set()
        self.server: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.url = ""

    def __enter__(self):
        if not self.local_proxy and not self.dynamic_proxy:
            return self
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(64)
        port = self.server.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()

    def close(self) -> None:
        self.stop_event.set()
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass
        self.server = None

    def set_dynamic_proxy(self, dynamic_proxy: str) -> None:
        sockets: list[socket.socket]
        with self.lock:
            self.dynamic_proxy = normalize_proxy_url(dynamic_proxy)
            sockets = list(self.active_sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    def _track_socket(self, sock: socket.socket) -> None:
        with self.lock:
            self.active_sockets.add(sock)

    def _untrack_socket(self, sock: socket.socket) -> None:
        with self.lock:
            self.active_sockets.discard(sock)

    def _serve(self) -> None:
        assert self.server is not None
        while not self.stop_event.is_set():
            try:
                client, _addr = self.server.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _handle_client(self, client: socket.socket) -> None:
        upstream = None
        self._track_socket(client)
        try:
            client.settimeout(30)
            head = self._read_http_head(client)
            if not head:
                return
            first_line = head.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
            parts = first_line.split()
            if len(parts) < 3:
                return
            method, target, version = parts[0].upper(), parts[1], parts[2]
            if method == "CONNECT":
                upstream = self._open_chain_to_target(target)
                self._track_socket(upstream)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._relay(client, upstream)
                return
            rewritten = self._rewrite_plain_request(head, method, target, version)
            upstream = self._open_chain_to_target(self._target_from_plain_request(method, target, head))
            self._track_socket(upstream)
            upstream.sendall(rewritten)
            self._relay(client, upstream)
        except Exception:
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except Exception:
                pass
        finally:
            self._untrack_socket(client)
            if upstream:
                self._untrack_socket(upstream)
            try:
                client.close()
            except Exception:
                pass

    def _read_http_head(self, client: socket.socket) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    def _target_from_plain_request(self, method: str, target: str, head: bytes) -> str:
        if target.startswith("http://") or target.startswith("https://"):
            parsed = urlparse(target)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            return f"{parsed.hostname}:{port}"
        host = ""
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"host:"):
                host = line.split(b":", 1)[1].strip().decode("latin1")
                break
        return host

    def _rewrite_plain_request(self, head: bytes, method: str, target: str, version: str) -> bytes:
        if not (target.startswith("http://") or target.startswith("https://")):
            return head
        parsed = urlparse(target)
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        lines = head.split(b"\r\n")
        lines[0] = f"{method} {path} {version}".encode("latin1")
        return b"\r\n".join(lines)

    def _open_chain_to_target(self, target: str) -> socket.socket:
        with self.lock:
            local_proxy = self.local_proxy
            dynamic_proxy = self.dynamic_proxy
        if local_proxy and is_socks_proxy_url(local_proxy):
            if dynamic_proxy:
                raise RuntimeError("SOCKS local proxy cannot be chained with another upstream proxy")
            return self._connect_socks_to_target(local_proxy, target)
        if local_proxy:
            if dynamic_proxy and is_socks_proxy_url(dynamic_proxy):
                raise RuntimeError("SOCKS dynamic proxy must be used directly, not behind local HTTP chain")
            sock = self._connect_proxy(local_proxy)
            self._send_connect(sock, self._proxy_connect_target(dynamic_proxy) if dynamic_proxy else target)
            if dynamic_proxy:
                self._send_connect(sock, target, proxy_url=dynamic_proxy)
            return sock
        if dynamic_proxy:
            if is_socks_proxy_url(dynamic_proxy):
                return self._connect_socks_to_target(dynamic_proxy, target)
            sock = self._connect_proxy(dynamic_proxy)
            self._send_connect(sock, target, proxy_url=dynamic_proxy)
            return sock
        host, port = self._split_host_port(target, 80)
        return socket.create_connection((host, port), timeout=30)

    def _connect_socks_to_target(self, proxy_url: str, target: str) -> socket.socket:
        if socks is None:
            raise RuntimeError("缺少 SOCKS 依赖，请先安装: pip install PySocks")
        parsed = urlparse(proxy_url)
        host = parsed.hostname
        if not host:
            raise RuntimeError(f"代理地址缺少 host: {proxy_url}")
        scheme = parsed.scheme.lower()
        proxy_type = socks.SOCKS5 if scheme in ("socks5", "socks5h") else socks.SOCKS4
        rdns = scheme in ("socks4a", "socks5h")
        target_host, target_port = self._split_host_port(target, 80)
        sock = socks.socksocket()
        sock.settimeout(30)
        sock.set_proxy(
            proxy_type,
            host,
            parsed.port or 1080,
            rdns=rdns,
            username=unquote(parsed.username or "") or None,
            password=unquote(parsed.password or "") or None,
        )
        sock.connect((target_host, target_port))
        return sock

    def _connect_proxy(self, proxy_url: str) -> socket.socket:
        parsed = urlparse(proxy_url)
        if parsed.scheme not in ("http", "https"):
            raise RuntimeError(f"链式代理当前只支持 http/https 代理: {proxy_url}")
        host = parsed.hostname
        if not host:
            raise RuntimeError(f"代理地址缺少 host: {proxy_url}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        raw = socket.create_connection((host, port), timeout=30)
        if parsed.scheme == "https":
            return ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        return raw

    def _proxy_connect_target(self, proxy_url: str) -> str:
        parsed = urlparse(proxy_url)
        if not parsed.hostname:
            raise RuntimeError(f"动态代理地址缺少 host: {proxy_url}")
        return f"{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"

    def _send_connect(self, sock: socket.socket, target: str, proxy_url: str = "") -> None:
        headers = [f"CONNECT {target} HTTP/1.1", f"Host: {target}", "Proxy-Connection: keep-alive"]
        auth = self._proxy_auth(proxy_url)
        if auth:
            headers.append(f"Proxy-Authorization: Basic {auth}")
        request = ("\r\n".join(headers) + "\r\n\r\n").encode("latin1")
        sock.sendall(request)
        response = self._read_http_head(sock)
        status = response.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        if " 200 " not in f" {status} ":
            raise RuntimeError(f"代理 CONNECT 失败: {status}")

    def _proxy_auth(self, proxy_url: str) -> str:
        parsed = urlparse(proxy_url)
        if not parsed.username:
            return ""
        username = unquote(parsed.username)
        password = unquote(parsed.password or "")
        return base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")

    def _split_host_port(self, target: str, default_port: int) -> tuple[str, int]:
        if target.startswith("["):
            host, rest = target[1:].split("]", 1)
            port = int(rest[1:]) if rest.startswith(":") else default_port
            return host, port
        if ":" in target:
            host, port = target.rsplit(":", 1)
            return host, int(port)
        return target, default_port

    def _relay(self, left: socket.socket, right: socket.socket) -> None:
        sockets = [left, right]
        for sock in sockets:
            sock.settimeout(None)
        try:
            while True:
                readable, _, _ = select.select(sockets, [], [], 60)
                if not readable:
                    return
                for src in readable:
                    dst = right if src is left else left
                    data = src.recv(65536)
                    if not data:
                        return
                    dst.sendall(data)
        finally:
            try:
                right.close()
            except Exception:
                pass
