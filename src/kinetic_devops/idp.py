"""Epicor IdP (OpenID Connect) sign-in for Kinetic environments.

Kinetic publishes its IdP settings anonymously at ``<server>/api/.configuration`` (the login page reads them
before anyone signs in). The IdP is a standard OIDC provider, so a CLI can sign in with authorization code +
PKCE and a loopback redirect (RFC 8252): the browser does the sign-in (Azure AD, SAML, ...), and the code
comes back to a listener on 127.0.0.1. No password ever passes through this process. Refresh tokens renew
the session without a prompt.

Epicor's published clients don't allow CLI flows, so each IdP tenant needs its own registered public client;
its ID is stored per tenant in the keyring (see Documents/EPICOR_IDP_SIGNIN.md).

Refresh tokens may rotate (one-time use): refreshes take a per-slot file lock and re-read the stored token
first, so parallel processes don't burn each other's refresh token.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from typing import Callable, Dict, Iterator, Optional

from kinetic_devops import _lazy_import

keyring = _lazy_import("keyring")
requests = _lazy_import("requests")

SERVICE_IDP = "kinetic-idp"
DEFAULT_PORT = 8765
BASE_SCOPES = "openid profile offline_access"
LOGIN_TIMEOUT_SEC = 300
LOCK_STALE_SEC = 60


class IdPError(RuntimeError):
    """Sign-in or refresh through Epicor IdP failed (message says what to do)."""


def discover(server_url: str, timeout: int = 30) -> Dict[str, str]:
    """IdP settings for a Kinetic server: tenant, endpoints, and the scope Kinetic's API needs."""
    base = server_url.rstrip("/")
    resp = requests.get(f"{base}/api/.configuration", timeout=timeout)
    resp.raise_for_status()
    idp = (resp.json() or {}).get("IdentityProvider") or {}
    if not idp.get("Enabled") or not idp.get("Endpoint"):
        raise IdPError(f"Epicor IdP sign-in is not enabled on {base}; use password sign-in ('auth renew').")
    endpoint = idp["Endpoint"].rstrip("/")
    disc = requests.get(f"{endpoint}/.well-known/openid-configuration", timeout=timeout).json()
    return {
        "endpoint": endpoint,
        "tenant": urllib.parse.urlparse(endpoint).path.rstrip("/").rsplit("/", 1)[-1],
        "authorize": disc["authorization_endpoint"],
        "token": disc["token_endpoint"],
        "api_scope": idp.get("ApiScope") or "epicor_erp",
    }


def _client_key(tenant: str) -> str:
    return f"tenant:{tenant}"


def get_client_id(tenant: str) -> str:
    return keyring.get_password(SERVICE_IDP, _client_key(tenant)) or ""


def set_client_id(tenant: str, client_id: str) -> None:
    keyring.set_password(SERVICE_IDP, _client_key(tenant), client_id.strip())


def _pkce() -> tuple:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def login_browser(
    settings: Dict[str, str],
    client_id: str,
    port: int = DEFAULT_PORT,
    timeout: int = LOGIN_TIMEOUT_SEC,
    open_url: Callable[[str], bool] = webbrowser.open,
) -> Dict:
    """Authorization code + PKCE with a loopback redirect. Returns the token response (not stored here)."""
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    result: Dict[str, str] = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Sign-in received. You can close this tab.")

        def log_message(self, *args):
            pass

    try:
        server = http.server.HTTPServer(("127.0.0.1", port), Callback)
    except OSError as exc:
        raise IdPError(f"Port {port} is busy; pass --port with one of the client's registered redirect ports.") from exc
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = settings["authorize"] + "?" + urllib.parse.urlencode({
            "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
            "scope": f"{BASE_SCOPES} {settings['api_scope']}", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        print(f"Opening the browser for Epicor sign-in (waiting up to {timeout // 60} min)...", flush=True)
        if not open_url(url):
            print("Couldn't open a browser; open this address yourself:\n" + url, flush=True)
        deadline = time.time() + timeout
        while not result and time.time() < deadline:
            time.sleep(0.25)
    finally:
        server.shutdown()
        server.server_close()

    if not result:
        raise IdPError("No sign-in came back in time. If the IdP showed an error page, check the client's "
                       f"redirect URIs include {redirect_uri}.")
    if "error" in result:
        raise IdPError(f"IdP refused the sign-in: {result['error']} {result.get('error_description', '')}".strip())
    if result.get("state") != state:
        raise IdPError("Sign-in response didn't match this request (state mismatch); try again.")
    return _token_request(settings["token"], {
        "grant_type": "authorization_code", "code": result.get("code", ""), "redirect_uri": redirect_uri,
        "client_id": client_id, "code_verifier": verifier})


def refresh(token_endpoint: str, client_id: str, refresh_token: str) -> Dict:
    """Exchange a refresh token. With rotation the response carries the new refresh token to store."""
    return _token_request(token_endpoint, {"grant_type": "refresh_token", "refresh_token": refresh_token,
                                           "client_id": client_id})


def _token_request(token_endpoint: str, data: Dict[str, str]) -> Dict:
    resp = requests.post(token_endpoint, data=data, timeout=30)
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if not resp.ok or "access_token" not in body:
        raise IdPError(f"IdP token request failed: {resp.status_code} {body.get('error', '')} "
                       f"{body.get('error_description', '')}".strip())
    return body


def session_meta(tok: Dict, settings: Dict[str, str], user_id: str, nickname: str, company: str) -> Dict:
    """Token-slot metadata in the shape the rest of auth.py reads (AccessToken, expires_in, _local_timestamp)."""
    now = time.time()
    meta = {
        "AccessToken": tok["access_token"],
        "expires_in": int(tok.get("expires_in") or 3600),
        "refresh_token": tok.get("refresh_token", ""),
        "auth_method": "idp",
        "idp_tenant": settings["tenant"],
        "idp_token_endpoint": settings["token"],
        "_local_timestamp": now,
        "_last_used": now,
        "user_id": user_id.lower(),
        "env_name": nickname,
    }
    if company:
        meta["current_company"] = company
    return meta


# --- Token-slot storage -------------------------------------------------------------------------------------
# Windows Credential Manager caps a secret at 2560 bytes, and keyring stores UTF-16, so about 1280 characters.
# An IdP session (JWT access token + refresh token + metadata) is larger. So the slot's "current_token" entry
# keeps the metadata only, marked "_idp_split", and the two tokens go to chunked side entries in the same slot.
SECRET_FIELDS = ("AccessToken", "refresh_token")
CHUNK_CHARS = 1000


def _side(field: str) -> str:
    return f"idp_{field}"


def _put_chunked(service: str, name: str, value: str) -> None:
    old = keyring.get_password(service, name) or ""
    chunks = [value[i:i + CHUNK_CHARS] for i in range(0, len(value), CHUNK_CHARS)] or [""]
    for i, part in enumerate(chunks):
        keyring.set_password(service, f"{name}#{i}", part)
    keyring.set_password(service, name, str(len(chunks)))
    for i in range(len(chunks), int(old) if old.isdigit() else 0):
        with contextlib.suppress(Exception):
            keyring.delete_password(service, f"{name}#{i}")


def _get_chunked(service: str, name: str) -> str:
    count = keyring.get_password(service, name) or ""
    if not count.isdigit():
        return ""
    parts = [keyring.get_password(service, f"{name}#{i}") for i in range(int(count))]
    return "" if any(p is None for p in parts) else "".join(parts)


def _delete_chunked(service: str, name: str) -> None:
    count = keyring.get_password(service, name) or ""
    for i in range(int(count) if count.isdigit() else 0):
        with contextlib.suppress(Exception):
            keyring.delete_password(service, f"{name}#{i}")
    with contextlib.suppress(Exception):
        keyring.delete_password(service, name)


def pack(slot: str, meta: Dict) -> Dict:
    """Store an IdP session's tokens in side entries; return the small metadata to keep in "current_token"."""
    if meta.get("auth_method") != "idp":
        return meta
    for field in SECRET_FIELDS:
        _put_chunked(slot, _side(field), str(meta.get(field) or ""))
    stub = {k: v for k, v in meta.items() if k not in SECRET_FIELDS and k not in ("_is_valid", "_remaining")}
    stub["_idp_split"] = True
    return stub


def unpack(slot: str, data: Dict) -> Dict:
    """Inverse of pack (no-op for other sessions)."""
    if data.get("_idp_split"):
        for field in SECRET_FIELDS:
            data[field] = _get_chunked(slot, _side(field))
    return data


def delete_secrets(slot: str) -> None:
    """Remove an IdP session's token side entries (call wherever a slot's "current_token" is deleted)."""
    for field in SECRET_FIELDS:
        _delete_chunked(slot, _side(field))


def move_secrets(old_slot: str, new_slot: str) -> None:
    for field in SECRET_FIELDS:
        value = _get_chunked(old_slot, _side(field))
        if value:
            _put_chunked(new_slot, _side(field), value)


@contextlib.contextmanager
def slot_lock(slot: str, wait: float = 30.0) -> Iterator[None]:
    """Cross-process lock around a refresh, so a rotated refresh token is used exactly once."""
    name = "kinetic-idp-" + hashlib.sha256(slot.encode()).hexdigest()[:16] + ".lock"
    path = os.path.join(tempfile.gettempdir(), name)
    deadline = time.time() + wait
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > LOCK_STALE_SEC:
                    os.remove(path)
                    continue
            except OSError:
                continue
            if time.time() > deadline:
                raise IdPError("Timed out waiting for another process to finish refreshing the IdP session.")
            time.sleep(0.2)
    try:
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


def refresh_session(meta: Dict, reread: Callable[[], Optional[Dict]], store: Callable[[Dict], None],
                    slot: str, force: bool = False) -> Optional[str]:
    """Renew an IdP session: lock, re-read (another process may have refreshed), refresh, store. Token or None.

    force: the caller's token was rejected (401) or a renewal was asked for, so a stored token that is still
    within its lifetime only counts if it differs from the caller's (i.e. another process already refreshed).
    """
    with slot_lock(slot):
        current = reread() or meta
        fresh = current.get("_is_valid") and current.get("AccessToken")
        if fresh and (not force or current["AccessToken"] != meta.get("AccessToken")):
            return current["AccessToken"]
        rt = current.get("refresh_token")
        tenant = current.get("idp_tenant", "")
        client_id = get_client_id(tenant) if tenant else ""
        if not rt or not client_id or not current.get("idp_token_endpoint"):
            return None
        try:
            tok = refresh(current["idp_token_endpoint"], client_id, rt)
        except IdPError:
            return None
        updated = dict(current)
        for k in ("_is_valid", "_remaining"):
            updated.pop(k, None)
        now = time.time()
        updated.update(AccessToken=tok["access_token"], expires_in=int(tok.get("expires_in") or 3600),
                       refresh_token=tok.get("refresh_token") or rt, _local_timestamp=now, _last_used=now)
        store(updated)
        return updated["AccessToken"]
