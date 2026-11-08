# -*- coding: utf-8 -*-
"""号池：账号记录、脱敏、配额、轮换、错误分类。纯 Python，不碰网络。"""
import time
import uuid

STATUS_OK = "ok"
STATUS_EXHAUSTED = "exhausted"
STATUS_INVALID = "invalid"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"

_ACCOUNT_ERRORS_INVALID = {"accounts.session_expired", "accounts.login_failed",
                           "accounts.invalid"}
_ACCOUNT_ERRORS_EXHAUSTED = {"download.limit"}


def new_account(label="", *, cookies=None, remix_userid="", remix_userkey="",
                bsrv="", email=""):
    account_id = "acc_" + uuid.uuid4().hex[:12]
    return {
        "id": account_id,
        "label": (label or "").strip()[:60] or "账号 " + account_id[-4:],
        "email": (email or "").strip()[:120],
        "remix_userid": str(remix_userid or ""),
        "remix_userkey": str(remix_userkey or ""),
        "bsrv": str(bsrv or ""),
        "cookies": dict(cookies or {}),
        "enabled": True,
        "status": STATUS_UNKNOWN,
        "quota_today": None,
        "quota_limit": None,
        "quota_updated_at": 0,
        "last_used": 0,
        "cooldown_until": 0,
        "last_error": "",
    }


def normalize_account(raw):
    raw = raw if isinstance(raw, dict) else {}
    account = new_account(
        raw.get("label") or "",
        cookies=raw.get("cookies") if isinstance(raw.get("cookies"), dict) else None,
        remix_userid=raw.get("remix_userid") or "",
        remix_userkey=raw.get("remix_userkey") or "",
        bsrv=raw.get("bsrv") or "",
        email=raw.get("email") or "",
    )
    account["id"] = str(raw.get("id") or account["id"])
    account["enabled"] = bool(raw.get("enabled", True))
    status = str(raw.get("status") or STATUS_UNKNOWN)
    account["status"] = status if status in (
        STATUS_OK, STATUS_EXHAUSTED, STATUS_INVALID, STATUS_ERROR, STATUS_UNKNOWN
    ) else STATUS_UNKNOWN
    for key in ("quota_today", "quota_limit"):
        value = raw.get(key)
        try:
            account[key] = int(value) if value is not None and value != "" else None
        except (TypeError, ValueError):
            account[key] = None
    for key in ("quota_updated_at", "last_used", "cooldown_until"):
        try:
            account[key] = float(raw.get(key) or 0)
        except (TypeError, ValueError):
            account[key] = 0.0
    account["last_error"] = str(raw.get("last_error") or "")[:300]
    return account


def _mask_secret(value):
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


def _mask_email(value):
    value = str(value or "")
    if "@" not in value:
        return _mask_secret(value)
    local, _, domain = value.partition("@")
    if len(local) <= 2:
        local = local[:1] + "***"
    else:
        local = local[:2] + "***"
    return local + "@" + domain


def public_account(account):
    account = normalize_account(account)
    return {
        "id": account["id"],
        "label": account["label"],
        "email": _mask_email(account["email"]),
        "remix_userid": _mask_secret(account["remix_userid"]),
        "remix_userkey": _mask_secret(account["remix_userkey"]),
        "bsrv": _mask_secret(account["bsrv"]),
        "has_cookies": bool(account["cookies"]),
        "enabled": account["enabled"],
        "status": account["status"],
        "quota_today": account["quota_today"],
        "quota_limit": account["quota_limit"],
        "remaining": remaining(account),
        "quota_updated_at": account["quota_updated_at"],
        "last_used": account["last_used"],
        "cooldown_until": account["cooldown_until"],
        "last_error": account["last_error"],
    }


def remaining(account):
    today = account.get("quota_today")
    limit = account.get("quota_limit")
    if today is None or limit is None:
        return None
    return max(0, int(limit) - int(today))


def apply_profile(account, profile, now=None):
    now = time.time() if now is None else now
    account["quota_today"] = profile.get("quota_today")
    account["quota_limit"] = profile.get("quota_limit")
    account["quota_updated_at"] = now
    left = remaining(account)
    if left is None:
        if account.get("status") in (STATUS_INVALID, STATUS_ERROR):
            account["status"] = STATUS_UNKNOWN
    else:
        account["status"] = STATUS_OK if left > 0 else STATUS_EXHAUSTED
        if left <= 0:
            account["cooldown_until"] = now + 3600
    account["last_error"] = ""
    return account


def mark_success(account, now=None):
    now = time.time() if now is None else now
    account["last_used"] = now
    account["status"] = STATUS_OK
    account["last_error"] = ""
    if account.get("quota_today") is not None:
        account["quota_today"] = int(account["quota_today"]) + 1
    return account


def mark_error(account, code, message="", now=None):
    now = time.time() if now is None else now
    account["last_error"] = str(message or code)[:300]
    if code in _ACCOUNT_ERRORS_INVALID:
        account["status"] = STATUS_INVALID
    elif code in _ACCOUNT_ERRORS_EXHAUSTED:
        account["status"] = STATUS_EXHAUSTED
        account["cooldown_until"] = now + 3600
    else:
        account["status"] = STATUS_ERROR
        account["cooldown_until"] = now + 60
    return account


def is_selectable(account, now=None):
    now = time.time() if now is None else now
    if not account.get("enabled"):
        return False
    if account.get("status") in (STATUS_INVALID, STATUS_EXHAUSTED):
        return False
    if account.get("status") == STATUS_ERROR and account.get("cooldown_until", 0) > now:
        return False
    left = remaining(account)
    if left is not None and left <= 0:
        return False
    return True


def select_account(accounts, now=None):
    now = time.time() if now is None else now
    candidates = [a for a in accounts if is_selectable(a, now)]
    if not candidates:
        return None
    candidates.sort(key=lambda a:(
        float(a.get("last_used") or 0),
        -(remaining(a) if remaining(a) is not None else 10 ** 9),
    ))
    return candidates[0]


def find_account(accounts, account_id):
    for account in accounts:
        if account.get("id") == account_id:
            return account
    return None


def merge_login(account, login):
    login = login or {}
    cookies = dict(login.get("cookies") or {})
    for key in ("remix_userid", "remix_userkey", "bsrv"):
        value = str(login.get(key) or "")
        if value:
            cookies.setdefault(key, value)
            account[key] = value
    # 不保存挑战 cookie
    cookies.pop("c_token", None)
    cookies.pop("c_time", None)
    account["cookies"] = cookies
    if login.get("email"):
        account["email"] = str(login["email"])[:120]
    account["status"] = STATUS_UNKNOWN
    account["last_error"] = ""
    return account
