#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
独立版巴西 PIX 支付链接提取脚本。

特点：
- 完全自包含，不 import / 调用 paypal_link_extractor.py 或其它项目脚本；
- 只做一件事：提取 ChatGPT/OpenAI 巴西 PIX（0 元促销）支付链接；
- 没有代理检测（check_proxies）、没有 PayPal 分支、没有多国家/多组合兜底；
- 保留“失败就换下一组代理重试”的轮换逻辑（三阶段 BR checkout / promo update / BR provider）；
- 需要你提供 ChatGPT/OpenAI 的 access_token。

固定流程：
  1) BR 代理创建 checkout（BR / BRL，不带促销，保留 pix payment_method_types）
  2) 优惠地区代理执行 checkout/update（plus-1-month-free），金额变 0
  3) BR 代理执行 Stripe init -> 创建 PIX payment_method -> confirm
  4) BR 代理执行 ChatGPT approve 与 payment_page 轮询，提取 PIX 链接/复制码/二维码

用法示例：
  python pix_link_extractor.py --access-token "<ACCESS_TOKEN>"
  python pix_link_extractor.py --token-file token.txt --json
  python pix_link_extractor.py --access-token "<ACCESS_TOKEN>" --proxy socks5h://127.0.0.1:1080
  python pix_link_extractor.py --config paypal_link_config.json --proxy-attempts 5
  $env:OPENAI_ACCESS_TOKEN="<ACCESS_TOKEN>"; python pix_link_extractor.py
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import uuid
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests

try:
    from curl_cffi.requests import Session as CurlCffiSession  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    CurlCffiSession = None

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional dependency
    sync_playwright = None


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
DEFAULT_STRIPE_PK = "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"
DEFAULT_PROMO_CAMPAIGN_ID = "plus-1-month-free"
DEFAULT_CONFIG_PATH = "pix_link_config.json"
PIX_LOCALE = ("pt-BR", "pt-BR")  # (browser_locale, elements_locale)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# PIX 固定面向巴西：checkout 国家 BR、货币 BRL、payment_method 账单国家 BR。
COUNTRY = "BR"
CURRENCY = "BRL"
PROCESSOR_ENTITY = "openai_ie"  # 非美国走 openai_ie

BR_BILLING_NAMES = [
    ("Lucas", "Silva"), ("Gabriel", "Santos"), ("Mateus", "Oliveira"),
    ("Joao", "Souza"), ("Mariana", "Costa"), ("Ana", "Pereira"),
]
BR_BILLING_STREETS = [
    ("Avenida Paulista 1000", "Sao Paulo", "SP", "01310-100"),
    ("Rua das Laranjeiras 50", "Rio de Janeiro", "RJ", "22240-003"),
]


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def short_error(detail: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(detail or "")).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------------- #
# access_token 解析
# --------------------------------------------------------------------------- #
def find_access_token(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("accessToken", "access_token", "token"):
            token = str(value.get(key) or "").strip()
            if token:
                return token
        for item in value.values():
            token = find_access_token(item)
            if token:
                return token
    elif isinstance(value, list):
        for item in value:
            token = find_access_token(item)
            if token:
                return token
    return ""


def extract_access_token(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("bearer "):
        return raw.split(None, 1)[1].strip()
    try:
        token = find_access_token(json.loads(raw))
        if token:
            return token
    except Exception:
        pass
    match = re.search(r'"(?:accessToken|access_token|token)"\s*:\s*"([^"]+)"', raw)
    if match:
        return match.group(1).strip()
    return raw


# --------------------------------------------------------------------------- #
# 代理：规范化 / 分组 / 轮换判定
# --------------------------------------------------------------------------- #
def coerce_proxy_items(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else re.split(r"[\r\n,，;]+", str(value))
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except Exception:
        return default


def env_proxy_seed_file() -> str:
    return (
        os.environ.get("PIX_PROXY_SEED_FILE", "").strip()
        or os.environ.get("PP_PROXY_SEED_FILE", "").strip()
        or os.path.join(SCRIPT_DIR, "proxy_seeds.txt")
    )


def load_proxy_seed_items(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return coerce_proxy_items(f.read())
    except FileNotFoundError:
        return []


def env_config() -> dict[str, Any]:
    """Map the existing WebUI PIX environment into this standalone extractor."""
    proxy_items = load_proxy_seed_items(env_proxy_seed_file())
    config: dict[str, Any] = {
        "access_token": os.environ.get("PIX_TOKEN", "").strip() or os.environ.get("PP_TOKEN", "").strip(),
        "token_file": os.path.join(SCRIPT_DIR, "token.txt"),
        "http_backend": os.environ.get("PIX_HTTP_BACKEND", "").strip() or "auto",
        "promo_campaign_id": os.environ.get("PIX_PROMO_ID", "").strip() or DEFAULT_PROMO_CAMPAIGN_ID,
        "timeout": env_int("PIX_TIMEOUT", 30),
        "poll_seconds": env_int("PIX_POLL_TIMEOUT", 45),
        "proxy_scheme": os.environ.get("PIX_PROXY_DEFAULT_SCHEME", "").strip(),
        "proxy_attempts": env_int(
            "PIX_PROXY_ATTEMPTS",
            env_int("PIX_CHECKOUT_RETRY_MAX", max(1, len(proxy_items) or 1)),
        ),
        "create_proxies": proxy_items,
        "followup_proxies": proxy_items,
        "approve_proxies": proxy_items,
    }
    return config


def force_proxy_scheme(proxy_url: str, scheme: str = "") -> str:
    text = str(proxy_url or "").strip()
    scheme = str(scheme or "").strip().lower().rstrip(":/")
    if not text or not scheme or scheme not in {"http", "https", "socks5", "socks5h"}:
        return text
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)(.*)$", text)
    if match:
        return f"{scheme}://{match.group(2)}"
    return f"{scheme}://{text}"


def normalize_proxy_url(proxy: str, force_scheme: str = "") -> str:
    text = str(proxy or "").strip()
    if not text:
        return ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        return force_proxy_scheme(text, force_scheme)
    # 支持 host:port:username:password 格式（如 1024proxy）。
    parts = text.split(":")
    if len(parts) >= 4:
        host, port, username = parts[0].strip(), parts[1].strip(), parts[2].strip()
        password = ":".join(parts[3:]).strip()
        if host and port and username and password:
            scheme = str(force_scheme or "http").strip().lower().rstrip(":/")
            return f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    # 兜底：host:port 视为 HTTP 代理。
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return force_proxy_scheme(f"http://{text}", force_scheme)
    return force_proxy_scheme(text, force_scheme)


def proxy_candidates(single_proxy: str, proxy_items: Any, fallback_items: Any = None, force_scheme: str = "") -> list[str]:
    raw_items = [single_proxy] if str(single_proxy or "").strip() else coerce_proxy_items(proxy_items)
    if not raw_items:
        raw_items = coerce_proxy_items(fallback_items)
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        proxy = normalize_proxy_url(item, force_scheme)
        if proxy and proxy not in seen:
            seen.add(proxy)
            result.append(proxy)
    return result


def build_proxy_plans(
    proxy: str,
    proxies: Any,
    create_proxy: str,
    create_proxies: Any,
    followup_proxy: str,
    followup_proxies: Any,
    approve_proxy: str,
    approve_proxies: Any,
    force_scheme: str = "",
) -> list[tuple[str, str, str]]:
    """生成若干组 (create, followup, approve) 代理，供失败轮换。未配置时返回 [("","","")]。"""
    common = proxy_candidates(proxy, proxies, None, force_scheme)
    create = proxy_candidates(create_proxy, create_proxies, common, force_scheme)
    followup = proxy_candidates(followup_proxy, followup_proxies, common or create, force_scheme)
    approve = proxy_candidates(approve_proxy, approve_proxies, followup, force_scheme)
    max_len = max(len(create), len(followup), len(approve), 1)

    def pick(items: list[str], index: int) -> str:
        return items[index % len(items)] if items else ""

    return [(pick(create, i), pick(followup, i), pick(approve, i)) for i in range(max_len)]


def mask_proxy(proxy_url: str) -> str:
    text = str(proxy_url or "").strip()
    if not text:
        return "无"
    # 只隐藏 password，保留 host/port 和 username，方便确认用的是哪条代理。
    return re.sub(r":([^:@/]+)@", ":***@", text)


def is_retryable_proxy_error(exc: Exception | str) -> bool:
    text = str(exc or "").lower()
    markers = (
        "curl:", "proxy", "connection was reset", "connection reset", "connection aborted",
        "timed out", "timeout", "recv failure", "could not resolve proxy", "failed to connect",
        "tls connect", "401", "403", "429", "502", "503", "504",
        "unauthorized", "forbidden", "blocked",
    )
    return any(marker in text for marker in markers)


def is_retryable_provider_error(exc: Exception | str) -> bool:
    text = str(exc or "").lower()
    markers = (
        "checkout_approval_payment_failure_with_payment_error",
        "generic_decline", "stripe submission failed", "payment_failure", "payment_error",
        "requires chatgpt approval",
    )
    return any(marker in text for marker in markers)


# --------------------------------------------------------------------------- #
# HTTP session
# --------------------------------------------------------------------------- #
def new_http_session(http_backend: str = "auto") -> requests.Session:
    backend = str(http_backend or "auto").strip().lower()
    if backend == "requests":
        session: Any = requests.Session()
    elif CurlCffiSession is not None:
        session = CurlCffiSession(impersonate="chrome136")
    else:
        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    return session  # type: ignore[return-value]


def apply_proxy(session: requests.Session, proxy_url: str) -> None:
    proxy_url = str(proxy_url or "").strip()
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})


def build_chatgpt_session(access_token: str, proxy_url: str, http_backend: str) -> requests.Session:
    token = extract_access_token(access_token)
    if not token:
        raise RuntimeError("缺少 access_token")
    device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"pix-device:{token}"))
    session = new_http_session(http_backend)
    session.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Authorization": f"Bearer {token}",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "Content-Type": "application/json",
        "oai-device-id": device_id,
        "oai-language": "pt-BR",
        "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "Cookie": f"oai-did={device_id}",
    })
    apply_proxy(session, proxy_url)
    return session


def build_stripe_session(proxy_url: str, http_backend: str) -> requests.Session:
    session = new_http_session(http_backend)
    session.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    apply_proxy(session, proxy_url)
    return session


# --------------------------------------------------------------------------- #
# 账单信息（仅巴西）
# --------------------------------------------------------------------------- #
def random_brazil_cpf(rng: random.Random | None = None) -> str:
    """生成通过校验位的巴西 CPF，用于 Stripe PIX billing_details[tax_id]。"""
    rng = rng or random
    digits = [rng.randint(0, 9) for _ in range(9)]
    for weight_start in (10, 11):
        checksum = sum(digit * weight for digit, weight in zip(digits, range(weight_start, 1, -1)))
        check_digit = 11 - (checksum % 11)
        digits.append(0 if check_digit >= 10 else check_digit)
    return "".join(str(item) for item in digits)


def brazil_billing(seed: str = "") -> dict[str, str]:
    """为同一账号生成稳定且格式有效的巴西付款资料，避免每次重试切换身份。"""
    rng = random.Random(uuid.uuid5(uuid.NAMESPACE_URL, f"pix-billing:{seed}").int) if seed else random
    first, last = rng.choice(BR_BILLING_NAMES)
    line1, city, state, postal = rng.choice(BR_BILLING_STREETS)
    suffix = rng.randint(1000, 9999)
    area_code = rng.choice(["11", "21", "31", "41", "51", "61", "71", "81"])
    mobile = "9" + "".join(str(rng.randint(0, 9)) for _ in range(8))
    return {
        "name": f"{first} {last}",
        "email": f"{first.lower()}.{last.lower()}{suffix}@outlook.com",
        "phone": f"+55{area_code}{mobile}",
        "country": COUNTRY,
        "line1": line1,
        "city": city,
        "state": state,
        "postal_code": postal,
        "tax_id": random_brazil_cpf(rng),
    }


# --------------------------------------------------------------------------- #
# ChatGPT checkout / update / approve
# --------------------------------------------------------------------------- #
def extract_processor_entity(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    direct = data.get("processor_entity") or data.get("processorEntity")
    if direct:
        return str(direct).strip()
    for key in ("checkout_session", "session", "checkout", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            found = extract_processor_entity(nested)
            if found:
                return found
    return ""


def extract_stripe_publishable_key(data: Any) -> str:
    if isinstance(data, str):
        match = re.search(r"pk_live_[A-Za-z0-9]+", data)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in ("stripe_publishable_key", "publishable_key", "publishableKey", "stripePublishableKey", "key"):
            found = extract_stripe_publishable_key(data.get(key))
            if found:
                return found
        for item in data.values():
            found = extract_stripe_publishable_key(item)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = extract_stripe_publishable_key(item)
            if found:
                return found
    return ""


def create_checkout(access_token: str, proxy_url: str, http_backend: str, timeout: int) -> dict[str, Any]:
    """步骤1：创建 checkout，不带促销，保留 pix payment_method_types。"""
    session = build_chatgpt_session(access_token, proxy_url, http_backend)
    body = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": COUNTRY, "currency": CURRENCY},
        "checkout_ui_mode": "custom",
    }
    response = session.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=body,
        headers={
            "Referer": "https://chatgpt.com/",
            "x-openai-target-path": "/backend-api/payments/checkout",
            "x-openai-target-route": "/backend-api/payments/checkout",
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"checkout create failed: HTTP {response.status_code} {response.text[:500]}")
    data = response.json() or {}
    cs_id = data.get("checkout_session_id") or data.get("session_id") or data.get("id")
    if not cs_id or not str(cs_id).startswith("cs_"):
        raise RuntimeError(f"checkout response missing cs_id: {str(data)[:500]}")
    entity = extract_processor_entity(data) or PROCESSOR_ENTITY
    return {
        "cs_id": str(cs_id),
        "processor_entity": entity,
        "stripe_publishable_key": extract_stripe_publishable_key(data) or DEFAULT_STRIPE_PK,
    }


def checkout_update(access_token: str, cs_id: str, checkout: dict[str, Any], proxy_url: str, http_backend: str, timeout: int, promo_campaign_id: str) -> None:
    """步骤2：checkout/update 应用促销，使金额变 0。"""
    entity = str(checkout.get("processor_entity") or PROCESSOR_ENTITY)
    session = build_chatgpt_session(access_token, proxy_url, http_backend)
    body: dict[str, Any] = {
        "checkout_session_id": cs_id,
        "processor_entity": entity,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "billing_details": {"country": COUNTRY, "currency": CURRENCY},
        "checkout_ui_mode": "custom",
    }
    promo = str(promo_campaign_id or "").strip()
    if promo:
        body["promo_campaign"] = {"promo_campaign_id": promo, "is_coupon_from_query_param": False}
    response = session.post(
        "https://chatgpt.com/backend-api/payments/checkout/update",
        json=body,
        headers={
            "Referer": f"https://chatgpt.com/checkout/{entity}/{cs_id}",
            "x-openai-target-path": "/backend-api/payments/checkout/update",
            "x-openai-target-route": "/backend-api/payments/checkout/update",
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"checkout update failed: HTTP {response.status_code} {response.text[:500]}")


class ChatgptApproveBlocked(Exception):
    pass


class StripeRequiresApproval(Exception):
    pass


def chatgpt_approve(access_token: str, cs_id: str, checkout: dict[str, Any], proxy_url: str, http_backend: str, timeout: int) -> None:
    entity = str(checkout.get("processor_entity") or PROCESSOR_ENTITY)
    session = build_chatgpt_session(access_token, proxy_url, http_backend)
    try:
        session.post(
            "https://chatgpt.com/backend-api/sentinel/ping",
            json={},
            headers={
                "Referer": "https://chatgpt.com/",
                "x-openai-target-path": "/backend-api/sentinel/ping",
                "x-openai-target-route": "/backend-api/sentinel/ping",
            },
            timeout=timeout,
        )
    except Exception:
        pass
    response = session.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json={"checkout_session_id": cs_id, "processor_entity": entity},
        headers={
            "Referer": f"https://chatgpt.com/checkout/{entity}/{cs_id}",
            "x-openai-target-path": "/backend-api/payments/checkout/approve",
            "x-openai-target-route": "/backend-api/payments/checkout/approve",
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"chatgpt approve failed: HTTP {response.status_code} {response.text[:500]}")
    try:
        result = (response.json() or {}).get("result")
    except Exception:
        result = ""
    normalized = str(result or "").strip().lower()
    if normalized in {"blocked", "exception"}:
        raise ChatgptApproveBlocked(f"chatgpt approve retryable result: {normalized!r}")
    if result != "approved":
        raise RuntimeError(f"chatgpt approve unexpected result: {result!r}")


def chatgpt_approve_with_retry(access_token: str, cs_id: str, checkout: dict[str, Any], proxy_url: str, http_backend: str, timeout: int, max_retries: int = 6) -> None:
    last_error = ""
    for attempt in range(max_retries):
        try:
            chatgpt_approve(access_token, cs_id, checkout, proxy_url, http_backend, timeout)
            return
        except ChatgptApproveBlocked as exc:
            last_error = str(exc)
            time.sleep(min(2 + attempt * 2, 10))
        except Exception as exc:
            last_error = str(exc)
            time.sleep(2)
    raise RuntimeError(f"ChatGPT approve 连续失败: {last_error}")


# --------------------------------------------------------------------------- #
# Stripe init / payment_method / confirm
# --------------------------------------------------------------------------- #
def stripe_init(stripe: requests.Session, cs_id: str, stripe_pk: str, timeout: int, stripe_js_id: str) -> dict[str, Any]:
    browser_locale, elements_locale = PIX_LOCALE
    response = stripe.post(
        f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
        data={
            "browser_locale": browser_locale,
            "browser_timezone": "America/Sao_Paulo",
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": stripe_js_id,
            "elements_session_client[locale]": elements_locale,
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": stripe_pk,
            "_stripe_version": STRIPE_VERSION_FULL,
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"stripe init failed: HTTP {response.status_code} {response.text[:500]}")
    return response.json() or {}


def stripe_amount_info(init_payload: Any) -> tuple[str, str]:
    if not isinstance(init_payload, dict):
        return "0", "missing_payload"
    total_summary = init_payload.get("total_summary")
    if isinstance(total_summary, dict) and total_summary.get("due") is not None:
        return str(total_summary.get("due")), "total_summary.due"
    invoice = init_payload.get("invoice")
    if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
        return str(invoice.get("amount_due")), "invoice.amount_due"
    line_items = init_payload.get("line_items")
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
    return "0", "fallback_zero"


def build_ctx(init_payload: dict[str, Any], stripe_js_id: str) -> dict[str, str]:
    _browser_locale, elements_locale = PIX_LOCALE
    return {
        "stripe_js_id": stripe_js_id,
        "elements_session_id": f"elements_session_{uuid.uuid4().hex[:11]}",
        "elements_session_config_id": str(init_payload.get("config_id") or uuid.uuid4()),
        "config_id": str(init_payload.get("config_id") or ""),
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "checkout_amount": stripe_amount_info(init_payload)[0],
        "currency": str(init_payload.get("currency") or CURRENCY).lower(),
        "locale": elements_locale,
        "runtime_version": DEFAULT_STRIPE_RUNTIME_VERSION,
    }


def create_pix_payment_method(stripe: requests.Session, cs_id: str, ctx: dict[str, str], billing: dict[str, str], stripe_pk: str, timeout: int) -> str:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body = {
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[phone]": billing["phone"],
        "billing_details[address][country]": billing["country"],
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][line2]": random.choice(["", "", "", "Apto 42", "Casa 3", "Bloco B"]),
        "billing_details[address][city]": billing["city"],
        "billing_details[address][postal_code]": billing["postal_code"],
        "billing_details[address][state]": billing["state"],
        # Stripe PIX 需要巴西付款人的 tax_id（CPF/CNPJ）。
        "billing_details[tax_id]": billing.get("tax_id") or random_brazil_cpf(),
        "type": "pix",
        "payment_user_agent": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(30000, 90000)),
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
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    response = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"stripe payment_methods failed: HTTP {response.status_code} {response.text[:500]}")
    pm_id = str((response.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"stripe payment_methods bad response: {response.text[:300]}")
    return pm_id


def to_openai_pay_url(stripe_hosted_url: str) -> str:
    url = str(stripe_hosted_url or "").strip()
    if not url:
        return ""
    if url.startswith("https://checkout.stripe.com"):
        return "https://pay.openai.com" + url[len("https://checkout.stripe.com"):]
    parsed = urlsplit(url)
    if parsed.netloc.lower() == "checkout.stripe.com":
        return urlunsplit((parsed.scheme or "https", "pay.openai.com", parsed.path, parsed.query, parsed.fragment))
    return url


def stripe_confirm(stripe: requests.Session, cs_id: str, pm_id: str, stripe_pk: str, init_payload: dict[str, Any], ctx: dict[str, str], stripe_hosted_url: str, timeout: int) -> dict[str, Any]:
    return_url = to_openai_pay_url(stripe_hosted_url) or stripe_hosted_url
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    response = stripe.post(
        f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
        data={
            "guid": uuid.uuid4().hex,
            "muid": uuid.uuid4().hex,
            "sid": uuid.uuid4().hex,
            "payment_method": pm_id,
            "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
            "version": runtime_version,
            "expected_amount": str(ctx.get("checkout_amount") or stripe_amount_info(init_payload)[0]),
            "expected_payment_method_type": "pix",
            "return_url": return_url,
            "elements_session_client[session_id]": ctx["elements_session_id"],
            "elements_session_client[locale]": str(ctx.get("locale") or "pt-BR"),
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
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"stripe confirm failed: HTTP {response.status_code} {response.text[:500]}")
    return response.json() or {}



def select_pix_payment_method(page, timeout_ms: int = 15000) -> str:
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


def browser_confirm_zero_pix(
    stripe_hosted_url: str,
    proxy_url: str,
    billing: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    """通过 Stripe Hosted Checkout 建立 0 元订阅所需的 PIX recurring mandate。"""
    if sync_playwright is None:
        raise RuntimeError("0 元 PIX 强制绑定需要 playwright")
    parsed_proxy = urlsplit(str(proxy_url or "").strip())
    browser_proxy: dict[str, str] | None = None
    if parsed_proxy.hostname and parsed_proxy.port:
        browser_proxy = {
            "server": f"{parsed_proxy.scheme or 'http'}://{parsed_proxy.hostname}:{parsed_proxy.port}",
        }
        if parsed_proxy.username:
            browser_proxy["username"] = unquote(parsed_proxy.username)
        if parsed_proxy.password:
            browser_proxy["password"] = unquote(parsed_proxy.password)

    browser_timeout = max(60_000, int(timeout) * 3_000)
    executable_path = os.environ.get("PIX_CHROMIUM_PATH", "").strip() or "/usr/bin/chromium"
    with sync_playwright() as playwright:
        launch_options: dict[str, Any] = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }
        if browser_proxy:
            launch_options["proxy"] = browser_proxy
        if os.path.isfile(executable_path):
            launch_options["executable_path"] = executable_path
        browser = playwright.chromium.launch(**launch_options)
        try:
            page = browser.new_page(
                locale="pt-BR",
                timezone_id="America/Sao_Paulo",
                viewport={"width": 1365, "height": 1000},
            )
            page.set_default_timeout(browser_timeout)
            page.goto(stripe_hosted_url, wait_until="domcontentloaded", timeout=browser_timeout)
            select_pix_payment_method(page, timeout_ms=15000)
            page.locator('input[name="taxId"]').fill(billing["tax_id"])
            page.locator('input[name="billingName"]').fill(billing["name"])
            page.locator('input[name="billingAddressLine1"]').fill(billing["line1"])
            page.locator('input[name="billingAddressLine2"]').fill("Apto 42")
            page.locator('input[name="billingDependentLocality"]').fill("Bela Vista")
            page.locator('input[name="billingLocality"]').fill(billing["city"])
            page.locator("select#billingAdministrativeArea").select_option(billing["state"], force=True)
            page.locator('input[name="billingPostalCode"]').fill(billing["postal_code"])
            page.wait_for_timeout(4_000)

            def is_confirm_response(response: Any) -> bool:
                return (
                    response.request.method == "POST"
                    and urlsplit(response.url).path.endswith("/confirm")
                    and "/v1/payment_pages/" in response.url
                )

            with page.expect_response(is_confirm_response, timeout=browser_timeout) as response_info:
                page.locator('[data-testid="hosted-payment-submit-button"]').click()
            response = response_info.value
            if response.status >= 400:
                raise RuntimeError(f"Stripe 浏览器 confirm 失败: HTTP {response.status} {(response.text() or '')[:500]}")
            payload = response.json() or {}
            if not isinstance(payload, dict):
                raise RuntimeError("Stripe 浏览器 confirm 返回格式异常")
            return payload
        finally:
            browser.close()


# --------------------------------------------------------------------------- #
# PIX 结果提取
# --------------------------------------------------------------------------- #
def is_external_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def extract_pix_details(payload: Any) -> dict[str, str]:
    """从 Stripe confirm/payment_page 响应中提取 PIX 支付资料。"""
    details: dict[str, str] = {}

    def put(key: str, value: Any) -> None:
        # Stripe deferred/custom checkout 有时会先返回占位对象：
        #   {"intent_path": "next_action[pix_display_qr_code][hosted_instructions_url]"}
        # 这不是最终支付链接，不能当作成功结果；保存为 *_intent_path 仅用于诊断。
        if isinstance(value, dict):
            intent_path = str(value.get("intent_path") or "").strip()
            if intent_path and f"{key}_intent_path" not in details:
                details[f"{key}_intent_path"] = intent_path
            return
        text = str(value or "").strip()
        if text and key not in details:
            details[key] = text

    def absorb_qr_dict(item: dict[str, Any]) -> None:
        put("pix_hosted_instructions_url", item.get("hosted_instructions_url"))
        put("pix_copy_paste", item.get("data"))
        put("pix_image_url_png", item.get("image_url_png"))
        put("pix_image_url_svg", item.get("image_url_svg"))
        put("pix_expires_at", item.get("expires_at"))

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            action_type = str(value.get("type") or "").strip().lower()
            nested_qr = value.get("pix_display_qr_code") or value.get("display_pix_qr_code")
            if isinstance(nested_qr, dict):
                absorb_qr_dict(nested_qr)
            if (
                action_type in {"pix_display_qr_code", "display_pix_qr_code"}
                or any(key in value for key in ("hosted_instructions_url", "image_url_png", "image_url_svg"))
            ):
                absorb_qr_dict(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return details


def has_pix_payment_artifact(details: dict[str, str]) -> bool:
    hosted = str(details.get("pix_hosted_instructions_url") or "").strip()
    png = str(details.get("pix_image_url_png") or "").strip()
    svg = str(details.get("pix_image_url_svg") or "").strip()
    copy_paste = str(details.get("pix_copy_paste") or "").strip()
    return bool(
        (hosted and is_external_url(hosted))
        or (png and is_external_url(png))
        or (svg and is_external_url(svg))
        or (copy_paste and not copy_paste.startswith("{") and "intent_path" not in copy_paste)
    )


def pix_primary_link(details: dict[str, str]) -> str:
    for key in ("pix_hosted_instructions_url", "pix_image_url_png", "pix_image_url_svg"):
        value = str(details.get(key) or "").strip()
        if value and is_external_url(value):
            return value
    return ""


def find_submission_attempt(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        item = payload.get("submission_attempt")
        if isinstance(item, dict):
            return item
        for value in payload.values():
            found = find_submission_attempt(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_submission_attempt(value)
            if found:
                return found
    return {}


def first_value_by_key(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = first_value_by_key(value, key)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = first_value_by_key(item, key)
            if found not in (None, "", [], {}):
                return found
    return None


def stripe_failure_summary(payload: dict[str, Any], submission: dict[str, Any]) -> str:
    parts = ["state=failed"]
    for key in ("failure_code", "failure_message", "error_code", "error_message", "decline_code"):
        value = submission.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"{key}={short_error(str(value), 120)}")

    last_error = (
        submission.get("last_payment_error")
        or submission.get("payment_error")
        or first_value_by_key(payload, "last_payment_error")
        or first_value_by_key(payload, "payment_error")
        or first_value_by_key(payload, "last_setup_error")
    )
    if isinstance(last_error, dict):
        for key in ("code", "decline_code", "message", "type"):
            value = last_error.get(key)
            if value not in (None, "", [], {}):
                parts.append(f"last_error.{key}={short_error(str(value), 160)}")
    elif last_error:
        parts.append(f"last_error={short_error(str(last_error), 180)}")

    payment_methods = (
        first_value_by_key(payload, "payment_method_types")
        or first_value_by_key(payload, "automatic_payment_method_types")
    )
    if payment_methods not in (None, "", [], {}):
        parts.append(f"payment_methods={payment_methods}")
    parts.append(f"keys={sorted(payload.keys())[:12]}")
    return ", ".join(parts)


def poll_pix_result(stripe: requests.Session, cs_id: str, stripe_pk: str, ctx: dict[str, str], timeout: int, poll_seconds: int) -> dict[str, str]:
    deadline = time.time() + max(1, poll_seconds)
    _browser_locale, elements_locale = PIX_LOCALE
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[locale]": elements_locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    last_err = ""
    while time.time() < deadline:
        response = stripe.get(f"https://api.stripe.com/v1/payment_pages/{cs_id}", params=params, timeout=timeout)
        if response.status_code == 200:
            payload = response.json() or {}
            details = extract_pix_details(payload)
            if has_pix_payment_artifact(details):
                return details
            submission = find_submission_attempt(payload)
            state = str(submission.get("state") or "")
            if state == "requires_approval":
                raise StripeRequiresApproval("payment page requires ChatGPT approval")
            if state == "failed":
                raise RuntimeError(f"stripe submission failed: {stripe_failure_summary(payload, submission)}")
            last_err = f"state={state or '未知'}, keys={sorted(payload.keys())[:12]}"
        else:
            last_err = f"HTTP {response.status_code} {response.text[:120]}"
        time.sleep(1)
    raise RuntimeError(f"PIX result resolution timeout: {last_err}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def generate_pix_link(
    access_token: str,
    create_proxy_url: str = "",
    followup_proxy_url: str = "",
    approve_proxy_url: str = "",
    http_backend: str = "auto",
    promo_campaign_id: str = DEFAULT_PROMO_CAMPAIGN_ID,
    pix_mode: str = "promo_zero",
    timeout: int = 30,
    poll_seconds: int = 45,
    verbose: bool = True,
) -> dict[str, Any]:
    # 未单独配置的阶段自动继承前一阶段代理。
    create_proxy_url = str(create_proxy_url or "").strip()
    followup_proxy_url = str(followup_proxy_url or "").strip() or create_proxy_url
    approve_proxy_url = str(approve_proxy_url or "").strip() or followup_proxy_url
    pix_mode = str(pix_mode or "promo_zero").strip().lower()
    if pix_mode not in {"auto", "paid", "promo_zero"}:
        raise ValueError(f"不支持的 PIX 出链模式: {pix_mode}")

    def log(message: str) -> None:
        if verbose:
            print(message, file=sys.stderr, flush=True)

    def new_checkout() -> tuple[dict[str, Any], str, str]:
        checkout_data = create_checkout(access_token, create_proxy_url, http_backend, timeout)
        checkout_id = checkout_data["cs_id"]
        publishable_key = str(checkout_data.get("stripe_publishable_key") or DEFAULT_STRIPE_PK)
        return checkout_data, checkout_id, publishable_key

    def init_stripe(checkout_id: str, publishable_key: str) -> tuple[requests.Session, str, dict[str, Any]]:
        stripe_session = build_stripe_session(approve_proxy_url, http_backend)
        js_id = str(uuid.uuid4())
        payload = stripe_init(stripe_session, checkout_id, publishable_key, timeout, js_id)
        return stripe_session, js_id, payload

    log("[1/6] 巴西线路创建 checkout (BR / BRL, 保留 pix)")
    checkout, cs_id, stripe_pk = new_checkout()

    promo_applied = False
    if pix_mode in {"auto", "promo_zero"} and promo_campaign_id:
        log(f"[2/6] 优惠地区线路执行 checkout/update (promo={promo_campaign_id})")
        try:
            checkout_update(access_token, cs_id, checkout, followup_proxy_url, http_backend, timeout, promo_campaign_id)
            promo_applied = True
        except Exception as exc:
            if pix_mode != "auto":
                raise
            log(f"[2/6] 优惠应用失败，自动按实际金额继续: {short_error(str(exc), 160)}")
    else:
        log("[2/6] 稳定出链模式：跳过 0 元优惠")

    log("[3/6] 巴西线路执行 Stripe init")
    stripe, stripe_js_id, init_payload = init_stripe(cs_id, stripe_pk)
    stripe_amount, stripe_amount_source = stripe_amount_info(init_payload)
    log(f"[3/6] Stripe 金额: {stripe_amount} ({stripe_amount_source})")

    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(f"stripe init response missing stripe_hosted_url, keys={sorted(init_payload.keys())}")
    ctx = build_ctx(init_payload, stripe_js_id)
    billing = brazil_billing()
    pm_id = ""
    confirm_payload: dict[str, Any] | None = None
    is_zero = str(stripe_amount).strip() in {"0", "0.0", "0.00"}

    if is_zero:
        try:
            log("[4/6] 浏览器选择 PIX 并填写 CPF/巴西账单，建立 recurring mandate")
            confirm_payload = browser_confirm_zero_pix(
                stripe_hosted_url,
                approve_proxy_url,
                billing,
                timeout,
            )
            pm_id = "browser-created"
            log("[5/6] Stripe Hosted Checkout confirm 已提交")
        except Exception as exc:
            if pix_mode != "auto":
                raise RuntimeError(f"0 元 PIX 浏览器绑定失败: {exc}") from exc
            log(f"[4/6] 0 元 PIX 浏览器绑定失败，自动按实际金额回退: {short_error(str(exc), 180)}")
            checkout, cs_id, stripe_pk = new_checkout()
            promo_applied = False
            stripe, stripe_js_id, init_payload = init_stripe(cs_id, stripe_pk)
            stripe_amount, stripe_amount_source = stripe_amount_info(init_payload)
            stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
            ctx = build_ctx(init_payload, stripe_js_id)
            log(f"[3/6] 回退后 Stripe 金额: {stripe_amount} ({stripe_amount_source})")

    if confirm_payload is None:
        log("[4/6] 创建 PIX payment_method")
        pm_id = create_pix_payment_method(stripe, cs_id, ctx, billing, stripe_pk, timeout)
        log("[5/6] Stripe confirm")
        confirm_payload = stripe_confirm(stripe, cs_id, pm_id, stripe_pk, init_payload, ctx, stripe_hosted_url, timeout)

    log("[6/6] 提取/等待 PIX 支付资料")
    pix_details = extract_pix_details(confirm_payload)
    if not has_pix_payment_artifact(pix_details):
        # confirm 未直接给出结果：先 approve，再轮询 payment_page。
        try:
            log("  → 执行 ChatGPT approve...")
            chatgpt_approve_with_retry(access_token, cs_id, checkout, approve_proxy_url, http_backend, timeout)
            log("  → approve 成功，开始 poll PIX 结果...")
            pix_details = poll_pix_result(stripe, cs_id, stripe_pk, ctx, timeout, poll_seconds)
        except StripeRequiresApproval:
            chatgpt_approve_with_retry(access_token, cs_id, checkout, approve_proxy_url, http_backend, timeout)
            pix_details = poll_pix_result(stripe, cs_id, stripe_pk, ctx, timeout, poll_seconds)
        except Exception as approve_exc:
            log(f"  → approve/poll 失败: {short_error(str(approve_exc), 120)}，尝试直接 poll...")
            pix_details = poll_pix_result(stripe, cs_id, stripe_pk, ctx, timeout, poll_seconds)

    if not has_pix_payment_artifact(pix_details):
        raise RuntimeError(f"未提取到可用的 PIX 支付资料；confirm keys={sorted(confirm_payload.keys())[:12]}")

    provider_url = pix_primary_link(pix_details)
    return {
        "cs_id": cs_id,
        "payment_method": "pix",
        "promo_campaign_id": promo_campaign_id,
        "promo_applied": promo_applied,
        "pix_mode": pix_mode,
        "payment_method_id": pm_id,
        "stripe_hosted_url": stripe_hosted_url,
        "provider_redirect_url": provider_url,
        "payment_link_type": "pix_hosted_instructions" if pix_details.get("pix_hosted_instructions_url") else "pix_qr_payload",
        "long_url": provider_url or str(pix_details.get("pix_copy_paste") or "").strip(),
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        **pix_details,
    }


# --------------------------------------------------------------------------- #
# 配置 / CLI
# --------------------------------------------------------------------------- #
def load_config(path: str, explicit: bool = False) -> dict[str, Any]:
    path = str(path or "").strip()
    if not path:
        return {}
    if not os.path.exists(path):
        if explicit:
            raise FileNotFoundError(f"配置文件不存在: {path}")
        return {}
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"配置文件根节点必须是 JSON object: {path}")
    return data


def cfg_value(args: argparse.Namespace, config: dict[str, Any], key: str, default: Any = None) -> Any:
    value = getattr(args, key, None)
    if value is not None:
        return value
    return config.get(key, default)


def cfg_str(args: argparse.Namespace, config: dict[str, Any], key: str, default: str = "") -> str:
    value = cfg_value(args, config, key, default)
    return "" if value is None else str(value)


def cfg_int(args: argparse.Namespace, config: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(cfg_value(args, config, key, default))
    except Exception:
        return default


def cfg_bool(args: argparse.Namespace, config: dict[str, Any], key: str, default: bool = False) -> bool:
    value = cfg_value(args, config, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def read_access_token(access_token: str, token_file: str) -> str:
    if access_token:
        if access_token == "-":
            return extract_access_token(sys.stdin.read())
        return extract_access_token(access_token)
    if token_file:
        try:
            with open(token_file, "r", encoding="utf-8-sig") as f:
                return extract_access_token(f.read())
        except FileNotFoundError:
            pass
    env_token = (
        os.environ.get("PIX_TOKEN")
        or os.environ.get("PP_TOKEN")
        or os.environ.get("OPENAI_ACCESS_TOKEN")
        or os.environ.get("ACCESS_TOKEN")
    )
    return extract_access_token(env_token or "")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="独立提取 ChatGPT/OpenAI 巴西 PIX 0 元支付链接")
    parser.add_argument("--config", default=None, help=f"JSON 配置文件；默认自动读取 .\\{DEFAULT_CONFIG_PATH}（如果存在）")
    parser.add_argument("-t", "--access-token", default=None, help="access_token；传 '-' 表示从 stdin 读取；也可用 OPENAI_ACCESS_TOKEN 环境变量")
    parser.add_argument("--token-file", default=None, help="从文件读取 access_token/session JSON")
    parser.add_argument("--proxy", default=None, help="三个阶段共用代理，例如 http://127.0.0.1:7890 或 socks5h://127.0.0.1:1080")
    parser.add_argument("--create-proxy", default=None, help="巴西 checkout 使用的代理；未填则用 --proxy")
    parser.add_argument("--followup-proxy", default=None, help="优惠地区 checkout/update 使用的代理")
    parser.add_argument("--approve-proxy", default=None, help="巴西 Stripe/PIX/approve 使用的代理")
    parser.add_argument("--proxy-scheme", default=None, help="强制代理协议：http / socks5 / socks5h")
    parser.add_argument("--proxy-attempts", type=int, default=None, help="失败时换下一组代理重试的次数，默认 1（配置文件可覆盖）")
    parser.add_argument("--http-backend", default=None, choices=["auto", "curl_cffi", "requests"], help="HTTP 后端；代理不稳时可用 requests")
    parser.add_argument("--promo-campaign-id", default=None, help="促销 ID；默认 plus-1-month-free")
    parser.add_argument(
        "--pix-mode",
        default=None,
        choices=["auto", "paid", "promo_zero"],
        help="promo_zero=浏览器强制绑定0元PIX；auto=失败时回退实际金额；paid=直接实际金额",
    )
    parser.add_argument("--timeout", type=int, default=None, help="单请求超时秒数，默认 30")
    parser.add_argument("--poll-seconds", type=int, default=None, help="轮询 PIX 结果的最长秒数，默认 45")
    parser.add_argument("--json", dest="json", action="store_true", default=None, help="以 JSON 输出完整结果")
    parser.add_argument("--no-json", dest="json", action="store_false", help="覆盖配置文件：关闭 JSON 输出")
    parser.add_argument("-o", "--output", default=None, help="把完整结果写入 JSON 文件")
    parser.add_argument("-q", "--quiet", dest="quiet", action="store_true", default=None, help="不输出步骤日志")
    parser.add_argument("--no-quiet", dest="quiet", action="store_false", help="覆盖配置文件：开启步骤日志")
    return parser


def print_result(output: dict[str, Any], output_path: str = "") -> None:
    print("OK")
    print(f"PIX 最终支付 URL:\n{output.get('long_url')}")
    print(f"link: {output.get('long_url')}")
    print(f"type: {output.get('payment_link_type')}")
    print(f"checkout_session: {output.get('cs_id')}")
    print(f"stripe_amount: {output.get('stripe_amount')} ({output.get('stripe_amount_source')})")
    if output.get("pix_copy_paste"):
        print(f"pix_copy_paste: {output.get('pix_copy_paste')}")
    if output.get("pix_image_url_png"):
        print(f"pix_qr_png: {output.get('pix_image_url_png')}")
    if output.get("pix_image_url_svg"):
        print(f"pix_qr_svg: {output.get('pix_image_url_svg')}")
    if output.get("pix_expires_at"):
        print(f"pix_expires_at: {output.get('pix_expires_at')}")
    if output_path:
        print(f"saved: {output_path}")


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        file_config = load_config(args.config or DEFAULT_CONFIG_PATH, explicit=bool(args.config))
        config = {**env_config(), **file_config}
    except Exception as exc:
        parser.error(str(exc))

    http_backend = cfg_str(args, config, "http_backend", "auto") or "auto"
    promo = cfg_str(args, config, "promo_campaign_id", DEFAULT_PROMO_CAMPAIGN_ID) or DEFAULT_PROMO_CAMPAIGN_ID
    pix_mode = cfg_str(args, config, "pix_mode", "promo_zero") or "promo_zero"
    timeout = max(1, cfg_int(args, config, "timeout", 30))
    poll_seconds = max(1, cfg_int(args, config, "poll_seconds", 45))
    proxy_scheme = cfg_str(args, config, "proxy_scheme", "")
    proxy_attempts = cfg_int(args, config, "proxy_attempts", 1)
    quiet = cfg_bool(args, config, "quiet", False)
    as_json = cfg_bool(args, config, "json", False)
    output_path = cfg_str(args, config, "output", "")
    verbose = not quiet

    token = read_access_token(cfg_str(args, config, "access_token", ""), cfg_str(args, config, "token_file", ""))
    if not token:
        parser.error("请通过 --access-token / --token-file / OPENAI_ACCESS_TOKEN 提供 access_token")

    proxy_plans = build_proxy_plans(
        cfg_str(args, config, "proxy", ""),
        cfg_value(args, config, "proxies", []),
        cfg_str(args, config, "create_proxy", ""),
        cfg_value(args, config, "create_proxies", []),
        cfg_str(args, config, "followup_proxy", ""),
        cfg_value(args, config, "followup_proxies", []),
        cfg_str(args, config, "approve_proxy", ""),
        cfg_value(args, config, "approve_proxies", []),
        proxy_scheme,
    )
    attempts = min(max(1, proxy_attempts), max(1, len(proxy_plans)))

    result: dict[str, Any] | None = None
    last_exc: Exception | None = None

    for attempt_index in range(1, attempts + 1):
        create_proxy, followup_proxy, approve_proxy = proxy_plans[attempt_index - 1]
        try:
            if attempts > 1 and verbose:
                print(f"[代理] 第 {attempt_index}/{attempts} 组", file=sys.stderr, flush=True)
                print(
                    f"[代理] backend={http_backend}, create={mask_proxy(create_proxy)}, "
                    f"promo={mask_proxy(followup_proxy)}, provider={mask_proxy(approve_proxy)}",
                    file=sys.stderr,
                    flush=True,
                )
            result = generate_pix_link(
                token, create_proxy, followup_proxy, approve_proxy,
                http_backend, promo, pix_mode, timeout, poll_seconds, verbose,
            )
            break
        except Exception as exc:
            last_exc = exc
            if attempt_index >= attempts or not (is_retryable_proxy_error(exc) or is_retryable_provider_error(exc)):
                break
            if verbose:
                print(f"[代理] 本组代理失败，换下一组: {short_error(str(exc), 200)}", file=sys.stderr, flush=True)

    if result is None:
        error = {"ok": False, "error": str(last_exc or "未知错误")}
        if as_json:
            print(json.dumps(error, ensure_ascii=False, indent=2))
        else:
            print(f"ERROR: {error['error']}", file=sys.stderr)
        return 1

    output = {"ok": True, **result}
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

    if as_json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print_result(output, output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
