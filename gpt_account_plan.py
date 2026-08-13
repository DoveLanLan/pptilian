from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import requests


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

MOMO_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
MOMO_DEFAULT_STRIPE_PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
MOMO_STRIPE_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
MOMO_DECISION_TEXT = {
    "ready": "支持真实试用，且当前结账会话支持 MoMo",
    "account_trial_ineligible": "账号没有真实试用资格",
    "trial_not_applied": "试用期未被后端采用",
    "momo_not_enabled": "试用已生效，但当前结账会话未启用 MoMo",
    "already_paid": "账号已有付费订阅，不适用于新订阅资格检测",
    "credential_invalid": "账号凭据失效或已过期",
    "checkout_failed": "结账会话创建失败，本次结果待确认",
    "stripe_init_failed": "结账会话已创建，但支付方式读取失败",
    "payment_methods_unknown": "支付接口未返回明确的支付方式列表",
    "unexpected_mode": "结账会话不是订阅模式",
}


def _walk(obj: Any, path: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k)
            next_path = f"{path}.{key}" if path else key
            yield next_path, key, v
            yield from _walk(v, next_path)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            next_path = f"{path}[{i}]"
            yield next_path, str(i), v
            yield from _walk(v, next_path)


def _interesting_path(path: str) -> bool:
    p = (path or "").lower()
    return any(
        token in p
        for token in (
            "plan",
            "subscription",
            "entitlement",
            "billing",
            "product",
            "workspace",
            "account",
            "seat",
            "sku",
        )
    )


def classify_chatgpt_plan(data: Any) -> tuple[str, list[str]]:
    """Return (free/plus/pro/unknown, evidence list)."""
    if not isinstance(data, (dict, list)):
        return "unknown", []

    evidence: list[str] = []
    active_subscription: bool | None = None
    current_plan_hint = ""
    free_hint = False

    pro_re = re.compile(r"(chatgpt[_ -]?pro|pro[_ -]?plan|plan[_ -]?type[:= ]*pro|\bpro\b)")
    plus_re = re.compile(r"(chatgpt[_ -]?plus|chatgptplusplan|plus[_ -]?plan|plan[_ -]?type[:= ]*plus|\bplus\b)")

    for path, key, value in _walk(data):
        pl = path.lower()
        kl = key.lower()

        # 这些字段只是“可购买/可试用/历史订阅”，不是当前账号属性，必须忽略。
        if any(skip in pl for skip in (
            "eligible_",
            "promo",
            "offer",
            "previously",
            "last_active_subscription",
            "yearly_plus",
            "trial",
        )):
            continue

        if isinstance(value, bool):
            # 当前生效订阅：只认 entitlement/current/active 语义，不认 history/previous/offers。
            if (
                "has_active_subscription" in kl
                or "is_paid_subscription_active" in kl
                or kl in {"is_subscribed", "is_paid"}
            ):
                active_subscription = bool(value)
                evidence.append(f"{path}={str(value).lower()}")
            continue

        if not isinstance(value, str):
            continue

        text = value.strip().lower()
        if not text:
            continue

        # 当前计划强字段。
        is_current_plan_field = (
            pl.endswith(".account.plan_type")
            or pl.endswith(".entitlement.subscription_plan")
            or kl in {"plan_type", "subscription_plan", "current_plan", "current_plan_type"}
        )
        if not is_current_plan_field:
            continue

        if "free" in text:
            free_hint = True
            evidence.append(f"{path}={value[:80]}")
            # account.plan_type=free / entitlement.subscription_plan=chatgptfreeplan 是强判定。
            current_plan_hint = current_plan_hint or "free"
            continue
        if pro_re.search(text):
            current_plan_hint = "pro"
            evidence.append(f"{path}={value[:80]}")
            continue
        if plus_re.search(text):
            current_plan_hint = "plus"
            evidence.append(f"{path}={value[:80]}")
            continue

    # 优先级：当前 active 订阅 + 当前 plan。没有 active 订阅时，即使存在 Plus offer，也不是 Plus。
    if active_subscription is False:
        if current_plan_hint in ("free", "", "unknown") or free_hint:
            return "free", evidence[:10]
        return "free", evidence[:10]
    if active_subscription is True:
        if current_plan_hint in ("pro", "plus"):
            return current_plan_hint, evidence[:10]
        return "plus", evidence[:10]
    if current_plan_hint in ("pro", "plus", "free"):
        return current_plan_hint, evidence[:10]
    if free_hint:
        return "free", evidence[:10]

    # accounts/check 正常返回但没有付费标志时，通常就是 free。
    if isinstance(data, dict) and (data.get("accounts") or data.get("account_ordering") or data.get("account")):
        return "free", evidence[:10]
    return "unknown", evidence[:10]


def detect_chatgpt_plan(
    access_token: str = "",
    cookie_header: str = "",
    proxy: str = "",
    timeout: int = 20,
) -> dict:
    access_token = (access_token or "").strip()
    cookie_header = (cookie_header or "").strip()
    proxy = (proxy or "").strip()
    if not access_token and not cookie_header:
        return {"ok": False, "plan": "unknown", "error": "缺少 access_token / cookie_header"}

    device_id = str(uuid.uuid4())
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "oai-language": "en-US",
        "oai-device-id": device_id,
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if cookie_header:
        headers["Cookie"] = cookie_header
        m = re.search(r"(?:^|;\s*)oai-did=([^;]+)", cookie_header)
        if m:
            headers["oai-device-id"] = m.group(1)

    sess = requests.Session()
    sess.trust_env = False if proxy else True
    if proxy:
        sess.proxies.update({"http": proxy, "https": proxy})

    urls = [
        "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        "https://chatgpt.com/backend-api/accounts/check",
        "https://chatgpt.com/backend-api/me",
    ]
    last_error = ""
    for url in urls:
        try:
            resp = sess.get(url, headers=headers, timeout=timeout)
            status = int(getattr(resp, "status_code", 0) or 0)
            if status == 401 or status == 403:
                last_error = f"HTTP {status}"
                continue
            if status >= 400:
                last_error = f"HTTP {status}: {resp.text[:160]}"
                continue
            data = resp.json() if resp.text else {}
            plan, evidence = classify_chatgpt_plan(data)
            return {
                "ok": True,
                "plan": plan,
                "source": url.rsplit("/", 1)[-1],
                "status_code": status,
                "evidence": evidence,
                "checked_at": time.time(),
            }
        except Exception as exc:
            last_error = str(exc)
            continue

    return {
        "ok": False,
        "plan": "unknown",
        "error": last_error or "账号属性检测失败",
        "checked_at": time.time(),
    }


def _response_preview(resp: requests.Response, limit: int = 240) -> str:
    try:
        text = resp.text or ""
    except Exception:
        text = ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _live_network_error(exc: Exception, proxy: str = "") -> str:
    """Convert requests transport errors into short, actionable UI messages."""
    if isinstance(exc, requests.exceptions.ProxyError):
        return "代理连接失败，请确认代理程序和端口已启动"
    if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
        return "检测请求超时，请检查代理线路后重试"
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS 连接失败，请检查代理协议和系统时间"
    if isinstance(exc, requests.exceptions.ConnectionError):
        if proxy:
            return "通过代理连接 ChatGPT 失败，请检查代理线路"
        return "连接 ChatGPT 失败，请检查网络"
    return str(exc)


def _dead_signal_reason(text: str) -> str:
    t = (text or "").lower()
    signals = (
        ("deactivated", "账号已停用/封禁"),
        ("account_deactivated", "账号已停用/封禁"),
        ("user_deactivated", "账号已停用/封禁"),
        ("suspended", "账号已暂停/封禁"),
        ("banned", "账号已封禁"),
        ("terminated", "账号已终止"),
        ("account disabled", "账号已禁用"),
        ("user disabled", "账号已禁用"),
        ("deleted", "账号已删除"),
    )
    for key, label in signals:
        if key in t:
            return label
    return ""


def _json_dead_signal(data: Any) -> str:
    """只看真正的账号/错误状态字段，避免把 feature_flags 里的 disabled:false 误判成封号。"""
    if not isinstance(data, (dict, list)):
        return _dead_signal_reason(str(data or ""))

    def label_from_text(text: str, strict: bool = False) -> str:
        t = (text or "").strip().lower()
        if not t:
            return ""
        checks = (
            (r"\b(account_)?deactivated\b|\buser_deactivated\b", "账号已停用/封禁"),
            (r"\bsuspended\b", "账号已暂停/封禁"),
            (r"\bbanned\b", "账号已封禁"),
            (r"\bterminated\b", "账号已终止"),
            (r"\bdeleted\b", "账号已删除"),
            (r"\b(account|user)[ _-]?disabled\b", "账号已禁用"),
        )
        for pat, label in checks:
            if re.search(pat, t):
                return label
        if strict and t in {"disabled", "deactivated", "suspended", "banned", "terminated", "deleted"}:
            return {
                "disabled": "账号已禁用",
                "deactivated": "账号已停用/封禁",
                "suspended": "账号已暂停/封禁",
                "banned": "账号已封禁",
                "terminated": "账号已终止",
                "deleted": "账号已删除",
            }.get(t, "")
        return ""

    status_keys = {
        "error", "error_code", "code", "message", "detail", "reason",
        "status", "account_status", "user_status", "state",
    }
    banned_bool_keys = {
        "is_deactivated", "deactivated",
        "is_suspended", "suspended",
        "is_banned", "banned",
        "is_terminated", "terminated",
        "is_deleted", "deleted",
        "account_deactivated", "user_deactivated",
        "account_suspended", "user_suspended",
        "account_banned", "user_banned",
        "account_disabled", "user_disabled",
    }

    for path, key, value in _walk(data):
        p = (path or "").lower()
        k = (key or "").lower()
        if isinstance(value, bool):
            if value is True and k in banned_bool_keys:
                return label_from_text(k.replace("_", " "), strict=True) or "账号状态异常"
            continue
        if not isinstance(value, str):
            continue

        strict_status_field = (
            k in status_keys
            or p.endswith(".account.status")
            or p.endswith(".user.status")
            or p.endswith(".account.state")
            or p.endswith(".user.state")
        )
        if strict_status_field:
            label = label_from_text(value, strict=True)
            if label:
                return label

        # error/message/detail 里可能是自然语言，允许宽松匹配；普通 feature 字段不扫。
        if k in {"error", "error_code", "code", "message", "detail", "reason"}:
            label = label_from_text(value, strict=False)
            if label:
                return label
    return ""


def _json_live_success_evidence(data: Any, source: str) -> tuple[bool, str]:
    """Require account-shaped JSON before treating a HTTP 2xx as proof of life."""
    if not isinstance(data, dict) or not data:
        return False, ""

    source = (source or "").strip().lower()
    if source == "api/auth/session":
        token = data.get("accessToken") or data.get("access_token")
        user = data.get("user")
        account = data.get("account")
        if isinstance(token, str) and token.strip() and (
            isinstance(user, dict) and user
            or isinstance(account, dict) and account
        ):
            return True, "session 返回用户身份和 access token"
        return False, ""

    if source in {"v4-2023-04-27", "check"}:
        accounts = data.get("accounts")
        account = data.get("account")
        ordering = data.get("account_ordering")
        if isinstance(accounts, (dict, list)) and bool(accounts):
            return True, "accounts/check 返回账号集合"
        if isinstance(account, dict) and bool(account):
            return True, "accounts/check 返回账号身份"
        if isinstance(ordering, list) and bool(ordering):
            return True, "accounts/check 返回账号顺序"
        return False, ""

    if source == "me":
        if any(data.get(key) for key in ("id", "email", "user_id", "account_id")):
            return True, "me 返回账号身份"
        user = data.get("user")
        account = data.get("account")
        if isinstance(user, dict) and user:
            return True, "me 返回用户身份"
        if isinstance(account, dict) and account:
            return True, "me 返回账号身份"
        return False, ""

    return False, ""


def _classify_live_http_status(status: int, text: str = "") -> tuple[str, str]:
    """Return (live_status, reason) for non-2xx ChatGPT auth responses."""
    signal = _dead_signal_reason(text)
    if signal:
        return "banned", f"{signal} HTTP {status}"
    lowered = (text or "").lower()
    if status in (401, 403) and any(
        marker in lowered
        for marker in ("token_revoked", "invalidated oauth token", "invalid_token")
    ):
        return "login_expired", f"token 已撤销或失效 HTTP {status}"
    if status == 401:
        return "login_expired", "登录态/token 已失效 HTTP 401"
    if status == 403:
        return "login_expired", "接口拒绝或登录态失效 HTTP 403（不能据此判断封号）"
    if status == 429:
        return "unknown", "请求过快 / 被限速 HTTP 429"
    if status >= 500:
        return "unknown", f"ChatGPT 服务异常 HTTP {status}"
    if status >= 400:
        return "unknown", f"检测接口 HTTP {status}"
    return "unknown", f"检测接口 HTTP {status}"


def detect_chatgpt_live(
    access_token: str = "",
    cookie_header: str = "",
    proxy: str = "",
    timeout: int = 15,
) -> dict:
    """Detect whether a stored ChatGPT credential is still usable.

    live_status:
      - alive:   ChatGPT auth/backend endpoint accepted the credential.
      - login_expired: token/session rejected by auth; 只能说明保存的凭证失效。
      - banned:  response explicitly indicates disabled/deactivated/suspended account.
      - unknown: network/rate-limit/service/endpoint issue, retry later.
    """
    access_token = (access_token or "").strip()
    cookie_header = (cookie_header or "").strip()
    proxy = (proxy or "").strip()
    checked_at = time.time()
    if not access_token and not cookie_header:
        return {
            "ok": False,
            "alive": False,
            "live_status": "unknown",
            "reason": "缺少 access_token / cookie_header",
            "checked_at": checked_at,
        }

    device_id = str(uuid.uuid4())
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "oai-language": "en-US",
        "oai-device-id": device_id,
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
        m = re.search(r"(?:^|;\s*)oai-did=([^;]+)", cookie_header)
        if m:
            headers["oai-device-id"] = m.group(1)

    sess = requests.Session()
    sess.trust_env = False if proxy else True
    if proxy:
        sess.proxies.update({"http": proxy, "https": proxy})

    banned_candidate: dict | None = None
    login_expired_candidate: dict | None = None
    unknown_candidate: dict | None = None
    session_evidence: dict | None = None

    def remember(status_name: str, reason: str, source: str, status_code: int = 0) -> None:
        nonlocal banned_candidate, login_expired_candidate, unknown_candidate
        item = {
            "ok": status_name == "alive",
            "alive": status_name == "alive",
            "live_status": status_name,
            "reason": reason,
            "source": source,
            "status_code": status_code,
            "checked_at": time.time(),
        }
        if status_name == "banned":
            banned_candidate = banned_candidate or item
        elif status_name == "login_expired":
            login_expired_candidate = login_expired_candidate or item
        else:
            unknown_candidate = unknown_candidate or item

    # Cookie/session-token 可先换新 accessToken，但 session 成功不等于后端业务
    # 接口接受该 token。必须继续探测受保护接口，避免把已撤销 token 误判为存活。
    if cookie_header:
        try:
            resp = sess.get("https://chatgpt.com/api/auth/session", headers=headers, timeout=timeout)
            status = int(getattr(resp, "status_code", 0) or 0)
            preview = _response_preview(resp)
            if 200 <= status < 300:
                try:
                    data = resp.json() if resp.text else {}
                except Exception:
                    data = {}
                new_token = ""
                if isinstance(data, dict):
                    new_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
                signal = _json_dead_signal(data)
                if signal:
                    return {
                        "ok": False,
                        "alive": False,
                        "live_status": "banned",
                        "reason": signal,
                        "source": "api/auth/session",
                        "status_code": status,
                        "checked_at": time.time(),
                    }
                session_ok, session_reason = _json_live_success_evidence(
                    data, "api/auth/session"
                )
                if new_token:
                    access_token = new_token
                if session_ok:
                    session_evidence = {
                        "source": "api/auth/session",
                        "status_code": status,
                        "reason": session_reason,
                    }
                else:
                    remember(
                        "unknown",
                        "session 接口 HTTP 2xx，但缺少可验证的用户/token 身份",
                        "api/auth/session",
                        status,
                    )
            else:
                st, reason = _classify_live_http_status(status, preview)
                remember(st, reason, "api/auth/session", status)
        except Exception as exc:
            remember(
                "unknown",
                f"session 检测异常: {_live_network_error(exc, proxy)}",
                "api/auth/session",
                0,
            )

    urls = [
        "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        "https://chatgpt.com/backend-api/accounts/check",
        "https://chatgpt.com/backend-api/me",
    ]
    for url in urls:
        h = dict(headers)
        if access_token:
            h["Authorization"] = f"Bearer {access_token}"
        try:
            resp = sess.get(url, headers=h, timeout=timeout)
            status = int(getattr(resp, "status_code", 0) or 0)
            preview = _response_preview(resp)
            source = url.rsplit("/", 1)[-1]
            if 200 <= status < 300:
                try:
                    data = resp.json() if resp.text else {}
                except Exception:
                    data = None
                signal = _json_dead_signal(data)
                if signal:
                    return {
                        "ok": False,
                        "alive": False,
                        "live_status": "banned",
                        "reason": signal,
                        "source": source,
                        "status_code": status,
                        "checked_at": time.time(),
                    }
                has_evidence, evidence_reason = _json_live_success_evidence(data, source)
                if has_evidence:
                    return {
                        "ok": True,
                        "alive": True,
                        "live_status": "alive",
                        "reason": evidence_reason or "受保护接口已接受凭证",
                        "source": source,
                        "status_code": status,
                        "checked_at": time.time(),
                    }
                remember(
                    "unknown",
                    f"{source} HTTP 2xx，但响应缺少账号身份字段",
                    source,
                    status,
                )
                continue
            st, reason = _classify_live_http_status(status, preview)
            remember(st, reason, source, status)
        except Exception as exc:
            remember(
                "unknown",
                f"检测异常: {_live_network_error(exc, proxy)}",
                url.rsplit("/", 1)[-1],
                0,
            )

    if banned_candidate:
        return banned_candidate
    if login_expired_candidate:
        return login_expired_candidate
    if session_evidence and unknown_candidate:
        unknown_candidate = dict(unknown_candidate)
        unknown_candidate["reason"] = (
            f"session 有效，但受保护接口未确认账号可用："
            f"{unknown_candidate.get('reason') or '结果不确定'}"
        )
    if unknown_candidate:
        return unknown_candidate
    return {
        "ok": False,
        "alive": False,
        "live_status": "unknown",
        "reason": "账号存活检测失败",
        "checked_at": time.time(),
    }


def _momo_result(decision: str, *, checked_at: float, **fields: Any) -> dict:
    """Build a credential-free public result for the MoMo eligibility probe."""
    conclusive = bool(fields.pop("conclusive", decision not in {
        "checkout_failed", "stripe_init_failed", "payment_methods_unknown"
    }))
    supported = fields.pop(
        "supported", True if decision == "ready" else (False if conclusive else None)
    )
    error = str(fields.pop("error", "") or "")[:240]
    return {
        "ok": conclusive,
        "supported": supported,
        "conclusive": conclusive,
        "decision": decision,
        "decision_text": MOMO_DECISION_TEXT.get(decision, decision),
        "source": str(fields.pop("source", "momo-checkout") or "momo-checkout")[:100],
        "checked_at": checked_at,
        "error": error,
        **fields,
    }


def _momo_checkout_error(status: int, text: str) -> str:
    sample = str(text or "")[:2000].lower()
    if re.search(
        r"already.{0,40}(paid|subscribed)|active.{0,20}subscription|"
        r"already a subscriber|user_already_paid",
        sample,
    ):
        return "already_paid"
    if re.search(r"cloudflare|cf-chl-|challenge-platform|captcha|<!doctype html", sample):
        return "checkout_failed"
    if status in (401, 403):
        return "credential_invalid"
    return "checkout_failed"


def _momo_actual_trial(payload: Any) -> bool:
    """Find applied trial markers without treating eligibility as a trial."""
    for path, key, value in _walk(payload):
        lower_key = key.lower()
        lower_path = path.lower()
        if "eligible" in lower_path:
            continue
        if lower_key == "trial_period_days":
            try:
                if int(value or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
        if lower_key == "trial_end" and value not in (None, "", 0, "0", False):
            return True
    return False


def _momo_methods(payload: Any) -> list[str] | None:
    if not isinstance(payload, dict):
        return None
    methods: list[str] = []
    observed_list = False

    def add(value: Any) -> None:
        nonlocal observed_list
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized and normalized not in methods:
                methods.append(normalized)
        elif isinstance(value, list):
            observed_list = True
            for item in value:
                add(item)
        elif isinstance(value, dict) and "type" in value:
            add(value.get("type"))

    sources = [payload]
    if isinstance(payload.get("elements_options"), dict):
        sources.append(payload["elements_options"])
    for source in sources:
        for key in (
            "payment_method_types", "ordered_payment_method_types",
            "payment_method_specs", "custom_payment_methods",
        ):
            if key in source:
                add(source.get(key))
    return sorted(methods) if methods or observed_list else None


def _momo_stripe_field(payload: dict, key: str) -> Any:
    elements = payload.get("elements_options")
    if isinstance(elements, dict) and key in elements:
        return elements.get(key)
    return payload.get(key)


def _momo_amount_due(payload: dict) -> int | None:
    candidates: list[Any] = []
    total_summary = payload.get("total_summary")
    if isinstance(total_summary, dict):
        candidates.append(total_summary.get("due"))
    candidates.extend([payload.get("amount_total"), _momo_stripe_field(payload, "amount")])
    invoice = payload.get("invoice")
    if isinstance(invoice, dict):
        candidates.append(invoice.get("amount_due"))
    for value in candidates:
        if value is not None:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                pass
    return None


def detect_chatgpt_momo_eligibility(
    access_token: str = "",
    cookie_header: str = "",
    proxy: str = "",
    timeout: int = 20,
    trial_days: int = 30,
) -> dict:
    """Check Vietnam trial/MoMo eligibility without confirming a payment.

    Exactly one unconfirmed Checkout Session is created. The result contains
    only non-secret summary fields and never includes credentials/session IDs.
    """
    access_token = str(access_token or "").strip()
    cookie_header = str(cookie_header or "").strip()
    proxy = str(proxy or "").strip()
    checked_at = time.time()
    if not access_token and not cookie_header:
        return _momo_result(
            "credential_invalid", checked_at=checked_at,
            error="缺少 access_token / cookie_header", source="momo-checkout/preflight",
        )

    device_id = str(uuid.uuid4())
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "oai-language": "vi-VN",
        "oai-device-id": device_id,
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if cookie_header:
        headers["Cookie"] = cookie_header
        match = re.search(r"(?:^|;\s*)oai-did=([^;]+)", cookie_header)
        if match:
            headers["oai-device-id"] = match.group(1)

    session = requests.Session()
    session.trust_env = False if proxy else True
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    body = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "billing_details": {"country": "VN", "currency": "VND"},
        "checkout_ui_mode": "custom",
        "subscription_data": {"trial_period_days": max(1, int(trial_days or 30))},
    }
    try:
        response = session.post(MOMO_CHECKOUT_URL, json=body, headers=headers, timeout=timeout)
    except Exception as exc:
        return _momo_result(
            "checkout_failed", checked_at=checked_at, conclusive=False, supported=None,
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )

    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        decision = _momo_checkout_error(status, getattr(response, "text", ""))
        definite = decision in {"already_paid", "credential_invalid"}
        return _momo_result(
            decision, checked_at=checked_at, conclusive=definite,
            supported=False if definite else None,
            error="" if definite else f"HTTP {status}", status_code=status,
        )
    try:
        checkout = response.json() or {}
    except Exception:
        checkout = {}
    if not isinstance(checkout, dict):
        checkout = {}

    one_click = checkout.get("one_click_trial_eligible")
    new_customer = checkout.get("is_new_stripe_customer")
    trial_in_checkout = _momo_actual_trial(checkout)
    checkout_id = str(
        checkout.get("checkout_session_id") or checkout.get("session_id")
        or checkout.get("id") or ""
    ).strip()
    raw_key = str(
        checkout.get("stripe_publishable_key") or checkout.get("publishable_key")
        or checkout.get("publishableKey") or checkout.get("stripePublishableKey")
        or checkout.get("key") or ""
    )
    key_match = re.search(r"pk_live_[A-Za-z0-9]+", raw_key)
    stripe_key = key_match.group(0) if key_match else MOMO_DEFAULT_STRIPE_PK
    common = {
        "one_click_trial_eligible": one_click if isinstance(one_click, bool) else None,
        "is_new_stripe_customer": new_customer if isinstance(new_customer, bool) else None,
        "trial_in_checkout": trial_in_checkout,
    }
    if one_click is False and not trial_in_checkout:
        return _momo_result(
            "account_trial_ineligible", checked_at=checked_at, actual_trial=False,
            has_momo=None, methods=None, stripe_mode=None, **common,
        )
    if not checkout_id.startswith("cs_"):
        return _momo_result(
            "checkout_failed", checked_at=checked_at, conclusive=False, supported=None,
            error="结账接口未返回有效会话", actual_trial=trial_in_checkout,
            has_momo=None, methods=None, stripe_mode=None, **common,
        )

    stripe_session = requests.Session()
    stripe_session.trust_env = False if proxy else True
    stripe_session.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    })
    if proxy:
        stripe_session.proxies.update({"http": proxy, "https": proxy})
    stripe_form = {
        "browser_locale": "vi-VN",
        "browser_timezone": "Asia/Ho_Chi_Minh",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
        "elements_session_client[session_id]": f"elements_session_{uuid.uuid4().hex[:11]}",
        "elements_session_client[locale]": "vi",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_key,
        "_stripe_version": MOMO_STRIPE_VERSION,
    }
    try:
        stripe_response = stripe_session.post(
            f"https://api.stripe.com/v1/payment_pages/{checkout_id}/init",
            data=stripe_form, timeout=timeout,
        )
    except Exception as exc:
        return _momo_result(
            "stripe_init_failed", checked_at=checked_at, conclusive=False, supported=None,
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
            actual_trial=trial_in_checkout, has_momo=None, methods=None,
            stripe_mode=None, **common,
        )
    stripe_status = int(getattr(stripe_response, "status_code", 0) or 0)
    if stripe_status >= 400:
        return _momo_result(
            "stripe_init_failed", checked_at=checked_at, conclusive=False, supported=None,
            error=f"HTTP {stripe_status}", status_code=stripe_status,
            actual_trial=trial_in_checkout, has_momo=None, methods=None,
            stripe_mode=None, **common,
        )
    try:
        stripe_payload = stripe_response.json() or {}
    except Exception:
        stripe_payload = {}
    if not isinstance(stripe_payload, dict):
        stripe_payload = {}

    methods = _momo_methods(stripe_payload)
    has_momo = None if methods is None else "momo" in methods
    actual_trial = bool(trial_in_checkout or _momo_actual_trial(stripe_payload))
    stripe_mode = _momo_stripe_field(stripe_payload, "mode")
    if not actual_trial and one_click is False:
        decision = "account_trial_ineligible"
    elif not actual_trial:
        decision = "trial_not_applied"
    elif stripe_mode != "subscription":
        decision = "unexpected_mode"
    elif has_momo is None:
        decision = "payment_methods_unknown"
    else:
        decision = "ready" if has_momo else "momo_not_enabled"
    return _momo_result(
        decision, checked_at=checked_at,
        conclusive=decision != "payment_methods_unknown",
        supported=(decision == "ready") if decision != "payment_methods_unknown" else None,
        actual_trial=actual_trial, has_momo=has_momo, methods=methods,
        stripe_mode=stripe_mode, amount_due=_momo_amount_due(stripe_payload),
        currency=_momo_stripe_field(stripe_payload, "currency"), **common,
    )
