from __future__ import annotations

import base64
import html as html_lib
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from urllib.parse import quote, urlencode, urlparse, unquote

import core as _c


CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
CHECKOUT_CONFIRM_URL = "https://chatgpt.com/backend-api/payments/checkout/confirm"
CHECKOUT_APPROVE_URL = "https://chatgpt.com/backend-api/payments/checkout/approve"
STRIPE_INIT_URL = "https://api.stripe.com/v1/payment_pages/{checkout_session_id}/init"
STRIPE_CONFIRM_URL = "https://api.stripe.com/v1/payment_pages/{checkout_session_id}/confirm"
STRIPE_PAGE_URL = "https://api.stripe.com/v1/payment_pages/{checkout_session_id}"
STRIPE_ELEMENTS_SESSIONS_URL = "https://api.stripe.com/v1/elements/sessions"
STRIPE_VERSION = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
STRIPE_JS_SDK_VERSION = "3eeb60efc5"
STRIPE_RV_TS = "2024-01-01 00:00:00 -0000"
STRIPE_RV = "3eeb60efc554e1de356807017990ea438f6b156a"
STRIPE_SV = "971bc6188a741072452a935de1be7526fa781f1e88e8adb8447145c67b902767"
DEFAULT_APPROVAL_ATTEMPTS = 60
MAX_APPROVAL_ATTEMPTS = 80
DEFAULT_POLL_ATTEMPTS = 30
UPI_DEFAULT_COUNTRY = "IN"
UPI_DEFAULT_CURRENCY = "INR"
UPI_SUPPORTED_LOCALES = {
    "en": "en",
    "en-us": "en",
    "english": "en",
    "hi": "hi",
    "hi-in": "hi",
    "hindi": "hi",
}


class UpiQrUnavailableError(RuntimeError):
    pass


class NoFreeTrialError(RuntimeError):
    pass


class PaymentMethodUnavailableError(RuntimeError):
    pass


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(float(str(os.environ.get(name, default)).strip()))
    except Exception:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _normalize_proxy(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return _c.normalize_proxy_url(text, default_scheme="http")
    except Exception:
        return text


def normalize_upi_region(value: str = "") -> tuple[str, str]:
    """Normalize the explicit India/INR region used by Stripe UPI."""
    country = str(value or UPI_DEFAULT_COUNTRY).strip().upper()
    if country != UPI_DEFAULT_COUNTRY:
        raise ValueError("UPI region must be IN (India)")
    return UPI_DEFAULT_COUNTRY, UPI_DEFAULT_CURRENCY


def normalize_upi_locale(value: str = "") -> str:
    raw = str(value or "en").strip().lower().replace("_", "-")
    locale = UPI_SUPPORTED_LOCALES.get(raw)
    if not locale:
        raise ValueError("UPI payment locale must be English (en) or Hindi (hi)")
    return locale


def normalize_upi_payment_email(value: str = "") -> str:
    email = str(value or "").strip()
    if not email:
        return f"upi-{uuid.uuid4().hex[:12]}@example.com"
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("UPI payment email format is invalid")
    return email


def _mask_headers(headers: dict | None) -> dict:
    clean = {}
    for key, value in (headers or {}).items():
        low = str(key).lower()
        if low == "authorization":
            text = str(value or "")
            clean[key] = text[:15] + "..." if len(text) > 15 else "..."
        elif low == "cookie":
            clean[key] = "[obfuscated cookies]"
        else:
            clean[key] = value
    return clean


def _debug_log(step: str, url: str, method: str, headers: dict | None, body, status: int, text: str) -> None:
    try:
        try:
            response_body = json.loads(text)
        except Exception:
            response_body = str(text or "")[:4000]
        entry = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "step": step,
            "url": url,
            "method": method,
            "request_headers": _mask_headers(headers),
            "request_body": body,
            "response_status": status,
            "response_body": response_body,
        }
        with open(os.path.join(os.path.dirname(__file__), "debug_trace.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _request(method: str, url: str, *, headers: dict | None = None, data=None, json_body=None,
             proxy_url: str = "", step: str = "UPI_HTTP") -> tuple[int, str, object]:
    proxy_url = _normalize_proxy(proxy_url)
    session = _c.opll_new_http_session()
    if hasattr(session, "trust_env"):
        session.trust_env = False if proxy_url else True
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    body_for_log = json_body if json_body is not None else data
    try:
        response = session.request(
            method.upper(),
            url,
            headers=headers or {},
            data=data,
            json=json_body,
            timeout=_c.PAY_LONG_LINK_TIMEOUT,
        )
        status = int(response.status_code)
        text = response.text
        final_url = str(getattr(response, "url", "") or "")
    except Exception as exc:
        status, text = 599, str(exc)
        final_url = ""
    try:
        parsed = json.loads(text) if text else None
    except Exception:
        parsed = None
    if parsed is None and final_url and final_url != url:
        parsed = {"_final_url": final_url, "_raw_text": text}
    _debug_log(step, url, method.upper(), headers or {}, body_for_log, status, text)
    return status, text, parsed if parsed is not None else text


def _chatgpt_headers(token: str, cookie: str = "", referer: str = "https://chatgpt.com/",
                     target_path: str = "", target_route: str = "") -> dict:
    cookie = _c.opll_normalize_chatgpt_cookie(cookie)
    device_id = _c.opll_cookie_value(cookie, "oai-did") or str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": referer,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": _c.DEFAULT_USER_AGENT,
        "oai-device-id": device_id,
        "oai-language": "zh-CN",
        "OAI-Language": "zh-CN",
    }
    if cookie:
        headers["Cookie"] = cookie
    if target_path:
        headers["X-OpenAI-Target-Path"] = target_path
    if target_route:
        headers["X-OpenAI-Target-Route"] = target_route
        headers["OAI-Chat-Web-Route"] = target_route
    return headers


def _stripe_headers(checkout_session_id: str = "") -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://js.stripe.com",
        "Referer": f"https://js.stripe.com/v3/elements-inner-payment-{quote(str(checkout_session_id), safe='')}.html"
        if checkout_session_id else "https://js.stripe.com/",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "User-Agent": _c.DEFAULT_USER_AGENT,
    }


def _checkout_payload(country: str = UPI_DEFAULT_COUNTRY,
                      currency: str = UPI_DEFAULT_CURRENCY) -> dict:
    payload = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "checkout_ui_mode": "custom",
        "cancel_url": "https://chatgpt.com/#pricing",
    }
    promo = str(os.environ.get("CHATGPT_UPI_PROMO_CAMPAIGN_ID") or "plus-1-month-free").strip()
    if promo:
        payload["promo_campaign"] = {"promo_campaign_id": promo, "is_coupon_from_query_param": False}
    return payload


def _create_checkout(token: str, proxy_url: str, cookie: str,
                     country: str = UPI_DEFAULT_COUNTRY,
                     currency: str = UPI_DEFAULT_CURRENCY) -> dict:
    payload = _checkout_payload(country, currency)
    headers = _chatgpt_headers(token, cookie, "https://chatgpt.com/",
                               "/backend-api/payments/checkout", "/backend-api/payments/checkout")
    status, text, data = _request("POST", CHECKOUT_URL, headers=headers, json_body=payload,
                                  proxy_url=proxy_url, step="UPI_CHATGPT_CHECKOUT")
    if status < 200 or status >= 300:
        raise RuntimeError(f"Create UPI checkout failed: HTTP {status} {_c.opll_short_error(text, 700)}")
    if not isinstance(data, dict):
        raise RuntimeError(f"Create UPI checkout bad response: {_c.opll_short_error(text, 700)}")
    cs_id = str(data.get("checkout_session_id") or data.get("session_id") or data.get("cs_id") or data.get("id") or "").strip()
    public_key = str(data.get("publishable_key") or data.get("public_key") or "").strip() or _c.opll_extract_stripe_publishable_key(data)
    if not cs_id or not cs_id.startswith("cs_"):
        raise RuntimeError(f"checkout response missing cs_id: {_c.opll_short_error(str(data), 700)}")
    if not public_key:
        raise RuntimeError(f"checkout response missing Stripe publishable key: {_c.opll_short_error(str(data), 700)}")
    entity = str(_c.opll_extract_processor_entity(data) or data.get("processor_entity") or "openai_llc").strip() or "openai_llc"
    hosted_url = str(data.get("url") or data.get("stripe_hosted_url") or data.get("checkout_url") or "").strip()
    if not hosted_url:
        hosted_url = f"https://chatgpt.com/checkout/{entity}/{cs_id}"
    return {
        "cs_id": cs_id,
        "processor_entity": entity,
        "stripe_publishable_key": public_key,
        "billing_country": country,
        "currency": currency,
        "chatgpt_checkout_url": hosted_url,
        "raw_checkout": data,
    }


def _add_elements_params(params: dict, stripe_js_id: str, locale: str = "en", elements_session_id: str = "") -> None:
    params["elements_session_client[client_betas][0]"] = "custom_checkout_server_updates_1"
    params["elements_session_client[client_betas][1]"] = "custom_checkout_manual_approval_1"
    params["elements_session_client[elements_init_source]"] = "custom_checkout"
    params["elements_session_client[referrer_host]"] = "chatgpt.com"
    if elements_session_id:
        params["elements_session_client[session_id]"] = elements_session_id
    params["elements_session_client[stripe_js_id]"] = stripe_js_id
    params["elements_session_client[locale]"] = locale
    params["elements_session_client[is_aggregation_expected]"] = "false"
    params["elements_options_client[saved_payment_method][enable_save]"] = "auto"
    params["elements_options_client[saved_payment_method][enable_redisplay]"] = "auto"


def _stripe_form_request(method: str, url: str, form: dict | None, proxy_url: str,
                         checkout_session_id: str, step: str) -> tuple[int, str, object]:
    body = urlencode(form or {}) if form is not None else None
    return _request(method, url, headers=_stripe_headers(checkout_session_id), data=body,
                    proxy_url=proxy_url, step=step)


def _stripe_init(checkout_session_id: str, public_key: str, proxy_url: str,
                 stripe_js_id: str, locale: str = "en",
                 browser_timezone: str = "Asia/Shanghai",
                 step_prefix: str = "UPI") -> tuple[int, str, object]:
    url = STRIPE_INIT_URL.format(checkout_session_id=quote(checkout_session_id, safe=""))
    form = {
        "browser_locale": locale,
        "browser_timezone": browser_timezone,
        "key": public_key,
        "_stripe_version": STRIPE_VERSION,
    }
    _add_elements_params(form, stripe_js_id, locale)
    return _stripe_form_request(
        "POST", url, form, proxy_url, checkout_session_id,
        f"{step_prefix}_STRIPE_INIT_CUSTOM",
    )


def _nested(data, path: list[str]):
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _pix_context_snapshot(data) -> dict:
    if not isinstance(data, dict):
        return {}
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
    address = customer.get("address") if isinstance(customer.get("address"), dict) else {}
    tax_context = data.get("tax_context") if isinstance(data.get("tax_context"), dict) else {}
    account_settings = data.get("account_settings") if isinstance(data.get("account_settings"), dict) else {}
    return {
        "geocoding_country": _nested(data, ["geocoding", "country_code"]) or "",
        "customer_country": address.get("country") or "",
        "tax_country": tax_context.get("customer_tax_country") or "",
        "merchant_country": account_settings.get("merchant_of_record_country") or account_settings.get("country") or "",
        "customer_email": customer.get("email") or data.get("customer_email") or "",
        "customer_name": customer.get("name") or customer.get("individual_name") or "",
    }


def _pix_context_has_non_br(data) -> bool:
    ctx = _pix_context_snapshot(data)
    return any(
        str(ctx.get(key) or "").strip().upper() not in ("", "BR")
        for key in ("customer_country", "tax_country")
    )


def _pix_context_is_br(data) -> bool:
    """Both authoritative Stripe customer and tax countries must be Brazil."""
    ctx = _pix_context_snapshot(data)
    return all(
        str(ctx.get(key) or "").strip().upper() == "BR"
        for key in ("customer_country", "tax_country")
    )


def _amount_minor(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return int(round(float(value)))
        except Exception:
            return None
    if isinstance(value, str):
        try:
            return int(round(float(value.strip())))
        except Exception:
            return None
    if isinstance(value, dict):
        for key in ("amount", "amount_due", "minor", "value"):
            found = _amount_minor(value.get(key))
            if found is not None:
                return found
    return None


def _payment_amount(data) -> int:
    return (
        _amount_minor(_nested(data, ["total_summary", "due"]))
        or _amount_minor(_nested(data, ["invoice", "amount_due"]))
        or _amount_minor(_nested(data, ["elements_options", "amount"]))
        or 0
    )


def _payment_amount_maybe(data) -> int | None:
    for candidate in (
        _nested(data, ["total_summary", "due"]),
        _nested(data, ["invoice", "amount_due"]),
        _nested(data, ["elements_options", "amount"]),
        data.get("amount_due") if isinstance(data, dict) else None,
    ):
        found = _amount_minor(candidate)
        if found is not None:
            return found
    return None


def _payment_methods(data) -> list[str]:
    specs = data.get("payment_method_specs") if isinstance(data, dict) else None
    spec_types = (
        [item.get("type") for item in specs if isinstance(item, dict)]
        if isinstance(specs, list)
        else None
    )
    candidates = [
        _nested(data, ["elements_options", "payment_method_types"]),
        data.get("payment_method_types") if isinstance(data, dict) else None,
        _nested(data, ["payment_method_preference", "ordered_payment_method_types"]),
        _nested(data, ["payment_method_preference", "payment_method_types"]),
        _nested(data, ["session", "payment_method_types"]),
        data.get("ordered_payment_method_types") if isinstance(data, dict) else None,
        data.get("ordered_payment_method_types_and_wallets") if isinstance(data, dict) else None,
        spec_types,
    ]
    for candidate in candidates:
        if isinstance(candidate, list) and candidate:
            return [str(x).strip().lower() for x in candidate if str(x).strip()]
    return []


def _scan_trial(value, depth: int = 0, signals: dict | None = None) -> dict:
    if signals is None:
        signals = {"couponName": "", "percentOff": None, "durationMonths": None}
    if depth > 8 or value is None:
        return signals
    if isinstance(value, list):
        for item in value:
            _scan_trial(item, depth + 1, signals)
        return signals
    if not isinstance(value, dict):
        return signals
    for key, item in value.items():
        low_key = str(key).lower()
        if isinstance(item, str):
            low_val = item.lower()
            if not signals["couponName"] and (
                "free trial" in low_val or "1 month free" in low_val or
                "one month free" in low_val or "plus-1-month-free" in low_val or
                "coupon" in low_key or "promotion" in low_key
            ):
                signals["couponName"] = item
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            if low_key in ("percent_off", "percentoff"):
                signals["percentOff"] = max(float(signals.get("percentOff") or 0), float(item))
            if low_key in ("duration_in_months", "durationmonths"):
                signals["durationMonths"] = max(float(signals.get("durationMonths") or 0), float(item))
        if isinstance(item, (dict, list)):
            _scan_trial(item, depth + 1, signals)
    return signals


def _sum_amounts(value) -> int:
    if not isinstance(value, list):
        return 0
    return sum((_amount_minor(item) or 0) for item in value)


def _discount_amount(data) -> int:
    direct = 0
    for candidate in (
        _nested(data, ["total_summary", "discount"]),
        _nested(data, ["total_summary", "total_discount_amount"]),
        _nested(data, ["invoice", "discount_amount"]),
        _nested(data, ["invoice", "total_discount_amount"]),
    ):
        direct = max(direct, _amount_minor(candidate) or 0)
    return max(
        direct,
        _sum_amounts(_nested(data, ["total_discount_amounts"])),
        _sum_amounts(_nested(data, ["invoice", "total_discount_amounts"])),
    )


def _subtotal_amount(data) -> int | None:
    for candidate in (
        _nested(data, ["total_summary", "subtotal"]),
        _nested(data, ["invoice", "subtotal"]),
        _nested(data, ["invoice", "amount_subtotal"]),
        _nested(data, ["elements_options", "amount"]),
    ):
        found = _amount_minor(candidate)
        if found is not None:
            return found
    return None


def _free_trial_status(data) -> dict:
    due = _payment_amount_maybe(data)
    discount = _discount_amount(data)
    signals = _scan_trial(data)
    methods = _payment_methods(data)
    coupon = str(signals.get("couponName") or "").strip()
    coupon_lower = coupon.lower()
    coupon_trial = any(x in coupon_lower for x in ("free trial", "1 month free", "one month free", "plus-1-month-free"))
    full_discount = (signals.get("percentOff") is not None and float(signals["percentOff"]) >= 100) or coupon_trial
    return {
        "hasFreeTrial": due == 0 or (discount > 0 and full_discount),
        "hasUpi": "upi" in methods,
        "due": due,
        "subtotal": _subtotal_amount(data),
        "discountAmount": discount,
        "couponName": coupon,
        "percentOff": signals.get("percentOff"),
        "durationMonths": signals.get("durationMonths"),
        "paymentMethodTypes": methods,
    }


def _xor_b64(value: str) -> str:
    padded = value + (" " * ((3 - (len(value) % 3)) % 3))
    xored = bytes([(5 ^ ord(ch)) & 0xFF for ch in padded])
    return quote(base64.b64encode(xored).decode("ascii"), safe="")


def _shift_printable(value: str, offset: int = 11) -> str:
    return "".join(chr(((ord(ch) - 32 + offset) % 95) + 32) for ch in value)


def _stripe_checksum(identifier: str) -> str:
    return _shift_printable(_xor_b64(json.dumps({"id": identifier}, separators=(",", ":"))), 11)


def _rv_timestamp() -> str:
    raw = json.dumps({"rvTs": STRIPE_RV_TS, "rv": STRIPE_RV, "sv": STRIPE_SV}, separators=(",", ":"))
    return _shift_printable(_xor_b64(raw), 11)


def _config_id(data) -> str:
    return str((data or {}).get("config_id") or _nested(data, ["elements_options", "__checkout_config_id"]) or "").strip() if isinstance(data, dict) else ""


def _unknown_param(data) -> str:
    error = data.get("error") if isinstance(data, dict) and isinstance(data.get("error"), dict) else data
    if not isinstance(error, dict) or str(error.get("code") or "") != "parameter_unknown":
        return ""
    return str(error.get("param") or "").strip()


def _first_text(*values, default: str = "") -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return default


def _customer_billing(init_data, payment_email: str = "",
                      billing_profile: dict | None = None) -> dict:
    """Pull billing fields from the live checkout page data before falling back.

    The fixed test email showed up in failed setup attempts. The hosted site path
    tends to keep billing/contact data aligned with the checkout customer, so the
    default here follows the page data and only falls back to the old static data.
    """
    supplied = billing_profile if isinstance(billing_profile, dict) else {}
    customer = init_data.get("customer") if isinstance(init_data, dict) and isinstance(init_data.get("customer"), dict) else {}
    address = customer.get("address") if isinstance(customer.get("address"), dict) else {}
    mode = str(os.environ.get("CHATGPT_UPI_CONFIRM_EMAIL_MODE") or "fixed").strip().lower()
    customer_email = _first_text(
        supplied.get("email"),
        customer.get("email"),
        init_data.get("customer_email") if isinstance(init_data, dict) else "",
        init_data.get("email") if isinstance(init_data, dict) else "",
    )
    # A country-specific profile is authoritative.  The previous ordering fell
    # through to normalize_upi_payment_email() when PIX built its tax form,
    # replacing the generated BR address email with upi-...@example.com.
    email = (
        str(supplied.get("email") or "").strip()
        or str(payment_email or "").strip()
        or (customer_email if mode == "customer" and customer_email else "")
        or normalize_upi_payment_email("")
    )
    country = _first_text(
        supplied.get("country"), address.get("country"), default="IN"
    ).upper()
    tax_id = re.sub(r"\D", "", str(supplied.get("tax_id") or ""))
    if country == "BR" and not tax_id:
        tax_id = _c.opll_generate_valid_br_cpf()
    return {
        "name": _first_text(supplied.get("name"), customer.get("name"), customer.get("individual_name"), default="Rahul Sharma"),
        "email": email,
        "phone": _first_text(supplied.get("phone"), customer.get("phone"), default=""),
        "line1": _first_text(supplied.get("line1"), address.get("line1"), default="Flat 302, Sai Residency"),
        "line2": _first_text(supplied.get("line2"), address.get("line2"), default=""),
        "city": _first_text(supplied.get("city"), address.get("city"), default="Mumbai"),
        "state": _first_text(supplied.get("state"), address.get("state"), default="Maharashtra"),
        "postal_code": _first_text(supplied.get("postal_code"), address.get("postal_code"), default="400069"),
        "country": country,
        "tax_id": tax_id,
    }


def _upi_terminal_failure(data) -> str:
    if not isinstance(data, dict):
        return ""
    paths = (
        ("setup_intent", "last_setup_error"),
        ("submission_attempt", "error"),
        ("last_setup_error",),
        ("error",),
    )
    parts: list[str] = []
    for path in paths:
        node = data
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            continue
        code = str(node.get("code") or "").strip()
        decline = str(node.get("decline_code") or "").strip()
        payment_error = node.get("payment_error") if isinstance(node.get("payment_error"), dict) else {}
        pcode = str(payment_error.get("code") or "").strip()
        pdecline = str(payment_error.get("decline_code") or "").strip()
        for item in (code, decline, pcode, pdecline):
            if item and item not in parts:
                parts.append(item)
    setup_status = str(_nested(data, ["setup_intent", "status"]) or "").strip()
    submission_state = str(_nested(data, ["submission_attempt", "state"]) or "").strip()
    if setup_status == "requires_payment_method" and ("setup_attempt_failed" in parts or "generic_decline" in parts):
        return "/".join(parts + [setup_status])
    if submission_state == "failed" and parts:
        return "/".join(parts + [submission_state])
    return ""


def _tax_region_form(public_key: str, stripe_js_id: str,
                     elements_session_id: str = "", locale: str = "en",
                     billing_profile: dict | None = None) -> dict:
    billing = _customer_billing({}, billing_profile=billing_profile)
    form = {
        "tax_region[country]": billing["country"],
        "tax_region[postal_code]": billing["postal_code"],
        "tax_region[state]": billing["state"],
        "tax_region[city]": billing["city"],
        "tax_region[line1]": billing["line1"],
        "tax_region[line2]": billing["line2"],
        "key": public_key,
        "_stripe_version": STRIPE_VERSION,
    }
    # Stripe payment_pages/{cs_id} accepts the tax_region group for PIX.  Its
    # live response rejects billing_details/customer_details as unknown grouped
    # parameters.  KAKAO 2.0 already uses this tax_region-only shape.
    if str(billing.get("country") or "").upper() != "BR":
        form.update({
            "billing_details[name]": billing["name"],
            "billing_details[email]": billing["email"],
            "billing_details[address][country]": billing["country"],
            "billing_details[address][postal_code]": billing["postal_code"],
            "billing_details[address][state]": billing["state"],
            "billing_details[address][city]": billing["city"],
            "billing_details[address][line1]": billing["line1"],
            "billing_details[address][line2]": billing["line2"],
            "customer_details[email]": billing["email"],
            "customer_details[name]": billing["name"],
            "customer_details[address][country]": billing["country"],
            "customer_details[address][postal_code]": billing["postal_code"],
            "customer_details[address][state]": billing["state"],
            "customer_details[address][city]": billing["city"],
            "customer_details[address][line1]": billing["line1"],
            "customer_details[address][line2]": billing["line2"],
        })
    if not str(billing.get("line2") or "").strip():
        form.pop("tax_region[line2]", None)
        form.pop("billing_details[address][line2]", None)
        form.pop("customer_details[address][line2]", None)
    _add_elements_params(form, stripe_js_id, locale, elements_session_id)
    return form


def _tax_update(checkout_session_id: str, public_key: str, proxy_url: str,
                stripe_js_id: str, elements_session_id: str = "",
                locale: str = "en",
                billing_profile: dict | None = None,
                step_prefix: str = "UPI") -> tuple[int, str, object]:
    url = STRIPE_PAGE_URL.format(checkout_session_id=quote(checkout_session_id, safe=""))
    form = _tax_region_form(
        public_key, stripe_js_id, elements_session_id, locale, billing_profile
    )
    status, text, data = _stripe_form_request(
        "POST", url, form, proxy_url, checkout_session_id,
        f"{step_prefix}_STRIPE_TAX_UPDATE",
    )
    for _ in range(10):
        unknown = _unknown_param(data)
        if not (status >= 400 and unknown):
            break
        removed = False
        for key in list(form):
            if key == unknown or key.startswith(f"{unknown}["):
                form.pop(key, None)
                removed = True
        if not removed:
            break
        status, text, data = _stripe_form_request(
            "POST", url, form, proxy_url, checkout_session_id,
            f"{step_prefix}_STRIPE_TAX_UPDATE_RETRY",
        )
    return status, text, data


def _confirm_form(init_data, public_key: str, checkout_session_id: str, stripe_js_id: str,
                  processor_entity: str, elements_session_id: str = "",
                  locale: str = "en", payment_email: str = "",
                  billing_profile: dict | None = None,
                  payment_method_type: str = "upi",
                  payment_method_id: str = "") -> dict:
    billing = _customer_billing(
        init_data if isinstance(init_data, dict) else {},
        payment_email=payment_email,
        billing_profile=billing_profile,
    )
    hosted_url = ""
    if isinstance(init_data, dict):
        hosted_url = _first_text(
            init_data.get("stripe_hosted_url"),
            init_data.get("stripeHostedUrl"),
            init_data.get("url"),
        )
    method_upper = str(payment_method_type or "upi").strip().upper()
    return_url_mode = str(
        os.environ.get(f"CHATGPT_{method_upper}2_RETURN_URL_MODE")
        or os.environ.get("CHATGPT_UPI_RETURN_URL_MODE")
        or "hosted"
    ).strip().lower()
    return_url = hosted_url if return_url_mode == "hosted" and hosted_url else f"https://chatgpt.com/checkout/{processor_entity}/{checkout_session_id}"
    form = {
        "guid": uuid.uuid4().hex,
        "muid": uuid.uuid4().hex,
        "sid": uuid.uuid4().hex,
        "payment_method_data[billing_details][name]": billing["name"],
        "payment_method_data[billing_details][email]": billing["email"],
        "payment_method_data[billing_details][address][line1]": billing["line1"],
        "payment_method_data[billing_details][address][line2]": billing["line2"],
        "payment_method_data[billing_details][address][city]": billing["city"],
        "payment_method_data[billing_details][address][state]": billing["state"],
        "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
        "payment_method_data[billing_details][address][country]": billing["country"],
        "payment_method_data[type]": payment_method_type,
        "payment_method_data[payment_user_agent]": f"stripe.js/{STRIPE_JS_SDK_VERSION}; stripe-js-v3/{STRIPE_JS_SDK_VERSION}; payment-element; deferred-intent",
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(random.randint(25000, 55000)),
        "expected_amount": str(_payment_amount(init_data)),
        "expected_payment_method_type": payment_method_type,
        "mandate_data[customer_acceptance][type]": "online",
        "mandate_data[customer_acceptance][online][infer_from_client]": "true",
        "version": STRIPE_JS_SDK_VERSION,
        "js_checksum": _stripe_checksum(str(init_data.get("id") if isinstance(init_data, dict) and init_data.get("id") else checkout_session_id)),
        "rv_timestamp": _rv_timestamp(),
        "return_url": return_url,
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": checkout_session_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "key": public_key,
        "_stripe_version": STRIPE_VERSION,
    }
    if method_upper == "PIX":
        # PIX billing details (including CPF/CNPJ) belong to the preceding
        # /v1/payment_methods request. payment_pages/{cs_id}/confirm consumes
        # that pm_ id and rejects an inline top-level billing_details group.
        form.pop("mandate_data[customer_acceptance][type]", None)
        form.pop("mandate_data[customer_acceptance][online][infer_from_client]", None)
    if payment_method_id:
        for key in list(form):
            if key.startswith("payment_method_data["):
                form.pop(key, None)
        form["payment_method"] = str(payment_method_id)
    if not str(billing.get("line2") or "").strip():
        form.pop("payment_method_data[billing_details][address][line2]", None)
    if isinstance(init_data, dict) and init_data.get("init_checksum"):
        form["init_checksum"] = str(init_data.get("init_checksum"))
    config_id = _config_id(init_data)
    if config_id:
        form["client_attribution_metadata[checkout_config_id]"] = config_id
    _add_elements_params(form, stripe_js_id, locale, elements_session_id)
    if elements_session_id:
        form["client_attribution_metadata[elements_session_id]"] = elements_session_id
        form["client_attribution_metadata[elements_session_config_id]"] = config_id or elements_session_id
    return form


def _pix_payment_method_form(init_data, public_key: str, checkout_session_id: str,
                             stripe_js_id: str, elements_session_id: str,
                             billing_profile: dict, locale: str = "pt-BR") -> dict:
    """Build the dedicated Stripe PIX PaymentMethod request.

    Stripe requires the CPF/CNPJ at billing_details[tax_id] while creating the
    pm_ object. The later payment-page confirm must reference that object.
    """
    billing = _customer_billing(
        init_data if isinstance(init_data, dict) else {},
        payment_email=str((billing_profile or {}).get("email") or ""),
        billing_profile=billing_profile,
    )
    config_id = _config_id(init_data)
    form = {
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[phone]": billing["phone"],
        "billing_details[address][country]": "BR",
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][line2]": billing["line2"],
        "billing_details[address][city]": billing["city"],
        "billing_details[address][postal_code]": billing["postal_code"],
        "billing_details[address][state]": billing["state"],
        "billing_details[tax_id]": billing["tax_id"],
        "type": "pix",
        "payment_user_agent": f"stripe.js/{STRIPE_JS_SDK_VERSION}; stripe-js-v3/{STRIPE_JS_SDK_VERSION}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(30000, 90000)),
        "client_attribution_metadata[checkout_session_id]": checkout_session_id,
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[checkout_config_id]": config_id,
        "client_attribution_metadata[elements_session_id]": elements_session_id,
        "client_attribution_metadata[elements_session_config_id]": config_id or elements_session_id,
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": public_key,
        "_stripe_version": STRIPE_VERSION,
    }
    if not str(billing.get("phone") or "").strip():
        form.pop("billing_details[phone]", None)
    if not str(billing.get("line2") or "").strip():
        form.pop("billing_details[address][line2]", None)
    return form


def _create_pix_payment_method(checkout_session_id: str, public_key: str,
                               init_data, proxy_url: str, stripe_js_id: str,
                               elements_session_id: str, billing_profile: dict,
                               locale: str = "pt-BR") -> tuple[int, str, object, str]:
    form = _pix_payment_method_form(
        init_data,
        public_key,
        checkout_session_id,
        stripe_js_id,
        elements_session_id,
        billing_profile,
        locale,
    )
    url = "https://api.stripe.com/v1/payment_methods"
    status, text, data = _stripe_form_request(
        "POST", url, form, proxy_url, checkout_session_id,
        "PIX2_STRIPE_PAYMENT_METHOD",
    )
    for _ in range(6):
        unknown = _unknown_param(data)
        if not (status >= 400 and unknown and unknown in form):
            break
        form.pop(unknown, None)
        status, text, data = _stripe_form_request(
            "POST", url, form, proxy_url, checkout_session_id,
            "PIX2_STRIPE_PAYMENT_METHOD_RETRY",
        )
    payment_method_id = str(data.get("id") or "").strip() if isinstance(data, dict) else ""
    return status, text, data, payment_method_id


def _stripe_confirm(checkout_session_id: str, public_key: str, init_data, proxy_url: str,
                    stripe_js_id: str, processor_entity: str,
                    elements_session_id: str = "", locale: str = "en",
                    payment_email: str = "",
                    billing_profile: dict | None = None,
                    payment_method_type: str = "upi",
                    step_prefix: str = "UPI",
                    payment_method_id: str = "") -> tuple[int, str, object]:
    url = STRIPE_CONFIRM_URL.format(checkout_session_id=quote(checkout_session_id, safe=""))
    form = _confirm_form(
        init_data,
        public_key,
        checkout_session_id,
        stripe_js_id,
        processor_entity,
        elements_session_id,
        locale,
        payment_email,
        billing_profile,
        payment_method_type,
        payment_method_id,
    )
    status, text, data = _stripe_form_request(
        "POST", url, form, proxy_url, checkout_session_id,
        f"{step_prefix}_STRIPE_CONFIRM",
    )
    for _ in range(5):
        unknown = _unknown_param(data)
        if not (status >= 400 and unknown):
            break
        if unknown in form:
            form.pop(unknown, None)
        else:
            prefix = unknown + "["
            removed = False
            for key in list(form.keys()):
                if key.startswith(prefix):
                    form.pop(key, None)
                    removed = True
            if not removed:
                break
        status, text, data = _stripe_form_request(
            "POST", url, form, proxy_url, checkout_session_id,
            f"{step_prefix}_STRIPE_CONFIRM_RETRY",
        )
    return status, text, data


def _approval_text(data) -> str:
    return str(data.get("result") or "").strip().lower() if isinstance(data, dict) else ""


def _approval_ok(status: int, data) -> bool:
    return status < 400 and _approval_text(data) == "approved"


def _post_checkout_action(token: str, checkout_session_id: str, processor_entity: str,
                          proxy_url: str, cookie: str, url: str, payload: dict, name: str) -> dict:
    referer = f"https://chatgpt.com/checkout/{processor_entity}/{checkout_session_id}"
    # Match gpt-upi-main exactly: checkout action calls use the checkout page
    # route as OpenAI target metadata, not the backend API path.  Using the API
    # path here still returns HTTP 200, but significantly increases
    # business-level result='blocked' in practice.
    target_path = f"/checkout/{processor_entity}/{checkout_session_id}"
    target_route = "/checkout/[processorEntity]/[checkoutSessionId]"
    headers = _chatgpt_headers(token, cookie, referer, target_path, target_route)
    status, text, data = _request("POST", url, headers=headers, json_body=payload,
                                  proxy_url=proxy_url, step=f"UPI_CHATGPT_{name.upper()}")
    return {"status": status, "text": text, "data": data, "name": name}


def _chatgpt_approval(token: str, checkout_session_id: str, processor_entity: str,
                      proxy_url: str, cookie: str, submission_attempt_id: str = "",
                      progress_callback=None,
                      payment_method_type: str = "upi") -> dict:
    confirm = _post_checkout_action(
        token, checkout_session_id, processor_entity, proxy_url, cookie,
        CHECKOUT_CONFIRM_URL,
        {"checkout_session_id": checkout_session_id,
         "selected_payment_method_type": payment_method_type},
        "confirm",
    )
    if _approval_ok(int(confirm["status"]), confirm["data"]):
        confirm["attemptStatuses"] = []
        return confirm
    method_upper = str(payment_method_type or "upi").strip().upper()
    attempts = _int_env(
        f"CHATGPT_{method_upper}2_APPROVAL_ATTEMPTS",
        _int_env("CHATGPT_UPI_APPROVAL_ATTEMPTS", DEFAULT_APPROVAL_ATTEMPTS, 1, MAX_APPROVAL_ATTEMPTS),
        1,
        MAX_APPROVAL_ATTEMPTS,
    )
    statuses: list[int] = []
    blocked_fast_fail = _int_env(
        f"CHATGPT_{method_upper}2_BLOCKED_FAST_FAIL",
        _int_env("CHATGPT_UPI_BLOCKED_FAST_FAIL", 5, 0, MAX_APPROVAL_ATTEMPTS),
        0,
        MAX_APPROVAL_ATTEMPTS,
    )
    blocked_count = 0
    last = confirm
    for attempt in range(1, attempts + 1):
        payload = {"checkout_session_id": checkout_session_id, "processor_entity": processor_entity}
        if submission_attempt_id:
            payload["submission_attempt_id"] = submission_attempt_id
        approve = _post_checkout_action(
            token, checkout_session_id, processor_entity, proxy_url, cookie,
            CHECKOUT_APPROVE_URL, payload, f"approve_{attempt}" if attempts > 1 else "approve",
        )
        statuses.append(int(approve["status"]))
        approve["attemptStatuses"] = list(statuses)
        last = approve
        result_text = _approval_text(approve["data"])
        _c._emit_payment_stage(
            progress_callback,
            "chatgpt_approval",
            f"ChatGPT approve {attempt}/{attempts}: {result_text or approve['status']}",
            5,
            7,
            approval_attempt=attempt,
            approval_attempts=attempts,
            approval_result=result_text,
        )
        if _approval_ok(int(approve["status"]), approve["data"]):
            return approve
        if result_text == "blocked":
            blocked_count += 1
            if blocked_fast_fail and blocked_count >= blocked_fast_fail:
                approve["fastFailReason"] = f"blocked x{blocked_count} on same proxy"
                return approve
        else:
            blocked_count = 0
        time.sleep(0.15)
    return last


def _payment_page_get(checkout_session_id: str, public_key: str, proxy_url: str,
                      stripe_js_id: str, elements_session_id: str = "",
                      locale: str = "en", step_prefix: str = "UPI") -> tuple[int, str, object]:
    base = STRIPE_PAGE_URL.format(checkout_session_id=quote(checkout_session_id, safe=""))
    params = {"key": public_key, "_stripe_version": STRIPE_VERSION}
    _add_elements_params(params, stripe_js_id, locale, elements_session_id)
    url = f"{base}?{urlencode(params)}"
    return _request("GET", url, headers=_stripe_headers(checkout_session_id),
                    proxy_url=proxy_url,
                    step=f"{step_prefix}_STRIPE_PAYMENT_PAGE_GET")


def _clean_urlish(value: str) -> str:
    text = html_lib.unescape(str(value or "").strip())
    text = text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    text = text.replace("\\u0026", "&").replace("\\u003d", "=").replace("&amp;", "&")
    try:
        text = unquote(text)
    except Exception:
        pass
    return text.strip(" \t\r\n\"'<>),;]")


def _merge_url_patterns(result: dict, value: str) -> None:
    text = _clean_urlish(value)
    if not text:
        return
    patterns = (
        ("hostedInstructionsUrl", r"https://payments\.stripe\.com/upi/instructions/[^\s\"'<>\\)]+"),
        ("stripeHostedUrl", r"https://checkout\.stripe\.com/c/pay/[^\s\"'<>\\)]+"),
        ("qrImageUrlPng", r"https://qr\.stripe\.com/[^\s\"'<>\\)]+\.png(?:\?[^\s\"'<>\\)]*)?"),
        ("qrImageUrlSvg", r"https://qr\.stripe\.com/[^\s\"'<>\\)]+\.svg(?:\?[^\s\"'<>\\)]*)?"),
        ("upiUri", r"upi://[^\s\"'<>]+"),
    )
    for out_key, pattern in patterns:
        if result.get(out_key):
            continue
        for match in re.finditer(pattern, text, re.I):
            val = _clean_urlish(match.group(0))
            if not val:
                continue
            result.setdefault(out_key, val)
            if out_key == "upiUri":
                result.setdefault("mobileAuthUrl", val)
            break


def merge_qr_key(result: dict, key: str, value) -> None:
    if value is None:
        return
    normalized = str(key or "").lower()
    if isinstance(value, str):
        val = _clean_urlish(value)
        low_val = val.lower()
        if val.startswith("upi://") and not result.get("upiUri"):
            result["upiUri"] = val
            result["mobileAuthUrl"] = val
        elif val.startswith("https://payments.stripe.com/upi/instructions/") and not result.get("hostedInstructionsUrl"):
            result["hostedInstructionsUrl"] = val
        elif val.startswith("https://checkout.stripe.com/c/pay/") and not result.get("stripeHostedUrl"):
            result["stripeHostedUrl"] = val
        elif val.startswith("https://qr.stripe.com/") and "svg" in low_val and not result.get("qrImageUrlSvg"):
            result["qrImageUrlSvg"] = val
        elif val.startswith("https://qr.stripe.com/") and "png" in low_val and not result.get("qrImageUrlPng"):
            result["qrImageUrlPng"] = val
        elif "upi://" in val and not result.get("upiUri"):
            match = re.search(r"upi://[^\s\"'<>]+", val)
            if match:
                result["upiUri"] = _clean_urlish(match.group(0))
                result["mobileAuthUrl"] = result["upiUri"]
        _merge_url_patterns(result, val)
    if normalized in {
        "hosted_instructions_url", "mobile_auth_url", "upi_uri",
        "image_url_svg", "qr_image_url_svg", "image_url_png", "qr_image_url_png",
        "stripe_hosted_url",
    } and isinstance(value, str) and value.strip():
        out_key = (
            "qrImageUrlSvg" if normalized in {"image_url_svg", "qr_image_url_svg"}
            else "qrImageUrlPng" if normalized in {"image_url_png", "qr_image_url_png"}
            else "hostedInstructionsUrl" if normalized == "hosted_instructions_url"
            else "mobileAuthUrl" if normalized == "mobile_auth_url"
            else "stripeHostedUrl" if normalized == "stripe_hosted_url"
            else "upiUri"
        )
        result.setdefault(out_key, _clean_urlish(value))
    if normalized in {"expires_at", "expires_after_timestamp", "qr_expires_at"}:
        expires_at = _c.opll_parse_epoch_seconds(value)
        if expires_at and not result.get("expiresAt"):
            result["expiresAt"] = expires_at


def extract_upi_next_action(data) -> dict:
    result: dict = {}

    def walk(value, key: str = ""):
        merge_qr_key(result, key, value)
        if isinstance(value, list):
            for item in value:
                walk(item, "")
            return
        if not isinstance(value, dict):
            return
        for child_key, child_value in value.items():
            if child_key == "qr_code" and isinstance(child_value, dict):
                merge_qr_key(result, "qr_expires_at", child_value.get("expires_at"))
                merge_qr_key(result, "image_url_svg", child_value.get("image_url_svg"))
                merge_qr_key(result, "image_url_png", child_value.get("image_url_png"))
            walk(child_value, str(child_key))

    walk(data, "")
    return result


def _decode_payload_b64(value: str):
    text = str(value or "").replace("&quot;", '"').replace("-", "+").replace("_", "/")
    try:
        text += "=" * ((4 - (len(text) % 4)) % 4)
        return json.loads(base64.b64decode(text).decode("utf-8"))
    except Exception:
        return None


def _extract_from_hosted_html(html: str) -> dict:
    result: dict = {}
    text = _clean_urlish(str(html or ""))
    _merge_url_patterns(result, text)
    meta = re.search(r"<meta\b(?=[^>]*\bid=[\"']payload[\"'])(?=[^>]*\bdata-message=[\"']([^\"']+)[\"'])[^>]*>", text, re.I | re.S)
    if meta:
        payload = _decode_payload_b64(meta.group(1))
        if isinstance(payload, dict):
            result.update({k: v for k, v in extract_upi_next_action(payload).items() if v})
    for match in re.finditer(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>", text, re.I | re.S):
        src = _clean_urlish(match.group(1) or "")
        tag = match.group(0) or ""
        if "qr.stripe.com" in src or "QRCode-image" in tag:
            merge_qr_key(result, "qr_image_url_png" if "png" in src.lower() else "qr_image_url_svg", src)
            break
    for match in re.finditer(r"upi://[^\s\"'<>]+", text):
        merge_qr_key(result, "upi_uri", match.group(0))
        break
    # Stripe embeds data in JSON blobs and escaped JS strings; URL regexes above
    # catch most cases after unescaping, but also scan common explicit fields.
    for key in ("hosted_instructions_url", "mobile_auth_url", "upi_uri", "image_url_png", "image_url_svg", "stripe_hosted_url"):
        for match in re.finditer(r"[\"']" + re.escape(key) + r"[\"']\s*:\s*[\"']([^\"']+)[\"']", text, re.I):
            merge_qr_key(result, key, match.group(1))
    return result


def _playwright_paths() -> tuple[str, str, str]:
    home = os.path.expanduser("~")
    runtime_root = os.path.join(home, ".cache", "codex-runtimes", "codex-primary-runtime", "dependencies")
    node = os.path.join(runtime_root, "node", "bin", "node.exe")
    node_modules = os.path.join(runtime_root, "node", "node_modules")
    pnpm_modules = os.path.join(node_modules, ".pnpm", "node_modules")
    if not os.path.exists(node):
        node = shutil.which("node") or shutil.which("node.exe") or ""
    return node, node_modules, pnpm_modules


def _playwright_hydrate_qr(qr_data: dict, proxy_url: str, locale: str = "en") -> dict:
    """Open hosted/instructions URL in headless Chromium and capture network URLs.

    This is a late fallback for the successful-site pattern: the frontend result
    is often just URLs observed while the hosted Stripe page loads
    (payments.stripe.com/upi/instructions, qr.stripe.com image, checkout URL).
    """
    if not _bool_env("CHATGPT_UPI_PLAYWRIGHT_HYDRATE", True):
        return {}
    start_url = str((qr_data or {}).get("hostedInstructionsUrl") or (qr_data or {}).get("stripeHostedUrl") or "").strip()
    if not start_url:
        return {}
    node, node_modules, pnpm_modules = _playwright_paths()
    if not node or not os.path.exists(node):
        return {}
    script = r"""
const fs = require('fs');
const Module = require('module');
if (process.env.NODE_PATH) Module._initPaths();
const { chromium } = require('playwright');

function clean(v) {
  return String(v || '').replace(/\\\//g, '/').replace(/\\u002F/gi, '/').replace(/\\u0026/gi, '&').trim();
}
function add(found, text) {
  text = clean(text);
  if (!text) return;
  const pats = [
    ['hostedInstructionsUrl', /https:\/\/payments\.stripe\.com\/upi\/instructions\/[^\s"'<>\\)]+/ig],
    ['stripeHostedUrl', /https:\/\/checkout\.stripe\.com\/c\/pay\/[^\s"'<>\\)]+/ig],
    ['qrImageUrlPng', /https:\/\/qr\.stripe\.com\/[^\s"'<>\\)]+\.png(?:\?[^\s"'<>\\)]*)?/ig],
    ['qrImageUrlSvg', /https:\/\/qr\.stripe\.com\/[^\s"'<>\\)]+\.svg(?:\?[^\s"'<>\\)]*)?/ig],
    ['upiUri', /upi:\/\/[^\s"'<>]+/ig],
  ];
  for (const [key, re] of pats) {
    let m;
    while ((m = re.exec(text))) {
      if (!found[key]) found[key] = clean(m[0]);
    }
  }
}
function proxyCfg(proxyUrl) {
  if (!proxyUrl) return undefined;
  try {
    const u = new URL(proxyUrl);
    let proto = u.protocol.replace(':', '').toLowerCase();
    if (proto === 'socks5h') proto = 'socks5';
    const cfg = { server: `${proto}://${u.hostname}:${u.port}` };
    if (u.username) cfg.username = decodeURIComponent(u.username);
    if (u.password) cfg.password = decodeURIComponent(u.password);
    return cfg;
  } catch { return undefined; }
}

(async () => {
  const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const found = {};
  add(found, input.url);
  const launch = { headless: true, args: ['--disable-blink-features=AutomationControlled', '--no-sandbox'] };
  const pxy = proxyCfg(input.proxy || '');
  if (pxy) launch.proxy = pxy;
  const browser = await chromium.launch(launch);
  const page = await browser.newPage({
    locale: input.locale || 'en-US',
    userAgent: input.userAgent || undefined,
    viewport: { width: 1365, height: 900 },
    extraHTTPHeaders: { 'accept-language': input.locale === 'hi-IN' ? 'hi-IN,hi;q=0.9,en;q=0.8' : 'en-US,en;q=0.9' },
  });
  page.on('request', req => add(found, req.url()));
  page.on('response', async res => {
    const url = res.url();
    add(found, url);
    const ct = String(res.headers()['content-type'] || '').toLowerCase();
    if (/json|text|html|javascript/.test(ct) || /payment_pages|instructions|checkout|stripe/.test(url)) {
      try { add(found, await res.text()); } catch {}
    }
  });
  page.on('framenavigated', frame => add(found, frame.url()));
  try {
    await page.goto(input.url, { waitUntil: 'domcontentloaded', timeout: input.timeoutMs || 25000 });
  } catch (e) {
    found.playwrightError = String(e && e.message || e);
  }
  try { await page.waitForTimeout(input.waitMs || 12000); } catch {}
  try { add(found, await page.content()); } catch {}
  found.finalUrl = page.url();
  await browser.close();
  fs.writeFileSync(process.argv[3], JSON.stringify(found));
})().catch(err => {
  fs.writeFileSync(process.argv[3], JSON.stringify({ playwrightError: String(err && err.stack || err) }));
  process.exit(0);
});
"""
    env = os.environ.copy()
    node_path = os.pathsep.join([p for p in (node_modules, pnpm_modules, env.get("NODE_PATH", "")) if p])
    env["NODE_PATH"] = node_path
    payload = {
        "url": start_url,
        "proxy": proxy_url,
        "userAgent": _c.DEFAULT_USER_AGENT,
        "locale": "hi-IN" if locale == "hi" else "en-US",
        "timeoutMs": _int_env("CHATGPT_UPI_PLAYWRIGHT_TIMEOUT_MS", 25000, 5000, 60000),
        "waitMs": _int_env("CHATGPT_UPI_PLAYWRIGHT_WAIT_MS", 12000, 1000, 60000),
    }
    with tempfile.TemporaryDirectory(prefix="upi_pw_") as td:
        script_path = os.path.join(td, "capture.js")
        input_path = os.path.join(td, "input.json")
        output_path = os.path.join(td, "output.json")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        try:
            proc = subprocess.run(
                [node, script_path, input_path, output_path],
                cwd=os.getcwd(),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=max(10, int(payload["timeoutMs"] / 1000) + int(payload["waitMs"] / 1000) + 20),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            return {"playwrightError": str(exc)}
        out: dict = {}
        try:
            if os.path.exists(output_path):
                with open(output_path, "r", encoding="utf-8") as f:
                    parsed = json.load(f)
                    if isinstance(parsed, dict):
                        out.update(parsed)
        except Exception as exc:
            out["playwrightError"] = str(exc)
        if proc.stderr and not out.get("playwrightError"):
            err = proc.stderr.strip()
            if err:
                out["playwrightStderr"] = err[-800:]
        return out


def _hydrate_qr(qr_data: dict, proxy_url: str, locale: str = "en") -> dict:
    out = dict(qr_data or {})
    seen: set[str] = set()
    for _round in range(4):
        if out.get("upiUri") and (out.get("qrImageUrlPng") or out.get("qrImageUrlSvg")):
            break
        candidates: list[tuple[str, str, str]] = []
        stripe_hosted_url = str(out.get("stripeHostedUrl") or "").strip()
        hosted_url = str(out.get("hostedInstructionsUrl") or "").strip()
        if stripe_hosted_url:
            candidates.append((stripe_hosted_url, "UPI_STRIPE_HOSTED_HTML", "https://chatgpt.com/"))
        if hosted_url:
            candidates.append((hosted_url, "UPI_HOSTED_INSTRUCTIONS_HTML", "https://js.stripe.com/"))
        changed = False
        for candidate_url, step, referer in candidates:
            candidate_url = _clean_urlish(candidate_url)
            if not candidate_url or candidate_url in seen:
                continue
            seen.add(candidate_url)
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": referer,
                "User-Agent": _c.DEFAULT_USER_AGENT,
            }
            status, html, parsed = _request("GET", candidate_url, headers=headers, proxy_url=proxy_url, step=step)
            if isinstance(parsed, dict):
                final_url = _clean_urlish(str(parsed.get("_final_url") or ""))
                if final_url:
                    before = dict(out)
                    merge_qr_key(out, "hosted_instructions_url" if "payments.stripe.com/upi/instructions/" in final_url else "stripe_hosted_url", final_url)
                    changed = changed or before != out
            if 200 <= status < 400:
                before = dict(out)
                out.update({k: v for k, v in _extract_from_hosted_html(html).items() if v})
                changed = changed or before != out
        if not candidates or not changed:
            break
    # A successful response often exposes only the instructions URL first.
    # Continue hydration until the independent link fields needed by the result
    # view have had a chance to populate.
    needs_browser_hydration = bool(
        out.get("hostedInstructionsUrl") or out.get("stripeHostedUrl")
    ) and (
        not out.get("upiUri")
        or not (out.get("qrImageUrlSvg") or out.get("qrImageUrlPng"))
        or not out.get("stripeHostedUrl")
    )
    if needs_browser_hydration or not (
        out.get("upiUri")
        or out.get("hostedInstructionsUrl")
        or out.get("qrImageUrlSvg")
        or out.get("qrImageUrlPng")
    ):
        before = dict(out)
        out.update({
            k: v
            for k, v in _playwright_hydrate_qr(out, proxy_url, locale).items()
            if v
        })
        if before != out:
            for key, value in list(out.items()):
                merge_qr_key(out, key, value)
    return out


def _qr_dict(qr_data: dict) -> dict:
    upi_uri = str(qr_data.get("upiUri") or qr_data.get("mobileAuthUrl") or "").strip()
    png = str(qr_data.get("qrImageUrlPng") or "").strip()
    svg = str(qr_data.get("qrImageUrlSvg") or "").strip()
    hosted = str(qr_data.get("hostedInstructionsUrl") or "").strip()
    stripe_hosted = str(qr_data.get("stripeHostedUrl") or "").strip()
    expires_at = _c.opll_parse_epoch_seconds(qr_data.get("expiresAt"))
    return {
        "qr_data": upi_uri,
        "qr_image_url": svg or png,
        "qr_image_url_png": png,
        "qr_image_url_svg": svg,
        "qr_image_data_url": _c.opll_make_qr_data_url(upi_uri) if upi_uri else "",
        "qr_hosted_instructions_url": hosted,
        "stripe_hosted_url": stripe_hosted,
        "qr_expires_at": expires_at,
        "qr_valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
    }


def _elements_session_optional(checkout_session_id: str, public_key: str, amount: int,
                               proxy_url: str, stripe_js_id: str,
                               locale: str = "en", currency: str = "inr",
                               payment_method_type: str = "upi",
                               step_prefix: str = "UPI",
                               return_payload: bool = False):
    method_upper = str(payment_method_type or "upi").strip().upper()
    default_enabled = True if method_upper == "PIX" else _bool_env("CHATGPT_UPI_EXPERIMENT_ELEMENTS_SESSION", False)
    enabled = _bool_env(
        f"CHATGPT_{method_upper}2_EXPERIMENT_ELEMENTS_SESSION",
        default_enabled,
    )
    if not enabled:
        return ("", {}) if return_payload else ""
    params = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(amount),
        "deferred_intent[currency]": currency,
        "deferred_intent[setup_future_usage]": "off_session",
        "deferred_intent[payment_method_types][0]": "card",
        "deferred_intent[payment_method_types][1]": "link",
        "deferred_intent[payment_method_types][2]": payment_method_type,
        "currency": currency,
        "key": public_key,
        "_stripe_version": STRIPE_VERSION,
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": stripe_js_id,
        "locale": locale,
        "type": "deferred_intent",
        "checkout_session_id": checkout_session_id,
    }
    headers = {
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": _c.DEFAULT_USER_AGENT,
    }
    status, _, data = _request("GET", f"{STRIPE_ELEMENTS_SESSIONS_URL}?{urlencode(params)}",
                               headers=headers, proxy_url=proxy_url,
                               step=f"{step_prefix}_STRIPE_ELEMENTS_SESSION")
    session_id = str(_nested(data, ["session_id"]) or "").strip() if isinstance(data, dict) else ""
    if status >= 400:
        return ("", {}) if return_payload else ""
    payload = data if isinstance(data, dict) else {}
    return (session_id, payload) if return_payload else session_id


def _confirm_and_extract(token: str, checkout: dict, provider_proxy_url: str,
                         cookie: str, progress_callback=None,
                         payment_locale: str = "en",
                         payment_email: str = "") -> dict:
    cs_id = str(checkout.get("cs_id") or "").strip()
    public_key = str(checkout.get("stripe_publishable_key") or "").strip()
    entity = str(checkout.get("processor_entity") or "openai_llc").strip() or "openai_llc"
    billing_profile = (
        dict(checkout.get("_upi_billing_profile") or {})
        if isinstance(checkout.get("_upi_billing_profile"), dict)
        else _c.opll_generate_in_profile(payment_email)
    )
    if payment_email:
        billing_profile["email"] = payment_email
    steps: list[dict] = []
    stripe_js_id = uuid.uuid4().hex

    _c._emit_payment_stage(progress_callback, "stripe_init", "Stripe custom init", 2, 7)
    init_status, init_text, init_data = _stripe_init(
        cs_id, public_key, provider_proxy_url, stripe_js_id, payment_locale
    )
    steps.append({"name": "stripe_init_custom", "status": init_status})
    if init_status >= 400 or not isinstance(init_data, dict) or init_data.get("error"):
        raise RuntimeError(f"Stripe custom init failed: HTTP {init_status} {_c.opll_short_error(init_text, 700)}")

    trial = _free_trial_status(init_data)
    steps.append({"name": "free_trial_check", "status": 200 if trial["hasFreeTrial"] else 402, "state": trial})
    if trial["paymentMethodTypes"] and not trial["hasUpi"]:
        raise PaymentMethodUnavailableError("PAYMENT_METHOD_UNAVAILABLE:UPI: available_payment_method_types=" + ",".join(trial["paymentMethodTypes"]))
    if _bool_env("CHATGPT_UPI_REQUIRE_FREE_TRIAL", True) and not trial["hasFreeTrial"]:
        raise NoFreeTrialError("NO_FREE_TRIAL: This account does not have the free trial offer. Please use another account.")

    elements_session_id = _elements_session_optional(
        cs_id,
        public_key,
        _payment_amount(init_data),
        provider_proxy_url,
        stripe_js_id,
        payment_locale,
    )
    if elements_session_id:
        steps.append({"name": "stripe_elements_session", "status": 200, "state": "has_session_id"})

    _c._emit_payment_stage(progress_callback, "stripe_tax_update", "Stripe tax region -> India(IN)", 3, 7)
    tax_status, tax_text, tax_data = _tax_update(
        cs_id,
        public_key,
        provider_proxy_url,
        stripe_js_id,
        elements_session_id,
        payment_locale,
        billing_profile,
    )
    steps.append({"name": "stripe_update_tax_region", "status": tax_status})
    if tax_status >= 400 or not isinstance(tax_data, dict) or tax_data.get("error"):
        raise RuntimeError(f"Stripe tax region update failed: HTTP {tax_status} {_c.opll_short_error(tax_text, 700)}")

    _c._emit_payment_stage(progress_callback, "stripe_confirm", "Stripe confirm UPI payment method", 4, 7)
    confirm_status, confirm_text, confirm_data = _stripe_confirm(
        cs_id,
        public_key,
        tax_data,
        provider_proxy_url,
        stripe_js_id,
        entity,
        elements_session_id,
        payment_locale,
        payment_email,
        billing_profile,
    )
    submission_attempt_id = str(_nested(confirm_data, ["submission_attempt", "id"]) or "").strip() if isinstance(confirm_data, dict) else ""
    steps.append({
        "name": "stripe_confirm_upi",
        "status": confirm_status,
        "state": _nested(confirm_data, ["submission_attempt", "state"]) if isinstance(confirm_data, dict) else "",
        "result": "submission_attempt_id" if submission_attempt_id else "",
    })
    if confirm_status >= 400 or not isinstance(confirm_data, dict) or confirm_data.get("error"):
        raise RuntimeError(f"Stripe UPI confirm failed: HTTP {confirm_status} {_c.opll_short_error(confirm_text, 700)}")
    terminal_reason = _upi_terminal_failure(confirm_data)
    if terminal_reason:
        steps[-1]["terminalReason"] = terminal_reason

    _c._emit_payment_stage(progress_callback, "chatgpt_approval", "ChatGPT checkout confirm/approve", 5, 7)
    approval = _chatgpt_approval(token, cs_id, entity, provider_proxy_url, cookie, submission_attempt_id, progress_callback)
    approval_status = int(approval.get("status") or 0)
    approval_data = approval.get("data")
    steps.append({
        "name": f"chatgpt_checkout_{approval.get('name') or 'approval'}",
        "status": approval_status,
        "result": _approval_text(approval_data),
        "attemptStatuses": approval.get("attemptStatuses") or [],
    })
    if not _approval_ok(approval_status, approval_data):
        attempts = "/".join(str(x) for x in (approval.get("attemptStatuses") or []))
        fast_fail = approval.get("fastFailReason")
        extra = f" approve_attempts={attempts}" if attempts else ""
        if fast_fail:
            extra += f" fast_fail={fast_fail}"
        raise UpiQrUnavailableError(f"ChatGPT approval returned abnormal result: HTTP {approval_status} {_c.opll_short_error(str(approval_data), 700)}.{extra}")

    qr_data: dict = {}
    for source in (init_data, tax_data, confirm_data, approval_data):
        qr_data.update({k: v for k, v in extract_upi_next_action(source).items() if v})

    _c._emit_payment_stage(progress_callback, "stripe_poll_qr", "Poll Stripe for UPI QR", 6, 7)
    statuses: list[int] = []
    for attempt in range(_int_env("CHATGPT_UPI_POLL_ATTEMPTS", DEFAULT_POLL_ATTEMPTS, 1, 90)):
        if qr_data.get("upiUri") or qr_data.get("hostedInstructionsUrl") or qr_data.get("qrImageUrlSvg") or qr_data.get("qrImageUrlPng"):
            break
        if attempt > 0:
            time.sleep(1.0)
        page_status, _, page_data = _payment_page_get(
            cs_id,
            public_key,
            provider_proxy_url,
            stripe_js_id,
            elements_session_id,
            payment_locale,
        )
        statuses.append(page_status)
        qr_data.update({k: v for k, v in extract_upi_next_action(page_data).items() if v})
        page_terminal = _upi_terminal_failure(page_data)
        if page_terminal:
            terminal_reason = page_terminal
            break
        if page_status >= 400:
            break
    if statuses:
        step = {"name": "stripe_payment_page_get", "status": statuses[-1], "attemptStatuses": statuses}
        if terminal_reason:
            step["terminalReason"] = terminal_reason
        steps.append(step)

    if not (qr_data.get("upiUri") or qr_data.get("hostedInstructionsUrl") or qr_data.get("qrImageUrlSvg") or qr_data.get("qrImageUrlPng")):
        refresh_status, _, refresh_data = _stripe_init(
            cs_id, public_key, provider_proxy_url, stripe_js_id, payment_locale
        )
        steps.append({"name": "stripe_init_refresh", "status": refresh_status})
        qr_data.update({k: v for k, v in extract_upi_next_action(refresh_data).items() if v})
        refresh_terminal = _upi_terminal_failure(refresh_data)
        if refresh_terminal:
            terminal_reason = refresh_terminal
            steps[-1]["terminalReason"] = terminal_reason

    _c._emit_payment_stage(progress_callback, "hydrate_qr", "Hydrate UPI hosted instructions", 7, 7)
    qr = _qr_dict(_hydrate_qr(qr_data, provider_proxy_url, payment_locale))
    if not (qr["qr_data"] or qr["qr_image_url"] or qr["qr_hosted_instructions_url"]):
        step_text = " -> ".join(f"{s.get('name')}:{s.get('status')}" for s in steps)
        terminal_text = f" Terminal: {terminal_reason}." if terminal_reason else ""
        hosted_text = f" Hosted: {qr.get('stripe_hosted_url')}." if qr.get("stripe_hosted_url") else ""
        raise UpiQrUnavailableError(f"No UPI QR data found after approval.{terminal_text}{hosted_text} Steps: {step_text or 'none'}")
    return {
        "qr": qr,
        "steps": steps,
        "expected_amount": str(_payment_amount(tax_data)),
        "payment_intent_status": str(_nested(tax_data, ["payment_intent", "status"]) or _nested(confirm_data, ["payment_intent", "status"]) or ""),
        "billing_email": payment_email,
        "payment_locale": payment_locale,
        "billing_profile": billing_profile,
    }


def _confirm_and_extract_pix(token: str, checkout: dict, provider_proxy_url: str,
                             cookie: str, progress_callback=None,
                             payment_locale: str = "pt-BR") -> dict:
    """UPI 2.0 _confirm_and_extract flow mirrored for Brazil PIX."""
    cs_id = str(checkout.get("cs_id") or "").strip()
    public_key = str(checkout.get("stripe_publishable_key") or "").strip()
    entity = str(checkout.get("processor_entity") or "openai_llc").strip() or "openai_llc"
    billing_profile = (
        dict(checkout.get("_pix_billing_profile") or {})
        if isinstance(checkout.get("_pix_billing_profile"), dict)
        else _c.opll_brazil_pix_billing(token)
    )
    payment_email = str(billing_profile.get("email") or "").strip()
    steps: list[dict] = []
    stripe_js_id = uuid.uuid4().hex
    browser_timezone = "America/Sao_Paulo"

    _c._emit_payment_stage(progress_callback, "stripe_init", "PIX 2.0: Stripe custom init", 2, 7)
    init_status, init_text, init_data = _stripe_init(
        cs_id,
        public_key,
        provider_proxy_url,
        stripe_js_id,
        payment_locale,
        browser_timezone=browser_timezone,
        step_prefix="PIX2",
    )
    steps.append({"name": "stripe_init_custom", "status": init_status})
    if init_status >= 400 or not isinstance(init_data, dict) or init_data.get("error"):
        raise RuntimeError(
            f"Stripe PIX custom init failed: HTTP {init_status} "
            f"{_c.opll_short_error(init_text, 700)}"
        )

    trial = _free_trial_status(init_data)
    methods = _payment_methods(init_data)
    trial["hasPix"] = "pix" in methods
    steps.append({
        "name": "free_trial_check",
        "status": 200 if trial["hasFreeTrial"] else 402,
        "state": trial,
        "stripeContext": _pix_context_snapshot(init_data),
    })
    # KAKAO/TWINT/PromptPay 2.0 establish the local billing/tax region before
    # deciding that a local payment method is absent.  A reused ChatGPT customer
    # can still carry IN/US billing here even when Stripe geocoding is BR, so the
    # pre-tax card/link list is diagnostic only for PIX.
    if methods and not trial["hasPix"]:
        steps[-1]["preTaxPaymentMethodTypes"] = methods
        steps[-1]["deferredPixCheckUntilAfterBrTax"] = True
    if _bool_env("CHATGPT_PIX2_REQUIRE_FREE_TRIAL", True) and not trial["hasFreeTrial"]:
        raise NoFreeTrialError(
            "NO_FREE_TRIAL: This account does not have the zero-BRL PIX trial offer."
        )

    if _pix_context_has_non_br(init_data):
        try:
            billing_sync = _c.opll_chatgpt_checkout_sync_billing(
                token,
                checkout,
                provider_proxy_url,
                billing_profile=billing_profile,
                chatgpt_cookie=cookie,
            )
            steps.append({
                "name": "chatgpt_pix_billing_sync_after_init",
                "status": 200,
                "state": billing_sync,
            })
        except Exception as exc:
            steps.append({
                "name": "chatgpt_pix_billing_sync_after_init",
                "status": 206,
                "error": _c.opll_short_error(str(exc), 300),
            })
        try:
            tax_sync = _c.opll_chatgpt_update_pix_taxes(
                token,
                checkout,
                provider_proxy_url,
                billing=billing_profile,
                chatgpt_cookie=cookie,
            )
            steps.append({
                "name": "chatgpt_pix_tax_sync_after_init",
                "status": 200,
                "state": tax_sync,
            })
        except Exception as exc:
            steps.append({
                "name": "chatgpt_pix_tax_sync_after_init",
                "status": 206,
                "error": _c.opll_short_error(str(exc), 300),
            })

    elements_result = _elements_session_optional(
        cs_id,
        public_key,
        _payment_amount(init_data),
        provider_proxy_url,
        stripe_js_id,
        payment_locale,
        currency="brl",
        payment_method_type="pix",
        step_prefix="PIX2",
        return_payload=True,
    )
    if isinstance(elements_result, tuple):
        elements_session_id = str(elements_result[0] or "").strip()
        elements_payload = (
            elements_result[1] if isinstance(elements_result[1], dict) else {}
        )
    else:
        # Compatibility with tests/extensions that return the legacy ID only.
        elements_session_id = str(elements_result or "").strip()
        elements_payload = {}
    elements_country = str(
        _nested(elements_payload, ["payment_method_preference", "country_code"])
        or _nested(elements_payload, ["country_code"])
        or ""
    ).strip().upper()
    elements_methods = _payment_methods(elements_payload)
    elements_has_pix = elements_country == "BR" and "pix" in elements_methods
    if elements_session_id:
        steps.append({
            "name": "stripe_elements_session",
            "status": 200,
            "state": {
                "hasSessionId": True,
                "countryCode": elements_country,
                "paymentMethodTypes": elements_methods,
                "hasPix": elements_has_pix,
            },
        })

    _c._emit_payment_stage(progress_callback, "stripe_tax_update", "PIX 2.0: Stripe tax region -> Brazil(BR)", 3, 7)
    tax_status, tax_text, tax_data = _tax_update(
        cs_id,
        public_key,
        provider_proxy_url,
        stripe_js_id,
        elements_session_id,
        payment_locale,
        billing_profile,
        step_prefix="PIX2",
    )
    steps.append({"name": "stripe_update_tax_region", "status": tax_status})
    if tax_status >= 400 or not isinstance(tax_data, dict) or tax_data.get("error"):
        raise RuntimeError(
            f"Stripe PIX tax region update failed: HTTP {tax_status} "
            f"{_c.opll_short_error(tax_text, 700)}"
        )

    tax_trial = _free_trial_status(tax_data)
    tax_methods = _payment_methods(tax_data)
    tax_trial["hasPix"] = "pix" in tax_methods or elements_has_pix
    tax_trial["pixActivationSource"] = (
        "elements_session_br" if elements_has_pix else "payment_page_tax"
    )
    tax_trial["elementsPaymentMethodTypes"] = elements_methods
    tax_trial["paymentPagePaymentMethodTypes"] = tax_methods
    steps[-1]["state"] = tax_trial
    steps[-1]["stripeContext"] = _pix_context_snapshot(tax_data)
    if _bool_env("CHATGPT_PIX2_REQUIRE_FREE_TRIAL", True) and not tax_trial["hasFreeTrial"]:
        raise NoFreeTrialError(
            "NO_FREE_TRIAL: BR tax synchronization did not keep the zero-BRL offer."
        )

    if not tax_trial["hasPix"]:
        _c._emit_payment_stage(
            progress_callback,
            "pix_br_tax_refresh",
            "PIX 2.0: synchronize ChatGPT BR taxes and refresh Stripe methods",
            4,
            7,
        )
        chatgpt_tax_result = {}
        billing_update_result = {}
        try:
            billing_update_result = _c.opll_chatgpt_checkout_sync_billing(
                token,
                checkout,
                provider_proxy_url,
                billing_profile=billing_profile,
                chatgpt_cookie=cookie,
            )
        except Exception as exc:
            billing_update_result = {
                "error": _c.opll_short_error(str(exc), 300),
            }
        steps.append({
            "name": "chatgpt_pix_checkout_billing_sync",
            "status": 200 if not billing_update_result.get("error") else 206,
            "state": billing_update_result,
        })
        try:
            chatgpt_tax_result = _c.opll_chatgpt_update_pix_taxes(
                token,
                checkout,
                provider_proxy_url,
                billing=billing_profile,
                chatgpt_cookie=cookie,
            )
        except Exception as exc:
            chatgpt_tax_result = {
                "error": _c.opll_short_error(str(exc), 300),
            }
        steps.append({
            "name": "chatgpt_pix_taxes",
            "status": 200 if not chatgpt_tax_result.get("error") else 206,
            "state": chatgpt_tax_result,
        })

        retry_tax_status, retry_tax_text, retry_tax_data = _tax_update(
            cs_id,
            public_key,
            provider_proxy_url,
            stripe_js_id,
            elements_session_id,
            payment_locale,
            billing_profile,
            step_prefix="PIX2_BR_TAX_RETRY",
        )
        steps.append({
            "name": "stripe_update_tax_region_retry",
            "status": retry_tax_status,
        })
        if retry_tax_status < 400 and isinstance(retry_tax_data, dict) and not retry_tax_data.get("error"):
            tax_data = retry_tax_data

        refresh_status, refresh_text, refresh_data = _stripe_init(
            cs_id,
            public_key,
            provider_proxy_url,
            stripe_js_id,
            payment_locale,
            browser_timezone=browser_timezone,
            step_prefix="PIX2_BR_TAX_REFRESH",
        )
        steps.append({
            "name": "stripe_init_after_br_tax",
            "status": refresh_status,
        })
        if refresh_status >= 400 or not isinstance(refresh_data, dict) or refresh_data.get("error"):
            raise RuntimeError(
                f"Stripe PIX BR-tax refresh failed: HTTP {refresh_status} "
                f"{_c.opll_short_error(refresh_text, 700)}"
            )
        tax_data = refresh_data
        tax_trial = _free_trial_status(tax_data)
        tax_methods = _payment_methods(tax_data)
        tax_trial["hasPix"] = "pix" in tax_methods or elements_has_pix
        tax_trial["pixActivationSource"] = (
            "elements_session_br" if elements_has_pix else "payment_page_refresh"
        )
        tax_trial["elementsPaymentMethodTypes"] = elements_methods
        tax_trial["paymentPagePaymentMethodTypes"] = tax_methods
        steps[-1]["stripeContext"] = _pix_context_snapshot(tax_data)

    if not tax_trial["hasPix"]:
        raise PaymentMethodUnavailableError(
            "PAYMENT_METHOD_UNAVAILABLE:PIX_AFTER_BR_TAX: "
            "available_payment_method_types=" + ",".join(tax_methods)
            + f"; elements_country={elements_country or '-'}"
            + f"; elements_payment_method_types={','.join(elements_methods) or '-'}"
            + f"; customer_country={_nested(tax_data, ['customer', 'address', 'country']) or '-'}"
            + f"; tax_country={_nested(tax_data, ['tax_context', 'customer_tax_country']) or '-'}"
            + f"; merchant_country={_nested(tax_data, ['account_settings', 'merchant_of_record_country']) or '-'}"
            + f"; customer_email={_nested(tax_data, ['customer', 'email']) or _nested(tax_data, ['customer_email']) or '-'}"
            + f"; customer_name={_nested(tax_data, ['customer', 'name']) or _nested(tax_data, ['customer', 'individual_name']) or '-'}"
            + f"; expected_billing_country={billing_profile.get('country') or '-'}"
            + f"; expected_billing_email={billing_profile.get('email') or '-'}"
        )

    _c._emit_payment_stage(progress_callback, "stripe_payment_method", "PIX 2.0: Create PIX payment method with BR billing + CPF", 4, 8)
    pm_status, pm_text, pm_data, payment_method_id = _create_pix_payment_method(
        cs_id,
        public_key,
        tax_data,
        provider_proxy_url,
        stripe_js_id,
        elements_session_id,
        billing_profile,
        payment_locale,
    )
    steps.append({
        "name": "stripe_create_pix_payment_method",
        "status": pm_status,
        "result": "payment_method_id" if payment_method_id.startswith("pm_") else "",
    })
    if pm_status >= 400 or not payment_method_id.startswith("pm_"):
        raise RuntimeError(
            f"Stripe PIX payment method creation failed: HTTP {pm_status} "
            f"{_c.opll_short_error(pm_text, 700)}"
        )

    _c._emit_payment_stage(progress_callback, "stripe_confirm", "PIX 2.0: Confirm PIX payment method", 5, 8)
    confirm_status, confirm_text, confirm_data = _stripe_confirm(
        cs_id,
        public_key,
        tax_data,
        provider_proxy_url,
        stripe_js_id,
        entity,
        elements_session_id,
        payment_locale,
        payment_email,
        billing_profile,
        payment_method_type="pix",
        step_prefix="PIX2",
        payment_method_id=payment_method_id,
    )
    submission_attempt_id = str(
        _nested(confirm_data, ["submission_attempt", "id"]) or ""
    ).strip() if isinstance(confirm_data, dict) else ""
    steps.append({
        "name": "stripe_confirm_pix",
        "status": confirm_status,
        "state": _nested(confirm_data, ["submission_attempt", "state"])
        if isinstance(confirm_data, dict) else "",
        "result": "submission_attempt_id" if submission_attempt_id else "",
    })
    if confirm_status >= 400 or not isinstance(confirm_data, dict) or confirm_data.get("error"):
        raise RuntimeError(
            f"Stripe PIX confirm failed: HTTP {confirm_status} "
            f"{_c.opll_short_error(confirm_text, 700)}"
        )
    terminal_reason = _upi_terminal_failure(confirm_data)
    if terminal_reason:
        steps[-1]["terminalReason"] = terminal_reason

    _c._emit_payment_stage(progress_callback, "chatgpt_approval", "PIX 2.0: ChatGPT checkout confirm/approve", 6, 8)
    approval = _chatgpt_approval(
        token,
        cs_id,
        entity,
        provider_proxy_url,
        cookie,
        submission_attempt_id,
        progress_callback,
        payment_method_type="pix",
    )
    approval_status = int(approval.get("status") or 0)
    approval_data = approval.get("data")
    steps.append({
        "name": f"chatgpt_checkout_{approval.get('name') or 'approval'}",
        "status": approval_status,
        "result": _approval_text(approval_data),
        "attemptStatuses": approval.get("attemptStatuses") or [],
    })
    if not _approval_ok(approval_status, approval_data):
        attempts = "/".join(str(x) for x in (approval.get("attemptStatuses") or []))
        fast_fail = approval.get("fastFailReason")
        extra = f" approve_attempts={attempts}" if attempts else ""
        if fast_fail:
            extra += f" fast_fail={fast_fail}"
        raise UpiQrUnavailableError(
            f"ChatGPT PIX approval returned abnormal result: HTTP {approval_status} "
            f"{_c.opll_short_error(str(approval_data), 700)}.{extra}"
        )

    pix: dict = {}

    def merge_pix(source) -> None:
        nonlocal pix
        pix = _c.opll_merge_pix_extract(pix, _c.opll_extract_pix_link(source))

    def has_pix_artifact() -> bool:
        return bool(
            pix.get("pix_payload")
            or pix.get("pix_hosted_instructions_url")
            or pix.get("pix_instructions_url")
            or pix.get("pix_qr_image_url")
        )

    for source in (init_data, tax_data, confirm_data, approval_data):
        merge_pix(source)

    _c._emit_payment_stage(progress_callback, "stripe_poll_qr", "PIX 2.0: Poll Stripe for PIX QR", 7, 8)
    statuses: list[int] = []
    poll_attempts = _int_env(
        "CHATGPT_PIX2_POLL_ATTEMPTS",
        _int_env("CHATGPT_UPI_POLL_ATTEMPTS", DEFAULT_POLL_ATTEMPTS, 1, 90),
        1,
        90,
    )
    for attempt in range(poll_attempts):
        if has_pix_artifact():
            break
        if attempt > 0:
            time.sleep(1.0)
        page_status, _, page_data = _payment_page_get(
            cs_id,
            public_key,
            provider_proxy_url,
            stripe_js_id,
            elements_session_id,
            payment_locale,
            step_prefix="PIX2",
        )
        statuses.append(page_status)
        merge_pix(page_data)
        page_terminal = _upi_terminal_failure(page_data)
        if page_terminal:
            terminal_reason = page_terminal
            break
        if page_status >= 400:
            break
    if statuses:
        step = {
            "name": "stripe_payment_page_get",
            "status": statuses[-1],
            "attemptStatuses": statuses,
        }
        if terminal_reason:
            step["terminalReason"] = terminal_reason
        steps.append(step)

    if not has_pix_artifact():
        refresh_status, _, refresh_data = _stripe_init(
            cs_id,
            public_key,
            provider_proxy_url,
            stripe_js_id,
            payment_locale,
            browser_timezone=browser_timezone,
            step_prefix="PIX2_REFRESH",
        )
        steps.append({"name": "stripe_init_refresh", "status": refresh_status})
        merge_pix(refresh_data)
        refresh_terminal = _upi_terminal_failure(refresh_data)
        if refresh_terminal:
            terminal_reason = refresh_terminal
            steps[-1]["terminalReason"] = terminal_reason

    _c._emit_payment_stage(progress_callback, "hydrate_qr", "PIX 2.0: Hydrate PIX hosted instructions", 8, 8)
    stripe = _c.opll_build_stripe_session(provider_proxy_url)
    pix = _c.opll_hydrate_pix_artifacts(
        stripe,
        provider_proxy_url,
        pix,
        str(pix.get("pix_hosted_instructions_url") or ""),
        str(pix.get("pix_instructions_url") or ""),
        str(pix.get("pix_checkout_url") or ""),
        str(pix.get("pix_redirect_url") or ""),
        str(init_data.get("stripe_hosted_url") or "") if isinstance(init_data, dict) else "",
    )
    if not has_pix_artifact():
        step_text = " -> ".join(
            f"{step.get('name')}:{step.get('status')}" for step in steps
        )
        terminal_text = f" Terminal: {terminal_reason}." if terminal_reason else ""
        raise UpiQrUnavailableError(
            f"No PIX QR data found after approval.{terminal_text} "
            f"Steps: {step_text or 'none'}"
        )

    expires_at, expires_raw = _c.opll_checkout_expires_at(
        checkout,
        init_data,
        tax_data,
        confirm_data,
        approval_data,
        pix,
    )
    return {
        "pix": pix,
        "steps": steps,
        "expected_amount": str(_payment_amount(tax_data)),
        "payment_methods": list(dict.fromkeys(
            (_payment_methods(tax_data) or methods) + elements_methods
        )),
        "payment_intent_status": str(
            _nested(tax_data, ["payment_intent", "status"])
            or _nested(confirm_data, ["payment_intent", "status"])
            or ""
        ),
        "billing_email": payment_email,
        "payment_locale": payment_locale,
        "billing_profile": billing_profile,
        "approval": approval,
        "init_payload": init_data,
        "tax_payload": tax_data,
        "confirm_payload": confirm_data,
        "expires_at": expires_at,
        "expires_raw": expires_raw,
    }


def extract_upi_qr_compat(payload) -> dict:
    qr = _qr_dict(extract_upi_next_action(payload))
    return qr if (qr["qr_data"] or qr["qr_image_url"] or qr["qr_hosted_instructions_url"]) else {}


def generate_upi_qr_high_success(access_token: str, entry_proxy_url: str = "",
                                 exit_proxy_url: str = "", progress_callback=None,
                                 chatgpt_cookie: str = "", upi_approve_mode: str = "full_auto",
                                 upi_region: str = "IN", payment_locale: str = "en",
                                 payment_email: str = "") -> dict:
    cookie = _c.opll_normalize_chatgpt_cookie(chatgpt_cookie)
    entry_proxy_url = _normalize_proxy(entry_proxy_url)
    provider_proxy_url = _normalize_proxy(exit_proxy_url or entry_proxy_url)
    country, currency = normalize_upi_region(upi_region)
    payment_locale = normalize_upi_locale(payment_locale)
    payment_email_input = str(payment_email or "").strip()
    payment_email = (
        normalize_upi_payment_email(payment_email_input)
        if payment_email_input
        else ""
    )
    billing_profile = _c.opll_generate_in_profile(payment_email)
    payment_email = str(billing_profile.get("email") or payment_email)
    token = _c.opll_access_token_with_cookie(access_token, cookie, entry_proxy_url) or _c.parse_session_json(access_token) or str(access_token or "").strip()
    if not token:
        raise RuntimeError("Access Token is required")

    _c._emit_payment_stage(progress_callback, "checkout", "ChatGPT checkout creates UPI session", 1, 7)
    checkout = _create_checkout(token, entry_proxy_url, cookie, country, currency)
    checkout["_upi_billing_profile"] = billing_profile
    extracted = _confirm_and_extract(
        token,
        checkout,
        provider_proxy_url,
        cookie,
        progress_callback,
        payment_locale=payment_locale,
        payment_email=payment_email,
    )
    qr = extracted["qr"]
    expires_at = int(qr.get("qr_expires_at") or 0)
    amount = str(extracted.get("expected_amount") or "")
    long_url = str(qr.get("qr_hosted_instructions_url") or checkout.get("chatgpt_checkout_url") or "").strip()
    _c._emit_payment_stage(progress_callback, "done", "UPI QR extracted", 7, 7)
    return {
        **{k: v for k, v in checkout.items() if k != "raw_checkout" and not k.startswith("_")},
        "stripe_hosted_url": str(qr.get("stripe_hosted_url") or checkout.get("chatgpt_checkout_url") or ""),
        "long_url": long_url,
        "stripe_amount": amount,
        "stripe_amount_source": "stripe_tax_update.total_summary.due" if amount else "",
        "payment_amount_display": _c.opll_format_minor_amount(amount, checkout.get("currency", "INR")) if amount else "",
        "expires_at": expires_at,
        "expires_raw": str(qr.get("qr_expires_at") or ""),
        "valid_seconds": max(0, expires_at - int(time.time())) if expires_at else 0,
        "local_payment": "upi",
        "local_payment_detected": True,
        "payment_intent_status": str(extracted.get("payment_intent_status") or ""),
        "extraction_status": "success",
        "billing_email": str(extracted.get("billing_email") or payment_email),
        "payment_locale": str(extracted.get("payment_locale") or payment_locale),
        "upi_region": country,
        "upi_billing": extracted.get("billing_profile") or billing_profile,
        "provider_redirect_url": str(qr.get("qr_image_data_url") or qr.get("qr_data") or ""),
        "stripe_redirect_url": str(qr.get("qr_image_url_png") or qr.get("qr_image_url_svg") or ""),
        "upi_qr": qr,
        "upi_qr_data": str(qr.get("qr_data") or ""),
        "upi_qr_image_url": str(qr.get("qr_image_url") or ""),
        "upi_qr_image_url_png": str(qr.get("qr_image_url_png") or ""),
        "upi_qr_image_url_svg": str(qr.get("qr_image_url_svg") or ""),
        "upi_qr_image_data_url": str(qr.get("qr_image_data_url") or ""),
        "upi_qr_hosted_instructions_url": str(qr.get("qr_hosted_instructions_url") or ""),
        "requires_chatgpt_cookie": False,
        "chatgpt_cookie_used": bool(cookie),
        "upi_approve_mode": "gpt_upi_main_high_success",
        "upi_logic_source": "gpt-upi-main-python-port",
        "upi_steps": extracted.get("steps") or [],
    }
