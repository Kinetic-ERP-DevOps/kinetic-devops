# Signing in through Epicor IdP

`kinetic_devops.auth` can sign in to a Kinetic environment through **Epicor IdP** (Epicor Identity Provider)
instead of an Epicor user name and password:

- You sign in in the **browser**, with whatever your IdP tenant allows (Azure AD, SAML, ...). With an existing
  browser session it usually completes without a click.
- **No password** is typed into, passed through or stored by the CLI.
- The session renews itself with a **refresh token**, so background jobs, scripts and agents keep working without a
  prompt until the refresh token's lifetime runs out.

Password sign-in (`auth renew`) stays the default. IdP sign-in is opt-in, per session.

## How it works

1. `GET <server>/api/.configuration` (anonymous; Kinetic's login page reads it too) gives the environment's IdP
   endpoint and the scope Kinetic's API needs (`epicor_erp idp_access`).
2. `auth idp-login` runs the OAuth 2.0 **authorization code flow with PKCE** and a **loopback redirect**
   (RFC 8252): it listens on `http://127.0.0.1:<port>/callback`, opens the browser at the IdP, and exchanges the
   returned code for tokens.
3. The access token is checked against a Kinetic business object, then stored in the same keyring slot as a
   password session, so every command and script uses it unchanged: `Authorization: Bearer <token>` plus the
   environment's API key.
4. When the access token expires (usually after an hour), the SDK uses the refresh token. Refresh tokens can be
   **one-time use** (rotated on every refresh), so a refresh takes a per-session lock and re-reads the stored
   token first. That way parallel processes never spend each other's refresh token.
5. If the refresh token has expired too, an interactive terminal opens the browser sign-in again. Without a
   terminal, the command stops with a message telling you to run `auth idp-login`; it never waits for input.

## One-time setup: register a client in your IdP tenant

Epicor's built-in clients (the ones Kinetic's web and mobile apps use) don't allow sign-in from other
applications, so each IdP tenant needs **its own client** for this tool. An IdP tenant administrator does this
once per tenant. Every environment that uses the tenant shares the client.

1. Open the IdP admin console: <https://login.epicor.com/admin/#/home>.
2. **Manage Apps** → **Add New** → **Custom Client** → **Native Application**.
3. Enter a name (e.g. `kinetic-devops CLI`). **Tab out of the field**, then click the **right arrow** at the bottom
   of the screen. Check **Complete**.
4. Go to **Applications**, find the new client in the list, open the row's **overflow menu** → **Edit**, and click
   **Advanced mode** (upper right).
5. Set:

   | Setting | Value |
   |---|---|
   | Client type | Native / public, **no client secret**, **require PKCE** |
   | Grant types | Authorization code, refresh token |
   | Redirect URIs | `http://127.0.0.1:8765/callback` (add a second port, e.g. `8766`, as a spare) |
   | Scopes | `openid`, `profile`, `epicor_erp`, `idp_access`; plus **allow offline access** (that is what issues refresh tokens) |
   | Refresh token usage | **One-time** (rotation) |
   | Refresh token expiration | Sliding, with the **absolute** lifetime **longer** than the sliding one (see below) |
   | External providers | Whatever your organisation signs in with (e.g. Azure AD, SAML) |
   | Client URI / Logo URI | Optional, shown on the consent screen; public https addresses only |

   Leave out scopes the tool doesn't use (e.g. `epicor_saas`, preview/POC scopes): a token should carry only
   what it needs.

6. Copy the **client ID**. It isn't a secret, but it is specific to your tenant: keep it out of the repository,
   issues and chat.

**Lifetimes.** The form doesn't show units, so check them before relying on them. Each use of a *sliding*
lifetime extends the session, up to the *absolute* lifetime. If the two are equal, the sliding part does nothing
and you sign in again every *absolute* period. One way to find the unit: sign in, leave the session idle a little
longer than the sliding value would be in minutes, then run any command. If it still works without a sign-in, the
unit is larger than minutes.

## Using it

```sh
# once per IdP tenant: store the client ID (prompted, hidden; kept in the OS keyring)
uv run python -m kinetic_devops.auth idp-client MyEnv

# sign in (opens the browser); --user names the session, as with password sign-in
uv run python -m kinetic_devops.auth idp-login MyEnv --user jdoe --co ACME

# or let it ask: the environment from the configured list, then (after the browser sign-in) the Epicor User ID,
# offering your IdP sign-in name (jdoe for jdoe@corp.example), and the company if there are several
uv run python -m kinetic_devops.auth idp-login

# start a new session, reusing nothing from before (e.g. to sign in as someone else): no list of existing
# sessions, KIN_USER is ignored, and the IdP asks for the sign-in again instead of reusing the browser's
uv run python -m kinetic_devops.auth idp-login --new-session

# from then on, everything works as with a password session, e.g.
KIN_USER=jdoe uv run python -m kinetic_devops.baq ...
uv run python -m kinetic_devops.auth renew MyEnv --user jdoe   # forces a refresh now
```

Use `--port` if 8765 is taken. The port must be one of the client's registered redirect URIs.

Anything passed on the command line (or `KIN_USER`) is used without asking. Without a terminal, a missing
environment or user stops at once with what to pass (before the browser opens); the company then defaults to the
first configured.

`--new-session` asks for the Epicor User ID (offering your IdP sign-in name) and the company even when sessions
exist or `KIN_USER` is set, and the environment if ENV isn't given; ENV, `--user` and `--co` still count. It sends
`prompt=login` (OIDC), so the IdP asks for the sign-in again even if the browser is still signed in. It needs a
terminal: without one it stops before the browser opens; pass ENV, `--user` and `--co` instead.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| IdP error page about the redirect URI | The client doesn't list `http://127.0.0.1:<port>/callback` for the port in use. |
| `unauthorized_client` | The client doesn't allow the authorization-code grant, or it's not a native/public client. |
| `idp-login` signs in as the wrong person without asking | The browser is still signed in to the IdP; use `--new-session`. |
| Signed in, but "Kinetic didn't accept the token" | The IdP identity isn't mapped to an ERP user, or the environment's API key is wrong for the company. |
| No refresh token returned / sign-in every hour | Offline access isn't allowed on the client. |
| "session has expired and can't be refreshed" in a script | The refresh token's lifetime ran out (or the IdP rejected it); run `auth idp-login` in a terminal. |
| "issuer ... is not the IdP endpoint Kinetic names" | The server's `/api/.configuration` and the IdP's discovery document disagree; check the server's IdP settings. |
| "The ID token has expired (exp)" right after signing in | This computer's clock is off by more than two minutes. |
| "Refusing the lock directory" | The directory named exists but isn't yours with mode 0700; remove it and retry. |

## Security notes

- The redirect is plain `http` on purpose: it only goes to `127.0.0.1` on your own machine, never across the network
  (RFC 8252). Everything else is `https` with certificate checks: the Kinetic server, the IdP endpoint it names, the
  discovery document and the authorize/token endpoints. A redirect away from `https` is refused.
- Discovery is checked: the document's `issuer` must be the IdP endpoint Kinetic names, and the authorize and token
  endpoints must be on the issuer's host.
- The listener takes exactly one callback: the first with this sign-in's `state`. Anything else (a wrong or missing
  `state`, a second callback, a `Host` other than `127.0.0.1:<port>`, an `iss` from another issuer) gets an error page
  and is ignored, so another program or a web page can't end or take over the sign-in. PKCE stops another local
  program from using an intercepted code. The page is sent with `no-store` and `no-referrer`.
- The ID token is checked (issuer, audience, expiry, and a `nonce` tied to this sign-in). The session keeps the IdP
  identity (`idp_username`); `idp-login` warns if it doesn't look like the `--user` you named the session for.
- Refreshes are serialised with an OS file lock (released when its process ends, so it never goes stale and is
  never taken over) on a file in a directory only you can access (`$XDG_RUNTIME_DIR/kinetic-devops`
  or `<tmp>/kinetic-devops-<uid>`, mode 0700; the per-user temp directory on Windows). A refresh token the IdP
  rejects (`invalid_grant`) is dropped, so the next run asks for a new sign-in.
- Tokens are stored where password-session tokens are: the OS keyring (Windows Credential Manager by default).
  The refresh token is long-lived, so treat the keyring entry like a password. With the vault encrypted
  (`auth encrypt-vault`) the tokens are encrypted with it too; run `encrypt-vault` again to encrypt IdP sessions
  that earlier versions stored in clear. The client ID is stored per IdP tenant under the keyring service
  `kinetic-idp`.
- The tool never prints tokens. Revoke a session from the IdP side (or let it expire) if a machine is lost.
