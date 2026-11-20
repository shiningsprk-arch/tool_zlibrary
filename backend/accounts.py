# -*- coding: utf-8 -*-
"""号池：账号记录、脱敏、配额、轮换、错误分类。纯 Python，不碰网络。"""
import time
import uuid

STATUS_OK = "ok"
STATUS_EXHAUSTED = "exhausted"
STATUS_INVALID = "invalid"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"

# 服务端额度耗尽后定期复查；不把 exhausted 当永久状态。
EXHAUSTED_RECHECK_SECONDS = 60 * 60
ERROR_RETRY_SECONDS = 60
INVALID_RECHECK_SECONDS = 10 * 60
_ACCOUNT_ERRORS_INVALID = {"accounts.session_expired", "accounts.login_failed",
                           "accounts.invalid"}
_ACCOUNT_ERRORS_EXHAUSTED = {"download.limit"}


def new_account(label="", *, cookies=None, remix_userid="", remix_userkey="",
                bsrv="", email="", password_enc="", auto_renew=False):
    account_id = "acc_" + uuid.uuid4().hex[:12]
    password_enc = str(password_enc or "")
    return {
        "id": account_id,
        "label": (label or "").strip()[:60] or "账号 " + account_id[-4:],
        "email": (email or "").strip()[:120],
        "remix_userid": str(remix_userid or ""),
        "remix_userkey": str(remix_userkey or ""),
        "bsrv": str(bsrv or ""),
        "cookies": dict(cookies or {}),
        "password_enc": password_enc,
        "auto_renew": bool(auto_renew) and bool(password_enc),
        "enabled": True,
        "status": STATUS_UNKNOWN,
        "quota_today": None,
        "quota_limit": None,
        "quota_updated_at": 0,
        "quota_stale": False,
        "quota_stale_at": 0,
        "state_updated_at": 0,
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
        password_enc=raw.get("password_enc") or "",
        auto_renew=raw.get("auto_renew"),
    )
    account["id"] = str(raw.get("id") or account["id"])
    account["enabled"] = bool(raw.get("enabled", True))
    account["quota_stale"] = bool(raw.get("quota_stale", False))
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
    for key in ("quota_updated_at", "quota_stale_at", "state_updated_at",
                "last_used", "cooldown_until"):
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
        "has_password": bool(account["password_enc"]),
        "auto_renew": bool(account["auto_renew"]),
        "enabled": account["enabled"],
        "status": account["status"],
        "quota_today": account["quota_today"],
        "quota_limit": account["quota_limit"],
        "quota_stale": bool(account["quota_stale"]),
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
    account["quota_stale"] = False
    account["quota_stale_at"] = now
    account["state_updated_at"] = now
    left = remaining(account)
    if left is None:
        if account.get("status") in (STATUS_INVALID, STATUS_ERROR):
            account["status"] = STATUS_UNKNOWN
        if account.get("status") == STATUS_EXHAUSTED:
            # 没有 quota 数据时不要把 exhausted 的冷却清零，避免每个任务都重复探测。
            if account.get("cooldown_until", 0) <= now:
                account["cooldown_until"] = now + EXHAUSTED_RECHECK_SECONDS
        else:
            account["cooldown_until"] = 0
    elif left > 0:
        account["status"] = STATUS_OK
        account["cooldown_until"] = 0
    else:
        account["status"] = STATUS_EXHAUSTED
        account["cooldown_until"] = now + EXHAUSTED_RECHECK_SECONDS
    account["last_error"] = ""
    return account


def mark_success(account, now=None):
    now = time.time() if now is None else now
    account["last_used"] = now
    account["state_updated_at"] = now
    account["status"] = STATUS_OK
    account["cooldown_until"] = 0
    account["last_error"] = ""
    # 不在本地累加 quota_today：下载完成后由 eapi_profile 回写服务端真值。
    return account


def mark_error(account, code, message="", now=None):
    now = time.time() if now is None else now
    account["last_error"] = str(message or code)[:300]
    account["quota_stale"] = True
    account["quota_stale_at"] = now
    account["state_updated_at"] = now
    if code in _ACCOUNT_ERRORS_INVALID:
        account["status"] = STATUS_INVALID
        if account.get("auto_renew") and account.get("password_enc"):
            account["cooldown_until"] = now + INVALID_RECHECK_SECONDS
    elif code in _ACCOUNT_ERRORS_EXHAUSTED:
        account["status"] = STATUS_EXHAUSTED
        account["cooldown_until"] = now + EXHAUSTED_RECHECK_SECONDS
    else:
        account["status"] = STATUS_ERROR
        account["cooldown_until"] = now + ERROR_RETRY_SECONDS
    return account


def is_selectable(account, now=None):
    now = time.time() if now is None else now
    if not account.get("enabled"):
        return False
    status = account.get("status")
    if status == STATUS_INVALID:
        if not (account.get("auto_renew") and account.get("password_enc")):
            return False
        return account.get("cooldown_until", 0) <= now
    if status in (STATUS_ERROR, STATUS_EXHAUSTED):
        # exhausted 只是“上次查询为 0”，冷却后允许重新 profile 复查。
        if account.get("cooldown_until", 0) > now:
            return False
        return True
    # OK/UNKNOWN 的 quota 可能过期；让调用方先 profile 再决定。
    return True


def _status_rank(account):
    return {
        STATUS_OK: 0,
        STATUS_UNKNOWN: 0,
        STATUS_ERROR: 1,
        STATUS_EXHAUSTED: 2,
    }.get(account.get("status"), 1)


def candidate_accounts(accounts, rotation="least_used", cursor="", now=None):
    """按轮换模式返回本次取链的候选顺序。

    round_robin 以最后成功使用的账号为游标，从其后面的账号开始环形遍历；
    游标用账号 id 而不是数组下标，账号增删/重排后不会跳到错误位置。
    """
    now = time.time() if now is None else now
    if rotation == "round_robin":
        ordered = list(accounts)
        if not ordered:
            return []
        start = 0
        if cursor:
            for index, account in enumerate(ordered):
                if account.get("id") == cursor:
                    start = (index + 1) % len(ordered)
                    break
        ordered = ordered[start:] + ordered[:start]
        return [account for account in ordered if is_selectable(account, now)]
    candidates = [a for a in accounts if is_selectable(a, now)]
    candidates.sort(key=lambda a: (
        _status_rank(a),
        float(a.get("last_used") or 0),
        -(remaining(a) if remaining(a) is not None else 10 ** 9),
    ))
    return candidates


def select_account(accounts, now=None):
    candidates = candidate_accounts(accounts, now=now)
    return candidates[0] if candidates else None


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
    account["quota_stale"] = True
    account["quota_stale_at"] = time.time()
    account["state_updated_at"] = time.time()
    account["cooldown_until"] = 0
    account["last_error"] = ""
    return account
