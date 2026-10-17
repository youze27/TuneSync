"""登录校验。

当前是本地账号 + 会话令牌（cookie），但接口形状按「可替换的身份源」设计：
外部 SSO（雷池等）只需实现 trusted_identity()，其余代码不用动。

口令用 PBKDF2-HMAC-SHA256 + 随机盐存储，不落明文；会话令牌同样只存随机值，
数据库被拖库也无法反推出可用凭据。
"""
import hashlib
import hmac
import os
import secrets
import time

from .core import db, merged_config, now

COOKIE = "ts_session"
SESSION_DAYS = 7
_PBKDF2_ROUNDS = 120_000


def _cfg(section, key, default=None):
    return merged_config().get(section, {}).get(key, default)


def auth_enabled():
    """是否启用登录校验。默认开启——口令在部署后首次设置。

    env TUNESYNC_AUTH 可强制覆盖（off/0/false 关，on/1/true 开），
    留给「把自己关在门外」时的应急入口。
    """
    forced = os.environ.get("TUNESYNC_AUTH", "").strip().lower()
    if forced in ("off", "0", "false", "no"):
        return False
    if forced in ("on", "1", "true", "yes"):
        return True
    return bool(_cfg("auth", "enabled", True))


def auth_proxy_header():
    """SSO 反代注入的可信身份请求头名，留空表示只用本地账号。"""
    return str(_cfg("auth", "proxy_header") or "").strip()


def trusted_identity(headers):
    """外部 SSO 入口：反代已校验身份并注入请求头时直接采信，返回用户名或 None。

    只有配了 auth.proxy_header 才启用，否则任何人都能自己塞这个头冒充登录。
    """
    name = auth_proxy_header()
    if not name:
        return None
    user = (headers.get(name) or "").strip()
    return user or None


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(password).encode(), salt.encode(), _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt}${dk.hex()}"


def verify_password(password, stored):
    try:
        algo, rounds, salt, digest = str(stored or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", str(password).encode(), salt.encode(), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), digest)


def count_users():
    return db().execute("SELECT COUNT(*) c FROM app_user").fetchone()["c"]


def create_user(username, password):
    db().execute(
        "INSERT INTO app_user(username, password_hash, created_at) VALUES(?,?,?)",
        (str(username).strip(), hash_password(password), now()))
    db().commit()


def get_user(username):
    return db().execute("SELECT * FROM app_user WHERE username=?", (str(username),)).fetchone()


def verify_login(username, password):
    """校验账号口令。返回用户 dict 或 None；失败原因只写日志，不回传给调用方。"""
    u = get_user(username)
    if not u:
        # 即使用户不存在也跑一遍哈希，避免用响应耗时区分「用户不存在」和「口令错」
        hash_password(password)
        return None
    if not verify_password(password, u["password_hash"]):
        return None
    return dict(u)


def _purge_expired():
    db().execute("DELETE FROM auth_session WHERE expires_at < ?", (_stamp(),))
    db().commit()


def _stamp(offset_days=SESSION_DAYS):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + offset_days * 86400))


def issue_session(user_id):
    """签发会话令牌，同时顺手清掉过期会话。"""
    _purge_expired()
    token = secrets.token_urlsafe(32)
    db().execute("INSERT INTO auth_session(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                 (token, user_id, now(), _stamp()))
    db().commit()
    return token


def revoke_session(token):
    db().execute("DELETE FROM auth_session WHERE token=?", (token,))
    db().commit()


def user_of_token(token):
    """令牌有效则返回用户 dict，否则 None。"""
    if not token:
        return None
    r = db().execute(
        "SELECT u.id, u.username, s.expires_at FROM auth_session s "
        "JOIN app_user u ON u.id = s.user_id WHERE s.token=?", (token,)).fetchone()
    if not r:
        return None
    if r["expires_at"] < _stamp(0):
        revoke_session(token)
        return None
    return {"id": r["id"], "username": r["username"]}


def change_password(user_id, old_password, new_password):
    """改口令要验旧口令，防止拿到会话的人直接改密码永久夺走账号。"""
    row = db().execute("SELECT password_hash FROM app_user WHERE id=?", (user_id,)).fetchone()
    if not row or not verify_password(old_password, row["password_hash"]):
        return False
    db().execute("UPDATE app_user SET password_hash=? WHERE id=?",
                 (hash_password(new_password), user_id))
    db().commit()
    # 口令一变，所有旧会话全部失效
    db().execute("DELETE FROM auth_session WHERE user_id=?", (user_id,))
    db().commit()
    return True


def touch_login(user_id):
    db().execute("UPDATE app_user SET last_login=? WHERE id=?", (now(), user_id))
    db().commit()
