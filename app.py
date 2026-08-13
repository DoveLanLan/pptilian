import json
import contextlib
import base64
import io
import os
import queue
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, send_from_directory, stream_with_context

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import (  # noqa: E402
    PAYPAL_GLOBAL_FLOW_BUILD,
    PAYPAL_GLOBAL_BILLING_COUNTRIES,
    PAYPAL_GLOBAL_PAYMENT_LOCALES,
    PAYMENT_MODES,
    ProxyChainServer,
    generate_payment_link,
    is_socks_proxy_url,
    mask_proxy_url,
    normalize_proxy_url,
    opll_probe_paypal_global_oaics_eligibility,
    opll_normalize_vn_country_proxy,
    parse_session_json,
    randomize_proxy_sid,
)
from gpt_account_plan import (  # noqa: E402
    detect_chatgpt_live,
    detect_chatgpt_momo_eligibility,
    detect_chatgpt_plan,
)

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = True

# ---------------------------------------------------------------------------
# 综合工具服务 / ChatGPT cookie-session pool integration
# ---------------------------------------------------------------------------
#
# The original link tool only accepted a manually pasted
# accessToken / Cookie.  The files copied from gpt-outlook-register-main are
# loaded here and exposed under /api/gptreg/* so the same page can:
#   1) import Outlook OAuth four-part accounts,
#   2) run the ChatGPT register/login protocol,
#   3) store access_token / session_token / cookie_header,
#   4) one-click fill those credentials into the existing link / QR generator.
GPTREG_AVAILABLE = False
GPTREG_IMPORT_ERROR = ""
gptreg_db = None
gptreg_registrar = None
gptreg_refetch_refresh_token = None

try:
    from webui import db as gptreg_db  # type: ignore
    from webui import registrar as gptreg_registrar  # type: ignore
    from webui.refetch_rt import refetch_refresh_token as gptreg_refetch_refresh_token  # type: ignore

    gptreg_db.init_db()
    try:
        gptreg_db.release_stale_in_use(stale_seconds=1800)
    except Exception:
        pass
    GPTREG_AVAILABLE = True
except Exception as exc:
    GPTREG_IMPORT_ERROR = str(exc)


def _gptreg_not_ready_response():
    return jsonify({
        "ok": False,
        "error": "GPT 注册/取 Cookie 模块未加载",
        "detail": GPTREG_IMPORT_ERROR,
    }), 500


def _gptreg_json() -> dict:
    return request.get_json(silent=True) or {}

DEFAULT_MODE = "无卡长链接 US/USD"
MAX_RETRY_ATTEMPTS = 500
MAX_CONCURRENCY = 100
MAX_SESSION_POOL = 500
CANCEL_EVENTS: dict[str, threading.Event] = {}
CANCEL_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
STATS_FILE = Path(__file__).with_name("stats.json")
STATS_LOCK = threading.Lock()
DEFAULT_GOST_PATH = r"E:\gost\gost.exe"


def _load_stats() -> dict:
    try:
        return json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"success_count": 0}


def _save_stats(stats: dict) -> None:
    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")


def _increment_success_count() -> int:
    with STATS_LOCK:
        stats = _load_stats()
        stats["success_count"] = int(stats.get("success_count") or 0) + 1
        _save_stats(stats)
        return stats["success_count"]


def _success_count() -> int:
    with STATS_LOCK:
        return int(_load_stats().get("success_count") or 0)


def _payment_result_public_fields(result: dict | None) -> dict:
    result = result or {}
    checkout_session_id = str(
        result.get("checkout_session_id")
        or result.get("checkout_id")
        or result.get("cs_id")
        or ""
    )
    return {
        "checkout_session_id": checkout_session_id,
        "checkout_id": str(result.get("checkout_id") or result.get("cs_id") or ""),
        "cs_id": str(result.get("cs_id") or ""),
        "session_kind": str(result.get("session_kind") or ""),
        "checkout_branch": str(result.get("checkout_branch") or ""),
        "checkout_session_type": str(result.get("checkout_session_type") or ""),
        "checkout_branch_requested": str(result.get("checkout_branch_requested") or ""),
        "checkout_branch_effective": str(result.get("checkout_branch_effective") or ""),
        "oaics_eligible": result.get("oaics_eligible"),
        "browser_profile": str(result.get("browser_profile") or ""),
    }


def _pool_from_text(text: str) -> list[str]:
    if not text:
        return []
    lines = []
    for line in text.splitlines():
        item = line.strip()
        if item and not item.startswith("#"):
            lines.append(item)
    return lines


def _email_type(email: str) -> str:
    domain = str(email or "").split("@")[-1].lower()
    if domain == "gmail.com":
        return "Gmail"
    if domain in {"outlook.com", "hotmail.com"}:
        return "Outlook"
    if domain == "icloud.com":
        return "iCloud"
    if domain == "qq.com":
        return "QQ 邮箱"
    if domain in {"163.com", "126.com"}:
        return "网易邮箱"
    if domain.endswith(".edu") or ".edu." in domain:
        return "教育邮箱"
    return "其他邮箱"


def _find_email(value) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if "email" in str(key).lower() and isinstance(item, str):
                match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", item)
                if match:
                    return match.group(0)
        for item in value.values():
            email = _find_email(item)
            if email:
                return email
    elif isinstance(value, list):
        for item in value:
            email = _find_email(item)
            if email:
                return email
    elif isinstance(value, str):
        match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", value)
        if match:
            return match.group(0)
    return ""


def _decode_jwt_payload(token: str) -> dict:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def _inspect_session_block(block: str, index: int) -> dict:
    raw = str(block or "").strip()
    token = parse_session_json(raw)
    email = ""
    try:
        email = _find_email(json.loads(raw))
    except Exception:
        email = _find_email(raw)
    if not email and token:
        email = _find_email(_decode_jwt_payload(token))
    return {
        "index": index,
        "ok": bool(token),
        "input_type": "Session JSON" if raw.lstrip()[:1] in ("{", "[") else "AccessToken",
        "email": email,
        "email_type": _email_type(email) if email else "号码注册 / 第三方登录 / Token 未包含邮箱",
        "token_preview": (token[:8] + "..." + token[-8:]) if token and len(token) > 20 else token,
        "message": "已识别" if token else "未识别到 AccessToken",
    }


def _inspect_sessions(session_text: str, session_pool_text: str = "") -> list[dict]:
    blocks = _session_blocks_from_text(session_pool_text)
    if not blocks:
        blocks = _session_blocks_from_text(session_text)
    seen: set[str] = set()
    items: list[dict] = []
    for block in blocks[:MAX_SESSION_POOL]:
        info = _inspect_session_block(block, len(items) + 1)
        token_key = parse_session_json(block) or str(info.get("email") or "") or str(block)
        if token_key in seen:
            continue
        seen.add(token_key)
        items.append(info)
    return items[:MAX_SESSION_POOL]


def _session_blocks_from_text(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    if "\n---" in raw or "---\n" in raw:
        blocks: list[str] = []
        current: list[str] = []
        for line in raw.splitlines():
            if line.strip() == "---":
                block = "\n".join(current).strip()
                if block:
                    blocks.append(block)
                current = []
            else:
                current.append(line)
        block = "\n".join(current).strip()
        if block:
            blocks.append(block)
        return blocks[:MAX_SESSION_POOL]
    first = raw.lstrip()[:1]
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) > 1 and first not in ("{", "["):
        return lines[:MAX_SESSION_POOL]
    token = parse_session_json(raw)
    if token:
        return [raw]
    return lines[:MAX_SESSION_POOL]


def _parse_access_token_pool(session_text: str, session_pool_text: str = "") -> list[str]:
    blocks = _session_blocks_from_text(session_pool_text)
    if not blocks:
        blocks = _session_blocks_from_text(session_text)
    tokens: list[str] = []
    seen: set[str] = set()
    for block in blocks[:MAX_SESSION_POOL]:
        for token in _extract_access_tokens_from_block(block):
            if token and token not in seen:
                seen.add(token)
                tokens.append(token)
                if len(tokens) >= MAX_SESSION_POOL:
                    return tokens[:MAX_SESSION_POOL]
    return tokens[:MAX_SESSION_POOL]


def _extract_access_tokens_from_block(block: str) -> list[str]:
    raw = str(block or "").strip()
    if not raw:
        return []
    tokens: list[str] = []

    def add(value) -> None:
        token = str(value or "").strip()
        if token and token not in tokens:
            tokens.append(token)

    def walk(value) -> None:
        if isinstance(value, dict):
            for key in ("accessToken", "access_token", "token"):
                if value.get(key):
                    add(value.get(key))
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            token = parse_session_json(value)
            if token:
                add(token)

    try:
        walk(json.loads(raw))
    except Exception:
        pass
    token = parse_session_json(raw)
    if token:
        add(token)
    if raw.startswith("eyJ") and raw.count(".") == 2 and len(raw) > 500:
        add(raw)
    # Also support pasted export files where multiple JSON snippets are on one line.
    for match in re.finditer(r'"(?:accessToken|access_token)"\s*:\s*"([^"]+)"', raw):
        add(match.group(1))
    return tokens


def _token_for_worker(access_tokens: list[str], worker_id: int) -> str:
    if not access_tokens:
        raise RuntimeError("Session 池为空")
    return access_tokens[(worker_id - 1) % len(access_tokens)]


def _normalize_payment_proxy(value: str) -> str:
    return normalize_proxy_url(value, default_scheme="socks5h")


def _pick_proxy(pool: list[str], default_scheme: str = "http") -> str:
    return normalize_proxy_url(random.choice(pool), default_scheme=default_scheme) if pool else ""


def _proxy_identity(proxy: str, default_scheme: str = "socks5h") -> str:
    try:
        return normalize_proxy_url(proxy, default_scheme=default_scheme).strip().lower()
    except Exception:
        return str(proxy or "").strip().lower()


def _filter_proxy_pool(pool: list[str], excluded: set[str] | None = None,
                       default_scheme: str = "socks5h") -> list[str]:
    if not pool or not excluded:
        return pool
    filtered = [item for item in pool if _proxy_identity(item, default_scheme) not in excluded]
    # If every line was marked bad, reopen the pool so a long run can keep cycling.
    if not filtered:
        excluded.clear()
        return pool
    return filtered


def _is_paypal_mode(mode_name: str) -> bool:
    return str(mode_name or "").strip().lower().startswith("paypal")


def _is_paypal_us_mode(mode_name: str) -> bool:
    text = str(mode_name or "").strip().lower()
    return text.startswith("paypal") and "us/usd" in text


def _is_true_no_card_us_mode(mode_name: str) -> bool:
    return bool((PAYMENT_MODES.get(mode_name) or {}).get("true_no_card_us"))


def _is_team_codex_low_mode(mode_name: str) -> bool:
    return bool((PAYMENT_MODES.get(str(mode_name or "").strip()) or {}).get("team_codex_low"))


def _is_ph_cross_region_mode(mode_name: str) -> bool:
    return bool((PAYMENT_MODES.get(str(mode_name or "").strip()) or {}).get("ph_cross_region_promo"))


def _is_ph_gcash_mode(mode_name: str) -> bool:
    return bool((PAYMENT_MODES.get(str(mode_name or "").strip()) or {}).get("ph_gcash_redirect"))


def _is_ba_pm_711_mode(mode_name: str) -> bool:
    return bool((PAYMENT_MODES.get(str(mode_name or "").strip()) or {}).get("ba_pm_711"))


def _is_paypal_global_rotation_mode(mode_name: str) -> bool:
    text = str(mode_name or "").strip().lower()
    cfg = PAYMENT_MODES.get(str(mode_name or "").strip()) or {}
    return bool(cfg.get("paypal_global_rotation")) or text == "paypal全球轮转" or (text.startswith("paypal") and "global" in text)


def _is_paypal_global_no_discount_mode(mode_name: str) -> bool:
    cfg = PAYMENT_MODES.get(str(mode_name or "").strip()) or {}
    return bool(cfg.get("paypal_global_no_discount"))


def _is_paypal_global_mode(mode_name: str) -> bool:
    return _is_paypal_global_rotation_mode(mode_name) or _is_paypal_global_no_discount_mode(mode_name)


def _is_nl_ideal_mode(mode_name: str) -> bool:
    cfg = PAYMENT_MODES.get(str(mode_name or "").strip()) or {}
    return str(cfg.get("local_payment") or "").lower() == "ideal"


def _is_nl_ideal_v2_mode(mode_name: str) -> bool:
    cfg = PAYMENT_MODES.get(str(mode_name or "").strip()) or {}
    return bool(cfg.get("ideal_v2"))


def _is_nl_ideal_v3_mode(mode_name: str) -> bool:
    cfg = PAYMENT_MODES.get(str(mode_name or "").strip()) or {}
    return bool(cfg.get("ideal_v3"))


def _is_kakao_mode(mode_name: str) -> bool:
    text = str(mode_name or "").strip().lower()
    return "kakao" in text or "韩国" in str(mode_name or "")


def _is_kakao_v2_mode(mode_name: str) -> bool:
    return bool((PAYMENT_MODES.get(str(mode_name or "").strip()) or {}).get("kakao_v2"))


def _is_twint_mode(mode_name: str) -> bool:
    cfg = PAYMENT_MODES.get(str(mode_name or "").strip()) or {}
    text = str(mode_name or "").strip().lower()
    return str(cfg.get("local_payment") or "").lower() == "twint" or "twint" in text


def _is_twint_v2_mode(mode_name: str) -> bool:
    return bool((PAYMENT_MODES.get(str(mode_name or "").strip()) or {}).get("twint_v2"))


def _is_promptpay_mode(mode_name: str) -> bool:
    cfg = PAYMENT_MODES.get(str(mode_name or "").strip()) or {}
    text = str(mode_name or "").strip().lower()
    return str(cfg.get("local_payment") or "").lower() == "promptpay" or "promptpay" in text


def _is_promptpay_v2_mode(mode_name: str) -> bool:
    return bool((PAYMENT_MODES.get(str(mode_name or "").strip()) or {}).get("promptpay_v2"))


def _is_upi_mode(mode_name: str) -> bool:
    text = str(mode_name or "").strip().lower()
    return "upi" in text


def _is_upi_v2_mode(mode_name: str) -> bool:
    return bool((PAYMENT_MODES.get(str(mode_name or "").strip()) or {}).get("upi_v2"))


def _is_pix_mode(mode_name: str) -> bool:
    cfg = PAYMENT_MODES.get(str(mode_name or "").strip()) or {}
    text = str(mode_name or "").strip().lower()
    return bool(cfg.get("pix_flow")) or str(cfg.get("local_payment") or "").lower() == "pix" or "pix" in text


def _is_pix_v2_mode(mode_name: str) -> bool:
    cfg = PAYMENT_MODES.get(str(mode_name or "").strip()) or {}
    return bool(cfg.get("pix_v2"))


def _is_pix_normal_mode(mode_name: str) -> bool:
    cfg = PAYMENT_MODES.get(str(mode_name or "").strip()) or {}
    return bool(cfg.get("pix_normal_qr")) or "正常" in str(mode_name or "")

def _is_pix_postpromo_mode(mode_name: str) -> bool:
    cfg = PAYMENT_MODES.get(str(mode_name or "").strip()) or {}
    return bool(cfg.get("pix_post_promo")) or "后置" in str(mode_name or "")


def _is_split_stage_mode(mode_name: str) -> bool:
    # UPI needs JP checkout to keep the trial amount at 0, then IN for Stripe/UPI QR.
    # The gpt-upi-main extraction flow itself supports separate checkout/provider proxies;
    # this app uses that supported split mode by default for UPI.
    return _is_nl_ideal_mode(mode_name) or _is_upi_mode(mode_name) or (_is_pix_mode(mode_name) and not _is_pix_normal_mode(mode_name)) or _is_kakao_mode(mode_name) or _is_twint_mode(mode_name) or _is_promptpay_mode(mode_name) or _is_ba_pm_711_mode(mode_name) or _is_paypal_global_rotation_mode(mode_name) or _is_ph_cross_region_mode(mode_name) or _is_ph_gcash_mode(mode_name)


def _stage_entry_proxy_text(data: dict, mode_name: str) -> str:
    if _is_pix_mode(mode_name):
        return str(data.get("pix_br_proxy_pool") or data.get("pix_entry_proxy") or
                   data.get("nl_entry_proxy") or data.get("payment_proxy_pool") or "")
    if _is_kakao_mode(mode_name):
        return str(data.get("kakao_entry_proxy") or data.get("nl_entry_proxy") or
                   data.get("payment_proxy_pool") or "")
    if _is_twint_mode(mode_name):
        return str(data.get("twint_entry_proxy") or data.get("nl_entry_proxy") or
                   data.get("payment_proxy_pool") or "")
    if _is_promptpay_mode(mode_name):
        return str(data.get("promptpay_entry_proxy") or data.get("nl_entry_proxy") or
                   data.get("payment_proxy_pool") or "")
    return str(data.get("nl_entry_proxy") or data.get("payment_proxy_pool") or "")


def _stage_exit_proxy_text(data: dict, mode_name: str) -> str:
    if _is_pix_mode(mode_name):
        return str(data.get("pix_vn_proxy_pool") or data.get("pix_middle_proxy") or
                   data.get("nl_exit_proxy") or data.get("provider_proxy_pool") or
                   data.get("paypal_proxy_pool") or "")
    if _is_kakao_mode(mode_name):
        return str(data.get("kakao_exit_proxy") or data.get("nl_exit_proxy") or
                   data.get("provider_proxy_pool") or data.get("paypal_proxy_pool") or "")
    if _is_twint_mode(mode_name):
        return str(data.get("twint_exit_proxy") or data.get("nl_exit_proxy") or
                   data.get("provider_proxy_pool") or data.get("paypal_proxy_pool") or "")
    if _is_promptpay_mode(mode_name):
        return str(data.get("promptpay_exit_proxy") or data.get("nl_exit_proxy") or
                   data.get("provider_proxy_pool") or data.get("paypal_proxy_pool") or "")
    return str(data.get("nl_exit_proxy") or data.get("provider_proxy_pool") or
               data.get("paypal_proxy_pool") or "")


def _stage_final_proxy_text(data: dict, mode_name: str) -> str:
    if _is_pix_mode(mode_name):
        return str(data.get("pix_br2_proxy_pool") or data.get("pix_final_proxy") or
                   data.get("pix_br_final_proxy") or "")
    return ""


def _first_proxy_from_text(value: str, default_scheme: str = "socks5h") -> str:
    pool = _pool_from_text(value)
    if not pool:
        return ""
    return normalize_proxy_url(pool[0], default_scheme=default_scheme)


def _pick_stage_proxy_from_input(raw_text: str, direct_text: str = "",
                                 fallback_pool: list[str] | None = None,
                                 default_scheme: str = "socks5h",
                                 excluded: set[str] | None = None) -> str:
    pool = _filter_proxy_pool(_pool_from_text(raw_text), excluded, default_scheme)
    if pool:
        return _pick_proxy(pool, default_scheme=default_scheme)
    direct = str(direct_text or "").strip()
    if direct and _proxy_identity(direct, default_scheme) not in (excluded or set()):
        return normalize_proxy_url(direct, default_scheme=default_scheme)
    return _pick_proxy(_filter_proxy_pool(fallback_pool or [], excluded, default_scheme), default_scheme=default_scheme)


def _paypal_strategies(data: dict, mode_name: str) -> set[str]:
    if not _is_paypal_us_mode(mode_name):
        return set()
    raw = data.get("paypal_strategies")
    if isinstance(raw, list):
        values = {str(item).strip() for item in raw}
    else:
        values = {part.strip() for part in str(raw or "").split(",") if part.strip()}
    if not values:
        values = {"jp_unified"}
    allowed = {"jp_unified", "jp_us_split"}
    return values & allowed or {"jp_unified"}


def _strategy_for_worker(strategies: set[str], worker_id: int) -> str:
    if "jp_unified" in strategies and "jp_us_split" in strategies:
        return "jp_unified" if worker_id % 2 == 1 else "jp_us_split"
    if "jp_us_split" in strategies:
        return "jp_us_split"
    return "jp_unified"


def _strategy_label(strategy: str) -> str:
    return "方案② JP checkout + US PayPal" if strategy == "jp_us_split" else "方案① 全流程日本"


def _validate_strategy_inputs(data: dict, mode_name: str, access_tokens: list[str]) -> tuple[bool, str]:
    if _is_ph_gcash_mode(mode_name):
        checkout_proxy = str(_stage_entry_proxy_text(data, mode_name)).strip()
        promotion_proxy = str(_stage_exit_proxy_text(data, mode_name)).strip()
        if not checkout_proxy or not promotion_proxy:
            return False, "菲律宾 GCash 提链必须填写 Checkout 代理池和优惠代理池"
        return True, ""
    if _is_ph_cross_region_mode(mode_name):
        checkout_proxy = str(_stage_entry_proxy_text(data, mode_name)).strip()
        promotion_proxy = str(_stage_exit_proxy_text(data, mode_name)).strip()
        if not checkout_proxy or not promotion_proxy:
            return False, "菲律宾跨区转优惠提链必须填写 Checkout 代理池和优惠代理池"
        return True, ""
    if _is_team_codex_low_mode(mode_name):
        us_proxy = str(data.get("payment_proxy_pool") or data.get("nl_entry_proxy") or data.get("payment_proxy") or data.get("proxy_pool") or "").strip()
        if not us_proxy:
            return False, "Team Codex 0.52模式必须填写 US代理池"
        return True, ""
    if _is_paypal_global_mode(mode_name):
        country_proxy = str(_stage_entry_proxy_text(data, mode_name)).strip()
        if not country_proxy:
            return False, "PayPal全球模式 requires PayPal proxy pool"
        if _is_paypal_global_rotation_mode(mode_name) and not str(_stage_exit_proxy_text(data, mode_name)).strip():
            return False, "PAYPAL全球轮转 requires main PayPal proxy pool and promo proxy pool"
        billing_country = str(data.get("paypal_billing_country") or "JP").strip().upper()
        if billing_country == "UK":
            billing_country = "GB"
        if billing_country not in PAYPAL_GLOBAL_BILLING_COUNTRIES:
            return False, f"PAYPAL全球轮转 billing country must be one of {sorted(PAYPAL_GLOBAL_BILLING_COUNTRIES)}"
        payment_locale = str(data.get("paypal_payment_locale") or "en").strip() or "en"
        if payment_locale not in PAYPAL_GLOBAL_PAYMENT_LOCALES:
            return False, f"PAYPAL全球轮转 payment locale must be one of {sorted(PAYPAL_GLOBAL_PAYMENT_LOCALES)}"
        return True, ""
    if _is_ba_pm_711_mode(mode_name):
        us_proxy = str(_stage_entry_proxy_text(data, mode_name)).strip()
        promo_proxy = str(_stage_exit_proxy_text(data, mode_name)).strip()
        if not us_proxy or not promo_proxy:
            return False, "7.11 BA/PM模式必须填写 US代理池 和 优惠代理池"
        return True, ""
    if _is_upi_v2_mode(mode_name):
        in_proxy = str(_stage_entry_proxy_text(data, mode_name)).strip()
        promo_proxy = str(_stage_exit_proxy_text(data, mode_name)).strip()
        if not in_proxy or not promo_proxy:
            return False, "UPI 2.0 mode requires IN proxy pool and promo proxy pool"
        return True, ""
    if _is_kakao_v2_mode(mode_name):
        kr_proxy = str(_stage_entry_proxy_text(data, mode_name)).strip()
        promo_proxy = str(_stage_exit_proxy_text(data, mode_name)).strip()
        if not kr_proxy or not promo_proxy:
            return False, "KAKAO 2.0模式必须填写 KR代理池 和 优惠代理池"
        return True, ""
    if _is_twint_v2_mode(mode_name):
        ch_proxy = str(_stage_entry_proxy_text(data, mode_name)).strip()
        promo_proxy = str(_stage_exit_proxy_text(data, mode_name)).strip()
        if not ch_proxy or not promo_proxy:
            return False, "TWINT模式必须填写 CH代理池 和 优惠代理池"
        return True, ""
    if _is_promptpay_v2_mode(mode_name):
        th_proxy = str(_stage_entry_proxy_text(data, mode_name)).strip()
        promo_proxy = str(_stage_exit_proxy_text(data, mode_name)).strip()
        if not th_proxy or not promo_proxy:
            return False, "PromptPay模式必须填写 TH代理池 和 优惠代理池"
        return True, ""
    if _is_pix_mode(mode_name):
        br1_proxy = str(_stage_entry_proxy_text(data, mode_name)).strip()
        br3_proxy = str(_stage_final_proxy_text(data, mode_name)).strip()
        if _is_pix_v2_mode(mode_name):
            promo_proxy = str(_stage_exit_proxy_text(data, mode_name)).strip()
            if not br1_proxy or not promo_proxy:
                return False, "PIX 2.0 requires BR proxy pool and promo proxy pool"
            return True, ""
        if _is_pix_normal_mode(mode_name):
            if not br1_proxy:
                return False, "PIX normal QR requires one BR proxy pool"
            return True, ""
        if _is_pix_postpromo_mode(mode_name):
            if not br1_proxy:
                return False, "PIX post-promo mode requires BR stage 1 proxy pool; VN and BR-final are optional"
            return True, ""
        if not br1_proxy or not br3_proxy:
            return False, "PIX mode requires BR stage 1 and BR stage 3 proxy pools; VN stage 2 is optional"
        return True, ""
    if _is_split_stage_mode(mode_name):
        entry_proxy = str(_stage_entry_proxy_text(data, mode_name)).strip()
        exit_proxy = str(_stage_exit_proxy_text(data, mode_name)).strip()
        if not entry_proxy or not exit_proxy:
            if _is_upi_mode(mode_name):
                return False, "UPI模式必须填写JP入口代理和IN出口代理"
            if _is_kakao_mode(mode_name):
                return False, "韩国KAKAO提链模式必须填写 JP入口代理 和 KR出口代理"
            if _is_twint_mode(mode_name):
                return False, "瑞士TWINT提链模式必须填写 CH代理池 和 优惠代理池"
            if _is_promptpay_mode(mode_name):
                return False, "泰国PromptPay提链模式必须填写 TH代理池 和 优惠代理池"
            if _is_nl_ideal_v3_mode(mode_name):
                return False, "荷兰3.0提链必须填写 NL 代理池和优惠地区代理池"
            if _is_nl_ideal_v2_mode(mode_name):
                return False, "iDEAL 2.0模式必须填写 NL代理池 和 VN优惠代理池"
            return False, "NL荷兰提链模式必须填写 JP入口代理 和 NL出口代理"
        return True, ""
    strategies = _paypal_strategies(data, mode_name)
    if not strategies:
        return True, ""
    provider_pool = _pool_from_text(data.get("provider_proxy_pool", "") or data.get("paypal_proxy_pool", ""))
    provider_proxy = str(data.get("provider_proxy", "") or data.get("paypal_proxy", "")).strip()
    needs_provider = "jp_us_split" in strategies
    if needs_provider and not provider_pool and not provider_proxy:
        return False, "选择方案②时必须填写 PayPal 阶段代理池"
    if len(strategies) >= 2 and len(access_tokens) < 2:
        return False, "同时选择方案①和②时，Session 池至少需要 2 个 Session / AccessToken"
    return True, ""


def _truthy_default(value, default: bool = True) -> bool:
    if value is None:
        return default
    return _truthy(value)


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _find_gost_executable() -> str:
    candidates = [
        os.environ.get("GOST_PATH", ""),
        str(Path(__file__).with_name("gost.exe")),
        str(Path(__file__).with_name("bin") / "gost.exe"),
        DEFAULT_GOST_PATH,
        shutil.which("gost") or "",
        shutil.which("gost.exe") or "",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError("未找到 gost.exe；请设置 GOST_PATH，或把 gost.exe 放到项目目录 / bin 目录")


def _free_tcp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _gost_forward_url(proxy_url: str) -> str:
    text = _normalize_payment_proxy(proxy_url)
    if text.startswith("socks5h://"):
        return "socks5://" + text.split("://", 1)[1]
    if text.startswith("socks4a://"):
        return "socks4://" + text.split("://", 1)[1]
    return text


class GostHttpBridge:
    def __init__(self, upstream_proxy: str):
        self.upstream_proxy = _normalize_payment_proxy(upstream_proxy)
        self.port = 0
        self.url = ""
        self.proc: subprocess.Popen | None = None

    def __enter__(self):
        if not is_socks_proxy_url(self.upstream_proxy):
            self.url = self.upstream_proxy
            return self
        gost = _find_gost_executable()
        self.port = _free_tcp_port()
        self.url = f"http://127.0.0.1:{self.port}"
        cmd = [
            gost,
            "-L", self.url,
            "-F", _gost_forward_url(self.upstream_proxy),
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        deadline = time.time() + 4
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("gost 启动失败，请检查 GOST_PATH 和上游代理格式")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.25):
                    return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("gost 本地 HTTP 代理启动超时")

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _requested_attempts(data: dict) -> int:
    try:
        attempts = int(data.get("max_attempts") or 1)
    except (TypeError, ValueError):
        attempts = 1
    return max(1, min(attempts, MAX_RETRY_ATTEMPTS))


def _requested_concurrency(data: dict) -> int:
    try:
        concurrency = int(data.get("concurrency") or 1)
    except (TypeError, ValueError):
        concurrency = 1
    return max(1, min(concurrency, MAX_CONCURRENCY))


def _register_cancel_token(cancel_token: str) -> threading.Event | None:
    if not cancel_token:
        return None
    with CANCEL_LOCK:
        event = threading.Event()
        CANCEL_EVENTS[cancel_token] = event
        return event


def _clear_cancel_token(cancel_token: str) -> None:
    if not cancel_token:
        return
    with CANCEL_LOCK:
        CANCEL_EVENTS.pop(cancel_token, None)


def _cancelled_response(attempt: int, attempts: int, failures: list[str],
                        label: str, effective: str) -> dict:
    return {
        "ok": False,
        "cancelled": True,
        "error": f"已手动停止重试，停在第 {attempt}/{attempts} 次",
        "attempt": attempt,
        "max_attempts": attempts,
        "proxy_used": label,
        "proxy_url": mask_proxy_url(effective) if effective else "",
        "errors": failures[-10:],
    }


def _build_effective_proxy(local: str, proxy: str, payment_proxy: str) -> tuple[str, str]:
    """Return (effective_url, label).

    payment_proxy is used directly. This lets the payment proxy pool contain
    http, https, socks4, socks4a, socks5, or socks5h URLs without forcing them
    through the local HTTP chain.
    """
    local = normalize_proxy_url(local)
    proxy = normalize_proxy_url(proxy)
    payment_proxy = _normalize_payment_proxy(payment_proxy)
    if proxy:
        proxy = randomize_proxy_sid(proxy)
    if payment_proxy:
        payment_proxy = randomize_proxy_sid(payment_proxy)

    if payment_proxy:
        return payment_proxy, mask_proxy_url(payment_proxy)

    effective = proxy or local
    if not effective:
        return "", "直连"

    try:
        with ProxyChainServer(local, proxy or "", lambda _: None) as chain:
            return chain.url or effective, mask_proxy_url(effective)
    except Exception:
        return effective, mask_proxy_url(effective)


def _build_stage_proxy(local: str, proxy: str, stage_proxy: str,
                       use_gost_bridge: bool, stack: contextlib.ExitStack) -> tuple[str, str]:
    normalized = _normalize_payment_proxy(stage_proxy)
    if normalized:
        normalized = randomize_proxy_sid(normalized)
        if use_gost_bridge and is_socks_proxy_url(normalized):
            bridge = stack.enter_context(GostHttpBridge(normalized))
            return bridge.url, f"{mask_proxy_url(normalized)} -> {mask_proxy_url(bridge.url)}"
        return normalized, mask_proxy_url(normalized)
    return _build_effective_proxy(local, proxy, "")


def _combined_proxy_label(checkout_label: str, provider_label: str, split_mode: bool) -> str:
    if not split_mode or not provider_label or provider_label == checkout_label:
        return checkout_label or "直连"
    return f"入口 {checkout_label or '直连'} | 出口 {provider_label}"


def _emit_progress(progress_callback, payload: dict) -> None:
    if progress_callback:
        progress_callback(payload)


def _classify_attempt_error(error: str) -> dict:
    """Map raw retry errors to the UI categories used in UPI diagnostics."""
    raw = str(error or "")
    low = raw.lower()
    category = "其他错误"
    category_key = "other"
    subtype = "未归类"
    diagnosis = "暂未命中明确规则，请结合原始错误判断。"

    if "parameter_invalid_empty" in low and "tax_region[line2]" in low:
        category = "UPI 账单字段错误"
        category_key = "upi_billing_field_error"
        subtype = "empty tax_region[line2]"
        diagnosis = "Stripe 不接受空的账单地址第二行；程序应省略该可选字段。"
    elif "curl: (35)" in low or "tls connect error" in low:
        category = "代理 TLS 连接错误"
        category_key = "proxy_tls_error"
        subtype = "curl 35 / TLS connect"
        diagnosis = "所选代理与目标站点的 TLS 握手失败，属于代理线路质量或 SOCKS/GOST 兼容问题。"
    elif "email_invalid" in low or "billing_details[email]" in low:
        category = "Stripe parameter error"
        category_key = "stripe_email_invalid"
        subtype = "email_invalid"
        diagnosis = "Stripe rejected billing_details[email]. The generated billing email was invalid; sanitize names with spaces/punctuation before creating PayPal PaymentMethod."
    elif "checkout/update promotion failed: http 403" in low:
        category = "优惠阶段被拦截"
        category_key = "promotion_http_403"
        subtype = "checkout/update HTTP 403"
        diagnosis = "优惠代理访问 checkout/update 时被拒绝；优先更换 TR/VN 优惠代理，Cookie 与当前 Session 一致时也可一并填写。"
    elif "oaics promo/update failed on all variants" in low:
        category = "OAICS 优惠阶段失败"
        category_key = "oaics_promo_update_failed"
        subtype = "all promo variants failed"
        diagnosis = "OAICS custom 已进入优惠阶段，但 full-profile/standard/page-route/hosted-contract 都没把金额刷到 0；优先换优惠代理或换该账号重试。"
    elif "oaics strict branch expected oaics_" in low or "expected oaics_ checkout but got" in low:
        category = "OAICS 会话类型不匹配"
        category_key = "oaics_checkout_type_mismatch"
        subtype = "expected oaics_ got cs_"
        diagnosis = "强制 OAICS custom 时实际 checkout 返回了 cs_live/cs_test；当前构建会尝试 BR seed OAICS 后再绑定账单，仍失败就换账号或主代理。"
    elif "oaics confirmation_tokens failed" in low:
        category = "OAICS Stripe token 错误"
        category_key = "oaics_confirmation_token_error"
        subtype = "confirmation_tokens failed"
        diagnosis = "已到 Stripe confirmation_tokens 阶段；通常是主代理出口 IP、fingerprint、账单字段或 Stripe 参数被拒。当前构建已补 IP 与浏览器画像。"
    elif "oaics stripe elements init failed" in low:
        category = "OAICS Stripe Elements 初始化错误"
        category_key = "oaics_elements_init_error"
        subtype = "elements/sessions failed"
        diagnosis = "已到 OAICS Stripe Elements init；上一版误带 payment_method_types 参数会触发 parameter_unknown，当前构建已移除并加 unknown-param 自动剥离重试。"
    elif "oaics checkout/confirm failed" in low:
        category = "OAICS OpenAI confirm 错误"
        category_key = "oaics_checkout_confirm_error"
        subtype = "checkout/confirm failed"
        diagnosis = "confirmation_token 已生成，但 OpenAI checkout/confirm 没接住；优先检查账号当前 checkout 类型、Cookie/AT 一致性和主代理。"
    elif "oaics intent confirm failed" in low:
        category = "OAICS Stripe Intent confirm 错误"
        category_key = "oaics_intent_confirm_error"
        subtype = "intent confirm failed"
        diagnosis = "OpenAI 已返回 client_secret，但 Stripe confirm setup/payment intent 失败；多见于 PayPal/Stripe 风控或主代理质量。"
    elif "oaics did not extract ba or pm link" in low:
        category = "OAICS PayPal 跳转解析失败"
        category_key = "oaics_paypal_redirect_missing"
        subtype = "no BA/PM redirect"
        diagnosis = "OAICS confirm 已走完一部分，但返回体没有 PayPal BA/pm-redirect；换主代理或账号重试。"
    elif ("pix still waiting for chatgpt approval" in low or
            ("pix instructions url not extracted" in low and "requires_approval" in low)):
        category = "PIX approve 未放行"
        category_key = "pix_requires_approval"
        subtype = "requires_approval"
        diagnosis = "Stripe 已到 0 BRL PIX，但 ChatGPT approve 还没有把订单放行；填同账号浏览器 Cookie 后重试，程序会按 BR-3/BR-1/VN/direct 逐路 approve。"
    elif "upi qr extraction timeout" in low and "submission_state=requires_approval" in low:
        category = "approval 放行 / requires_approval timeout"
        category_key = "approval_requires_approval_timeout"
        subtype = "requires_approval timeout"
        diagnosis = "已进入 UPI 流程，但 Stripe 一直等待 ChatGPT approve 放行；高度疑似 Sentinel approve 被 blocked 或 approve 无效。"
    elif "result='blocked'" in low or 'result="blocked"' in low or ("blocked" in low and "approve" in low):
        category = "approval 被 blocked"
        category_key = "approval_blocked"
        subtype = "Sentinel blocked"
        diagnosis = "ChatGPT approve 返回 blocked 或被风控拦截，后端自动 approve 未真正放行。"
    elif "chatgpt approve failed" in low or "approve failed" in low or "checkout/approve" in low:
        category = "approval 错误"
        category_key = "approval_error"
        subtype = "approve request failed"
        diagnosis = "approve 请求失败；优先检查 Cookie 是否匹配当前账号，以及 IN 出口代理是否被风控。"
    elif "ssl: unexpected_eof_while_reading" in low or "unexpected_eof_while_reading" in low:
        category = "网络 / 代理错误"
        category_key = "network_or_proxy_timeout"
        subtype = "SSL unexpected EOF"
        diagnosis = "TLS 连接被代理或远端提前断开，常见于 JP checkout 阶段代理不稳定。"
    elif "record_layer_failure" in low or "record layer failure" in low:
        category = "网络 / 代理错误"
        category_key = "network_or_proxy_timeout"
        subtype = "SSL record layer failure"
        diagnosis = "SSL 记录层失败，通常是代理链路/TLS 握手异常。"
    elif "remotedisconnected" in low or "remote end closed connection" in low or "unable to connect to proxy" in low:
        category = "网络 / 代理错误"
        category_key = "network_or_proxy_timeout"
        subtype = "Proxy RemoteDisconnected"
        diagnosis = "代理连接被上游关闭或代理不可用。"
    elif "curl: (28)" in low or "operation timed out after 30002" in low:
        category = "网络 / 代理错误"
        category_key = "network_or_proxy_timeout"
        subtype = "curl 30 秒超时"
        diagnosis = "请求 30 秒无响应，多数是代理出口或目标链路超时。"
    elif "read timed out" in low or "connect timeout" in low or "timed out" in low or "proxyerror" in low or "connection reset" in low:
        category = "网络 / 代理错误"
        category_key = "network_or_proxy_timeout"
        subtype = "network/proxy timeout"
        diagnosis = "网络或代理层超时/重置；优先换对应阶段代理。"
    elif (
        "checkout create failed: http 403" in low
        and ("<html" in low or "<!doctype html" in low)
    ):
        category = "Checkout 接口拒绝"
        category_key = "checkout_edge_forbidden"
        subtype = "checkout HTTP 403 HTML"
        diagnosis = (
            "ChatGPT Checkout 路由返回网页层 403；这不是普通代理连通测试，"
            "更偏向当前出口被目标路由拦截。若 Access Token 失效，通常会看到 HTTP 401 或 JSON 认证错误。"
        )
    elif (
        "source upi checkout failed" in low
        or "checkout create failed" in low
        or "checkout failed" in low
        or "checkout response" in low
    ):
        category = "checkout create error"
        category_key = "checkout_error"
        subtype = "checkout failed"
        diagnosis = "ChatGPT checkout creation failed; check entry proxy, Session, and account state."
    elif "payment_method_types_mismatch" in low and "payment_method_type=ideal" in low:
        category = "iDEAL 未开放 / 支付方式不匹配"
        category_key = "ideal_not_available"
        subtype = "ideal not in payment_method_types"
        diagnosis = "当前 Stripe checkout 没有真正开放 iDEAL；需换 NL 出口/账号/Session 组合，或查看原始错误里的 payment_method_types/ordered_payment_method_types。"
    elif "payment page did not expose ideal" in low:
        category = "iDEAL 未开放"
        category_key = "ideal_not_available"
        subtype = "ideal not exposed"
        if "legacy_zip_logic_contains_ideal=true" in low:
            diagnosis = "已接入压缩包旧版检测作对照：旧版全文搜索会认为有 iDEAL，但 Stripe 明确支付方式列表没有 iDEAL；继续 confirm 会得到 payment_method_types_mismatch。"
        else:
            diagnosis = "Stripe init 页实际支付方式列表里没有 iDEAL；已显示压缩包旧版检测结果 legacy_zip_logic_contains_ideal，继续 confirm 只会失败。"
    elif "payment_method_types_mismatch" in low and "payment_method_type=kakao_pay" in low:
        category = "KAKAO 未开放 / 支付方式不匹配"
        category_key = "kakao_not_available"
        subtype = "kakao_pay not in payment_method_types"
        diagnosis = "当前 Stripe checkout 没有真正开放 KAKAO Pay；需换 KR 出口/账号/Session 组合。"
    elif ("payment_method_types_mismatch" in low and "payment_method_type=twint" in low) or "did not expose twint" in low:
        category = "TWINT 未开放 / 支付方式不匹配"
        category_key = "twint_not_available"
        subtype = "twint not in payment_method_types"
        diagnosis = "当前 Stripe checkout 没有真正开放 TWINT；需换 CH 出口/账号/Session 组合。"
    elif ("payment_method_types_mismatch" in low and "payment_method_type=promptpay" in low) or "did not expose promptpay" in low:
        category = "PromptPay 未开放 / 支付方式不匹配"
        category_key = "promptpay_not_available"
        subtype = "promptpay not in payment_method_types"
        diagnosis = "当前 Stripe checkout 没有真正开放 PromptPay；需换 TH 出口/账号/Session 组合。"
    elif "did not expose paypal" in low or ("payment_method_types_mismatch" in low and "payment_method_type=paypal" in low):
        category = "PayPal advertised-list / explicit PM stage"
        category_key = "paypal_explicit_pm_stage"
        if ("paypal全球" in raw or "paypal global" in low or "paypal" in low) and "after billing/tax sync" in low:
            subtype = "historical advertised-method gate"
            diagnosis = "Old advertised-method gate log. Current build treats card/link as observation, then continues explicit PayPal PaymentMethod."
        elif "payment_method_types_mismatch" in low and "payment_method_type=paypal" in low:
            subtype = "explicit paypal confirm mismatch"
            diagnosis = "Flow already reached explicit PayPal PaymentMethod/confirm; Stripe returned mismatch at confirm stage, not at init gate."
        else:
            subtype = "paypal advertised list missing"
            diagnosis = "For non-GB, current build should continue past card/link into explicit PayPal PaymentMethod; refresh page and start a new run if this appears."
    elif "no upi qr data found after approval" in low or "upi qr missing" in low or "retrieve no qr" in low:
        category = "UPI QR missing"
        category_key = "upi_qr_missing"
        subtype = "approved but no qr"
        diagnosis = "UPI 2.0 reached approve, but Stripe did not return QR fields. Try another AT / IN exit / promo proxy combo, or add Cookie for the hydration stage."
    elif "failed to set stripe tax region" in low or "confirm payment method failed" in low or "stripe confirm" in low or "stripe submission failed" in low:
        category = "Stripe stage error"
        category_key = "stripe_error"
        subtype = "stripe tax/update/confirm failed"
        diagnosis = "Source UPI Stripe tax update or UPI confirm failed; check IN exit proxy and checkout session validity."
    elif "paypal全球轮转 oaics amount is not 0 after promo" in low:
        category = "OAICS 优惠未归零"
        category_key = "oaics_amount_not_zero_after_promo"
        subtype = "promo non-zero"
        diagnosis = "OAICS checkout/update 成功返回，但金额仍不是 0；说明这次优惠没应用到账单，会自动重试其它账号/代理组合。"
    elif "amount is not zero" in low or "amount is not 0" in low:
        category = "金额非 0"
        category_key = "amount_not_zero"
        subtype = "non-zero amount"
        diagnosis = "当前 checkout 金额没有归零；优先换优惠代理或账号重试。"
    elif "access token" in low or "unauthorized" in low or "http 401" in low:
        category = "认证 / Cookie 错误"
        category_key = "auth_or_cookie_error"
        subtype = "auth/cookie"
        diagnosis = "Session 或 Cookie 可能失效/不匹配。"

    return {
        "error_category": category,
        "error_category_key": category_key,
        "error_subtype": subtype,
        "error_diagnosis": diagnosis,
    }


def _generate_with_retries(access_token: str, mode_name: str, data: dict,
                           progress_callback=None) -> dict:
    attempts = _requested_attempts(data)
    cancel_token = str(data.get("cancel_token") or "").strip()
    cancel_event = _register_cancel_token(cancel_token)
    local = data.get("local_proxy", "")
    proxy = data.get("proxy", "")
    payment_proxy = data.get("payment_proxy", "")
    provider_proxy = data.get("provider_proxy", "") or data.get("paypal_proxy", "")
    proxy_pool = _pool_from_text(data.get("proxy_pool", ""))
    payment_proxy_pool = _pool_from_text(data.get("payment_proxy_pool", ""))
    provider_proxy_pool = _pool_from_text(data.get("provider_proxy_pool", "") or data.get("paypal_proxy_pool", ""))
    use_gost_bridge = _truthy(data.get("gost_bridge"))
    paypal_mode = _is_paypal_mode(mode_name)
    split_stage_mode = _is_split_stage_mode(mode_name)
    true_no_card_us_mode = _is_true_no_card_us_mode(mode_name)
    team_codex_low_mode = _is_team_codex_low_mode(mode_name)
    ph_cross_region_mode = _is_ph_cross_region_mode(mode_name)
    ph_gcash_mode = _is_ph_gcash_mode(mode_name)
    ba_pm_711_mode = _is_ba_pm_711_mode(mode_name)
    paypal_global_rotation_mode = _is_paypal_global_rotation_mode(mode_name)
    paypal_global_no_discount_mode = _is_paypal_global_no_discount_mode(mode_name)
    paypal_global_mode = paypal_global_rotation_mode or paypal_global_no_discount_mode
    upi_v2_mode = _is_upi_v2_mode(mode_name)
    kakao_v2_mode = _is_kakao_v2_mode(mode_name)
    twint_v2_mode = _is_twint_v2_mode(mode_name)
    promptpay_v2_mode = _is_promptpay_v2_mode(mode_name)
    pix_mode = _is_pix_mode(mode_name)
    pix_v2_mode = _is_pix_v2_mode(mode_name)
    if split_stage_mode:
        use_gost_bridge = True
    if ba_pm_711_mode or upi_v2_mode or kakao_v2_mode or twint_v2_mode or promptpay_v2_mode or paypal_global_mode or pix_mode:
        # BA/PM, UPI 2.0, PIX and PAYPAL global rotation let the UI checkbox decide whether
        # SOCKS proxies should be bridged through GOST or used as direct socks5h.
        use_gost_bridge = _truthy(data.get("gost_bridge"))
    if true_no_card_us_mode or team_codex_low_mode or paypal_global_mode:
        # These modes use direct socks5h proxies; PAYPAL global billing now comes from the user's 21-country selection.
        use_gost_bridge = False
    paypal_strategies = _paypal_strategies(data, mode_name)
    worker_strategy = _strategy_for_worker(paypal_strategies, 1) if paypal_strategies else ""
    failures: list[str] = []
    last_label = "直连"
    last_effective = ""
    promo_403_provider_blacklist: set[str] = set()

    try:
        for attempt in range(1, attempts + 1):
            if cancel_event and cancel_event.is_set():
                return _cancelled_response(attempt, attempts, failures, last_label, last_effective)

            # Proxy policy: rotate between attempts, but keep selected entry/exit proxies
            # sticky inside the current attempt (checkout -> Stripe -> approve -> polling).
            attempt_proxy = "" if split_stage_mode else proxy or _pick_proxy(proxy_pool)
            if split_stage_mode:
                attempt_payment_proxy = _pick_stage_proxy_from_input(
                    _stage_entry_proxy_text(data, mode_name),
                    "",
                    [],
                    default_scheme="socks5h",
                )
            else:
                attempt_payment_proxy = payment_proxy or _pick_proxy(payment_proxy_pool, default_scheme="socks5h")
            attempt_provider_proxy = ""
            if split_stage_mode:
                attempt_provider_proxy = _pick_stage_proxy_from_input(
                    _stage_exit_proxy_text(data, mode_name),
                    "",
                    [],
                    default_scheme="socks5h",
                    excluded=promo_403_provider_blacklist if paypal_global_rotation_mode else None,
                )
                if _is_nl_ideal_v3_mode(mode_name) or _is_nl_ideal_v2_mode(mode_name) or _is_ba_pm_711_mode(mode_name) or kakao_v2_mode or twint_v2_mode or promptpay_v2_mode or (pix_mode and not pix_v2_mode):
                    attempt_provider_proxy = opll_normalize_vn_country_proxy(attempt_provider_proxy)
            elif paypal_mode and not paypal_global_no_discount_mode and (not paypal_strategies or worker_strategy == "jp_us_split"):
                attempt_provider_proxy = provider_proxy or _pick_proxy(provider_proxy_pool, default_scheme="socks5h")
            attempt_pix_final_proxy = ""
            if pix_mode and not pix_v2_mode:
                attempt_pix_final_proxy = _pick_stage_proxy_from_input(
                    _stage_final_proxy_text(data, mode_name),
                    "",
                    [],
                    default_scheme="socks5h",
                )
            attempt_started = time.perf_counter()
            effective = ""
            provider_effective = ""
            pix_final_effective = ""
            label = ""

            try:
                with contextlib.ExitStack() as stack:
                    effective, checkout_label = _build_stage_proxy(
                        "" if split_stage_mode else local,
                        attempt_proxy,
                        attempt_payment_proxy,
                        use_gost_bridge,
                        stack,
                    )
                    provider_effective = effective
                    provider_label = checkout_label
                    if pix_mode:
                        provider_effective = ""
                        provider_label = "optional empty"
                    if (paypal_mode or split_stage_mode) and attempt_provider_proxy:
                        provider_effective, provider_label = _build_stage_proxy(
                            "", "", attempt_provider_proxy, use_gost_bridge, stack
                        )
                    pix_final_label = ""
                    if pix_mode and attempt_pix_final_proxy:
                        pix_final_effective, pix_final_label = _build_stage_proxy(
                            "", "", attempt_pix_final_proxy, use_gost_bridge, stack
                        )
                    elif pix_mode and _is_pix_postpromo_mode(mode_name):
                        pix_final_effective = effective
                        pix_final_label = f"{checkout_label or 'direct'} (reuse BR-1)"
                    label = _combined_proxy_label(checkout_label, provider_label, paypal_mode or split_stage_mode)
                    if pix_mode:
                        if pix_v2_mode:
                            label = f"PIX 2.0 BR {checkout_label or 'direct'} | promo {provider_label or 'direct'} | Stripe/approve reuses BR"
                        elif _is_pix_normal_mode(mode_name):
                            label = f"BR {checkout_label or 'direct'}"
                        elif _is_pix_postpromo_mode(mode_name):
                            label = f"BR-1 {checkout_label or 'direct'} | VN {provider_label or 'optional empty'} | BR-final {pix_final_label or 'reuse BR-1'}"
                        else:
                            label = f"BR-1 {checkout_label or 'direct'} | VN {provider_label or 'optional empty'} | BR-3 {pix_final_label or 'direct'}"
                    if paypal_global_rotation_mode and attempt_provider_proxy:
                        label = f"main PayPal {checkout_label or 'direct'} | promo {provider_label} | stage3 reuses main PayPal"
                    elif paypal_global_no_discount_mode:
                        label = f"PayPal global no-discount {checkout_label or 'direct'} | all stages reuse same proxy"
                    elif ph_cross_region_mode:
                        label = f"PH Checkout {checkout_label or 'direct'} | Promotion {provider_label or 'direct'}"
                    elif ph_gcash_mode:
                        label = f"GCash PH Checkout/Adyen {checkout_label or 'direct'} | Promotion {provider_label or 'direct'}"
                    last_label = label
                    last_effective = effective
                    _emit_progress(progress_callback, {
                        "event": "attempt_start",
                        "attempt": attempt,
                        "max_attempts": attempts,
                        "proxy_used": label,
                        "strategy": _strategy_label(worker_strategy) if worker_strategy else "",
                        "started_at": datetime.utcnow().isoformat() + "Z",
                    })
                    result = generate_payment_link(
                        access_token,
                        mode_name,
                        effective,
                        provider_proxy_url=provider_effective if (paypal_mode or split_stage_mode) else "",
                        pix_final_proxy_url=pix_final_effective if pix_mode else "",
                        progress_callback=lambda event: _emit_progress(progress_callback, {
                            **event,
                            "attempt": attempt,
                            "max_attempts": attempts,
                            "proxy_used": label,
                            "strategy": _strategy_label(worker_strategy) if worker_strategy else "",
                        }),
                        chatgpt_cookie=str(data.get("chatgpt_cookie") or data.get("cookie") or ""),
                        upi_approve_mode=str(data.get("upi_approve_mode") or data.get("approve_mode") or "full_auto"),
                        upi_region=str(data.get("upi_region") or "IN"),
                        upi_payment_locale=str(data.get("upi_payment_locale") or data.get("payment_locale") or "en"),
                        upi_payment_email=str(data.get("upi_payment_email") or data.get("payment_email") or ""),
                        paypal_billing_country=str(data.get("paypal_billing_country") or "JP"),
                        paypal_payment_locale=str(data.get("paypal_payment_locale") or "en"),
                        paypal_billing_email=str(data.get("paypal_billing_email") or ""),
                        paypal_proxy_country=str(data.get("paypal_proxy_country") or data.get("paypal_main_proxy_country") or ""),
                        paypal_promo_country=str(data.get("paypal_promo_country") or data.get("promo_proxy_country") or ""),
                        paypal_checkout_branch=str(data.get("paypal_checkout_branch") or "auto"),
                    )
                duration = round(time.perf_counter() - attempt_started, 2)
                long_url = result.get("long_url") or ""
                if not long_url:
                    raise RuntimeError("生成结果为空，未返回 long_url")
                success_count = _increment_success_count()
                _emit_progress(progress_callback, {
                    "event": "attempt_success",
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "proxy_used": label,
                    "duration_seconds": duration,
                    "long_url": long_url,
                    "success_count": success_count,
                    **_payment_result_public_fields(result),
                })
                return {
                    "ok": True,
                    "long_url": long_url,
                    "payment_mode": mode_name,
                    "proxy_used": label,
                    "proxy_url": mask_proxy_url(effective) if effective else "",
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "duration_seconds": duration,
                    "success_count": success_count,
                    "strategy": _strategy_label(worker_strategy) if worker_strategy else "",
                    "errors": failures[-10:],
                    "result": {k: v for k, v in result.items()},
                    **_payment_result_public_fields(result),
                }
            except Exception as exc:
                duration = round(time.perf_counter() - attempt_started, 2)
                error_text = f"第 {attempt} 次 / {duration} 秒 / 代理 {label}: {exc}"
                failures.append(error_text)
                error_info = _classify_attempt_error(str(exc))
                if paypal_global_rotation_mode and error_info.get("category_key") == "promotion_http_403" and attempt_provider_proxy:
                    promo_403_provider_blacklist.add(_proxy_identity(attempt_provider_proxy, "socks5h"))
                _emit_progress(progress_callback, {
                    "event": "attempt_error",
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "proxy_used": label,
                    "duration_seconds": duration,
                    "error": str(exc),
                    "strategy": _strategy_label(worker_strategy) if worker_strategy else "",
                    **error_info,
                })
                if cancel_event and cancel_event.is_set():
                    return _cancelled_response(attempt, attempts, failures, label, effective)
    finally:
        _clear_cancel_token(cancel_token)

    return {
        "ok": False,
        "error": f"{attempts} 次内仍未提链成功；最后错误: {failures[-1] if failures else '未知错误'}",
        "attempt": attempts,
        "max_attempts": attempts,
        "proxy_used": last_label,
        "proxy_url": mask_proxy_url(last_effective) if last_effective else "",
        "errors": failures[-10:],
    }


def _update_job(job_id: str, **changes) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(changes)


def _record_job_progress(job_id: str, event: dict) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        event_name = event.get("event")
        if event_name == "attempt_start":
            job["current_attempt"] = event.get("attempt", 0)
            job["status_text"] = f"正在第 {event.get('attempt')}/{event.get('max_attempts')} 次提链"
            job["current_proxy"] = event.get("proxy_used", "")
            job["stage_state"] = {
                "1": {
                    "worker_id": 1,
                    "attempt": event.get("attempt", 0),
                    "max_attempts": event.get("max_attempts"),
                    "stage_label": "准备开始",
                    "stage_index": 0,
                    "stage_total": 7,
                    "stage_at": time.time(),
                    "proxy_used": event.get("proxy_used", ""),
                    "strategy": event.get("strategy", ""),
                }
            }
        elif event_name == "attempt_stage":
            state = job.setdefault("stage_state", {})
            state["1"] = {
                "worker_id": 1,
                "attempt": event.get("attempt", job.get("current_attempt", 0)),
                "max_attempts": event.get("max_attempts", job.get("max_attempts")),
                "stage_key": event.get("stage_key", ""),
                "stage_label": event.get("stage_label", "处理中"),
                "stage_index": event.get("stage_index", 0),
                "stage_total": event.get("stage_total", 7),
                "stage_at": event.get("stage_at") or time.time(),
                "proxy_used": event.get("proxy_used", job.get("current_proxy", "")),
                "strategy": event.get("strategy", ""),
            }
            manual_fields = (
                "manual_approval_required", "manual_approval_url", "manual_pay_url",
                "manual_stripe_url", "manual_cs_id", "manual_wait_seconds",
            )
            for manual_key in manual_fields:
                if manual_key in event and event.get(manual_key) not in (None, ""):
                    state["1"][manual_key] = event.get(manual_key)
            if event.get("manual_approval_required"):
                job["manual_approval"] = {k: state["1"].get(k) for k in manual_fields if state["1"].get(k) not in (None, "")}
                job["manual_approval"].update({
                    "attempt": state["1"].get("attempt"),
                    "proxy_used": state["1"].get("proxy_used", ""),
                    "stage_at": state["1"].get("stage_at"),
                })
            job["status_text"] = f"正在第 {state['1']['attempt']}/{state['1']['max_attempts']} 次：{state['1']['stage_label']}"
        elif event_name in ("attempt_error", "attempt_success"):
            attempts_log = job.setdefault("attempts_log", [])
            attempts_log.append(event)
            job["current_attempt"] = event.get("attempt", job.get("current_attempt", 0))
            state = job.setdefault("stage_state", {})
            if event_name == "attempt_error":
                state["1"] = {
                    "worker_id": 1,
                    "attempt": event.get("attempt", 0),
                    "max_attempts": event.get("max_attempts"),
                    "stage_label": "失败，准备下一次",
                    "stage_index": 0,
                    "stage_total": 7,
                    "stage_at": time.time(),
                    "proxy_used": event.get("proxy_used", ""),
                    "strategy": event.get("strategy", ""),
                }
                job["status_text"] = (
                    f"第 {event.get('attempt')}/{event.get('max_attempts')} 次失败，"
                    f"耗时 {event.get('duration_seconds')} 秒"
                )
            else:
                state["1"] = {
                    "worker_id": 1,
                    "attempt": event.get("attempt", 0),
                    "max_attempts": event.get("max_attempts"),
                    "stage_label": "成功",
                    "stage_index": event.get("stage_total", 7),
                    "stage_total": event.get("stage_total", 7),
                    "stage_at": time.time(),
                    "proxy_used": event.get("proxy_used", ""),
                    "strategy": event.get("strategy", ""),
                }
                job["status_text"] = (
                    f"第 {event.get('attempt')}/{event.get('max_attempts')} 次成功，"
                    f"耗时 {event.get('duration_seconds')} 秒"
                )
                job["success_count"] = event.get("success_count", _success_count())


def _record_job_progress_v2(job_id: str, event: dict) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        event_name = event.get("event")
        worker_id = event.get("worker_id")
        worker_prefix = f"线路 {worker_id} " if worker_id else ""
        attempt = int(event.get("attempt") or 0)
        max_attempts = event.get("max_attempts")
        if event_name == "attempt_start":
            job["current_attempt"] = max(int(job.get("current_attempt") or 0), attempt)
            job["status_text"] = f"{worker_prefix}正在第 {attempt}/{max_attempts} 次提链"
            job["current_proxy"] = event.get("proxy_used", "")
            state = job.setdefault("stage_state", {})
            state[str(worker_id or 1)] = {
                "worker_id": worker_id or 1,
                "session_index": event.get("session_index"),
                "attempt": attempt,
                "max_attempts": max_attempts,
                "stage_label": "准备开始",
                "stage_index": 0,
                "stage_total": 7,
                "stage_at": time.time(),
                "proxy_used": event.get("proxy_used", ""),
                "strategy": event.get("strategy", ""),
            }
            if event.get("session_count"):
                job["session_count"] = event.get("session_count")
        elif event_name == "attempt_stage":
            state = job.setdefault("stage_state", {})
            key = str(worker_id or 1)
            state[key] = {
                "worker_id": worker_id or 1,
                "session_index": event.get("session_index"),
                "attempt": attempt,
                "max_attempts": max_attempts,
                "stage_key": event.get("stage_key", ""),
                "stage_label": event.get("stage_label", "处理中"),
                "stage_index": event.get("stage_index", 0),
                "stage_total": event.get("stage_total", 7),
                "stage_at": event.get("stage_at") or time.time(),
                "proxy_used": event.get("proxy_used", job.get("current_proxy", "")),
                "strategy": event.get("strategy", ""),
            }
            manual_fields = (
                "manual_approval_required", "manual_approval_url", "manual_pay_url",
                "manual_stripe_url", "manual_cs_id", "manual_wait_seconds",
            )
            for manual_key in manual_fields:
                if manual_key in event and event.get(manual_key) not in (None, ""):
                    state[key][manual_key] = event.get(manual_key)
            if event.get("manual_approval_required"):
                job["manual_approval"] = {k: state[key].get(k) for k in manual_fields if state[key].get(k) not in (None, "")}
                job["manual_approval"].update({
                    "worker_id": worker_id or 1,
                    "session_index": event.get("session_index"),
                    "attempt": attempt,
                    "proxy_used": state[key].get("proxy_used", ""),
                    "stage_at": state[key].get("stage_at"),
                })
            job["status_text"] = f"{worker_prefix}第 {attempt}/{max_attempts} 次：{state[key]['stage_label']}"
        elif event_name in ("attempt_error", "attempt_success"):
            attempts_log = job.setdefault("attempts_log", [])
            attempts_log.append(event)
            total_budget = int(event.get("total_attempt_budget") or job.get("max_attempts") or event.get("max_attempts") or 0)
            if total_budget:
                job["max_attempts"] = total_budget
            job["current_attempt"] = len(attempts_log)
            state = job.setdefault("stage_state", {})
            key = str(worker_id or 1)
            if event_name == "attempt_error":
                state[key] = {
                    "worker_id": worker_id or 1,
                    "session_index": event.get("session_index"),
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "stage_label": "失败，准备下一次",
                    "stage_index": 0,
                    "stage_total": event.get("stage_total", 7),
                    "stage_at": time.time(),
                    "proxy_used": event.get("proxy_used", ""),
                    "strategy": event.get("strategy", ""),
                }
                job["status_text"] = f"{worker_prefix}第 {attempt}/{max_attempts} 次失败，耗时 {event.get('duration_seconds')} 秒"
            else:
                state[key] = {
                    "worker_id": worker_id or 1,
                    "session_index": event.get("session_index"),
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "stage_label": "成功",
                    "stage_index": event.get("stage_total", 7),
                    "stage_total": event.get("stage_total", 7),
                    "stage_at": time.time(),
                    "proxy_used": event.get("proxy_used", ""),
                    "strategy": event.get("strategy", ""),
                }
                job["status_text"] = f"{worker_prefix}第 {attempt}/{max_attempts} 次成功，耗时 {event.get('duration_seconds')} 秒"
                job["success_count"] = event.get("success_count", _success_count())


def _run_retry_job(job_id: str, access_token: str, mode_name: str, data: dict) -> None:
    try:
        result = _generate_with_retries(
            access_token,
            mode_name,
            data,
            progress_callback=lambda event: _record_job_progress_v2(job_id, event),
        )
        if result.get("ok"):
            result["access_token"] = access_token
            status = "success"
        elif result.get("cancelled"):
            status = "cancelled"
        else:
            status = "failed"
        _update_job(
            job_id,
            status=status,
            done=True,
            result=result,
            finished_at=datetime.utcnow().isoformat() + "Z",
            success_count=_success_count(),
        )
    except Exception as exc:
        _update_job(
            job_id,
            status="failed",
            done=True,
            result={"ok": False, "error": str(exc), "success_count": _success_count()},
            finished_at=datetime.utcnow().isoformat() + "Z",
            success_count=_success_count(),
        )


def _run_concurrent_retry_job(job_id: str, access_tokens: list[str], mode_name: str, data: dict) -> None:
    attempts = _requested_attempts(data)
    concurrency = _requested_concurrency(data)
    cancel_event = _register_cancel_token(job_id)
    local = data.get("local_proxy", "")
    proxy = data.get("proxy", "")
    payment_proxy = data.get("payment_proxy", "")
    provider_proxy = data.get("provider_proxy", "") or data.get("paypal_proxy", "")
    proxy_pool = _pool_from_text(data.get("proxy_pool", ""))
    payment_proxy_pool = _pool_from_text(data.get("payment_proxy_pool", ""))
    provider_proxy_pool = _pool_from_text(data.get("provider_proxy_pool", "") or data.get("paypal_proxy_pool", ""))
    use_gost_bridge = _truthy(data.get("gost_bridge"))
    paypal_mode = _is_paypal_mode(mode_name)
    split_stage_mode = _is_split_stage_mode(mode_name)
    true_no_card_us_mode = _is_true_no_card_us_mode(mode_name)
    team_codex_low_mode = _is_team_codex_low_mode(mode_name)
    ba_pm_711_mode = _is_ba_pm_711_mode(mode_name)
    paypal_global_rotation_mode = _is_paypal_global_rotation_mode(mode_name)
    paypal_global_no_discount_mode = _is_paypal_global_no_discount_mode(mode_name)
    paypal_global_mode = paypal_global_rotation_mode or paypal_global_no_discount_mode
    upi_v2_mode = _is_upi_v2_mode(mode_name)
    kakao_v2_mode = _is_kakao_v2_mode(mode_name)
    twint_v2_mode = _is_twint_v2_mode(mode_name)
    promptpay_v2_mode = _is_promptpay_v2_mode(mode_name)
    pix_mode = _is_pix_mode(mode_name)
    pix_v2_mode = _is_pix_v2_mode(mode_name)
    if split_stage_mode:
        use_gost_bridge = True
    if ba_pm_711_mode or upi_v2_mode or kakao_v2_mode or twint_v2_mode or promptpay_v2_mode or paypal_global_mode or pix_mode:
        # BA/PM, UPI 2.0, PIX and PAYPAL global rotation let the UI checkbox decide whether
        # SOCKS proxies should be bridged through GOST or used as direct socks5h.
        use_gost_bridge = _truthy(data.get("gost_bridge"))
    if true_no_card_us_mode or team_codex_low_mode or paypal_global_mode:
        # These modes use direct socks5h proxies; PAYPAL global billing now comes from the user's 21-country selection.
        use_gost_bridge = False
    paypal_strategies = _paypal_strategies(data, mode_name)
    state_lock = threading.Lock()
    total_attempt_budget = attempts * concurrency
    state = {
        "completed_attempts": 0,
        "results": {},
        "failures": [],
        "last_label": "",
        "last_effective": "",
    }

    def worker(worker_id: int) -> None:
        worker_token = _token_for_worker(access_tokens, worker_id)
        session_index = ((worker_id - 1) % len(access_tokens)) + 1
        worker_strategy = _strategy_for_worker(paypal_strategies, worker_id) if paypal_strategies else ""
        promo_403_provider_blacklist: set[str] = set()
        for attempt in range(1, attempts + 1):
            with state_lock:
                # A successful worker only finishes its own line.  The shared cancel
                # event is reserved for an explicit stop request, otherwise one fast
                # result would incorrectly terminate every other concurrent line.
                should_stop = bool(cancel_event and cancel_event.is_set())
            if should_stop:
                return
            # Proxy policy: rotate between attempts, but keep selected entry/exit proxies
            # sticky inside the current attempt (checkout -> Stripe -> approve -> polling).
            attempt_proxy = "" if split_stage_mode else proxy or _pick_proxy(proxy_pool)
            if split_stage_mode:
                attempt_payment_proxy = _pick_stage_proxy_from_input(
                    _stage_entry_proxy_text(data, mode_name),
                    "",
                    [],
                    default_scheme="socks5h",
                )
            else:
                attempt_payment_proxy = payment_proxy or _pick_proxy(payment_proxy_pool, default_scheme="socks5h")
            attempt_provider_proxy = ""
            if split_stage_mode:
                attempt_provider_proxy = _pick_stage_proxy_from_input(
                    _stage_exit_proxy_text(data, mode_name),
                    "",
                    [],
                    default_scheme="socks5h",
                    excluded=promo_403_provider_blacklist if paypal_global_rotation_mode else None,
                )
                if _is_nl_ideal_v3_mode(mode_name) or _is_nl_ideal_v2_mode(mode_name) or _is_ba_pm_711_mode(mode_name) or kakao_v2_mode or twint_v2_mode or promptpay_v2_mode or (pix_mode and not pix_v2_mode):
                    attempt_provider_proxy = opll_normalize_vn_country_proxy(attempt_provider_proxy)
            elif paypal_mode and not paypal_global_no_discount_mode and (not paypal_strategies or worker_strategy == "jp_us_split"):
                attempt_provider_proxy = provider_proxy or _pick_proxy(provider_proxy_pool, default_scheme="socks5h")
            attempt_pix_final_proxy = ""
            if pix_mode and not pix_v2_mode:
                attempt_pix_final_proxy = _pick_stage_proxy_from_input(
                    _stage_final_proxy_text(data, mode_name),
                    "",
                    [],
                    default_scheme="socks5h",
                )
            attempt_started = time.perf_counter()
            effective = ""
            provider_effective = ""
            pix_final_effective = ""
            label = ""
            try:
                with contextlib.ExitStack() as stack:
                    effective, checkout_label = _build_stage_proxy(
                        "" if split_stage_mode else local,
                        attempt_proxy,
                        attempt_payment_proxy,
                        use_gost_bridge,
                        stack,
                    )
                    provider_effective = effective
                    provider_label = checkout_label
                    if pix_mode:
                        provider_effective = ""
                        provider_label = "optional empty"
                    if (paypal_mode or split_stage_mode) and attempt_provider_proxy:
                        provider_effective, provider_label = _build_stage_proxy(
                            "", "", attempt_provider_proxy, use_gost_bridge, stack
                        )
                    pix_final_label = ""
                    if pix_mode and attempt_pix_final_proxy:
                        pix_final_effective, pix_final_label = _build_stage_proxy(
                            "", "", attempt_pix_final_proxy, use_gost_bridge, stack
                        )
                    elif pix_mode and _is_pix_postpromo_mode(mode_name):
                        pix_final_effective = effective
                        pix_final_label = f"{checkout_label or 'direct'} (reuse BR-1)"
                    label = _combined_proxy_label(checkout_label, provider_label, paypal_mode or split_stage_mode)
                    if pix_mode:
                        if pix_v2_mode:
                            label = f"PIX 2.0 BR {checkout_label or 'direct'} | promo {provider_label or 'direct'} | Stripe/approve reuses BR"
                        elif _is_pix_normal_mode(mode_name):
                            label = f"BR {checkout_label or 'direct'}"
                        elif _is_pix_postpromo_mode(mode_name):
                            label = f"BR-1 {checkout_label or 'direct'} | VN {provider_label or 'optional empty'} | BR-final {pix_final_label or 'reuse BR-1'}"
                        else:
                            label = f"BR-1 {checkout_label or 'direct'} | VN {provider_label or 'optional empty'} | BR-3 {pix_final_label or 'direct'}"
                    if paypal_global_rotation_mode and attempt_provider_proxy:
                        label = f"main PayPal {checkout_label or 'direct'} | promo {provider_label} | stage3 reuses main PayPal"
                    elif paypal_global_no_discount_mode:
                        label = f"PayPal global no-discount {checkout_label or 'direct'} | all stages reuse same proxy"
                    with state_lock:
                        state["last_label"] = label
                        state["last_effective"] = effective
                    _record_job_progress_v2(job_id, {
                        "event": "attempt_start",
                        "attempt": attempt,
                        "max_attempts": attempts,
                        "total_attempt_budget": total_attempt_budget,
                        "worker_id": worker_id,
                        "session_index": session_index,
                        "session_count": len(access_tokens),
                        "proxy_used": label,
                        "strategy": _strategy_label(worker_strategy) if worker_strategy else "",
                        "started_at": datetime.utcnow().isoformat() + "Z",
                    })
                    result = generate_payment_link(
                        worker_token,
                        mode_name,
                        effective,
                        provider_proxy_url=provider_effective if (paypal_mode or split_stage_mode) else "",
                        pix_final_proxy_url=pix_final_effective if pix_mode else "",
                        progress_callback=lambda event: _record_job_progress_v2(job_id, {
                            **event,
                            "attempt": attempt,
                            "max_attempts": attempts,
                            "total_attempt_budget": total_attempt_budget,
                            "worker_id": worker_id,
                            "session_index": session_index,
                            "session_count": len(access_tokens),
                            "proxy_used": label,
                            "strategy": _strategy_label(worker_strategy) if worker_strategy else "",
                        }),
                        chatgpt_cookie=str(data.get("chatgpt_cookie") or data.get("cookie") or ""),
                        upi_approve_mode=str(data.get("upi_approve_mode") or data.get("approve_mode") or "full_auto"),
                        upi_region=str(data.get("upi_region") or "IN"),
                        upi_payment_locale=str(data.get("upi_payment_locale") or data.get("payment_locale") or "en"),
                        upi_payment_email=str(data.get("upi_payment_email") or data.get("payment_email") or ""),
                        paypal_billing_country=str(data.get("paypal_billing_country") or "JP"),
                        paypal_payment_locale=str(data.get("paypal_payment_locale") or "en"),
                        paypal_billing_email=str(data.get("paypal_billing_email") or ""),
                        paypal_proxy_country=str(data.get("paypal_proxy_country") or data.get("paypal_main_proxy_country") or ""),
                        paypal_promo_country=str(data.get("paypal_promo_country") or data.get("promo_proxy_country") or ""),
                        paypal_checkout_branch=str(data.get("paypal_checkout_branch") or "auto"),
                    )

                duration = round(time.perf_counter() - attempt_started, 2)
                long_url = result.get("long_url") or ""
                if not long_url:
                    raise RuntimeError("生成结果为空，未返回 long_url")
                with state_lock:
                    success_count = _increment_success_count()
                    response = {
                        "ok": True,
                        "long_url": long_url,
                        "payment_mode": mode_name,
                        "proxy_used": label,
                        "proxy_url": mask_proxy_url(effective) if effective else "",
                        "attempt": attempt,
                        "max_attempts": attempts,
                        "total_attempt_budget": total_attempt_budget,
                        "worker_id": worker_id,
                        "session_index": session_index,
                        "session_count": len(access_tokens),
                        "access_token": worker_token,
                        "concurrency": concurrency,
                        "duration_seconds": duration,
                        "success_count": success_count,
                        "strategy": _strategy_label(worker_strategy) if worker_strategy else "",
                        "errors": [],
                        "result": {k: v for k, v in result.items()},
                        **_payment_result_public_fields(result),
                    }
                    state["results"][worker_id] = response
                _record_job_progress_v2(job_id, {
                    "event": "attempt_success",
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "total_attempt_budget": total_attempt_budget,
                    "worker_id": worker_id,
                    "session_index": session_index,
                    "session_count": len(access_tokens),
                    "proxy_used": label,
                    "duration_seconds": duration,
                    "long_url": long_url,
                    "success_count": success_count,
                    "strategy": _strategy_label(worker_strategy) if worker_strategy else "",
                    **_payment_result_public_fields(result),
                })
                return
            except Exception as exc:
                duration = round(time.perf_counter() - attempt_started, 2)
                error_text = f"线路 {worker_id} / 第 {attempt} 次 / {duration} 秒 / 代理 {label}: {exc}"
                with state_lock:
                    state["completed_attempts"] = int(state["completed_attempts"]) + 1
                    state["failures"].append(error_text)
                    state["last_label"] = label
                    state["last_effective"] = effective
                error_info = _classify_attempt_error(str(exc))
                if paypal_global_rotation_mode and error_info.get("category_key") == "promotion_http_403" and attempt_provider_proxy:
                    promo_403_provider_blacklist.add(_proxy_identity(attempt_provider_proxy, "socks5h"))
                _record_job_progress_v2(job_id, {
                    "event": "attempt_error",
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "total_attempt_budget": total_attempt_budget,
                    "worker_id": worker_id,
                    "session_index": session_index,
                    "session_count": len(access_tokens),
                    "proxy_used": label,
                    "duration_seconds": duration,
                    "error": str(exc),
                    "strategy": _strategy_label(worker_strategy) if worker_strategy else "",
                    **error_info,
                })

    threads = [
        threading.Thread(target=worker, args=(worker_id,), daemon=True)
        for worker_id in range(1, concurrency + 1)
    ]

    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        with state_lock:
            worker_results = [state["results"][worker_id] for worker_id in sorted(state["results"])]
            failures = list(state["failures"])
            last_label = str(state["last_label"] or "")
            last_effective = str(state["last_effective"] or "")

        if worker_results:
            # Keep the first successful response at the top level for compatibility
            # with existing result renderers, and expose every line result separately.
            result = dict(worker_results[0])
            result.update({
                "results": worker_results,
                "successful_workers": len(worker_results),
                "failed_workers": concurrency - len(worker_results),
                "partial_success": len(worker_results) < concurrency,
                "errors": failures[-10:],
            })
            status = "success"
            status_text = f"并发完成：{len(worker_results)}/{concurrency} 条线路成功"
        elif cancel_event and cancel_event.is_set():
            result = _cancelled_response(min(len(failures), total_attempt_budget), total_attempt_budget, failures, last_label, last_effective)
            result["concurrency"] = concurrency
            result["per_worker_attempts"] = attempts
            result["total_attempt_budget"] = total_attempt_budget
            status = "cancelled"
            status_text = "已停止并发提链"
        else:
            result = {
                "ok": False,
                "error": f"{concurrency} 条线路各 {attempts} 次内仍未提链成功；最后错误: {failures[-1] if failures else '未知错误'}",
                "attempt": total_attempt_budget,
                "max_attempts": total_attempt_budget,
                "per_worker_attempts": attempts,
                "total_attempt_budget": total_attempt_budget,
                "concurrency": concurrency,
                "proxy_used": last_label,
                "proxy_url": mask_proxy_url(last_effective) if last_effective else "",
                "errors": failures[-10:],
            }
            status = "failed"
            status_text = f"并发完成：0/{concurrency} 条线路成功"

        _update_job(
            job_id,
            status=status,
            status_text=status_text,
            done=True,
            result=result,
            finished_at=datetime.utcnow().isoformat() + "Z",
            success_count=_success_count(),
        )
    except Exception as exc:
        _update_job(
            job_id,
            status="failed",
            done=True,
            result={"ok": False, "error": str(exc), "success_count": _success_count()},
            finished_at=datetime.utcnow().isoformat() + "Z",
            success_count=_success_count(),
        )
    finally:
        _clear_cancel_token(job_id)


def _test_proxy_candidate(index: int, raw_proxy: str, use_gost_bridge: bool) -> dict:
    started = time.perf_counter()
    normalized = _normalize_payment_proxy(raw_proxy)
    label = mask_proxy_url(normalized) if normalized else "系统代理/直连"
    effective = normalized
    try:
        if use_gost_bridge and is_socks_proxy_url(normalized):
            bridge_context = GostHttpBridge(normalized)
        else:
            bridge_context = contextlib.nullcontext(None)

        with bridge_context as bridge:
            if bridge:
                effective = bridge.url
                label = f"{mask_proxy_url(normalized)} -> {mask_proxy_url(effective)}"
            session = requests.Session()
            session.trust_env = not bool(effective)
            if effective:
                session.proxies.update({"http": effective, "https": effective})
            chatgpt_status = 0
            try:
                cg_resp = session.get("https://chatgpt.com/cdn-cgi/trace", timeout=12)
                chatgpt_status = cg_resp.status_code
                if cg_resp.status_code >= 500:
                    raise RuntimeError(f"ChatGPT HTTP {cg_resp.status_code}")
            except Exception as exc:
                raise RuntimeError(f"ChatGPT 连通失败: {exc}") from exc

            stripe_status = 0
            try:
                stripe_resp = session.get("https://api.stripe.com/", timeout=12)
                stripe_status = stripe_resp.status_code
                if stripe_resp.status_code >= 500:
                    raise RuntimeError(f"Stripe HTTP {stripe_resp.status_code}")
            except Exception as exc:
                raise RuntimeError(f"Stripe connection failed: {exc}") from exc

            ip_info = {}
            ipinfo_error = ""
            try:
                ip_resp = session.get("https://ipinfo.io/json", timeout=12)
                if ip_resp.status_code >= 400:
                    raise RuntimeError(f"ipinfo HTTP {ip_resp.status_code}")
                ip_info = ip_resp.json() or {}
            except Exception as exc:
                ipinfo_error = str(exc)

        return {
            "ok": True,
            "index": index,
            "raw_proxy": raw_proxy,
            "proxy_used": label,
            "latency_seconds": round(time.perf_counter() - started, 2),
            "ip": ip_info.get("ip", ""),
            "country": ip_info.get("country", ""),
            "city": ip_info.get("city", ""),
            "org": ip_info.get("org", ""),
            "chatgpt_status": chatgpt_status,
            "stripe_status": stripe_status,
            "ipinfo_error": ipinfo_error,
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "index": index,
            "raw_proxy": raw_proxy,
            "proxy_used": label,
            "latency_seconds": round(time.perf_counter() - started, 2),
            "ip": "",
            "country": "",
            "city": "",
            "org": "",
            "chatgpt_status": 0,
            "error": str(exc),
        }



# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
MAIL_MANAGER_REPO_DIR = Path(__file__).resolve().parent / "services" / "xiaopingguo"
MAIL_MANAGER_STATIC_DIR = MAIL_MANAGER_REPO_DIR / "mail_manager" / "static"
MAIL_MANAGER_LITE_DB = MAIL_MANAGER_REPO_DIR / "mail_manager_lite.db"


def _mailmgr_conn():
    MAIL_MANAGER_REPO_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MAIL_MANAGER_LITE_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mail_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            email_normalized TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL DEFAULT '',
            client_id TEXT NOT NULL DEFAULT '',
            refresh_token TEXT NOT NULL DEFAULT '',
            bound INTEGER NOT NULL DEFAULT 0,
            bound_at TEXT,
            token_valid INTEGER NOT NULL DEFAULT 1,
            last_refresh_time TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return conn


def _mailmgr_account_dict(row):
    return {
        "id": int(row["id"]),
        "email": row["email"],
        "lastRefreshTime": row["last_refresh_time"],
        "bound": bool(row["bound"]),
        "boundAt": row["bound_at"],
        "tokenValid": bool(row["token_valid"]),
    }


def _mailmgr_credentials_dict(row):
    return {
        "email": row["email"] or "",
        "password": row["password"] or "",
        "clientId": row["client_id"] or "",
        "refreshToken": row["refresh_token"] or "",
    }


def _mailmgr_get_account(account_id: int):
    with _mailmgr_conn() as conn:
        return conn.execute("SELECT * FROM mail_accounts WHERE id = ?", (int(account_id),)).fetchone()


def _mailmgr_error(message: str, status: int = 400, code: str = "MAIL_MANAGER_ERROR"):
    return jsonify({"ok": False, "error": message, "code": code}), status



def _mailmgr_short_error(exc, limit=180):
    msg = str(exc or "")
    low = msg.lower()
    if "aadsts70000" in low or "invalid_grant" in low:
        return "Token scope mismatch; tried .default/Mail.Read"
    if "imap" in low and ("auth" in low or "xoauth2" in low):
        return "IMAP XOAUTH2 auth failed"
    msg = " ".join(msg.replace("\r", " ").replace("\n", " ").split())
    return msg[:limit]


def _mailmgr_update_rolling_token(row, token_data):
    new_refresh = (token_data or {}).get("refresh_token")
    if new_refresh and new_refresh != row["refresh_token"]:
        with _mailmgr_conn() as conn:
            conn.execute(
                "UPDATE mail_accounts SET refresh_token = ?, last_refresh_time = CURRENT_TIMESTAMP, token_valid = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_refresh, int(row["id"])),
            )


def _mailmgr_decode_header_value(value):
    from email.header import decode_header, make_header
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return str(value or "")


def _mailmgr_extract_message_body(msg):
    import re as _re

    def part_text(part):
        payload = part.get_payload(decode=True)
        if not payload:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except Exception:
            return payload.decode("utf-8", errors="replace")

    plain = ""
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            ctype = part.get_content_type().lower()
            if ctype == "text/plain" and not plain:
                plain = part_text(part)
            elif ctype == "text/html" and not html:
                html = part_text(part)
    else:
        ctype = msg.get_content_type().lower()
        if ctype == "text/html":
            html = part_text(msg)
        else:
            plain = part_text(msg)
    if plain.strip():
        return plain.strip()
    html = html or ""
    html = _re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = _re.sub(r"(?s)<[^>]+>", " ", html)
    html = _re.sub(r"\s+", " ", html)
    return html.strip()


def _mailmgr_imap_messages(row, folder: str = "inbox", limit: int = 100):
    import imaplib
    from email import policy as email_policy
    from email.parser import BytesParser
    from email.utils import parsedate_to_datetime
    from mail_outlook import get_outlook_access_token, IMAP_HOST

    client_id = str(row["client_id"] or "").strip()
    refresh_token = str(row["refresh_token"] or "").strip()
    email_addr = str(row["email"] or "").strip()
    token_data = get_outlook_access_token(refresh_token, client_id)
    _mailmgr_update_rolling_token(row, token_data)
    access_token = token_data.get("access_token")
    if not access_token:
        raise RuntimeError("IMAP access_token empty")

    folder = str(folder or "inbox").lower()
    folders = ["INBOX"] if folder != "junkemail" else ["Junk", "Junk Email", "Junk E-mail", "Spam"]
    limit = max(1, min(100, int(limit or 100)))
    mail = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    try:
        auth_string = f"user={email_addr}\x01auth=Bearer {access_token}\x01\x01"
        typ, _ = mail.authenticate("XOAUTH2", lambda _: auth_string.encode())
        if typ != "OK":
            raise RuntimeError("IMAP XOAUTH2 failed")
        selected = False
        for folder_name in folders:
            mailbox = '"%s"' % folder_name if " " in folder_name else folder_name
            typ, _ = mail.select(mailbox, readonly=True)
            if typ == "OK":
                selected = True
                break
        if not selected:
            typ, _ = mail.select("INBOX", readonly=True)
            if typ != "OK":
                raise RuntimeError("IMAP folder select failed")
        typ, data = mail.search(None, "ALL")
        if typ != "OK":
            raise RuntimeError("IMAP search failed")
        ids = (data[0] or b"").split()
        ids = list(reversed(ids[-limit:]))
        messages = []
        for mid in ids:
            typ, fetched = mail.fetch(mid, "(RFC822)")
            if typ != "OK" or not fetched:
                continue
            raw = None
            for item in fetched:
                if isinstance(item, tuple) and item[1]:
                    raw = item[1]
                    break
            if not raw:
                continue
            msg = BytesParser(policy=email_policy.default).parsebytes(raw)
            subject = _mailmgr_decode_header_value(msg.get("Subject", ""))
            sender = _mailmgr_decode_header_value(msg.get("From", ""))
            date_raw = str(msg.get("Date", ""))
            received = date_raw
            try:
                received = parsedate_to_datetime(date_raw).isoformat()
            except Exception:
                pass
            body = _mailmgr_extract_message_body(msg)
            messages.append({
                "id": mid.decode("ascii", errors="ignore"),
                "subject": subject,
                "bodyPreview": body[:240],
                "body": {"contentType": "text", "content": body},
                "sender": sender,
                "from": sender,
                "receivedDateTime": received,
            })
        return messages
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _mailmgr_graph_messages(row, folder: str = "inbox", limit: int = 100):
    try:
        from mail_outlook import get_graph_access_token
    except Exception as exc:
        raise RuntimeError(f"Graph module load failed: {exc}") from exc

    client_id = str(row["client_id"] or "").strip()
    refresh_token = str(row["refresh_token"] or "").strip()
    if not client_id or not refresh_token:
        raise RuntimeError("missing clientId or refreshToken")

    token_error = None
    try:
        token_data = get_graph_access_token(refresh_token, client_id)
    except Exception as exc:
        token_error = exc
        token_data = None
    if not token_data:
        try:
            return _mailmgr_imap_messages(row, folder=folder, limit=limit)
        except Exception as imap_exc:
            raise RuntimeError("mail read failed: Graph %s; IMAP %s" % (_mailmgr_short_error(token_error), _mailmgr_short_error(imap_exc)))

    access_token = token_data.get("access_token")
    if not access_token:
        try:
            return _mailmgr_imap_messages(row, folder=folder, limit=limit)
        except Exception as imap_exc:
            raise RuntimeError("mail read failed: empty Graph token; IMAP %s" % _mailmgr_short_error(imap_exc))
    _mailmgr_update_rolling_token(row, token_data)

    folder = str(folder or "inbox").lower()
    if folder not in {"inbox", "junkemail"}:
        folder = "inbox"
    limit = max(1, min(100, int(limit or 100)))
    params = {
        "$top": str(limit),
        "$orderby": "receivedDateTime desc",
        "$select": "subject,bodyPreview,body,from,receivedDateTime,id",
    }
    url = f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages"
    resp = requests.get(
        url,
        params=params,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Prefer": 'outlook.body-content-type="text"',
        },
        timeout=25,
    )
    if resp.status_code >= 400:
        try:
            return _mailmgr_imap_messages(row, folder=folder, limit=limit)
        except Exception as imap_exc:
            raise RuntimeError("mail read failed: Graph HTTP %s; IMAP %s" % (resp.status_code, _mailmgr_short_error(imap_exc)))
    data = resp.json()
    messages = []
    for msg in data.get("value") or []:
        from_obj = msg.get("from") or {}
        email_addr = ""
        name = ""
        if isinstance(from_obj, dict):
            email_obj = from_obj.get("emailAddress") or {}
            if isinstance(email_obj, dict):
                email_addr = str(email_obj.get("address") or "")
                name = str(email_obj.get("name") or "")
        body = msg.get("body") or {}
        content = body.get("content") if isinstance(body, dict) else ""
        messages.append({
            "id": msg.get("id") or "",
            "subject": msg.get("subject") or "",
            "bodyPreview": msg.get("bodyPreview") or "",
            "body": {"contentType": body.get("contentType", "text") if isinstance(body, dict) else "text", "content": content or ""},
            "sender": name or email_addr,
            "from": email_addr,
            "receivedDateTime": msg.get("receivedDateTime") or "",
        })
    return messages

def _mailmgr_parse_import_text(text: str):
    accounts = []
    skipped = []
    for line_no, raw_line in enumerate(str(text or "").replace("\ufeff", "").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            if line.startswith("{"):
                item = json.loads(line)
                email = str(item.get("email") or "").strip()
                password = str(item.get("password") or "")
                client_id = str(item.get("clientId") or item.get("client_id") or "").strip()
                refresh_token = str(item.get("refreshToken") or item.get("refresh_token") or "").strip()
            else:
                parts = [p.strip() for p in line.split("----")]
                if len(parts) < 4:
                    skipped.append({"line": line_no, "reason": "less_than_4_parts"})
                    continue
                email_idx = next((i for i, part in enumerate(parts) if "@" in part), -1)
                if email_idx < 0:
                    skipped.append({"line": line_no, "reason": "email_not_found"})
                    continue
                if email_idx == 0:
                    email = parts[0]
                    password = parts[1]
                    client_id = parts[2]
                    refresh_token = "----".join(parts[3:]).strip()
                elif email_idx == 2:
                    email = parts[2]
                    password = "----".join(parts[3:]).strip()
                    client_id = parts[0]
                    refresh_token = parts[1]
                elif email_idx == 3:
                    email = parts[3]
                    password = parts[2]
                    client_id = parts[0]
                    refresh_token = parts[1]
                elif email_idx == 1:
                    email = parts[1]
                    password = parts[0]
                    client_id = parts[2]
                    refresh_token = "----".join(parts[3:]).strip()
                else:
                    email = parts[email_idx]
                    password = parts[email_idx + 1] if email_idx + 1 < len(parts) else ""
                    client_id = parts[0]
                    refresh_token = parts[1]
            if not email or "@" not in email or not client_id or not refresh_token:
                skipped.append({"line": line_no, "reason": "missing_required_field"})
                continue
            accounts.append({
                "email": email,
                "password": password,
                "clientId": client_id,
                "refreshToken": refresh_token,
            })
        except Exception as exc:
            skipped.append({"line": line_no, "reason": str(exc)})
    return accounts, skipped



@app.get("/mail-manager")
def mail_manager_redirect():
    return "", 302, {"Location": "/mail-manager/"}


@app.get("/mail-manager/")
def mail_manager_page():
    index_path = MAIL_MANAGER_STATIC_DIR / "index.html"
    if not index_path.exists():
        return _mailmgr_error("mail manager static index not found", 404)
    return send_from_directory(str(MAIL_MANAGER_STATIC_DIR), "index.html")


@app.get("/mail-manager/static/<path:filename>")
def mail_manager_static(filename):
    return send_from_directory(str(MAIL_MANAGER_STATIC_DIR), filename)


@app.get("/mail-manager/api/check-auth")
def mail_manager_check_auth():
    return jsonify({"authenticated": True, "csrfToken": "local-mail-manager", "noLogin": True})


@app.post("/mail-manager/api/login")
def mail_manager_login():
    return jsonify({"ok": True, "authenticated": True, "csrfToken": "local-mail-manager"})


@app.post("/mail-manager/api/logout")
def mail_manager_logout():
    return jsonify({"ok": True, "authenticated": True})


@app.get("/mail-manager/api/accounts")
def mail_manager_accounts():
    with _mailmgr_conn() as conn:
        rows = conn.execute("SELECT * FROM mail_accounts ORDER BY id DESC").fetchall()
    return jsonify({"accounts": [_mailmgr_account_dict(row) for row in rows]})

@app.post("/mail-manager/api/accounts/import")
def mail_manager_import_accounts():
    data = request.get_json(silent=True) or {}
    accounts = data.get("accounts") or []
    if not isinstance(accounts, list) or not accounts:
        return _mailmgr_error("没有可导入账号")
    imported = 0
    with _mailmgr_conn() as conn:
        for item in accounts:
            if not isinstance(item, dict):
                continue
            email = str(item.get("email") or "").strip()
            client_id = str(item.get("clientId") or item.get("client_id") or "").strip()
            refresh_token = str(item.get("refreshToken") or item.get("refresh_token") or "").strip()
            password = str(item.get("password") or "")
            if not email or not client_id or not refresh_token:
                continue
            conn.execute(
                """
                INSERT INTO mail_accounts (email, email_normalized, password, client_id, refresh_token, token_valid, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(email_normalized) DO UPDATE SET
                    email = excluded.email,
                    password = excluded.password,
                    client_id = excluded.client_id,
                    refresh_token = excluded.refresh_token,
                    token_valid = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (email, email.lower(), password, client_id, refresh_token),
            )
            imported += 1
    if imported <= 0:
        return _mailmgr_error("没有写入账号，请检查格式")
    return jsonify({"ok": True, "imported": imported})


@app.post("/mail-manager/api/accounts/import-text")
def mail_manager_import_accounts_text():
    data = request.get_json(silent=True) or {}
    text = data.get("text") or ""
    accounts, skipped = _mailmgr_parse_import_text(text)
    if not accounts:
        return _mailmgr_error("没有可导入账号，请检查格式")
    with app.test_request_context(json={"accounts": accounts}):
        resp = mail_manager_import_accounts()
    # mail_manager_import_accounts normally returns a Response; normalize payload here.
    imported = 0
    try:
        payload = resp.get_json() if hasattr(resp, "get_json") else None
        imported = int((payload or {}).get("imported") or 0)
    except Exception:
        imported = len(accounts)
    return jsonify({"ok": True, "imported": imported or len(accounts), "parsed": len(accounts), "skipped": len(skipped), "skippedItems": skipped[:5]})


@app.post("/api/accounts/import-text")
def mail_manager_import_accounts_text_alias():
    return mail_manager_import_accounts_text()


@app.post("/mail-manager/api/accounts/<int:account_id>/reveal")
def mail_manager_reveal_account(account_id):
    row = _mailmgr_get_account(account_id)
    if row is None:
        return _mailmgr_error("?????", 404)
    return jsonify(_mailmgr_credentials_dict(row))


@app.post("/mail-manager/api/accounts/export")
def mail_manager_export_accounts():
    with _mailmgr_conn() as conn:
        rows = conn.execute("SELECT * FROM mail_accounts ORDER BY id").fetchall()
    body = "".join(json.dumps(_mailmgr_credentials_dict(row), ensure_ascii=False) + "\n" for row in rows)
    return Response(
        body,
        mimetype="application/x-ndjson; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=accounts.jsonl"},
    )


@app.patch("/mail-manager/api/accounts/<int:account_id>")
def mail_manager_update_account(account_id):
    data = request.get_json(silent=True) or {}
    row = _mailmgr_get_account(account_id)
    if row is None:
        return _mailmgr_error("?????", 404)
    updates = []
    values = []
    if "bound" in data:
        bound = 1 if bool(data.get("bound")) else 0
        updates.append("bound = ?")
        values.append(bound)
        updates.append("bound_at = CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP ELSE NULL END")
        values.append(bound)
    if "token_valid" in data or "tokenValid" in data:
        token_valid = data.get("tokenValid", data.get("token_valid"))
        updates.append("token_valid = ?")
        values.append(1 if bool(token_valid) else 0)
    if updates:
        values.append(int(account_id))
        with _mailmgr_conn() as conn:
            conn.execute(f"UPDATE mail_accounts SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", values)
    row = _mailmgr_get_account(account_id)
    return jsonify(_mailmgr_account_dict(row))


@app.delete("/mail-manager/api/accounts/<int:account_id>")
def mail_manager_delete_account(account_id):
    with _mailmgr_conn() as conn:
        conn.execute("DELETE FROM mail_accounts WHERE id = ?", (int(account_id),))
    return jsonify({"ok": True})


@app.post("/mail-manager/api/accounts/delete-batch")
def mail_manager_delete_batch():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    ids = [int(x) for x in ids if str(x).isdigit()]
    if not ids:
        return _mailmgr_error("?????????")
    q = ",".join("?" for _ in ids)
    with _mailmgr_conn() as conn:
        conn.execute(f"DELETE FROM mail_accounts WHERE id IN ({q})", ids)
    return jsonify({"ok": True, "deleted": len(ids)})


@app.post("/mail-manager/api/accounts/random-unbound")
def mail_manager_random_unbound():
    with _mailmgr_conn() as conn:
        row = conn.execute("SELECT * FROM mail_accounts WHERE bound = 0 ORDER BY RANDOM() LIMIT 1").fetchone()
    if row is None:
        return _mailmgr_error("???????", 404)
    return jsonify(_mailmgr_account_dict(row))


@app.post("/mail-manager/api/accounts/check-bound")
def mail_manager_check_bound():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    ids = [int(x) for x in ids if str(x).isdigit()]
    return jsonify({"ok": True, "checked": len(ids), "results": []})


@app.post("/mail-manager/api/accounts/<int:account_id>/mail")
def mail_manager_read_mail(account_id):
    row = _mailmgr_get_account(account_id)
    if row is None:
        return _mailmgr_error("?????", 404)
    data = request.get_json(silent=True) or {}
    folder = data.get("folder") or "inbox"
    limit = data.get("limit") or 100
    try:
        return jsonify(_mailmgr_graph_messages(row, folder=folder, limit=limit))
    except Exception as exc:
        return _mailmgr_error(_mailmgr_short_error(exc), 502, "MAIL_READ_FAILED")


@app.post("/mail-manager/api/accounts/<int:account_id>/verify-code")
def mail_manager_verify_code(account_id):
    row = _mailmgr_get_account(account_id)
    if row is None:
        return _mailmgr_error("?????", 404)
    data = request.get_json(silent=True) or {}
    keyword = str(data.get("keyword") or "").strip().lower()
    max_results = int(data.get("maxResults") or 100)
    try:
        from mail_outlook import _extract_otp_from_html
        messages = []
        for folder in ("inbox", "junkemail"):
            messages.extend(_mailmgr_graph_messages(row, folder=folder, limit=max_results))
        for msg in messages:
            hay = "\n".join([
                str(msg.get("subject") or ""),
                str(msg.get("sender") or msg.get("from") or ""),
                str(msg.get("bodyPreview") or ""),
                str((msg.get("body") or {}).get("content") or ""),
            ])
            if keyword and keyword not in hay.lower():
                continue
            code = _extract_otp_from_html(hay)
            if code:
                return jsonify({"code": code, "message": msg})
    except Exception as exc:
        return _mailmgr_error(str(exc), 502, "VERIFY_CODE_FAILED")
    return _mailmgr_error("???????", 404, "CODE_NOT_FOUND")

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "time": datetime.utcnow().isoformat() + "Z",
        "paypal_global_flow_build": PAYPAL_GLOBAL_FLOW_BUILD,
    })


@app.route("/api/stats")
def stats():
    return jsonify({"ok": True, "success_count": _success_count()})


@app.route("/api/session-inspect", methods=["POST"])
def session_inspect():
    data = request.get_json(silent=True) or {}
    session_text = (data.get("session_json") or data.get("session_text") or "").strip()
    session_pool_text = (data.get("session_pool") or data.get("session_pool_text") or "").strip()
    items = _inspect_sessions(session_text, session_pool_text)
    return jsonify({
        "ok": True,
        "count": len(items),
        "items": items,
    })


@app.route("/api/payment-modes")
def payment_modes():
    modes = {}
    for name, cfg in PAYMENT_MODES.items():
        modes[name] = {
            "country": cfg.get("country", ""),
            "currency": cfg.get("currency", ""),
            "paypal": ("PayPal" in name) or ("PAYPAL" in name) or bool(cfg.get("paypal_global_rotation")) or bool(cfg.get("paypal_global_no_discount")),
            "paypal_global_no_discount": bool(cfg.get("paypal_global_no_discount")),
            "short_link": bool(cfg.get("chatgpt_short_link") or cfg.get("ph_cross_region_promo")),
            "ph_cross_region_promo": bool(cfg.get("ph_cross_region_promo")),
            "gcash": bool(cfg.get("ph_gcash_redirect")),
            "ba_pm_711": bool(cfg.get("ba_pm_711")),
            "ideal_v3": bool(cfg.get("ideal_v3")),
            "upi_v2": bool(cfg.get("upi_v2")),
            "pix_v2": bool(cfg.get("pix_v2")),
            "kakao_v2": bool(cfg.get("kakao_v2")),
            "twint_v2": bool(cfg.get("twint_v2")),
            "promptpay_v2": bool(cfg.get("promptpay_v2")),
            "momo": str(cfg.get("local_payment") or "").lower() == "momo",
            "pix": bool(cfg.get("pix_flow")) or str(cfg.get("local_payment") or "").lower() == "pix",
        }
    return jsonify({"ok": True, "modes": modes})


@app.route("/api/generate-link", methods=["POST"])
def generate_link():
    data = request.get_json(silent=True) or {}
    access_token = (data.get("access_token") or "").strip()
    mode_name = (data.get("payment_mode") or DEFAULT_MODE).strip()
    if not access_token:
        return jsonify({"ok": False, "error": "缺少 access_token"}), 400

    response = _generate_with_retries(access_token, mode_name, data)
    return jsonify(response), (200 if response.get("ok") or response.get("cancelled") else 500)


@app.route("/api/paste-session", methods=["POST"])
def paste_session():
    data = request.get_json(silent=True) or {}
    session_text = (data.get("session_json") or data.get("session_text") or "").strip()
    session_pool_text = (data.get("session_pool") or data.get("session_pool_text") or "").strip()
    mode_name = (data.get("payment_mode") or DEFAULT_MODE).strip()
    if not session_text and not session_pool_text:
        return jsonify({"ok": False, "error": "缺少 session_json / session_text"}), 400

    access_tokens = _parse_access_token_pool(session_text, session_pool_text)
    if not access_tokens:
        return jsonify({"ok": False, "error": "未能从粘贴内容解析 accessToken"}), 400
    valid, error = _validate_strategy_inputs(data, mode_name, access_tokens)
    if not valid:
        return jsonify({"ok": False, "error": error}), 400
    access_token = access_tokens[0]

    response = _generate_with_retries(access_token, mode_name, data)
    if response.get("ok"):
        response["access_token"] = access_token
    return jsonify(response), (200 if response.get("ok") or response.get("cancelled") else 500)


@app.route("/api/start-retry", methods=["POST"])
def start_retry():
    data = request.get_json(silent=True) or {}
    session_text = (data.get("session_json") or data.get("session_text") or "").strip()
    session_pool_text = (data.get("session_pool") or data.get("session_pool_text") or "").strip()
    mode_name = (data.get("payment_mode") or DEFAULT_MODE).strip()
    access_tokens = _parse_access_token_pool(session_text, session_pool_text)
    if not access_tokens:
        return jsonify({"ok": False, "error": "未能从 Session 输入或 Session 池解析 accessToken"}), 400
    valid, error = _validate_strategy_inputs(data, mode_name, access_tokens)
    if not valid:
        return jsonify({"ok": False, "error": error}), 400

    job_id = uuid.uuid4().hex
    job_data = dict(data)
    job_data["cancel_token"] = job_id
    job_data["max_attempts"] = _requested_attempts(job_data)
    job_data["concurrency"] = _requested_concurrency(job_data)
    requested_concurrency = job_data["concurrency"]
    strategies = _paypal_strategies(job_data, mode_name)
    if len(strategies) >= 2:
        job_data["concurrency"] = max(2, job_data["concurrency"])
    if _truthy_default(job_data.get("session_safe_mode"), True):
        job_data["concurrency"] = max(1, min(job_data["concurrency"], len(access_tokens)))
    concurrency_adjusted = job_data["concurrency"] != requested_concurrency
    total_attempt_budget = job_data["max_attempts"] * job_data["concurrency"]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "ok": True,
            "job_id": job_id,
            "status": "running",
            "done": False,
            "current_attempt": 0,
            "max_attempts": total_attempt_budget,
            "per_worker_attempts": job_data["max_attempts"],
            "total_attempt_budget": total_attempt_budget,
            "concurrency": job_data["concurrency"],
            "requested_concurrency": requested_concurrency,
            "concurrency_adjusted": concurrency_adjusted,
            "session_count": len(access_tokens),
            "current_proxy": "",
            "status_text": "准备开始提链",
            "attempts_log": [],
            "result": None,
            "success_count": _success_count(),
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
    thread = threading.Thread(
        target=_run_concurrent_retry_job if job_data["concurrency"] > 1 else _run_retry_job,
        args=(job_id, access_tokens if job_data["concurrency"] > 1 else access_tokens[0], mode_name, job_data),
        daemon=True,
    )
    thread.start()
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "success_count": _success_count(),
        "session_count": len(access_tokens),
        "concurrency": job_data["concurrency"],
        "requested_concurrency": requested_concurrency,
        "concurrency_adjusted": concurrency_adjusted,
        "per_worker_attempts": job_data["max_attempts"],
        "total_attempt_budget": total_attempt_budget,
    })


@app.route("/api/retry-status/<job_id>")
def retry_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        payload = dict(job)
        payload["attempts_log"] = list(job.get("attempts_log", []))
    payload["success_count"] = _success_count()
    return jsonify(payload)


@app.route("/api/cancel-retry", methods=["POST"])
def cancel_retry():
    data = request.get_json(silent=True) or {}
    cancel_token = str(data.get("cancel_token") or data.get("job_id") or "").strip()
    if not cancel_token:
        return jsonify({"ok": False, "error": "缺少 cancel_token"}), 400
    with CANCEL_LOCK:
        event = CANCEL_EVENTS.get(cancel_token)
        if event:
            event.set()
    return jsonify({
        "ok": True,
        "cancelled": bool(event),
        "message": "停止请求已收到，当前尝试结束后会停止" if event else "没有找到正在运行的重试任务",
    })


@app.route("/api/proxy-test", methods=["POST"])
def proxy_test():
    data = request.get_json(silent=True) or {}
    url = normalize_proxy_url(data.get("proxy_url", ""))
    if not url:
        return jsonify({"ok": False, "error": "缺少 proxy_url"}), 400
    try:
        response = requests.get(
            "https://ipinfo.io/json",
            proxies={"http": url, "https": url},
            timeout=15,
        )
        if response.status_code >= 400:
            return jsonify({"ok": False, "error": f"HTTP {response.status_code}"})
        payload = response.json() or {}
        return jsonify({
            "ok": True,
            "ip": payload.get("ip", ""),
            "country": payload.get("country", ""),
            "city": payload.get("city", ""),
            "org": payload.get("org", ""),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/proxy-pool-test", methods=["POST"])
def proxy_pool_test():
    data = request.get_json(silent=True) or {}
    pool = _pool_from_text(data.get("payment_proxy_pool") or data.get("proxy_pool") or "") or [""]
    if False and not pool:
        return jsonify({"ok": False, "error": "代理池为空"}), 400
    use_gost_bridge = _truthy(data.get("gost_bridge"))
    workers = min(8, len(pool))
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(_test_proxy_candidate, index, proxy, use_gost_bridge)
            for index, proxy in enumerate(pool, start=1)
        ]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item.get("index", 0))
    ok_count = sum(1 for item in results if item.get("ok"))
    return jsonify({
        "ok": True,
        "total": len(results),
        "ok_count": ok_count,
        "bad_count": len(results) - ok_count,
        "results": results,
    })


# ---------------------------------------------------------------------------
# 综合工具服务 / credential pool API
# ---------------------------------------------------------------------------

@app.route("/api/gptreg/health")
def gptreg_health():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    return jsonify({"ok": True, "stats": gptreg_db.stats()})



# Mail manager compatibility aliases for embedded/cached xiaopingguo modules
@app.get("/api/accounts")
def mail_manager_accounts_alias():
    return mail_manager_accounts()


@app.post("/api/accounts/import")
def mail_manager_import_accounts_alias():
    return mail_manager_import_accounts()


@app.post("/api/accounts/export")
def mail_manager_export_accounts_alias():
    return mail_manager_export_accounts()


@app.post("/api/accounts/random-unbound")
def mail_manager_random_unbound_alias():
    return mail_manager_random_unbound()


@app.post("/api/accounts/check-bound")
def mail_manager_check_bound_alias():
    return mail_manager_check_bound()


@app.post("/api/accounts/<int:account_id>/reveal")
def mail_manager_reveal_account_alias(account_id):
    return mail_manager_reveal_account(account_id)


@app.patch("/api/accounts/<int:account_id>")
def mail_manager_update_account_alias(account_id):
    return mail_manager_update_account(account_id)


@app.delete("/api/accounts/<int:account_id>")
def mail_manager_delete_account_alias(account_id):
    return mail_manager_delete_account(account_id)


@app.post("/api/accounts/<int:account_id>/mail")
def mail_manager_mail_alias(account_id):
    return mail_manager_read_mail(account_id)


@app.post("/api/accounts/<int:account_id>/verify-code")
def mail_manager_verify_code_alias(account_id):
    return mail_manager_verify_code(account_id)

@app.route("/api/gptreg/import", methods=["POST"])
def gptreg_import():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    result = gptreg_db.import_accounts(data.get("text") or "")
    return jsonify({"ok": True, **result, "stats": gptreg_db.stats()})


@app.route("/api/gptreg/accounts")
def gptreg_accounts():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    status = (request.args.get("status") or "").strip()
    limit = int(request.args.get("limit") or 500)
    items = []
    for item in gptreg_db.list_accounts(status=status, limit=limit):
        row = dict(item)
        # 前端列表不需要暴露 Outlook 密码 / Microsoft refresh_token。
        row["password_len"] = len(row.get("password") or "")
        row["client_id_len"] = len(row.get("client_id") or "")
        row["refresh_token_len"] = len(row.get("refresh_token") or "")
        row.pop("password", None)
        row.pop("client_id", None)
        row.pop("refresh_token", None)
        items.append(row)
    return jsonify({"ok": True, "items": items})


@app.route("/api/gptreg/accounts/<path:email>", methods=["DELETE"])
def gptreg_delete_account(email: str):
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    ok = gptreg_db.delete_account(email)
    if not ok:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "stats": gptreg_db.stats()})


@app.route("/api/gptreg/accounts/<path:email>/reset", methods=["POST"])
def gptreg_reset_account(email: str):
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    ok = gptreg_db.reset_to_available(email)
    if not ok:
        return jsonify({"ok": False, "error": f"邮箱 {email} 不存在"}), 404
    return jsonify({"ok": True, "email": email, "stats": gptreg_db.stats()})


@app.route("/api/gptreg/accounts/reset_failed", methods=["POST"])
def gptreg_reset_failed():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    n = gptreg_db.reset_failed_to_available()
    return jsonify({"ok": True, "reset": n, "stats": gptreg_db.stats()})


@app.route("/api/gptreg/accounts/release_stale", methods=["POST"])
def gptreg_release_stale():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    stale_seconds = int(data.get("stale_seconds") or 1800)
    n = gptreg_db.release_stale_in_use(stale_seconds=stale_seconds)
    return jsonify({"ok": True, "released": n, "stats": gptreg_db.stats()})


@app.route("/api/gptreg/accounts/bulk_delete", methods=["POST"])
def gptreg_bulk_delete_accounts():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    status = (data.get("status") or "").strip()
    emails = data.get("emails") or []
    if status:
        n = gptreg_db.delete_accounts_by_status(status)
        return jsonify({"ok": True, "deleted": n, "by": "status", "stats": gptreg_db.stats()})
    if emails:
        n = gptreg_db.delete_accounts_by_emails(emails)
        return jsonify({"ok": True, "deleted": n, "by": "emails", "stats": gptreg_db.stats()})
    return jsonify({"ok": False, "error": "需要 status 或 emails"}), 400


@app.route("/api/gptreg/register", methods=["POST"])
def gptreg_register():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    email = (data.get("email") or "").strip()
    mail_source = gptreg_db.get_setting("mail_source", "outlook")

    if mail_source == "cf_temp":
        account = {
            "email": f"cf_placeholder_{int(time.time())}@cf.local",
            "password": "",
            "client_id": "",
            "refresh_token": "",
        }
    elif email:
        account = gptreg_db.claim_account(email)
        if not account:
            return jsonify({
                "ok": False,
                "error": f"邮箱 {email} 不可用（不存在 / 已 in_use / 已完成）",
            }), 400
    else:
        account = gptreg_db.claim_next()
        if not account:
            return jsonify({"ok": False, "error": "号池里没有 available 账号，请先导入"}), 400

    options = {
        "want_access_token": bool(data.get("want_access_token", True)),
        "want_session_token": bool(data.get("want_session_token", True)),
        "want_refresh_token": bool(data.get("want_refresh_token", False)),
        "proxy": (data.get("proxy") or "").strip(),
        "otp_timeout": int(data.get("otp_timeout") or 300),
        "allow_existing_login": bool(data.get("allow_existing_login", True)),
    }
    run_id = gptreg_registrar.start_registration(account, options)
    return jsonify({"ok": True, "run_id": run_id, "email": account["email"]})


@app.route("/api/gptreg/runs")
def gptreg_runs():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    limit = int(request.args.get("limit") or 50)
    return jsonify({"ok": True, "items": gptreg_db.list_runs(limit=limit)})


@app.route("/api/gptreg/runs/<run_id>/stream")
def gptreg_run_stream(run_id: str):
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    q = gptreg_registrar.get_run_queue(run_id)
    if q is None:
        return jsonify({"ok": False, "error": "run 不存在或日志流已结束"}), 404

    def gen():
        try:
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    yield "event: end\ndata: {}\n\n"
                    break
                if isinstance(item, str) and item.startswith("__EVENT__:"):
                    yield "event: status\ndata: " + item[len("__EVENT__:"):] + "\n\n"
                else:
                    yield "data: " + json.dumps({"line": str(item)}, ensure_ascii=False) + "\n\n"
        finally:
            try:
                gptreg_registrar.remove_run_queue(run_id)
            except Exception:
                pass

    return Response(stream_with_context(gen()), mimetype="text/event-stream")


@app.route("/api/gptreg/registered")
def gptreg_registered():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    limit = int(request.args.get("limit") or 500)
    return jsonify({"ok": True, "items": gptreg_db.list_registered(limit=limit)})


@app.route("/api/gptreg/registered/export")
def gptreg_registered_export():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    limit = int(request.args.get("limit") or 5000)
    items = gptreg_db.list_registered_full(limit=limit)
    buf = io.BytesIO()
    safe_re = re.compile(r"[^A-Za-z0-9._@-]")
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            email = (item.get("email") or "unknown").strip()
            base = safe_re.sub("_", email) or "unknown"
            name = f"{base}.json"
            i = 2
            while name in used_names:
                name = f"{base}_{i}.json"
                i += 1
            used_names.add(name)
            zf.writestr(name, json.dumps(item, ensure_ascii=False, indent=2))
    buf.seek(0)
    ts = time.strftime("%Y%m%d-%H%M%S")
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="gpt-accounts-{ts}.zip"',
            "X-Account-Count": str(len(items)),
        },
    )



@app.route("/api/gptreg/settings/sms")
def gptreg_get_sms_settings():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    return jsonify({"ok": True, "config": gptreg_db.get_sms_config()})


@app.route("/api/gptreg/settings/sms", methods=["POST"])
def gptreg_save_sms_settings():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    gptreg_db.save_sms_config(data)
    return jsonify({"ok": True, "config": gptreg_db.get_sms_config()})


@app.route("/api/gptreg/settings/sms/test", methods=["POST"])
def gptreg_test_sms_settings():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    cfg = gptreg_db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        return jsonify({"ok": False, "error": "sms_api_key is empty"}), 400
    try:
        from sms_provider import create_sms_provider
        provider = create_sms_provider(cfg.get("sms_provider"), cfg)
        balance = provider.get_balance()
        return jsonify({
            "ok": True,
            "provider": cfg.get("sms_provider"),
            "balance": balance,
            "message": f"connection ok, balance: {balance}",
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/gptreg/settings/sms/countries")
def gptreg_sms_countries():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    cfg = gptreg_db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        return jsonify({"ok": False, "error": "sms_api_key is empty"}), 400
    try:
        from sms_provider import create_sms_provider, OPENAI_SMS_COUNTRIES, SMS_COUNTRY_NAMES_CN
        provider = create_sms_provider(cfg.get("sms_provider"), cfg)
        rows = provider.get_top_countries(service=cfg.get("sms_service") or "dr")
        for r in rows:
            cid = str(r.get("country"))
            r["openai_sms_safe"] = cid in OPENAI_SMS_COUNTRIES
            r["name_cn"] = SMS_COUNTRY_NAMES_CN.get(cid, "unknown")
        return jsonify({"ok": True, "countries": rows[:30], "openai_sms_safe": list(OPENAI_SMS_COUNTRIES)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/gptreg/settings/sms/all_countries")
def gptreg_sms_all_countries():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    try:
        from sms_provider import SMS_COUNTRY_NAMES_CN, OPENAI_SMS_COUNTRIES
        items = sorted(SMS_COUNTRY_NAMES_CN.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 9999)
        countries = [{
            "id": cid,
            "name_cn": name,
            "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES,
        } for cid, name in items]
        return jsonify({"ok": True, "countries": countries, "openai_sms_safe": list(OPENAI_SMS_COUNTRIES)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/gptreg/settings/export")
def gptreg_get_export_settings():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    return jsonify({"ok": True, "config": gptreg_db.get_export_config()})


@app.route("/api/gptreg/settings/export", methods=["POST"])
def gptreg_save_export_settings():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    gptreg_db.save_export_config(data)
    return jsonify({"ok": True, "config": gptreg_db.get_export_config()})


@app.route("/api/gptreg/settings/export/test", methods=["POST"])
def gptreg_test_export_settings():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    target = (data.get("target") or "").strip().lower()
    cfg = gptreg_db.get_export_internal_config()
    try:
        from webui import exporter as gptreg_exporter
        if target == "cpa":
            return jsonify(gptreg_exporter.test_cpa(cfg["cpa"]))
        if target == "sub2api":
            return jsonify(gptreg_exporter.test_sub2api(cfg["sub2api"]))
        return jsonify({"ok": False, "error": f"unknown target: {target}"}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/gptreg/registered/export_to_panel", methods=["POST"])
def gptreg_export_registered_to_panel():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "missing email"}), 400
    cred = gptreg_db.get_registered(email)
    if not cred:
        return jsonify({"ok": False, "error": f"not found: {email}"}), 404
    cfg = gptreg_db.get_export_internal_config()
    targets = {str(t).strip().lower() for t in (data.get("targets") or ["cpa", "sub2api"]) if t}
    out = {"ok": True, "email": email, "cpa": None, "sub2api": None}
    try:
        from webui import exporter as gptreg_exporter
        if "cpa" in targets:
            cpa_cfg = dict(cfg["cpa"])
            cpa_cfg["enabled"] = True
            try:
                out["cpa"] = gptreg_exporter.export_to_cpa(cred, cpa_cfg)
            except Exception as exc:
                out["cpa"] = {"ok": False, "error": str(exc)}
        if "sub2api" in targets:
            sub2api_cfg = dict(cfg["sub2api"])
            sub2api_cfg["enabled"] = True
            try:
                out["sub2api"] = gptreg_exporter.export_to_sub2api(cred, sub2api_cfg)
            except Exception as exc:
                out["sub2api"] = {"ok": False, "error": str(exc)}
        return jsonify(out)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

def _gptreg_build_full_cookie_header(row: dict) -> str:
    """Merge the saved cookie jar with all useful NextAuth session aliases.

    Existing browser cookies are preserved.  The stored complete session token is
    added under the two names recognized by gpt-upi-main.  Long tokens also get
    NextAuth-style numbered chunks so the copied value contains the browser forms
    ``__Secure-next-auth.session-token.0/.1/...`` as well.
    """
    raw_cookie = str((row or {}).get("cookie_header") or "").strip()
    if raw_cookie.lower().startswith("cookie:"):
        raw_cookie = raw_cookie.split(":", 1)[1].strip()

    pairs: list[list[str]] = []
    indexes: dict[str, int] = {}

    def put(name: str, value: str) -> None:
        name = str(name or "").strip()
        value = str(value or "").strip()
        if not name or not value:
            return
        if name in indexes:
            pairs[indexes[name]][1] = value
            return
        indexes[name] = len(pairs)
        pairs.append([name, value])

    for part in raw_cookie.split(";"):
        item = part.strip()
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        put(name, value)

    session_token = str((row or {}).get("session_token") or "").strip()
    if not session_token:
        for name in (
            "__Secure-next-auth.session-token",
            "next-auth.session-token",
            "__Secure-authjs.session-token",
            "authjs.session-token",
        ):
            if name in indexes:
                session_token = pairs[indexes[name]][1]
                break
    if not session_token:
        chunks: list[tuple[int, str]] = []
        prefix = "__Secure-next-auth.session-token."
        for name, value in pairs:
            if name.startswith(prefix) and name[len(prefix):].isdigit():
                chunks.append((int(name[len(prefix):]), value))
        if chunks:
            session_token = "".join(value for _, value in sorted(chunks))

    if session_token:
        put("__Secure-next-auth.session-token", session_token)
        put("next-auth.session-token", session_token)
        # ChatGPT 新版 Auth.js 也会识别这两个名字；检测存活时一起带上。
        put("__Secure-authjs.session-token", session_token)
        put("authjs.session-token", session_token)

        # Stay below the usual per-cookie 4 KiB boundary after name/attributes.
        chunk_size = 3800
        if len(session_token) > chunk_size:
            for index, start in enumerate(range(0, len(session_token), chunk_size)):
                chunk = session_token[start:start + chunk_size]
                put(f"__Secure-next-auth.session-token.{index}", chunk)
                put(f"__Secure-authjs.session-token.{index}", chunk)

    return "; ".join(f"{name}={value}" for name, value in pairs)


@app.route("/api/gptreg/registered/<path:email>")
def gptreg_registered_one(email: str):
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    row = gptreg_db.get_registered(email)
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    row = dict(row)
    row["full_cookie_header"] = _gptreg_build_full_cookie_header(row)
    return jsonify({"ok": True, "data": row})


def _gptreg_refresh_one_plan(email: str, proxy: str = "") -> dict:
    row = gptreg_db.get_registered(email)
    if not row:
        return {"ok": False, "email": email, "plan": "unknown", "error": "not found"}
    info = detect_chatgpt_plan(
        access_token=row.get("access_token", ""),
        cookie_header=row.get("cookie_header", ""),
        proxy=(proxy or "").strip(),
        timeout=20,
    )
    updates = {
        "account_plan": info.get("plan", "unknown") or "unknown",
        "account_plan_source": info.get("source", ""),
        "account_plan_checked_at": info.get("checked_at", time.time()),
        "account_plan_error": "" if info.get("ok") else info.get("error", ""),
        "account_plan_evidence": info.get("evidence", []),
    }
    gptreg_db.update_registered_extra(email, updates)
    return {"email": email, **info, **updates}


_GPTREG_DEFINITE_LIVE_STATUSES = {"alive", "login_expired", "banned", "dead"}


def _gptreg_previous_live_snapshot(row: dict) -> tuple[str, dict]:
    """Return the last conclusive live status and its stored metadata."""
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    status = str(
        extra.get("account_live_status") or row.get("account_live_status") or "unknown"
    ).strip().lower()
    if status not in _GPTREG_DEFINITE_LIVE_STATUSES:
        status = "unknown"
    return status, extra


def _gptreg_check_one_live(email: str, proxy: str = "") -> dict:
    row = gptreg_db.get_registered(email)
    if not row:
        return {
            "ok": False,
            "email": email,
            "alive": False,
            "live_status": "unknown",
            "error": "not found",
        }
    full_cookie = _gptreg_build_full_cookie_header(row)
    info = detect_chatgpt_live(
        access_token=row.get("access_token", ""),
        cookie_header=full_cookie or row.get("cookie_header", ""),
        proxy=(proxy or "").strip(),
        timeout=15,
    )
    probe_status = str(info.get("live_status", "unknown") or "unknown").strip().lower()
    if probe_status not in _GPTREG_DEFINITE_LIVE_STATUSES | {"unknown"}:
        probe_status = "unknown"
    reason = info.get("reason") or info.get("error") or ""
    checked_at = float(info.get("checked_at") or time.time())
    created_at = float(row.get("created_at") or 0)
    elapsed_hours = round(max(0.0, checked_at - created_at) / 3600.0, 3) if created_at else 0
    previous_status, extra = _gptreg_previous_live_snapshot(row)
    used_previous_status = (
        probe_status == "unknown" and previous_status in _GPTREG_DEFINITE_LIVE_STATUSES
    )
    live_status = previous_status if used_previous_status else probe_status
    previous_confirmed_at = extra.get("account_live_last_confirmed_at") or 0
    try:
        previous_live_hours = float(extra.get("account_live_hours") or row.get("account_live_hours") or 0) if previous_confirmed_at else 0
    except Exception:
        previous_live_hours = 0
    try:
        previous_failed_after_hours = float(extra.get("account_live_failed_after_hours") or 0)
    except Exception:
        previous_failed_after_hours = 0
    if probe_status == "alive":
        # 只有本次明确检测为“存活”，才把注册→检测的时间记作真实存活时长。
        live_hours = elapsed_hours
        failed_after_hours = 0
    else:
        # 失效/封禁/未知时，不能倒推出真实死亡时间，不能继续累计为“存活”。
        # 若之前有过明确存活检测，保留“最后确认存活时长”；否则为 0。
        live_hours = previous_live_hours if previous_live_hours > 0 else 0
        if probe_status in ("login_expired", "banned", "dead"):
            failed_after_hours = elapsed_hours
        elif used_previous_status:
            failed_after_hours = previous_failed_after_hours
        else:
            failed_after_hours = 0
    # Every attempt is recorded separately.  A transport/proxy/rate-limit failure is
    # inconclusive and must not erase the last conclusive account status.
    updates = {
        "account_live_probe_status": probe_status,
        "account_live_probe_source": info.get("source", ""),
        "account_live_probe_checked_at": checked_at,
        "account_live_probe_error": reason if probe_status == "unknown" else "",
        "account_live_probe_status_code": info.get("status_code", 0),
        "account_live_hours": live_hours,
        "account_live_failed_after_hours": failed_after_hours,
    }
    if used_previous_status:
        updates["account_live_error"] = f"本次检测未完成，已保留上次结论：{reason}".rstrip("：")
    else:
        updates.update({
            "account_live_status": live_status,
            "account_live_source": info.get("source", ""),
            "account_live_checked_at": checked_at,
            "account_live_error": "" if live_status == "alive" else reason,
            "account_live_status_code": info.get("status_code", 0),
        })
    if probe_status == "alive":
        updates["account_live_last_confirmed_at"] = checked_at
    elif previous_confirmed_at:
        updates["account_live_last_confirmed_at"] = previous_confirmed_at
    else:
        updates["account_live_last_confirmed_at"] = 0
    gptreg_db.update_registered_extra(email, updates)
    return {
        "email": email,
        "detect_ok": bool(info.get("ok")),
        **info,
        **updates,
        "probe_live_status": probe_status,
        "account_live_status": live_status,
        "used_previous_status": used_previous_status,
        "ok": True,
        # `alive` describes this probe; account_live_status may be an older
        # conclusive snapshot retained when the current probe is inconclusive.
        "alive": probe_status == "alive",
    }


def _gptreg_check_one_momo(email: str, proxy: str = "") -> dict:
    row = gptreg_db.get_registered(email)
    if not row:
        return {"ok": False, "email": email, "decision": "unknown", "error": "not found"}
    full_cookie = _gptreg_build_full_cookie_header(row)
    info = detect_chatgpt_momo_eligibility(
        access_token=row.get("access_token", ""),
        cookie_header=full_cookie or row.get("cookie_header", ""),
        proxy=(proxy or "").strip(),
        timeout=20,
        trial_days=30,
    )
    checked_at = float(info.get("checked_at") or time.time())
    conclusive = bool(info.get("conclusive"))
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    previous_conclusive = bool(extra.get("momo_eligibility_conclusive"))
    updates = {
        "momo_eligibility_probe_decision": info.get("decision", "unknown"),
        "momo_eligibility_probe_checked_at": checked_at,
        "momo_eligibility_probe_error": info.get("error", ""),
    }
    if conclusive or not previous_conclusive:
        updates.update({
            "momo_eligibility_decision": info.get("decision", "unknown"),
            "momo_eligibility_supported": info.get("supported"),
            "momo_eligibility_conclusive": conclusive,
            "momo_eligibility_text": info.get("decision_text", ""),
            "momo_eligibility_checked_at": checked_at,
            "momo_eligibility_error": info.get("error", ""),
            "momo_has_momo": info.get("has_momo"),
            "momo_actual_trial": info.get("actual_trial"),
            "momo_methods": info.get("methods"),
            "momo_one_click_trial_eligible": info.get("one_click_trial_eligible"),
            "momo_is_new_stripe_customer": info.get("is_new_stripe_customer"),
            "momo_stripe_mode": info.get("stripe_mode"),
        })
    else:
        updates["momo_eligibility_error"] = (
            f"本次检测未完成，已保留上次结论：{info.get('error') or info.get('decision_text') or '待确认'}"
        )
    gptreg_db.update_registered_extra(email, updates)
    return {
        "email": email,
        **info,
        **updates,
        "used_previous_result": bool(not conclusive and previous_conclusive),
        "ok": True,
    }


def _gptreg_check_one_oaics(email: str, proxy: str = "") -> dict:
    row = gptreg_db.get_registered(email)
    if not row:
        return {"ok": False, "email": email, "decision": "unknown", "error": "not found"}
    token = parse_session_json(row.get("access_token", "")) or str(row.get("access_token") or "").strip()
    if not token:
        checked_at = time.time()
        updates = {
            "oaics_eligibility_decision": "credential_missing",
            "oaics_eligibility_supported": False,
            "oaics_eligibility_conclusive": True,
            "oaics_eligibility_checked_at": checked_at,
            "oaics_eligibility_error": "missing access_token",
        }
        gptreg_db.update_registered_extra(email, updates)
        return {"ok": True, "email": email, **updates}
    full_cookie = _gptreg_build_full_cookie_header(row)
    info = opll_probe_paypal_global_oaics_eligibility(
        token,
        proxy_url=(proxy or "").strip(),
        chatgpt_cookie=full_cookie or row.get("cookie_header", ""),
        billing_email=email,
        browser_profile="firefox147",
        use_cache=False,
    )
    checked_at = time.time()
    eligible = bool(info.get("oaics_eligible") or info.get("eligible"))
    decision = "eligible" if eligible else ("probe_failed" if not info.get("ok") else "not_eligible")
    updates = {
        "oaics_eligibility_decision": decision,
        "oaics_eligibility_supported": eligible,
        "oaics_eligibility_conclusive": bool(info.get("ok")),
        "oaics_eligibility_checked_at": checked_at,
        "oaics_eligibility_error": "" if info.get("ok") else str(info.get("error") or ""),
        "oaics_probe_country": str(info.get("probe_country") or "BR"),
        "oaics_probe_currency": str(info.get("probe_currency") or "BRL"),
        "oaics_checkout_session_id": str(info.get("checkout_session_id") or ""),
        "oaics_session_kind": str(info.get("session_kind") or ""),
        "oaics_checkout_session_type": str(info.get("checkout_session_type") or ""),
        "oaics_checkout_branch": str(info.get("checkout_branch") or ""),
        "oaics_browser_profile": str(info.get("browser_profile") or "firefox147"),
    }
    gptreg_db.update_registered_extra(email, updates)
    return {
        "ok": True,
        "email": email,
        **info,
        **updates,
        "decision": decision,
        "supported": eligible,
    }


@app.route("/api/gptreg/registered/refresh_plan", methods=["POST"])
def gptreg_refresh_plan():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "缺少 email"}), 400
    result = _gptreg_refresh_one_plan(email, proxy=(data.get("proxy") or "").strip())
    return jsonify(result), (200 if result.get("ok") else 500)


@app.route("/api/gptreg/registered/bulk_refresh_plan", methods=["POST"])
def gptreg_bulk_refresh_plan():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    proxy = (data.get("proxy") or "").strip()
    emails = data.get("emails") or []
    if not emails:
        emails = [r.get("email") for r in gptreg_db.list_registered(limit=5000)]
    results = []
    for email in emails:
        if not email:
            continue
        results.append(_gptreg_refresh_one_plan(str(email), proxy=proxy))
    return jsonify({
        "ok": True,
        "total": len(results),
        "ok_count": sum(1 for r in results if r.get("ok")),
        "results": results,
    })


@app.route("/api/gptreg/registered/check_live", methods=["POST"])
def gptreg_check_live():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "缺少 email"}), 400
    result = _gptreg_check_one_live(email, proxy=(data.get("proxy") or "").strip())
    return jsonify(result), (200 if result.get("ok") else 404)


@app.route("/api/gptreg/registered/bulk_check_live", methods=["POST"])
def gptreg_bulk_check_live():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    proxy = (data.get("proxy") or "").strip()
    emails = data.get("emails") or []
    if not emails:
        emails = [r.get("email") for r in gptreg_db.list_registered(limit=5000)]
    emails = [str(e).strip() for e in emails if str(e or "").strip()]

    results = []
    if emails:
        max_workers = max(1, min(12, int(data.get("concurrency") or 8), len(emails)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_gptreg_check_one_live, email, proxy): email
                for email in emails
            }
            for fut in as_completed(futures):
                email = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as exc:
                    checked_at = time.time()
                    row = {}
                    extra = {}
                    previous_status = "unknown"
                    try:
                        row = gptreg_db.get_registered(email) or {}
                        previous_status, extra = _gptreg_previous_live_snapshot(row)
                        if extra.get("account_live_last_confirmed_at"):
                            live_hours = float(extra.get("account_live_hours") or row.get("account_live_hours") or 0)
                        else:
                            live_hours = 0
                    except Exception:
                        live_hours = 0
                    used_previous_status = previous_status in _GPTREG_DEFINITE_LIVE_STATUSES
                    effective_status = previous_status if used_previous_status else "unknown"
                    error = str(exc)
                    updates = {
                        "account_live_probe_status": "unknown",
                        "account_live_probe_error": error,
                        "account_live_probe_checked_at": checked_at,
                        "account_live_probe_status_code": 0,
                        "account_live_error": (
                            f"本次检测未完成，已保留上次结论：{error}"
                            if used_previous_status else error
                        ),
                    }
                    if not used_previous_status:
                        updates.update({
                            "account_live_status": "unknown",
                            "account_live_checked_at": checked_at,
                            "account_live_hours": 0,
                            "account_live_failed_after_hours": 0,
                        })
                    results.append({
                        "ok": True,
                        "email": email,
                        "alive": effective_status == "alive",
                        "live_status": "unknown",
                        "probe_live_status": "unknown",
                        "reason": error,
                        "account_live_status": effective_status,
                        "account_live_error": updates["account_live_error"],
                        "account_live_checked_at": (
                            extra.get("account_live_checked_at", 0)
                            if used_previous_status else checked_at
                        ),
                        "account_live_hours": live_hours,
                        "account_live_failed_after_hours": extra.get("account_live_failed_after_hours", 0),
                        "used_previous_status": used_previous_status,
                    })
                    gptreg_db.update_registered_extra(email, updates)

    def probe_status(item: dict) -> str:
        return str(
            item.get("probe_live_status") or item.get("live_status") or "unknown"
        ).lower()

    def effective_status(item: dict) -> str:
        return str(
            item.get("account_live_status") or item.get("live_status") or "unknown"
        ).lower()

    # Bulk totals describe this run, not a retained historical snapshot.
    alive_count = sum(1 for r in results if probe_status(r) == "alive")
    login_expired_count = sum(1 for r in results if probe_status(r) == "login_expired")
    banned_count = sum(1 for r in results if probe_status(r) in ("banned", "dead"))
    unknown_count = len(results) - alive_count - login_expired_count - banned_count
    probe_unknown_count = sum(
        1 for r in results
        if str(r.get("probe_live_status") or r.get("live_status") or "unknown").lower() == "unknown"
    )
    preserved_count = sum(1 for r in results if r.get("used_previous_status"))
    return jsonify({
        "ok": True,
        "total": len(results),
        "alive_count": alive_count,
        "login_expired_count": login_expired_count,
        "banned_count": banned_count,
        "dead_count": banned_count,
        "unknown_count": unknown_count,
        "probe_unknown_count": probe_unknown_count,
        "preserved_count": preserved_count,
        "effective_alive_count": sum(
            1 for r in results if effective_status(r) == "alive"
        ),
        "results": results,
    })


@app.route("/api/gptreg/registered/check_momo", methods=["POST"])
def gptreg_check_momo():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "缺少 email"}), 400
    result = _gptreg_check_one_momo(email, proxy=(data.get("proxy") or "").strip())
    return jsonify(result), (200 if result.get("ok") else 404)


@app.route("/api/gptreg/registered/bulk_check_momo", methods=["POST"])
def gptreg_bulk_check_momo():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    proxy = (data.get("proxy") or "").strip()
    emails = data.get("emails") or []
    if not emails:
        emails = [r.get("email") for r in gptreg_db.list_registered(limit=5000)]
    emails = [str(value).strip() for value in emails if str(value or "").strip()]
    results = []
    if emails:
        max_workers = max(1, min(6, int(data.get("concurrency") or 4), len(emails)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_gptreg_check_one_momo, email, proxy): email
                for email in emails
            }
            for future in as_completed(futures):
                email = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({
                        "ok": False,
                        "email": email,
                        "decision": "checkout_failed",
                        "conclusive": False,
                        "supported": None,
                        "error": str(exc)[:240],
                    })
    counts = {}
    for item in results:
        decision = str(item.get("decision") or "unknown")
        counts[decision] = counts.get(decision, 0) + 1
    return jsonify({
        "ok": True,
        "total": len(results),
        "supported_count": sum(1 for item in results if item.get("supported") is True),
        "conclusive_count": sum(1 for item in results if item.get("conclusive") is True),
        "failed_count": sum(1 for item in results if item.get("conclusive") is not True),
        "counts": counts,
        "results": results,
    })


@app.route("/api/gptreg/registered/check_oaics", methods=["POST"])
def gptreg_check_oaics():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "缺少 email"}), 400
    result = _gptreg_check_one_oaics(email, proxy=(data.get("proxy") or "").strip())
    return jsonify(result), (200 if result.get("ok") else 404)


@app.route("/api/gptreg/registered/bulk_check_oaics", methods=["POST"])
def gptreg_bulk_check_oaics():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    proxy = (data.get("proxy") or "").strip()
    emails = data.get("emails") or []
    if not emails:
        emails = [r.get("email") for r in gptreg_db.list_registered(limit=5000)]
    emails = [str(value).strip() for value in emails if str(value or "").strip()]
    results = []
    if emails:
        max_workers = max(1, min(6, int(data.get("concurrency") or 4), len(emails)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_gptreg_check_one_oaics, email, proxy): email
                for email in emails
            }
            for future in as_completed(futures):
                email = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    checked_at = time.time()
                    updates = {
                        "oaics_eligibility_decision": "probe_failed",
                        "oaics_eligibility_supported": False,
                        "oaics_eligibility_conclusive": False,
                        "oaics_eligibility_checked_at": checked_at,
                        "oaics_eligibility_error": str(exc)[:240],
                    }
                    try:
                        gptreg_db.update_registered_extra(email, updates)
                    except Exception:
                        pass
                    results.append({
                        "ok": True,
                        "email": email,
                        "decision": "probe_failed",
                        "supported": False,
                        "error": str(exc)[:240],
                        **updates,
                    })
    counts = {}
    for item in results:
        decision = str(item.get("decision") or item.get("oaics_eligibility_decision") or "unknown")
        counts[decision] = counts.get(decision, 0) + 1
    return jsonify({
        "ok": True,
        "total": len(results),
        "supported_count": sum(1 for item in results if item.get("supported") is True or item.get("oaics_eligibility_supported") is True),
        "conclusive_count": sum(1 for item in results if item.get("oaics_eligibility_conclusive") is True),
        "failed_count": sum(1 for item in results if item.get("oaics_eligibility_conclusive") is not True),
        "counts": counts,
        "results": results,
    })


@app.route("/api/gptreg/registered/<path:email>", methods=["DELETE"])
def gptreg_delete_registered(email: str):
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    ok = gptreg_db.delete_registered(email)
    if not ok:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/gptreg/registered/bulk_delete", methods=["POST"])
def gptreg_bulk_delete_registered():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    emails = data.get("emails") or []
    all_flag = bool(data.get("all", False))
    if all_flag:
        n = gptreg_db.delete_all_registered()
    else:
        n = gptreg_db.delete_registered_by_emails(emails)
    return jsonify({"ok": True, "deleted": n})


@app.route("/api/gptreg/registered/refetch_rt", methods=["POST"])
def gptreg_refetch_rt():
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "缺少 email"}), 400
    result = gptreg_refetch_refresh_token(
        email=email,
        proxy=(data.get("proxy") or "").strip(),
        force=bool(data.get("force", False)),
    )
    return jsonify(result), (200 if result.get("ok") or result.get("skipped") else 500)


@app.route("/api/gptreg/registered/refetch_rt/start", methods=["POST"])
def gptreg_start_refetch_rt():
    """后台启动 RT 重拿，前端用现有 run SSE 接口读取完整日志。"""
    if not GPTREG_AVAILABLE:
        return _gptreg_not_ready_response()
    data = _gptreg_json()
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "缺少 email"}), 400

    row = gptreg_db.get_registered(email)
    if not row:
        return jsonify({"ok": False, "error": f"DB 里没有 {email}"}), 404

    access_token = (row.get("access_token") or "").strip()
    session_token = (row.get("session_token") or "").strip()
    if not access_token and not session_token:
        return jsonify({
            "ok": False,
            "error": "该号既无 access_token 也无 session_token，无法重试",
        }), 400

    force = bool(data.get("force", False))
    refresh_token = (row.get("refresh_token") or "").strip()
    if refresh_token and not force:
        return jsonify({
            "ok": True,
            "skipped": True,
            "refresh_token_len": len(refresh_token),
            "message": "已有 refresh_token，已跳过重拿",
        })

    run_id = gptreg_registrar.start_refetch_rt(
        email=email,
        proxy=(data.get("proxy") or "").strip() or None,
        force=force,
    )
    return jsonify({
        "ok": True,
        "started": True,
        "run_id": run_id,
        "email": email,
    })


# ---------------------------------------------------------------------------
# free 无头注册 / migrated AliasHub integration
# ---------------------------------------------------------------------------
FREE_HEADLESS_DIR = Path(__file__).resolve().parent / "services" / "free-headless-registration"
FREE_HEADLESS_PORT = int(os.environ.get("FREE_HEADLESS_PORT", "4180"))
FREE_HEADLESS_PROCESS = None
FREE_HEADLESS_LOCK = threading.Lock()


def _free_headless_url(path: str = "/api/health") -> str:
    return f"http://127.0.0.1:{FREE_HEADLESS_PORT}{path}"


def _free_headless_alive(timeout: float = 0.8) -> bool:
    try:
        response = requests.get(_free_headless_url(), timeout=timeout)
        return response.status_code == 200
    except Exception:
        return False


def _free_headless_node() -> str:
    configured = str(os.environ.get("FREE_HEADLESS_NODE") or "").strip()
    candidates = [configured] if configured else []
    runtime_root = Path(os.environ.get("LOCALAPPDATA") or "") / "AliasHub" / "node-runtime"
    if runtime_root.is_dir():
        candidates.extend(
            str(path)
            for path in sorted(runtime_root.glob("node-v22*-win-x64/node.exe"), reverse=True)
        )
    discovered = shutil.which("node")
    if discovered:
        candidates.append(discovered)
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def _free_headless_start() -> bool:
    global FREE_HEADLESS_PROCESS
    if _free_headless_alive():
        return True
    with FREE_HEADLESS_LOCK:
        if _free_headless_alive():
            return True
        entrypoint = FREE_HEADLESS_DIR / "server" / "index.js"
        node = _free_headless_node()
        if not entrypoint.is_file() or not node:
            return False
        log_path = FREE_HEADLESS_DIR / "free_headless_host.log"
        log_fp = open(log_path, "a", encoding="utf-8", errors="replace")
        env = os.environ.copy()
        env["HOST"] = "127.0.0.1"
        env["PORT"] = str(FREE_HEADLESS_PORT)
        env["PUBLIC_BASE_URL"] = f"http://127.0.0.1:{FREE_HEADLESS_PORT}"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            FREE_HEADLESS_PROCESS = subprocess.Popen(
                [node, "server/index.js"],
                cwd=str(FREE_HEADLESS_DIR),
                env=env,
                stdout=log_fp,
                stderr=log_fp,
                creationflags=creationflags,
            )
        except Exception:
            log_fp.close()
            return False
        for _ in range(60):
            if _free_headless_alive(timeout=0.5):
                return True
            time.sleep(0.25)
        return _free_headless_alive(timeout=1.2)


@app.get("/free-headless")
def free_headless_redirect():
    return "", 302, {"Location": "/free-headless/"}


@app.get("/free-headless/")
def free_headless_page():
    if not _free_headless_start():
        return Response(
            "free headless registration service did not start",
            status=503,
            mimetype="text/plain",
        )
    return "", 302, {"Location": f"http://127.0.0.1:{FREE_HEADLESS_PORT}/#registration"}


@app.get("/api/free-headless/health")
def free_headless_health():
    started = _free_headless_start()
    return jsonify({
        "ok": bool(started and _free_headless_alive()),
        "port": FREE_HEADLESS_PORT,
        "project": str(FREE_HEADLESS_DIR),
    }), (200 if started else 503)


# ---------------------------------------------------------------------------
# Comprehensive console: checkout-link + PayPal agreement protocol
# ---------------------------------------------------------------------------
CONSOLE_SERVICES = {
    "checkout": {
        "dir": Path(__file__).resolve().parent / "services" / "pay153-checkout-link",
        "static_dir": Path(__file__).resolve().parent / "services" / "pay153-checkout-link" / "static",
        "port": int(os.environ.get("CHECKOUT_LINK_PORT", "18082")),
        "entrypoint": "app.py",
        "health_path": "/api/health",
        "log_name": "checkout_link_web.log",
    },
    "protocol": {
        "dir": Path(__file__).resolve().parent / "services" / "paypal-agreement-protocol",
        "static_dir": Path(__file__).resolve().parent / "services" / "paypal-agreement-protocol" / "web_static",
        "port": int(os.environ.get("PAYPAL_PROTOCOL_PORT", "18080")),
        "entrypoint": "web.py",
        "health_path": "/api/health",
        "log_name": "paypal_protocol_web.log",
    },
}
CONSOLE_PROCESSES = {name: None for name in CONSOLE_SERVICES}
CONSOLE_LOCKS = {name: threading.Lock() for name in CONSOLE_SERVICES}


def _console_url(name: str, path: str = "/") -> str:
    service = CONSOLE_SERVICES[name]
    return f"http://127.0.0.1:{service['port']}{path}"


def _console_alive(name: str, timeout: float = 0.45) -> bool:
    service = CONSOLE_SERVICES[name]
    try:
        response = requests.get(_console_url(name, service["health_path"]), timeout=timeout)
        return response.status_code == 200
    except Exception:
        return False


def _console_start(name: str) -> bool:
    if _console_alive(name):
        return True
    lock = CONSOLE_LOCKS[name]
    with lock:
        if _console_alive(name):
            return True
        service = CONSOLE_SERVICES[name]
        entrypoint = service["dir"] / service["entrypoint"]
        if not entrypoint.exists():
            return False
        log_path = service["dir"] / service["log_name"]
        log_fp = open(log_path, "a", encoding="utf-8", errors="replace")
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        if name == "checkout":
            env["PAY153_HOST"] = "127.0.0.1"
            env["PAY153_PORT"] = str(service["port"])
            env.setdefault(
                "PH_SHORT_CONTEXT_PATH",
                str(service["dir"] / "data" / "ph_short_contexts.jsonl"),
            )
            command = [sys.executable, str(entrypoint)]
        else:
            env.setdefault("PAYPAL_WEB_COOKIE_SECURE", "0")
            env.setdefault("PAYPAL_WEB_PRODUCTION", "0")
            command = [
                sys.executable,
                str(entrypoint),
                "--host", "127.0.0.1",
                "--port", str(service["port"]),
            ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            CONSOLE_PROCESSES[name] = subprocess.Popen(
                command,
                cwd=str(service["dir"]),
                env=env,
                stdout=log_fp,
                stderr=log_fp,
                creationflags=creationflags,
            )
        except Exception:
            log_fp.close()
            return False
        for _ in range(40):
            if _console_alive(name, timeout=0.35):
                return True
            time.sleep(0.2)
        return _console_alive(name, timeout=0.8)


def _console_api_proxy(name: str, api_path: str):
    service = CONSOLE_SERVICES[name]
    if not _console_start(name):
        return jsonify({
            "ok": False,
            "error": f"{name} console service did not start",
            "port": service["port"],
        }), 503
    suffix = f"/api/{api_path}"
    if request.query_string:
        suffix += "?" + request.query_string.decode("utf-8", errors="ignore")
    headers = {}
    for key in (
        "Content-Type", "Cookie", "Authorization", "User-Agent",
        "X-Pay153-Internal-Key", "X-Grok-Access-Token",
    ):
        value = request.headers.get(key)
        if value:
            headers[key] = value
    if request.remote_addr:
        headers["X-Real-IP"] = request.remote_addr
    try:
        upstream = requests.request(
            request.method,
            _console_url(name, suffix),
            data=request.get_data(),
            headers=headers,
            timeout=120,
            allow_redirects=False,
        )
    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": f"{name} console upstream request failed",
            "detail": str(exc),
        }), 502
    response = Response(upstream.content, status=upstream.status_code)
    for key in ("Content-Type", "Cache-Control", "Location"):
        value = upstream.headers.get(key)
        if value:
            response.headers[key] = value
    raw_headers = getattr(upstream.raw, "headers", None)
    cookies = raw_headers.getlist("Set-Cookie") if raw_headers and hasattr(raw_headers, "getlist") else []
    if not cookies and upstream.headers.get("Set-Cookie"):
        cookies = [upstream.headers["Set-Cookie"]]
    for cookie in cookies:
        response.headers.add("Set-Cookie", cookie)
    return response


@app.get("/checkout-link")
def checkout_link_redirect():
    return "", 302, {"Location": "/checkout-link/"}


@app.get("/checkout-link/")
def checkout_link_page():
    if not _console_start("checkout"):
        return Response("checkout-link service did not start", status=503, mimetype="text/plain")
    static_dir = CONSOLE_SERVICES["checkout"]["static_dir"]
    if not (static_dir / "index.html").exists():
        return Response("checkout-link static/index.html not found", status=404, mimetype="text/plain")
    return send_from_directory(str(static_dir), "index.html")


@app.get("/checkout-link/static/<path:filename>")
def checkout_link_static(filename: str):
    return send_from_directory(str(CONSOLE_SERVICES["checkout"]["static_dir"]), filename)


@app.route("/checkout-link/api/<path:api_path>", methods=["GET", "POST"])
def checkout_link_api_proxy(api_path: str):
    return _console_api_proxy("checkout", api_path)


@app.get("/paypal-pay")
def paypal_protocol_redirect():
    return "", 302, {"Location": "/paypal-pay/"}


@app.get("/paypal-pay/")
def paypal_protocol_page():
    if not _console_start("protocol"):
        return Response("PayPal protocol service did not start", status=503, mimetype="text/plain")
    static_dir = CONSOLE_SERVICES["protocol"]["static_dir"]
    if not (static_dir / "index.html").exists():
        return Response("paypal protocol web_static/index.html not found", status=404, mimetype="text/plain")
    return send_from_directory(str(static_dir), "index.html")


@app.get("/paypal-pay/static/<path:filename>")
def paypal_protocol_static(filename: str):
    return send_from_directory(str(CONSOLE_SERVICES["protocol"]["static_dir"]), filename)


@app.route("/paypal-pay/api/<path:api_path>", methods=["GET", "POST"])
def paypal_protocol_api_proxy(api_path: str):
    return _console_api_proxy("protocol", api_path)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug)
