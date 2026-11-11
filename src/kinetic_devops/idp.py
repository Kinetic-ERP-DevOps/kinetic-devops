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
import json
import os
import secrets
import stat
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
CLOCK_SKEW_SEC = 120


class IdPError(RuntimeError):
    """Sign-in or refresh through Epicor IdP failed (message says what to do). error: the OAuth error code, if any."""

    def __init__(self, message: str, error: str = ""):
        super().__init__(message)
        self.error = error


def _https(url: str, what: str) -> urllib.parse.ParseResult:
    """Every address outside this machine must be https (the loopback redirect is the only http one, RFC 8252)."""
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise IdPError(f"{what} must be an https:// address (got {url!r}); refusing to use it for sign-in.")
    return parsed


def _same_url(a: str, b: str) -> bool:
    """URLs equal apart from the case of scheme and host and a trailing slash."""
    pa, pb = urllib.parse.urlparse(a), urllib.parse.urlparse(b)
    return ((pa.scheme.lower(), (pa.netloc or "").lower(), pa.path.rstrip("/"))
            == (pb.scheme.lower(), (pb.netloc or "").lower(), pb.path.rstrip("/")))


REDIRECTS = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 5


def _get_json(url: str, what: str, timeout: int) -> Dict:
    """GET over https with certificate checks; refuse an error status, or a redirect that leaves https.

    Redirects are followed here, not by requests, so a Location that isn't https is refused before anything is sent
    to it (requests would make the plain-http request first and only then let us look at it).
    """
    _https(url, what)
    for _ in range(MAX_REDIRECTS + 1):
        resp = requests.get(url, timeout=timeout, verify=True, allow_redirects=False)
        location = resp.headers.get("Location") or resp.headers.get("location")
        if resp.status_code not in REDIRECTS or not location:
            break
        url = urllib.parse.urljoin(url, location)
        _https(url, f"{what} redirect target")
    else:
        raise IdPError(f"{what}: more than {MAX_REDIRECTS} redirects ({url}).")
    if not resp.ok:
        raise IdPError(f"{what} answered HTTP {resp.status_code} ({url}).")
    try:
        body = resp.json()
    except ValueError as exc:
        raise IdPError(f"{what} is not JSON ({url}).") from exc
    if not isinstance(body, dict):
        raise IdPError(f"{what} is not a JSON object ({url}).")
    return body


def discover(server_url: str, timeout: int = 30) -> Dict[str, str]:
    """IdP settings for a Kinetic server: tenant, issuer, endpoints, and the scope Kinetic's API needs.

    Checks (OIDC Discovery 1.0, 4.3): https everywhere, the discovery document's issuer is the endpoint Kinetic
    names, and the authorize and token endpoints are on the issuer's host (a document that sends the code or the
    refresh token anywhere else is refused).
    """
    base = server_url.rstrip("/")
    _https(base, "The Kinetic server URL")
    idp = _get_json(f"{base}/api/.configuration", "Kinetic's /api/.configuration", timeout).get("IdentityProvider") or {}
    if not idp.get("Enabled") or not idp.get("Endpoint"):
        raise IdPError(f"Epicor IdP sign-in is not enabled on {base}; use password sign-in ('auth renew').")
    endpoint = str(idp["Endpoint"]).rstrip("/")
    _https(endpoint, "The IdP endpoint in /api/.configuration")
    disc = _get_json(f"{endpoint}/.well-known/openid-configuration", "The IdP discovery document", timeout)
    issuer = str(disc.get("issuer") or "")
    if not _same_url(issuer, endpoint):
        raise IdPError(f"The IdP discovery document's issuer ({issuer or 'missing'}) is not the IdP endpoint Kinetic "
                       f"names ({endpoint}); refusing to sign in.")
    host = _https(issuer, "The IdP issuer").hostname
    for key in ("authorization_endpoint", "token_endpoint"):
        if _https(disc.get(key), f"The IdP's {key}").hostname != host:
            raise IdPError(f"The IdP's {key} ({disc.get(key)}) is not on the issuer's host ({host}); refusing.")
    return {
        "endpoint": endpoint,
        "issuer": issuer,
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
    """Authorization code + PKCE with a loopback redirect. Returns the token response (not stored here).

    The listener takes exactly one callback: the first with this request's state (constant-time compare). Anything
    else gets 400 and is ignored, so another local program or a web page can't end or hijack the sign-in: a wrong or
    missing state, a second callback, or a Host header other than 127.0.0.1:<port> (DNS rebinding: a page on another
    name that resolves to 127.0.0.1). An iss parameter (RFC 9207), if the IdP sends one, must be the issuer.
    """
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    expected_host = f"127.0.0.1:{port}"
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    result: Dict[str, str] = {}
    taken = threading.Lock()

    class Callback(http.server.BaseHTTPRequestHandler):
        def _reply(self, status: int, text: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")      # the URL carried the code: keep it out of caches
            self.send_header("Referrer-Policy", "no-referrer")  # and out of Referer headers
            self.end_headers()
            self.wfile.write(text.encode())

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                return self._reply(404, "Not found.")
            if self.headers.get("Host", "") != expected_host:
                return self._reply(400, "Wrong host for this sign-in.")
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            with taken:
                if result:
                    return self._reply(400, "This sign-in was already received.")
                if not secrets.compare_digest(query.get("state", "").encode(), state.encode()):
                    return self._reply(400, "This response doesn't belong to the sign-in in progress.")
                result.update(query)
            self._reply(200, "Sign-in received. You can close this tab.")

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
            "scope": f"{BASE_SCOPES} {settings['api_scope']}", "state": state, "nonce": nonce,
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
    if "iss" in result and not _same_url(result["iss"], settings["issuer"]):
        raise IdPError(f"The sign-in response came from another issuer ({result['iss']}); refusing (RFC 9207).")
    if "error" in result:
        raise IdPError(f"IdP refused the sign-in: {result['error']} {result.get('error_description', '')}".strip())
    tok = _token_request(settings["token"], {
        "grant_type": "authorization_code", "code": result.get("code", ""), "redirect_uri": redirect_uri,
        "client_id": client_id, "code_verifier": verifier})
    claims = check_id_token(tok, settings["issuer"], client_id, nonce)
    tok["idp_identity"] = {"sub": str(claims.get("sub") or ""), "username": identity_name(claims)}
    return tok


def _jwt_claims(token: str) -> Dict:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise IdPError("The IdP's ID token is not a readable JWT; refusing the sign-in.") from exc
    if not isinstance(claims, dict):
        raise IdPError("The IdP's ID token has no claims object; refusing the sign-in.")
    return claims


def check_id_token(tok: Dict, issuer: str, client_id: str, nonce: str, now: Optional[float] = None) -> Dict:
    """Check the ID token from the code exchange (OIDC Core 3.1.3.7); return its claims.

    iss is the discovery issuer, aud holds this client (with several audiences, azp must be this client), exp is in
    the future (CLOCK_SKEW_SEC allowed) and nonce is this sign-in's. No signature check: the token came straight from
    the token endpoint over https with certificate checks, which 3.1.3.7 (6) accepts instead.
    """
    raw = tok.get("id_token")
    if not raw:
        raise IdPError("The IdP returned no ID token for an OpenID sign-in; refusing the sign-in.")
    claims = _jwt_claims(str(raw))
    now = time.time() if now is None else now
    if not isinstance(claims.get("iss"), str) or not _same_url(claims["iss"], issuer):
        raise IdPError(f"The ID token was issued by {claims.get('iss')!r}, not {issuer}; refusing the sign-in.")
    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else [a for a in aud if isinstance(a, str)] if isinstance(aud, list) else []
    if client_id not in audiences:
        raise IdPError("The ID token is not for this client (aud); refusing the sign-in.")
    if (len(audiences) > 1 or "azp" in claims) and claims.get("azp") != client_id:
        raise IdPError("The ID token was issued to another party (azp); refusing the sign-in.")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp + CLOCK_SKEW_SEC < now:
        raise IdPError("The ID token has expired (exp); check this computer's clock, then sign in again.")
    if not secrets.compare_digest(str(claims.get("nonce") or "").encode(), nonce.encode()):
        raise IdPError("The ID token doesn't belong to this sign-in (nonce); refusing the sign-in.")
    return claims


def identity_name(claims: Dict) -> str:
    """The sign-in name a person recognises, from the usual OIDC / Azure AD claims."""
    for key in ("preferred_username", "email", "upn", "unique_name", "name"):
        if isinstance(claims.get(key), str) and claims[key].strip():
            return claims[key].strip()
    return ""


def identity_matches(identity: str, user_id: str) -> bool:
    """Does the IdP sign-in name look like the Epicor user ID naming the session (e.g. jdoe vs jdoe@corp.example)?"""
    who, user = identity.strip().lower(), user_id.strip().lower()
    if not who or not user:
        return True   # nothing to compare: no warning
    local = who.split("@", 1)[0].rsplit("\\", 1)[-1]
    return user == local or user in who or local in user


def refresh(token_endpoint: str, client_id: str, refresh_token: str) -> Dict:
    """Exchange a refresh token. With rotation the response carries the new refresh token to store."""
    return _token_request(token_endpoint, {"grant_type": "refresh_token", "refresh_token": refresh_token,
                                           "client_id": client_id})


def _token_request(token_endpoint: str, data: Dict[str, str]) -> Dict:
    """POST to the token endpoint. Every failure is an IdPError (callers then ask for a new sign-in), not a traceback."""
    _https(token_endpoint, "The IdP token endpoint")   # also guards a stored endpoint used for refreshes
    try:
        resp = requests.post(token_endpoint, data=data, timeout=30, verify=True, allow_redirects=False)
    except requests.RequestException as exc:
        raise IdPError(f"Couldn't reach the IdP token endpoint ({type(exc).__name__}); check the network, "
                       "then sign in again.") from exc
    try:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    except ValueError as exc:
        raise IdPError(f"The IdP token endpoint answered HTTP {resp.status_code} with malformed JSON.") from exc
    if not isinstance(body, dict):
        body = {}
    if not resp.ok or "access_token" not in body:
        raise IdPError(f"IdP token request failed: {resp.status_code} {body.get('error', '')} "
                       f"{body.get('error_description', '')}".strip(), error=str(body.get("error") or ""))
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
    identity = tok.get("idp_identity") or {}
    if identity.get("sub"):
        meta["idp_subject"] = identity["sub"]
    if identity.get("username"):
        meta["idp_username"] = identity["username"]
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


def lock_dir() -> str:
    """Directory for refresh locks, private to this user.

    A shared temp directory would let any local user pre-create a session's lock (refreshes then time out) or remove
    it. Windows: the per-user temp directory. POSIX: $XDG_RUNTIME_DIR/kinetic-devops, else <tmp>/kinetic-devops-<uid>,
    created 0700 and refused unless it is a real directory owned by this user that nobody else can access.
    """
    if os.name == "nt":
        path = os.path.join(tempfile.gettempdir(), "kinetic-devops")
        os.makedirs(path, exist_ok=True)
        return path
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    path = (os.path.join(runtime, "kinetic-devops") if runtime and os.path.isdir(runtime)
            else os.path.join(tempfile.gettempdir(), f"kinetic-devops-{uid}"))
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or st.st_mode & 0o077:
        raise IdPError(f"Refusing the lock directory {path}: it must be a directory you own with mode 0700. "
                       "Remove it and retry.")
    return path


def _try_lock(fd: int) -> bool:
    """Take an exclusive OS lock on the open file without waiting; False if another holder has it."""
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   # byte 0 (the file is empty: locking past EOF is allowed)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:   # BlockingIOError / PermissionError: held by another open of the file
        return False


def _unlock(fd: int) -> None:
    with contextlib.suppress(OSError):
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def slot_lock(slot: str, wait: float = 30.0) -> Iterator[None]:
    """Cross-process lock around a refresh, so a rotated refresh token is used exactly once.

    An OS file lock (flock on POSIX, msvcrt.locking on Windows) on a per-slot file that is never deleted. The OS
    drops the lock when its holder closes the file or dies, so a crashed process can't leave a stale lock and no
    one ever has to guess that a lock is stale and take it over: a slow holder keeps its lock however long it
    takes (a takeover after a timeout would let two processes use the same refresh token, and the old holder's
    clean-up would delete the new holder's lock). Each acquisition opens the file anew, so threads exclude each
    other too.
    """
    name = "kinetic-idp-" + hashlib.sha256(slot.encode()).hexdigest()[:16] + ".lock"
    path = os.path.join(lock_dir(), name)
    deadline = time.time() + wait
    while True:
        fd = None
        with contextlib.suppress(OSError):   # can't open it now: wait like for a held lock; never retry at once
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            if _try_lock(fd):
                break
            os.close(fd)
        if time.time() > deadline:
            raise IdPError("Timed out waiting for another process to finish refreshing the IdP session.")
        time.sleep(0.2)
    try:
        yield
    finally:
        _unlock(fd)
        os.close(fd)


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
        except IdPError as exc:
            if exc.error == "invalid_grant":
                # The IdP rejected this refresh token for good (expired, revoked or already used): drop it, so later
                # runs go straight to "sign in again" instead of sending a dead token every time.
                dropped = {k: v for k, v in current.items() if k not in ("_is_valid", "_remaining")}
                dropped["refresh_token"] = ""
                store(dropped)
            return None
        updated = dict(current)
        for k in ("_is_valid", "_remaining"):
            updated.pop(k, None)
        now = time.time()
        updated.update(AccessToken=tok["access_token"], expires_in=int(tok.get("expires_in") or 3600),
                       refresh_token=tok.get("refresh_token") or rt, _local_timestamp=now, _last_used=now)
        store(updated)
        return updated["AccessToken"]
