"""Authentication for internal deployment.

Modes (config auth.mode):
  - "ldap"  : bind against the company LDAP/AD server (production)
  - "token" : single shared access token (simple internal pilot)
  - "none"  : open (local dev only — refuses to run if host is public)

On success a signed, expiring session cookie is issued (itsdangerous).
Protected routes require a valid session. Credentials are never logged or
stored; LDAP bind is done live per login.
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from backend.app_config import CFG

COOKIE_NAME = "koosys_session"


def _secret() -> str:
    s = CFG.get("auth.session_secret", "")
    if not s:
        # fail loud in production; a random per-process secret only suits dev
        import os
        s = os.environ.get("KOOSYS_SESSION_SECRET", "")
    if not s:
        raise RuntimeError(
            "auth.session_secret (or KOOSYS_SESSION_SECRET) must be set for "
            "authenticated deployment.")
    return s


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt="koosys-session")


def issue_session(username: str) -> str:
    return _serializer().dumps({"u": username, "t": int(time.time())})


def verify_session(token: str) -> Optional[str]:
    ttl = int(CFG.get("auth.session_ttl_seconds", 28800))  # 8h
    try:
        data = _serializer().loads(token, max_age=ttl)
        return data.get("u")
    except (BadSignature, SignatureExpired, Exception):  # noqa: BLE001
        return None


def current_user(request: Request) -> Optional[str]:
    mode = str(CFG.get("auth.mode", "none")).lower()
    if mode == "none":
        return "anonymous"
    tok = request.cookies.get(COOKIE_NAME)
    return verify_session(tok) if tok else None


# ------------------------------------------------------------------ login
def authenticate(username: str, password: str) -> tuple[bool, str]:
    """Return (ok, message). Never logs the password."""
    mode = str(CFG.get("auth.mode", "none")).lower()
    if mode == "none":
        return True, "auth disabled"
    if mode == "token":
        expected = CFG.get("auth.shared_token", "")
        import os
        expected = expected or os.environ.get("KOOSYS_SHARED_TOKEN", "")
        if expected and password == expected:
            return True, "token ok"
        return False, "invalid token"
    if mode == "ldap":
        return _ldap_authenticate(username, password)
    return False, f"unknown auth mode: {mode}"


def _ldap_authenticate(username: str, password: str) -> tuple[bool, str]:
    import ssl

    from ldap3 import ALL, Connection, Server, Tls

    if not username or not password:
        return False, "username and password required"

    uri = CFG.get("auth.ldap.server_uri", "")
    if not uri:
        return False, "auth.ldap.server_uri not configured"
    use_tls = bool(CFG.get("auth.ldap.use_tls", True))
    verify = bool(CFG.get("auth.ldap.tls_verify", True))
    tls = Tls(validate=ssl.CERT_REQUIRED if verify else ssl.CERT_NONE) if use_tls else None
    server = Server(uri, use_ssl=uri.lower().startswith("ldaps"), tls=tls, get_info=ALL,
                    connect_timeout=int(CFG.get("auth.ldap.timeout_seconds", 8)))

    # bind DN: either a direct template, or search-then-bind
    dn_template = CFG.get("auth.ldap.bind_dn_template", "")
    if dn_template:
        user_dn = dn_template.format(username=_escape_dn(username))
        return _try_bind(server, user_dn, password)

    # search-then-bind: bind as a service account, find the user, bind as user
    svc_dn = CFG.get("auth.ldap.search_bind_dn", "")
    svc_pw = CFG.get("auth.ldap.search_bind_password", "")
    base_dn = CFG.get("auth.ldap.base_dn", "")
    user_attr = CFG.get("auth.ldap.user_attribute", "sAMAccountName")
    if not base_dn:
        return False, "auth.ldap.base_dn required for search-bind"
    try:
        svc = Connection(server, user=svc_dn or None, password=svc_pw or None,
                         auto_bind=True)
    except Exception as exc:  # noqa: BLE001
        return False, f"LDAP service bind failed: {str(exc)[:120]}"
    flt = f"({user_attr}={_escape_filter(username)})"
    if not svc.search(base_dn, flt, attributes=["distinguishedName"]):
        return False, "user not found"
    if not svc.entries:
        return False, "user not found"
    user_dn = str(svc.entries[0].entry_dn)
    svc.unbind()
    return _try_bind(server, user_dn, password)


def _try_bind(server, user_dn: str, password: str) -> tuple[bool, str]:
    from ldap3 import Connection

    try:
        conn = Connection(server, user=user_dn, password=password, auto_bind=True)
        conn.unbind()
        return True, "ldap bind ok"
    except Exception:  # noqa: BLE001 — invalid creds or server error
        return False, "invalid credentials"


def _escape_dn(value: str) -> str:
    for ch in ('\\', ',', '+', '"', '<', '>', ';', '='):
        value = value.replace(ch, '\\' + ch)
    return value


def _escape_filter(value: str) -> str:
    out = []
    for ch in value:
        if ch in ('*', '(', ')', '\\', '\0'):
            out.append('\\%02x' % ord(ch))
        else:
            out.append(ch)
    return ''.join(out)
